ARG RUST_IMAGE
ARG PYTHON_IMAGE
ARG UBUNTU_IMAGE
FROM ${RUST_IMAGE} AS rust-toolchain
FROM ${PYTHON_IMAGE} AS python-toolchain
FROM ${UBUNTU_IMAGE}
# Both stages are exact upstream indexes replicated without byte changes.
COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup
COPY --from=python-toolchain /usr/local /usr/local
COPY --from=python-toolchain /etc/ssl/certs /etc/ssl/certs
COPY --from=python-toolchain /usr/share/ca-certificates /usr/share/ca-certificates
ENV PATH=/usr/local/cargo/bin:/usr/local/bin:${PATH} \
    RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo \
    RUSTUP_TOOLCHAIN=1.98.1 PYTHONPATH=/opt/emulebb PYTHONDONTWRITEBYTECODE=1
COPY emule_workspace/__init__.py emule_workspace/ubuntu_ci.py \
     emule_workspace/ci_linux_image_probe.py /opt/emulebb/emule_workspace/
RUN ldconfig \
    && python3 -B -m emule_workspace.ubuntu_ci --image-build --report /opt/emulebb/ubuntu-dependencies.json \
    && rustup component add --toolchain 1.98.1 clippy rustfmt \
    && chmod -R a+rX /usr/local/cargo /usr/local/rustup
# Consumers provide their own uid and writable registry/target mounts. Toolchain
# files stay readable; publication never bakes operator ownership into the image.
WORKDIR /tmp
