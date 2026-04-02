"""RTK command builder — maps sandbox tool arguments to RTK CLI flags."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import Any


def build_rtk_command(command: str, arguments: dict[str, Any]) -> list[str]:
    """Convert a sandbox tool call into an RTK CLI command.

    Args:
        command: Command name with ``sandbox_`` prefix stripped
            (e.g. ``"read"``, ``"grep"``, ``"git"``).
        arguments: Tool-specific arguments from the AI's tool call.

    Returns:
        Command list suitable for ``subprocess`` or container exec.
    """
    builder = _BUILDERS.get(command)
    if builder is not None:
        return builder(arguments)
    return ["echo", f"Unknown sandbox command: {command}"]


def _build_read(args: dict[str, Any]) -> list[str]:
    cmd = ["rtk", "read", args.get("path", ".")]
    if "offset" in args:
        cmd.extend(["--offset", str(args["offset"])])
    if "limit" in args:
        cmd.extend(["--limit", str(args["limit"])])
    return cmd


def _build_ls(args: dict[str, Any]) -> list[str]:
    cmd = ["rtk", "ls"]
    if args.get("path"):
        cmd.append(args["path"])
    return cmd


def _build_grep(args: dict[str, Any]) -> list[str]:
    cmd = ["rtk", "grep", args.get("pattern", "")]
    if args.get("path"):
        cmd.append(args["path"])
    if args.get("type"):
        cmd.extend(["--type", args["type"]])
    return cmd


def _build_find(args: dict[str, Any]) -> list[str]:
    cmd = ["rtk", "find"]
    if args.get("path"):
        cmd.append(args["path"])
    if args.get("name"):
        cmd.extend(["-name", args["name"]])
    if args.get("type"):
        cmd.extend(["-type", args["type"]])
    return cmd


def _build_git(args: dict[str, Any]) -> list[str]:
    raw = args.get("args", "status")
    return ["rtk", "git"] + shlex.split(raw)


def _build_write(args: dict[str, Any]) -> list[str]:
    path = shlex.quote(args.get("path", ""))
    content = args.get("content", "")
    # Use printf to handle special characters correctly
    return ["sh", "-c", f"printf '%s' {shlex.quote(content)} > {path}"]


def _build_edit(args: dict[str, Any]) -> list[str]:
    # Safety: !r (repr) produces valid Python string literals, preventing injection.
    # The list-based exec bypasses the shell entirely.
    path = args.get("path", "")
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    # Use Python for reliable string replacement (available in sandbox via busybox)
    script = (
        f"import sys; p={path!r}; "
        f"t=open(p).read(); "
        f"o={old_string!r}; n={new_string!r}; "
        f"c=t.count(o); "
        f"print(f'Error: {{c}} matches found, expected 1',file=sys.stderr) or sys.exit(1) "
        f"if c!=1 else None; "
        f"open(p,'w').write(t.replace(o,n,1)); "
        f"print(f'Replaced 1 occurrence in {{p}}')"
    )
    return ["python3", "-c", script]


def _build_delete(args: dict[str, Any]) -> list[str]:
    path = shlex.quote(args.get("path", ""))
    return [
        "sh", "-c",
        f"if [ -d {path} ]; then rm -rf {path}; else rm -f {path}; fi"
        f" && echo Deleted {path}"
        f" || {{ echo \"Failed to delete {path}\" >&2; exit 1; }}",
    ]


def _build_diff(args: dict[str, Any]) -> list[str]:
    return ["rtk", "diff", args.get("file_a", ""), args.get("file_b", "")]


def _build_bash(args: dict[str, Any]) -> list[str]:
    command_str = args.get("command", "")
    return ["rtk", "summary", command_str]


_BuilderFn = Callable[[dict[str, Any]], list[str]]

_BUILDERS: dict[str, _BuilderFn] = {
    "read": _build_read,
    "write": _build_write,
    "edit": _build_edit,
    "ls": _build_ls,
    "grep": _build_grep,
    "find": _build_find,
    "git": _build_git,
    "diff": _build_diff,
    "delete": _build_delete,
    "bash": _build_bash,
}
