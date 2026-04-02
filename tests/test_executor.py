"""Tests for ContainerSandboxExecutor."""

from __future__ import annotations

import pytest
from roomkit.sandbox.tools import SANDBOX_TOOL_PREFIX, SANDBOX_TOOL_SCHEMAS

from roomkit_sandbox import ContainerSandboxExecutor
from roomkit_sandbox._shared import ExecResult


class MockBackend:
    """In-memory mock backend for testing."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.executed: list[tuple[str, list[str]]] = []
        self._containers: dict[str, bool] = {}
        self._next_result = ExecResult(exit_code=0, stdout="", stderr="")

    async def create_container(self, session_id, labels=None, env=None):
        cid = f"mock-{session_id}"
        self.created.append(cid)
        self._containers[cid] = True
        return cid

    async def exec_command(self, container_id, cmd, workdir="/workspace", env=None, timeout=30):
        self.executed.append((container_id, cmd))
        return self._next_result

    async def container_exists(self, container_id):
        return self._containers.get(container_id, False)

    async def find_container(self, session_id):
        cid = f"mock-{session_id}"
        if self._containers.get(cid):
            return cid
        return None


@pytest.fixture
def backend():
    return MockBackend()


@pytest.fixture
def executor(backend):
    return ContainerSandboxExecutor(
        backend=backend,
        session_id="test-session",
    )


@pytest.mark.asyncio
async def test_tool_definitions(executor):
    defs = executor.tool_definitions()
    assert len(defs) == len(SANDBOX_TOOL_SCHEMAS)
    for d in defs:
        assert d["name"].startswith(SANDBOX_TOOL_PREFIX)


@pytest.mark.asyncio
async def test_execute_creates_container(executor, backend):
    result = await executor.execute("ls", {})
    assert len(backend.created) == 1
    assert result.success


@pytest.mark.asyncio
async def test_execute_reuses_container(executor, backend):
    await executor.execute("ls", {})
    await executor.execute("read", {"path": "/tmp/test"})
    # Should create only once
    assert len(backend.created) == 1
    assert len(backend.executed) == 2


@pytest.mark.asyncio
async def test_execute_routes_to_rtk(executor, backend):
    await executor.execute("grep", {"pattern": "TODO", "path": "src/"})
    _, cmd = backend.executed[-1]
    assert cmd == ["rtk", "grep", "TODO", "src/"]


@pytest.mark.asyncio
async def test_execute_git(executor, backend):
    await executor.execute("git", {"args": "diff HEAD~3"})
    _, cmd = backend.executed[-1]
    assert cmd == ["rtk", "git", "diff", "HEAD~3"]


@pytest.mark.asyncio
async def test_execute_returns_failure(executor, backend):
    backend._next_result = ExecResult(exit_code=1, stdout="", stderr="not found")
    result = await executor.execute("read", {"path": "/nonexistent"})
    assert not result.success
    assert result.exit_code == 1
    assert result.error == "not found"


@pytest.mark.asyncio
async def test_setup_commands(backend):
    executor = ContainerSandboxExecutor(
        backend=backend,
        session_id="setup-test",
        setup_commands=["git clone https://example.com/repo.git /workspace/repo"],
    )
    await executor.execute("ls", {})
    # Setup command + actual command = 2 executions
    assert len(backend.executed) == 2
    _, setup_cmd = backend.executed[0]
    assert setup_cmd == ["git", "clone", "https://example.com/repo.git", "/workspace/repo"]


@pytest.mark.asyncio
async def test_per_call_timeout(backend):
    executor = ContainerSandboxExecutor(backend=backend, session_id="timeout-test")
    await executor.execute("bash", {"command": "make test", "timeout": 120})
    # The backend should receive the per-call timeout
    # (We can't directly assert timeout passed to exec_command with this mock,
    # but we verify it doesn't crash)
    assert len(backend.executed) == 1


@pytest.mark.asyncio
async def test_close_releases_reference(executor, backend):
    await executor.execute("ls", {})
    assert executor._container_id is not None
    await executor.close()
    assert executor._container_id is None
