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

**Image:** Alpine 3.21 + RTK + git + bash + curl + jq + gh + uv (37MB).

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

## SmolBSD Backend

**Best for:** Local AI assistants requiring VM-level isolation.

```python
from roomkit_sandbox import ContainerSandboxExecutor, NativeCommandBuilder
from roomkit_sandbox.smolbsd_backend import SmolBSDSandboxBackend

backend = SmolBSDSandboxBackend(smolbsd_dir="/path/to/smolBSD")
sandbox = ContainerSandboxExecutor(
    backend=backend,
    command_builder=NativeCommandBuilder(),  # No RTK on NetBSD
)
```

**Requirements:**
- Linux with KVM (`/dev/kvm` readable and writable by the roomkit user), QEMU and `socat`
- `passt`, with QEMU 10.1 or later, for VMs with network access (see Network below)
- A smolBSD checkout ([github.com/NetBSDfr/smolBSD](https://github.com/NetBSDfr/smolBSD)),
  tested at `3c5fd41` (2026-10-07), at a path of 63 characters at most (unix socket limit)
- The `roomkit-sandbox` guest image built into it (see setup below)

**Setup:**

```bash
# 1. Install prerequisites
sudo apt install curl git bmake qemu-system-x86_64 binutils libarchive-tools gdisk socat jq lsof passt

# 2. Clone smolBSD and fetch its prebuilt builder image (no root needed)
git clone https://github.com/NetBSDfr/smolBSD.git ~/smolBSD
(cd ~/smolBSD && bmake fetchimg)

# 3. Build the guest image, from this repository
smolbsd/build.sh ~/smolBSD NOSUDO=1
# -> ~/smolBSD/images/roomkit-sandbox-amd64:latest.img (NetBSD + git, curl, bash, jq)
```

**How it works:**
- One microVM per session, booted from the shared image in snapshot mode: the
  image never changes. The first command of a session waits ~0.5 s for the boot.
- Commands reach the VM over a VirtIO console socket, served by the image's
  runner (`smolbsd/runner.sh`): no SSH, no port on the host, ~10 ms per command.
- Each session has a workspace disk mounted on `/workspace` (sparse file,
  `workspace_gb`, 8 GiB by default). If the VM dies, the next command boots a
  new one on the same disk; `delete_container` removes it.
- VMs run detached: they survive a roomkit restart and `find_container` picks
  them up again. Their state lives under `state_dir`
  (default `~/.local/state/roomkit-sandbox/smolbsd`).
- The environment given at creation reaches every command; `execute(env=...)`
  adds values for one command.
- Timeouts are enforced inside the VM: the command's process tree is killed and
  the exit code is 124. A VM whose runner stops answering is stopped.

**Network:** on by default, `network=False` turns it off. Nothing outside the
host can reach a VM.
- With `passt` installed and QEMU 10.1 or later, the VM reaches what the host
  reaches (the internet, the LAN, host services listening on its other
  interfaces), like a Docker container, but not the services listening on the
  host's loopback interface. DNS queries go to the host's resolver.
- Without it, VMs get QEMU user-mode networking and the backend logs a warning:
  the guest then reaches every service listening on the host's loopback
  (`127.0.0.1`) through `10.0.2.2`, such as a local database, a dev server or a
  model server. Install `passt`, or use `network=False` for code you do not trust.

**Options:** `memory` (MB, 512), `cpus` (1), `network` (True), `workspace_gb` (8),
`state_dir`, `boot_timeout` (30 s), `extra_env`, `service` and `image_tag` (the
image file, `images/<service>-<arch>:<tag>.img`).

**Characteristics:**
- True VM isolation (separate kernel, hypervisor escape required)
- Native Unix commands (no RTK)
- Ideal for untrusted code execution on a local machine, with `passt` or
  `network=False`

**Tests against real VMs:**

```bash
SMOLBSD_DIR=~/smolBSD uv run pytest tests/test_smolbsd_e2e.py
```

---

## Comparison

| | Docker | Kubernetes | SmolBSD |
|--|--------|------------|---------|
| **Isolation** | Container | Pod | VM |
| **First boot** | ~500ms | ~2-5s | ~0.5s |
| **Reuse** | Instant | Instant | Instant |
| **Image size** | 37MB | 37MB | 1GB |
| **Commands** | RTK | RTK | Native |
| **Production** | Dev/CI | Yes | Local |
| **Install** | `[docker]` | `[kubernetes]` | Manual |
