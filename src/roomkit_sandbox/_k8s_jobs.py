"""The Kubernetes backend's command lifecycle, owned by native Jobs.

The workspace backend supplies the authenticated SDK calls. This class owns no
workspace data and implements no scheduler or retry loop beside Kubernetes.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from roomkit_sandbox import _k8s_objects as objects
from roomkit_sandbox._k8s_stream import consume
from roomkit_sandbox._shared import ExecResult

# A blocked synchronous SDK handshake must not consume every API worker.
_CONNECTION_SLOTS = threading.BoundedSemaphore(4)


class CommandJobs:
    def __init__(
        self,
        call: Callable[..., Awaitable[Any]],
        optional: Callable[..., Awaitable[Any]],
        namespace: str,
        stream: Any,
        api_factory: Callable[[], Any],
    ) -> None:
        self._call = call
        self._optional = optional
        self._namespace = namespace
        self._stream = stream
        self._exec_api_factory = api_factory

    async def create(self, body: dict[str, Any]) -> Any:
        from kubernetes.client.exceptions import ApiException

        try:
            return await self._call("create_namespaced_job", body=body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            raise RuntimeError(
                "Sandbox workspace already has a command; an orphan is reclaimed "
                "by Kubernetes after its deadline and result retention period"
            ) from exc

    async def _read_same_pod(self, pod: Any) -> Any:
        current = await self._call("read_namespaced_pod", pod.metadata.name)
        if current.metadata.uid != pod.metadata.uid:
            raise RuntimeError("Sandbox command was replaced")
        return current

    async def ready(self, job: Any) -> Any:
        deadline = time.monotonic() + objects.STARTUP_SECONDS
        while time.monotonic() < deadline:
            pods = await self._call(
                "list_namespaced_pod",
                label_selector=(f"batch.kubernetes.io/controller-uid={job.metadata.uid}"),
            )
            if not pods.items:
                await asyncio.sleep(0.2)
                continue
            current = pods.items[0]
            if current.status.phase in {"Succeeded", "Failed"}:
                raise RuntimeError(f"Sandbox failed before attachment: {current.status.phase}")
            for status in current.status.container_statuses or []:
                if status.name == "sandbox" and status.ready:
                    return current
            await asyncio.sleep(0.2)
        raise TimeoutError("Sandbox pod did not become ready within 60s")

    async def shorten_deadline(self, resource: Any, method: str, timeout: int) -> None:
        from kubernetes.client.exceptions import ApiException

        if resource.status.start_time is None:
            raise RuntimeError("Sandbox resource has no start time")
        elapsed = (datetime.now(UTC) - resource.status.start_time).total_seconds()
        # Only shorten the creation-time deadline. The latter already bounds a
        # creator that disappears during image pulling or workspace setup.
        seconds = min(resource.spec.active_deadline_seconds, max(1, math.ceil(elapsed + timeout)))
        uid = resource.metadata.uid
        for attempt in range(3):
            current = resource.spec.active_deadline_seconds
            if current <= seconds:
                return
            try:
                await self._call(
                    method,
                    resource.metadata.name,
                    body=[
                        {"op": "test", "path": "/metadata/uid", "value": uid},
                        # Status updates do not compete with this write. Only a
                        # concurrent deadline change requires reconciliation.
                        {"op": "test", "path": "/spec/activeDeadlineSeconds", "value": current},
                        {"op": "replace", "path": "/spec/activeDeadlineSeconds", "value": seconds},
                    ],
                )
                return
            except ApiException as exc:
                if exc.status not in {409, 422} or attempt == 2:
                    raise
                resource = await self._call(
                    method.replace("patch_", "read_"), resource.metadata.name
                )
                if resource.metadata.uid != uid:
                    raise RuntimeError("Sandbox command was replaced") from exc

    async def attach(self, pod: Any, payload: str, timeout: int) -> ExecResult:
        stopped = threading.Event()
        deadline = time.monotonic() + timeout

        def run() -> ExecResult:
            if not _CONNECTION_SLOTS.acquire(blocking=False):
                raise RuntimeError("Sandbox connection capacity is exhausted")
            api = None
            try:
                if stopped.is_set():
                    raise TimeoutError("Sandbox attachment cancelled")
                api = self._exec_api_factory()
                response = self._stream.stream(
                    api.connect_get_namespaced_pod_attach,
                    pod.metadata.name,
                    self._namespace,
                    container="sandbox",
                    stderr=True,
                    stdin=True,
                    stdout=True,
                    tty=False,
                    _preload_content=False,
                )
                return consume(response, payload, deadline, stopped, expects_status=False)
            finally:
                if api is not None:
                    api.api_client.close()
                _CONNECTION_SLOTS.release()

        try:
            return await asyncio.wait_for(asyncio.to_thread(run), timeout=timeout)
        finally:
            stopped.set()

    async def exit_code(self, pod: Any) -> int:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = await self._read_same_pod(pod)
            if current.status.reason == "DeadlineExceeded":
                raise TimeoutError("Sandbox exceeded its Kubernetes deadline")
            for status in current.status.container_statuses or []:
                if status.name == "sandbox" and status.state and status.state.terminated:
                    return status.state.terminated.exit_code
            await asyncio.sleep(0.1)
        raise RuntimeError("Sandbox attachment closed without a terminated container")

    async def remove(self, job: Any) -> None:
        from kubernetes.client.exceptions import ApiException

        try:
            await self._call(
                "delete_namespaced_job",
                job.metadata.name,
                body={
                    "preconditions": {"uid": job.metadata.uid},
                    "propagationPolicy": "Foreground",
                },
            )
        except ApiException as exc:
            if exc.status not in {404, 409}:
                raise
            if exc.status == 409:
                current = await self._optional("read_namespaced_job", job.metadata.name)
                if current is not None and current.metadata.uid == job.metadata.uid:
                    raise
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = await self._optional("read_namespaced_job", job.metadata.name)
            if current is None or current.metadata.uid != job.metadata.uid:
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Sandbox Job deletion was not acknowledged; workspace retained")
        # PVC deletion can make GC remove the Job before we ask. Its generated
        # pods still belong to this immutable UID, never the next command's.
        pods = await self._call(
            "list_namespaced_pod",
            label_selector=(f"batch.kubernetes.io/controller-uid={job.metadata.uid}"),
        )
        for pod in pods.items:
            await self.remove_pod(pod)

    async def remove_pod(self, pod: Any) -> None:
        from kubernetes.client.exceptions import ApiException

        try:
            await self._call(
                "delete_namespaced_pod",
                pod.metadata.name,
                body={"preconditions": {"uid": pod.metadata.uid}},
            )
        except ApiException as exc:
            if exc.status == 404:
                return
            raise
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = await self._optional("read_namespaced_pod", pod.metadata.name)
            if current is None or current.metadata.uid != pod.metadata.uid:
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("Sandbox pod deletion was not acknowledged; workspace retained")
