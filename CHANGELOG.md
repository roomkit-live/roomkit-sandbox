# Changelog

All notable changes to **roomkit-sandbox** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.1] — 2026-07-03

### Fixed

- **`sandbox_bash` now returns the command's real stdout.** The default
  `RtkCommandBuilder.build_bash` wrapped every command in `rtk err`, which on
  success printed only a `[ok]` line and dropped stdout. For read commands
  (`ls`, `cat`, `find`, `git log`) stdout *is* the payload, so agents got
  nothing back and looped re-running variations to coax out output that never
  came (observed: a repo review that ran `ls` dozens of times). Bash now runs
  the command plainly (`sh -c`); the executor already splits
  stdout/stderr/exit-code, so failures still surface their message. The
  structured tools (`sandbox_read`/`ls`/`grep`/`git`) keep their RTK
  token-optimization — only the general-purpose bash terminal changed.

## [0.3.0] — 2026-06-25

### Added

- **`uv` preinstalled in the sandbox image.** Agents get Astral's fast
  Python package/venv manager for `uv venv`, `uv pip install`, `uv run`,
  and `uvx` inside the sandbox. Copied from the official
  `ghcr.io/astral-sh/uv` image, pinned by version + digest. A ready-to-use
  virtualenv at `/opt/venv` is active by default (`VIRTUAL_ENV` + `PATH`), so
  `uv pip install <pkg>` works without a per-session `uv venv` and stays off
  the externally-managed (PEP 668) system Python.

### Changed

- **Bumped RTK 0.34.2 → 0.42.4** in the sandbox image (both arches);
  per-arch checksums updated accordingly.

### Fixed

- **`sandbox_bash` surfaces real errors now.** `build_bash` mapped to
  `rtk summary`, which can collapse a failed command to a bare
  "[error] N errors" count with no message — leaving the agent unable to
  self-correct (observed: `uv pip install` → "No virtual environment found"
  was hidden). It now uses `rtk err`, which reports the actual
  errors/warnings verbatim on failure (and a short "[ok]" on success).

## [0.2.4] — 2026-05-28

### Added

- **GitHub CLI (`gh`) preinstalled in the sandbox image.** Agents can
  now run `gh` directly inside sandboxes for issues, PRs, releases,
  and API calls without bundling the binary at runtime. Installed
  from `community/github-cli` on the Alpine base (amd64) and from
  the official `cli.github.com` apt repo on the Debian Trixie base
  (arm64).

### Changed

- **Multi-arch image build (`linux/amd64` + `linux/arm64`).** The
  Dockerfile now uses two staged bases — Alpine 3.21 for amd64 and
  Debian Trixie slim for arm64 — selected via `TARGETARCH`. The arm64
  base ships glibc because the upstream RTK `aarch64` binary is built
  against `aarch64-unknown-linux-gnu`. RTK checksums are now declared
  per architecture (`RTK_SHA256_AMD64`, `RTK_SHA256_ARM64`).

## [0.2.3] — 2026-05-22

### Fixed

- **`KubernetesSandboxBackend`: 401 Unauthorized on every K8s API call
  in-cluster.** The `kubernetes` Python client v36 (and earlier) has an
  identifier mismatch: `load_incluster_config()` writes the
  ServiceAccount token to `Configuration.api_key["authorization"]`
  (legacy), but the generated API methods authenticate via
  `auth_settings=["BearerToken"]` and look up
  `api_key["BearerToken"]`. The lookup misses → no `Authorization`
  header is sent → the API server returns 401, even with a perfectly
  valid token. Verified live: raw `curl` with the same projected
  token returned HTTP 200, the python client returned 401.

  Wired a `refresh_api_key_hook` that re-reads the token from
  `/var/run/secrets/kubernetes.io/serviceaccount/token` on every call
  and writes it under `BearerToken` (with the `Bearer` prefix). The
  hook also covers projected SA token rotation (~1h via
  `BoundServiceAccountTokenVolume`) since it re-reads on each call.
  Seeded once at startup so the very first call doesn't depend on the
  hook firing. Drop this block once `kubernetes-client` ships the fix
  in a release (currently on master, unreleased as of v36.0.0).

## [0.2.2] — 2026-04-02

### Fixed

- **K8s label value sanitization.** Session IDs containing colons
  (e.g. `sandbox:user-id`) caused 422 Unprocessable Entity errors from
  the Kubernetes API when creating sandbox pods, because `:` is not
  valid in K8s label values.

### Changed

- New `_k8s_label_value()` helper that replaces invalid characters with
  dashes; applied to pod labels in `create_container()` and label
  selectors in `find_container()`.

## [0.2.1] — 2026-04-02

### Fixed

- **`_build_edit` rewritten to use `awk`.** The sandbox container has
  no Python; `python3 -c` calls were silently failing.
- **SmolBSD env setup**: fixed broken single-quote nesting that garbled
  environment variables.
- **SmolBSD `exec_command`**: removed duplicate error handling; timeout
  / exception handling is now consistent across all three backends.
- **`_build_delete`** now emits a clear error message on failure
  instead of failing silently.
- **`_k8s_label`** sanitizes all invalid characters and truncates to
  63 chars (K8s limit).

### Security

- Added env-key validation (`_ENV_KEY_RE`) to K8s and SmolBSD backends
  — prevents shell injection via malformed environment variable names.

### Changed

- Extracted `ExecResult` and `DEFAULT_IMAGE` into `_shared.py` —
  eliminated 3× duplication.
- Renamed `backend.py` → `docker_backend.py` for clarity.
- Removed redundant `_exec_api` from K8s backend (identical duplicate
  of `_core_api`).
- Standardised on `asyncio.to_thread` across all backends (was mixed
  with `run_in_executor`).
- Added `delete_container` to `ContainerBackendProtocol`.
- Typed `_BUILDERS` dict and Protocol `exec_command` return with
  proper signatures.

### Tests

- 51 tests passing (up from 36).
- Added edge-case tests for write/edit/delete builders (special chars,
  quotes, newlines, spaces).

## [0.2.0] — 2026-04-02

### Added

- **`KubernetesSandboxBackend`** — production-ready K8s backend for
  sandbox execution.
  - Lightweight pods: 512 MiB RAM, 1 CPU (vs 4 GiB for Claude Code
    containers).
  - K8s exec stream API for command execution.
  - Label-based pod discovery and session caching.
  - Non-root execution (UID 1000), configurable namespace /
    service-account / pull-secret.

### Install

```bash
pip install roomkit-sandbox[docker]       # Docker only
pip install roomkit-sandbox[kubernetes]   # Kubernetes only
pip install roomkit-sandbox[docker,kubernetes]
```

## [0.1.0] — 2026-04-01

Initial release. Container-based sandbox executor for RoomKit — gives
AI agents sandboxed command execution via [RTK](https://github.com/rtk-ai/rtk).

### Added

- **`ContainerSandboxExecutor`** — manages container lifecycle,
  routes tool calls to RTK commands, supports setup commands for
  repo cloning.
- **7 sandbox tools**: `sandbox_read`, `sandbox_ls`, `sandbox_grep`,
  `sandbox_find`, `sandbox_git`, `sandbox_diff`, `sandbox_bash`.
- **RTK command builder** — maps tool arguments to `rtk` CLI flags
  with token-optimised output (60-90% fewer tokens).
- **`DockerSandboxBackend`** — standalone Docker adapter with
  label-based container discovery and reuse.
- **Per-call timeout** — `sandbox_bash` supports custom timeout via
  tool arguments.
- **Race-safe** — `asyncio.Lock` on container creation,
  `threading.Lock` on Docker client init.

### Docker image

```bash
docker pull ghcr.io/roomkit-live/sandbox:latest
```

Alpine 3.21 + RTK 0.34.2 + git + bash + curl + jq — **37 MB**.

### Requirements

- Python 3.12+
- `roomkit >= 0.7.0a8` (with `SandboxExecutor` ABC)
- `docker >= 7.0` (optional, for standalone use)

[Unreleased]: https://github.com/roomkit-live/roomkit-sandbox/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/roomkit-live/roomkit-sandbox/compare/v0.2.4...v0.3.0
[0.2.4]: https://github.com/roomkit-live/roomkit-sandbox/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/roomkit-live/roomkit-sandbox/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/roomkit-live/roomkit-sandbox/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/roomkit-live/roomkit-sandbox/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/roomkit-live/roomkit-sandbox/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/roomkit-live/roomkit-sandbox/releases/tag/v0.1.0
