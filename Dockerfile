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
ARG RTK_VERSION=0.34.0
ARG TARGETARCH=amd64
RUN curl -fsSL "https://github.com/rtk-ai/rtk/releases/download/v${RTK_VERSION}/rtk-linux-${TARGETARCH}" \
    -o /usr/local/bin/rtk \
    && chmod +x /usr/local/bin/rtk \
    && rtk --version

# Non-root user for security
RUN adduser -D -s /bin/bash sandbox
USER sandbox
WORKDIR /workspace

# Keep container running for exec commands
CMD ["tail", "-f", "/dev/null"]
