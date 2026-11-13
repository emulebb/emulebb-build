"""Execute public version probes inside the shared Android toolchain image."""
from __future__ import annotations

import json
import argparse
import os
from pathlib import Path
import platform
import re
import subprocess
import zipfile

from .android_inputs import emulator_inputs, emulator_subset, tool_inputs
from .android_install import emulator_layout


def _properties(path: Path) -> dict[str, str]:
    return {key.strip(): value.strip() for line in path.read_text().splitlines()
            if "=" in line and not line.lstrip().startswith("#") for key, value in [line.split("=", 1)]}


def probe(*, apk: bool = False) -> dict:
    """Prove actual native execution; never claim APK or device qualification."""
    inputs = tool_inputs()
    if platform.system() != "Linux" or platform.machine() != "x86_64" or os.getuid() == 0:
        raise RuntimeError("Android tool probes require non-root native Linux x86_64")
    Path.home().mkdir(parents=True, exist_ok=True)
    root = Path("/opt/android-tools")
    if json.loads((root / "tool-inputs.json").read_text()) != inputs:
        raise RuntimeError("Installed Android tool inputs differ from the reviewed pins")
    tools = root / "sdk/cmdline-tools" / inputs["assets"]["command-line-tools"]["version"]
    properties = dict(line.split("=", 1) for line in (tools / "source.properties").read_text().splitlines()
                      if "=" in line and not line.lstrip().startswith("#"))
    properties = {key.strip(): value.strip() for key, value in properties.items()}
    if properties.get("Pkg.Revision") != inputs["assets"]["command-line-tools"]["version"]:
        raise RuntimeError("Installed Android command-line package revision differs from the pin")
    commands = {
        "java": (["java", "-version"], inputs["assets"]["jdk"]["version"].split("+")[0]),
        "gradle": (["gradle", "--version", "--no-daemon"], "Gradle " + inputs["assets"]["gradle"]["version"]),
        # Invoke the separately pinned native CLI, never the SDK's downloader.
        "android-cli": ([str(root / "bin/android"), "--version"], inputs["assets"]["android-cli"]["version"]),
        "adb": (["adb", "version"], inputs["assets"]["platform-tools"]["version"]),
    }
    ndk = root / "sdk/ndk" / inputs["assets"]["ndk"]["version"] / "toolchains/llvm/prebuilt/linux-x86_64/bin"
    for target in ("aarch64-linux-android", "x86_64-linux-android"):
        commands[target] = ([str(ndk / (target + ("34" if apk else "28") + "-clang")), "--version"], "clang version")
    if apk:
        build_tools = root / "sdk/build-tools" / inputs["assets"]["build-tools"]["version"]
        source = Path.home() / "alignment-input.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("probe.txt", "APK build-tool execution probe")
        commands.update({
            "aapt2": ([str(build_tools / "aapt2"), "version"], "Android Asset Packaging Tool"),
            "apksigner": ([str(build_tools / "apksigner"), "version"], "."),
            "zipalign": ([str(build_tools / "zipalign"), "-f", "-P", "16", "4", str(source),
                          str(source.with_name("alignment-output.zip"))], ""),
            "zipalign-check": ([str(build_tools / "zipalign"), "-c", "-P", "16", "4",
                                str(source.with_name("alignment-output.zip"))], ""),
        })
    versions = {}
    for name, (command, expected) in commands.items():
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                check=False, timeout=120, stdin=subprocess.DEVNULL)
        if result.returncode or expected not in result.stdout or len(result.stdout) > 65536:
            raise RuntimeError(f"Unexpected Android tool version probe: {name}\n{result.stdout[:65536]}")
        versions[name] = result.stdout.strip()
    return {"schema": "emulebb.android-apk-toolchain-probe/1" if apk else "emulebb.android-toolchain-probe/1", "host": inputs["host"],
            "uid": os.getuid(), "gid": os.getgid(), "inputs": inputs,
            "commandLinePackageRevision": properties["Pkg.Revision"], "probes": versions}


def probe_emulator(*, require_kvm: bool = False) -> dict:
    """Prove offline tool execution and installed image metadata, not guest boot."""
    if platform.system() != "Linux" or platform.machine() != "x86_64" or os.getuid() == 0:
        raise RuntimeError("Emulator tool probes require non-root native Linux x86_64")
    root = Path("/opt/android-emulator")
    installed = json.loads((root / "emulator-inputs.json").read_text())
    roles = installed.get("assets", {})
    if not isinstance(roles, dict) or "emulator" not in roles:
        raise RuntimeError("Installed emulator input inventory is invalid")
    images = tuple(sorted(role.removeprefix("system-image-") for role in roles if role.startswith("system-image-")))
    inputs = emulator_subset(emulator_inputs(), images)
    if installed != inputs:
        raise RuntimeError("Installed emulator inputs differ from the reviewed pins")
    metadata = {}
    for role, (_archive_root, directory) in emulator_layout(inputs).items():
        properties = _properties(root / directory / "source.properties")
        item = inputs["assets"][role]
        if role == "emulator":
            build = item["url"].rsplit("-", 1)[-1].removesuffix(".zip")
            if properties.get("Pkg.Revision") != item["version"] or properties.get("Pkg.BuildId") != build:
                raise RuntimeError("Installed emulator version/build differs from its pin")
        elif role == "platform-tools":
            if properties.get("Pkg.Revision") != item["version"]:
                raise RuntimeError("Installed emulator ADB package revision differs from its pin")
        else:
            api, revision = item["version"].split("-r")
            tag = "default" if role == "system-image-min" else "page_size_16kb"
            if (properties.get("Pkg.Revision") != revision or properties.get("AndroidVersion.ApiLevel") != api
                    or properties.get("SystemImage.Abi") != "x86_64"
                    or tag not in properties.get("SystemImage.TagId", "").split(",")
                    or properties.get("AndroidVersion.PreviewSdkInt", "0") != "0"):
                raise RuntimeError("Installed emulator guest API/revision/ABI/page-size variant differs from its pin")
        metadata[role] = properties
    Path.home().mkdir(parents=True, exist_ok=True)
    executable = str(root / "emulator/emulator")
    version = subprocess.run([executable, "-no-window", "-version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, timeout=60, check=False)
    expected = re.escape(inputs["assets"]["emulator"]["version"])
    if version.returncode or len(version.stdout) > 65536 or re.search(rf"Android emulator version\s+{expected}(?:\.[0-9]+)?\b", version.stdout) is None:
        raise RuntimeError(f"Unexpected emulator version execution: {version.stdout[:65536]}")
    adb = subprocess.run([str(root / "platform-tools/adb"), "version"], text=True, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=30, check=False)
    adb_version = re.escape(inputs["assets"]["platform-tools"]["version"])
    if (adb.returncode or len(adb.stdout) > 65536
            or re.search(rf"\bVersion\s+{adb_version}(?:-|\s|$)", adb.stdout) is None):
        raise RuntimeError("Unexpected emulator ADB version execution")
    kvm = {"deviceVisible": Path("/dev/kvm").exists(), "usable": False}
    if kvm["deviceVisible"]:
        acceleration = subprocess.run([executable, "-no-window", "-accel-check"], text=True, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=30, check=False)
        kvm["usable"] = acceleration.returncode == 0
    if require_kvm and not kvm["usable"]:
        raise RuntimeError("Hardware-accelerated Android execution requires usable KVM; no software fallback")
    return {"schema": "emulebb.android-emulator-toolchain-probe/1", "inputs": inputs,
            "uid": os.getuid(), "gid": os.getgid(), "metadata": metadata,
            "versionOutput": version.stdout.strip(), "adbVersionOutput": adb.stdout.strip(),
            "kvm": kvm, "runtimeExecution": "not-run"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emulator", action="store_true")
    parser.add_argument("--apk", action="store_true")
    parser.add_argument("--require-kvm", action="store_true")
    arguments = parser.parse_args()
    if arguments.apk and arguments.emulator:
        parser.error("--apk and --emulator are mutually exclusive")
    if arguments.require_kvm and not arguments.emulator:
        parser.error("--require-kvm requires --emulator")
    print(json.dumps(probe_emulator(require_kvm=arguments.require_kvm) if arguments.emulator else probe(apk=arguments.apk), sort_keys=True))
