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


@dataclass
class ContainerInfo:
    """One container a backend manages, as ``list_containers`` reports it.

    ``container_id`` is what the other backend verbs take (a Docker id, a pod
    name); ``session_id`` is the executor's reuse key when the backend recorded
    it; ``labels`` are the backend's own markers merged with the caller's;
    ``status`` is the backend's lifecycle word, lower-cased (``running``,
    ``exited``, ``pending``…); ``created_at`` is ISO-8601 when known.
    """

    container_id: str
    session_id: str | None
    labels: dict[str, str]
    status: str
    created_at: str | None = None
