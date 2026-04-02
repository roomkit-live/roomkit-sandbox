# Command Builder

The `CommandBuilder` ABC defines how sandbox tool calls are translated into shell commands. Two implementations are provided, and developers can create their own.

## Architecture

```
Agent calls sandbox_read(path="/src/main.py")
    → SandboxExecutor strips prefix → command="read", args={path: "/src/main.py"}
    → CommandBuilder.build("read", args)
    → Dispatches to build_read(args)
    → Returns ["rtk", "read", "/src/main.py"]  (or ["cat", "-n", "/src/main.py"])
    → Backend executes in container/VM
```

## Built-in Implementations

### RtkCommandBuilder (default)

Uses [RTK](https://github.com/rtk-ai/rtk) for token-optimized output (60-90% fewer tokens). Used by Docker and Kubernetes backends.

| Tool | Command |
|------|---------|
| `sandbox_read` | `rtk read <path> [--offset N] [--limit N]` |
| `sandbox_ls` | `rtk ls [path]` |
| `sandbox_grep` | `rtk grep <pattern> [path] [--type py]` |
| `sandbox_find` | `rtk find [path] [-name pat] [-type f]` |
| `sandbox_git` | `rtk git <args>` |
| `sandbox_diff` | `rtk diff <file_a> <file_b>` |
| `sandbox_bash` | `rtk summary <command>` |
| `sandbox_write` | `printf '%s' <content> > <path>` |
| `sandbox_edit` | awk-based string replacement |
| `sandbox_delete` | `rm -f` / `rm -rf` |

### NativeCommandBuilder

Uses standard Unix commands. Used by SmolBSD and works on any POSIX system.

| Tool | Command |
|------|---------|
| `sandbox_read` | `cat -n <path>` (with tail/head for offset/limit) |
| `sandbox_ls` | `ls -la [path]` |
| `sandbox_grep` | `grep -rn <pattern> [path]` |
| `sandbox_find` | `find [path] [-name pat] [-type f]` |
| `sandbox_git` | `git <args>` |
| `sandbox_diff` | `diff -u <file_a> <file_b>` |
| `sandbox_bash` | `sh -c <command>` |
| `sandbox_write` | `printf '%s' <content> > <path>` (shared) |
| `sandbox_edit` | awk-based string replacement (shared) |
| `sandbox_delete` | `rm -f` / `rm -rf` (shared) |

## Creating a Custom Builder

Subclass `CommandBuilder` and override the abstract methods. Add new `build_<name>` methods for custom tools — they're auto-dispatched.

```python
from roomkit_sandbox.commands import CommandBuilder

class MyCommandBuilder(CommandBuilder):
    """Custom builder with project-specific tools."""

    def build_read(self, args):
        # Use bat (syntax highlighting) instead of cat
        return ["bat", "--plain", "--line-range", f"{args.get('offset', 0)}:", args["path"]]

    def build_ls(self, args):
        return ["exa", "--long", args.get("path", ".")]

    def build_grep(self, args):
        return ["rg", args["pattern"], args.get("path", ".")]

    def build_find(self, args):
        return ["fd", args.get("name", ""), args.get("path", ".")]

    def build_git(self, args):
        return ["git"] + args.get("args", "status").split()

    def build_diff(self, args):
        return ["delta", args["file_a"], args["file_b"]]

    def build_bash(self, args):
        return ["bash", "-c", args["command"]]

    # Custom tool — auto-dispatched when agent calls sandbox_docker
    def build_docker(self, args):
        return ["docker", args.get("subcommand", "ps")]
```

Then pass it to the executor:

```python
sandbox = ContainerSandboxExecutor(
    backend=backend,
    command_builder=MyCommandBuilder(),
)
```

**Note:** For custom tools to appear in the agent's tool list, you also need to add their schemas via a custom `SandboxExecutor.tool_definitions()` override.

## Shared Commands

`build_write`, `build_edit`, and `build_delete` are implemented in the base `CommandBuilder` class using POSIX-compatible commands (printf, awk, rm). They work across all environments and don't need to be overridden unless you want different behavior.
