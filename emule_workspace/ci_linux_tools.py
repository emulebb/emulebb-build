"""Verified packaging tools installed only while constructing the CI image."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

from .python_runtime import download_verified

ASSETS = Path(__file__).parent / "release_assets"
CONFIG = ASSETS / "ci_linux/toolchain.json"
REQUIREMENTS = ASSETS / "ci_linux/requirements.txt"
RUNTIME = ASSETS / "appimage/type2-runtime.json"


def tool_inputs() -> dict:
    """Read maintained tool authorities; URLs and versions are not runtime input."""
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def install(destination: Path, *, downloader=download_verified, runner=subprocess.run) -> dict:
    """Install only hash-pinned archives and requirements; never run product code."""
    arch = {"x86_64": "x86_64", "aarch64": "aarch64"}.get(platform.machine().lower())
    if platform.system() != "Linux" or arch is None or os.geteuid() != 0:
        raise RuntimeError("Packaging tool installation requires a native Linux image-build root")
    inputs = tool_inputs()
    destination.mkdir(parents=True, exist_ok=True)
    npm = destination / "npm.tgz"
    downloader(inputs["npm"]["url"], npm, inputs["npm"]["sha256"])
    environment = {**os.environ, "PYYAML_FORCE_LIBYAML": "0"}
    commands = (
        ([sys.executable, "-m", "pip", "install", "--require-hashes", "--no-cache-dir",
          "--disable-pip-version-check", "--timeout", "20", "--retries", "2", "-r", str(REQUIREMENTS)], 600),
        (["npm", "install", "--global", "--ignore-scripts", "--offline", str(npm)], 120),
    )
    for command, budget in commands:
        # Use the same privileged GNU timeout model as signed APT provisioning.
        runner(["timeout", "--signal=TERM", "--kill-after=3s", f"{budget - 3}s", *command],
               env=environment, check=True, timeout=budget)
    npm.unlink()
    appimage = destination / "appimagetool"
    info = inputs["appimagetool"]
    downloader(f"https://github.com/AppImage/appimagetool/releases/download/{info['version']}/appimagetool-{arch}.AppImage",
               appimage, info["sha256"][arch])
    appimage.chmod(0o755)
    runtime = json.loads(RUNTIME.read_text(encoding="utf-8"))
    runtime_path = destination / ("runtime-" + arch)
    downloader(f"https://github.com/AppImage/type2-runtime/releases/download/{runtime['version']}/runtime-{arch}",
               runtime_path, runtime["sha256"][arch])
    receipt = {"schema": "emulebb.ci-packaging-tools/1", "architecture": arch, "inputs": inputs,
               "pythonRequirementsSha256": hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest(),
               "appimageRuntime": runtime, "status": "passed"}
    (destination / "inputs.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    arguments = parser.parse_args()
    install(arguments.destination)
