"""Tests for SmolBSDSandboxBackend.

The lifecycle tests replace QEMU with ``fake_smolbsd_runner.py`` run as a
detached process: the backend's own pidfile, liveness, ping, exec and
stop code paths run for real, only ``startnb.sh`` is swapped out.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from roomkit_sandbox import _smolbsd_vm, smolbsd_backend
from roomkit_sandbox._smolbsd_vm import VmPaths, VmSpec, is_vm_name, vm_name, write_conf
from roomkit_sandbox.smolbsd_backend import SmolBSDSandboxBackend, _command_script

FAKE_RUNNER = Path(__file__).with_name("fake_smolbsd_runner.py")


def test_vm_name_is_stable_unique_and_safe():
    assert vm_name("sandbox:user123") == vm_name("sandbox:user123")
    assert vm_name("A:b") != vm_name("a.b")  # same slug, different sessions
    assert is_vm_name(vm_name("x" * 200))
    assert len(vm_name("x" * 200)) == 29
    assert is_vm_name(vm_name("../../etc"))
    assert not is_vm_name("../../etc")
    assert not is_vm_name("sbx-a-0000000g")


def test_conf_sources_to_the_expected_vm(tmp_path):
    paths = VmPaths(name="sbx-s-12345678", state_dir=tmp_path / "state", smolbsd_dir=tmp_path)
    paths.dir.mkdir(parents=True)
    spec = VmSpec(
        image=tmp_path / "images" / "roomkit-sandbox-amd64:latest.img",
        service="roomkit-sandbox",
        memory=512,
        cpus=2,
        network=False,
        workspace_bytes=1 << 30,
    )
    write_conf(paths, spec)
    script = f'. "{paths.conf}"; printf "%s|" "$img" "$root" "$mem" "$uuid" "$max_ports" "$nonet"'
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True).stdout
    assert out.split("|")[:6] == [
        str(spec.image),
        "NAME=roomkit-sandboxroot",
        "512",
        "sbx-s-12345678",
        "3",
        "yes",
    ]
    extra = subprocess.run(
        ["sh", "-c", f'. "{paths.conf}"; printf "%s" "$extra"'], capture_output=True, text=True
    ).stdout
    assert extra.startswith("-snapshot ")
    assert f"file={paths.disk},format=raw,id=ws,snapshot=off" in extra


def test_command_script_quotes_env_and_workdir():
    script = _command_script(["echo", "a b"], "/work dir", {"TOKEN": "x'y"})
    assert script == "export TOKEN='x'\"'\"'y'\ncd '/work dir' 2>/dev/null || true\necho 'a b'\n"
    with pytest.raises(ValueError, match="Invalid environment variable name"):
        _command_script(["true"], "", {"BAD-KEY": "1"})


def test_constructor_rejects_unsafe_paths(tmp_path):
    with pytest.raises(ValueError, match="whitespace"):
        SmolBSDSandboxBackend(smolbsd_dir=str(tmp_path / "with space"))
    with pytest.raises(ValueError, match="too long"):
        SmolBSDSandboxBackend(smolbsd_dir="/" + "d" * 63)
    SmolBSDSandboxBackend(smolbsd_dir="/" + "d" * 62, state_dir=str(tmp_path))


@pytest.fixture
def short_dir():
    """A directory short enough for socket paths (pytest's tmp_path is not)."""
    path = Path(tempfile.mkdtemp(prefix="rk-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def smolbsd_dir(short_dir):
    (short_dir / "images").mkdir()
    (short_dir / "images" / "roomkit-sandbox-amd64:latest.img").touch()
    (short_dir / "images" / "roomkit-sandbox-evbarm-aarch64:latest.img").touch()
    return short_dir


@pytest.fixture
def fake_qemu(monkeypatch):
    """Replace startnb.sh with the fake runner; returns the started process list."""
    started: list[subprocess.Popen] = []
    flags: list[str] = []

    async def fake_start(paths: VmPaths) -> None:
        proc = subprocess.Popen(
            [sys.executable, str(FAKE_RUNNER), str(paths.socket), *flags],
            start_new_session=True,
            cwd=Path(__file__).parent.parent,
        )
        paths.pidfile.write_text(str(proc.pid))
        started.append(proc)

    monkeypatch.setattr(smolbsd_backend, "start", fake_start)
    yield started, flags
    for proc in started:
        proc.kill()
        proc.wait()


@pytest.fixture
def backend(smolbsd_dir, tmp_path):
    return SmolBSDSandboxBackend(
        smolbsd_dir=str(smolbsd_dir),
        state_dir=str(tmp_path / "state"),
        boot_timeout=10,
        extra_env={"BASE": "base"},
    )


async def test_lifecycle_survives_a_new_backend(backend, fake_qemu, smolbsd_dir, tmp_path):
    started, _ = fake_qemu
    vm = await backend.create_container("chat:42", labels={"tenant": "t1"}, env={"TOKEN": "a b"})
    disk = tmp_path / "state" / vm / "workspace.img"
    assert disk.stat().st_size == 8 << 30
    assert disk.stat().st_blocks == 0  # sparse until the guest writes

    echo = ["sh", "-c", 'echo "$BASE $TOKEN $STEP"']
    result = await backend.exec_command(vm, echo, env={"STEP": "1"})
    assert (result.exit_code, result.stdout) == (0, "base a b 1\n")
    assert await backend.container_exists(vm)
    assert await backend.create_container("chat:42") == vm  # reused, not booted again
    assert len(started) == 1

    # roomkit restarts: a fresh backend finds the running VM and its environment
    again = SmolBSDSandboxBackend(smolbsd_dir=str(smolbsd_dir), state_dir=str(tmp_path / "state"))
    assert await again.find_container("chat:42") == vm
    result = await again.exec_command(vm, ["sh", "-c", 'echo "$TOKEN"'])
    assert result.stdout == "a b\n"
    [info] = await again.list_containers({"tenant": "t1"})
    assert (info.container_id, info.session_id, info.status) == (vm, "chat:42", "running")
    assert await again.list_containers({"tenant": "other"}) == []

    await again.delete_container(vm)
    assert started[0].wait(timeout=5) is not None
    assert not await again.container_exists(vm)
    assert await again.find_container("chat:42") is None
    assert await again.list_containers() == []
    assert not (tmp_path / "state" / vm).exists()
    assert not list(smolbsd_dir.glob(f"*{vm}*"))
    result = await again.exec_command(vm, ["true"])
    assert result.exit_code == -1


async def test_dead_vm_is_listed_stopped_then_recreated(backend, fake_qemu):
    started, _ = fake_qemu
    vm = await backend.create_container("s1")
    started[0].kill()
    started[0].wait()

    assert not await backend.container_exists(vm)
    assert await backend.find_container("s1") is None
    [info] = await backend.list_containers()
    assert info.status == "stopped"

    disk = backend._paths(vm).disk
    disk.write_bytes(b"files of the dead VM")
    assert await backend.create_container("s1") == vm
    assert len(started) == 2
    assert disk.read_bytes() == b"files of the dead VM"  # same workspace, not recreated
    assert (await backend.exec_command(vm, ["echo", "back"])).stdout == "back\n"


async def test_stuck_runner_stops_the_vm(backend, fake_qemu, monkeypatch):
    started, flags = fake_qemu
    flags.append("--hang-on-exec")
    monkeypatch.setattr(smolbsd_backend, "_EXEC_GRACE", 0.2)
    vm = await backend.create_container("stuck")

    result = await backend.exec_command(vm, ["sleep", "60"], timeout=1)

    assert result.exit_code == 124
    assert "unresponsive" in result.stderr
    assert started[0].wait(timeout=5) is not None
    assert not await backend.container_exists(vm)


async def test_boot_failure_reports_console_and_cleans_up(backend, monkeypatch, tmp_path):
    async def no_qemu(paths: VmPaths) -> None:
        paths.console.write_text("qemu: could not open kernel\n")

    monkeypatch.setattr(smolbsd_backend, "start", no_qemu)
    monkeypatch.setattr(backend, "_boot_timeout", 0.5)
    with pytest.raises(RuntimeError, match="(?s)QEMU is not running.*could not open kernel"):
        await backend.create_container("broken")


async def test_missing_image_fails_clearly(short_dir):
    backend = SmolBSDSandboxBackend(smolbsd_dir=str(short_dir), state_dir=str(short_dir / "s"))
    with pytest.raises(FileNotFoundError, match="smolbsd/build.sh"):
        await backend.create_container("s")


async def test_invalid_env_key_fails_before_boot(backend, fake_qemu):
    started, _ = fake_qemu
    with pytest.raises(ValueError, match="Invalid environment variable name"):
        await backend.create_container("s", env={"NOT VALID": "1"})
    assert started == []


async def test_path_like_ids_are_rejected(backend, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    await backend.delete_container("../victim")
    assert victim.exists()
    assert not await backend.container_exists("../victim")
    result = await backend.exec_command("../victim", ["true"])
    assert result.exit_code == -1


async def test_concurrent_creates_boot_once(backend, fake_qemu):
    started, _ = fake_qemu
    names = await asyncio.gather(*(backend.create_container("same") for _ in range(3)))
    assert len(set(names)) == 1
    assert len(started) == 1


def test_stop_never_signals_its_own_group(tmp_path, monkeypatch):
    sent: list[tuple[str, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append(("killpg", pgid)))
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(("kill", pid)))
    _smolbsd_vm._signal_group(os.getpid(), _smolbsd_vm.signal.SIGTERM)
    assert sent == [("kill", os.getpid())]
