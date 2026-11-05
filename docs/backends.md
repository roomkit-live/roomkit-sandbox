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

**Best for:** Kubernetes deployments with an explicit isolation and storage policy.

```python
from roomkit_sandbox import ContainerSandboxExecutor
from roomkit_sandbox.k8s_backend import KubernetesSandboxBackend

backend = KubernetesSandboxBackend(
    image="ghcr.io/roomkit-live/sandbox@sha256:<verified-digest>",
    namespace="sandboxes",
    service_account="sandbox-runtime",
    runtime_class="gvisor",
    workspace_storage_class="quota-enforced-storage",
)
sandbox = ContainerSandboxExecutor(backend=backend, session_id="member-123")
```

Install `roomkit-sandbox[kubernetes]`. The controller needs namespaced RBAC:
`jobs` (batch API): create/get/list/patch/delete; `pods`: get/list/patch/delete;
`pods/attach`: get/create; `persistentvolumeclaims`: create/get/list/delete;
`secrets`: create/get. It does not need `pods/exec` or direct pod creation.

Each command runs as the main process in a new Job's pod. Ending that process,
reaching the deadline, or cancelling the caller retires the pod and its detached
children. No background server survives a completed command. A fixed Job name
admits one command per workspace across controllers. A second simultaneous call
fails explicitly, rather than mutating the workspace concurrently. Each call
pays pod scheduling and volume-mount startup time.

Files persist on a member's PVC: `/workspace`, `/home/sandbox` and `/opt/venv`.
The image's initial virtualenv is copied once; image upgrades must preserve its
Python compatibility or explicitly migrate that environment. `/tmp` is a 256 MiB
memory-backed volume that disappears with the command. The default 4 GiB PVC
request is **not proof of a hard quota**: choose a provisioner that enforces it.
A RuntimeClass is also an explicit choice; Kubernetes without one still shares
its host kernel. Specifying one never permits a native-runtime fallback.

Pods use UID/GID 1000, a read-only root, dropped capabilities, RuntimeDefault
seccomp, no privilege escalation, and no service-account token. Limits are
1 CPU, 512 MiB memory and 1 GiB ephemeral storage. Enforce network policy,
node PID limits, storage capacity, admission policy and aggregate quotas in the
deployment. Files written to memory-backed `/tmp` count against the memory limit.

Command timeouts are integers from 1 to 300 seconds. Creation has a separate
60-second startup budget, accounted in the Job and pod deadlines. Both deadlines
are shortened before releasing the command, using synchronized controller/node
clocks. The Job controller handles a pod that never schedules; the kubelet also
holds the running pod's deadline. Losing the API process does not remove either.
An orphan's result is retained for 120 seconds before Kubernetes garbage-collects
its Job. Until then, the workspace can report busy. TTL-after-finished and the
Job controller must be enabled; a controller outage can delay cleanup.

The caller receives at most 1 MiB of combined stdout/stderr; excess output fails
the command and retires its Job. Four synchronous SDK attachment workers per
API process bound stalled handshakes. Command credentials are delivered on stdin,
never in a pod spec. The fixed creation environment uses an immutable Secret
owned by the workspace; deleting the workspace removes it through Kubernetes GC.

`create_container`/`find_container` return an opaque workspace handle, and
`container_exists` tests workspace readiness, including its environment Secret.
`list_containers` includes idle workspaces. `close` retains files;
`delete_container` explicitly removes them, subject to the storage class's reclaim
policy. Old persistent pods are refused, never auto-deleted: export and verify
workspace, home and virtualenv contents before migrating to the new namespace.

The lifecycle and object builder are parts of this existing backend. Native
Kubernetes Jobs replace custom stale-command recovery; no second scheduler runs.

**Opt-in integration proof:** provision a disposable restricted namespace with a
quota, no workload network access and the above RBAC (plus `pods/log:get` for the
test observer), then run `tests/test_k8s_e2e.py`. Set `KUBECONFIG`,
`SANDBOX_E2E_NAMESPACE`, `SANDBOX_E2E_IMAGE` (digest), `SANDBOX_E2E_RUNTIME_CLASS`
and `SANDBOX_E2E_STORAGE_CLASS`. Tests kill their own controller process, never
existing workloads. They retain real 120-second Job TTLs. A native-runtime run
proves lifecycle only; repeat with the intended isolated runtime before rollout.

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
| **Isolation** | Container | Explicit RuntimeClass | VM |
| **First boot** | ~500ms | ~2-5s | ~0.5s |
| **Reuse** | Instant | Files persist; new pod per command | Instant |
| **Image size** | 37MB | 37MB | 1GB |
| **Commands** | RTK | RTK | Native |
| **Production** | Dev/CI | Yes | Local |
| **Install** | `[docker]` | `[kubernetes]` | Manual |
