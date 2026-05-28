# --- x86_64: Alpine (musl) ---
FROM alpine:3.21 AS base-amd64

RUN apk add --no-cache \
    bash \
    git \
    curl \
    jq \
    python3 \
    github-cli

ARG RTK_VERSION=0.34.2
ARG RTK_SHA256_AMD64=419b38216c8b1249cc72386d4bbcfe9e7808bde0af63159c826438da534f9e59
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

ARG RTK_VERSION=0.34.2
ARG RTK_SHA256_ARM64=fc168635cf65715dae5cb4f11cd76044b4c824702d50c328070b78ab31fb6c51
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

USER sandbox
WORKDIR /workspace

# Keep container running for exec commands
CMD ["tail", "-f", "/dev/null"]
