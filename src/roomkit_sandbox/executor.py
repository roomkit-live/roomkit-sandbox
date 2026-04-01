"""Container-based SandboxExecutor implementation.

Runs sandbox commands inside lightweight Docker containers using RTK
(https://github.com/rtk-ai/rtk) for token-optimized output.

Supports two modes:

1. **Standalone** — uses the built-in :class:`DockerSandboxBackend`::

       sandbox = ContainerSandboxExecutor(
           image="ghcr.io/roomkit-live/sandbox:latest",
       )

2. **With external backend** — pass any object implementing
   ``create_container``, ``exec_command``, ``container_exists``,
   and ``find_container`` (e.g. Luge's ``ContainerBackend``)::

       sandbox = ContainerSandboxExecutor(backend=my_backend)
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from roomkit.sandbox import SandboxExecutor, SandboxResult
from roomkit.sandbox.tools import SANDBOX_TOOL_SCHEMAS

from roomkit_sandbox.commands import build_rtk_command

logger = logging.getLogger("roomkit_sandbox")


@runtime_checkable
class ContainerBackendProtocol(Protocol):
    """Minimal interface for container backends."""

    async def create_container(
        self, session_id: str, labels: dict[str, str] | None = ..., env: dict[str, str] | None = ...
    ) -> str: ...

    async def exec_command(
        self, container_id: str, cmd: list[str], workdir: str = ..., env: dict[str, str] | None = ..., timeout: int = ...
    ) -> Any: ...

    async def container_exists(self, container_id: str) -> bool: ...

    async def find_container(self, session_id: str) -> str | None: ...


class ContainerSandboxExecutor(SandboxExecutor):
    """Runs sandbox commands via RTK in lightweight Docker containers.

    Args:
        backend: Container backend (Docker, Kubernetes, or any compatible
            implementation).  If ``None``, creates a built-in
            :class:`~roomkit_sandbox.backend.DockerSandboxBackend`.
        image: Docker image for sandbox containers (only used with
            built-in backend).
        session_id: Identifier for container reuse across calls.
            Containers are discovered by label on startup.
        workdir: Working directory inside the container.
        timeout: Default command timeout in seconds.
        setup_commands: Commands to run on first container creation
            (e.g. ``["git clone ... /workspace/repo"]``).
        labels: Extra labels for container creation.
        env: Extra environment variables for container creation.
    """

    def __init__(
        self,
        backend: Any | None = None,
        image: str = "ghcr.io/roomkit-live/sandbox:latest",
        session_id: str | None = None,
        workdir: str = "/workspace",
        timeout: int = 30,
        setup_commands: list[str] | None = None,
        labels: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._workdir = workdir
        self._timeout = timeout
        self._setup_commands = setup_commands or []
        self._labels = labels or {}
        self._env = env or {}
        self._session_id = session_id or "roomkit-sandbox"
        self._container_id: str | None = None
        self._initialized = False

        if backend is not None:
            self._backend = backend
        else:
            from roomkit_sandbox.backend import DockerSandboxBackend

            self._backend = DockerSandboxBackend(image=image)

    async def _ensure_container(self) -> str:
        """Get or create a sandbox container, returning its ID."""
        # Fast path: cached container still running
        if self._container_id is not None:
            if await self._backend.container_exists(self._container_id):
                return self._container_id
            self._container_id = None

        # Discovery: find existing container by session label
        found = await self._backend.find_container(self._session_id)
        if found is not None:
            self._container_id = found
            logger.info("Reusing sandbox container %s", found[:12])
            return found

        # Create new container
        container_id = await self._backend.create_container(
            session_id=self._session_id,
            labels=self._labels,
            env=self._env,
        )
        self._container_id = container_id

        # Run setup commands (e.g. git clone)
        for cmd_str in self._setup_commands:
            logger.info("Running setup: %s", cmd_str[:80])
            result = await self._backend.exec_command(
                container_id,
                ["bash", "-c", cmd_str],
                workdir=self._workdir,
                timeout=self._timeout,
            )
            if result.exit_code != 0:
                logger.warning(
                    "Setup command failed (exit %d): %s",
                    result.exit_code,
                    result.stderr[:200] if hasattr(result, "stderr") else "",
                )

        self._initialized = True
        return container_id

    async def execute(
        self,
        command: str,
        arguments: dict[str, Any] | None = None,
    ) -> SandboxResult:
        """Run a sandbox command via RTK in the container."""
        container_id = await self._ensure_container()
        cmd = build_rtk_command(command, arguments or {})

        try:
            result = await self._backend.exec_command(
                container_id,
                cmd,
                workdir=self._workdir,
                timeout=self._timeout,
            )
            return SandboxResult(
                exit_code=result.exit_code,
                output=result.stdout if hasattr(result, "stdout") else str(result),
                error=result.stderr if hasattr(result, "stderr") else "",
            )
        except TimeoutError:
            return SandboxResult(
                exit_code=124,
                error=f"Command timed out after {self._timeout}s",
            )
        except Exception as exc:
            logger.exception("Sandbox command failed: %s", command)
            return SandboxResult(exit_code=1, error=str(exc))

    def tool_definitions(self) -> list[dict[str, Any]]:
        """Return the full RTK tool catalog."""
        return list(SANDBOX_TOOL_SCHEMAS)

    async def close(self) -> None:
        """Release container reference (does not destroy for reuse)."""
        self._container_id = None
