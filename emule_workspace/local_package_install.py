"""Local package-style install orchestration for operator live-wire profiles."""

from __future__ import annotations

import json
import os
import shutil
import socket
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import suite_installer
from .build import APP_EXE_NAME, app_pdb_name_for_exe, package_app_exe_name
from .config import LocalPackageInstallOptions, ReleasePackageOptions, WorkspaceOptions
from .layout import WorkspaceLayout, file_token
from .release import (
    EMULEBB_PACKAGE_ROOT_NAME,
    EMULEBB_RELEASE_ASSET_ROOT_NAME,
    EMULEBB_RUNTIME_SCRIPT_PATHS,
    create_release_package,
)

LIVE_WIRE_SCHEMA = "emulebb-build-tests.live-wire-inputs.v1"
LEGACY_LIVE_WIRE_SCHEMAS = ("emule-build-tests.live-wire-inputs.v1",)
LOCAL_INSTALL_KEY = "local_package_install"
INSTALL_MANIFEST_SCHEMA = "emulebb.local-package-install.v1"
TEST_INSTALLS_DIR_NAME = "test-installs"
TEST_PROFILE_SEED_DIR_NAME = "harness-profile-seed"
HARNESS_PROFILE_SEED_FILES = frozenset({"preferences.ini", "preferences.dat", "server.met", "nodes.dat"})
DEFAULT_REST_PORT = 4711
DEFAULT_PROWLARR_PORT = 9696
DEFAULT_RADARR_PORT = 7878
DEFAULT_SONARR_PORT = 8989
DEFAULT_LIDARR_PORT = 8686
DEFAULT_WHISPARR_PORT = 6969
DEFAULT_P2P_BIND_INTERFACE = "hide.me"
RETIRED_LOCAL_INSTALL_FIELDS = ("profile_dir", "procdump_path")


@dataclass(frozen=True)
class LocalInstallConfig:
    """Resolved local install settings loaded from ignored live-wire JSON."""

    live_wire_inputs_file: Path
    target_path: Path
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
    p2p_bind_interface: str
    enable_rest: bool
    enable_crash_dumps: bool
    rest_use_ssl_override: bool | None


@dataclass(frozen=True)
class ProfileRestConfig:
    """REST configuration read from the selected eMuleBB profile."""

    api_key: str
    host: str
    port: int
    use_ssl: bool


@dataclass(frozen=True)
class InstallArtifacts:
    """Release artifact paths consumed by the local installer."""

    release_root: Path
    emule_zip: Path
    emule_manifest: Path
    emule_sbom: Path
    package_exe: Path
    package_pdb: Path
    arch: str
    executable_name: str = APP_EXE_NAME
    package_flavor: str = "standard"
    installer_script: Path | None = None


@dataclass(frozen=True)
class MaterializedLocalInstall:
    """Resolved paths for an installer-created local suite install."""

    target_path: Path
    app_root: Path
    app_exe: Path
    profile_dir: Path
    profile_config_dir: Path
    profile_seed_config_dir: Path
    manifest_path: Path


@dataclass(frozen=True)
class DecodedText:
    """Text decoded from disk with the encoding needed for round-trip writes."""

    text: str
    encoding: str


def install_local_package(
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    options: LocalPackageInstallOptions,
) -> None:
    """Builds and deploys a local package install from ignored live-wire inputs."""

    materialize_local_install(layout, workspace_options, options)


def materialize_local_install(
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    options: LocalPackageInstallOptions,
) -> MaterializedLocalInstall:
    """Materializes a local package install through the suite installer."""

    config = load_local_install_config(layout, options.live_wire_inputs_file)
    return _materialize_local_install_from_config(layout, workspace_options, options, config)


def materialize_test_local_install(
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    options: LocalPackageInstallOptions,
    *,
    run_id: str,
    suite_name: str,
    client_id: str = "primary",
    lan_bind_address: str | None = None,
) -> MaterializedLocalInstall:
    """Materializes an isolated installer-created local install for one test client."""

    base_config = load_local_install_config(layout, options.live_wire_inputs_file)
    lan_bind = (lan_bind_address or "").strip() or "127.0.0.1"
    emulebb_port, prowlarr_port, radarr_port, sonarr_port, lidarr_port, whisparr_port = choose_free_tcp_ports(
        6, host=lan_bind
    )
    test_config = replace(
        base_config,
        target_path=test_install_root(layout, run_id=run_id, suite_name=suite_name, client_id=client_id),
        lan_bind_address=lan_bind,
        emulebb_lan_bind_address=lan_bind,
        emulebb_port=emulebb_port,
        prowlarr_port=prowlarr_port,
        radarr_port=radarr_port,
        sonarr_port=sonarr_port,
        lidarr_port=lidarr_port,
        whisparr_port=whisparr_port,
    )
    materialized = _materialize_local_install_from_config(layout, workspace_options, options, test_config)
    seed_config_dir = prepare_test_profile_seed(layout, materialized.profile_config_dir, test_config.target_path)
    return replace(materialized, profile_seed_config_dir=seed_config_dir)


def _materialize_local_install_from_config(
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    options: LocalPackageInstallOptions,
    config: LocalInstallConfig,
) -> MaterializedLocalInstall:
    """Materializes a local install from an already resolved config object."""

    if workspace_options.configuration != "Release":
        raise RuntimeError("install local package requires --config Release.")
    if not options.skip_build:
        create_release_package(
            layout,
            workspace_options,
            ReleasePackageOptions(release_version=options.release_version, clean=options.clean),
        )

    artifacts = resolve_install_artifacts(layout, workspace_options, options.release_version, package_flavor=options.package_flavor)
    installer_path = suite_installer.extract_packaged_installer(
        package_zip=artifacts.emule_zip,
        install_root=config.target_path,
        release_version=options.release_version,
    )
    artifacts = replace(artifacts, installer_script=installer_path)
    suite_installer.invoke_suite_installer(build_suite_installer_options(config, artifacts, options.release_version))
    profile_dir = suite_profile_dir(config)
    rest_config = prepare_profile_preferences(config, profile_dir)
    deploy_local_install(layout, config, rest_config, artifacts, options.release_version)
    return materialized_local_install_from_config(config, executable_name=artifacts.executable_name)


def load_local_install_config(layout: WorkspaceLayout, raw_inputs_path: str | None) -> LocalInstallConfig:
    """Loads local package install settings from the ignored live-wire JSON."""

    inputs_path = resolve_live_wire_inputs_path(layout, raw_inputs_path)
    payload = _load_json_object(inputs_path, "live-wire inputs file")
    schema = payload.get("schema")
    if schema not in (LIVE_WIRE_SCHEMA, *LEGACY_LIVE_WIRE_SCHEMAS):
        raise RuntimeError(f"Live-wire inputs schema must be {LIVE_WIRE_SCHEMA!r}.")
    raw_config = payload.get(LOCAL_INSTALL_KEY)
    if not isinstance(raw_config, dict):
        raise RuntimeError(f"Live-wire inputs field {LOCAL_INSTALL_KEY!r} must be an object.")
    retired_fields = [field for field in RETIRED_LOCAL_INSTALL_FIELDS if field in raw_config]
    if retired_fields:
        raise RuntimeError(
            "Local package install no longer accepts retired field(s): "
            + ", ".join(repr(field) for field in retired_fields)
            + ". The suite installer owns the profile and crash dump setup."
        )

    target_path = _required_path(raw_config, "target_path")
    rest_host = _optional_nullable_string(raw_config, "rest_host")
    emulebb_lan_bind_address = _optional_nullable_string(raw_config, "emulebb_lan_bind_address") or rest_host
    rest_port = _optional_nullable_int(raw_config, "rest_port")
    emulebb_port = _optional_nullable_int(raw_config, "emulebb_port") or rest_port or DEFAULT_REST_PORT

    return LocalInstallConfig(
        live_wire_inputs_file=inputs_path,
        target_path=target_path,
        lan_bind_address=_optional_nullable_string(raw_config, "lan_bind_address"),
        emulebb_lan_bind_address=emulebb_lan_bind_address,
        emulebb_port=emulebb_port,
        prowlarr_port=_optional_int(raw_config, "prowlarr_port", DEFAULT_PROWLARR_PORT),
        radarr_port=_optional_int(raw_config, "radarr_port", DEFAULT_RADARR_PORT),
        sonarr_port=_optional_int(raw_config, "sonarr_port", DEFAULT_SONARR_PORT),
        lidarr_port=_optional_int(raw_config, "lidarr_port", DEFAULT_LIDARR_PORT),
        whisparr_port=_optional_int(raw_config, "whisparr_port", DEFAULT_WHISPARR_PORT),
        dependency_manifest=_optional_nullable_path(raw_config, "dependency_manifest"),
        import_profile_dir=_optional_nullable_path(raw_config, "import_profile_dir"),
        p2p_bind_interface=_optional_string(raw_config, "p2p_bind_interface", DEFAULT_P2P_BIND_INTERFACE),
        enable_rest=_optional_bool(raw_config, "enable_rest", True),
        enable_crash_dumps=_optional_bool(raw_config, "enable_crash_dumps", True),
        rest_use_ssl_override=_optional_nullable_bool(raw_config, "rest_use_ssl"),
    )


def resolve_live_wire_inputs_path(layout: WorkspaceLayout, raw_inputs_path: str | None) -> Path:
    """Resolves the operator live-wire JSON path."""

    if not raw_inputs_path:
        return (layout.tests_repo_root / "live-wire-inputs.local.json").resolve()
    candidate = Path(raw_inputs_path)
    if candidate.is_absolute():
        return candidate.resolve()
    workspace_relative = (layout.emule_workspace_root / candidate).resolve()
    if workspace_relative.exists():
        return workspace_relative
    return (layout.tests_repo_root / candidate).resolve()


def resolve_install_artifacts(
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    release_version: str,
    *,
    package_flavor: str = "standard",
) -> InstallArtifacts:
    """Returns the release artifacts and private symbols used by the installer."""

    arch = "arm64" if workspace_options.platform == "ARM64" else "x64"
    release_root = layout.output_release_root / f"emulebb-v{release_version}"
    if package_flavor not in {"standard", "diagnostics"}:
        raise RuntimeError(f"Unsupported eMuleBB package flavor: {package_flavor}")
    flavor_asset_suffix = "" if package_flavor == "standard" else f"-{package_flavor}"
    executable_name = package_app_exe_name(package_flavor)
    pdb_name = app_pdb_name_for_exe(executable_name)
    package_build_root = (
        layout.output_packages_root / "build" / f"emulebb-v{release_version}" / arch / package_flavor / "app"
    )
    artifacts = InstallArtifacts(
        release_root=release_root,
        emule_zip=release_root / f"emulebb-{release_version}{flavor_asset_suffix}-{arch}.zip",
        emule_manifest=release_root / f"emulebb-{release_version}{flavor_asset_suffix}-{arch}.manifest.json",
        emule_sbom=release_root / f"emulebb-{release_version}{flavor_asset_suffix}-{arch}.sbom.spdx.json",
        package_exe=package_build_root / executable_name,
        package_pdb=package_build_root / pdb_name,
        arch=arch,
        executable_name=executable_name,
        package_flavor=package_flavor,
    )
    missing = [
        path
        for path in (
            artifacts.emule_zip,
            artifacts.emule_manifest,
            artifacts.emule_sbom,
            artifacts.package_exe,
            artifacts.package_pdb,
        )
        if not path.is_file()
    ]
    if missing:
        raise RuntimeError("Local package install is missing required artifacts:\n" + "\n".join(str(path) for path in missing))
    validate_packaged_runtime_scripts(layout, artifacts.emule_zip)
    return artifacts


def validate_packaged_runtime_scripts(layout: WorkspaceLayout, package_zip: Path) -> None:
    """Fails when a skip-build package contains stale runtime PowerShell assets."""

    source_root = layout.build_repo_root / "emule_workspace" / "release_assets" / EMULEBB_RELEASE_ASSET_ROOT_NAME
    if not source_root.is_dir():
        return
    with zipfile.ZipFile(package_zip, "r") as archive:
        for relative_path in EMULEBB_RUNTIME_SCRIPT_PATHS:
            source_path = source_root / relative_path
            if not source_path.is_file():
                continue
            package_entry_name = f"{EMULEBB_PACKAGE_ROOT_NAME}/{relative_path}".replace("\\", "/")
            try:
                package_payload = archive.read(package_entry_name)
            except KeyError as exc:
                raise RuntimeError(f"Release package is missing runtime asset {package_entry_name}. Rebuild the package.") from exc
            source_payload = source_path.read_bytes()
            if _sha256_bytes(package_payload) != _sha256_bytes(source_payload):
                raise RuntimeError(
                    "Release package contains a stale runtime asset. Rebuild the package before using "
                    f"--materialize-test-install-skip-build:\n{package_entry_name}\n{source_path}"
                )


def suite_profile_dir(config: LocalInstallConfig) -> Path:
    """Returns the installer-owned eMuleBB profile path."""

    return config.target_path / "profiles" / "emulebb"


def materialized_local_install_from_config(
    config: LocalInstallConfig,
    *,
    executable_name: str = APP_EXE_NAME,
    profile_seed_config_dir: Path | None = None,
) -> MaterializedLocalInstall:
    """Builds typed metadata for the installer-owned local install layout."""

    target_path = config.target_path
    app_root = target_path / "apps" / EMULEBB_PACKAGE_ROOT_NAME
    profile_dir = suite_profile_dir(config)
    profile_config_dir = profile_dir / "config"
    return MaterializedLocalInstall(
        target_path=target_path,
        app_root=app_root,
        app_exe=app_root / executable_name,
        profile_dir=profile_dir,
        profile_config_dir=profile_config_dir,
        profile_seed_config_dir=profile_seed_config_dir or profile_config_dir,
        manifest_path=target_path / "manifests" / "local-install.json",
    )


def test_install_root(
    layout: WorkspaceLayout,
    *,
    run_id: str,
    suite_name: str,
    client_id: str = "primary",
) -> Path:
    """Returns the isolated suite install root for one parallel test client."""

    return (
        layout.output_tmp_root
        / TEST_INSTALLS_DIR_NAME
        / file_token(run_id)
        / file_token(suite_name)
        / file_token(client_id)
    )


def build_suite_installer_options(
    config: LocalInstallConfig,
    artifacts: InstallArtifacts,
    release_version: str,
) -> suite_installer.SuiteInstallerOptions:
    """Maps local install config into the suite installer command contract."""

    if artifacts.installer_script is None:
        raise RuntimeError("Suite installer path was not resolved.")
    return suite_installer.SuiteInstallerOptions(
        install_root=config.target_path,
        release_root=artifacts.release_root,
        installer_script=artifacts.installer_script,
        release_version=release_version,
        platform=artifacts.arch,
        lan_bind_address=config.lan_bind_address,
        emulebb_lan_bind_address=config.emulebb_lan_bind_address,
        emulebb_port=config.emulebb_port,
        prowlarr_port=config.prowlarr_port,
        radarr_port=config.radarr_port,
        sonarr_port=config.sonarr_port,
        lidarr_port=config.lidarr_port,
        whisparr_port=config.whisparr_port,
        dependency_manifest=config.dependency_manifest,
        import_profile_dir=config.import_profile_dir,
        emulebb_pdb_path=artifacts.package_pdb,
        emulebb_package_flavor=artifacts.package_flavor,
        p2p_bind_interface=config.p2p_bind_interface,
    )


def choose_free_tcp_ports(count: int, *, host: str = "127.0.0.1") -> tuple[int, ...]:
    """Reserves and returns one contiguous Dynamic/Private TCP port block."""

    bind_host = host.strip()
    if bind_host in {"", "0.0.0.0", "::", "[::]"}:
        raise ValueError("test install port probes must bind to one explicit interface")

    sockets: list[socket.socket] = []
    try:
        for base_port in range(49152, 65535 - count + 2):
            block_sockets: list[socket.socket] = []
            try:
                for port in range(base_port, base_port + count):
                    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    probe.bind((bind_host, port))
                    block_sockets.append(probe)
                sockets.extend(block_sockets)
                return tuple(range(base_port, base_port + count))
            except OSError:
                for probe in block_sockets:
                    probe.close()
        raise RuntimeError(f"Could not reserve a contiguous free TCP port block of {count} ports on {bind_host}.")
    finally:
        for probe in sockets:
            probe.close()


def prepare_test_profile_seed(layout: WorkspaceLayout, installer_config_dir: Path, install_root: Path) -> Path:
    """Builds a curated harness seed for deterministic live suites."""

    if not installer_config_dir.is_dir():
        raise RuntimeError(f"Installer profile config directory is missing: {installer_config_dir}")
    fallback_seed_dir = layout.tests_repo_root / "manifests" / "live-profile-seed" / "config"
    if not fallback_seed_dir.is_dir():
        raise RuntimeError(f"Harness fallback profile seed directory is missing: {fallback_seed_dir}")
    seed_config_dir = install_root / TEST_PROFILE_SEED_DIR_NAME / "config"
    if seed_config_dir.exists():
        shutil.rmtree(seed_config_dir)
    seed_config_dir.mkdir(parents=True, exist_ok=True)

    for file_name in sorted(HARNESS_PROFILE_SEED_FILES):
        source = fallback_seed_dir / file_name if file_name == "preferences.ini" else installer_config_dir / file_name
        if not source.is_file():
            source = fallback_seed_dir / file_name
        if not source.is_file():
            raise RuntimeError(
                "Installer-backed test profile seed is missing required file "
                f"{file_name!r} from {installer_config_dir} and fallback {fallback_seed_dir}."
            )
        shutil.copy2(source, seed_config_dir / file_name)

    return seed_config_dir


def prepare_profile_preferences(config: LocalInstallConfig, profile_dir: Path) -> ProfileRestConfig:
    """Ensures required profile preferences and returns REST connection settings."""

    profile_config_dir = profile_dir / "config"
    preferences_path = profile_config_dir / "preferences.ini"
    if not preferences_path.is_file():
        raise RuntimeError(f"Profile preferences.ini is missing: {preferences_path}")

    decoded_preferences = read_text_preserving_encoding(preferences_path)
    values = parse_ini_values(decoded_preferences.text)
    webserver = values.get("webserver", {})
    emule = values.get("emule", {})
    api_key = webserver.get("apikey", "").strip()
    if not api_key:
        raise RuntimeError(f"Profile WebServer ApiKey is missing: {preferences_path}")

    host = webserver.get("bindaddr", "").strip() or "127.0.0.1"
    port = _parse_int(webserver.get("port", ""), DEFAULT_REST_PORT)
    use_ssl = config.rest_use_ssl_override if config.rest_use_ssl_override is not None else _parse_bool(webserver.get("usehttps", ""), False)

    updates: list[tuple[str, str, str]] = []
    if config.enable_rest and not _parse_bool(webserver.get("enabled", ""), False):
        updates.append(("WebServer", "Enabled", "1"))
    if config.enable_crash_dumps and emule.get("createcrashdump", "") != "2":
        updates.append(("eMule", "CreateCrashDump", "2"))

    if updates:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = preferences_path.with_name(f"{preferences_path.name}.local-install-{timestamp}.bak")
        shutil.copy2(preferences_path, backup_path)
        updated_text = update_ini_values(decoded_preferences.text, updates)
        preferences_path.write_text(updated_text, encoding=decoded_preferences.encoding, newline="")
        print(f"Updated profile preferences: {preferences_path}")
        print(f"Profile preferences backup: {backup_path}")

    return ProfileRestConfig(api_key=api_key, host=host, port=port, use_ssl=use_ssl)


def deploy_local_install(
    layout: WorkspaceLayout,
    config: LocalInstallConfig,
    rest_config: ProfileRestConfig,
    artifacts: InstallArtifacts,
    release_version: str,
) -> None:
    """Adds Python-owned manifests to the installer-created suite."""

    target_root = config.target_path
    symbols_dir = target_root / "symbols" / f"emulebb-v{release_version}" / artifacts.arch
    manifests_dir = target_root / "manifests" / f"emulebb-v{release_version}"
    diagnostics_dir = target_root / "diagnostics"
    for path in (symbols_dir, manifests_dir, diagnostics_dir):
        path.mkdir(parents=True, exist_ok=True)
    deployed_exe = target_root / "apps" / EMULEBB_PACKAGE_ROOT_NAME / artifacts.executable_name
    if _sha256(deployed_exe) != _sha256(artifacts.package_exe):
        raise RuntimeError(
            f"Local package install extracted {artifacts.executable_name} that does not match the package-build executable "
            f"used for symbols:\n{deployed_exe}\n{artifacts.package_exe}"
        )

    pdb_name = app_pdb_name_for_exe(artifacts.executable_name)
    adjacent_pdb = target_root / "apps" / EMULEBB_PACKAGE_ROOT_NAME / pdb_name
    versioned_pdb = symbols_dir / pdb_name
    if not adjacent_pdb.is_file() or not versioned_pdb.is_file():
        raise RuntimeError(
            "The suite installer did not deploy required eMuleBB debug symbols beside the executable "
            f"and in the versioned symbols directory:\n{adjacent_pdb}\n{versioned_pdb}"
        )
    if _sha256(adjacent_pdb) != _sha256(artifacts.package_pdb) or _sha256(versioned_pdb) != _sha256(artifacts.package_pdb):
        raise RuntimeError("The suite installer deployed eMuleBB debug symbols that do not match package-build emulebb.pdb.")
    for manifest in (artifacts.emule_manifest, artifacts.emule_sbom):
        shutil.copy2(manifest, manifests_dir / manifest.name)

    write_install_manifest(layout, config, rest_config, artifacts, release_version)
    print(f"Local package install: {target_root}")
    print(f"Profile: {suite_profile_dir(config)}")
    print(f"Symbols: {symbols_dir}")
    print(f"Adjacent debug info: {adjacent_pdb}")


def write_install_manifest(
    layout: WorkspaceLayout,
    config: LocalInstallConfig,
    rest_config: ProfileRestConfig,
    artifacts: InstallArtifacts,
    release_version: str,
) -> None:
    """Writes a local manifest with hashes needed to match dumps to symbols."""

    target_root = config.target_path
    manifest_path = target_root / "manifests" / "local-install.json"
    emule_exe = target_root / "apps" / EMULEBB_PACKAGE_ROOT_NAME / artifacts.executable_name
    pdb_name = app_pdb_name_for_exe(artifacts.executable_name)
    pdb_path = target_root / "symbols" / f"emulebb-v{release_version}" / artifacts.arch / pdb_name
    adjacent_pdb_path = target_root / "apps" / EMULEBB_PACKAGE_ROOT_NAME / pdb_name
    suite_config_path = target_root / "manifests" / "suite-config.json"
    suite_install_path = target_root / "manifests" / "suite-install.json"
    payload = {
        "schema": INSTALL_MANIFEST_SCHEMA,
        "installedAtUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "releaseVersion": release_version,
        "platform": artifacts.arch,
        "packageFlavor": artifacts.package_flavor,
        "executableName": artifacts.executable_name,
        "installKind": suite_installer.LOCAL_INSTALL_KIND,
        "bundle": suite_installer.SUITE_BUNDLE,
        "workspaceRoot": str(layout.emule_workspace_root),
        "liveWireInputsFile": str(config.live_wire_inputs_file),
        "targetPath": str(target_root),
        "profileDir": str(suite_profile_dir(config)),
        "importProfileDir": str(config.import_profile_dir) if config.import_profile_dir else None,
        "rest": {
            "host": rest_config.host,
            "port": rest_config.port,
            "useSsl": rest_config.use_ssl,
            "apiKeyPresent": bool(rest_config.api_key),
        },
        "suite": {
            "config": _optional_file_record(suite_config_path),
            "installManifest": _optional_file_record(suite_install_path),
        },
        "artifacts": {
            "emuleZip": _file_record(artifacts.emule_zip),
            "emuleManifest": _file_record(artifacts.emule_manifest),
            "packageExe": _file_record(artifacts.package_exe),
            "packagePdb": _file_record(artifacts.package_pdb),
            "deployedExe": _file_record(emule_exe),
            "deployedPdb": _file_record(pdb_path),
            "deployedAdjacentPdb": _file_record(adjacent_pdb_path),
        },
    }
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")


def parse_ini_values(text: str) -> dict[str, dict[str, str]]:
    """Parses INI values into case-insensitive section/key dictionaries."""

    result: dict[str, dict[str, str]] = {}
    current_section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1].strip().lower()
            result.setdefault(current_section, {})
            continue
        if "=" in line and current_section:
            key, value = line.split("=", 1)
            result.setdefault(current_section, {})[key.strip().lower()] = value.strip()
    return result


def update_ini_values(text: str, updates: list[tuple[str, str, str]]) -> str:
    """Updates or appends INI keys while preserving unrelated lines."""

    pending = {(section.lower(), key.lower()): (section, key, value) for section, key, value in updates}
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    current_section = ""
    seen_sections: set[str] = set()

    for raw_line in lines:
        line_body = raw_line.rstrip("\r\n")
        newline = raw_line[len(line_body) :]
        stripped = line_body.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            _append_pending_for_section(output, pending, current_section, newline or "\n")
            current_section = stripped[1:-1].strip().lower()
            seen_sections.add(current_section)
            output.append(raw_line)
            continue
        if "=" in line_body and current_section:
            key, _value = line_body.split("=", 1)
            pending_key = (current_section, key.strip().lower())
            if pending_key in pending:
                _section, original_key, value = pending.pop(pending_key)
                line_newline = newline or "\n"
                output.append(f"{original_key}={value}{line_newline}")
                continue
        output.append(raw_line)

    final_newline = "\n"
    if lines:
        last = lines[-1]
        line_body = last.rstrip("\r\n")
        final_newline = last[len(line_body) :] or "\n"
    _append_pending_for_section(output, pending, current_section, final_newline)
    for section, key, value in list(pending.values()):
        if section.lower() not in seen_sections:
            if output and not output[-1].endswith(("\n", "\r")):
                output.append(final_newline)
            output.append(f"[{section}]{final_newline}")
            output.append(f"{key}={value}{final_newline}")
            pending.pop((section.lower(), key.lower()), None)
    return "".join(output)


def read_text_preserving_encoding(path: Path) -> DecodedText:
    """Reads text while preserving common Windows profile encodings for writes."""

    data = path.read_bytes()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return DecodedText(data.decode("utf-16"), "utf-16")
    if data.startswith(b"\xef\xbb\xbf"):
        return DecodedText(data.decode("utf-8-sig"), "utf-8-sig")
    try:
        return DecodedText(data.decode("utf-8"), "utf-8")
    except UnicodeDecodeError:
        return DecodedText(data.decode("mbcs"), "mbcs")


def _append_pending_for_section(
    output: list[str],
    pending: dict[tuple[str, str], tuple[str, str, str]],
    section: str,
    newline: str,
) -> None:
    for pending_key, (_section, key, value) in list(pending.items()):
        if pending_key[0] == section:
            output.append(f"{key}={value}{newline}")
            pending.pop(pending_key)


def _load_json_object(path: Path, description: str) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"{description} is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{description} is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{description} must contain one JSON object: {path}")
    return payload


def _required_path(payload: dict[str, Any], key: str) -> Path:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Local package install field {key!r} must be a non-empty path string.")
    return _expanded_path(value)


def _optional_nullable_path(payload: dict[str, Any], key: str) -> Path | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Local package install field {key!r} must be null or a non-empty path string.")
    return _expanded_path(value)


def _expanded_path(value: str) -> Path:
    return Path(os.path.expandvars(value.strip())).expanduser().resolve()


def _optional_string(payload: dict[str, Any], key: str, default: str) -> str:
    if key not in payload and default == "":
        return ""
    value = payload.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Local package install field {key!r} must be a non-empty string.")
    return value.strip()


def _optional_nullable_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Local package install field {key!r} must be null or a non-empty string.")
    return value.strip()


def _optional_int(payload: dict[str, Any], key: str, default: int) -> int:
    value = payload.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeError(f"Local package install field {key!r} must be a positive integer.")
    return value


def _optional_nullable_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeError(f"Local package install field {key!r} must be null or a positive integer.")
    return value


def _optional_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise RuntimeError(f"Local package install field {key!r} must be a boolean.")
    return value


def _optional_nullable_bool(payload: dict[str, Any], key: str) -> bool | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise RuntimeError(f"Local package install field {key!r} must be null or a boolean.")
    return value


def _parse_int(value: str, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _parse_bool(value: str, default: bool) -> bool:
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _file_record(path: Path) -> dict[str, object]:
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _optional_file_record(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    return _file_record(path)


def _sha256(path: Path) -> str:
    hasher = __import__("hashlib").sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return __import__("hashlib").sha256(payload).hexdigest()
