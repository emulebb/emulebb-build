ARG BASE_IMAGE=docker.io/library/rust:1.98.1-alpine3.24@sha256:7cc1c22d77d9432f7fe012a70e6d3e555af54c2a6832700ed7d553f1769ae89f
FROM ${BASE_IMAGE}
ARG CROSS_TARGET
RUN if command -v apk >/dev/null; then \
      apk add --no-cache build-base linux-headers binutils cmake perl python3 tar zstd ca-certificates tzdata; \
    else \
      apt-get update && apt-get install -y --no-install-recommends \
        python3 binutils cmake perl zstd ca-certificates tzdata && rm -rf /var/lib/apt/lists/*; \
    fi
# Cross images keep rustup proxies and toolchains under /root. Install the
# pinned toolchain in public executable paths before running as the operator.
RUN set -eu; cargo_bin=$(dirname "$(command -v rustup)"); mkdir -p /usr/local/cargo/bin; \
    if [ "$cargo_bin" != /usr/local/cargo/bin ]; then cp -a "$cargo_bin"/. /usr/local/cargo/bin/; fi
ENV PATH=/usr/local/cargo/bin:${PATH} RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo
RUN set -eu; rustup toolchain install 1.98.1 --profile minimal --no-self-update; \
    if [ -n "$CROSS_TARGET" ]; then \
      rustup target add --toolchain 1.98.1 "$CROSS_TARGET"; \
      if [ "$CROSS_TARGET" = aarch64-unknown-linux-gnu ]; then \
        apt-get update && apt-get install -y --no-install-recommends gcc-aarch64-linux-gnu libc6-dev-arm64-cross \
          && rm -rf /var/lib/apt/lists/*; \
      fi; \
    fi
RUN rustup component add --toolchain 1.98.1 clippy
ARG BUILD_UID
ARG BUILD_GID
# This changes only the image's Cargo home, never host output permissions.
RUN chown -R "$BUILD_UID:$BUILD_GID" /usr/local/cargo
ENV RUSTUP_TOOLCHAIN=1.98.1 PYTHONDONTWRITEBYTECODE=1
