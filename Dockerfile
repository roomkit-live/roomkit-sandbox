# --- x86_64: Alpine (musl) ---
FROM alpine:3.21 AS base-amd64

RUN apk add --no-cache \
    bash \
    git \
    curl \
    jq \
    python3 \
    github-cli

ARG RTK_VERSION=0.42.4
ARG RTK_SHA256_AMD64=34975116da11e09e502501daf758143e0b22ed3a42a10eb67fb693a6270d9e36
RUN curl -fsSL "https://github.com/rtk-ai/rtk/releases/download/v${RTK_VERSION}/rtk-x86_64-unknown-linux-musl.tar.gz" \
    -o /tmp/rtk.tar.gz \
    && echo "${RTK_SHA256_AMD64}  /tmp/rtk.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/rtk.tar.gz -C /usr/local/bin/ rtk \
    && chmod +x /usr/local/bin/rtk \
    && rm /tmp/rtk.tar.gz \
    && rtk --version

RUN adduser -D -s /bin/bash sandbox

# --- ARM64: Debian Trixie (glibc, required by RTK ARM binary) ---
FROM debian:trixie-slim AS base-arm64

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    git \
    curl \
    jq \
    python3 \
    ca-certificates \
    gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/*

ARG RTK_VERSION=0.42.4
ARG RTK_SHA256_ARM64=cc2b91c064eb670c097c184913c8fbcb1a943d53d7fe505375e96ba0c5b6459f
RUN curl -fsSL "https://github.com/rtk-ai/rtk/releases/download/v${RTK_VERSION}/rtk-aarch64-unknown-linux-gnu.tar.gz" \
    -o /tmp/rtk.tar.gz \
    && echo "${RTK_SHA256_ARM64}  /tmp/rtk.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/rtk.tar.gz -C /usr/local/bin/ rtk \
    && chmod +x /usr/local/bin/rtk \
    && rm /tmp/rtk.tar.gz \
    && rtk --version

RUN useradd -m -s /bin/bash sandbox

# --- Final: select base by TARGETARCH ---
ARG TARGETARCH
FROM base-${TARGETARCH} AS final

# uv — fast Python package/venv manager, so agents can install deps, create
# venvs, and run scripts inside the sandbox. Copied from the official Astral
# image (statically linked, runs on both the Alpine and Debian bases); pinned
# by version + digest for a verifiable supply chain. buildx pulls the arch
# matching the target platform automatically.
COPY --from=ghcr.io/astral-sh/uv:0.11.24@sha256:99ea34acedc870ba4ad11a1f540a1c04267c9f30aadc465a94406f52dfda2c36 /uv /uvx /usr/local/bin/

USER sandbox
WORKDIR /workspace

# Keep container running for exec commands
CMD ["tail", "-f", "/dev/null"]
