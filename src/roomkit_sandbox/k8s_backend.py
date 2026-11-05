"""Kubernetes workspaces with one disposable pod per command.

Files belong to a PVC. Processes belong to a pod with a kubelet deadline,
including when the API dies. A fixed Job name admits one command per
workspace across every API replica; an attachment nonce prevents name-reuse races.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import shlex
import uuid
from typing import Any

from roomkit_sandbox import _k8s_objects as objects
from roomkit_sandbox._k8s_jobs import CommandJobs
from roomkit_sandbox._k8s_stream import MAX_TIMEOUT_SECONDS, new_api
from roomkit_sandbox._shared import DEFAULT_IMAGE, ContainerInfo, ExecResult

logger = logging.getLogger("roomkit_sandbox.k8s")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_payload(env: dict[str, str]) -> str:
    lines = []
    for key, value in env.items():
        if not _ENV_KEY_RE.fullmatch(key):
            raise ValueError(f"Invalid environment variable name: {key!r}")
        lines.append(f"export {key}={shlex.quote(value)}")
    payload = base64.b64encode("\n".join(lines).encode()).decode() + "\n"
    if len(payload) > 65536:
        raise ValueError("Sandbox environment exceeds 64 KiB")
    return payload


def _k8s_label(key: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_./-]", "-", key).strip("-_.")[:63] or "label"


def _k8s_label_value(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", value).strip("-_.")[:63] or "value"


def _labels(labels: dict[str, str] | None) -> dict[str, str]:
    return {_k8s_label(k): _k8s_label_value(v) for k, v in (labels or {}).items()}


class KubernetesSandboxBackend:
    """Persistent files, disposable processes; requires PVC and Secret RBAC.

    ``runtime_class`` is mandatory when the deployment requires stronger kernel
    isolation. A missing class fails at admission, never falls back to runc.
    ``workspace_storage_class`` selects the existing storage provisioner; 4 GiB
    is a storage request, whose enforcement depends on that provisioner.
    """

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        namespace: str = "luge",
        service_account: str = "default",
        image_pull_secret: str = "",  # nosec B107
        extra_env: dict[str, str] | None = None,
        runtime_class: str | None = None,
        workspace_storage_class: str | None = None,
    ) -> None:
        if runtime_class is not None and not runtime_class.strip():
            raise ValueError("runtime_class must be a non-empty RuntimeClass name")
        self._image = image
        self._namespace = namespace
        self._service_account = service_account
        self._image_pull_secret = image_pull_secret
        self._extra_env = extra_env or {}
        self._runtime_class = runtime_class
        self._storage_class = workspace_storage_class
        self._core_api: Any = None
        self._batch_api: Any = None
        self._stream: Any = None
        self._exec_api_factory: Any = new_api

    def _init_client(self) -> None:
        if self._core_api is None:
            from kubernetes import client, stream

            self._core_api = new_api()
            self._batch_api = client.BatchV1Api(self._core_api.api_client)
            self._stream = stream

    def _jobs(self) -> CommandJobs:
        self._init_client()
        return CommandJobs(
            self._call, self._optional, self._namespace, self._stream, self._exec_api_factory
        )

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        self._init_client()
        return await asyncio.to_thread(
            getattr(self._batch_api if "_job" in method else self._core_api, method),
            *args,
            namespace=self._namespace,
            _request_timeout=(5, 10),
            **kwargs,
        )

    async def _optional(self, method: str, name: str) -> Any:
        from kubernetes.client.exceptions import ApiException

        try:
            return await self._call(method, name)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise

    @staticmethod
    def _require_workspace(workspace: Any) -> None:
        annotations = workspace.metadata.annotations or {}
        if annotations.get(objects.FORMAT) != objects.FORMAT_VERSION:
            raise RuntimeError("Unrecognized sandbox workspace; refusing to adopt it")
        if workspace.metadata.deletion_timestamp:
            raise RuntimeError("Sandbox workspace is being deleted")

    async def _workspace(self, name: str) -> Any:
        workspace = await self._optional("read_namespaced_persistent_volume_claim", name)
        if workspace is None:
            raise RuntimeError("Sandbox workspace no longer exists")
        self._require_workspace(workspace)
        return workspace

    async def _legacy_pods(self, session_id: str | None = None) -> list[Any]:
        selector = "managed-by=roomkit-sandbox"
        if session_id is not None:
            selector += f",session={_k8s_label_value(session_id)}"
        result = await self._call("list_namespaced_pod", label_selector=selector)
        return [pod for pod in result.items if not pod.metadata.name.startswith("command-")]

    async def create_container(
        self,
        session_id: str,
        labels: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Create the persistent workspace; no untrusted process starts here."""
        from kubernetes.client.exceptions import ApiException

        if await self._legacy_pods(session_id):
            raise RuntimeError("Legacy sandbox found; migrate its workspace before replacing it")
        merged_env = {**self._extra_env, **(env or {})}
        _env_payload(merged_env)  # Validate before any resource is written.
        body = objects.claim(session_id, _labels(labels), self._storage_class)
        try:
            workspace = await self._call("create_namespaced_persistent_volume_claim", body=body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            workspace = await self._workspace(objects.workspace_name(session_id))
        self._require_workspace(workspace)
        if workspace.metadata.annotations.get(objects.SESSION) != session_id:
            raise RuntimeError("Sandbox workspace identity mismatch")
        try:
            await self._call(
                "create_namespaced_secret", body=objects.environment(workspace, merged_env)
            )
        except ApiException as exc:
            if exc.status != 409:
                raise
            secret = await self._call("read_namespaced_secret", workspace.metadata.name)
            owners = secret.metadata.owner_references or []
            if not any(owner.uid == workspace.metadata.uid for owner in owners):
                raise RuntimeError("Sandbox environment belongs to another workspace") from exc
        return workspace.metadata.name

    async def container_exists(self, container_id: str) -> bool:
        workspace = await self._optional("read_namespaced_persistent_volume_claim", container_id)
        if workspace is None:
            return False
        self._require_workspace(workspace)
        environment = await self._optional("read_namespaced_secret", container_id)
        if environment is None:
            return False
        if not any(
            owner.uid == workspace.metadata.uid
            for owner in environment.metadata.owner_references or []
        ):
            raise RuntimeError("Sandbox environment belongs to another workspace")
        return True

    async def find_container(self, session_id: str) -> str | None:
        name = objects.workspace_name(session_id)
        if await self.container_exists(name):
            return name
        if await self._legacy_pods(session_id):
            raise RuntimeError("Legacy sandbox found; migrate its workspace before replacing it")
        return None

    async def exec_command(
        self,
        container_id: str,
        cmd: list[str],
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> ExecResult:
        """Run one command, then retire every process; retain the workspace."""
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or not 0 < timeout <= MAX_TIMEOUT_SECONDS
        ):
            raise ValueError(
                f"Sandbox timeout must be an integer between 1 and {MAX_TIMEOUT_SECONDS}s"
            )
        nonce = uuid.uuid4().hex
        payload = nonce + "\n" + _env_payload(env or {})
        workspace = await self._workspace(container_id)
        body = objects.command_job(
            workspace,
            image=self._image,
            service_account=self._service_account,
            image_pull_secret=self._image_pull_secret,
            runtime_class=self._runtime_class,
            command=cmd,
            workdir=workdir,
            nonce=nonce,
            timeout=timeout,
        )
        jobs = self._jobs()
        job = await jobs.create(body)
        logger.info(
            "Sandbox command created workspace=%s job_uid=%s timeout_seconds=%s",
            container_id,
            job.metadata.uid,
            timeout,
        )
        try:
            pod = await jobs.ready(job)
            # Teardown marks the PVC first. A creator racing it may have made
            # a waiting pod, but must never release untrusted work into it.
            current_workspace = await self._workspace(container_id)
            if current_workspace.metadata.uid != workspace.metadata.uid:
                raise RuntimeError("Sandbox workspace was replaced")
            current_job = await self._call("read_namespaced_job", job.metadata.name)
            if current_job.metadata.uid != job.metadata.uid:
                raise RuntimeError("Sandbox command was replaced")
            await jobs.shorten_deadline(current_job, "patch_namespaced_job", timeout)
            await jobs.shorten_deadline(pod, "patch_namespaced_pod", timeout)
            result = await jobs.attach(pod, payload, timeout)
            result.exit_code = await jobs.exit_code(pod)
            return result
        finally:
            # A failed delete is an error, never a claimed successful stop. The
            # kubelet deadline remains effective if this process/API is gone.
            await asyncio.shield(jobs.remove(job))
            logger.info(
                "Sandbox command retired workspace=%s job_uid=%s", container_id, job.metadata.uid
            )

    async def delete_container(self, container_id: str) -> None:
        """Explicit teardown deletes files too, unlike command completion."""
        jobs = self._jobs()
        workspace = await self._optional("read_namespaced_persistent_volume_claim", container_id)
        if workspace is None:
            # Legacy pods remain visible for explicit administrative teardown.
            legacy = await self._optional("read_namespaced_pod", container_id)
            if legacy and (legacy.metadata.labels or {}).get("managed-by") == objects.OWNER:
                await jobs.remove_pod(legacy)
            return
        self._require_workspace(workspace)
        await self._call(
            "delete_namespaced_persistent_volume_claim",
            container_id,
            body={"preconditions": {"uid": workspace.metadata.uid}},
        )
        job = await self._optional("read_namespaced_job", objects.command_name(container_id))
        if job is not None and any(
            owner.uid == workspace.metadata.uid for owner in job.metadata.owner_references or []
        ):
            await jobs.remove(job)
        # GC may have removed the Job first, leaving its running pod behind.
        # The immutable PVC UID survives on every pod, independently of names
        # and owners that GC can already have collected.
        pods = await self._call(
            "list_namespaced_pod",
            label_selector=(f"{objects.WORKSPACE_UID}={workspace.metadata.uid}"),
        )
        for pod in pods.items:
            await jobs.remove_pod(pod)
        # The immutable creation environment has the PVC as its owner: GC
        # removes it with the workspace, never after an individual command.

    async def list_containers(self, labels: dict[str, str] | None = None) -> list[ContainerInfo]:
        selector = ",".join(
            f"{k}={v}" for k, v in {**_labels(labels), "managed-by": objects.OWNER}.items()
        )
        claims = await self._call(
            "list_namespaced_persistent_volume_claim", label_selector=selector
        )
        pods = await self._call("list_namespaced_pod", label_selector=selector)
        active = {
            (pod.metadata.labels or {}).get("batch.kubernetes.io/job-name"): pod
            for pod in pods.items
        }
        infos = []
        for claim in claims.items:
            meta = claim.metadata
            pod = active.get(objects.command_name(meta.name))
            status = str(pod.status.phase).lower() if pod else "ready"
            infos.append(
                ContainerInfo(
                    meta.name,
                    (meta.annotations or {}).get(objects.SESSION),
                    meta.labels or {},
                    status,
                    meta.creation_timestamp.isoformat() if meta.creation_timestamp else None,
                )
            )
        for pod in pods.items:
            if not pod.metadata.name.startswith("command-"):
                meta = pod.metadata
                infos.append(
                    ContainerInfo(
                        meta.name,
                        (meta.labels or {}).get("session"),
                        meta.labels or {},
                        "legacy",
                        meta.creation_timestamp.isoformat() if meta.creation_timestamp else None,
                    )
                )
        return infos
