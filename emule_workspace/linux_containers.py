"""Shared native-Linux and Windows-to-WSL Docker execution for Rust packaging."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


BINFMT_IMAGE = "docker.io/tonistiigi/binfmt@sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0"
ALPINE_IMAGE = "docker.io/library/alpine:3.24@sha256:3f4d6b32e26703c6082839234f43769d5046b3ac7c910033911d1e71d8cd630a"
RUST_IMAGES = {
    "gnu": "docker.io/library/rust:1.98.1-bookworm@sha256:93ce27a88655056a51dbdd8f5f2d7ddc071c7b0070fb288a37b5a285fc83971e",
    "musl": "docker.io/library/rust:1.98.1-alpine3.24@sha256:7cc1c22d77d9432f7fe012a70e6d3e555af54c2a6832700ed7d553f1769ae89f",
}


class LinuxDocker:
    """@brief Execute Docker on Linux or inside the explicitly selected WSL distro."""

    def __init__(self, distro: str = "Ubuntu") -> None:
        if not distro.strip():
            raise ValueError("WSL distribution must be non-empty")
        self.distro = distro
        self.prefix = ["wsl.exe", "-d", distro, "--"] if os.name == "nt" else []

    def path(self, path: Path) -> str:
        """@brief Translate an existing operator path without changing its environment."""
        if not self.prefix:
            return str(path.resolve())
        return subprocess.check_output(
            [*self.prefix, "wslpath", "-a", "-u", path.resolve().as_posix()], text=True,
        ).strip()

    def run(self, *arguments: str, capture: bool = False, timeout: int | None = None):
        """@brief Run a Docker command, propagating missing tools and failed checks."""
        return subprocess.run(
            [*self.prefix, "docker", *arguments], check=True, text=True,
            capture_output=capture, timeout=timeout,
        )

    def preflight(self, output: Path, *, setup_arm64: bool = False) -> dict:
        """@brief Verify both Linux architectures; optionally register pinned ARM64 QEMU."""
        versions = json.loads(self.run("version", "--format", "{{json .}}", capture=True, timeout=30).stdout)
        if setup_arm64 and versions["Server"]["Arch"] != "arm64":
            self.run("run", "--privileged", "--rm", BINFMT_IMAGE, "--install", "arm64", timeout=180)
        builder = self.run("buildx", "inspect", capture=True, timeout=30).stdout
        platforms = []
        for line in builder.splitlines():
            if line.strip().startswith("Platforms:"):
                platforms.extend(value.strip().rstrip("*") for value in line.split(":", 1)[1].split(","))
        missing = {"linux/amd64", "linux/arm64"} - set(platforms)
        if missing:
            raise RuntimeError(f"Docker builder lacks {sorted(missing)}; run linux-containers --setup-arm64")
        # WHY: advertised targets alone do not prove an ARM64 executable runs.
        machine = self.run("run", "--rm", "--network", "none", "--platform", "linux/arm64",
                           ALPINE_IMAGE, "uname", "-m", capture=True, timeout=180).stdout.strip()
        if machine != "aarch64":
            raise RuntimeError(f"ARM64 execution probe returned {machine!r}")
        report = {"docker": versions["Server"]["Version"], "host_arch": versions["Server"]["Arch"],
                  "platforms": platforms, "arm64_execution": machine, "binfmt_image": BINFMT_IMAGE,
                  "alpine_image": ALPINE_IMAGE, "rust_images": RUST_IMAGES}
        path = output / "reports" / "linux-containers" / "preflight.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Linux container preflight passed: {path}")
        return report
