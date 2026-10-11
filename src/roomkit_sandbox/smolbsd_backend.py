"""SmolBSD backend for roomkit-sandbox.

Uses smolBSD (https://github.com/NetBSDfr/smolBSD) to run sandbox
commands inside lightweight NetBSD-based microVMs via QEMU/KVM: one VM
per session, a kernel of its own, booted in well under a second.

Ideal for local AI assistants where container-level isolation isn't
sufficient (untrusted code execution on local machines).

Commands reach the VM over a VirtIO console port, served in the guest
by the runner of the ``roomkit-sandbox`` image (``smolbsd/`` in this
repository): no SSH, no network port on the host. VMs run detached
and survive a roomkit restart. Each session also has a workspace disk,
mounted on ``/workspace``, that outlives its VM until the session's
container is deleted.

Requires:
- smolBSD cloned, with the ``roomkit-sandbox`` image built into it
  (``smolbsd/build.sh /path/to/smolBSD``)
- QEMU with KVM support, and ``socat``

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
import os
import platform
import re
import shlex
import shutil
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from roomkit_sandbox._shared import ContainerInfo, ExecResult
from roomkit_sandbox._smolbsd_runner import RunnerChannel
from roomkit_sandbox._smolbsd_vm import (
    VmPaths,
    VmSpec,
    VmState,
    ensure_disk,
    is_vm_name,
    read_state,
    running_pid,
    start,
    stop,
    vm_name,
    write_conf,
    write_state,
)

logger = logging.getLogger("roomkit_sandbox.smolbsd")

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# smolBSD's name for the host architecture, as in its image file names
_ARCHES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "evbarm-aarch64",
    "arm64": "evbarm-aarch64",
}

# Paths end up in startnb.sh's eval'ed QEMU command line and in QEMU
# option strings: no whitespace, quotes or commas.
_SAFE_PATH_RE = re.compile(r"^[^\s,'\"\\]+$")

# Longest socket path: dir + "/s-" + longest VM name (29) + "-p2.sock",
# under the smallest AF_UNIX limit (104 bytes with the NUL, on macOS).
_MAX_SOCKET_DIR = 104 - 1 - len("/s-") - 29 - len("-p2.sock")

# How long the host waits past a command's timeout before deciding the
# runner (which enforces the timeout itself) is stuck.
_EXEC_GRACE = 5.0
_PING_TIMEOUT = 2.0


def _default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "roomkit-sandbox" / "smolbsd"


def _safe_path(path: Path, what: str) -> Path:
    if not _SAFE_PATH_RE.match(str(path)):
        raise ValueError(f"{what} must not contain whitespace, quotes or commas: {path}")
    return path


def _export_lines(env: dict[str, str]) -> str:
    """Shell lines exporting ``env``, keys validated."""
    lines = []
    for key, value in env.items():
        if not _ENV_KEY_RE.match(key):
            raise ValueError(f"Invalid environment variable name: {key!r}")
        lines.append(f"export {key}={shlex.quote(value)}\n")
    return "".join(lines)


def _command_script(cmd: list[str], workdir: str, env: dict[str, str] | None) -> str:
    script = _export_lines(env or {})
    if workdir:
        script += f"cd {shlex.quote(workdir)} 2>/dev/null || true\n"
    return script + shlex.join(cmd) + "\n"


class SmolBSDSandboxBackend:
    """SmolBSD backend using QEMU microVMs.

    Each sandbox session boots a NetBSD microVM from the shared guest
    image (in snapshot mode, so the image never changes) and talks to
    its runner through a VirtIO console socket. The VM keeps running
    until :meth:`delete_container`, across roomkit restarts; when it
    dies, the next one boots on the same workspace disk.

    Args:
        smolbsd_dir: Path to the cloned smolBSD directory (contains
            ``startnb.sh``, ``kernels/``, ``images/``).
        service: smolBSD service of the guest image.
        image_tag: Tag of the guest image (``images/<service>-<arch>:<tag>.img``);
            empty for an untagged image.
        memory: VM memory in MB.
        cpus: Number of CPU cores.
        network: Give VMs outbound network access (QEMU user-mode NAT).
        workspace_gb: Size of each session's workspace disk, in GiB. The
            file is sparse: it takes the space actually written.
        state_dir: Where each VM's config, state, console log and
            workspace disk live. Defaults to ``$XDG_STATE_HOME/roomkit-sandbox/smolbsd``.
        boot_timeout: Seconds to wait for a new VM's runner to answer.
        extra_env: Environment variables for every VM.
    """

    def __init__(
        self,
        smolbsd_dir: str,
        service: str = "roomkit-sandbox",
        image_tag: str = "latest",
        memory: int = 512,
        cpus: int = 1,
        network: bool = True,
        workspace_gb: int = 8,
        state_dir: str | None = None,
        boot_timeout: float = 30.0,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._smolbsd_dir = _safe_path(Path(smolbsd_dir).resolve(), "smolbsd_dir")
        if len(str(self._smolbsd_dir)) > _MAX_SOCKET_DIR:
            raise ValueError(f"smolbsd_dir path too long for unix sockets: {self._smolbsd_dir}")
        root = Path(state_dir) if state_dir else _default_state_dir()
        self._state_dir = _safe_path(root.resolve(), "state_dir")
        arch = _ARCHES.get(platform.machine().lower())
        if arch is None:
            raise ValueError(f"Unsupported host architecture for smolBSD: {platform.machine()}")
        tag = f":{image_tag}" if image_tag else ""
        self._spec = VmSpec(
            image=self._smolbsd_dir / "images" / f"{service}-{arch}{tag}.img",
            service=service,
            memory=memory,
            cpus=cpus,
            network=network,
            workspace_bytes=workspace_gb << 30,
        )
        self._boot_timeout = boot_timeout
        self._extra_env = extra_env or {}
        self._channels: dict[str, RunnerChannel] = {}
        self._create_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _paths(self, name: str) -> VmPaths:
        return VmPaths(name=name, state_dir=self._state_dir, smolbsd_dir=self._smolbsd_dir)

    def _channel(self, name: str) -> RunnerChannel:
        if name not in self._channels:
            self._channels[name] = RunnerChannel(str(self._paths(name).socket))
        return self._channels[name]

    async def _responds(self, name: str) -> bool:
        try:
            await self._channel(name).ping(_PING_TIMEOUT)
        except (OSError, TimeoutError):
            return False
        return True

    async def create_container(
        self,
        session_id: str,
        labels: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Boot a smolBSD microVM for the session and return its name."""
        env_script = _export_lines({**self._extra_env, **(env or {})})
        name = vm_name(session_id)
        paths = self._paths(name)
        async with self._create_locks[name]:
            if running_pid(paths) is not None and await self._responds(name):
                logger.info("Reusing existing smolBSD VM %s", name)
                return name
            await self._boot(paths, session_id, labels or {}, env_script)
        logger.info("Created smolBSD VM %s (service=%s)", name, self._spec.service)
        return name

    async def _boot(
        self, paths: VmPaths, session_id: str, labels: dict[str, str], env_script: str
    ) -> None:
        if not self._spec.image.is_file():
            raise FileNotFoundError(
                f"smolBSD image not found: {self._spec.image} "
                "(build it with smolbsd/build.sh from roomkit-sandbox)"
            )
        await stop(paths)  # clear what a dead VM of this session left behind
        paths.dir.mkdir(parents=True, exist_ok=True)
        ensure_disk(paths, self._spec.workspace_bytes)
        write_conf(paths, self._spec)
        write_state(paths, VmState(session_id, labels, datetime.now(UTC).isoformat()))
        await start(paths)
        try:
            await self._wait_ready(paths)
            if env_script:
                await self._channel(paths.name).set_env(env_script, _PING_TIMEOUT)
        except Exception as exc:
            await stop(paths)
            raise RuntimeError(
                f"smolBSD VM {paths.name} did not boot: {exc}\n{_tail(paths.console)}"
            ) from exc

    async def _wait_ready(self, paths: VmPaths) -> None:
        """Wait for QEMU to start, then for the runner to answer a ping."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        while True:
            elapsed = loop.time() - started
            if running_pid(paths) is None:
                # QEMU writes its pidfile as it starts, long before the guest boots
                if elapsed >= min(5.0, self._boot_timeout):
                    raise RuntimeError("QEMU is not running")
            elif paths.socket.exists() and await self._responds(paths.name):
                return
            elif elapsed >= self._boot_timeout:
                raise TimeoutError(f"runner silent after {self._boot_timeout}s")
            await asyncio.sleep(0.1)

    async def exec_command(
        self,
        container_id: str,
        cmd: list[str],
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> ExecResult:
        """Execute a command in a smolBSD VM through its runner."""
        if not is_vm_name(container_id) or running_pid(self._paths(container_id)) is None:
            return ExecResult(exit_code=-1, stdout="", stderr=f"VM {container_id} not found")
        script = _command_script(cmd, workdir, env)
        try:
            return await self._channel(container_id).execute(script, timeout, _EXEC_GRACE)
        except TimeoutError:
            logger.error("Runner of VM %s stuck past %ss, stopping the VM", container_id, timeout)
            await stop(self._paths(container_id))
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"Timed out after {timeout}s; VM {container_id} stopped (unresponsive)",
            )
        except Exception as e:
            logger.error("Error executing command in VM %s: %s", container_id, e)
            return ExecResult(exit_code=-1, stdout="", stderr=str(e))

    async def container_exists(self, container_id: str) -> bool:
        """Check if a VM's QEMU is running."""
        return is_vm_name(container_id) and running_pid(self._paths(container_id)) is not None

    async def find_container(self, session_id: str) -> str | None:
        """Find the session's VM, if it is running and its runner answers."""
        name = vm_name(session_id)
        if running_pid(self._paths(name)) is not None and await self._responds(name):
            return name
        return None

    async def list_containers(self, labels: dict[str, str] | None = None) -> list[ContainerInfo]:
        """Every VM in the state directory, narrowed by the labels it was given."""
        if not self._state_dir.is_dir():
            return []
        infos: list[ContainerInfo] = []
        for vm_dir in sorted(self._state_dir.iterdir()):
            paths = self._paths(vm_dir.name)
            state = read_state(paths) if is_vm_name(vm_dir.name) else None
            if state is None:
                continue
            if labels and any(state.labels.get(k) != v for k, v in labels.items()):
                continue
            infos.append(
                ContainerInfo(
                    container_id=vm_dir.name,
                    session_id=state.session_id,
                    labels=state.labels,
                    status="running" if running_pid(paths) is not None else "stopped",
                    created_at=state.created_at,
                )
            )
        return infos

    async def delete_container(self, container_id: str) -> None:
        """Stop the VM and remove its state and workspace disk."""
        if not is_vm_name(container_id):
            return
        self._channels.pop(container_id, None)
        paths = self._paths(container_id)
        await stop(paths)
        await asyncio.to_thread(shutil.rmtree, paths.dir, ignore_errors=True)
        logger.info("Deleted smolBSD VM %s", container_id)


def _tail(path: Path, size: int = 2000) -> str:
    """End of a console log, for boot error messages."""
    try:
        return path.read_bytes()[-size:].decode(errors="replace")
    except OSError:
        return ""
