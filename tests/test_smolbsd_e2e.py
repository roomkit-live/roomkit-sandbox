"""End-to-end tests of SmolBSDSandboxBackend on real smolBSD VMs.

They run only when ``SMOLBSD_DIR`` points at a smolBSD checkout with the
``roomkit-sandbox`` image built into it (``smolbsd/build.sh``) and QEMU
can use KVM::

    SMOLBSD_DIR=~/dev/smolBSD uv run pytest tests/test_smolbsd_e2e.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import tempfile
from pathlib import Path

import pytest
from roomkit import (
    ChannelCategory,
    InboundMessage,
    RoomEvent,
    RoomKit,
    TextContent,
    WebSocketChannel,
)
from roomkit.channels.ai import AIChannel
from roomkit.providers.ai.base import AIResponse, AIToolCall
from roomkit.providers.ai.mock import MockAIProvider

from roomkit_sandbox import ContainerSandboxExecutor, NativeCommandBuilder
from roomkit_sandbox._smolbsd_vm import running_pid
from roomkit_sandbox.smolbsd_backend import SmolBSDSandboxBackend

SMOLBSD_DIR = os.environ.get("SMOLBSD_DIR", "")

pytestmark = pytest.mark.skipif(
    not SMOLBSD_DIR or not os.access("/dev/kvm", os.R_OK | os.W_OK),
    reason="needs SMOLBSD_DIR with the roomkit-sandbox image, and KVM",
)


@pytest.fixture
async def backend():
    state_dir = Path(tempfile.mkdtemp(prefix="rk-e2e-"))
    backend = SmolBSDSandboxBackend(smolbsd_dir=SMOLBSD_DIR, state_dir=str(state_dir))
    yield backend
    for info in await backend.list_containers():
        await backend.delete_container(info.container_id)
    shutil.rmtree(state_dir, ignore_errors=True)


def _executor(backend: SmolBSDSandboxBackend, **kwargs) -> ContainerSandboxExecutor:
    return ContainerSandboxExecutor(
        backend=backend, command_builder=NativeCommandBuilder(), session_id="e2e:1", **kwargs
    )


async def test_agent_work_survives_roomkit_restart_and_vm_death(backend):
    sandbox = _executor(backend, env={"TOKEN": "a b"}, labels={"suite": "e2e"})

    r = await sandbox.execute(
        "bash", {"command": 'uname -s; echo "$TOKEN $STEP"; pwd'}, env={"STEP": "1"}
    )
    assert (r.exit_code, r.output) == (0, "smolBSD\na b 1\n/workspace\n")

    r = await sandbox.execute("bash", {"command": "echo hi > note.txt; mkdir -p a/b"})
    assert r.exit_code == 0, r.error
    r = await sandbox.execute("bash", {"command": "cd a/b && git init -q repo && pwd -P"})
    assert (r.exit_code, r.output) == (0, "/workspace/a/b\n"), r.error
    [info] = await backend.list_containers({"suite": "e2e"})
    assert info.status == "running"

    r = await sandbox.execute("bash", {"command": "sleep 30; echo never", "timeout": 2})
    assert r.exit_code == 124
    assert "never" not in r.output

    # roomkit restarts: new backend and executor, same session, same VM
    again = _executor(
        SmolBSDSandboxBackend(smolbsd_dir=SMOLBSD_DIR, state_dir=str(backend._state_dir))
    )
    r = await again.execute("read", {"path": "note.txt"})
    assert "hi" in r.output
    r = await again.execute("bash", {"command": 'echo "$TOKEN"'})
    assert r.output == "a b\n"
    assert len(await backend.list_containers()) == 1

    # the VM is killed: the next command boots another on the same workspace
    pid = running_pid(backend._paths(info.container_id))
    assert pid is not None
    os.kill(pid, signal.SIGKILL)
    r = await again.execute("bash", {"command": "cat note.txt; git -C a/b/repo status --short"})
    assert (r.exit_code, r.output) == (0, "hi\n"), r.error


async def test_roomkit_agent_runs_its_tools_in_the_vm(backend):
    """A RoomKit AI channel, its model scripted, works through its sandbox tools."""
    steps = [
        ("sandbox_write", {"path": "hello.txt", "content": "hi\n"}),
        ("sandbox_edit", {"path": "hello.txt", "old_string": "hi", "new_string": "from the VM"}),
        ("sandbox_read", {"path": "hello.txt"}),
        ("sandbox_bash", {"command": "uname -s"}),
    ]
    provider = MockAIProvider(
        ai_responses=[
            AIResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=[AIToolCall(id=f"c{i}", name=name, arguments=args)],
            )
            for i, (name, args) in enumerate(steps)
        ]
        + [AIResponse(content="done", finish_reason="stop")]
    )
    kit = RoomKit()
    user = WebSocketChannel("user")
    agent = AIChannel("agent", provider=provider, sandbox=_executor(backend))
    kit.register_channel(user)
    kit.register_channel(agent)
    replies: list[str] = []

    async def on_reply(_conn: str, event: RoomEvent) -> None:
        replies.append(getattr(event.content, "body", ""))

    user.register_connection("conn", on_reply, room_id="room")
    await kit.create_room(room_id="room")
    await kit.attach_channel("room", "user")
    await kit.attach_channel("room", "agent", category=ChannelCategory.INTELLIGENCE)

    await kit.process_inbound(
        InboundMessage(channel_id="user", sender_id="u", content=TextContent(body="go"))
    )
    await kit.close()

    results = {
        part.name: json.loads(part.result)
        for message in provider.calls[-1].messages
        if message.role == "tool"
        for part in message.content
    }
    assert results["sandbox_write"]["exit_code"] == 0, results["sandbox_write"]
    assert "Replaced 1 occurrence" in results["sandbox_edit"]["output"]
    assert "from the VM" in results["sandbox_read"]["output"]
    assert results["sandbox_bash"]["output"] == "smolBSD\n"
    assert "done" in replies


def _online() -> bool:
    try:
        socket.getaddrinfo("github.com", 443)
    except OSError:
        return False
    return True


@pytest.mark.skipif(not _online(), reason="host is offline")
async def test_network_is_open_and_fast_from_first_command(backend):
    sandbox = _executor(backend)
    r = await sandbox.execute(
        "bash",
        {
            "command": "curl -sS -o /dev/null -w '%{http_code} %{time_namelookup}' https://github.com"
        },
    )
    code, dns = r.output.split()
    assert (r.exit_code, code) == (0, "200")
    assert float(dns) < 2  # no DAD wait on the guest address
