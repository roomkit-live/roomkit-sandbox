"""Tests for SmolBSDSandboxBackend with mocked CLI calls."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from roomkit_sandbox.smolbsd_backend import SmolBSDSandboxBackend, _safe_vm_name


def test_safe_vm_name():
    assert _safe_vm_name("sandbox:user123") == "sandbox-sandbox-user123"
    assert _safe_vm_name("test.session") == "sandbox-test-session"
    assert len(_safe_vm_name("a" * 100)) <= 58


@pytest.fixture
def backend():
    return SmolBSDSandboxBackend(stack="base")


class MockProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_create_container(backend):
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        # First call: incus info (container_exists check) — not found
        # Second call: sandbox-start — success
        # Third call: sandbox --cmd (env setup) — skipped (no env)
        # Fourth call: sandbox --cmd (mkdir) — success
        mock_exec.side_effect = [
            MockProcess(returncode=1),  # incus info → not found
            MockProcess(returncode=0),  # sandbox-start → success
            MockProcess(returncode=0),  # sandbox --cmd mkdir → success
        ]
        name = await backend.create_container("test-session")
        assert name == "sandbox-test-session"
        assert mock_exec.call_count == 3


@pytest.mark.asyncio
async def test_exec_command(backend):
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_exec.return_value = MockProcess(
            returncode=0, stdout=b"file1.py\nfile2.py\n"
        )
        result = await backend.exec_command("sandbox-test", ["ls", "-la"])
        assert result.exit_code == 0
        assert "file1.py" in result.stdout


@pytest.mark.asyncio
async def test_container_exists(backend):
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_exec.return_value = MockProcess(
            returncode=0, stdout=b"Name: test\nStatus: RUNNING\n"
        )
        assert await backend.container_exists("sandbox-test")


@pytest.mark.asyncio
async def test_container_not_exists(backend):
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_exec.return_value = MockProcess(returncode=1)
        assert not await backend.container_exists("nonexistent")


@pytest.mark.asyncio
async def test_find_container(backend):
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_exec.return_value = MockProcess(
            returncode=0, stdout=b"Status: RUNNING\n"
        )
        found = await backend.find_container("test-session")
        assert found == "sandbox-test-session"


@pytest.mark.asyncio
async def test_find_container_not_found(backend):
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_exec.return_value = MockProcess(returncode=1)
        found = await backend.find_container("missing")
        assert found is None


@pytest.mark.asyncio
async def test_delete_container(backend):
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_exec.return_value = MockProcess(returncode=0)
        await backend.delete_container("sandbox-test")
        # sandbox-stop should be called
        assert mock_exec.called
