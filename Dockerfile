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
ARG RTK_VERSION=0.34.2
ARG RTK_SHA256=419b38216c8b1249cc72386d4bbcfe9e7808bde0af63159c826438da534f9e59
RUN curl -fsSL "https://github.com/rtk-ai/rtk/releases/download/v${RTK_VERSION}/rtk-x86_64-unknown-linux-musl.tar.gz" \
    -o /tmp/rtk.tar.gz \
    && echo "${RTK_SHA256}  /tmp/rtk.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/rtk.tar.gz -C /usr/local/bin/ rtk \
    && chmod +x /usr/local/bin/rtk \
    && rm /tmp/rtk.tar.gz \
    && rtk --version

# Non-root user for security
RUN adduser -D -s /bin/bash sandbox
USER sandbox
WORKDIR /workspace

# Keep container running for exec commands
CMD ["tail", "-f", "/dev/null"]
