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
   and ``find_container``.  ``exec_command`` must return an object
   with ``exit_code``, ``stdout``, and ``stderr`` attributes (see
   :class:`~roomkit_sandbox.ExecResult`)::

       sandbox = ContainerSandboxExecutor(backend=my_backend)
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any, Protocol, runtime_checkable

from roomkit.sandbox import SandboxExecutor, SandboxResult
from roomkit.sandbox.tools import SANDBOX_TOOL_SCHEMAS

from roomkit_sandbox._shared import DEFAULT_IMAGE, ExecResult
from roomkit_sandbox.commands import CommandBuilder, RtkCommandBuilder

logger = logging.getLogger("roomkit_sandbox")


@runtime_checkable
class ContainerBackendProtocol(Protocol):
    """Minimal interface for container backends.

    ``exec_command`` must return an object with ``exit_code: int``,
    ``stdout: str``, and ``stderr: str`` attributes.
    """

    async def create_container(
        self,
        session_id: str,
        labels: dict[str, str] | None = ...,
        env: dict[str, str] | None = ...,
    ) -> str: ...

    async def exec_command(
        self,
        container_id: str,
        cmd: list[str],
        workdir: str = ...,
        env: dict[str, str] | None = ...,
        timeout: int = ...,
    ) -> ExecResult: ...

    async def container_exists(self, container_id: str) -> bool: ...

    async def find_container(self, session_id: str) -> str | None: ...

    async def delete_container(self, container_id: str) -> None: ...


class ContainerSandboxExecutor(SandboxExecutor):
    """Runs sandbox commands via RTK in lightweight Docker containers.

    Args:
        backend: Container backend (Docker, Kubernetes, or any compatible
            implementation).  If ``None``, creates a built-in
            :class:`~roomkit_sandbox.docker_backend.DockerSandboxBackend`.
        image: Docker image for sandbox containers (only used with
            built-in backend).
        session_id: Identifier for container reuse across calls.
            Containers are discovered by label on startup.
        workdir: Working directory inside the container.
        timeout: Default command timeout in seconds.
        setup_commands: Shell commands to run on first container creation
            (e.g. ``["git clone ... /workspace/repo"]``).
        labels: Extra labels for container creation.
        env: Extra environment variables for container creation.
    """

    def __init__(
        self,
        backend: Any | None = None,
        image: str = DEFAULT_IMAGE,
        session_id: str | None = None,
        workdir: str = "/workspace",
        timeout: int = 30,
        setup_commands: list[str] | None = None,
        labels: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        command_builder: CommandBuilder | None = None,
    ) -> None:
        self._workdir = workdir
        self._timeout = timeout
        self._setup_commands = setup_commands or []
        self._labels = labels or {}
        self._env = env or {}
        self._session_id = session_id or "roomkit-sandbox"
        self._container_id: str | None = None
        self._lock = asyncio.Lock()
        self._command_builder = command_builder or RtkCommandBuilder()

        if backend is not None:
            self._backend = backend
        else:
            from roomkit_sandbox.docker_backend import DockerSandboxBackend

            self._backend = DockerSandboxBackend(image=image)

    async def _ensure_container(self) -> str:
        """Get or create a sandbox container, returning its ID."""
        async with self._lock:
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
                cmd = shlex.split(cmd_str)
                result = await self._backend.exec_command(
                    container_id,
                    cmd,
                    workdir=self._workdir,
                    timeout=self._timeout,
                )
                if result.exit_code != 0:
                    logger.warning(
                        "Setup command failed (exit %d): %s",
                        result.exit_code,
                        result.stderr[:200],
                    )

            return container_id

    async def execute(
        self,
        command: str,
        arguments: dict[str, Any] | None = None,
    ) -> SandboxResult:
        """Run a sandbox command in the container."""
        container_id = await self._ensure_container()
        args = arguments or {}
        cmd = self._command_builder.build(command, args)

        # Per-call timeout override (e.g. sandbox_bash timeout parameter)
        timeout = args.get("timeout", self._timeout)
        if not isinstance(timeout, int) or timeout <= 0:
            timeout = self._timeout

        try:
            result = await self._backend.exec_command(
                container_id,
                cmd,
                workdir=self._workdir,
                timeout=timeout,
            )
            return SandboxResult(
                exit_code=result.exit_code,
                output=result.stdout,
                error=result.stderr,
            )
        except TimeoutError:
            return SandboxResult(
                exit_code=124,
                error=f"Command timed out after {timeout}s",
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
