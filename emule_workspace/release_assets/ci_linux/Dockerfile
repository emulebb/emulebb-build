ARG RUST_IMAGE
ARG PYTHON_IMAGE
ARG UBUNTU_IMAGE
ARG NODE_IMAGE
FROM ${RUST_IMAGE} AS rust-toolchain
FROM ${PYTHON_IMAGE} AS python-toolchain
FROM ${NODE_IMAGE} AS node-toolchain
FROM ${UBUNTU_IMAGE}
# Both stages are exact upstream indexes replicated without byte changes.
COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup
COPY --from=python-toolchain /usr/local /usr/local
COPY --from=python-toolchain /etc/ssl/certs /etc/ssl/certs
COPY --from=python-toolchain /usr/share/ca-certificates /usr/share/ca-certificates
COPY --from=node-toolchain /usr/local/bin/node /usr/local/bin/node
COPY --from=node-toolchain /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=node-toolchain /usr/local/LICENSE /opt/emulebb/licenses/Node-LICENSE
ENV PATH=/usr/local/cargo/bin:/usr/local/bin:${PATH} \
    RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo \
    RUSTUP_TOOLCHAIN=1.98.1 PYTHONPATH=/opt/emulebb PYTHONDONTWRITEBYTECODE=1 \
    APPIMAGETOOL=/opt/emulebb/tools/appimagetool APPIMAGE_EXTRACT_AND_RUN=1 NPM_CONFIG_UPDATE_NOTIFIER=false
COPY emule_workspace /opt/emulebb/emule_workspace
RUN ldconfig \
    && python3 -B -m emule_workspace.ubuntu_ci --image-build --report /opt/emulebb/ubuntu-dependencies.json \
    && ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && python3 -B -m emule_workspace.ci_linux_tools --destination /opt/emulebb/tools \
    && rustup component add --toolchain 1.98.1 clippy rustfmt \
    && chmod -R a+rX /usr/local/cargo /usr/local/rustup
# Consumers provide their own uid and writable registry/target mounts. Toolchain
# files stay readable; publication never bakes operator ownership into the image.
WORKDIR /tmp
