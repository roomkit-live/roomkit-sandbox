"""RTK command builder — maps sandbox tool arguments to RTK CLI flags."""

from __future__ import annotations

import shlex
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


def _build_diff(args: dict[str, Any]) -> list[str]:
    return ["rtk", "diff", args.get("file_a", ""), args.get("file_b", "")]


def _build_bash(args: dict[str, Any]) -> list[str]:
    command_str = args.get("command", "")
    return ["rtk", "summary", command_str]


_BUILDERS: dict[str, Any] = {
    "read": _build_read,
    "ls": _build_ls,
    "grep": _build_grep,
    "find": _build_find,
    "git": _build_git,
    "diff": _build_diff,
    "bash": _build_bash,
}
