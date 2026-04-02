# Sandbox Backends

roomkit-sandbox provides three container backends for running sandbox commands. Each implements the same 4-method protocol, so the `ContainerSandboxExecutor` works identically with any of them.

## Docker Backend

**Best for:** Local development, CI/CD.

```python
from roomkit_sandbox import ContainerSandboxExecutor
from roomkit_sandbox.docker_backend import DockerSandboxBackend

backend = DockerSandboxBackend(
    image="ghcr.io/roomkit-live/sandbox:latest",
    memory_limit="512m",
    cpu_count=1,
    network="my-docker-network",  # optional
)
sandbox = ContainerSandboxExecutor(backend=backend)
```

**Requirements:**
- Docker running
- `pip install roomkit-sandbox[docker]`

**Image:** Alpine 3.21 + RTK + git + bash + curl + jq (37MB).

```bash
docker pull ghcr.io/roomkit-live/sandbox:latest
```

**Characteristics:**
- Container isolation (shared kernel)
- ~500ms startup, instant reuse
- RTK for token-optimized output
- Per-user persistent containers (label-based discovery)

---

## Kubernetes Backend

**Best for:** Production cloud deployments.

```python
from roomkit_sandbox import ContainerSandboxExecutor
from roomkit_sandbox.k8s_backend import KubernetesSandboxBackend

backend = KubernetesSandboxBackend(
    image="ghcr.io/roomkit-live/sandbox:latest",
    namespace="production",
    service_account="sandbox-sa",
    image_pull_secret="ghcr-secret",  # optional
)
sandbox = ContainerSandboxExecutor(backend=backend)
```

**Requirements:**
- Kubernetes cluster (in-cluster or kubeconfig)
- `pip install roomkit-sandbox[kubernetes]`

**Pod spec:**
- 512Mi memory, 1 CPU (much lighter than application pods)
- Non-root execution (UID 1000)
- RestartPolicy: Never
- Label-based pod discovery for session reuse

**Characteristics:**
- Pod isolation (shared kernel)
- ~2-5s startup (pod scheduling), instant reuse
- RTK for token-optimized output
- Scales with cluster

---

## SmolBSD Backend (Experimental)

**Best for:** Local AI assistants requiring VM-level isolation.

```python
from roomkit_sandbox import ContainerSandboxExecutor, NativeCommandBuilder
from roomkit_sandbox.smolbsd_backend import SmolBSDSandboxBackend

backend = SmolBSDSandboxBackend(
    smolbsd_dir="/path/to/smolBSD",
    service="sshd",
)
sandbox = ContainerSandboxExecutor(
    backend=backend,
    command_builder=NativeCommandBuilder(),  # No RTK on NetBSD
)
```

**Requirements:**
- smolBSD installed ([github.com/NetBSDfr/smolBSD](https://github.com/NetBSDfr/smolBSD))
- QEMU with KVM support
- sshd service image built (see setup below)

**Setup:**

```bash
# 1. Install prerequisites
sudo apt install curl git bmake qemu-system-x86_64 binutils libarchive-tools gdisk socat jq lsof

# 2. Clone smolBSD
git clone https://github.com/NetBSDfr/smolBSD.git
cd smolBSD

# 3. Fetch builder image
bmake fetchimg

# 4. Add your SSH key for the sshd service
cp ~/.ssh/id_ed25519.pub service/sshd/etc/

# 5. Build the sshd service image
bmake SERVICE=sshd build
```

**Current limitations:**
- SSH-based exec (~5s first boot, ~50ms subsequent commands)
- No git, curl, or bash in default sshd image (only POSIX core tools)
- VirtIO socket exec planned (would reduce to ~100ms boot)

**Characteristics:**
- True VM isolation (separate kernel, hypervisor escape required)
- ~70ms kernel boot (5s with SSH wait)
- Native Unix commands (no RTK)
- Ideal for untrusted code execution

---

## Comparison

| | Docker | Kubernetes | SmolBSD |
|--|--------|------------|---------|
| **Isolation** | Container | Pod | VM |
| **First boot** | ~500ms | ~2-5s | ~5s (SSH) |
| **Reuse** | Instant | Instant | Instant |
| **Image size** | 37MB | 37MB | 512MB |
| **Commands** | RTK | RTK | Native |
| **Production** | Dev/CI | Yes | Local |
| **Install** | `[docker]` | `[kubernetes]` | Manual |
