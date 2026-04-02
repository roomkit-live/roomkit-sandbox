"""Tests for RTK command builder."""

from roomkit_sandbox.commands import build_rtk_command


def test_build_read():
    cmd = build_rtk_command("read", {"path": "/src/main.py"})
    assert cmd == ["rtk", "read", "/src/main.py"]


def test_build_read_with_offset_limit():
    cmd = build_rtk_command("read", {"path": "/src/main.py", "offset": 10, "limit": 50})
    assert cmd == ["rtk", "read", "/src/main.py", "--offset", "10", "--limit", "50"]


def test_build_ls_default():
    cmd = build_rtk_command("ls", {})
    assert cmd == ["rtk", "ls"]


def test_build_ls_with_path():
    cmd = build_rtk_command("ls", {"path": "/src"})
    assert cmd == ["rtk", "ls", "/src"]


def test_build_grep():
    cmd = build_rtk_command("grep", {"pattern": "TODO", "path": "src/"})
    assert cmd == ["rtk", "grep", "TODO", "src/"]


def test_build_grep_with_type():
    cmd = build_rtk_command("grep", {"pattern": "def main", "type": "py"})
    assert cmd == ["rtk", "grep", "def main", "--type", "py"]


def test_build_find():
    cmd = build_rtk_command("find", {"path": ".", "name": "*.py", "type": "f"})
    assert cmd == ["rtk", "find", ".", "-name", "*.py", "-type", "f"]


def test_build_git_status():
    cmd = build_rtk_command("git", {"args": "status"})
    assert cmd == ["rtk", "git", "status"]


def test_build_git_diff():
    cmd = build_rtk_command("git", {"args": "diff HEAD~3"})
    assert cmd == ["rtk", "git", "diff", "HEAD~3"]


def test_build_git_clone():
    cmd = build_rtk_command("git", {"args": "clone https://github.com/org/repo.git"})
    assert cmd == ["rtk", "git", "clone", "https://github.com/org/repo.git"]


def test_build_git_default():
    cmd = build_rtk_command("git", {})
    assert cmd == ["rtk", "git", "status"]


def test_build_diff():
    cmd = build_rtk_command("diff", {"file_a": "a.py", "file_b": "b.py"})
    assert cmd == ["rtk", "diff", "a.py", "b.py"]


def test_build_bash():
    cmd = build_rtk_command("bash", {"command": "make test"})
    assert cmd == ["rtk", "summary", "make test"]


def test_build_write():
    cmd = build_rtk_command("write", {"path": "/tmp/test.txt", "content": "hello world"})
    assert cmd[0] == "sh"
    assert "/tmp/test.txt" in cmd[2]
    assert "hello world" in cmd[2]


def test_build_edit():
    cmd = build_rtk_command("edit", {
        "path": "main.py",
        "old_string": "def foo():",
        "new_string": "def bar():",
    })
    assert cmd[0] == "python3"
    assert "main.py" in cmd[2]
    assert "def foo():" in cmd[2]
    assert "def bar():" in cmd[2]


def test_build_delete():
    cmd = build_rtk_command("delete", {"path": "/tmp/old.txt"})
    assert cmd[0] == "sh"
    assert "rm" in cmd[2]


def test_unknown_command():
    cmd = build_rtk_command("unknown", {})
    assert cmd[0] == "echo"
    assert "Unknown" in cmd[1]
