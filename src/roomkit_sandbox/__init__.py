"""roomkit-sandbox — Container-based sandbox executor for RoomKit.

Provides :class:`ContainerSandboxExecutor`, a ready-made
:class:`~roomkit.sandbox.SandboxExecutor` implementation that runs
commands inside lightweight containers using RTK for token-optimized
output.

Backends:

- :class:`DockerSandboxBackend` — for Docker (``pip install roomkit-sandbox[docker]``)
- :class:`KubernetesSandboxBackend` — for Kubernetes (``pip install roomkit-sandbox[kubernetes]``)
- :class:`SmolBSDSandboxBackend` — for smolBSD microVMs (local VM isolation)

Usage::

    from roomkit_sandbox import ContainerSandboxExecutor

    sandbox = ContainerSandboxExecutor(
        image="ghcr.io/roomkit-live/sandbox:latest",
    )

    agent = Agent("my-agent", provider=..., sandbox=sandbox)
"""

from roomkit_sandbox._shared import DEFAULT_IMAGE, ExecResult
from roomkit_sandbox.executor import ContainerSandboxExecutor

__all__ = ["ContainerSandboxExecutor", "ExecResult", "DEFAULT_IMAGE"]
