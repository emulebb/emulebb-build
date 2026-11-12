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
import shutil

from .ubuntu_ci import PROFILES
from .ci_linux_tools import tool_inputs, REQUIREMENTS, RUNTIME


def metadata_tools_probe(directory: Path) -> dict:
    """Exercise installed metadata validators without network or product execution."""
    desktop = directory / "synthetic-probe.desktop"
    desktop.write_text("[Desktop Entry]\nType=Application\nName=Synthetic probe\nExec=true\nTerminal=true\n",
                       encoding="utf-8")
    metainfo = directory / "org.example.synthetic-probe.metainfo.xml"
    metainfo.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<component type="generic">'
        '<id>org.example.synthetic-probe</id><name>Synthetic probe</name>'
        '<summary>Verify installed metadata validation tools</summary>'
        '<metadata_license>CC0-1.0</metadata_license><project_license>MIT</project_license>'
        '<url type="homepage">https://example.invalid/</url></component>\n', encoding="utf-8")
    for command in (["desktop-file-validate", str(desktop)],
                    ["appstreamcli", "validate", "--no-net", str(metainfo)]):
        subprocess.run(command, check=True, capture_output=True, timeout=20)
    return {"desktopFileValidation": "passed", "appstreamValidation": "passed"}


def appimage_tools_probe(directory: Path, appimagetool: Path, runtime: Path) -> dict:
    """Assemble and extract neutral bytes with the production tool, offline and without FUSE."""
    from .release import _run_packaging_tool
    native_true = shutil.which("true")
    if native_true is None:
        raise RuntimeError("Synthetic AppImage requires the installed native coreutils true binary")
    appdir = directory / "Synthetic.AppDir"
    appdir.mkdir()
    shutil.copy2(native_true, appdir / "AppRun")
    (appdir / "AppRun").chmod(0o755)
    (appdir / "synthetic-probe.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Synthetic probe\nExec=AppRun\n"
        "Icon=synthetic-probe\nTerminal=true\nCategories=Utility;\n", encoding="utf-8")
    (appdir / "synthetic-probe.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">'
        '<rect width="16" height="16" fill="#336699"/></svg>\n', encoding="utf-8")
    (appdir / "synthetic-payload.txt").write_bytes(b"Synthetic AppImage packaging proof\n")
    expected = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in appdir.iterdir()}
    image = directory / "synthetic-probe.AppImage"
    runtime_digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
    # WHY: the real assembly path exercises file(1) and the bundled squashfs
    # builder. Its pinned runtime prevents downloads; GNU timeout bounds this
    # existing packaging helper and its process group without a second runner.
    _run_packaging_tool(("timeout", "--signal=TERM", "--kill-after=3s", "45s",
                         str(appimagetool), "--runtime-file", str(runtime), str(appdir), str(image)),
                        "Synthetic appimagetool")
    if not image.is_file() or image.is_symlink():
        raise RuntimeError("Synthetic AppImage assembly produced no regular image")
    # appimagetool embeds the image MD5 in the output runtime's .digest_md5
    # section. Bind its verified input; the output prefix is intentionally changed.
    if hashlib.sha256(runtime.read_bytes()).hexdigest() != runtime_digest:
        raise RuntimeError("Synthetic AppImage assembly changed its pinned runtime input")
    image.chmod(0o755)
    extracted = directory / "extracted"
    extracted.mkdir()
    subprocess.run([str(image), "--appimage-extract"], cwd=extracted,
                   check=True, capture_output=True, timeout=30)
    root = extracted / "squashfs-root"
    for name, digest in expected.items():
        path = root / name
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError("Synthetic AppImage extracted payload differs: " + name)
    return {"nonrootAppImagePackaging": "passed", "appimageExtraction": "no-fuse",
            "appimagePayloadFiles": sorted(expected), "appimageAppRunSha256": expected["AppRun"],
            "appimageRuntimeSha256": runtime_digest}


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
    node_notice = Path("/opt/emulebb/licenses/Node-LICENSE").read_bytes()
    if b"Node.js" not in node_notice or len(node_notice) < 1000:
        raise RuntimeError("Prepared Node distribution lacks its bundled third-party notices")
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
        metadata_tools = metadata_tools_probe(Path(directory))
        appimage_tools = appimage_tools_probe(Path(directory), appimage, runtime)
    return {"schema": "emulebb.prepared-linux-probe/1", "status": "passed", "architecture": architecture,
            "uid": os.geteuid(), "rustc": rust, "cargo": cargo, "clippy": clippy, "rustfmt": rustfmt,
            "python": platform.python_version(), "installedPackages": installed.splitlines(),
            "packagingTools": {"node": node, "npm": npm, "pythonPackages": packages,
                               "nodeLicenseSha256": hashlib.sha256(node_notice).hexdigest(),
                               "git": output("git", "--version"), "appimagetool": appimage_version,
                               "appimageRuntimeSha256": runtime_pin["sha256"][machine],
                               "requirementsSha256": hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest(),
                               "nonrootDebPackaging": "passed", **metadata_tools, **appimage_tools},
            "pythonRuntime": {"openssl": ssl.OPENSSL_VERSION, "sqlite": sqlite3.sqlite_version,
                              "caCertificates": certificates, "venvExecution": "passed",
                              "compressionRoundTrips": ["bz2", "lzma", "zlib", "zstd"]},
            "provisioning": provisioning}


if __name__ == "__main__":
    print(json.dumps(probe(), sort_keys=True))
