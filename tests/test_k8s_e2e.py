"""Opt-in proofs against a disposable namespace, never an existing workspace.

Provision restricted admission, a bounded quota, no network, and controller
RBAC for pods/attach, pods/log, pods, PVCs and Secrets first. The native runtime
can prove lifecycle; repeat with gVisor to prove the complete deployment.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest

from roomkit_sandbox import _k8s_objects as objects
from roomkit_sandbox._k8s_stream import new_api
from roomkit_sandbox.k8s_backend import KubernetesSandboxBackend

NAMESPACE = os.environ.get("SANDBOX_E2E_NAMESPACE", "")
pytestmark = pytest.mark.skipif(not NAMESPACE, reason="requires a disposable Kubernetes namespace")


def configured_backend():
    return KubernetesSandboxBackend(
        namespace=NAMESPACE,
        image=os.environ["SANDBOX_E2E_IMAGE"],
        runtime_class=os.environ["SANDBOX_E2E_RUNTIME_CLASS"],
        workspace_storage_class=os.environ["SANDBOX_E2E_STORAGE_CLASS"],
    )


@pytest.fixture
async def workspace():
    backend = configured_backend()
    name = await backend.create_container("e2e-" + uuid.uuid4().hex, labels={"proof": "roomkit"})
    try:
        yield backend, name
    finally:
        await backend.delete_container(name)


@pytest.mark.asyncio
async def test_files_and_tools_survive_command_boundaries(workspace):
    backend, name = workspace
    result = await backend.exec_command(
        name,
        [
            "sh",
            "-ec",
            "echo kept > /workspace/file; "
            "echo home > /home/sandbox/file; touch /opt/venv/preserved; "
            "python -c 'print(40 + 2)'; git --version; uv --version; rtk --version; gh --version",
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert "42" in result.stdout
    result = await backend.exec_command(
        name,
        [
            "sh",
            "-ec",
            "test -e /opt/venv/preserved; "
            "cat /workspace/file /home/sandbox/file; "
            "test ! -e /var/run/secrets/kubernetes.io/serviceaccount/token",
        ],
    )
    assert result.exit_code == 0 and result.stdout == "kept\nhome\n", result.stderr


@pytest.mark.asyncio
async def test_detached_child_cannot_outlive_command(workspace):
    backend, name = workspace
    result = await backend.exec_command(
        name,
        [
            "sh",
            "-ec",
            "setsid sh -c "
            "'touch /workspace/started; sleep 3; touch /workspace/escaped' "
            "</dev/null >/dev/null 2>&1 & "
            "while test ! -e /workspace/started; do sleep 0.1; done; echo done",
        ],
    )
    assert result.exit_code == 0 and result.stdout == "done\n", result.stderr
    await asyncio.sleep(4)
    result = await backend.exec_command(
        name, ["sh", "-ec", "test -e /workspace/started; test ! -e /workspace/escaped"]
    )
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_timeout_and_output_limit_retire_pods(workspace):
    backend, name = workspace
    with pytest.raises(TimeoutError):
        await backend.exec_command(name, ["sleep", "20"], timeout=1)
    with pytest.raises(RuntimeError, match="capture limit"):
        await backend.exec_command(name, ["python", "-c", "print('x' * (2 * 1024 * 1024))"])
    result = await backend.exec_command(name, ["printf", "ready"])
    assert result.exit_code == 0 and result.stdout == "ready"


async def wait_for_orphan_gc(batch, name, *, timeout=155):
    from kubernetes.client.exceptions import ApiException

    saw_deadline = False
    async with asyncio.timeout(timeout):
        while True:
            try:
                job = await asyncio.to_thread(
                    batch.read_namespaced_job, name, NAMESPACE, _request_timeout=(3, 3)
                )
            except ApiException as exc:
                if exc.status == 404:
                    assert saw_deadline
                    return
                raise
            saw_deadline |= any(
                condition.reason == "DeadlineExceeded" for condition in job.status.conditions or []
            )
            await asyncio.sleep(0.5)


@pytest.mark.asyncio
@pytest.mark.parametrize("unscheduled", [False, True])
async def test_job_deadline_and_gc_survive_controller_sigkill(workspace, unscheduled):
    from kubernetes import client

    backend, name = workspace
    code = """
import asyncio
from tests.test_k8s_e2e import configured_backend
from roomkit_sandbox import _k8s_objects as objects
if __import__('sys').argv[2] == 'pending':
    # Accelerate only the startup budget; the Job TTL remains the production 120s.
    objects.STARTUP_SECONDS = 2
    build = objects.command_job
    def unscheduled_job(*args, **kwargs):
        job = build(*args, **kwargs)
        job['spec']['template']['spec']['affinity'] = {'nodeAffinity': {
            'requiredDuringSchedulingIgnoredDuringExecution': {'nodeSelectorTerms': [{
                'matchExpressions': [{'key': 'kubernetes.io/hostname', 'operator': 'In',
                                      'values': ['sandbox-proof-no-such-node']}]}]}}}
        return job
    objects.command_job = unscheduled_job
asyncio.run(configured_backend().exec_command(
    __import__('sys').argv[1], ['sh', '-ec',
    'printf started; sleep 15; touch /workspace/escaped'], timeout=3))
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        name,
        "pending" if unscheduled else "running",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    api = new_api()
    batch = client.BatchV1Api(api.api_client)
    job_name = objects.command_name(name)
    started_at = asyncio.get_running_loop().time()
    try:
        async with asyncio.timeout(65):
            while True:
                try:
                    job = await asyncio.to_thread(
                        batch.read_namespaced_job, job_name, NAMESPACE, _request_timeout=(3, 3)
                    )
                    pods = await asyncio.to_thread(
                        api.list_namespaced_pod,
                        NAMESPACE,
                        label_selector=f"batch.kubernetes.io/controller-uid={job.metadata.uid}",
                        _request_timeout=(3, 3),
                    )
                    if unscheduled and job.status.start_time:
                        assert all(not pod.spec.node_name for pod in pods.items)
                        break
                    if pods.items:
                        log = await asyncio.to_thread(
                            api.read_namespaced_pod_log,
                            pods.items[0].metadata.name,
                            NAMESPACE,
                            container="sandbox",
                            _request_timeout=(3, 3),
                        )
                        if log == "started":
                            started_at = asyncio.get_running_loop().time()
                            break
                except Exception:
                    if child.returncode is not None:
                        _, error = await child.communicate()
                        pytest.fail(error.decode())
                await asyncio.sleep(0.1)
        child.kill()
        await child.wait()
        await wait_for_orphan_gc(batch, job_name)
        # The killed command would write after 15 s. Check beyond that instant,
        # not merely when the status first says DeadlineExceeded.
        await asyncio.sleep(max(0, 16 - (asyncio.get_running_loop().time() - started_at)))
        result = await backend.exec_command(name, ["sh", "-ec", "test ! -e /workspace/escaped"])
        assert result.exit_code == 0
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        api.api_client.close()
