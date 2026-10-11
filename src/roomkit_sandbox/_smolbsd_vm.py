"""smolBSD microVM processes for the roomkit-sandbox backend.

Each VM gets a ``startnb.sh`` config file of its own and runs detached
from the roomkit process (own session, reparented to init), so it
outlives a roomkit restart. Everything needed to find it again derives
from its name: ``startnb.sh`` names the QEMU pidfile after the config
file and the VirtIO console sockets after the ``uuid`` the file sets,
both in the smolBSD directory it runs from.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shlex
import signal
from dataclasses import asdict, dataclass
from pathlib import Path

# Short enough that the socket path in the smolBSD directory stays under
# the AF_UNIX limit: "sbx-" + slug (16) + "-" + digest (8).
_SLUG_LEN = 16
_NAME_RE = re.compile(rf"^sbx-[a-z0-9-]{{1,{_SLUG_LEN}}}-[0-9a-f]{{8}}$")


def vm_name(session_id: str) -> str:
    """Stable, collision-free VM name for a session ID."""
    slug = re.sub(r"[^a-z0-9]+", "-", session_id.lower())[:_SLUG_LEN].strip("-") or "session"
    digest = hashlib.sha256(session_id.encode()).hexdigest()[:8]
    return f"sbx-{slug}-{digest}"


def is_vm_name(name: str) -> bool:
    """Whether ``name`` is one :func:`vm_name` could return (never a path)."""
    return bool(_NAME_RE.match(name))


@dataclass(frozen=True)
class VmPaths:
    """Where one VM's files live."""

    name: str
    state_dir: Path
    smolbsd_dir: Path

    @property
    def dir(self) -> Path:
        return self.state_dir / self.name

    @property
    def conf(self) -> Path:
        return self.dir / f"{self.name}.conf"

    @property
    def state(self) -> Path:
        return self.dir / "state.json"

    @property
    def console(self) -> Path:
        return self.dir / "console.log"

    @property
    def disk(self) -> Path:
        """The VM's workspace disk, mounted on ``/workspace`` by the guest."""
        return self.dir / "workspace.img"

    @property
    def pidfile(self) -> Path:
        return self.smolbsd_dir / f"qemu-{self.name}.pid"

    @property
    def socket(self) -> Path:
        """Host end of the runner's port (guest ``/dev/ttyVI02``)."""
        return self.smolbsd_dir / f"s-{self.name}-p2.sock"

    def leftovers(self) -> list[Path]:
        """Files QEMU leaves in the smolBSD directory when killed hard."""
        return [self.pidfile, self.smolbsd_dir / f"s-{self.name}-p1.sock", self.socket]


@dataclass(frozen=True)
class VmSpec:
    """What to boot: the guest image and the VM's resources."""

    image: Path
    service: str
    memory: int
    cpus: int
    network: bool
    workspace_bytes: int


def write_conf(paths: VmPaths, spec: VmSpec) -> None:
    """Write the ``startnb.sh`` config file for this VM.

    ``uuid`` and ``max_ports=3`` (console + control port p1 + runner port
    p2) fix the socket names. ``-snapshot`` keeps the shared image
    untouched, except for the workspace disk (``snapshot=off``), which
    keeps the Agent's files across VM restarts. (A 9p share would let
    the host read them, but NetBSD's 9p client fails ``getcwd()`` in any
    subdirectory, which breaks git and most build tools.)
    """
    extra = (
        f"-snapshot -drive if=none,file={paths.disk},format=raw,id=ws,snapshot=off "
        "-device virtio-blk-device,drive=ws"
    )
    values = {
        "img": str(spec.image),
        "root": f"NAME={spec.service}root",
        "mem": str(spec.memory),
        "cores": str(spec.cpus),
        "uuid": paths.name,
        "max_ports": "3",
        "extra": extra,
    }
    if not spec.network:
        values["nonet"] = "yes"
    paths.conf.write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in values.items()))


@dataclass(frozen=True)
class VmState:
    """What the backend records about a VM when it creates it."""

    session_id: str
    labels: dict[str, str]
    created_at: str


def ensure_disk(paths: VmPaths, size: int) -> None:
    """Create the workspace disk, sparse and blank, unless it exists.

    The guest formats a blank disk on first boot; an existing one keeps
    the files of the session's previous VM.
    """
    if not paths.disk.exists():
        with paths.disk.open("wb") as disk:
            disk.truncate(size)


def write_state(paths: VmPaths, state: VmState) -> None:
    paths.state.write_text(json.dumps(asdict(state)))


def read_state(paths: VmPaths) -> VmState | None:
    try:
        raw = json.loads(paths.state.read_text())
        return VmState(
            session_id=str(raw["session_id"]),
            labels={str(k): str(v) for k, v in raw["labels"].items()},
            created_at=str(raw["created_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


async def start(paths: VmPaths) -> None:
    """Launch ``startnb.sh`` detached; it returns once the VM is spawned."""
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        './startnb.sh -f "$1" </dev/null >>"$2" 2>&1 &',
        "sh",
        str(paths.conf),
        str(paths.console),
        cwd=paths.smolbsd_dir,
        start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


def running_pid(paths: VmPaths) -> int | None:
    """PID of this VM's QEMU, or None when it is not running.

    The pidfile can outlive QEMU and its PID be reused, so the process
    must also carry this VM's socket name on its command line.
    """
    try:
        pid = int(paths.pidfile.read_text().strip())
    except (OSError, ValueError):
        return None
    if not Path("/proc").is_dir():  # no procfs (macOS): best effort
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        return pid
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return pid if paths.socket.name.encode() in cmdline else None


async def stop(paths: VmPaths, grace: float = 5.0) -> None:
    """Stop the VM (TERM, then KILL after ``grace``) and clear its leftovers."""
    pid = running_pid(paths)
    if pid is not None:
        _signal_group(pid, signal.SIGTERM)
        deadline = asyncio.get_running_loop().time() + grace
        while running_pid(paths) is not None:
            if asyncio.get_running_loop().time() > deadline:
                _signal_group(pid, signal.SIGKILL)
                break
            await asyncio.sleep(0.1)
    for leftover in paths.leftovers():
        leftover.unlink(missing_ok=True)


def _signal_group(pid: int, sig: signal.Signals) -> None:
    """Signal QEMU with its process group: startnb.sh and its socat watcher."""
    with contextlib.suppress(ProcessLookupError):
        pgid = os.getpgid(pid)
        if pgid == os.getpgrp():  # never signal our own group
            os.kill(pid, sig)
        else:
            os.killpg(pgid, sig)
