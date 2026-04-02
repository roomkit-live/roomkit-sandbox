"""Command builder ABC and implementations for sandbox tool execution.

Two implementations:
- :class:`RtkCommandBuilder` — uses RTK for token-optimized output (Docker, K8s)
- :class:`NativeCommandBuilder` — uses native Unix commands (SmolBSD, any POSIX system)
"""

from __future__ import annotations

import shlex
from abc import ABC, abstractmethod
from typing import Any


class CommandBuilder(ABC):
    """Abstract base for mapping sandbox tool calls to shell commands."""

    def build(self, command: str, arguments: dict[str, Any]) -> list[str]:
        """Convert a sandbox tool call into a shell command list.

        Args:
            command: Command name with ``sandbox_`` prefix stripped.
            arguments: Tool-specific arguments.

        Returns:
            Command list suitable for subprocess or container exec.
        """
        method = getattr(self, f"build_{command}", None)
        if method is not None:
            return method(arguments)
        return ["echo", f"Unknown sandbox command: {command}"]

    @abstractmethod
    def build_read(self, args: dict[str, Any]) -> list[str]: ...

    @abstractmethod
    def build_ls(self, args: dict[str, Any]) -> list[str]: ...

    @abstractmethod
    def build_grep(self, args: dict[str, Any]) -> list[str]: ...

    @abstractmethod
    def build_find(self, args: dict[str, Any]) -> list[str]: ...

    @abstractmethod
    def build_git(self, args: dict[str, Any]) -> list[str]: ...

    @abstractmethod
    def build_diff(self, args: dict[str, Any]) -> list[str]: ...

    @abstractmethod
    def build_bash(self, args: dict[str, Any]) -> list[str]: ...

    def build_write(self, args: dict[str, Any]) -> list[str]:
        path = shlex.quote(args.get("path", ""))
        content = args.get("content", "")
        return ["sh", "-c", f"printf '%s' {shlex.quote(content)} > {path}"]

    def build_edit(self, args: dict[str, Any]) -> list[str]:
        path = args.get("path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        awk_script = (
            'BEGIN { o=ENVIRON["_OLD"]; n=ENVIRON["_NEW"] } '
            "{ b = b sep $0; sep = RS } "
            "END { "
            "  c=0; t=b; "
            "  while ((p=index(t,o)) > 0) { c++; t=substr(t,p+length(o)) } "
            '  if (c!=1) { printf "Error: %d matches, expected 1\\n", c > "/dev/stderr"; exit 1 } '
            '  p=index(b,o); printf "%s%s%s\\n", substr(b,1,p-1), n, substr(b,p+length(o)) '
            "}"
        )
        qp = shlex.quote(path)
        return [
            "sh",
            "-c",
            f"_OLD={shlex.quote(old_string)} _NEW={shlex.quote(new_string)} "
            f"awk {shlex.quote(awk_script)} {qp} > {qp}.tmp && mv {qp}.tmp {qp} "
            f"&& echo 'Replaced 1 occurrence in' {qp}",
        ]

    def build_delete(self, args: dict[str, Any]) -> list[str]:
        path = shlex.quote(args.get("path", ""))
        return [
            "sh",
            "-c",
            f"if [ -d {path} ]; then rm -rf {path}; else rm -f {path}; fi"
            f" && echo Deleted {path}"
            f' || {{ echo "Failed to delete {path}" >&2; exit 1; }}',
        ]


class RtkCommandBuilder(CommandBuilder):
    """Uses RTK for token-optimized output (Docker, Kubernetes)."""

    def build_read(self, args: dict[str, Any]) -> list[str]:
        cmd = ["rtk", "read", args.get("path", ".")]
        if "offset" in args:
            cmd.extend(["--offset", str(args["offset"])])
        if "limit" in args:
            cmd.extend(["--limit", str(args["limit"])])
        return cmd

    def build_ls(self, args: dict[str, Any]) -> list[str]:
        cmd = ["rtk", "ls"]
        if args.get("path"):
            cmd.append(args["path"])
        return cmd

    def build_grep(self, args: dict[str, Any]) -> list[str]:
        cmd = ["rtk", "grep", args.get("pattern", "")]
        if args.get("path"):
            cmd.append(args["path"])
        if args.get("type"):
            cmd.extend(["--type", args["type"]])
        return cmd

    def build_find(self, args: dict[str, Any]) -> list[str]:
        cmd = ["rtk", "find"]
        if args.get("path"):
            cmd.append(args["path"])
        if args.get("name"):
            cmd.extend(["-name", args["name"]])
        if args.get("type"):
            cmd.extend(["-type", args["type"]])
        return cmd

    def build_git(self, args: dict[str, Any]) -> list[str]:
        raw = args.get("args", "status")
        return ["rtk", "git"] + shlex.split(raw)

    def build_diff(self, args: dict[str, Any]) -> list[str]:
        return ["rtk", "diff", args.get("file_a", ""), args.get("file_b", "")]

    def build_bash(self, args: dict[str, Any]) -> list[str]:
        return ["rtk", "summary", args.get("command", "")]


class NativeCommandBuilder(CommandBuilder):
    """Uses native Unix commands (SmolBSD, any POSIX system)."""

    def build_read(self, args: dict[str, Any]) -> list[str]:
        path = shlex.quote(args.get("path", "."))
        offset = args.get("offset", 0)
        limit = args.get("limit")
        if offset or limit:
            start = (offset or 0) + 1
            tail = f"tail -n +{start}"
            head = f" | head -n {limit}" if limit else ""
            return ["sh", "-c", f"cat -n {path} | {tail}{head}"]
        return ["cat", "-n", path]

    def build_ls(self, args: dict[str, Any]) -> list[str]:
        path = args.get("path", ".")
        return ["ls", "-la", path]

    def build_grep(self, args: dict[str, Any]) -> list[str]:
        cmd = ["grep", "-rn", args.get("pattern", "")]
        if args.get("path"):
            cmd.append(args["path"])
        else:
            cmd.append(".")
        return cmd

    def build_find(self, args: dict[str, Any]) -> list[str]:
        cmd = ["find", args.get("path", ".")]
        if args.get("name"):
            cmd.extend(["-name", args["name"]])
        if args.get("type"):
            cmd.extend(["-type", args["type"]])
        return cmd

    def build_git(self, args: dict[str, Any]) -> list[str]:
        raw = args.get("args", "status")
        return ["git"] + shlex.split(raw)

    def build_diff(self, args: dict[str, Any]) -> list[str]:
        return ["diff", "-u", args.get("file_a", ""), args.get("file_b", "")]

    def build_bash(self, args: dict[str, Any]) -> list[str]:
        return ["sh", "-c", args.get("command", "")]


# Default instance for backward compatibility
_rtk_builder = RtkCommandBuilder()


def build_rtk_command(command: str, arguments: dict[str, Any]) -> list[str]:
    """Legacy function — delegates to :class:`RtkCommandBuilder`."""
    return _rtk_builder.build(command, arguments)
