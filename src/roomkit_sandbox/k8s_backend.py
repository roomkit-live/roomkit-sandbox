"""Kubernetes backend for roomkit-sandbox.

Creates lightweight pods with the sandbox image for command execution.
Requires the ``kubernetes`` Python SDK::

    pip install roomkit-sandbox[kubernetes]
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time
from typing import Any

from roomkit_sandbox._shared import DEFAULT_IMAGE, ExecResult

logger = logging.getLogger("roomkit_sandbox.k8s")

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_pod_name(session_id: str) -> str:
    """Convert a session ID to a valid K8s pod name."""
    name = re.sub(r"[^a-z0-9-]", "-", session_id.lower())
    return f"sandbox-{name}"[:63]


def _k8s_label(key: str) -> str:
    """Convert dot-separated keys to K8s-compatible label keys."""
    return key.replace(".", "-")


class KubernetesSandboxBackend:
    """Kubernetes backend for running sandbox pods.

    Args:
        image: Container image for sandbox pods.
        namespace: Kubernetes namespace.
        service_account: Service account for pods.
        image_pull_secret: Optional image pull secret name.
    """

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        namespace: str = "luge",
        service_account: str = "default",
        image_pull_secret: str = "",
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._image = image
        self._namespace = namespace
        self._service_account = service_account
        self._image_pull_secret = image_pull_secret
        self._extra_env = extra_env or {}
        self._core_api: Any = None
        self._stream: Any = None
        self._session_pods: dict[str, str] = {}

    def _init_client(self) -> None:
        if self._core_api is not None:
            return
        try:
            from kubernetes import client, config, stream
        except ImportError as exc:
            raise ImportError(
                "kubernetes package required: pip install roomkit-sandbox[kubernetes]"
            ) from exc
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self._core_api = client.CoreV1Api()
        self._stream = stream

    async def create_container(
        self,
        session_id: str,
        labels: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Create a sandbox pod and return its name."""
        self._init_client()

        from kubernetes.client import (
            V1Container,
            V1EnvVar,
            V1LocalObjectReference,
            V1ObjectMeta,
            V1Pod,
            V1PodSpec,
            V1ResourceRequirements,
            V1SecurityContext,
        )
        pod_name = _safe_pod_name(session_id)

        # Build labels
        k8s_labels = {
            "app": "roomkit-sandbox",
            "session": session_id,
            "managed-by": "roomkit-sandbox",
        }
        for k, v in (labels or {}).items():
            k8s_labels[_k8s_label(k)] = v

        # Build env vars
        merged_env = dict(self._extra_env)
        if env:
            merged_env.update(env)
        env_vars = [V1EnvVar(name=k, value=v) for k, v in merged_env.items()]

        pod = V1Pod(
            metadata=V1ObjectMeta(name=pod_name, labels=k8s_labels),
            spec=V1PodSpec(
                restart_policy="Never",
                service_account_name=self._service_account,
                image_pull_secrets=(
                    [V1LocalObjectReference(name=self._image_pull_secret)]
                    if self._image_pull_secret
                    else None
                ),
                containers=[
                    V1Container(
                        name="sandbox",
                        image=self._image,
                        command=["tail", "-f", "/dev/null"],
                        working_dir="/workspace",
                        env=env_vars,
                        resources=V1ResourceRequirements(
                            requests={"cpu": "0.5", "memory": "256Mi"},
                            limits={"cpu": "1", "memory": "512Mi"},
                        ),
                        security_context=V1SecurityContext(run_as_user=1000),
                    )
                ],
            ),
        )

        try:
            await asyncio.to_thread(
                self._core_api.create_namespaced_pod, self._namespace, pod,
            )
        except Exception as e:
            if "AlreadyExists" in str(e):
                logger.warning("Pod %s already exists, reusing", pod_name)
            else:
                raise

        await self._wait_for_pod_ready(pod_name)
        self._session_pods[session_id] = pod_name
        logger.info("Created sandbox pod %s (session=%s)", pod_name, session_id)
        return pod_name

    async def _wait_for_pod_ready(self, pod_name: str, timeout: int = 60) -> None:
        """Wait for pod to reach Running state."""
        start = time.monotonic()

        while True:
            try:
                pod = await asyncio.to_thread(
                    self._core_api.read_namespaced_pod, pod_name, self._namespace,
                )
                if pod.status.phase == "Running":
                    if pod.status.container_statuses:
                        for cs in pod.status.container_statuses:
                            if cs.name == "sandbox" and cs.ready:
                                return
                elif pod.status.phase in ("Failed", "Succeeded"):
                    raise RuntimeError(f"Pod {pod_name} is in {pod.status.phase} state")
            except Exception as e:
                if "not found" not in str(e).lower():
                    raise

            if time.monotonic() - start > timeout:
                raise TimeoutError(f"Pod {pod_name} not ready after {timeout}s")
            await asyncio.sleep(1)

    async def exec_command(
        self,
        container_id: str,
        cmd: list[str],
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> ExecResult:
        """Execute a command in a sandbox pod."""
        self._init_client()

        # Build exec command with workdir/env prefix
        exec_cmd = cmd
        if workdir or env:
            prefix_parts = []
            if env:
                for key, value in env.items():
                    if not _ENV_KEY_RE.match(key):
                        raise ValueError(f"Invalid environment variable name: {key!r}")
                    safe_value = value.replace("'", "'\\''")
                    prefix_parts.append(f"export {key}='{safe_value}'")
            if workdir:
                prefix_parts.append(f"cd {shlex.quote(workdir)}")
            prefix = " && ".join(prefix_parts)

            if len(cmd) == 3 and cmd[0] == "sh" and cmd[1] == "-c":
                exec_cmd = ["sh", "-c", f"{prefix} && {cmd[2]}"]
            else:
                exec_cmd = ["sh", "-c", f"{prefix} && {shlex.join(cmd)}"]

        def _exec_blocking() -> ExecResult:
            resp = self._stream.stream(
                self._core_api.connect_get_namespaced_pod_exec,
                container_id,
                self._namespace,
                command=exec_cmd,
                container="sandbox",
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
            )
            stdout = ""
            stderr = ""
            while resp.is_open():
                resp.update(timeout=1)
                if resp.peek_stdout():
                    stdout += resp.read_stdout()
                if resp.peek_stderr():
                    stderr += resp.read_stderr()
            return ExecResult(
                exit_code=resp.returncode or 0,
                stdout=stdout,
                stderr=stderr,
            )

        return await asyncio.wait_for(
            asyncio.to_thread(_exec_blocking),
            timeout=timeout,
        )

    async def container_exists(self, container_id: str) -> bool:
        """Check if a pod exists and is running."""
        self._init_client()
        try:
            pod = await asyncio.to_thread(
                self._core_api.read_namespaced_pod, container_id, self._namespace,
            )
            return pod.status.phase == "Running"
        except Exception:
            return False

    async def find_container(self, session_id: str) -> str | None:
        """Find a running pod by session ID label."""
        # Check cache first
        pod_name = self._session_pods.get(session_id)
        if pod_name and await self.container_exists(pod_name):
            return pod_name

        self._init_client()
        try:
            pods = await asyncio.to_thread(
                self._core_api.list_namespaced_pod,
                namespace=self._namespace,
                label_selector=f"session={session_id},managed-by=roomkit-sandbox",
            )
            for pod in pods.items:
                if pod.status.phase == "Running":
                    self._session_pods[session_id] = pod.metadata.name
                    return pod.metadata.name
        except Exception as e:
            logger.error("Error finding pod by session %s: %s", session_id, e)
        return None

    async def delete_container(self, container_id: str) -> None:
        """Delete a sandbox pod."""
        self._init_client()
        try:
            await asyncio.to_thread(
                self._core_api.delete_namespaced_pod, container_id, self._namespace,
            )
            # Clean up cache
            self._session_pods = {
                k: v for k, v in self._session_pods.items() if v != container_id
            }
            logger.info("Deleted sandbox pod %s", container_id)
        except Exception:
            logger.warning("Failed to delete pod %s", container_id, exc_info=True)
