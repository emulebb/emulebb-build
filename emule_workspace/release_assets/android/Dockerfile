FROM docker.io/library/rust:1.98.1-bookworm@sha256:93ce27a88655056a51dbdd8f5f2d7ddc071c7b0070fb288a37b5a285fc83971e
RUN apt-get update && apt-get install --no-install-recommends -y python3 unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY emule_workspace/__init__.py emule_workspace/python_runtime.py \
     emule_workspace/android_inputs.py emule_workspace/android_install.py /opt/emulebb/emule_workspace/
COPY emule_workspace/release_assets/android/toolchain.json /opt/emulebb/emule_workspace/release_assets/android/toolchain.json
ENV PYTHONPATH=/opt/emulebb PYTHONDONTWRITEBYTECODE=1
RUN --mount=type=bind,from=android-inputs,target=/inputs \
    python3 -B -m emule_workspace.android_install --cache /inputs --destination /opt/android-tools
ENV JAVA_HOME=/opt/android-tools/jdk \
    ANDROID_HOME=/opt/android-tools/sdk \
    ANDROID_SDK_ROOT=/opt/android-tools/sdk \
    ANDROID_NDK_HOME=/opt/android-tools/sdk/ndk/30.0.16248370
ENV PATH=/opt/android-tools/bin:/opt/android-tools/jdk/bin:/opt/android-tools/gradle/bin:/opt/android-tools/sdk/platform-tools:/opt/android-tools/sdk/cmdline-tools/23.0/bin:${PATH}
RUN rustup target add --toolchain 1.98.1 aarch64-linux-android x86_64-linux-android \
    && rustup component add --toolchain 1.98.1 clippy
COPY emule_workspace/android_probe.py /opt/emulebb/emule_workspace/android_probe.py
