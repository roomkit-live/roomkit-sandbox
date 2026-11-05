"""The Kubernetes boundary: persistent files, one bounded pod per command."""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import threading
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException

from roomkit_sandbox import _k8s_objects as objects
from roomkit_sandbox._k8s_jobs import CommandJobs
from roomkit_sandbox.k8s_backend import KubernetesSandboxBackend, _env_payload, _k8s_label_value


def model(kind, data):
    with ApiClient() as api:
        return api.deserialize(SimpleNamespace(data=json.dumps(data)), kind)


class MockCoreApi:
    def __init__(self):
        self.claims = {}
        self.secrets = {}
        self.pods = {}
        self.jobs = {}
        self.created_pods = []
        self.deleted_pods = []
        self.patches = []
        self.api_client = SimpleNamespace(close=lambda: None)
        self.lock = threading.Lock()

    def _create(self, storage, kind, body):
        with self.lock:
            name = body["metadata"]["name"]
            if name in storage:
                raise ApiException(status=409)
            data = json.loads(json.dumps(body))
            data["metadata"].update(uid=uuid.uuid4().hex, resourceVersion="1")
            if kind == "V1Pod":
                data["status"] = {
                    "phase": "Running",
                    "startTime": datetime.now(UTC).isoformat(),
                    "containerStatuses": [
                        {
                            "name": "sandbox",
                            "ready": True,
                            "restartCount": 0,
                            "image": "sandbox",
                            "imageID": "sha256:test",
                            "state": {"running": {}},
                        }
                    ],
                }
            elif kind == "V1Job":
                data["status"] = {"startTime": datetime.now(UTC).isoformat()}
            storage[name] = model(kind, data)
            return storage[name]

    def _read(self, storage, name):
        if name not in storage:
            raise ApiException(status=404)
        return storage[name]

    def _list(self, storage, selector):
        labels = dict(part.split("=", 1) for part in selector.split(",") if part)
        return SimpleNamespace(
            items=[
                item
                for item in storage.values()
                if all((item.metadata.labels or {}).get(k) == v for k, v in labels.items())
            ]
        )

    def create_namespaced_persistent_volume_claim(self, namespace, body, **kwargs):
        return self._create(self.claims, "V1PersistentVolumeClaim", body)

    def read_namespaced_persistent_volume_claim(self, name, namespace, **kwargs):
        return self._read(self.claims, name)

    def list_namespaced_persistent_volume_claim(self, namespace, label_selector="", **kwargs):
        return self._list(self.claims, label_selector)

    def delete_namespaced_persistent_volume_claim(self, name, namespace, body, **kwargs):
        assert self.claims[name].metadata.uid == body["preconditions"]["uid"]
        del self.claims[name]

    def create_namespaced_secret(self, namespace, body, **kwargs):
        return self._create(self.secrets, "V1Secret", body)

    def read_namespaced_secret(self, name, namespace, **kwargs):
        return self._read(self.secrets, name)

    def create_namespaced_pod(self, namespace, body, **kwargs):
        pod = self._create(self.pods, "V1Pod", body)
        self.created_pods.append(body)
        return pod

    def create_namespaced_job(self, namespace, body, **kwargs):
        job = self._create(self.jobs, "V1Job", body)
        template = json.loads(json.dumps(body["spec"]["template"]))
        template["metadata"]["name"] = job.metadata.name + "-" + job.metadata.uid[:5]
        template["metadata"]["labels"].update(
            {
                "batch.kubernetes.io/controller-uid": job.metadata.uid,
                "batch.kubernetes.io/job-name": job.metadata.name,
            }
        )
        self.create_namespaced_pod(namespace, template)
        return job

    def read_namespaced_job(self, name, namespace, **kwargs):
        return self._read(self.jobs, name)

    def patch_namespaced_job(self, name, namespace, body, **kwargs):
        job = self._read(self.jobs, name)
        assert body[0]["value"] == job.metadata.uid
        assert body[1]["value"] == job.spec.active_deadline_seconds
        assert body[2]["value"] <= job.spec.active_deadline_seconds
        job.spec.active_deadline_seconds = body[2]["value"]
        return job

    def delete_namespaced_job(self, name, namespace, body, **kwargs):
        job = self._read(self.jobs, name)
        if job.metadata.uid != body["preconditions"]["uid"]:
            raise ApiException(status=409)
        for pod in list(self.pods.values()):
            if (pod.metadata.labels or {}).get(
                "batch.kubernetes.io/controller-uid"
            ) == job.metadata.uid:
                self.delete_namespaced_pod(
                    pod.metadata.name, namespace, {"preconditions": {"uid": pod.metadata.uid}}
                )
        del self.jobs[name]

    def read_namespaced_pod(self, name, namespace, **kwargs):
        return self._read(self.pods, name)

    def list_namespaced_pod(self, namespace, label_selector="", **kwargs):
        return self._list(self.pods, label_selector)

    def patch_namespaced_pod(self, name, namespace, body, **kwargs):
        pod = self._read(self.pods, name)
        assert body[0] == {"op": "test", "path": "/metadata/uid", "value": pod.metadata.uid}
        assert body[1]["value"] == pod.spec.active_deadline_seconds
        assert body[2]["value"] <= pod.spec.active_deadline_seconds
        pod.spec.active_deadline_seconds = body[2]["value"]
        self.patches.append(body)
        return pod

    def delete_namespaced_pod(self, name, namespace, body, **kwargs):
        pod = self._read(self.pods, name)
        if pod.metadata.uid != body["preconditions"]["uid"]:
            raise ApiException(status=409)
        self.deleted_pods.append(name)
        del self.pods[name]

    def connect_get_namespaced_pod_attach(self, *args, **kwargs):
        pass


class MockStreamResp:
    def __init__(self, stdout="", stderr="", exit_code=0):
        self._stdout, self._stderr = stdout, stderr
        self.returncode = exit_code
        self._open = True
        self.stdin_written = []
        self.closed = False
        self.on_end = lambda: None

    def write_stdin(self, data):
        self.stdin_written.append(data)

    def is_open(self):
        return self._open

    def update(self, timeout=1):
        self._open = False
        self.on_end()

    def peek_stdout(self):
        return bool(self._stdout)

    def read_stdout(self):
        out, self._stdout = self._stdout, ""
        return out

    def peek_stderr(self):
        return bool(self._stderr)

    def read_stderr(self):
        out, self._stderr = self._stderr, ""
        return out

    def close(self):
        self.closed = True


class MockStream:
    def __init__(self, response=None, api=None):
        self.response = response or MockStreamResp(stdout="hello\n")
        self.api = api
        self.kwargs = {}

    def stream(self, method, name, namespace, **kwargs):
        self.kwargs = kwargs
        if self.api:

            def finish():
                self.api.pods[name].status.container_statuses[0].state = SimpleNamespace(
                    terminated=SimpleNamespace(exit_code=self.response.returncode)
                )
                self.api.pods[name].status.phase = "Succeeded"

            self.response.on_end = finish
        return self.response


@pytest.fixture
def backend():
    backend = KubernetesSandboxBackend(namespace="test", runtime_class="gvisor")
    backend._core_api = MockCoreApi()
    backend._batch_api = backend._core_api
    backend._exec_api_factory = lambda: backend._core_api
    backend._stream = MockStream(api=backend._core_api)
    return backend


@pytest.mark.asyncio
async def test_creation_owns_files_and_fixed_environment_without_starting_a_pod(backend):
    name = await backend.create_container("member", env={"SETTING": "value"})
    assert not backend._core_api.pods
    assert backend._core_api.secrets[name].string_data == {"SETTING": "value"}
    assert await backend.find_container("member") == name
    assert await backend.container_exists(name)
    assert await backend.create_container("member", env={"SETTING": "changed"}) == name
    assert backend._core_api.secrets[name].string_data == {"SETTING": "value"}


@pytest.mark.asyncio
async def test_interrupted_creation_is_not_discovered_until_its_environment_is_repaired(backend):
    create_secret = backend._core_api.create_namespaced_secret

    def unavailable(*args, **kwargs):
        raise ApiException(status=503)

    backend._core_api.create_namespaced_secret = unavailable
    with pytest.raises(ApiException):
        await backend.create_container("interrupted", env={"SETTING": "value"})
    assert len(backend._core_api.claims) == 1
    uid = next(iter(backend._core_api.claims.values())).metadata.uid
    assert await backend.find_container("interrupted") is None
    backend._core_api.create_namespaced_secret = create_secret
    name = await backend.create_container("interrupted", env={"SETTING": "value"})
    assert backend._core_api.claims[name].metadata.uid == uid
    assert await backend.find_container("interrupted") == name


@pytest.mark.asyncio
async def test_completed_result_is_owned_until_acknowledged(backend, monkeypatch):
    name = await backend.create_container("member")
    entered, release = asyncio.Event(), asyncio.Event()
    read_exit = CommandJobs.exit_code

    async def hold_result(self, pod):
        entered.set()
        await release.wait()
        return await read_exit(self, pod)

    monkeypatch.setattr(CommandJobs, "exit_code", hold_result)
    first = asyncio.create_task(backend.exec_command(name, ["true"]))
    await entered.wait()
    with pytest.raises(RuntimeError, match="already has a command"):
        await backend.exec_command(name, ["true"])
    assert not backend._core_api.deleted_pods
    release.set()
    assert (await first).exit_code == 0
    assert not backend._core_api.jobs


def test_job_controller_owns_pending_deadline_and_orphan_retention():
    workspace = SimpleNamespace(
        metadata=SimpleNamespace(name="workspace-test", uid="id", labels={})
    )
    job = objects.command_job(
        workspace,
        image="sandbox",
        service_account="none",
        image_pull_secret="",
        runtime_class="gvisor",
        command=["true"],
        workdir="/workspace",
        nonce="invocation",
        timeout=30,
    )
    assert job["spec"]["activeDeadlineSeconds"] == 90
    assert job["spec"]["ttlSecondsAfterFinished"] == 120
    assert job["spec"]["backoffLimit"] == 0
    assert "ownerReferences" not in job["spec"]["template"]["metadata"]
    assert "name" not in job["spec"]["template"]["metadata"]


@pytest.mark.asyncio
async def test_command_retires_processes_and_retains_workspace(backend):
    name = await backend.create_container("member")
    result = await backend.exec_command(name, ["rtk", "ls"])
    assert result.stdout == "hello\n" and result.exit_code == 0
    assert not backend._core_api.pods
    assert name in backend._core_api.claims
    assert await backend.find_container("member") == name
    assert backend._core_api.patches
    assert backend._stream.response.closed


@pytest.mark.asyncio
async def test_deadline_and_restricted_posture_exist_before_attachment(backend):
    name = await backend.create_container("member")
    await backend.exec_command(name, ["true"], timeout=12)
    spec = backend._core_api.created_pods[0]["spec"]
    assert spec["activeDeadlineSeconds"] == 12 + objects.STARTUP_SECONDS
    assert spec["terminationGracePeriodSeconds"] == 1
    assert spec["runtimeClassName"] == "gvisor"
    assert spec["automountServiceAccountToken"] is False
    assert spec["restartPolicy"] == "Never"
    for container in spec["containers"] + spec["initContainers"]:
        security = container["securityContext"]
        assert security["runAsNonRoot"] and security["runAsUser"] == 1000
        assert security["runAsGroup"] == 1000 and security["readOnlyRootFilesystem"]
        assert security["allowPrivilegeEscalation"] is False
        assert security["capabilities"]["drop"] == ["ALL"]
        assert security["seccompProfile"]["type"] == "RuntimeDefault"
    paths = {mount["mountPath"] for mount in spec["containers"][0]["volumeMounts"]}
    assert paths == {"/workspace", "/home/sandbox", "/opt/venv", "/tmp"}
    assert backend._core_api.patches[0][2]["value"] <= 13


@pytest.mark.asyncio
async def test_per_command_credentials_travel_only_on_attachment_stdin(backend):
    name = await backend.create_container("member")
    secret = "tok'en $HOME"
    await backend.exec_command(name, ["true"], env={"TOKEN": secret})
    assert secret not in json.dumps(backend._core_api.created_pods)
    assert secret not in str(backend._core_api.secrets[name])
    nonce, payload, _ = backend._stream.response.stdin_written[0].split("\n")
    assert nonce in backend._core_api.created_pods[0]["spec"]["containers"][0]["command"][2]
    assert "export TOKEN=" in base64.b64decode(payload).decode()


@pytest.mark.asyncio
async def test_a_missing_runtime_is_not_retried_with_native_execution(backend):
    name = await backend.create_container("member")

    def reject(namespace, body, **kwargs):
        assert body["spec"]["template"]["spec"]["runtimeClassName"] == "gvisor"
        raise ApiException(status=403, reason="RuntimeClass missing")

    backend._core_api.create_namespaced_job = reject
    with pytest.raises(ApiException, match="RuntimeClass missing"):
        await backend.exec_command(name, ["true"])
    assert backend._stream.kwargs == {}


@pytest.mark.asyncio
async def test_legacy_workspace_is_neither_reused_nor_deleted(backend):
    legacy = {
        "metadata": {
            "name": "sandbox-member",
            "labels": {"managed-by": "roomkit-sandbox", "session": "member"},
        },
        "spec": {"containers": [{"name": "sandbox", "image": "legacy"}]},
    }
    backend._core_api.create_namespaced_pod("test", legacy)
    with pytest.raises(RuntimeError, match="migrate its workspace"):
        await backend.create_container("member")
    with pytest.raises(RuntimeError, match="migrate its workspace"):
        await backend.find_container("member")
    assert "sandbox-member" in backend._core_api.pods
    assert not backend._core_api.claims


@pytest.mark.asyncio
async def test_inventory_and_explicit_teardown_include_idle_workspaces(backend):
    name = await backend.create_container("member", labels={"luge.tenant_id": "t1"})
    await backend.create_container("other", labels={"luge.tenant_id": "t2"})
    items = await backend.list_containers({"luge.tenant_id": "t1"})
    assert [(item.container_id, item.status) for item in items] == [(name, "ready")]
    await backend.delete_container(name)
    assert not await backend.container_exists(name)
    assert len(backend._core_api.claims) == 1


@pytest.mark.asyncio
async def test_teardown_stops_old_pods_when_gc_removed_their_job_and_a_new_workspace_exists(
    backend,
):
    name = await backend.create_container("member")
    api = backend._core_api
    original = api.claims[name]

    def create_job(workspace):
        body = objects.command_job(
            workspace,
            image="sandbox",
            service_account="none",
            image_pull_secret="",
            runtime_class="gvisor",
            command=["sleep", "60"],
            workdir="/workspace",
            nonce="invocation",
            timeout=30,
        )
        return api.create_namespaced_job("test", body)

    old_job = create_job(original)
    delete_claim = api.delete_namespaced_persistent_volume_claim

    def gc_then_recreate(*args, **kwargs):
        delete_claim(*args, **kwargs)
        del api.jobs[old_job.metadata.name]  # GC has not yet removed its pod.
        replacement = api.create_namespaced_persistent_volume_claim(
            "test", objects.claim("member", {}, None)
        )
        create_job(replacement)

    api.delete_namespaced_persistent_volume_claim = gc_then_recreate
    await backend.delete_container(name)
    assert api.claims[name].metadata.uid != original.metadata.uid
    assert len(api.jobs) == len(api.pods) == 1
    assert all(
        pod.metadata.labels[objects.WORKSPACE_UID] != original.metadata.uid
        for pod in api.pods.values()
    )


@pytest.mark.parametrize("first,second", [("a:b", "a-b"), ("x" * 100, "x" * 99 + "y")])
def test_workspace_identity_does_not_collapse_after_label_sanitization(first, second):
    assert objects.workspace_name(first) != objects.workspace_name(second)
    assert len(objects.command_name(objects.workspace_name(first))) <= 63


@pytest.mark.parametrize("nonce,expected", [("matching", "safe"), ("wrong", "")])
def test_attachment_is_bound_to_its_invocation(nonce, expected):
    workspace = SimpleNamespace(
        metadata=SimpleNamespace(name="workspace-test", uid="id", labels={})
    )
    pod = objects.command_pod(
        workspace,
        image="sandbox",
        service_account="none",
        image_pull_secret="",
        runtime_class="gvisor",
        command=["sh", "-c", "printf safe"],
        workdir="/tmp",
        nonce="matching",
        timeout=5,
    )
    result = subprocess.run(
        pod["spec"]["containers"][0]["command"],  # nosec B603
        input=nonce + "\n" + _env_payload({}),
        capture_output=True,
        text=True,
        timeout=1,
    )
    assert result.stdout == expected
    assert (result.returncode == 0) == (nonce == "matching")


def test_invalid_environment_is_rejected():
    with pytest.raises(ValueError):
        _env_payload({"BAD-NAME": "value"})
    with pytest.raises(ValueError):
        _env_payload({"HUGE": "x" * 65536})
    assert _k8s_label_value("member:1") == "member-1"


@pytest.mark.asyncio
async def test_status_updates_do_not_invalidate_deadline_changes(backend):
    name = await backend.create_container("member")
    patch = backend._core_api.patch_namespaced_job

    def status_update(job_name, namespace, body, **kwargs):
        backend._core_api.jobs[job_name].metadata.resource_version = "status-changed"
        assert body[1]["path"] == "/spec/activeDeadlineSeconds"
        return patch(job_name, namespace, body, **kwargs)

    backend._core_api.patch_namespaced_job = status_update
    assert (await backend.exec_command(name, ["true"])).exit_code == 0


@pytest.mark.asyncio
async def test_concurrent_deadline_reduction_is_retained(backend):
    name = await backend.create_container("member")
    changed = []

    def shorter(job_name, namespace, body, **kwargs):
        job = backend._core_api.jobs[job_name]
        job.spec.active_deadline_seconds = 5
        changed.append(job)
        raise ApiException(status=422, reason="deadline changed")

    backend._core_api.patch_namespaced_job = shorter
    assert (await backend.exec_command(name, ["true"])).exit_code == 0
    assert len(changed) == 1 and changed[0].spec.active_deadline_seconds == 5
