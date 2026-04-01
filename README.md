# roomkit-sandbox

Container-based sandbox executor for [RoomKit](https://github.com/roomkit-live/roomkit) — gives AI agents sandboxed command execution via [RTK](https://github.com/rtk-ai/rtk).

## What it does

`roomkit-sandbox` provides a ready-made `SandboxExecutor` implementation that runs commands inside lightweight Docker containers using RTK for token-optimized output (60-90% fewer tokens).

Any AI agent (Anthropic, OpenAI, Ollama, vLLM) connected to RoomKit gets access to:

| Tool | Description |
|------|-------------|
| `sandbox_read` | Read file contents with line ranges |
| `sandbox_ls` | List directory contents |
| `sandbox_grep` | Search file contents (regex) |
| `sandbox_find` | Find files by pattern |
| `sandbox_git` | Run any git command |
| `sandbox_diff` | Compare two files |
| `sandbox_bash` | Execute shell commands |

## Installation

```bash
pip install roomkit-sandbox
# With Docker backend:
pip install roomkit-sandbox[docker]
```

## Quick Start

```python
from roomkit import Agent
from roomkit.providers.anthropic import AnthropicAIProvider, AnthropicConfig
from roomkit_sandbox import ContainerSandboxExecutor

# Create a sandbox executor
sandbox = ContainerSandboxExecutor(
    image="ghcr.io/roomkit-live/sandbox:latest",
    session_id="my-agent-sandbox",
)

# Attach to any RoomKit agent
agent = Agent(
    "code-reviewer",
    provider=AnthropicAIProvider(AnthropicConfig(api_key="sk-...")),
    system_prompt="You are a code reviewer with access to a sandbox.",
    sandbox=sandbox,
)
```

## With an Existing Backend

If you already have a container backend (e.g. Luge's `ContainerBackend`), pass it directly:

```python
sandbox = ContainerSandboxExecutor(
    backend=my_container_backend,  # Docker or Kubernetes
    session_id=f"sandbox:{user_id}",
    setup_commands=["git clone https://github.com/org/repo.git /workspace/repo"],
    workdir="/workspace/repo",
)
```

## Container Image

Build the lightweight sandbox image (~30-50MB):

```bash
docker build -t roomkit-sandbox:latest .
```

Contents: Alpine 3.21 + bash + git + curl + jq + RTK binary. No Node.js, no Python, no heavy runtimes.

## Architecture

```
Agent (any provider) ──tool call──> RoomKit AIChannel
                                        │
                                   SandboxExecutor
                                        │
                              ContainerSandboxExecutor
                                        │
                                   Docker / K8s
                                        │
                              Lightweight container
                                   (Alpine + RTK)
                                        │
                              rtk read / grep / git / bash
                                        │
                              Token-optimized output → Agent
```

## License

MIT
