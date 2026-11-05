"""Bounded clients and cleanup at the Kubernetes command boundary."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from roomkit_sandbox._k8s_stream import MAX_OUTPUT_BYTES, consume
from roomkit_sandbox.k8s_backend import KubernetesSandboxBackend
from tests.test_k8s_backend import MockCoreApi, MockStream, MockStreamResp


class EndlessStream(MockStreamResp):
    def __init__(self, chunk=""):
        super().__init__()
        self.chunk = chunk
        self.started = threading.Event()
        self.ended = threading.Event()
        self.updates = 0

    def is_open(self):
        return not self.closed

    def update(self, timeout=1):
        self.started.set()
        self.updates += 1
        if not self.chunk:
            time.sleep(min(timeout, 0.01))
        self._stdout = self.chunk

    def close(self):
        super().close()
        self.ended.set()


def backend_for(response):
    backend = KubernetesSandboxBackend(namespace="test")
    backend._core_api = MockCoreApi()
    backend._batch_api = backend._core_api
    backend._exec_api_factory = lambda: backend._core_api
    backend._stream = MockStream(response, api=backend._core_api)
    return backend


@pytest.mark.asyncio
async def test_timeout_stops_the_thread_and_closes_the_stream():
    response = EndlessStream()
    backend = backend_for(response)
    name = await backend.create_container("member")
    with pytest.raises(TimeoutError):
        await backend.exec_command(name, ["sleep", "60"], timeout=1)
    assert await asyncio.to_thread(response.ended.wait, 1)
    assert response.closed
    assert not backend._core_api.pods
    assert name in backend._core_api.claims
    updates = response.updates
    await asyncio.sleep(0.03)
    assert response.updates == updates


@pytest.mark.asyncio
async def test_cancellation_closes_the_stream_and_releases_the_worker():
    response = EndlessStream()
    backend = backend_for(response)
    name = await backend.create_container("member")
    call = asyncio.create_task(backend.exec_command(name, ["sleep", "60"]))
    assert await asyncio.to_thread(response.started.wait, 1)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert await asyncio.to_thread(response.ended.wait, 1)
    assert not backend._core_api.pods
    assert name in backend._core_api.claims


@pytest.mark.asyncio
async def test_replicas_cannot_run_two_commands_on_the_same_workspace():
    response = EndlessStream()
    backend = backend_for(response)
    name = await backend.create_container("member")
    other = backend_for(MockStreamResp())
    other._core_api = backend._core_api
    other._batch_api = backend._batch_api
    call = asyncio.create_task(backend.exec_command(name, ["sleep", "60"]))
    assert await asyncio.to_thread(response.started.wait, 1)
    with pytest.raises(RuntimeError, match="already has a command"):
        await other.exec_command(name, ["true"])
    assert not backend._core_api.deleted_pods
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert await asyncio.to_thread(response.ended.wait, 1)


@pytest.mark.parametrize("chunk", ["a" * 65536, "😀" * 16384])
def test_continuous_output_is_bounded_in_bytes(chunk):
    response = EndlessStream(chunk)
    with pytest.raises(RuntimeError, match="capture limit"):
        consume(response, None, time.monotonic() + 1, threading.Event())
    assert response.closed
    assert response.updates == MAX_OUTPUT_BYTES // len(chunk.encode()) + 1


def test_stopped_before_connection_does_not_send_credentials():
    response = MockStreamResp()
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(TimeoutError):
        consume(response, "sensitive payload", time.monotonic() + 1, stopped)
    assert response.stdin_written == []
    assert response.closed


def test_disconnection_without_status_is_not_success():
    response = MockStreamResp(exit_code=None)
    with pytest.raises(RuntimeError, match="without an exit status"):
        consume(response, None, time.monotonic() + 1, threading.Event())
    assert response.closed


@pytest.mark.asyncio
async def test_timeout_cannot_be_increased_without_bound():
    response = MockStreamResp()
    backend = backend_for(response)
    with pytest.raises(ValueError, match="300"):
        await backend.exec_command("sandbox", ["sleep", "100000"], timeout=100000)
    assert backend._stream.kwargs == {}
