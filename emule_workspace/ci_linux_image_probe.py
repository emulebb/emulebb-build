"""Native non-root compiler-image evidence, without running product code."""
from __future__ import annotations

import json
import os
import platform
from pathlib import Path
import subprocess
import tempfile
import venv
import hashlib
import importlib.metadata

from .ubuntu_ci import PROFILES
from .ci_linux_tools import tool_inputs, REQUIREMENTS, RUNTIME


def probe() -> dict:
    """Require the real installed toolchain and dependency material inventory."""
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine().lower())
    if platform.system() != "Linux" or architecture is None or os.geteuid() == 0:
        raise RuntimeError("Prepared compiler must run on a native Linux CPU as a non-root user")
    def output(*args):
        return subprocess.check_output(args, text=True, timeout=20).strip()
    rust = output("rustc", "--version")
    cargo = output("cargo", "--version")
    clippy = output("cargo", "clippy", "--version")
    rustfmt = output("rustfmt", "--version")
    if not rust.startswith("rustc 1.98.1 ") or not cargo.startswith("cargo 1.98.1 ") or platform.python_version() != "3.15.2":
        raise RuntimeError("Prepared compiler toolchain differs from the exact reviewed pins")
    # Loading these extensions catches missing dependencies in the copied
    # pinned CPython stage, rather than claiming a version-only executable works.
    import bz2
    import ctypes
    import lzma
    import readline
    import sqlite3
    import ssl
    import uuid
    import zlib
    from compression import zstd
    sample = b"synthetic compiler image dependency probe"
    for codec in (bz2, lzma, zlib, zstd):
        if codec.decompress(codec.compress(sample)) != sample:
            raise RuntimeError("Prepared Python compression dependency failed")
    certificates = ssl.create_default_context().cert_store_stats()
    if certificates.get("x509_ca", 0) <= 0:
        raise RuntimeError("Prepared Python lacks its verified-stage CA roots")
    with sqlite3.connect(":memory:") as database:
        if database.execute("select 1").fetchone() != (1,):
            raise RuntimeError("Prepared Python SQLite runtime failed")
    with tempfile.TemporaryDirectory(prefix="emulebb-venv-probe-") as directory:
        environment = Path(directory) / "venv"
        venv.EnvBuilder(with_pip=False).create(environment)
        subprocess.run([str(environment / "bin/python"), "-B", "-c",
                        "import ssl,sqlite3; from compression import zstd"], check=True, timeout=20)
    installed = output("dpkg-query", "--show", "--showformat=${Package}\t${Version}\t${Architecture}\\n",
                       *PROFILES["rust-prepared-builder"])
    provisioning = json.loads(Path("/opt/emulebb/ubuntu-dependencies.json").read_text(encoding="utf-8"))
    if provisioning.get("status") != "passed" or provisioning.get("architecture") != architecture:
        raise RuntimeError("Prepared compiler lacks its successful native signed APT receipt")
    inputs = tool_inputs()
    node, npm = output("node", "--version"), output("npm", "--version")
    if node != "v" + inputs["node"]["version"] or npm != inputs["npm"]["version"]:
        raise RuntimeError("Prepared frontend tools differ from the reviewed pins")
    packages = {name: importlib.metadata.version(name) for name in inputs["pythonPackages"]}
    if packages != inputs["pythonPackages"]:
        raise RuntimeError("Prepared Python CLI dependencies differ from the reviewed pins")
    # Import the real CLI dependency graph without fabricating a hosted context.
    from . import cli
    machine = "x86_64" if architecture == "amd64" else "aarch64"
    appimage = Path("/opt/emulebb/tools/appimagetool")
    if hashlib.sha256(appimage.read_bytes()).hexdigest() != inputs["appimagetool"]["sha256"][machine]:
        raise RuntimeError("Prepared AppImage builder differs from its verified archive")
    appimage_version = output(str(appimage), "--version")
    runtime = Path("/opt/emulebb/tools/runtime-" + machine)
    runtime_pin = json.loads(RUNTIME.read_text(encoding="utf-8"))
    if hashlib.sha256(runtime.read_bytes()).hexdigest() != runtime_pin["sha256"][machine]:
        raise RuntimeError("Prepared AppImage runtime differs from its reviewed pin")
    # Exercise actual non-root package-tool output, with no dependency download.
    with tempfile.TemporaryDirectory(prefix="emulebb-deb-probe-") as directory:
        root = Path(directory) / "package"
        (root / "DEBIAN").mkdir(parents=True)
        (root / "DEBIAN/control").write_text(
            f"Package: synthetic-ci-probe\nVersion: 1.0\nArchitecture: {architecture}\n"
            "Maintainer: Synthetic <fixture@example.invalid>\nDescription: Toolchain probe\n", encoding="utf-8")
        subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(root), str(Path(directory) / "probe.deb")],
                       check=True, capture_output=True, timeout=20)
    return {"schema": "emulebb.prepared-linux-probe/1", "status": "passed", "architecture": architecture,
            "uid": os.geteuid(), "rustc": rust, "cargo": cargo, "clippy": clippy, "rustfmt": rustfmt,
            "python": platform.python_version(), "installedPackages": installed.splitlines(),
            "packagingTools": {"node": node, "npm": npm, "pythonPackages": packages,
                               "git": output("git", "--version"), "appimagetool": appimage_version,
                               "appimageRuntimeSha256": runtime_pin["sha256"][machine],
                               "requirementsSha256": hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest(),
                               "nonrootDebPackaging": "passed"},
            "pythonRuntime": {"openssl": ssl.OPENSSL_VERSION, "sqlite": sqlite3.sqlite_version,
                              "caCertificates": certificates, "venvExecution": "passed",
                              "compressionRoundTrips": ["bz2", "lzma", "zlib", "zstd"]},
            "provisioning": provisioning}


if __name__ == "__main__":
    print(json.dumps(probe(), sort_keys=True))
