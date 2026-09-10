"""Tests for KubernetesSandboxBackend using mocked K8s client."""

from __future__ import annotations

import pytest

from roomkit_sandbox.k8s_backend import (
    KubernetesSandboxBackend,
    _k8s_label,
    _k8s_label_value,
    _safe_pod_name,
)


def test_safe_pod_name():
    assert _safe_pod_name("sandbox:user123") == "sandbox-sandbox-user123"
    assert _safe_pod_name("test.session") == "sandbox-test-session"
    assert len(_safe_pod_name("a" * 100)) <= 63


def test_k8s_label():
    assert _k8s_label("app.tenant_id") == "app.tenant_id"
    assert _k8s_label("simple") == "simple"
    assert _k8s_label("key with spaces") == "key-with-spaces"
    assert len(_k8s_label("a" * 100)) <= 63


def test_k8s_label_value():
    assert _k8s_label_value("simple") == "simple"
    assert _k8s_label_value("sandbox:user123") == "sandbox-user123"
    assert _k8s_label_value("a-b_c.d") == "a-b_c.d"
    assert _k8s_label_value("has spaces!") == "has-spaces"
    assert len(_k8s_label_value("a" * 100)) <= 63


class MockPodStatus:
    def __init__(self, phase="Running", ready=True):
        self.phase = phase
        self.container_statuses = [MockContainerStatus(ready=ready)] if ready is not None else None


class MockContainerStatus:
    def __init__(self, ready=True):
        self.name = "sandbox"
        self.ready = ready


class MockPodMetadata:
    def __init__(self, name="test-pod"):
        self.name = name


class MockPod:
    def __init__(self, name="test-pod", phase="Running", ready=True):
        self.metadata = MockPodMetadata(name)
        self.status = MockPodStatus(phase, ready)


class MockPodList:
    def __init__(self, items=None):
        self.items = items or []


class MockCoreApi:
    def __init__(self):
        self.created_pods = []
        self.deleted_pods = []
        self._pods = {}

    def connect_get_namespaced_pod_exec(self, *args, **kwargs):
        pass

    def create_namespaced_pod(self, namespace, pod):
        name = pod.metadata.name
        self.created_pods.append(name)
        self._pods[name] = MockPod(name)

    def read_namespaced_pod(self, name, namespace):
        if name in self._pods:
            return self._pods[name]
        raise Exception("not found")

    def list_namespaced_pod(self, namespace, label_selector=""):
        return MockPodList(list(self._pods.values()))

    def delete_namespaced_pod(self, name, namespace):
        self.deleted_pods.append(name)
        self._pods.pop(name, None)


class MockStreamResp:
    def __init__(self, stdout="", stderr="", exit_code=0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = exit_code
        self._open = True

    def is_open(self):
        was_open = self._open
        self._open = False
        return was_open

    def update(self, timeout=1):
        pass

    def peek_stdout(self):
        return bool(self._stdout)

    def read_stdout(self):
        out = self._stdout
        self._stdout = ""
        return out

    def peek_stderr(self):
        return bool(self._stderr)

    def read_stderr(self):
        err = self._stderr
        self._stderr = ""
        return err


class MockStream:
    def __init__(self, result=None):
        self._result = result or MockStreamResp()

    def stream(self, *args, **kwargs):
        return self._result


@pytest.fixture
def backend():
    b = KubernetesSandboxBackend(namespace="test")
    b._core_api = MockCoreApi()
    b._stream = MockStream(MockStreamResp(stdout="hello\n", exit_code=0))
    return b


@pytest.mark.asyncio
async def test_create_container(backend):
    pod_name = await backend.create_container("test-session")
    assert pod_name == "sandbox-test-session"
    assert len(backend._core_api.created_pods) == 1


@pytest.mark.asyncio
async def test_create_container_with_labels(backend):
    pod_name = await backend.create_container("sess1", labels={"app.type": "sandbox"})
    assert pod_name.startswith("sandbox-")


@pytest.mark.asyncio
async def test_exec_command(backend):
    result = await backend.exec_command("sandbox-test", ["rtk", "ls"])
    assert result.exit_code == 0
    assert result.stdout == "hello\n"


@pytest.mark.asyncio
async def test_exec_command_failure(backend):
    backend._stream = MockStream(MockStreamResp(stderr="error", exit_code=1))
    result = await backend.exec_command("sandbox-test", ["rtk", "read", "/bad"])
    assert result.exit_code == 1
    assert result.stderr == "error"


@pytest.mark.asyncio
async def test_container_exists(backend):
    await backend.create_container("exists-test")
    assert await backend.container_exists("sandbox-exists-test")
    assert not await backend.container_exists("nonexistent")


@pytest.mark.asyncio
async def test_find_container(backend):
    await backend.create_container("find-test")
    found = await backend.find_container("find-test")
    assert found == "sandbox-find-test"


@pytest.mark.asyncio
async def test_find_container_not_found(backend):
    found = await backend.find_container("missing")
    assert found is None


@pytest.mark.asyncio
async def test_delete_container(backend):
    await backend.create_container("delete-test")
    await backend.delete_container("sandbox-delete-test")
    assert "sandbox-delete-test" in backend._core_api.deleted_pods


@pytest.mark.asyncio
async def test_list_containers_reports_every_managed_pod(backend):
    """The inventory verb: every pod, its labels, its phase, lower-cased."""
    await backend.create_container("inv-a", labels={"app.tenant_id": "t1"})
    await backend.create_container("inv-b", labels={"app.tenant_id": "t2"})

    infos = await backend.list_containers({"app.tenant_id": "t1"})

    # The mock ignores the selector, so the assertion is on the projection.
    names = {info.container_id for info in infos}
    assert {"sandbox-inv-a", "sandbox-inv-b"} <= names
    assert all(info.status == "running" for info in infos)
    assert all(info.session_id is None or isinstance(info.session_id, str) for info in infos)


def test_list_containers_builds_a_sanitized_selector(monkeypatch):
    """Filter keys and values go through the label sanitizer create used."""
    recorded = {}

    class _Api:
        def list_namespaced_pod(self, namespace, label_selector=""):
            recorded["selector"] = label_selector
            return MockPodList([])

    backend = KubernetesSandboxBackend(namespace="ns")
    backend._core_api = _Api()
    backend._stream = MockStream()
    import asyncio

    asyncio.run(backend.list_containers({"app.user_id": "user:1"}))
    assert recorded["selector"] == "managed-by=roomkit-sandbox,app.user_id=user-1"
