# roomkit-sandbox

Container-based sandbox executor for [RoomKit](https://github.com/roomkit-live/roomkit) — gives AI agents sandboxed command execution via [RTK](https://github.com/rtk-ai/rtk).

## What it does

`roomkit-sandbox` provides a ready-made `SandboxExecutor` implementation that runs commands inside lightweight containers or VMs. Any AI agent (Anthropic, OpenAI, Ollama, vLLM) connected to RoomKit gets access to:

| Tool | Description |
|------|-------------|
| `sandbox_read` | Read file contents with line ranges |
| `sandbox_write` | Write content to a file |
| `sandbox_edit` | Replace a string in a file |
| `sandbox_ls` | List directory contents |
| `sandbox_grep` | Search file contents (regex) |
| `sandbox_find` | Find files by pattern |
| `sandbox_git` | Run any git command |
| `sandbox_diff` | Compare two files |
| `sandbox_delete` | Delete a file or directory |
| `sandbox_bash` | Execute shell commands |

## Installation

```bash
# Docker backend (development)
pip install roomkit-sandbox[docker]

# Kubernetes backend (production)
pip install roomkit-sandbox[kubernetes]

# SmolBSD backend (local VM isolation)
pip install roomkit-sandbox
```

## Backends

Three backends for different deployment profiles:

| | Docker | Kubernetes | SmolBSD |
|--|--------|------------|---------|
| **Isolation** | Container | Pod | VM |
| **Boot** | ~500ms | ~2-5s | ~5s (SSH) |
| **Image** | 37MB (Alpine + RTK) | 37MB | 512MB (NetBSD) |
| **Commands** | RTK (token-optimized) | RTK | Native (POSIX) |
| **Use case** | Dev, CI | Production | Local assistant |

See [docs/backends.md](docs/backends.md) for detailed setup instructions.

## Quick Start

### Docker

```python
from roomkit import Agent
from roomkit_sandbox import ContainerSandboxExecutor
from roomkit_sandbox.docker_backend import DockerSandboxBackend

sandbox = ContainerSandboxExecutor(
    backend=DockerSandboxBackend(image="ghcr.io/roomkit-live/sandbox:latest"),
    session_id="my-sandbox",
)

agent = Agent("reviewer", provider=..., sandbox=sandbox)
```

### Kubernetes

```python
from roomkit_sandbox import ContainerSandboxExecutor
from roomkit_sandbox.k8s_backend import KubernetesSandboxBackend

sandbox = ContainerSandboxExecutor(
    backend=KubernetesSandboxBackend(
        image="ghcr.io/roomkit-live/sandbox:latest",
        namespace="production",
    ),
)
```

### SmolBSD (Experimental)

```python
from roomkit_sandbox import ContainerSandboxExecutor, NativeCommandBuilder
from roomkit_sandbox.smolbsd_backend import SmolBSDSandboxBackend

sandbox = ContainerSandboxExecutor(
    backend=SmolBSDSandboxBackend(smolbsd_dir="/path/to/smolBSD", service="sshd"),
    command_builder=NativeCommandBuilder(),
)
```

## Command Builders

The `CommandBuilder` ABC controls how tool calls become shell commands:

- **`RtkCommandBuilder`** (default) — uses RTK for 60-90% token reduction
- **`NativeCommandBuilder`** — uses standard Unix commands (cat, grep, find, git)
- **Custom** — subclass `CommandBuilder` for your own tools

See [docs/commands.md](docs/commands.md) for details and examples.

## Container Image

```bash
docker pull ghcr.io/roomkit-live/sandbox:latest
```

Alpine 3.21 + RTK 0.34.2 + git + bash + curl + jq — **37MB**.

## Architecture

```
Agent (any provider) ──tool call──> RoomKit AIChannel
                                        │
                                   SandboxExecutor
                                        │
                              ContainerSandboxExecutor
                                        │
                    ┌───────────────────┼───────────────────┐
                    │                   │                   │
              DockerBackend      K8sBackend        SmolBSDBackend
                    │                   │                   │
              Container (37MB)     Pod (37MB)         VM (512MB)
                    │                   │                   │
              RtkCommandBuilder  RtkCommandBuilder  NativeCommandBuilder
                    │                   │                   │
              Token-optimized    Token-optimized    Standard output
```

## Documentation

- [Backends](docs/backends.md) — Docker, Kubernetes, SmolBSD setup and comparison
- [Command Builders](docs/commands.md) — RTK vs Native, creating custom builders

## License

MIT
