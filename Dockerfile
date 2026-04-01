FROM alpine:3.21

# Core tools: git for repo operations, bash for shell execution,
# curl for downloads, jq for JSON processing, openssh for git+ssh
RUN apk add --no-cache \
    bash \
    git \
    curl \
    jq \
    openssh-client

# Install RTK — static Rust binary, no dependencies
# Checksum verified to prevent supply chain attacks
ARG RTK_VERSION=0.34.0
ARG TARGETARCH=amd64
ARG RTK_SHA256=f1214690ad4bafab8358883385c2597d11174845516a5fd4bbea694ce61266f2
RUN curl -fsSL "https://github.com/rtk-ai/rtk/releases/download/v${RTK_VERSION}/rtk-linux-${TARGETARCH}" \
    -o /usr/local/bin/rtk \
    && echo "${RTK_SHA256}  /usr/local/bin/rtk" | sha256sum -c - \
    && chmod +x /usr/local/bin/rtk \
    && rtk --version

# Non-root user for security
RUN adduser -D -s /bin/bash sandbox
USER sandbox
WORKDIR /workspace

# Keep container running for exec commands
CMD ["tail", "-f", "/dev/null"]
