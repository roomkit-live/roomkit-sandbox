"""Minimal Docker backend for standalone roomkit-sandbox use.

Integrators that already have a container backend can pass it
directly to
:class:`~roomkit_sandbox.ContainerSandboxExecutor`.  This module
provides a lightweight alternative for standalone use that only
depends on the ``docker`` Python SDK.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("roomkit_sandbox.backend")

DEFAULT_IMAGE = "ghcr.io/roomkit-live/sandbox:latest"
DEFAULT_MEMORY_LIMIT = "1g"
DEFAULT_CPU_COUNT = 1


@dataclass
class ExecResult:
    """Result of executing a command in a container.

    Any external container backend passed to
    :class:`~roomkit_sandbox.ContainerSandboxExecutor` must return
    objects with these three attributes from ``exec_command``.
    """

    exit_code: int
    stdout: str
    stderr: str


class DockerSandboxBackend:
    """Minimal Docker backend for running sandbox containers.

    Uses the ``docker`` Python SDK.  Only implements the methods needed
    by :class:`~roomkit_sandbox.ContainerSandboxExecutor`.

    Args:
        image: Docker image to use for sandbox containers.
        memory_limit: Memory limit (e.g. ``"1g"``, ``"512m"``).
        cpu_count: Number of CPUs to allocate.
        network: Docker network to attach (optional).
        extra_env: Additional environment variables for containers.
    """

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        memory_limit: str = DEFAULT_MEMORY_LIMIT,
        cpu_count: int = DEFAULT_CPU_COUNT,
        network: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._image = image
        self._memory_limit = memory_limit
        self._cpu_count = cpu_count
        self._network = network
        self._extra_env = extra_env or {}
        self._client: Any = None
        self._client_lock = threading.Lock()

    def _get_client(self) -> Any:
        with self._client_lock:
            if self._client is None:
                try:
                    import docker
                except ImportError as exc:
                    raise ImportError(
                        "docker package required: pip install roomkit-sandbox[docker]"
                    ) from exc
                self._client = docker.from_env()
            return self._client

    async def create_container(
        self,
        session_id: str,
        labels: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Create a new sandbox container and return its ID."""
        client = self._get_client()
        merged_labels = {
            "roomkit.sandbox": "true",
            "roomkit.sandbox.session_id": session_id,
        }
        if labels:
            merged_labels.update(labels)

        merged_env = dict(self._extra_env)
        if env:
            merged_env.update(env)

        kwargs: dict[str, Any] = {
            "image": self._image,
            "detach": True,
            "labels": merged_labels,
            "environment": merged_env,
            "mem_limit": self._memory_limit,
            "cpu_count": self._cpu_count,
            "working_dir": "/workspace",
        }
        if self._network:
            kwargs["network"] = self._network

        container = await asyncio.to_thread(client.containers.run, **kwargs)
        logger.info("Created sandbox container %s (session=%s)", container.short_id, session_id)
        return container.id

    async def exec_command(
        self,
        container_id: str,
        cmd: list[str],
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> ExecResult:
        """Execute a command in a running container."""
        client = self._get_client()
        container = await asyncio.to_thread(client.containers.get, container_id)

        def _run() -> ExecResult:
            result = container.exec_run(
                cmd,
                workdir=workdir,
                environment=env or {},
                demux=True,
            )
            stdout = (result.output[0] or b"").decode(errors="replace") if result.output else ""
            stderr = (result.output[1] or b"").decode(errors="replace") if result.output else ""
            return ExecResult(
                exit_code=result.exit_code or 0,
                stdout=stdout,
                stderr=stderr,
            )

        return await asyncio.wait_for(
            asyncio.to_thread(_run),
            timeout=timeout,
        )

    async def container_exists(self, container_id: str) -> bool:
        """Check if a container exists and is running."""
        client = self._get_client()
        try:
            container = await asyncio.to_thread(client.containers.get, container_id)
            return container.status == "running"
        except Exception:
            return False

    async def find_container(self, session_id: str) -> str | None:
        """Find a running container by session ID label."""
        client = self._get_client()
        containers = await asyncio.to_thread(
            client.containers.list,
            filters={"label": f"roomkit.sandbox.session_id={session_id}"},
        )
        for c in containers:
            if c.status == "running":
                return c.id
        return None

    async def delete_container(self, container_id: str) -> None:
        """Stop and remove a container."""
        client = self._get_client()
        try:
            container = await asyncio.to_thread(client.containers.get, container_id)
            await asyncio.to_thread(container.stop, timeout=5)
            await asyncio.to_thread(container.remove, force=True)
            logger.info("Removed sandbox container %s", container_id[:12])
        except Exception:
            logger.warning("Failed to remove container %s", container_id[:12], exc_info=True)
