"""SmolBSD backend for roomkit-sandbox.

Uses smolBSD (https://github.com/NetBSDfr/smolBSD) to run sandbox
commands inside lightweight NetBSD-based microVMs via QEMU/KVM.
Provides true VM isolation with ~70ms boot times.

Ideal for local AI assistants where container-level isolation isn't
sufficient (untrusted code execution on local machines).

Requires:
- smolBSD cloned and built (``bmake fetchimg && bmake SERVICE=rescue build``)
- QEMU with KVM support

Usage::

    from roomkit_sandbox import ContainerSandboxExecutor
    from roomkit_sandbox.smolbsd_backend import SmolBSDSandboxBackend

    from roomkit_sandbox.commands import NativeCommandBuilder

    backend = SmolBSDSandboxBackend(
        smolbsd_dir="/home/user/dev/smolBSD",
    )
    sandbox = ContainerSandboxExecutor(
        backend=backend,
        command_builder=NativeCommandBuilder(),
    )
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex

from roomkit_sandbox._shared import ExecResult

logger = logging.getLogger("roomkit_sandbox.smolbsd")

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_vm_name(session_id: str) -> str:
    """Convert a session ID to a valid smolBSD VM name."""
    return "sandbox-" + session_id.replace(":", "-").replace(".", "-").lower()[:50]


class SmolBSDSandboxBackend:
    """SmolBSD backend using QEMU microVMs.

    Each sandbox session boots a NetBSD microVM via ``startnb.sh``,
    communicates through SSH (port forwarding), and cleans up on close.

    Args:
        smolbsd_dir: Path to the cloned smolBSD directory (contains
            ``startnb.sh``, ``kernels/``, ``images/``).
        service: Service image to use (e.g. ``"rescue"``).
        memory: VM memory in MB.
        cpus: Number of CPU cores.
        ssh_base_port: Base SSH port for VMs. Each VM gets a unique
            port derived from the session ID.
        workdir: Working directory inside the VM.
    """

    def __init__(
        self,
        smolbsd_dir: str,
        service: str = "rescue",
        memory: int = 256,
        cpus: int = 1,
        ssh_base_port: int = 2022,
        workdir: str = "/home/ssh",
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._smolbsd_dir = smolbsd_dir
        self._service = service
        self._memory = memory
        self._cpus = cpus
        self._ssh_base_port = ssh_base_port
        self._workdir = workdir
        self._extra_env = extra_env or {}
        self._vms: dict[str, dict] = {}  # session_id -> {name, pid, port}

        # smolBSD paths (relative to smolbsd_dir — startnb.sh requires this)
        self._startnb = "./startnb.sh"
        self._kernel = "kernels/netbsd-SMOL"
        self._image = f"images/{service}-amd64.img"

    def _ssh_port(self, session_id: str) -> int:
        """Derive a unique SSH port from session ID."""
        return self._ssh_base_port + (hash(session_id) % 100)

    async def create_container(
        self,
        session_id: str,
        labels: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Boot a smolBSD microVM and return its name."""
        vm_name = _safe_vm_name(session_id)

        # Check if already running
        if vm_name in self._vms and await self.container_exists(vm_name):
            logger.info("Reusing existing smolBSD VM %s", vm_name)
            return vm_name

        ssh_port = self._ssh_port(session_id)

        # Boot the VM in daemonized mode
        cmd = [
            self._startnb,
            "-k",
            self._kernel,
            "-i",
            self._image,
            "-m",
            str(self._memory),
            "-c",
            str(self._cpus),
            "-p",
            f"::{ssh_port}-:22",
            "-d",
            "-s",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._smolbsd_dir,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            err = stderr.decode(errors="replace") + stdout.decode(errors="replace")
            raise RuntimeError(f"Failed to boot smolBSD VM {vm_name}: {err}")

        # Wait for VM to be reachable via SSH
        await self._wait_for_ssh(ssh_port)

        self._vms[vm_name] = {
            "session_id": session_id,
            "port": ssh_port,
        }
        logger.info("Created smolBSD VM %s (port=%d, service=%s)", vm_name, ssh_port, self._service)

        # Set up environment variables if provided
        merged_env = dict(self._extra_env)
        if env:
            merged_env.update(env)
        if merged_env:
            for k, v in merged_env.items():
                if not _ENV_KEY_RE.match(k):
                    raise ValueError(f"Invalid environment variable name: {k!r}")
                await self._ssh_exec(ssh_port, f"export {k}={shlex.quote(v)}")

        return vm_name

    async def _wait_for_ssh(self, port: int, timeout: int = 15) -> None:
        """Wait for SSH to become available on the forwarded port."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port),
                    timeout=2,
                )
                writer.close()
                await writer.wait_closed()
                return
            except (ConnectionRefusedError, TimeoutError, OSError):
                await asyncio.sleep(0.5)
        raise TimeoutError(f"SSH not reachable on port {port} after {timeout}s")

    async def _ssh_exec(self, port: int, command: str) -> ExecResult:
        """Execute a command via SSH in the VM."""
        ssh_cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-p",
            str(port),
            "ssh@127.0.0.1",
            command,
        ]
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return ExecResult(
            exit_code=proc.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )

    async def exec_command(
        self,
        container_id: str,
        cmd: list[str],
        workdir: str = "/root",
        env: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> ExecResult:
        """Execute a command in a smolBSD VM via SSH."""
        vm_info = self._vms.get(container_id)
        if not vm_info:
            return ExecResult(exit_code=-1, stdout="", stderr=f"VM {container_id} not found")

        # Build command with workdir and env
        parts = []
        if env:
            for key, value in env.items():
                if not _ENV_KEY_RE.match(key):
                    raise ValueError(f"Invalid environment variable name: {key!r}")
                parts.append(f"export {key}={shlex.quote(value)}")
        if workdir:
            parts.append(f"cd {shlex.quote(workdir)} 2>/dev/null || true")
        parts.append(shlex.join(cmd))
        full_cmd = " && ".join(parts)

        try:
            return await asyncio.wait_for(
                self._ssh_exec(vm_info["port"], full_cmd),
                timeout=timeout,
            )
        except TimeoutError:
            return ExecResult(exit_code=124, stdout="", stderr=f"Timed out after {timeout}s")
        except Exception as e:
            logger.error("Error executing command in VM %s: %s", container_id, e)
            return ExecResult(exit_code=-1, stdout="", stderr=str(e))

    async def container_exists(self, container_id: str) -> bool:
        """Check if a VM is running by testing SSH connectivity."""
        vm_info = self._vms.get(container_id)
        if not vm_info:
            return False
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", vm_info["port"]),
                timeout=2,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (ConnectionRefusedError, TimeoutError, OSError):
            return False

    async def find_container(self, session_id: str) -> str | None:
        """Find a running VM by session ID."""
        vm_name = _safe_vm_name(session_id)
        if vm_name in self._vms and await self.container_exists(vm_name):
            return vm_name
        return None

    async def delete_container(self, container_id: str) -> None:
        """Kill the QEMU process for a VM."""
        vm_info = self._vms.pop(container_id, None)
        if not vm_info:
            return
        # Kill QEMU by finding the process using the SSH port
        try:
            proc = await asyncio.create_subprocess_exec(
                "fuser",
                "-k",
                f"{vm_info['port']}/tcp",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            logger.info("Deleted smolBSD VM %s", container_id)
        except Exception:
            logger.warning("Failed to delete VM %s", container_id, exc_info=True)
