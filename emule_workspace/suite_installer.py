"""Python boundary for invoking the packaged eMuleBB suite installer."""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .process import find_tool, run_native
from .release import EMULEBB_PACKAGE_ROOT_NAME

SUITE_BUNDLE = "Full"
LOCAL_INSTALL_KIND = "Development"


@dataclass(frozen=True)
class SuiteInstallerOptions:
    """Resolved command inputs for a non-interactive suite installer run."""

    install_root: Path
    release_root: Path
    installer_script: Path
    release_version: str
    platform: str
    lan_bind_address: str | None
    emulebb_lan_bind_address: str | None
    emulebb_port: int
    prowlarr_port: int
    radarr_port: int
    sonarr_port: int
    lidarr_port: int
    whisparr_port: int
    dependency_manifest: Path | None
    import_profile_dir: Path | None
    emulebb_pdb_path: Path | None
    p2p_bind_interface: str
    emulebb_package_flavor: str = "standard"
    install_kind: str = LOCAL_INSTALL_KIND
    bundle: str = SUITE_BUNDLE


@dataclass(frozen=True)
class SuiteInstallerInvocation:
    """Materialized command and staging paths for auditing and tests."""

    command: tuple[str | Path, ...]
    installer_script: Path
    staging_root: Path


def extract_packaged_installer(*, package_zip: Path, install_root: Path, release_version: str) -> Path:
    """Extracts the packaged installer into transient local staging."""

    staging_root = install_root / ".staging" / f"installer-{release_version}-{_timestamp_for_path()}"
    _assert_under_root(staging_root, install_root, "installer staging path")
    installer_path = staging_root / "Install-eMuleBBSuite.ps1"
    manifest_helper_path = staging_root / "Import-SuiteAppManifest.ps1"
    staging_root.mkdir(parents=True, exist_ok=True)
    installer_member_name = f"{EMULEBB_PACKAGE_ROOT_NAME}/scripts/Install-eMuleBBSuite.ps1"
    manifest_helper_member_name = f"{EMULEBB_PACKAGE_ROOT_NAME}/scripts/Import-SuiteAppManifest.ps1"
    with zipfile.ZipFile(package_zip, "r") as archive:
        try:
            installer_data = archive.read(installer_member_name)
        except KeyError as exc:
            raise RuntimeError(f"Release package is missing {installer_member_name}.") from exc
        try:
            manifest_helper_data = archive.read(manifest_helper_member_name)
        except KeyError as exc:
            raise RuntimeError(f"Release package is missing {manifest_helper_member_name}.") from exc
    installer_path.write_bytes(installer_data)
    manifest_helper_path.write_bytes(manifest_helper_data)
    return installer_path


def invoke_suite_installer(options: SuiteInstallerOptions) -> SuiteInstallerInvocation:
    """Runs the packaged suite installer to materialize a local install."""

    powershell = find_tool(("powershell.exe", "powershell", "pwsh.exe", "pwsh"))
    if powershell is None:
        raise RuntimeError("PowerShell was not found on PATH.")
    invocation = build_suite_installer_invocation(powershell=powershell, options=options)
    try:
        run_native(list(invocation.command), label="eMuleBB suite installer", cwd=options.install_root)
    finally:
        if invocation.staging_root.exists():
            shutil.rmtree(invocation.staging_root)
    return invocation


def build_suite_installer_invocation(*, powershell: Path, options: SuiteInstallerOptions) -> SuiteInstallerInvocation:
    """Builds the non-interactive command line for the packaged installer."""

    command: list[str | Path] = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        options.installer_script,
        "-NonInteractive",
        "-Force",
        "-NoStart",
        "-Bundle",
        options.bundle,
        "-InstallRoot",
        str(options.install_root),
        "-ReleaseBaseUrl",
        options.release_root.resolve().as_uri(),
        "-Version",
        options.release_version,
        "-Platform",
        options.platform,
        "-InstallKind",
        options.install_kind,
        "-EmulebbPackageFlavor",
        options.emulebb_package_flavor,
        "-EmulebbPort",
        str(options.emulebb_port),
        "-ProwlarrPort",
        str(options.prowlarr_port),
        "-RadarrPort",
        str(options.radarr_port),
        "-SonarrPort",
        str(options.sonarr_port),
        "-LidarrPort",
        str(options.lidarr_port),
        "-WhisparrPort",
        str(options.whisparr_port),
        "-P2PBindInterface",
        options.p2p_bind_interface,
    ]
    suite_config = options.install_root / "manifests" / "suite-config.json"
    if suite_config.is_file():
        command.extend(("-ConfigFile", str(suite_config)))
    if options.lan_bind_address:
        command.extend(("-ControlBindAddress", options.lan_bind_address))
    if options.emulebb_lan_bind_address:
        command.extend(("-EmulebbBindAddress", options.emulebb_lan_bind_address))
    if options.dependency_manifest:
        command.extend(("-DependencyManifest", str(options.dependency_manifest)))
    if options.import_profile_dir:
        command.extend(("-ImportProfileDir", str(options.import_profile_dir)))
    if options.emulebb_pdb_path:
        command.extend(("-EmulebbPdbPath", str(options.emulebb_pdb_path)))
    return SuiteInstallerInvocation(
        command=tuple(command),
        installer_script=options.installer_script,
        staging_root=options.installer_script.parent,
    )


def _timestamp_for_path() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _assert_under_root(path: Path, root: Path, description: str) -> None:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    if resolved_path == resolved_root or not _is_relative_to(resolved_path, resolved_root):
        raise RuntimeError(f"{description} must stay under {resolved_root}: {resolved_path}")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
