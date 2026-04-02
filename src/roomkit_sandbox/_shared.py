"""Shared types and constants for roomkit-sandbox."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_IMAGE = "ghcr.io/roomkit-live/sandbox:latest"


@dataclass
class ExecResult:
    """Result of executing a command in a container.

    Any external container backend passed to
    :class:`~roomkit_sandbox.ContainerSandboxExecutor` must return
    objects with these three attributes from ``exec_command``.
    """

    exit_code: int
    stdout: str
    stderr: str
