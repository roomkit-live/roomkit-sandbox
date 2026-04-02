"""SmolBSD backend for roomkit-sandbox.

Uses smolBSD (https://github.com/NetBSDfr/smolBSD) to run sandbox
commands inside lightweight NetBSD-based microVMs. Provides true VM
isolation with ~10ms boot times via Firecracker/QEMU and btrfs
copy-on-write snapshots.

Ideal for local AI assistants where container-level isolation isn't
sufficient (untrusted code execution, multi-tenant local setups).

Requires:
- smolBSD installed and configured (``sandbox-setup`` completed)
- Incus running (Linux) or OrbStack VM (macOS)
- A golden image with git, bash, and core tools

Usage::

    from roomkit_sandbox import ContainerSandboxExecutor
    from roomkit_sandbox.smolbsd_backend import SmolBSDSandboxBackend

    backend = SmolBSDSandboxBackend(stack="base")
    sandbox = ContainerSandboxExecutor(backend=backend)
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
from typing import Any

from roomkit_sandbox._shared import ExecResult

logger = logging.getLogger("roomkit_sandbox.smolbsd")


def _safe_vm_name(session_id: str) -> str:
    """Convert a session ID to a valid smolBSD VM name."""
    return "sandbox-" + session_id.replace(":", "-").replace(".", "-").lower()[:50]


class SmolBSDSandboxBackend:
    """SmolBSD backend for running sandbox microVMs.

    Wraps the smolBSD CLI tools (``sandbox-start``, ``sandbox``,
    ``sandbox-stop``) to manage Incus-based microVMs.

    Args:
        stack: SmolBSD stack to use for golden image (e.g. ``"base"``,
            ``"python"``, ``"rust"``). Default: ``"base"``.
        workdir: Working directory inside the VM. Default: ``/workspace``.
        ssh_port_base: Base port for SSH port mapping. Each VM gets
            ``ssh_port_base + slot``.
        sandbox_bin: Path to the ``sandbox-start`` binary. Auto-detected
            from PATH if not specified.
    """

    def __init__(
        self,
        stack: str = "base",
        workdir: str = "/workspace",
        sandbox_bin: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._stack = stack
        self._workdir = workdir
        self._extra_env = extra_env or {}
        self._vms: dict[str, str] = {}  # session_id -> vm_name

        # Locate smolBSD binaries
        self._sandbox_start = sandbox_bin or shutil.which("sandbox-start") or "sandbox-start"
        self._sandbox_cmd = shutil.which("sandbox") or "sandbox"
        self._sandbox_stop = shutil.which("sandbox-stop") or "sandbox-stop"

    async def create_container(
        self,
        session_id: str,
        labels: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Create a smolBSD microVM and return its name."""
        vm_name = _safe_vm_name(session_id)

        # Check if VM already exists
        if await self.container_exists(vm_name):
            self._vms[session_id] = vm_name
            logger.info("Reusing existing smolBSD VM %s", vm_name)
            return vm_name

        # Create new VM from golden image
        cmd = [self._sandbox_start, vm_name, "--stack", self._stack]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            err = stderr.decode(errors="replace")
            raise RuntimeError(f"Failed to create smolBSD VM {vm_name}: {err}")

        self._vms[session_id] = vm_name
        logger.info("Created smolBSD VM %s (stack=%s)", vm_name, self._stack)

        # Set up environment variables if provided
        merged_env = dict(self._extra_env)
        if env:
            merged_env.update(env)
        if merged_env:
            env_script = "; ".join(
                f"export {k}={shlex.quote(v)}" for k, v in merged_env.items()
            )
            await self._exec_in_vm(
                vm_name,
                f"echo '{env_script}' >> /etc/profile.d/sandbox-env.sh",
            )

        # Create workspace directory
        await self._exec_in_vm(vm_name, f"mkdir -p {self._workdir}")

        return vm_name

    async def exec_command(
        self,
        container_id: str,
        cmd: list[str],
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> ExecResult:
        """Execute a command in a smolBSD VM."""
        # Build command with workdir and env
        parts = []
        if env:
            for key, value in env.items():
                parts.append(f"export {key}={shlex.quote(value)}")
        if workdir:
            parts.append(f"cd {shlex.quote(workdir)}")
        parts.append(shlex.join(cmd))
        full_cmd = " && ".join(parts)

        try:
            return await asyncio.wait_for(
                self._exec_in_vm(container_id, full_cmd),
                timeout=timeout,
            )
        except TimeoutError:
            return ExecResult(exit_code=124, stdout="", stderr=f"Timed out after {timeout}s")
        except Exception as e:
            logger.error("Error executing command in VM %s: %s", container_id, e)
            return ExecResult(exit_code=-1, stdout="", stderr=str(e))

    async def _exec_in_vm(self, vm_name: str, command: str) -> ExecResult:
        """Execute a command inside a smolBSD VM via the sandbox CLI."""
        proc = await asyncio.create_subprocess_exec(
            self._sandbox_cmd, vm_name, "--cmd", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return ExecResult(
            exit_code=proc.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )

    async def container_exists(self, container_id: str) -> bool:
        """Check if a smolBSD VM exists and is running."""
        proc = await asyncio.create_subprocess_exec(
            "incus", "info", container_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return False
        return "Status: RUNNING" in stdout.decode(errors="replace")

    async def find_container(self, session_id: str) -> str | None:
        """Find a running VM by session ID."""
        vm_name = self._vms.get(session_id)
        if vm_name and await self.container_exists(vm_name):
            return vm_name

        # Try the expected name
        expected = _safe_vm_name(session_id)
        if await self.container_exists(expected):
            self._vms[session_id] = expected
            return expected

        return None

    async def delete_container(self, container_id: str) -> None:
        """Stop and remove a smolBSD VM."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._sandbox_stop, container_id, "--rm",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            # Clean up cache
            self._vms = {k: v for k, v in self._vms.items() if v != container_id}
            logger.info("Deleted smolBSD VM %s", container_id)
        except Exception:
            logger.warning("Failed to delete VM %s", container_id, exc_info=True)
