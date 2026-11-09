"""Execute public version probes inside the shared Android toolchain image."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import subprocess

from .android_inputs import tool_inputs


def probe() -> dict:
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
        commands[target] = ([str(ndk / (target + "28-clang")), "--version"], "clang version")
    versions = {}
    for name, (command, expected) in commands.items():
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                check=False, timeout=120, stdin=subprocess.DEVNULL)
        if result.returncode or expected not in result.stdout or len(result.stdout) > 65536:
            raise RuntimeError(f"Unexpected Android tool version probe: {name}\n{result.stdout[:65536]}")
        versions[name] = result.stdout.strip()
    return {"schema": "emulebb.android-toolchain-probe/1", "host": inputs["host"],
            "uid": os.getuid(), "gid": os.getgid(), "inputs": inputs,
            "commandLinePackageRevision": properties["Pkg.Revision"], "probes": versions}


if __name__ == "__main__":
    print(json.dumps(probe(), sort_keys=True))
