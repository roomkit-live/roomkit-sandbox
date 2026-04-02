"""Tests for SmolBSDSandboxBackend with mocked subprocess calls."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from roomkit_sandbox.smolbsd_backend import SmolBSDSandboxBackend, _safe_vm_name


def test_safe_vm_name():
    assert _safe_vm_name("sandbox:user123") == "sandbox-sandbox-user123"
    assert _safe_vm_name("test.session") == "sandbox-test-session"
    assert len(_safe_vm_name("a" * 100)) <= 58


@pytest.fixture
def backend(tmp_path):
    # Create minimal smolBSD directory structure
    (tmp_path / "startnb.sh").touch()
    (tmp_path / "kernels").mkdir()
    (tmp_path / "kernels" / "netbsd-SMOL").touch()
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "rescue-amd64.img").touch()
    return SmolBSDSandboxBackend(smolbsd_dir=str(tmp_path))


class MockProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_exec_command(backend):
    # Pre-register a VM so exec_command can find it
    backend._vms["sandbox-test"] = {"session_id": "test", "port": 22022}
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_exec.return_value = MockProcess(returncode=0, stdout=b"file1.py\nfile2.py\n")
        result = await backend.exec_command("sandbox-test", ["ls", "-la"])
        assert result.exit_code == 0
        assert "file1.py" in result.stdout


@pytest.mark.asyncio
async def test_exec_command_vm_not_found(backend):
    result = await backend.exec_command("nonexistent", ["ls"])
    assert result.exit_code == -1
    assert "not found" in result.stderr


@pytest.mark.asyncio
async def test_container_exists_true(backend):
    backend._vms["sandbox-test"] = {"session_id": "test", "port": 22022}
    with patch("asyncio.open_connection") as mock_conn:

        class MockWriter:
            def close(self):
                pass

            async def wait_closed(self):
                pass

        mock_conn.return_value = (None, MockWriter())
        assert await backend.container_exists("sandbox-test")


@pytest.mark.asyncio
async def test_container_exists_false(backend):
    assert not await backend.container_exists("nonexistent")


@pytest.mark.asyncio
async def test_find_container(backend):
    backend._vms["sandbox-test-session"] = {"session_id": "test-session", "port": 22022}
    with patch("asyncio.open_connection") as mock_conn:

        class MockWriter:
            def close(self):
                pass

            async def wait_closed(self):
                pass

        mock_conn.return_value = (None, MockWriter())
        found = await backend.find_container("test-session")
        assert found == "sandbox-test-session"


@pytest.mark.asyncio
async def test_find_container_not_found(backend):
    found = await backend.find_container("missing")
    assert found is None


@pytest.mark.asyncio
async def test_delete_container(backend):
    backend._vms["sandbox-test"] = {"session_id": "test", "port": 22022}
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_exec.return_value = MockProcess(returncode=0)
        await backend.delete_container("sandbox-test")
        assert "sandbox-test" not in backend._vms
