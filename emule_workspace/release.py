"""Release package orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import tomllib
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .build import (
    APP_EXE_NAME,
    DIAGNOSTICS_APP_EXE_NAME,
    app_property_overrides,
    build_emulebb_rust_client,
    ensure_app_dependency_artifacts,
    staged_emulebb_rust_root,
    verify_app_control_flow_guard,
    with_trailing_separator,
)
from .artifact_names import utc_run_id
from .build_state import BuildSession
from .config import (
    AmutorrentPackageOptions,
    EmulebbRustPackageOptions,
    QbittorrentbbPackageOptions,
    ReleasePackageOptions,
    WorkspaceOptions,
)
from .git import git_output, repo_branch, repo_head, repo_status_lines
from .layout import AppVariant, WorkspaceLayout
from .msbuild import env_override, invoke_msbuild_project
from .qbittorrentbb_runtime import stage_qbittorrentbb_runtime

PE_MACHINES = {"x64": 0x8664, "ARM64": 0xAA64}
STARTUP_DIAGNOSTICS_BINARY_MARKERS = (
    b"emulebb-diagnostics-startup.trace.json",
    "emulebb-diagnostics-startup.trace.json".encode("utf-16le"),
)
PACKET_DIAGNOSTICS_BINARY_MARKERS = (
    b"emulebb-diagnostics-packet.log",
    "emulebb-diagnostics-packet.log".encode("utf-16le"),
)
UPLOAD_SLOT_DIAGNOSTICS_BINARY_MARKERS = (
    b"emulebb-diagnostics-upload-slot.log",
    "emulebb-diagnostics-upload-slot.log".encode("utf-16le"),
)
DOWNLOAD_SLOT_DIAGNOSTICS_BINARY_MARKERS = (
    b"emulebb-diagnostics-download-slot.log",
    "emulebb-diagnostics-download-slot.log".encode("utf-16le"),
)
BAD_PEER_DIAGNOSTICS_BINARY_MARKERS = (
    b"emulebb-diagnostics-bad-peer.log",
    "emulebb-diagnostics-bad-peer.log".encode("utf-16le"),
)
KAD_DIAGNOSTICS_BINARY_MARKERS = (
    b"emulebb-diagnostics-kad.log",
    "emulebb-diagnostics-kad.log".encode("utf-16le"),
)
AMUTORRENT_NODE_VERSION = "v24.16.0"
AMUTORRENT_NODE_ARCHIVES = {
    "x64": (
        "node-v24.16.0-win-x64.zip",
        "edaca9bd58ec8e92037dac4e877d52f6b8f430b81c18b57e264b4e2fb111cd56",
    ),
    "ARM64": (
        "node-v24.16.0-win-arm64.zip",
        "14834611d4c6b3c06054e7007732b90474c16e0b32f395e05b55a571ef71c6d2",
    ),
}
RELEASE_THIRD_PARTY_COMPONENTS = (
    ("Crypto++", "Boost-1.0"),
    ("id3lib", "LGPL-2.0-only"),
    ("miniupnpc", "BSD-3-Clause"),
    ("libpcpnatpmp", "NOASSERTION"),
    ("ResizableLib", "Artistic-2.0"),
    ("zlib", "Zlib"),
    ("Mbed TLS / TF-PSA-Crypto", "Apache-2.0 OR GPL-2.0-or-later"),
    ("nlohmann/json", "MIT"),
)
RELEASE_VERSION_PATTERN = re.compile(
    r"\d+\.\d+\.\d+(?:-(?:(?:rc|beta)\.\d+|nightly\.\d{8}\.[0-9a-f]{7,40}))?"
)
RELEASE_VERSION_FORMAT = "MAJOR.MINOR.PATCH[-rc.N|-beta.N|-nightly.YYYYMMDD.SHA]"
SIGNING_CERT_SHA1_ENV = "EMULEBB_RELEASE_SIGN_CERT_SHA1"
SIGNING_CERT_PATH_ENV = "EMULEBB_RELEASE_SIGN_CERT_PATH"
SIGNING_CERT_PASSWORD_ENV = "EMULEBB_RELEASE_SIGN_CERT_PASSWORD"
SIGNING_TIMESTAMP_URL_ENV = "EMULEBB_RELEASE_SIGN_TIMESTAMP_URL"
SIGNTOOL_PATH_ENV = "EMULEBB_SIGNTOOL"
DEFAULT_SIGNING_TIMESTAMP_URL = "http://timestamp.digicert.com"
EMULEBB_RUNTIME_SCRIPT_PATHS = (
    "scripts/Bootstrap-eMuleBBSuite.ps1",
    "scripts/Import-SuiteAppManifest.ps1",
    "scripts/Install-eMuleBBSuite.ps1",
    "scripts/Repair-Firewall.ps1",
    "scripts/Set-DefenderExclusions.ps1",
    "scripts/Enable-LongPaths.ps1",
    "scripts/Register-Prowlarr.ps1",
    "scripts/Register-ArrStack.ps1",
    "scripts/Register-aMuTorrent.ps1",
    "scripts/Initialize-Suite.ps1",
    "scripts/Start-eMuleBB.ps1",
    "scripts/Start-Suite.ps1",
    "scripts/Stop-Suite.ps1",
    "scripts/Get-SuiteStatus.ps1",
    "scripts/Get-SuiteInfo.ps1",
    "scripts/Repair-Suite.ps1",
)
EMULEBB_CONFIG_ASSET_PATHS = (
    "config/suite-apps.json",
    "config/suite-languages.json",
)
EMULEBB_AUTOMATION_EXAMPLE_PATHS = (
    "automation/README.md",
    "automation/Import-eMuleBBRestExample.ps1",
    "automation/Get-eMuleBBStatus.ps1",
    "automation/Set-eMuleBBLimits.ps1",
    "automation/Search-eMuleBB.ps1",
    "automation/Download-ReleaseGroup.ps1",
)
EMULEBB_SKIN_ASSET_PATHS = (
    "skins/emulebb-slate.eMuleSkin.ini",
    "skins/emulebb-graphite.eMuleSkin.ini",
    "skins/emulebb-midnight.eMuleSkin.ini",
    "skins/emulebb-steel.eMuleSkin.ini",
    "skins/emulebb-moss.eMuleSkin.ini",
    "skins/emulebb-daylight-soft.eMuleSkin.ini",
    "skins/emulebb-retro-teal.eMuleSkin.ini",
    "skins/emulebb-phosphor-green.eMuleSkin.ini",
    "skins/emulebb-slate.eMuleToolbar.kad02.bmp",
    "skins/emulebb-graphite.eMuleToolbar.kad02.bmp",
    "skins/emulebb-midnight.eMuleToolbar.kad02.bmp",
    "skins/emulebb-steel.eMuleToolbar.kad02.bmp",
    "skins/emulebb-moss.eMuleToolbar.kad02.bmp",
    "skins/emulebb-daylight-soft.eMuleToolbar.kad02.bmp",
    "skins/emulebb-retro-teal.eMuleToolbar.kad02.bmp",
    "skins/emulebb-phosphor-green.eMuleToolbar.kad02.bmp",
)
EMULEBB_RUNTIME_ASSET_PATHS = (*EMULEBB_RUNTIME_SCRIPT_PATHS, *EMULEBB_CONFIG_ASSET_PATHS, *EMULEBB_SKIN_ASSET_PATHS)
EMULEBB_AUTOMATION_EXAMPLE_ASSET_ROOT_NAME = "emulebb_automation_examples"
EMULEBB_RUST_PACKAGE_ROOT_NAME = "emulebb-rust"
EMULEBB_RUST_PACKAGE_REQUIRED_WEBUI_ENTRY = f"{EMULEBB_RUST_PACKAGE_ROOT_NAME}/webui/index.html"


@dataclass(frozen=True)
class ReleasePackageFlavorSpec:
    """Build and artifact naming policy for one eMuleBB package flavor."""

    name: str
    asset_suffix: str
    enable_startup_diagnostics: bool
    enable_packet_diagnostics: bool
    enable_upload_slot_diagnostics: bool
    enable_download_slot_diagnostics: bool
    enable_bad_peer_diagnostics: bool
    enable_kad_diagnostics: bool
    executable_name: str
    diagnostic_features: tuple[str, ...] = ()


RELEASE_PACKAGE_FLAVORS = (
    ReleasePackageFlavorSpec(
        name="standard",
        asset_suffix="",
        enable_startup_diagnostics=False,
        enable_packet_diagnostics=False,
        enable_upload_slot_diagnostics=False,
        enable_download_slot_diagnostics=False,
        enable_bad_peer_diagnostics=False,
        enable_kad_diagnostics=False,
        executable_name=APP_EXE_NAME,
    ),
    ReleasePackageFlavorSpec(
        name="diagnostics",
        asset_suffix="-diagnostics",
        enable_startup_diagnostics=True,
        enable_packet_diagnostics=True,
        enable_upload_slot_diagnostics=True,
        enable_download_slot_diagnostics=True,
        enable_bad_peer_diagnostics=True,
        enable_kad_diagnostics=True,
        executable_name=DIAGNOSTICS_APP_EXE_NAME,
        diagnostic_features=(
            "bad-peer-diagnostics",
            "download-slot-diagnostics",
            "kad-diagnostics",
            "packet-diagnostics",
            "startup-diagnostics",
            "upload-slot-diagnostics",
        ),
    ),
)
EMULEBB_PACKAGE_ROOT_NAME = "eMuleBB"
EMULEBB_RELEASE_ASSET_ROOT_NAME = "emulebb"


def create_amutorrent_package(
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    package_options: AmutorrentPackageOptions,
) -> None:
    """Builds the optional aMuTorrent controller ZIP plus manifest."""

    if workspace_options.configuration != "Release":
        raise RuntimeError("package amutorrent requires --config Release.")
    if not _is_release_version(package_options.release_version):
        raise RuntimeError(
            f"Release version must use {RELEASE_VERSION_FORMAT} format: {package_options.release_version}"
        )

    amutorrent_root = layout.resolve_workspace_path("repos/amutorrent")
    if not amutorrent_root.is_dir():
        raise RuntimeError(
            "package amutorrent requires a manual repos/amutorrent checkout; "
            "it is no longer materialized by workspace setup."
        )
    _assert_clean_amutorrent_package_inputs(layout, amutorrent_root)
    asset_arch = "arm64" if workspace_options.platform == "ARM64" else "x64"
    package_build_root = layout.output_packages_root / "build" / "amutorrent" / asset_arch
    staged_source_root = package_build_root / "source"
    static_build_root = package_build_root / "static"
    _assert_path_under_root(package_build_root, layout.output_packages_root, "aMuTorrent package build path")
    if package_options.clean and package_build_root.exists():
        shutil.rmtree(package_build_root)
    node_bin_dir = _ensure_amutorrent_build_node(layout, workspace_options.platform)
    _stage_amutorrent_source(amutorrent_root, staged_source_root)
    _build_amutorrent_webapp(staged_source_root, package_options.clean, static_build_root, node_bin_dir)

    release_root = _release_root_for_version(layout, package_options.release_version)
    staging_root = release_root / "staging" / f"amutorrent-{asset_arch}"
    package_root = staging_root / "aMuTorrent"
    zip_path = release_root / f"emulebb-{package_options.release_version}-amutorrent-{asset_arch}.zip"
    manifest_path = release_root / f"emulebb-{package_options.release_version}-amutorrent-{asset_arch}.manifest.json"
    sbom_path = release_root / f"emulebb-{package_options.release_version}-amutorrent-{asset_arch}.sbom.spdx.json"
    for path_to_check in (staging_root, package_root, zip_path, manifest_path, sbom_path):
        _assert_path_under_root(path_to_check, release_root, "aMuTorrent package path")

    if staging_root.exists():
        shutil.rmtree(staging_root)
    package_root.mkdir(parents=True, exist_ok=True)
    _copy_amutorrent_runtime(staged_source_root, package_root, static_build_root)
    _write_amutorrent_readme(package_root, package_options.release_version, workspace_options.platform)
    _copy_package_file(amutorrent_root / "LICENSE", package_root, Path("LICENSE-aMuTorrent.txt"))
    _write_amutorrent_sbom(
        layout=layout,
        workspace_options=workspace_options,
        package_options=package_options,
        amutorrent_root=amutorrent_root,
        package_root=package_root,
        release_root=release_root,
        asset_name=zip_path.name,
    )

    if zip_path.exists():
        zip_path.unlink()
    release_root.mkdir(parents=True, exist_ok=True)
    _write_zip(staging_root, package_root, zip_path)
    _assert_amutorrent_package_contents(zip_path)

    zip_hash = _sha256(zip_path)
    package_file_hashes = _zip_entry_hashes(zip_path)
    shutil.copy2(package_root / "SBOM.spdx.json", sbom_path)
    sbom_hash = _sha256(sbom_path)
    manifest = _build_amutorrent_manifest(
        layout=layout,
        workspace_options=workspace_options,
        package_options=package_options,
        amutorrent_root=amutorrent_root,
        zip_path=zip_path,
        release_root=release_root,
        zip_hash=zip_hash,
        sbom_path=sbom_path,
        sbom_hash=sbom_hash,
        package_file_hashes=package_file_hashes,
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"aMuTorrent package: {zip_path}")
    print(f"aMuTorrent manifest: {manifest_path}")
    print(f"aMuTorrent SBOM: {sbom_path}")
    print(f"SHA256: {zip_hash}")


def create_qbittorrentbb_package(
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    package_options: QbittorrentbbPackageOptions,
) -> None:
    """Packages qBittorrentBB into a ZIP plus manifest and SBOM.

    Consumes the qbittorrent.exe produced by `build qbittorrentbb-ci` (under the
    output build root) and the qbittorrentbb / emulebb-libtorrent fork checkouts
    (env-overridable, so this runs on CI without a materialized workspace). When
    EMULEBB_QT_PREFIX is set the package is a folder build (like upstream
    qBittorrent): the exe plus its runtime DLLs (Qt6, OpenSSL, zlib, libtorrent,
    discovered via dumpbin) and the Qt plugins. Artifacts are always unsigned.
    """

    if workspace_options.configuration != "Release":
        raise RuntimeError("package qbittorrentbb requires --config Release.")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?", package_options.release_version):
        raise RuntimeError(
            "qBittorrentBB package version must be MAJOR.MINOR.PATCH[-suffix]: "
            f"{package_options.release_version}"
        )

    asset_arch = "arm64" if workspace_options.platform == "ARM64" else "x64"
    exe_source = (
        layout.output_build_root / "qbittorrentbb" / workspace_options.configuration / "qbittorrent.exe"
    )
    if not exe_source.is_file():
        raise RuntimeError(
            f"qbittorrent.exe not found: {exe_source}. Run build qbittorrentbb-ci first."
        )

    qbt_env = os.environ.get("EMULEBB_QBT_REPO", "").strip()
    lt_env = os.environ.get("EMULEBB_LIBTORRENT_REPO", "").strip()
    qbt_root = Path(qbt_env) if qbt_env else layout.resolve_workspace_path("repos/qbittorrentbb")
    lt_root = Path(lt_env) if lt_env else layout.resolve_workspace_path("repos/third_party/emulebb-libtorrent")

    version = package_options.release_version
    release_root = layout.output_release_root / f"qbittorrentbb-{version}"
    staging_root = release_root / "staging" / f"qbittorrentbb-{asset_arch}"
    package_root = staging_root / "qBittorrentBB"
    zip_path = release_root / f"qbittorrentbb-{version}-{asset_arch}.zip"
    manifest_path = release_root / f"qbittorrentbb-{version}-{asset_arch}.manifest.json"
    sbom_path = release_root / f"qbittorrentbb-{version}-{asset_arch}.sbom.spdx.json"
    for path_to_check in (staging_root, package_root, zip_path, manifest_path, sbom_path):
        _assert_path_under_root(path_to_check, release_root, "qBittorrentBB package path")

    if package_options.clean and release_root.exists():
        shutil.rmtree(release_root)
    if staging_root.exists():
        shutil.rmtree(staging_root)
    package_root.mkdir(parents=True, exist_ok=True)

    shutil.copy2(exe_source, package_root / "qbittorrent.exe")

    # Folder build (like upstream qBittorrent): when a dynamic Qt prefix is given,
    # bundle the exe's runtime DLLs (discovered via dumpbin) and the Qt plugins.
    qt_prefix_env = os.environ.get("EMULEBB_QT_PREFIX", "").strip()
    bundled_runtime: list[str] = []
    if qt_prefix_env:
        qt_prefix = Path(qt_prefix_env)
        search_dirs = [
            exe_source.parent,
            qt_prefix / "bin",
            layout.output_third_party_build_root / "deps" / "libtorrent" / "bin",
            layout.output_third_party_build_root / "emulebb-libtorrent" / workspace_options.configuration,
        ]
        staged_runtime = stage_qbittorrentbb_runtime(
            executable=package_root / "qbittorrent.exe",
            target_root=package_root,
            qt_prefix=qt_prefix,
            qbt_root=qbt_root,
            search_dirs=search_dirs,
        )
        bundled_runtime = staged_runtime.bundled_runtime
    linkage = ("dynamic Qt6 folder build (runtime DLLs + plugins bundled)"
               if qt_prefix_env else "static (vcpkg x64-windows-static, single exe)")

    license_source = _qbittorrentbb_license(qbt_root)
    if license_source is not None:
        _copy_package_file(license_source, package_root, Path("COPYING.txt"))
    _write_qbittorrentbb_readme(package_root, version, asset_arch)
    _write_qbittorrentbb_sbom(
        workspace_options=workspace_options,
        qbt_root=qbt_root,
        lt_root=lt_root,
        package_root=package_root,
        release_root=release_root,
        asset_name=zip_path.name,
        version=version,
    )

    if zip_path.exists():
        zip_path.unlink()
    release_root.mkdir(parents=True, exist_ok=True)
    _write_zip(staging_root, package_root, zip_path)

    zip_hash = _sha256(zip_path)
    exe_hash = _sha256(package_root / "qbittorrent.exe")
    package_file_hashes = _zip_entry_hashes(zip_path)
    shutil.copy2(package_root / "SBOM.spdx.json", sbom_path)
    sbom_hash = _sha256(sbom_path)
    manifest = {
        "schema": "emulebb.qbittorrentbb.package/1",
        "name": f"qbittorrentbb-{version}-{asset_arch}",
        "version": version,
        "platform": workspace_options.platform,
        "configuration": workspace_options.configuration,
        "signed": False,
        "linkage": linkage,
        "bundledRuntime": bundled_runtime,
        "builtUtc": datetime.now(timezone.utc).isoformat(),
        "asset": zip_path.name,
        "sha256": zip_hash,
        "executable": "qBittorrentBB/qbittorrent.exe",
        "executableSha256": exe_hash,
        "sbom": sbom_path.name,
        "sbomSha256": sbom_hash,
        "perFileSha256": package_file_hashes,
        "source": {
            "qbittorrentbb": _package_repo_provenance(qbt_root),
            "emulebbLibtorrent": _package_repo_provenance(lt_root),
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"qBittorrentBB package: {zip_path}")
    print(f"qBittorrentBB manifest: {manifest_path}")
    print(f"qBittorrentBB SBOM: {sbom_path}")
    print(f"SHA256: {zip_hash}")


def create_emulebb_rust_package(
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    package_options: EmulebbRustPackageOptions,
) -> None:
    """Builds or reuses the staged Rust runtime and creates native release assets."""

    if workspace_options.configuration != "Release":
        raise RuntimeError("package emulebb-rust requires --config Release.")
    if workspace_options.platform != "x64":
        raise RuntimeError("package emulebb-rust currently supports only --platform x64.")
    if not _is_release_version(package_options.release_version):
        raise RuntimeError(
            f"Rust release version must use {RELEASE_VERSION_FORMAT} format: {package_options.release_version}"
        )
    rust_root = layout.emulebb_rust_repo_root
    if rust_root is None or not rust_root.is_dir():
        raise RuntimeError("package emulebb-rust requires repos/emulebb-rust in the workspace manifest.")
    _assert_emulebb_rust_version_matches(rust_root, package_options.release_version)
    _assert_clean_emulebb_rust_package_inputs(layout, rust_root)

    if not package_options.skip_build:
        session = BuildSession(
            layout=layout,
            options=workspace_options,
            command_name="package emulebb-rust",
            clean=package_options.clean,
            stamp=utc_run_id(),
        )
        try:
            build_emulebb_rust_client(
                session,
                clean=package_options.clean,
                diagnostics=False,
                target_os=package_options.target_os,
            )
        finally:
            session.write_recap()

    if package_options.target_os == "linux":
        _create_emulebb_rust_linux_packages(layout, workspace_options, package_options, rust_root)
        return

    release_root = _emulebb_rust_release_root(layout, package_options.release_version)
    staging_root = release_root / "staging" / "windows-x64"
    package_root = staging_root / EMULEBB_RUST_PACKAGE_ROOT_NAME
    zip_path = release_root / f"emulebb-rust-v{package_options.release_version}-windows-x64.zip"
    manifest_path = release_root / f"emulebb-rust-v{package_options.release_version}-windows-x64.manifest.json"
    sbom_path = release_root / f"emulebb-rust-v{package_options.release_version}-windows-x64.sbom.spdx.json"
    sums_path = release_root / "SHA256SUMS"
    for path_to_check in (release_root, staging_root, package_root, zip_path, manifest_path, sbom_path, sums_path):
        _assert_path_under_root(path_to_check, release_root, "emulebb-rust package path")

    if staging_root.exists():
        shutil.rmtree(staging_root)
    package_root.mkdir(parents=True, exist_ok=True)
    staged_bin = staged_emulebb_rust_root(layout) / "bin"
    exe_source = staged_bin / "emulebb-rust.exe"
    webui_source = staged_bin / "webui"
    if not exe_source.is_file():
        raise RuntimeError(f"Cannot package missing staged emulebb-rust executable: {exe_source}")
    if not (webui_source / "index.html").is_file():
        raise RuntimeError(f"Cannot package missing staged emulebb-rust WebUI: {webui_source / 'index.html'}")
    _assert_pe_machine(exe_source, workspace_options.platform)
    shutil.copy2(exe_source, package_root / "emulebb-rust.exe")
    shutil.copytree(webui_source, package_root / "webui")
    _copy_package_file(
        rust_root / "emulebb-rust-settings.example.toml",
        package_root,
        Path("emulebb-rust-settings.example.toml"),
    )
    _copy_package_file(rust_root / "LICENSE", package_root, Path("LICENSE"))
    _copy_package_file(
        layout.tooling_repo_root / "docs" / "products" / "emulebb-rust" / "RELEASE-SCOPE.md",
        package_root,
        Path("RELEASE-SCOPE.md"),
    )
    _write_emulebb_rust_readme(package_root, package_options.release_version)
    _write_emulebb_rust_sbom(
        layout=layout,
        rust_root=rust_root,
        package_root=package_root,
        release_root=release_root,
        asset_name=zip_path.name,
        version=package_options.release_version,
    )

    if zip_path.exists():
        zip_path.unlink()
    release_root.mkdir(parents=True, exist_ok=True)
    _write_zip(staging_root, package_root, zip_path)
    _assert_emulebb_rust_package_contents(zip_path)

    zip_hash = _sha256(zip_path)
    exe_hash = _sha256(package_root / "emulebb-rust.exe")
    package_file_hashes = _zip_entry_hashes(zip_path)
    shutil.copy2(package_root / "SBOM.spdx.json", sbom_path)
    sbom_hash = _sha256(sbom_path)
    manifest = _build_emulebb_rust_manifest(
        layout=layout,
        rust_root=rust_root,
        workspace_options=workspace_options,
        package_options=package_options,
        zip_path=zip_path,
        release_root=release_root,
        zip_hash=zip_hash,
        sbom_path=sbom_path,
        sbom_hash=sbom_hash,
        exe_hash=exe_hash,
        package_file_hashes=package_file_hashes,
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    _write_sha256sums(sums_path, (zip_path, manifest_path, sbom_path))
    print(f"eMuleBB Rust package: {zip_path}")
    print(f"eMuleBB Rust manifest: {manifest_path}")
    print(f"eMuleBB Rust SBOM: {sbom_path}")
    print(f"SHA256: {zip_hash}")


def _create_emulebb_rust_linux_packages(
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    package_options: EmulebbRustPackageOptions,
    rust_root: Path,
) -> None:
    """Creates unsigned amd64 DEB and x86_64 AppImage beta assets."""

    release_root = _emulebb_rust_release_root(layout, package_options.release_version)
    staging_root = release_root / "staging" / "linux-x86_64"
    deb_root = staging_root / "deb"
    appdir = staging_root / "AppDir"
    deb_path = release_root / f"emulebb-rust-v{package_options.release_version}-linux-amd64.deb"
    appimage_path = release_root / f"emulebb-rust-v{package_options.release_version}-linux-x86_64.AppImage"
    sbom_path = release_root / f"emulebb-rust-v{package_options.release_version}-linux-x86_64.sbom.spdx.json"
    sums_path = release_root / "SHA256SUMS"
    for path in (release_root, staging_root, deb_root, appdir, deb_path, appimage_path, sbom_path, sums_path):
        _assert_path_under_root(path, release_root, "emulebb-rust Linux package path")

    if staging_root.exists():
        shutil.rmtree(staging_root)
    release_root.mkdir(parents=True, exist_ok=True)
    staged_bin = staged_emulebb_rust_root(layout) / "bin"
    executable = staged_bin / "emulebb-rust"
    webui = staged_bin / "webui"
    if not executable.is_file():
        raise RuntimeError(f"Cannot package missing staged Linux emulebb-rust executable: {executable}")
    if not (webui / "index.html").is_file():
        raise RuntimeError(f"Cannot package missing staged emulebb-rust WebUI: {webui / 'index.html'}")

    _stage_emulebb_rust_linux_tree(
        destination=deb_root / "usr",
        executable=executable,
        webui=webui,
        rust_root=rust_root,
        tooling_root=layout.tooling_repo_root,
        version=package_options.release_version,
    )
    control_root = deb_root / "DEBIAN"
    control_root.mkdir(parents=True)
    (control_root / "control").write_text(
        "\n".join(
            (
                "Package: emulebb-rust",
                f"Version: {package_options.release_version}",
                "Section: net",
                "Priority: optional",
                "Architecture: amd64",
                "Maintainer: eMuleBB project <noreply@github.com>",
                "Description: Headless eMuleBB Rust daemon with embedded WebUI",
                "",
            )
        ),
        encoding="utf-8",
        newline="\n",
    )

    shutil.copytree(deb_root / "usr", appdir / "usr")
    (appdir / "AppRun").write_text(
        "#!/bin/sh\nHERE=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
        "exec \"$HERE/usr/lib/emulebb-rust/emulebb-rust\" \"$@\"\n",
        encoding="utf-8",
        newline="\n",
    )
    (appdir / "emulebb-rust.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=eMuleBB Rust\nExec=emulebb-rust\n"
        "Icon=emulebb-rust\nCategories=Network;FileTransfer;\nTerminal=true\n",
        encoding="utf-8",
        newline="\n",
    )
    (appdir / "emulebb-rust.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">'
        '<rect width="128" height="128" rx="22" fill="#2457c5"/>'
        '<path d="M28 36h72v16H48v12h44v15H48v13h54v16H28z" fill="white"/></svg>\n',
        encoding="utf-8",
        newline="\n",
    )
    for executable_path in (
        deb_root / "usr" / "bin" / "emulebb-rust",
        deb_root / "usr" / "lib" / "emulebb-rust" / "emulebb-rust",
        appdir / "AppRun",
        appdir / "usr" / "bin" / "emulebb-rust",
        appdir / "usr" / "lib" / "emulebb-rust" / "emulebb-rust",
    ):
        executable_path.chmod(0o755)

    _write_emulebb_rust_linux_sbom(
        layout=layout,
        rust_root=rust_root,
        package_root=appdir,
        release_root=release_root,
        asset_name=appimage_path.name,
        version=package_options.release_version,
    )
    shutil.copy2(appdir / "SBOM.spdx.json", sbom_path)
    shutil.copy2(appdir / "SBOM.spdx.json", deb_root / "usr" / "share" / "doc" / "emulebb-rust" / "SBOM.spdx.json")

    appimagetool = shutil.which("appimagetool") or os.environ.get("APPIMAGETOOL", "").strip()
    if not appimagetool:
        raise RuntimeError("Linux AppImage packaging requires appimagetool on PATH or APPIMAGETOOL.")
    # Package from the host's native temporary filesystem. WSL DrvFS mounts can
    # report every path as 0777 and ignore chmod, which would otherwise leak
    # writable/executable modes into both Linux artifacts.
    with tempfile.TemporaryDirectory(prefix="emulebb-rust-linux-package-") as native_temp:
        native_root = Path(native_temp)
        native_deb_root = native_root / "deb"
        native_appdir = native_root / "AppDir"
        shutil.copytree(deb_root, native_deb_root)
        shutil.copytree(appdir, native_appdir)
        _normalize_emulebb_rust_linux_modes(native_deb_root, is_appdir=False)
        _normalize_emulebb_rust_linux_modes(native_appdir, is_appdir=True)
        _run_packaging_tool(
            ("dpkg-deb", "--build", "--root-owner-group", str(native_deb_root), str(deb_path)),
            "dpkg-deb",
        )
        _run_packaging_tool(
            (appimagetool, "--no-appstream", str(native_appdir), str(appimage_path)),
            "appimagetool",
        )

    assets = (deb_path, appimage_path)
    manifests: list[Path] = []
    for asset in assets:
        manifest_path = asset.with_name(f"{asset.name}.manifest.json")
        manifest = {
            "schema": "emulebb.rust.package/1",
            "package": "emulebb-rust",
            "version": package_options.release_version,
            "tag": f"rust-v{package_options.release_version}",
            "platform": "linux-x86_64",
            "configuration": workspace_options.configuration,
            "signed": False,
            "builtUtc": datetime.now(timezone.utc).isoformat(),
            "asset": asset.name,
            "sha256": _sha256(asset),
            "executable": "/usr/lib/emulebb-rust/emulebb-rust",
            "webuiRoot": "/usr/lib/emulebb-rust/webui",
            "sbom": sbom_path.name,
            "sbomSha256": _sha256(sbom_path),
            "source": {
                "emulebbRust": _package_repo_provenance(rust_root),
                "emulebbBuild": _package_repo_provenance(layout.build_repo_root),
                "emulebbTooling": _package_repo_provenance(layout.tooling_repo_root),
            },
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
        manifests.append(manifest_path)
    _write_sha256sums(sums_path, (*assets, *manifests, sbom_path))
    print(f"eMuleBB Rust DEB: {deb_path}")
    print(f"eMuleBB Rust AppImage: {appimage_path}")
    print(f"eMuleBB Rust SBOM: {sbom_path}")


def _stage_emulebb_rust_linux_tree(
    *,
    destination: Path,
    executable: Path,
    webui: Path,
    rust_root: Path,
    tooling_root: Path,
    version: str,
) -> None:
    """Stages the common Linux filesystem tree used by DEB and AppImage."""

    lib_root = destination / "lib" / "emulebb-rust"
    doc_root = destination / "share" / "doc" / "emulebb-rust"
    bin_root = destination / "bin"
    lib_root.mkdir(parents=True)
    doc_root.mkdir(parents=True)
    bin_root.mkdir(parents=True)
    shutil.copy2(executable, lib_root / "emulebb-rust")
    shutil.copytree(webui, lib_root / "webui")
    shutil.copy2(rust_root / "emulebb-rust-settings.example.toml", doc_root)
    shutil.copy2(rust_root / "LICENSE", doc_root)
    shutil.copy2(tooling_root / "docs" / "products" / "emulebb-rust" / "RELEASE-SCOPE.md", doc_root)
    (doc_root / "README.md").write_text(
        f"# eMuleBB Rust {version}\n\nUnsigned Linux x86_64 beta package. The native Slint UI is not included.\n"
        "Run `emulebb-rust --profile <profile-dir>`; the daemon serves the packaged WebUI beside its binary.\n",
        encoding="utf-8",
        newline="\n",
    )
    (bin_root / "emulebb-rust").write_text(
        '#!/bin/sh\nexec /usr/lib/emulebb-rust/emulebb-rust "$@"\n',
        encoding="utf-8",
        newline="\n",
    )


def _normalize_emulebb_rust_linux_modes(root: Path, *, is_appdir: bool) -> None:
    """Applies release-safe modes on a native Linux package staging tree."""

    for path in root.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    executable_paths = [
        root / "usr" / "bin" / "emulebb-rust",
        root / "usr" / "lib" / "emulebb-rust" / "emulebb-rust",
    ]
    if is_appdir:
        executable_paths.append(root / "AppRun")
    for path in executable_paths:
        path.chmod(0o755)
    if not is_appdir:
        (root / "DEBIAN").chmod(0o755)
        (root / "DEBIAN" / "control").chmod(0o644)


def _write_emulebb_rust_linux_sbom(
    *,
    layout: WorkspaceLayout,
    rust_root: Path,
    package_root: Path,
    release_root: Path,
    asset_name: str,
    version: str,
) -> None:
    sbom_path = package_root / "SBOM.spdx.json"
    document = _build_spdx_sbom(
        name=f"eMuleBB Rust {version} Linux x86_64 package",
        namespace=f"https://github.com/emulebb/emulebb-rust/releases/download/rust-v{version}/{asset_name}.sbom",
        package_name=f"emulebb-rust-{version}-linux-x86_64",
        package_version=version,
        package_license="GPL-2.0-only",
        package_comment="Unsigned Linux x86_64 beta package for the headless Rust daemon and embedded WebUI.",
        package_root=package_root,
        release_root=release_root,
        components=[
            _repo_spdx_package("emulebb-rust source", rust_root, declared_license="GPL-2.0-only"),
            _repo_spdx_package("eMule build orchestration", layout.build_repo_root),
            _repo_spdx_package("eMule tooling docs", layout.tooling_repo_root),
        ],
    )
    sbom_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")


def _run_packaging_tool(command: tuple[str, ...], label: str) -> None:
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"{label} failed with exit code {completed.returncode}: {detail}")


def _qbittorrentbb_license(qbt_root: Path) -> Path | None:
    """Returns the qBittorrentBB license file, if present in the fork checkout."""

    for candidate in ("COPYING", "LICENSE", "LICENSE.txt"):
        path = qbt_root / candidate
        if path.is_file():
            return path
    return None


def _package_repo_provenance(repo_path: Path) -> dict[str, str]:
    """Returns compact git provenance (commit, branch, remote) for a fork checkout."""

    try:
        branch = repo_branch(repo_path)
    except Exception:
        branch = ""
    return {
        "commit": _git_value(repo_path, "rev-parse", "HEAD"),
        "branch": branch,
        "remote": _git_value(repo_path, "config", "--get", "remote.origin.url"),
    }


def _emulebb_rust_release_root(layout: WorkspaceLayout, release_version: str) -> Path:
    """Returns the Rust release artifact root for one version."""

    return layout.output_release_root / f"rust-v{release_version}"


def _assert_emulebb_rust_version_matches(rust_root: Path, release_version: str) -> None:
    """Requires the Rust workspace version to match the requested package version."""

    cargo_toml = rust_root / "Cargo.toml"
    if not cargo_toml.is_file():
        raise RuntimeError(f"Cannot package missing Rust workspace manifest: {cargo_toml}")
    with cargo_toml.open("rb") as stream:
        manifest = tomllib.load(stream)
    version = str(manifest.get("workspace", {}).get("package", {}).get("version", ""))
    if version != release_version:
        raise RuntimeError(
            f"package emulebb-rust version mismatch: --release-version is '{release_version}' "
            f"but Cargo.toml workspace.package.version is '{version}'."
        )


def _assert_clean_emulebb_rust_package_inputs(layout: WorkspaceLayout, rust_root: Path) -> None:
    """Requires clean source/provenance inputs before writing Rust release assets."""

    dirty: list[str] = []
    for label, repo in (
        ("emulebb-rust", rust_root),
        ("emulebb-build", layout.build_repo_root),
        ("emulebb-tooling", layout.tooling_repo_root),
    ):
        lines = [line for line in repo_status_lines(repo) if not line.startswith("## ")]
        if lines:
            dirty.append(f"{label} ({repo}):\n" + "\n".join(f"  {line}" for line in lines))
    if dirty:
        raise RuntimeError(
            "package emulebb-rust requires clean provenance inputs before writing assets:\n"
            + "\n\n".join(dirty)
        )


def _write_emulebb_rust_readme(package_root: Path, version: str) -> None:
    """Writes the package-facing README for emulebb-rust."""

    readme_path = package_root / "README.md"
    _assert_path_under_root(readme_path, package_root, "emulebb-rust package README")
    readme = (
        f"eMuleBB Rust {version}\n"
        "====================\n\n"
        "This is the unsigned Windows x64 beta package for the headless emulebb-rust daemon.\n\n"
        "Run `emulebb-rust.exe --profile <profile-dir>` from this directory or from a script that points at a profile.\n"
        "The daemon serves the embedded browser WebUI from the packaged `webui` directory beside the executable.\n\n"
        "Package contents:\n\n"
        "- `emulebb-rust.exe` - regular headless daemon executable.\n"
        "- `webui/` - packaged embedded SPA WebUI static assets.\n"
        "- `emulebb-rust-settings.example.toml` - local settings example.\n"
        "- `RELEASE-SCOPE.md` - beta scope, omissions, and platform support.\n"
        "- `SBOM.spdx.json` - package file inventory and provenance.\n\n"
        "The native Slint UI is not shipped in this package.\n"
    )
    readme_path.write_text(readme, encoding="utf-8", newline="\n")


def _write_emulebb_rust_sbom(
    *,
    layout: WorkspaceLayout,
    rust_root: Path,
    package_root: Path,
    release_root: Path,
    asset_name: str,
    version: str,
) -> None:
    """Writes a package-local SPDX SBOM for the Rust release asset."""

    sbom_path = package_root / "SBOM.spdx.json"
    _assert_path_under_root(sbom_path, package_root, "emulebb-rust package SBOM")
    components = [
        _repo_spdx_package("emulebb-rust source", rust_root, declared_license="GPL-2.0-only"),
        _repo_spdx_package("eMule build orchestration", layout.build_repo_root),
        _repo_spdx_package("eMule tooling docs", layout.tooling_repo_root),
    ]
    document = _build_spdx_sbom(
        name=f"eMuleBB Rust {version} Windows x64 package",
        namespace=f"https://github.com/emulebb/emulebb-rust/releases/download/rust-v{version}/{asset_name}.sbom",
        package_name=f"emulebb-rust-{version}-windows-x64",
        package_version=version,
        package_license="GPL-2.0-only",
        package_comment="Unsigned Windows x64 beta package for the headless Rust daemon and embedded WebUI.",
        package_root=package_root,
        release_root=release_root,
        components=components,
    )
    sbom_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")


def _build_emulebb_rust_manifest(
    *,
    layout: WorkspaceLayout,
    rust_root: Path,
    workspace_options: WorkspaceOptions,
    package_options: EmulebbRustPackageOptions,
    zip_path: Path,
    release_root: Path,
    zip_hash: str,
    sbom_path: Path,
    sbom_hash: str,
    exe_hash: str,
    package_file_hashes: dict[str, str],
) -> dict[str, object]:
    """Builds the machine-readable manifest for the Rust package ZIP."""

    return {
        "schema": "emulebb.rust.package/1",
        "package": "emulebb-rust",
        "version": package_options.release_version,
        "tag": f"rust-v{package_options.release_version}",
        "platform": workspace_options.platform,
        "configuration": workspace_options.configuration,
        "signed": False,
        "builtUtc": datetime.now(timezone.utc).isoformat(),
        "asset": zip_path.name,
        "sha256": zip_hash,
        "executable": f"{EMULEBB_RUST_PACKAGE_ROOT_NAME}/emulebb-rust.exe",
        "executableSha256": exe_hash,
        "webuiRoot": f"{EMULEBB_RUST_PACKAGE_ROOT_NAME}/webui",
        "releaseScope": f"{EMULEBB_RUST_PACKAGE_ROOT_NAME}/RELEASE-SCOPE.md",
        "settingsExample": f"{EMULEBB_RUST_PACKAGE_ROOT_NAME}/emulebb-rust-settings.example.toml",
        "sbom": sbom_path.relative_to(release_root).as_posix(),
        "sbomSha256": sbom_hash,
        "perFileSha256": package_file_hashes,
        "source": {
            "emulebbRust": _package_repo_provenance(rust_root),
            "emulebbBuild": _package_repo_provenance(layout.build_repo_root),
            "emulebbTooling": _package_repo_provenance(layout.tooling_repo_root),
        },
    }


def _write_sha256sums(sums_path: Path, paths: tuple[Path, ...]) -> None:
    """Writes GNU-style SHA256SUMS for release assets."""

    release_root = sums_path.parent
    lines = [f"{_sha256(path)}  {path.relative_to(release_root).as_posix()}" for path in paths]
    sums_path.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")


def _write_qbittorrentbb_readme(package_root: Path, version: str, asset_arch: str) -> None:
    """Writes a short README into the staged qBittorrentBB package."""

    readme = (
        f"qBittorrentBB {version} ({asset_arch})\n"
        f"{'=' * 40}\n\n"
        "Statically linked single-executable build (vcpkg x64-windows-static,\n"
        "Qt6 and engine dependencies linked in). No accompanying DLLs are required.\n\n"
        "This artifact is unsigned. Verify integrity with the SHA-256 recorded in\n"
        "the accompanying .manifest.json before running.\n\n"
        "qBittorrentBB is a fork of qBittorrent. See COPYING.txt for license terms.\n"
    )
    (package_root / "README.txt").write_text(readme, encoding="utf-8", newline="\n")


def _write_qbittorrentbb_sbom(
    *,
    workspace_options: WorkspaceOptions,
    qbt_root: Path,
    lt_root: Path,
    package_root: Path,
    release_root: Path,
    asset_name: str,
    version: str,
) -> None:
    """Writes a package-local SPDX SBOM for the static qBittorrentBB asset."""

    sbom_path = package_root / "SBOM.spdx.json"
    _assert_path_under_root(sbom_path, package_root, "qBittorrentBB package SBOM")
    components = [
        _repo_spdx_package("qBittorrentBB source", qbt_root, declared_license="GPL-2.0-or-later"),
        _repo_spdx_package("emulebb-libtorrent engine", lt_root, declared_license="BSD-3-Clause"),
    ]
    document = _build_spdx_sbom(
        name=f"qBittorrentBB {version} {workspace_options.platform} static package",
        namespace=f"https://github.com/emulebb/qbittorrentbb/releases/download/v{version}/{asset_name}.sbom",
        package_name=f"qbittorrentbb-{version}-{workspace_options.platform}",
        package_version=version,
        package_license="GPL-2.0-or-later",
        package_comment="Statically linked single-exe qBittorrentBB package (unsigned).",
        package_root=package_root,
        release_root=release_root,
        components=components,
    )
    sbom_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")


def create_release_package(
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    package_options: ReleasePackageOptions,
) -> None:
    """Builds the main app and creates a release ZIP plus manifest."""

    if workspace_options.configuration != "Release":
        raise RuntimeError("package release requires --config Release.")
    if not _is_release_version(package_options.release_version):
        raise RuntimeError(
            f"Release version must use {RELEASE_VERSION_FORMAT} format: {package_options.release_version}"
        )

    ensure_canonical_app_anchor(layout)
    app_variant = layout.get_app_variant("main")
    app_root = app_variant.path
    _assert_release_source_branch(app_variant)
    _assert_clean_release_inputs(layout, app_root)
    _assert_package_version_matches_app(app_root, package_options.release_version)

    asset_arch = _release_asset_arch(workspace_options.platform)
    release_root = _release_root(layout, package_options)
    package_build_arch_root = layout.output_packages_root / "build" / f"emulebb-v{package_options.release_version}" / asset_arch
    _assert_path_under_root(package_build_arch_root, layout.output_packages_root, "release package build path")
    if package_options.clean and package_build_arch_root.exists():
        shutil.rmtree(package_build_arch_root)

    session = BuildSession(
        layout=layout,
        options=workspace_options,
        command_name="package release",
        clean=package_options.clean,
        stamp=utc_run_id(),
    )
    try:
        for flavor in RELEASE_PACKAGE_FLAVORS:
            package_build_root = _package_build_root(layout, package_options, workspace_options.platform, flavor.name)
            _build_package_app(
                session,
                app_root,
                flavor=flavor,
                package_app_output_root=package_build_root / "app",
                package_app_intermediate_root=package_build_root / "app-obj",
                clean=package_options.clean,
            )
        language_build_root = _package_language_build_root(layout, package_options, workspace_options.platform)
        _build_language_resources(session, app_root, package_options.clean, language_build_root)
    finally:
        session.write_recap()

    expected_language_dlls = _expected_language_dlls(layout.tooling_repo_root)
    lang_path = _package_language_path(language_build_root / "bin", workspace_options.platform, expected_language_dlls)
    if not lang_path.exists():
        raise RuntimeError(f"Cannot package missing release runtime path: {lang_path}")

    for flavor in RELEASE_PACKAGE_FLAVORS:
        package_build_root = _package_build_root(layout, package_options, workspace_options.platform, flavor.name)
        exe_path = package_build_root / "app" / flavor.executable_name
        if not exe_path.exists():
            raise RuntimeError(f"Cannot package missing release runtime path: {exe_path}")
        _assert_pe_machine(exe_path, workspace_options.platform)
        _assert_release_binary_diagnostics(exe_path, flavor)

        asset_stem = _release_asset_stem(package_options.release_version, asset_arch, flavor)
        staging_root = release_root / "staging" / asset_arch / flavor.name
        package_root = staging_root / EMULEBB_PACKAGE_ROOT_NAME
        zip_path = release_root / f"{asset_stem}.zip"
        manifest_path = release_root / f"{asset_stem}.manifest.json"
        sbom_path = release_root / f"{asset_stem}.sbom.spdx.json"
        for path_to_check in (staging_root, package_root, zip_path, manifest_path, sbom_path):
            _assert_path_under_root(path_to_check, release_root, "release package path")

        if staging_root.exists():
            shutil.rmtree(staging_root)
        package_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(exe_path, package_root / flavor.executable_name)
        _copy_language_dlls(lang_path, package_root / "lang", expected_language_dlls)
        _write_package_readme(package_root, package_options.release_version, workspace_options.platform, flavor=flavor)
        _write_package_release_notes(package_root, package_options.release_version)
        _write_package_license_notice(package_root)
        _write_package_third_party_notices(package_root)
        _write_package_gpl_text(layout, package_root)
        _copy_package_file(
            layout.tooling_repo_root / "docs" / "rest" / "REST-API-CONTRACT.md",
            package_root,
            Path("docs/REST-API-CONTRACT.md"),
        )
        _copy_package_file(
            layout.tooling_repo_root / "docs" / "rest" / "REST-API-OPENAPI.yaml",
            package_root,
            Path("docs/REST-API-OPENAPI.yaml"),
        )
        _copy_package_file(
            layout.tooling_repo_root / "docs" / "rest" / "REST-API-PARITY-INVENTORY.md",
            package_root,
            Path("docs/REST-API-PARITY-INVENTORY.md"),
        )
        _copy_emule_runtime_assets(layout.build_repo_root, package_root)
        signature_policy = _sign_release_package_files(package_root, require_signing=package_options.require_signing)
        _write_release_sbom(
            layout=layout,
            workspace_options=workspace_options,
            package_options=package_options,
            app_variant=app_variant,
            app_root=app_root,
            package_root=package_root,
            release_root=release_root,
            asset_name=zip_path.name,
            flavor=flavor,
        )
        bootstrapper_asset_path, bootstrapper_hash_path, bootstrapper_hash = _write_standalone_bootstrapper_asset(
            package_root=package_root,
            release_root=release_root,
            release_version=package_options.release_version,
        )
        suite_scripts_asset_path, suite_scripts_manifest_path, suite_scripts_hash = _write_suite_scripts_bundle_asset(
            package_root=package_root,
            release_root=release_root,
            release_version=package_options.release_version,
        )
        automation_examples_asset_path, automation_examples_manifest_path, automation_examples_hash = (
            _write_automation_examples_asset(
                build_repo_root=layout.build_repo_root,
                release_root=release_root,
                release_version=package_options.release_version,
            )
        )

        if zip_path.exists():
            zip_path.unlink()
        release_root.mkdir(parents=True, exist_ok=True)
        _write_zip(staging_root, package_root, zip_path)
        _assert_release_package_contents(zip_path, expected_language_dlls, workspace_options.platform, flavor=flavor)

        zip_hash = _sha256(zip_path)
        exe_hash = _sha256(exe_path)
        package_file_hashes = _zip_entry_hashes(zip_path)
        shutil.copy2(package_root / "SBOM.spdx.json", sbom_path)
        sbom_hash = _sha256(sbom_path)
        manifest = _build_release_manifest(
            layout=layout,
            workspace_options=workspace_options,
            package_options=package_options,
            app_variant=app_variant,
            app_root=app_root,
            zip_path=zip_path,
            release_root=release_root,
            zip_hash=zip_hash,
            sbom_path=sbom_path,
            sbom_hash=sbom_hash,
            exe_hash=exe_hash,
            expected_language_dlls=expected_language_dlls,
            package_file_hashes=package_file_hashes,
            bootstrapper_asset_path=bootstrapper_asset_path,
            bootstrapper_hash_path=bootstrapper_hash_path,
            bootstrapper_hash=bootstrapper_hash,
            suite_scripts_asset_path=suite_scripts_asset_path,
            suite_scripts_manifest_path=suite_scripts_manifest_path,
            suite_scripts_hash=suite_scripts_hash,
            suite_scripts_manifest_hash=_sha256(suite_scripts_manifest_path),
            automation_examples_asset_path=automation_examples_asset_path,
            automation_examples_manifest_path=automation_examples_manifest_path,
            automation_examples_hash=automation_examples_hash,
            automation_examples_manifest_hash=_sha256(automation_examples_manifest_path),
            signature_policy=signature_policy,
            flavor=flavor,
        )
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"Release package ({flavor.name}): {zip_path}")
        print(f"Release manifest ({flavor.name}): {manifest_path}")
        print(f"Release SBOM ({flavor.name}): {sbom_path}")
        print(f"SHA256 ({flavor.name}): {zip_hash}")


def _build_release_manifest(
    *,
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    package_options: ReleasePackageOptions,
    app_variant: AppVariant,
    app_root: Path,
    zip_path: Path,
    release_root: Path,
    zip_hash: str,
    sbom_path: Path,
    sbom_hash: str,
    exe_hash: str,
    expected_language_dlls: tuple[str, ...],
    package_file_hashes: dict[str, str],
    bootstrapper_asset_path: Path,
    bootstrapper_hash_path: Path,
    bootstrapper_hash: str,
    suite_scripts_asset_path: Path,
    suite_scripts_manifest_path: Path,
    suite_scripts_hash: str,
    suite_scripts_manifest_hash: str,
    automation_examples_asset_path: Path,
    automation_examples_manifest_path: Path,
    automation_examples_hash: str,
    automation_examples_manifest_hash: str,
    signature_policy: dict[str, object],
    flavor: ReleasePackageFlavorSpec = RELEASE_PACKAGE_FLAVORS[0],
) -> dict[str, object]:
    """Builds the provenance manifest written next to one release asset."""

    return {
        "product": "eMule broadband edition",
        "compactName": "eMuleBB",
        "version": package_options.release_version,
        "tag": f"emulebb-v{package_options.release_version}",
        "configuration": workspace_options.configuration,
        "platform": workspace_options.platform,
        "packageFlavor": flavor.name,
        "diagnosticFeatures": list(flavor.diagnostic_features),
        "executableName": flavor.executable_name,
        "executablePath": f"{EMULEBB_PACKAGE_ROOT_NAME}/{flavor.executable_name}",
        "asset": zip_path.name,
        "assetPath": zip_path.relative_to(release_root).as_posix(),
        "sha256": zip_hash,
        "sbomFormat": "SPDX-2.3 JSON",
        "sbomPath": sbom_path.relative_to(release_root).as_posix(),
        "sbomSha256": sbom_hash,
        "bootstrapperAsset": bootstrapper_asset_path.relative_to(release_root).as_posix(),
        "bootstrapperSha256": bootstrapper_hash,
        "bootstrapperSha256Path": bootstrapper_hash_path.relative_to(release_root).as_posix(),
        "suiteScriptsAsset": suite_scripts_asset_path.relative_to(release_root).as_posix(),
        "suiteScriptsSha256": suite_scripts_hash,
        "suiteScriptsManifest": suite_scripts_manifest_path.relative_to(release_root).as_posix(),
        "suiteScriptsManifestSha256": suite_scripts_manifest_hash,
        "automationExamplesAsset": automation_examples_asset_path.relative_to(release_root).as_posix(),
        "automationExamplesSha256": automation_examples_hash,
        "automationExamplesManifest": automation_examples_manifest_path.relative_to(release_root).as_posix(),
        "automationExamplesManifestSha256": automation_examples_manifest_hash,
        "signaturePolicy": signature_policy,
        "emulebbExeSha256": exe_hash,
        "languageDllCount": len(expected_language_dlls),
        "languageDlls": list(expected_language_dlls),
        "packageFileSha256": package_file_hashes,
        "appVariant": app_variant.name,
        "appBranch": repo_branch(app_root),
        "appCommit": repo_head(app_root),
        "buildBranch": repo_branch(layout.build_repo_root),
        "buildCommit": repo_head(layout.build_repo_root),
        "buildTestsBranch": repo_branch(layout.tests_repo_root),
        "buildTestsCommit": repo_head(layout.tests_repo_root),
        "toolingBranch": repo_branch(layout.tooling_repo_root),
        "toolingCommit": repo_head(layout.tooling_repo_root),
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "includedPaths": [
            f"{EMULEBB_PACKAGE_ROOT_NAME}/{flavor.executable_name}",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/lang",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/README.md",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/RELEASE-NOTES.md",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/LICENSE-NOTICE.txt",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/GPL-2.0-or-later.txt",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/THIRD-PARTY-NOTICES.txt",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/SBOM.spdx.json",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/scripts",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/skins",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/docs/REST-API-CONTRACT.md",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/docs/REST-API-OPENAPI.yaml",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/docs/REST-API-PARITY-INVENTORY.md",
        ],
    }


def _build_amutorrent_manifest(
    *,
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    package_options: AmutorrentPackageOptions,
    amutorrent_root: Path,
    zip_path: Path,
    release_root: Path,
    zip_hash: str,
    sbom_path: Path,
    sbom_hash: str,
    package_file_hashes: dict[str, str],
) -> dict[str, object]:
    """Builds the provenance manifest for one optional aMuTorrent asset."""

    node_archive_name, node_archive_sha256 = AMUTORRENT_NODE_ARCHIVES[workspace_options.platform]
    upstream_base = _amutorrent_upstream_base(amutorrent_root)
    return {
        "product": "eMule broadband edition",
        "compactName": "eMuleBB",
        "package": "aMuTorrent optional controller",
        "version": package_options.release_version,
        "tag": f"emulebb-v{package_options.release_version}",
        "configuration": workspace_options.configuration,
        "platform": workspace_options.platform,
        "asset": zip_path.name,
        "assetPath": zip_path.relative_to(release_root).as_posix(),
        "sha256": zip_hash,
        "sbomFormat": "SPDX-2.3 JSON",
        "sbomPath": sbom_path.relative_to(release_root).as_posix(),
        "sbomSha256": sbom_hash,
        "packageFileSha256": package_file_hashes,
        "runtimePolicy": {
            "minimumPathNodeMajor": 24,
            "pinnedFallbackNodeVersion": AMUTORRENT_NODE_VERSION,
            "pinnedFallbackNodeArchive": node_archive_name,
            "pinnedFallbackNodeArchiveSha256": node_archive_sha256,
            "runnerOwner": "eMuleBB suite installer",
            "pathPm2Allowed": False,
            "packageLocalPm2Allowed": False,
            "packageLocalDataRoot": "aMuTorrent/data",
            "packageLocalLogRoot": "aMuTorrent/logs",
            "packageLocalRuntimeRoot": "aMuTorrent/runtime",
            "localAppDataUsed": False,
            "spacesInInstallPathAllowed": False,
        },
        "upstreamBase": upstream_base,
        "amutorrentBranch": repo_branch(amutorrent_root),
        "amutorrentCommit": repo_head(amutorrent_root),
        "buildBranch": repo_branch(layout.build_repo_root),
        "buildCommit": repo_head(layout.build_repo_root),
        "buildTestsBranch": repo_branch(layout.tests_repo_root),
        "buildTestsCommit": repo_head(layout.tests_repo_root),
        "toolingBranch": repo_branch(layout.tooling_repo_root),
        "toolingCommit": repo_head(layout.tooling_repo_root),
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "includedPaths": [
            "aMuTorrent/server",
            "aMuTorrent/server/node_modules",
            "aMuTorrent/static",
            "aMuTorrent/README.md",
            "aMuTorrent/LICENSE-aMuTorrent.txt",
            "aMuTorrent/SBOM.spdx.json",
        ],
    }


def _amutorrent_upstream_base(amutorrent_root: Path) -> dict[str, str]:
    """Reads the fork manifest's recorded upstream base for package provenance."""

    manifest_path = amutorrent_root / "fork-delta.json"
    if not manifest_path.is_file():
        return {}
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    upstream = payload.get("upstream")
    if not isinstance(upstream, dict):
        return {}
    result: dict[str, str] = {}
    for key in ("url", "branch", "baseCommit", "baseVersion"):
        value = upstream.get(key)
        if isinstance(value, str) and value:
            result[key] = value
    return result


def _write_release_sbom(
    *,
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    package_options: ReleasePackageOptions,
    app_variant: AppVariant,
    app_root: Path,
    package_root: Path,
    release_root: Path,
    asset_name: str,
    flavor: ReleasePackageFlavorSpec = RELEASE_PACKAGE_FLAVORS[0],
) -> None:
    """Writes a package-local SPDX SBOM for the main release asset."""

    sbom_path = package_root / "SBOM.spdx.json"
    _assert_path_under_root(sbom_path, package_root, "release package SBOM")
    components = [
        _repo_spdx_package("eMuleBB app source", app_root, declared_license="GPL-2.0-or-later"),
        _repo_spdx_package("eMule build orchestration", layout.build_repo_root),
        _repo_spdx_package("eMule build tests", layout.tests_repo_root),
        _repo_spdx_package("eMule tooling docs", layout.tooling_repo_root),
    ]
    components.extend(_third_party_spdx_packages())
    document = _build_spdx_sbom(
        name=f"eMuleBB {package_options.release_version} {workspace_options.platform} {flavor.name} release package",
        namespace=f"https://github.com/emulebb/emulebb/releases/download/emulebb-v{package_options.release_version}/{asset_name}.sbom",
        package_name=f"emulebb-{package_options.release_version}{flavor.asset_suffix}-{workspace_options.platform}",
        package_version=package_options.release_version,
        package_license="GPL-2.0-or-later",
        package_comment=(
            f"Main app {flavor.name} release package built from app variant {app_variant.name}. "
            f"Diagnostic features: {', '.join(flavor.diagnostic_features) or 'none'}."
        ),
        package_root=package_root,
        release_root=release_root,
        components=components,
    )
    sbom_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")


def _write_amutorrent_sbom(
    *,
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    package_options: AmutorrentPackageOptions,
    amutorrent_root: Path,
    package_root: Path,
    release_root: Path,
    asset_name: str,
) -> None:
    """Writes a package-local SPDX SBOM for the optional aMuTorrent asset."""

    sbom_path = package_root / "SBOM.spdx.json"
    _assert_path_under_root(sbom_path, package_root, "aMuTorrent package SBOM")
    components = [
        _repo_spdx_package("aMuTorrent source", amutorrent_root),
        _repo_spdx_package("eMule build orchestration", layout.build_repo_root),
        _repo_spdx_package("eMule build tests", layout.tests_repo_root),
        _repo_spdx_package("eMule tooling docs", layout.tooling_repo_root),
    ]
    document = _build_spdx_sbom(
        name=f"eMuleBB aMuTorrent {package_options.release_version} {workspace_options.platform} package",
        namespace=f"https://github.com/emulebb/emulebb/releases/download/emulebb-v{package_options.release_version}/{asset_name}.sbom",
        package_name=f"emulebb-{package_options.release_version}-amutorrent-{workspace_options.platform}",
        package_version=package_options.release_version,
        package_license="NOASSERTION",
        package_comment="Optional aMuTorrent controller package.",
        package_root=package_root,
        release_root=release_root,
        components=components,
    )
    sbom_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")


def _build_spdx_sbom(
    *,
    name: str,
    namespace: str,
    package_name: str,
    package_version: str,
    package_license: str,
    package_comment: str,
    package_root: Path,
    release_root: Path,
    components: list[dict[str, object]],
) -> dict[str, object]:
    """Builds a compact SPDX 2.3 JSON document for one staged package."""

    root_package = _component_spdx_package(
        name=package_name,
        declared_license=package_license,
        version=package_version,
        comment=package_comment,
    )
    root_package["SPDXID"] = "SPDXRef-Package"
    root_package["filesAnalyzed"] = True

    files = []
    relationships = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": root_package["SPDXID"],
        }
    ]
    file_sha1s = []
    for relative_name, digest in _staged_package_file_hashes(package_root).items():
        file_id = _spdx_ref("File", relative_name)
        sha1_digest = _sha1(package_root.parent / relative_name)
        file_sha1s.append(sha1_digest)
        files.append(
            {
                "SPDXID": file_id,
                "fileName": relative_name,
                "checksums": [
                    {"algorithm": "SHA1", "checksumValue": sha1_digest},
                    {"algorithm": "SHA256", "checksumValue": digest},
                ],
                "licenseConcluded": "NOASSERTION",
                "copyrightText": "NOASSERTION",
            }
        )
        relationships.append(
            {
                "spdxElementId": root_package["SPDXID"],
                "relationshipType": "CONTAINS",
                "relatedSpdxElement": file_id,
            }
        )

    root_package["packageVerificationCode"] = {
        "packageVerificationCodeValue": hashlib.sha1("".join(sorted(file_sha1s)).encode("ascii")).hexdigest()
    }
    packages = [root_package, *components]
    for component in components:
        relationships.append(
            {
                "spdxElementId": root_package["SPDXID"],
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": component["SPDXID"],
            }
        )

    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": name,
        "documentNamespace": namespace,
        "creationInfo": {
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "creators": ["Tool: emule_workspace.release"],
        },
        "packages": packages,
        "files": files,
        "relationships": relationships,
        "documentDescribes": [root_package["SPDXID"]],
        "comment": f"Generated from staged package files under {release_root.name}.",
    }


def _staged_package_file_hashes(package_root: Path) -> dict[str, str]:
    """Returns SHA-256 hashes for staged package files, excluding the SBOM itself."""

    hashes: dict[str, str] = {}
    staging_root = package_root.parent
    for path in sorted(package_root.rglob("*")):
        if not path.is_file() or path.name == "SBOM.spdx.json":
            continue
        hashes[path.relative_to(staging_root).as_posix()] = _sha256(path)
    return hashes


def _repo_spdx_package(name: str, repo_path: Path, *, declared_license: str = "NOASSERTION") -> dict[str, object]:
    package = _component_spdx_package(
        name=name,
        declared_license=declared_license,
        version=_git_value(repo_path, "rev-parse", "HEAD"),
        download_location=_git_value(repo_path, "config", "--get", "remote.origin.url") or "NOASSERTION",
    )
    try:
        branch = repo_branch(repo_path)
    except Exception:
        branch = ""
    if branch:
        package["comment"] = f"Git branch: {branch}"
    return package


def _third_party_spdx_packages() -> list[dict[str, object]]:
    return [
        _component_spdx_package(name=name, declared_license=declared_license)
        for name, declared_license in RELEASE_THIRD_PARTY_COMPONENTS
    ]


def _component_spdx_package(
    *,
    name: str,
    declared_license: str,
    version: str | None = None,
    download_location: str = "NOASSERTION",
    checksums: list[dict[str, str]] | None = None,
    comment: str | None = None,
) -> dict[str, object]:
    package: dict[str, object] = {
        "name": name,
        "SPDXID": _spdx_ref("Package", name),
        "downloadLocation": download_location,
        "filesAnalyzed": False,
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": declared_license,
        "copyrightText": "NOASSERTION",
    }
    if version:
        package["versionInfo"] = version
    if checksums:
        package["checksums"] = checksums
    if comment:
        package["comment"] = comment
    return package


def _git_value(repo_path: Path, *args: str) -> str:
    try:
        return git_output(repo_path, *args).strip()
    except Exception:
        return ""


def _spdx_ref(prefix: str, value: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9.-]+", "-", value).strip(".-")
    return f"SPDXRef-{prefix}-{suffix or 'unknown'}"


def _assert_release_source_branch(app_variant: AppVariant) -> None:
    """Requires release packages to come from the configured source branch."""

    current_branch = repo_branch(app_variant.path)
    if current_branch != app_variant.branch:
        raise RuntimeError(
            "package release requires app variant "
            f"'{app_variant.name}' at {app_variant.path} to be on branch "
            f"'{app_variant.branch}', not '{current_branch}'."
        )


def _assert_clean_release_inputs(layout: WorkspaceLayout, app_root: Path) -> None:
    """Rejects dirty inputs whose exact commits are recorded in the manifest."""

    repos = (
        ("app source", app_root),
        ("build orchestration", layout.build_repo_root),
        ("build tests", layout.tests_repo_root),
        ("tooling docs", layout.tooling_repo_root),
    )
    dirty_inputs: list[str] = []
    for label, repo_path in repos:
        changes = [line for line in repo_status_lines(repo_path) if not line.startswith("## ")]
        if changes:
            sample = "\n    ".join(changes[:20])
            dirty_inputs.append(f"- {label}: {repo_path}\n    {sample}")
    if dirty_inputs:
        raise RuntimeError(
            "package release requires clean provenance inputs before writing assets:\n"
            + "\n".join(dirty_inputs)
        )


def _assert_clean_amutorrent_package_inputs(layout: WorkspaceLayout, amutorrent_root: Path) -> None:
    """Rejects dirty inputs whose exact commits are recorded in the aMuTorrent manifest."""

    repos = (
        ("aMuTorrent source", amutorrent_root),
        ("build orchestration", layout.build_repo_root),
        ("build tests", layout.tests_repo_root),
        ("tooling docs", layout.tooling_repo_root),
    )
    dirty_inputs: list[str] = []
    for label, repo_path in repos:
        changes = [line for line in repo_status_lines(repo_path) if not line.startswith("## ")]
        if changes:
            sample = "\n    ".join(changes[:20])
            dirty_inputs.append(f"- {label}: {repo_path}\n    {sample}")
    if dirty_inputs:
        raise RuntimeError(
            "package amutorrent requires clean provenance inputs before writing assets:\n"
            + "\n".join(dirty_inputs)
        )


def ensure_canonical_app_anchor(layout: WorkspaceLayout) -> None:
    """Ensures the canonical app repo is clean and detached at origin/main."""

    canonical_repo_path = layout.seed_repo_path
    if not canonical_repo_path.is_dir():
        raise RuntimeError(f"Canonical app repo is missing: {canonical_repo_path}")
    status_lines = repo_status_lines(canonical_repo_path)
    if len(status_lines) > 1:
        raise RuntimeError(f"Canonical app repo has local changes and cannot be re-anchored automatically: {canonical_repo_path}")
    expected_revision = f"refs/remotes/origin/{layout.seed_repo_branch}"
    expected_head = git_output(canonical_repo_path, "rev-parse", expected_revision).strip()
    current_branch = repo_branch(canonical_repo_path)
    current_head = git_output(canonical_repo_path, "rev-parse", "HEAD").strip()
    if current_branch == "HEAD" and current_head == expected_head:
        return
    print(f"Reanchoring canonical app repo to detached origin/{layout.seed_repo_branch} at {expected_head}")
    git_output(canonical_repo_path, "checkout", "--detach", expected_revision)


def _release_asset_arch(platform: str) -> str:
    """Returns the release asset architecture token for one build platform."""

    return "arm64" if platform == "ARM64" else "x64"


def _release_root(layout: WorkspaceLayout, package_options: ReleasePackageOptions) -> Path:
    """Returns the release artifact root for one package version."""

    return _release_root_for_version(layout, package_options.release_version)


def _release_root_for_version(layout: WorkspaceLayout, release_version: str) -> Path:
    """Returns the release artifact root for one release version."""

    return layout.output_release_root / f"emulebb-v{release_version}"


def _package_build_root(
    layout: WorkspaceLayout,
    package_options: ReleasePackageOptions,
    platform: str,
    flavor: str,
) -> Path:
    """Returns the package-only app build output root for one release asset."""

    return (
        layout.output_packages_root
        / "build"
        / f"emulebb-v{package_options.release_version}"
        / _release_asset_arch(platform)
        / flavor
    )


def _package_language_build_root(
    layout: WorkspaceLayout,
    package_options: ReleasePackageOptions,
    platform: str,
) -> Path:
    """Returns the package-only language DLL build output root."""

    return (
        layout.output_packages_root
        / "build"
        / f"emulebb-v{package_options.release_version}"
        / _release_asset_arch(platform)
        / "language"
    )


def _release_asset_stem(release_version: str, asset_arch: str, flavor: ReleasePackageFlavorSpec) -> str:
    """Returns the file stem for one eMuleBB release package flavor."""

    return f"emulebb-{release_version}{flavor.asset_suffix}-{asset_arch}"


def _build_package_app(
    session: BuildSession,
    app_root: Path,
    *,
    flavor: ReleasePackageFlavorSpec = RELEASE_PACKAGE_FLAVORS[0],
    package_app_output_root: Path,
    package_app_intermediate_root: Path,
    clean: bool,
) -> None:
    target = "Rebuild" if clean else "Build"
    ensure_app_dependency_artifacts(session.layout, session.options, clean=clean)
    extra_properties = [*app_property_overrides(session.layout, session.options.platform)]
    extra_properties.append(f"/p:EnableStartupDiagnostics={'true' if flavor.enable_startup_diagnostics else 'false'}")
    extra_properties.append(f"/p:EnablePacketDiagnostics={'true' if flavor.enable_packet_diagnostics else 'false'}")
    extra_properties.append(
        f"/p:EnableUploadSlotDiagnostics={'true' if flavor.enable_upload_slot_diagnostics else 'false'}"
    )
    extra_properties.append(
        f"/p:EnableDownloadSlotDiagnostics={'true' if flavor.enable_download_slot_diagnostics else 'false'}"
    )
    extra_properties.append(
        f"/p:EnableBadPeerDiagnostics={'true' if flavor.enable_bad_peer_diagnostics else 'false'}"
    )
    extra_properties.append(f"/p:EnableKadDiagnostics={'true' if flavor.enable_kad_diagnostics else 'false'}")
    if flavor.executable_name != APP_EXE_NAME:
        extra_properties.append(f"/p:TargetName={Path(flavor.executable_name).stem}")
    extra_properties.append(f"/p:OutDir={with_trailing_separator(package_app_output_root)}")
    extra_properties.append(f"/p:IntDir={with_trailing_separator(package_app_intermediate_root)}")
    override = env_override(session.layout.toolset_override_variable)
    if override:
        extra_properties.append(f"/p:PlatformToolset={override}")
    invoke_msbuild_project(
        session,
        project_path=app_root / "srchybrid" / "emule.vcxproj",
        extra_properties=extra_properties,
        max_cpu_count=1 if session.options.platform == "ARM64" else None,
        target=target,
        step_name=f"APP main {flavor.name} package binary",
    )
    verify_app_control_flow_guard(
        session,
        binary_path=package_app_output_root / flavor.executable_name,
        step_name=f"APP main {flavor.name} package binary CFG",
    )


def _build_language_resources(session: BuildSession, app_root: Path, clean: bool, language_build_root: Path) -> None:
    language_solution = app_root / "srchybrid" / "lang" / "lang.sln"
    if not language_solution.is_file():
        raise RuntimeError(f"Cannot build missing language solution: {language_solution}")
    target = "Rebuild" if clean else "Build"
    if clean and language_build_root.exists():
        shutil.rmtree(language_build_root)
    output_root = language_build_root / "bin"
    intermediate_root = language_build_root / "obj"
    language_projects = sorted(language_solution.parent.glob("*.vcxproj"))
    if not language_projects:
        raise RuntimeError(f"Cannot build release languages without project files under: {language_solution.parent}")
    output_root.mkdir(parents=True, exist_ok=True)
    for project_path in language_projects:
        project_intermediate_root = intermediate_root / project_path.stem
        project_intermediate_root.mkdir(parents=True, exist_ok=True)
        invoke_msbuild_project(
            session,
            project_path=project_path,
            configuration="Dynamic",
            platform=session.options.platform,
            extra_properties=(
                _default_platform_toolset_property(session.layout),
                f"/p:OutDir={with_trailing_separator(output_root)}",
                f"/p:IntDir={with_trailing_separator(project_intermediate_root)}",
                "/p:PostBuildEventUseInBuild=false",
            ),
            max_cpu_count=1,
            target=target,
            step_name=f"APP main language {project_path.stem}",
        )


def _default_platform_toolset_property(layout: WorkspaceLayout) -> str:
    override = env_override(layout.toolset_override_variable)
    return f"/p:PlatformToolset={override}" if override else "/p:PlatformToolset=v143"


def _ensure_amutorrent_build_node(layout: WorkspaceLayout, platform: str) -> Path:
    """Materializes the pinned Node toolchain used to build aMuTorrent native modules.

    WHY pin (and not use the ambient PATH Node): aMuTorrent ships native modules
    (better-sqlite3) whose ``NODE_MODULE_VERSION`` is keyed to the Node *major*, and
    the installed suite loads them under the pinned ``AMUTORRENT_NODE_VERSION`` runtime.
    Building against whatever Node happens to be on PATH (e.g. Node 25 while the suite
    ships Node 24) produces an ABI-mismatched package that fails to launch. Building
    against the exact pinned Node — downloaded once and verified by SHA-256 — makes the
    native module deterministically match the shipped runtime regardless of the build
    host's installed Node. Returns the directory containing ``node.exe``/``npm.cmd``.
    """

    if platform not in AMUTORRENT_NODE_ARCHIVES:
        raise RuntimeError(f"No pinned aMuTorrent build Node is defined for platform {platform!r}.")
    archive_name, archive_sha256 = AMUTORRENT_NODE_ARCHIVES[platform]
    node_root = layout.output_tools_root / "node"
    node_dir = node_root / archive_name[: -len(".zip")]
    node_exe = node_dir / "node.exe"
    npm_cmd = node_dir / "npm.cmd"

    if not (node_exe.is_file() and npm_cmd.is_file()):
        archive_path = node_root / archive_name
        if not (archive_path.is_file() and _sha256(archive_path) == archive_sha256):
            node_root.mkdir(parents=True, exist_ok=True)
            tmp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
            tmp_path.unlink(missing_ok=True)
            url = f"https://nodejs.org/dist/{AMUTORRENT_NODE_VERSION}/{archive_name}"
            with urllib.request.urlopen(url, timeout=300) as response, tmp_path.open("wb") as handle:
                shutil.copyfileobj(response, handle)
            actual = _sha256(tmp_path)
            if actual != archive_sha256:
                tmp_path.unlink(missing_ok=True)
                raise RuntimeError(
                    "Pinned aMuTorrent Node archive SHA256 mismatch. "
                    f"Expected {archive_sha256}, got {actual}."
                )
            tmp_path.replace(archive_path)
        if node_dir.exists():
            shutil.rmtree(node_dir)
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(node_root)
        if not (node_exe.is_file() and npm_cmd.is_file()):
            raise RuntimeError(
                f"Pinned Node archive {archive_name} did not yield node.exe/npm.cmd under {node_dir}."
            )

    # Re-check the materialized Node actually runs the pinned version on this host:
    # catches a corrupt extract or a cross-arch archive that cannot execute here.
    completed = subprocess.run(
        [str(node_exe), "-p", "process.versions.node"],
        check=True,
        capture_output=True,
        text=True,
    )
    reported = "v" + completed.stdout.strip()
    if reported != AMUTORRENT_NODE_VERSION:
        raise RuntimeError(
            f"Pinned aMuTorrent build Node reported {reported}, expected {AMUTORRENT_NODE_VERSION}."
        )
    return node_dir


def _stage_amutorrent_source(amutorrent_root: Path, staged_source_root: Path) -> None:
    """Stages package build inputs without repo-local generated outputs."""

    if staged_source_root.exists():
        shutil.rmtree(staged_source_root)
    _copy_tree_filtered(amutorrent_root, staged_source_root, _exclude_amutorrent_source_stage)


def _exclude_amutorrent_source_stage(relative_path: Path, source_path: Path) -> bool:
    parts = relative_path.parts
    if any(part in {".git", "node_modules", ".cache", "__pycache__"} for part in parts):
        return True
    if parts and parts[0] in {"data", "logs", "tmp", "temp"}:
        return True
    if len(parts) >= 2 and parts[0] == "server" and parts[1] in {"data", "logs"}:
        return True
    if relative_path == Path("static/output.css"):
        return True
    if len(parts) >= 2 and parts[0] == "static" and parts[1] == "dist":
        return relative_path.name != "chart.umd.min.js"
    if source_path.is_file() and source_path.suffix.lower() in {".log", ".db", ".sqlite", ".sqlite3", ".map"}:
        return True
    return False


def _build_amutorrent_webapp(
    staged_source_root: Path,
    clean: bool,
    static_output_root: Path,
    node_bin_dir: Path,
) -> None:
    """Installs runtime dependencies and refreshes bundled assets with the pinned Node."""

    if clean:
        for generated_path in (
            staged_source_root / "node_modules",
            staged_source_root / "server" / "node_modules",
            static_output_root,
        ):
            if generated_path.exists():
                shutil.rmtree(generated_path)
    npm = node_bin_dir / "npm.cmd"
    if not npm.is_file():
        raise RuntimeError(f"Pinned aMuTorrent build Node toolchain is missing npm: {npm}.")
    env = os.environ.copy()
    env["AMUTORRENT_STATIC_OUTPUT_ROOT"] = str(static_output_root)
    # Put the pinned Node first on PATH so npm and any lifecycle scripts resolve it,
    # not the build host's ambient Node.
    env["PATH"] = str(node_bin_dir) + os.pathsep + env.get("PATH", "")
    static_output_root.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(npm), "ci"], cwd=staged_source_root, check=True, env=env)
    subprocess.run([str(npm), "ci", "--prefix", "server", "--omit=dev"], cwd=staged_source_root, check=True, env=env)
    subprocess.run([str(npm), "run", "build"], cwd=staged_source_root, check=True, env=env)


def _copy_amutorrent_runtime(amutorrent_root: Path, package_root: Path, static_build_root: Path) -> None:
    """Copies the aMuTorrent runtime payload."""

    server_root = amutorrent_root / "server"
    static_root = amutorrent_root / "static"
    if not (server_root / "node_modules").is_dir():
        raise RuntimeError(f"Cannot package missing aMuTorrent production node_modules: {server_root / 'node_modules'}")
    if not (static_build_root / "dist" / "app.bundle.js").is_file():
        raise RuntimeError(f"Cannot package missing built aMuTorrent frontend bundle: {static_build_root / 'dist' / 'app.bundle.js'}")
    if not (static_build_root / "output.css").is_file():
        raise RuntimeError(f"Cannot package missing built aMuTorrent CSS bundle: {static_build_root / 'output.css'}")

    _copy_tree_filtered(server_root, package_root / "server", _exclude_amutorrent_server_runtime)
    _copy_tree_filtered(static_root, package_root / "static", _exclude_amutorrent_static_source_runtime)
    _copy_tree_filtered(static_build_root, package_root / "static", _exclude_amutorrent_static_runtime)


def _exclude_amutorrent_server_runtime(relative_path: Path, source_path: Path) -> bool:
    parts = relative_path.parts
    if parts and parts[0] in {"data", "logs"}:
        return True
    if any(part in {".cache", "__pycache__"} for part in parts):
        return True
    if source_path.is_file() and source_path.suffix.lower() == ".map":
        return True
    if "node_modules" not in parts and source_path.is_file() and source_path.suffix.lower() in {".log", ".db", ".sqlite", ".sqlite3"}:
        return True
    return False


def _exclude_amutorrent_static_runtime(relative_path: Path, source_path: Path) -> bool:
    parts = relative_path.parts
    if parts and parts[0] in {"components", "contexts", "hooks", "utils"}:
        return True
    if relative_path.name == "app.js":
        return True
    if source_path.is_file() and source_path.suffix.lower() in {".map", ".sh"}:
        return True
    return False


def _exclude_amutorrent_static_source_runtime(relative_path: Path, source_path: Path) -> bool:
    parts = relative_path.parts
    if parts and parts[0] == "dist":
        return True
    if relative_path.name == "output.css":
        return True
    return _exclude_amutorrent_static_runtime(relative_path, source_path)


def _expected_language_dlls(tooling_repo_root: Path) -> tuple[str, ...]:
    """Returns the release language DLL names from the stock language manifest."""

    manifest_path = tooling_repo_root / "helpers" / "rc-release-languages.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"Cannot package without release language manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    languages = manifest.get("languages")
    if not isinstance(languages, list) or not languages:
        raise RuntimeError(f"Release language manifest has no languages: {manifest_path}")
    dlls: list[str] = []
    for entry in languages:
        rc_name = entry.get("rc") if isinstance(entry, dict) else None
        if not isinstance(rc_name, str) or not rc_name.endswith(".rc"):
            raise RuntimeError(f"Release language manifest entry is missing an .rc file name: {entry!r}")
        dlls.append(Path(rc_name).with_suffix(".dll").name)
    return tuple(sorted(dlls))


def _package_language_path(lang_path: Path, platform: str, expected_language_dlls: tuple[str, ...]) -> Path:
    if not lang_path.is_dir():
        raise RuntimeError(f"Cannot package missing built language DLLs: {lang_path}")
    missing = [dll for dll in expected_language_dlls if not (lang_path / dll).is_file()]
    if missing:
        raise RuntimeError(f"Cannot package missing built language DLLs in {lang_path}:\n" + "\n".join(missing))
    extra = sorted(path.name for path in lang_path.glob("*.dll") if path.name not in expected_language_dlls)
    if extra:
        raise RuntimeError(f"Cannot package unexpected language DLLs in {lang_path}:\n" + "\n".join(extra))
    for dll in expected_language_dlls:
        _assert_pe_machine(lang_path / dll, platform)
    return lang_path


def _copy_language_dlls(lang_path: Path, destination_path: Path, expected_language_dlls: tuple[str, ...]) -> None:
    """Copies only release-manifest language DLLs from a build output directory."""

    destination_path.mkdir(parents=True, exist_ok=True)
    for dll in expected_language_dlls:
        shutil.copy2(lang_path / dll, destination_path / dll)


def _copy_package_file(source_path: Path, package_root: Path, relative_destination_path: Path) -> None:
    if not source_path.is_file():
        raise RuntimeError(f"Cannot package missing file: {source_path}")
    destination_path = package_root / relative_destination_path
    _assert_path_under_root(destination_path, package_root, "release package file")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination_path)


def _sign_release_package_files(package_root: Path, *, require_signing: bool) -> dict[str, object]:
    """Authenticode-signs staged release files when a signing identity is configured."""

    config = _release_signing_config(require_signing=require_signing)
    if config is None:
        return {
            "mode": "unsigned",
            "required": False,
            "signedFiles": [],
            "reason": "No Authenticode signing identity configured.",
        }

    signed_files: list[str] = []
    staging_root = package_root.parent
    for path in _release_signing_targets(package_root):
        command = [
            str(config["signtool"]),
            "sign",
            "/fd",
            "SHA256",
            "/tr",
            str(config["timestamp_url"]),
            "/td",
            "SHA256",
            *config["identity_args"],
            str(path),
        ]
        subprocess.run(command, check=True)
        signed_files.append(path.relative_to(staging_root).as_posix())

    return {
        "mode": "authenticode",
        "required": require_signing,
        "timestampUrl": config["timestamp_url"],
        "signedFiles": signed_files,
    }


def _release_signing_config(*, require_signing: bool) -> dict[str, object] | None:
    signtool = os.environ.get(SIGNTOOL_PATH_ENV) or shutil.which("signtool.exe") or shutil.which("signtool")
    cert_sha1 = os.environ.get(SIGNING_CERT_SHA1_ENV, "").strip()
    cert_path = os.environ.get(SIGNING_CERT_PATH_ENV, "").strip()
    cert_password = os.environ.get(SIGNING_CERT_PASSWORD_ENV, "")
    timestamp_url = os.environ.get(SIGNING_TIMESTAMP_URL_ENV, DEFAULT_SIGNING_TIMESTAMP_URL).strip()

    if cert_sha1 and cert_path:
        raise RuntimeError(f"Set only one of {SIGNING_CERT_SHA1_ENV} or {SIGNING_CERT_PATH_ENV}.")
    if not cert_sha1 and not cert_path:
        if require_signing:
            raise RuntimeError(
                "Release package signing is required but no signing identity is configured. "
                f"Set {SIGNING_CERT_SHA1_ENV} or {SIGNING_CERT_PATH_ENV}."
            )
        return None
    if not signtool:
        raise RuntimeError(f"Release package signing requires signtool.exe on PATH or {SIGNTOOL_PATH_ENV}.")
    if not timestamp_url:
        raise RuntimeError(f"Release package signing requires {SIGNING_TIMESTAMP_URL_ENV} or the default timestamp URL.")

    if cert_path:
        resolved_cert_path = Path(cert_path).expanduser().resolve()
        if not resolved_cert_path.is_file():
            raise RuntimeError(f"Release signing certificate file is missing: {resolved_cert_path}")
        identity_args: tuple[str, ...] = ("/f", str(resolved_cert_path))
        if cert_password:
            identity_args = (*identity_args, "/p", cert_password)
    else:
        identity_args = ("/sha1", cert_sha1)

    return {
        "signtool": Path(signtool),
        "timestamp_url": timestamp_url,
        "identity_args": identity_args,
    }


def _release_signing_targets(package_root: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(package_root.rglob("*"))
        if path.is_file() and path.suffix.lower() in {".exe", ".dll", ".ps1"}
    )


def _copy_emule_runtime_assets(build_repo_root: Path, package_root: Path) -> None:
    """Copies package-owned eMuleBB runtime assets."""

    source_root = build_repo_root / "emule_workspace" / "release_assets" / EMULEBB_RELEASE_ASSET_ROOT_NAME
    for relative_path in EMULEBB_RUNTIME_ASSET_PATHS:
        _copy_package_file(source_root / relative_path, package_root, Path(relative_path))


def _write_standalone_bootstrapper_asset(
    *,
    package_root: Path,
    release_root: Path,
    release_version: str,
) -> tuple[Path, Path, str]:
    """Copies the web bootstrapper next to the ZIP so public setup does not depend on a branch."""

    source_path = package_root / "scripts" / "Bootstrap-eMuleBBSuite.ps1"
    if not source_path.is_file():
        raise RuntimeError(f"Cannot publish missing suite bootstrapper: {source_path}")
    asset_path = release_root / "Bootstrap-eMuleBBSuite.ps1"
    hash_path = release_root / "Bootstrap-eMuleBBSuite.ps1.sha256"
    _assert_path_under_root(asset_path, release_root, "suite bootstrapper asset")
    _assert_path_under_root(hash_path, release_root, "suite bootstrapper hash")
    source_text = source_path.read_text(encoding="utf-8")
    asset_text = _bake_bootstrapper_release_version(source_text, release_version)
    asset_path.write_text(asset_text, encoding="utf-8", newline="\n")
    digest = _sha256(asset_path)
    hash_path.write_text(f"{digest}  {asset_path.name}\n", encoding="ascii", newline="\n")
    return asset_path, hash_path, digest


def _write_suite_scripts_bundle_asset(
    *,
    package_root: Path,
    release_root: Path,
    release_version: str,
) -> tuple[Path, Path, str]:
    """Writes the suite control scripts and config as a separate hashed release asset."""

    asset_path = release_root / f"suite-scripts-{release_version}.zip"
    manifest_path = release_root / f"suite-scripts-{release_version}.manifest.json"
    _assert_path_under_root(asset_path, release_root, "suite scripts asset")
    _assert_path_under_root(manifest_path, release_root, "suite scripts manifest")
    release_root.mkdir(parents=True, exist_ok=True)
    manifest_entries = []
    with zipfile.ZipFile(asset_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative_path in (*EMULEBB_RUNTIME_SCRIPT_PATHS, *EMULEBB_CONFIG_ASSET_PATHS):
            source_path = package_root / relative_path
            if not source_path.is_file():
                raise RuntimeError(f"Cannot publish missing suite bundle asset: {source_path}")
            entry_name = f"{EMULEBB_PACKAGE_ROOT_NAME}/{relative_path}"
            archive.write(source_path, entry_name)
            manifest_entries.append(
                {
                    "path": entry_name,
                    "sha256": _sha256(source_path),
                    "bytes": source_path.stat().st_size,
                }
            )
    digest = _sha256(asset_path)
    manifest = {
        "schema": "emulebb.suite-scripts-manifest.v1",
        "version": release_version,
        "asset": asset_path.name,
        "sha256": digest,
        "entries": manifest_entries,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return asset_path, manifest_path, digest


def _write_automation_examples_asset(
    *,
    build_repo_root: Path,
    release_root: Path,
    release_version: str,
) -> tuple[Path, Path, str]:
    """Writes the REST automation examples as a separate hashed release asset."""

    source_root = build_repo_root / "emule_workspace" / "release_assets" / EMULEBB_AUTOMATION_EXAMPLE_ASSET_ROOT_NAME
    asset_path = release_root / f"automation-examples-{release_version}.zip"
    manifest_path = release_root / f"automation-examples-{release_version}.manifest.json"
    _assert_path_under_root(asset_path, release_root, "automation examples asset")
    _assert_path_under_root(manifest_path, release_root, "automation examples manifest")
    release_root.mkdir(parents=True, exist_ok=True)
    manifest_entries = []
    with zipfile.ZipFile(asset_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative_path in EMULEBB_AUTOMATION_EXAMPLE_PATHS:
            source_path = source_root / relative_path
            if not source_path.is_file():
                raise RuntimeError(f"Cannot publish missing automation example asset: {source_path}")
            entry_name = f"{EMULEBB_PACKAGE_ROOT_NAME}/examples/{relative_path}"
            archive.write(source_path, entry_name)
            manifest_entries.append(
                {
                    "path": entry_name,
                    "sha256": _sha256(source_path),
                    "bytes": source_path.stat().st_size,
                }
            )
    digest = _sha256(asset_path)
    manifest = {
        "schema": "emulebb.automation-examples-manifest.v1",
        "version": release_version,
        "asset": asset_path.name,
        "sha256": digest,
        "entries": manifest_entries,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return asset_path, manifest_path, digest


def _bake_bootstrapper_release_version(source_text: str, release_version: str) -> str:
    """Returns the standalone bootstrapper with its release version default pinned."""

    escaped_version = release_version.replace("'", "''")
    pattern = re.compile(r"(?m)^(\s*\[string\]\$Version)(?:\s*=\s*'[^']*')?,\s*$")
    updated, count = pattern.subn(rf"\1 = '{escaped_version}',", source_text, count=1)
    if count != 1:
        raise RuntimeError("Cannot bake release version into suite bootstrapper: Version parameter not found.")
    return updated


def _copy_directory_contents(source_path: Path, destination_path: Path) -> None:
    destination_path.mkdir(parents=True, exist_ok=True)
    for child in source_path.iterdir():
        target = destination_path / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)


def _copy_tree_filtered(source_path: Path, destination_path: Path, exclude) -> None:
    """Copies a tree while allowing package-specific runtime exclusions."""

    destination_path.mkdir(parents=True, exist_ok=True)
    for source_child in sorted(source_path.rglob("*")):
        relative_path = source_child.relative_to(source_path)
        if exclude(relative_path, source_child):
            continue
        target = destination_path / relative_path
        _assert_path_under_root(target, destination_path, "filtered package file")
        if source_child.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif source_child.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_child, target)


def _write_package_readme(
    package_root: Path,
    release_version: str,
    platform: str,
    *,
    flavor: ReleasePackageFlavorSpec = RELEASE_PACKAGE_FLAVORS[0],
) -> None:
    """Writes the package-facing README."""

    readme_path = package_root / "README.md"
    _assert_path_under_root(readme_path, package_root, "release package README")
    asset_arch = "arm64" if platform == "ARM64" else "x64"
    readme_path.write_text(
        "\n".join(
            (
                "# eMule broadband edition",
                "",
                f"Version: {release_version}",
                f"Architecture: {asset_arch}",
                f"Package flavor: {flavor.name}",
                "",
                f"Run `{flavor.executable_name}` from this directory. The package is portable and keeps the",
                "stock eMule language DLLs under `lang/`.",
                "",
                (
                    "Diagnostics enabled: " + ", ".join(flavor.diagnostic_features)
                    if flavor.diagnostic_features
                    else "Diagnostics enabled: none"
                ),
                "",
                "REST API documentation is included under `docs/`. Language DLLs are built",
                "from the stock eMule language resource set and are architecture-specific.",
                "`SBOM.spdx.json` records the package files and source/dependency",
                "components in SPDX 2.3 JSON format.",
                "",
                "Windows setup and Prowlarr/Radarr/Sonarr integration helpers are included",
                "under `scripts/` and are launched from the app Tools menu. Bundled skin profiles and",
                "toolbar bitmap strips are included under `skins/`.",
                "",
                "MediaInfo integration remains optional. To enable audio/video metadata,",
                f"install a compatible external `MediaInfo.dll` next to `{flavor.executable_name}`; it is not",
                "bundled in this ZIP.",
                "",
                "This ZIP does not include debug symbols.",
            )
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_package_release_notes(package_root: Path, release_version: str) -> None:
    """Writes concise release notes for the binary package."""

    notes_path = package_root / "RELEASE-NOTES.md"
    _assert_path_under_root(notes_path, package_root, "release package notes")
    notes_path.write_text(
        "\n".join(
            (
                "# Release Notes",
                "",
                f"eMule broadband edition {release_version} is the first public release-candidate line",
                "for eMuleBB.",
                "",
                "- Preserves stock eD2K/Kad protocol compatibility.",
                "- Ships x64 and ARM64 portable ZIP assets.",
                "- Bundles the full stock language DLL set for the selected architecture.",
                "- Includes the in-process REST API documentation used by controller integrations.",
                "- Includes an SPDX 2.3 JSON SBOM in the package root.",
                "- Does not bundle optional external MediaInfo runtime DLLs.",
            )
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_package_license_notice(package_root: Path) -> None:
    notice_path = package_root / "LICENSE-NOTICE.txt"
    _assert_path_under_root(notice_path, package_root, "release package license notice")
    notice_path.write_text(
        "\n".join(
            (
                "eMule broadband edition contains eMule-derived application code licensed under GPL-2.0-or-later.",
                "The source tree retains the per-file GPL notices from the original eMule project and eMuleBB changes.",
                "Third-party libraries are linked from the canonical workspace dependency pins and retain their upstream licenses.",
                "See GPL-2.0-or-later.txt and THIRD-PARTY-NOTICES.txt in this package.",
                "For complete corresponding source, use the eMuleBB source repositories "
                "at the app commit recorded in the package manifest.",
            )
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_package_third_party_notices(package_root: Path) -> None:
    """Writes third-party dependency notices for the bundled binary."""

    notice_path = package_root / "THIRD-PARTY-NOTICES.txt"
    _assert_path_under_root(notice_path, package_root, "release package third-party notices")
    notice_path.write_text(
        "\n".join(
            (
                "Third-party notices for eMule broadband edition",
                "",
                "The binary is built from the canonical workspace dependency pins recorded",
                "in the release manifest. The package does not redistribute separate",
                "third-party DLLs except stock eMule language resource DLLs.",
                "",
                "Linked dependencies and license families:",
                "- Crypto++: Boost Software License 1.0",
                "- id3lib: GNU Library General Public License 2.0",
                "- miniupnpc: BSD-style license from the MiniUPnP project",
                "- libpcpnatpmp: PCP/NAT-PMP client library license from the pinned fork",
                "- ResizableLib: Artistic License 2.0",
                "- zlib: zlib license",
                "- Mbed TLS / TF-PSA-Crypto: Apache-2.0 OR GPL-2.0-or-later",
                "- nlohmann/json: MIT license",
                "",
                "Complete corresponding source and full upstream license files are available",
                "from the eMuleBB source repositories at the commits recorded in the",
                "release manifest.",
            )
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_package_gpl_text(layout: WorkspaceLayout, package_root: Path) -> None:
    """Writes the GPL-2.0-or-later license text from a pinned local dependency."""

    license_path = layout.resolve_workspace_path("repos/third_party/emulebb-mbedtls/LICENSE")
    if not license_path.is_file():
        raise RuntimeError(f"Cannot package missing GPL license source: {license_path}")
    text = license_path.read_text(encoding="utf-8", errors="replace")
    start = text.find("                    GNU GENERAL PUBLIC LICENSE")
    if start < 0:
        raise RuntimeError(f"Cannot find GPL text in license source: {license_path}")
    gpl_text = text[start:].strip() + "\n"
    destination_path = package_root / "GPL-2.0-or-later.txt"
    _assert_path_under_root(destination_path, package_root, "release package GPL text")
    destination_path.write_text(gpl_text, encoding="utf-8", newline="\n")


def _write_amutorrent_readme(package_root: Path, release_version: str, platform: str) -> None:
    """Writes the optional aMuTorrent package README."""

    asset_arch = "arm64" if platform == "ARM64" else "x64"
    readme_path = package_root / "README.md"
    _assert_path_under_root(readme_path, package_root, "aMuTorrent package README")
    readme_path.write_text(
        "\n".join(
            (
                "# aMuTorrent optional controller",
                "",
                f"eMule broadband edition package version: {release_version}",
                f"Architecture: {asset_arch}",
                "",
                "This ZIP is a controller runtime payload. Use the eMuleBB suite",
                "installer from the main app package to create start/stop scripts,",
                "download Node when needed, and wire package-local runtime state.",
                "",
                "The suite installer uses Node 24 or newer from PATH when available.",
                f"Otherwise it downloads the pinned {AMUTORRENT_NODE_VERSION} Windows runtime",
                "under the suite install root.",
                "`SBOM.spdx.json` records the packaged controller files and runtime",
                "components in SPDX 2.3 JSON format.",
                "",
                "Runtime state is provided by the suite installer through",
                "`AMUTORRENT_DATA_DIR`; this package does not write Windows AppData",
                "defaults and does not include a standalone installer.",
                "",
                "This package is a portable multi-client aMuTorrent controller. It keeps",
                "runtime defaults under the package root.",
            )
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_zip(staging_root: Path, package_root: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package_root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(staging_root).as_posix())


def _assert_release_package_contents(
    zip_path: Path,
    expected_language_dlls: tuple[str, ...],
    platform: str,
    *,
    flavor: ReleasePackageFlavorSpec = RELEASE_PACKAGE_FLAVORS[0],
) -> None:
    with zipfile.ZipFile(zip_path, "r") as archive:
        entry_names = [name.replace("\\", "/") for name in archive.namelist()]
        entry_set = set(entry_names)
        stale_root_entries = [name for name in entry_names if name.startswith("eMule/")]
        if stale_root_entries:
            sample = "\n".join(stale_root_entries[:20])
            raise RuntimeError(f"Release package contains retired eMule root entries; use {EMULEBB_PACKAGE_ROOT_NAME}/:\n{sample}")
        required_entries = (
            f"{EMULEBB_PACKAGE_ROOT_NAME}/{flavor.executable_name}",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/README.md",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/RELEASE-NOTES.md",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/LICENSE-NOTICE.txt",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/GPL-2.0-or-later.txt",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/THIRD-PARTY-NOTICES.txt",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/SBOM.spdx.json",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/docs/REST-API-CONTRACT.md",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/docs/REST-API-OPENAPI.yaml",
            f"{EMULEBB_PACKAGE_ROOT_NAME}/docs/REST-API-PARITY-INVENTORY.md",
            *(f"{EMULEBB_PACKAGE_ROOT_NAME}/{relative_path}" for relative_path in EMULEBB_RUNTIME_ASSET_PATHS),
        )
        for required_entry in required_entries:
            if required_entry not in entry_set:
                raise RuntimeError(f"Release package is missing required entry '{required_entry}': {zip_path}")
        if flavor.executable_name != APP_EXE_NAME and f"{EMULEBB_PACKAGE_ROOT_NAME}/{APP_EXE_NAME}" in entry_set:
            raise RuntimeError(
                "Diagnostics release package must not include an emulebb.exe compatibility alias; "
                f"use {flavor.executable_name} only."
            )
        for relative_path in EMULEBB_RUNTIME_SCRIPT_PATHS:
            entry_name = f"{EMULEBB_PACKAGE_ROOT_NAME}/{relative_path}"
            script = archive.read(entry_name).decode("utf-8-sig")
            first_non_empty = next((line.strip() for line in script.splitlines() if line.strip()), "")
            if first_non_empty != "#Requires -Version 5.1":
                raise RuntimeError(f"Release package script '{entry_name}' must declare Windows PowerShell 5.1 compatibility.")
        language_dlls = sorted(name for name in entry_names if re.fullmatch(rf"{EMULEBB_PACKAGE_ROOT_NAME}/lang/[^/]+\.dll", name))
        expected_language_entries = tuple(f"{EMULEBB_PACKAGE_ROOT_NAME}/lang/{dll}" for dll in expected_language_dlls)
        missing_language_entries = [name for name in expected_language_entries if name not in entry_set]
        extra_language_entries = [name for name in language_dlls if name not in expected_language_entries]
        if missing_language_entries:
            raise RuntimeError("Release package is missing language DLLs:\n" + "\n".join(missing_language_entries))
        if extra_language_entries:
            raise RuntimeError("Release package contains unexpected language DLLs:\n" + "\n".join(extra_language_entries))
        for entry_name in (f"{EMULEBB_PACKAGE_ROOT_NAME}/{flavor.executable_name}", *language_dlls):
            _assert_pe_machine_bytes(archive.read(entry_name), platform, entry_name)
        webserver_files = [name for name in entry_names if re.fullmatch(rf"{EMULEBB_PACKAGE_ROOT_NAME}/webserver/.+[^/]", name)]
        if webserver_files:
            raise RuntimeError(
                "Release package contains legacy webserver payload that is not shipped in RC assets:\n"
                + "\n".join(webserver_files[:20])
            )
        forbidden_entries = [
            name
            for name in entry_names
            if re.search(r"(^|/)(Win32|x86)(/|$)", name)
            or re.search(r"\.(pdb|obj|ilk|idb|iobj|ipdb|tlog|lastbuildstate|vcxproj|filters|sln|aps|res|rc|rc2|cpp|c|h|hpp)$", name)
        ]
        if forbidden_entries:
            sample = "\n".join(forbidden_entries[:20])
            raise RuntimeError(f"Release package contains build/source artifacts:\n{sample}")
    print(f"Package content check: {zip_path} ({len(entry_names)} entries, {len(language_dlls)} language DLLs)")


def _assert_emulebb_rust_package_contents(zip_path: Path) -> None:
    """Checks the Rust package for required runtime files and forbidden UI artifacts."""

    with zipfile.ZipFile(zip_path, "r") as archive:
        entry_names = [name.replace("\\", "/") for name in archive.namelist()]
        entry_set = set(entry_names)
        required_entries = {
            f"{EMULEBB_RUST_PACKAGE_ROOT_NAME}/emulebb-rust.exe",
            EMULEBB_RUST_PACKAGE_REQUIRED_WEBUI_ENTRY,
            f"{EMULEBB_RUST_PACKAGE_ROOT_NAME}/emulebb-rust-settings.example.toml",
            f"{EMULEBB_RUST_PACKAGE_ROOT_NAME}/LICENSE",
            f"{EMULEBB_RUST_PACKAGE_ROOT_NAME}/README.md",
            f"{EMULEBB_RUST_PACKAGE_ROOT_NAME}/RELEASE-SCOPE.md",
            f"{EMULEBB_RUST_PACKAGE_ROOT_NAME}/SBOM.spdx.json",
        }
        for required_entry in required_entries:
            if required_entry not in entry_set:
                raise RuntimeError(f"eMuleBB Rust package is missing required entry '{required_entry}': {zip_path}")
        forbidden_entries = sorted(
            name
            for name in entry_names
            if "emulebb-rust-ui" in Path(name).name or "emulebb-rust-diagnostics" in Path(name).name
        )
        if forbidden_entries:
            sample = "\n".join(forbidden_entries[:20])
            raise RuntimeError(f"eMuleBB Rust package contains forbidden UI/diagnostics artifacts:\n{sample}")
        root_prefix = f"{EMULEBB_RUST_PACKAGE_ROOT_NAME}/"
        outside_root = sorted(name for name in entry_names if not name.startswith(root_prefix))
        if outside_root:
            sample = "\n".join(outside_root[:20])
            raise RuntimeError(f"eMuleBB Rust package contains entries outside {EMULEBB_RUST_PACKAGE_ROOT_NAME}/:\n{sample}")
        _assert_pe_machine_bytes(
            archive.read(f"{EMULEBB_RUST_PACKAGE_ROOT_NAME}/emulebb-rust.exe"),
            "x64",
            f"{EMULEBB_RUST_PACKAGE_ROOT_NAME}/emulebb-rust.exe",
        )
    print(f"eMuleBB Rust package content check: {zip_path} ({len(entry_names)} entries)")


def _assert_amutorrent_package_contents(zip_path: Path) -> None:
    """Checks the optional aMuTorrent package for required runtime files and forbidden state."""

    with zipfile.ZipFile(zip_path, "r") as archive:
        entry_names = [name.replace("\\", "/") for name in archive.namelist()]
        entry_set = set(entry_names)
        required_entries = (
            "aMuTorrent/README.md",
            "aMuTorrent/LICENSE-aMuTorrent.txt",
            "aMuTorrent/SBOM.spdx.json",
            "aMuTorrent/server/server.js",
            "aMuTorrent/server/package.json",
            "aMuTorrent/server/package-lock.json",
            "aMuTorrent/server/node_modules/express/package.json",
            "aMuTorrent/server/node_modules/better-sqlite3/package.json",
            "aMuTorrent/static/index.html",
            "aMuTorrent/static/output.css",
            "aMuTorrent/static/dist/app.bundle.js",
        )
        for required_entry in required_entries:
            if required_entry not in entry_set:
                raise RuntimeError(f"aMuTorrent package is missing required entry '{required_entry}': {zip_path}")
        forbidden_entries = [
            name
            for name in entry_names
            if name.startswith("aMuTorrent/server/data/")
            or name.startswith("aMuTorrent/server/logs/")
            or name.startswith("aMuTorrent/data/")
            or name.startswith("aMuTorrent/logs/")
            or name.startswith("aMuTorrent/runtime/")
            or name.startswith("aMuTorrent/installer/")
            or name.startswith("aMuTorrent/node_modules/")
            or name.startswith("aMuTorrent/static/components/")
            or name.startswith("aMuTorrent/static/contexts/")
            or name.startswith("aMuTorrent/static/hooks/")
            or name.startswith("aMuTorrent/static/utils/")
            or name == "aMuTorrent/static/app.js"
            or "/.git/" in name
            or "/tests/" in name
            or name.endswith(".map")
            or name.endswith(".log")
            or name.endswith(".db")
            or name.endswith(".sqlite")
            or name.endswith(".sqlite3")
            or name == "aMuTorrent/server/node_modules/nodemon/package.json"
            or " " in name
        ]
        if forbidden_entries:
            sample = "\n".join(forbidden_entries[:20])
            raise RuntimeError(f"aMuTorrent package contains forbidden generated or source artifacts:\n{sample}")
    print(f"aMuTorrent package content check: {zip_path} ({len(entry_names)} entries)")


def _assert_pe_machine(path: Path, platform: str) -> None:
    """Checks that one PE file matches the selected package platform."""

    if _pe_machine(path.read_bytes(), str(path)) != PE_MACHINES[platform]:
        raise RuntimeError(f"PE architecture mismatch for {path}: expected {platform}.")


def _assert_release_binary_diagnostics(path: Path, flavor: ReleasePackageFlavorSpec) -> None:
    """Checks that one package binary has exactly the diagnostics expected for its flavor."""

    _assert_binary_marker_state(
        path,
        markers=STARTUP_DIAGNOSTICS_BINARY_MARKERS,
        expected=flavor.enable_startup_diagnostics,
        description="startup diagnostics support",
        enable_property="/p:EnableStartupDiagnostics=true",
        disable_property="/p:EnableStartupDiagnostics=false",
    )
    _assert_binary_marker_state(
        path,
        markers=PACKET_DIAGNOSTICS_BINARY_MARKERS,
        expected=flavor.enable_packet_diagnostics,
        description="packet diagnostics support",
        enable_property="/p:EnablePacketDiagnostics=true",
        disable_property="/p:EnablePacketDiagnostics=false",
    )
    _assert_binary_marker_state(
        path,
        markers=UPLOAD_SLOT_DIAGNOSTICS_BINARY_MARKERS,
        expected=flavor.enable_upload_slot_diagnostics,
        description="upload slot diagnostics support",
        enable_property="/p:EnableUploadSlotDiagnostics=true",
        disable_property="/p:EnableUploadSlotDiagnostics=false",
    )
    _assert_binary_marker_state(
        path,
        markers=DOWNLOAD_SLOT_DIAGNOSTICS_BINARY_MARKERS,
        expected=flavor.enable_download_slot_diagnostics,
        description="download slot diagnostics support",
        enable_property="/p:EnableDownloadSlotDiagnostics=true",
        disable_property="/p:EnableDownloadSlotDiagnostics=false",
    )
    _assert_binary_marker_state(
        path,
        markers=BAD_PEER_DIAGNOSTICS_BINARY_MARKERS,
        expected=flavor.enable_bad_peer_diagnostics,
        description="bad-peer diagnostics support",
        enable_property="/p:EnableBadPeerDiagnostics=true",
        disable_property="/p:EnableBadPeerDiagnostics=false",
    )
    _assert_binary_marker_state(
        path,
        markers=KAD_DIAGNOSTICS_BINARY_MARKERS,
        expected=flavor.enable_kad_diagnostics,
        description="Kad diagnostics support",
        enable_property="/p:EnableKadDiagnostics=true",
        disable_property="/p:EnableKadDiagnostics=false",
    )


def _assert_binary_marker_state(
    path: Path,
    *,
    markers: tuple[bytes, ...],
    expected: bool,
    description: str,
    enable_property: str,
    disable_property: str,
) -> None:
    """Checks marker presence or absence in a compiled binary."""

    payload = path.read_bytes()
    found = any(marker in payload for marker in markers)
    if expected and not found:
        raise RuntimeError(
            f"Release diagnostics package binary is missing {description}; "
            f"rebuild {path} with {enable_property}."
        )
    if not expected and found:
        raise RuntimeError(
            f"Release standard package binary still contains {description}; "
            f"rebuild {path} with {disable_property}."
        )


def _assert_startup_diagnostics_not_compiled(path: Path) -> None:
    """Rejects standard release package binaries that still include startup diagnostics support."""

    _assert_binary_marker_state(
        path,
        markers=STARTUP_DIAGNOSTICS_BINARY_MARKERS,
        expected=False,
        description="startup diagnostics support",
        enable_property="/p:EnableStartupDiagnostics=true",
        disable_property="/p:EnableStartupDiagnostics=false",
    )


def _assert_pe_machine_bytes(payload: bytes, platform: str, label: str) -> None:
    """Checks that one PE payload from a ZIP matches the selected package platform."""

    machine = _pe_machine(payload, label)
    expected = PE_MACHINES[platform]
    if machine != expected:
        raise RuntimeError(f"PE architecture mismatch for {label}: got 0x{machine:04X}, expected {platform}.")


def _pe_machine(payload: bytes, label: str) -> int:
    """Returns the COFF machine type from a PE payload."""

    if len(payload) < 0x40 or payload[:2] != b"MZ":
        raise RuntimeError(f"Not a PE file: {label}")
    pe_offset = struct.unpack_from("<I", payload, 0x3C)[0]
    if pe_offset < 0 or pe_offset + 6 > len(payload):
        raise RuntimeError(f"Invalid PE header offset in {label}")
    if payload[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise RuntimeError(f"Invalid PE signature in {label}")
    return struct.unpack_from("<H", payload, pe_offset + 4)[0]


def _app_mod_release_version(app_root: Path) -> str:
    version_header_path = app_root / "srchybrid" / "Version.h"
    if not version_header_path.is_file():
        raise RuntimeError(f"Cannot read missing app version header: {version_header_path}")
    parts: dict[str, int] = {}
    pattern = re.compile(r"^\s*#define\s+MOD_RELEASE_VERSION_(MAJOR|MINOR|PATCH)\s+(\d+)\s*$")
    for line in version_header_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.match(line)
        if match:
            parts[match.group(1)] = int(match.group(2))
    for required_part in ("MAJOR", "MINOR", "PATCH"):
        if required_part not in parts:
            raise RuntimeError(f"Cannot find MOD_RELEASE_VERSION_{required_part} in {version_header_path}")
    return f"{parts['MAJOR']}.{parts['MINOR']}.{parts['PATCH']}"


def _assert_package_version_matches_app(app_root: Path, release_version: str) -> None:
    app_release_version = _app_mod_release_version(app_root)
    if _base_release_version(release_version) != app_release_version:
        raise RuntimeError(
            f"package release version mismatch: --release-version is '{release_version}' "
            f"but app MOD_RELEASE_VERSION is '{app_release_version}'."
        )


def _is_release_version(release_version: str) -> bool:
    return RELEASE_VERSION_PATTERN.fullmatch(release_version) is not None


def _base_release_version(release_version: str) -> str:
    return release_version.split("-", 1)[0]


def _assert_path_under_root(path: Path, root: Path, label: str) -> None:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise RuntimeError(f"{label} resolved outside expected root: {resolved_path}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _zip_entry_hashes(zip_path: Path) -> dict[str, str]:
    """Returns SHA-256 hashes for every file entry in a release ZIP."""

    hashes: dict[str, str] = {}
    with zipfile.ZipFile(zip_path, "r") as archive:
        for name in sorted(archive.namelist()):
            if name.endswith("/"):
                continue
            hashes[name.replace("\\", "/")] = hashlib.sha256(archive.read(name)).hexdigest()
    return hashes
