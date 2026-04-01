"""roomkit-sandbox — Container-based sandbox executor for RoomKit.

Provides :class:`ContainerSandboxExecutor`, a ready-made
:class:`~roomkit.sandbox.SandboxExecutor` implementation that runs
commands inside lightweight Docker containers using RTK for
token-optimized output.

Usage::

    from roomkit_sandbox import ContainerSandboxExecutor

    sandbox = ContainerSandboxExecutor(
        image="ghcr.io/roomkit-live/sandbox:latest",
    )

    agent = Agent("my-agent", provider=..., sandbox=sandbox)
"""

from roomkit_sandbox.executor import ContainerSandboxExecutor

__all__ = ["ContainerSandboxExecutor"]
