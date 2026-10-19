"""Hyper-V based Windows guest package-smoke orchestration."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence

from .artifact_names import utc_run_id
from .config import ACTIVE_EMULEBB_RELEASE_VERSION, ReleasePackageOptions, WorkspaceOptions
from .layout import WorkspaceLayout, file_token
from .release import create_release_package

VM_LAB_SCHEMA = "emulebb.windows-vm-lab.v1"
DEFAULT_CONFIG_FILE_NAME = "vm-lab.local.json"
EXAMPLE_CONFIG_FILE_NAME = "vm-lab.example.json"
DEFAULT_SWITCH_NAME = "emulebb-vm-private"
DEFAULT_PROVISIONING_SWITCH_NAME = "emulebb-vm-nat"
DEFAULT_PROVISIONING_NAT_PREFIX = "192.168.250.0/24"
DEFAULT_PROVISIONING_GATEWAY = "192.168.250.1"
DEFAULT_PROVISIONING_PREFIX_LENGTH = 24
DEFAULT_PROVISIONING_DNS = ("1.1.1.1", "8.8.8.8")
DEFAULT_PROVISIONING_GUEST_IPS = {"win10": "192.168.250.10", "win11": "192.168.250.11"}
DEFAULT_VPN_SWITCH_NAME = "emulebb-vm-external"
DEFAULT_CHECKPOINT_NAME = "emulebb-clean"
DEFAULT_GUEST_USERNAME = "emulebbtest"
DEFAULT_GUEST_PASSWORD_ENV = "EMULEBB_VM_TEST_PASSWORD"
DEFAULT_MEMORY_MB = 4096
DEFAULT_DISK_GB = 64
DEFAULT_PROCESSOR_COUNT = 2
WINDOWS_VM_SUITE_NAME = "windows-vm"
WINDOWS_VM_RESULT_FILE_NAME = "windows-vm-result.json"
WINDOWS_VM_SUMMARY_FILE_NAME = "windows-vm-summary.json"
VM_LAB_AUDIT_SCHEMA = "emulebb.windows-vm-lab-audit.v1"
SUPPORTED_TARGETS = ("win10", "win11")
LOCAL_SWARM_REST_OPENAPI_RELATIVE_PATH = Path("docs") / "rest" / "REST-API-OPENAPI.yaml"
LOCAL_SWARM_APP_SOURCE_RELATIVE_DIR = Path("app") / "emulebb-main" / "srchybrid"
LOCAL_SWARM_APP_SOURCE_FILES = (
    "WebServerJsonSeams.h",
    "WebServerQBitCompatSeams.h",
    "WebServerArrCompatSeams.h",
    "WebServerArrCompat.cpp",
)
PYTHON_INSTALLER_URL = "https://www.python.org/ftp/python/3.13.13/python-3.13.13-amd64.exe"
PYTHON_INSTALLER_SHA256 = "3c9c81d80f91c002ced86d645422d81432c68c7d9b6b0e974768ca2e449a4d00"
PYTHON_INSTALLER_FILE_NAME = "python-3.13.13-amd64.exe"
PYTHON_INSTALL_DIR = r"C:\Python313"
VM_GUEST_LIVE_PYTHON_PACKAGES = ("pywin32", "pywinauto", "jsonschema", "PyYAML", "playwright", "certifi")
VM_GUEST_PLAYWRIGHT_BROWSER = "chromium"
HIDE_ME_INSTALLER_URL = "https://hide.me/en/software/windowsv4/download"
HIDE_ME_INSTALLER_FILE_NAME = "hide-me-vpn-windows.exe"
HIDE_ME_INSTALL_DIR = r"C:\Program Files (x86)\hide.me VPN"
HIDE_ME_SETTINGS_PATH_ENV = "EMULEBB_VM_HIDE_ME_SETTINGS_PATH"
HIDE_ME_SIGNER_TOKENS = ("eVenture", "hide.me")
PWSH_RELEASE_API_URL = "https://api.github.com/repos/PowerShell/PowerShell/releases/latest"
PWSH_INSTALLER_FILE_NAME = "PowerShell-win-x64.msi"
PWSH_INSTALL_DIR = r"C:\Program Files\PowerShell\7"
PWSH_SIGNER_TOKENS = ("Microsoft Corporation",)
DOTNET_DESKTOP_RUNTIME_URL = "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/6.0.36/windowsdesktop-runtime-6.0.36-win-x64.exe"
DOTNET_DESKTOP_RUNTIME_FILE_NAME = "windowsdesktop-runtime-6.0.36-win-x64.exe"
DOTNET_DESKTOP_RUNTIME_DIR = r"C:\Program Files\dotnet\shared\Microsoft.WindowsDesktop.App\6.0.36"
DOTNET_DESKTOP_RUNTIME_X86_URL = "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/6.0.36/windowsdesktop-runtime-6.0.36-win-x86.exe"
DOTNET_DESKTOP_RUNTIME_X86_FILE_NAME = "windowsdesktop-runtime-6.0.36-win-x86.exe"
DOTNET_DESKTOP_RUNTIME_X86_DIR = r"C:\Program Files (x86)\dotnet\shared\Microsoft.WindowsDesktop.App\6.0.36"
DOTNET_SIGNER_TOKENS = ("Microsoft Corporation",)
VC_REDIST_X64_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
VC_REDIST_X64_FILE_NAME = "vc_redist.x64.exe"
VC_REDIST_X64_RUNTIME_DLL = r"C:\Windows\System32\MSVCP140.dll"
VC_REDIST_SIGNER_TOKENS = ("Microsoft Corporation",)


@dataclass(frozen=True)
class HyperVLabSettings:
    """Hyper-V resources used by the VM lab."""

    switch_name: str = DEFAULT_SWITCH_NAME
    provisioning_switch_name: str = DEFAULT_PROVISIONING_SWITCH_NAME
    provisioning_nat_prefix: str = DEFAULT_PROVISIONING_NAT_PREFIX
    provisioning_gateway: str = DEFAULT_PROVISIONING_GATEWAY
    provisioning_prefix_length: int = DEFAULT_PROVISIONING_PREFIX_LENGTH
    provisioning_dns: tuple[str, ...] = DEFAULT_PROVISIONING_DNS
    provisioning_guest_ips: dict[str, str] | None = None
    vpn_switch_name: str = DEFAULT_VPN_SWITCH_NAME
    checkpoint_name: str = DEFAULT_CHECKPOINT_NAME
    memory_mb: int = DEFAULT_MEMORY_MB
    disk_gb: int = DEFAULT_DISK_GB
    processor_count: int = DEFAULT_PROCESSOR_COUNT


@dataclass(frozen=True)
class GuestSettings:
    """Windows guest account settings."""

    username: str = DEFAULT_GUEST_USERNAME
    password_env: str = DEFAULT_GUEST_PASSWORD_ENV
    password: str = ""


@dataclass(frozen=True)
class VmTargetSettings:
    """One Windows VM target definition."""

    key: str
    vm_name: str
    iso_path: Path
    edition: str


@dataclass(frozen=True)
class VmLabConfig:
    """Resolved Windows VM lab configuration."""

    config_path: Path
    hyperv: HyperVLabSettings
    guest: GuestSettings
    targets: dict[str, VmTargetSettings]


@dataclass(frozen=True)
class VmPrepareOptions:
    """Options for preparing Windows Hyper-V lab images."""

    config_file: str | None = None
    matrix: tuple[str, ...] = SUPPORTED_TARGETS
    rebuild_images: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class VmManualOptions:
    """Options for launching an operator-owned Windows VM session."""

    config_file: str | None = None
    matrix: tuple[str, ...] = ("win10",)
    release_version: str = ACTIVE_EMULEBB_RELEASE_VERSION
    skip_build: bool = False
    install_root: str = r"C:\eMuleBBManual"
    start_app: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class WindowsVmTestOptions:
    """Options for running Windows VM package-smoke tests."""

    config_file: str | None = None
    matrix: tuple[str, ...] = SUPPORTED_TARGETS
    profile: str = "package-smoke"
    release_version: str = ACTIVE_EMULEBB_RELEASE_VERSION
    skip_build: bool = False
    keep_running: bool = False
    dry_run: bool = False
    fixture_size_bytes: int = 25 * 1024 * 1024
    swarm_tier: int = 1
    local_swarm_mode: str = "plan"


@dataclass(frozen=True)
class PowerShellResult:
    """Captured PowerShell command result."""

    command: list[str]
    stdout: str
    stderr: str
    returncode: int


class PowerShellRunner:
    """Runs host PowerShell snippets, with dry-run capture for tests and planning."""

    def __init__(self, *, cwd: Path, dry_run: bool = False) -> None:
        self.cwd = cwd
        self.dry_run = dry_run
        self.commands: list[str] = []

    def run(self, script: str, *, label: str, capture_json: bool = False) -> str:
        self.commands.append(script)
        if self.dry_run:
            return "null" if capture_json else ""
        executable = shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"
        script_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                suffix=".ps1",
                prefix="emulebb-vm-lab-",
                dir=str(self.cwd),
                encoding="utf-8",
                newline="\n",
                delete=False,
            ) as handle:
                handle.write(script)
                script_path = Path(handle.name)
            command = [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]
            completed = subprocess.run(
                command,
                cwd=str(self.cwd),
                env=_powershell_subprocess_env(executable),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        finally:
            if script_path is not None:
                script_path.unlink(missing_ok=True)
        if completed.returncode != 0:
            tail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"{label} failed with exit code {completed.returncode}.\n{tail}")
        return completed.stdout


def _powershell_subprocess_env(executable: str) -> dict[str, str]:
    """Builds an environment that keeps Windows PowerShell on Windows modules."""

    env = os.environ.copy()
    if Path(executable).name.lower() != "powershell.exe":
        return env
    module_path_key = next((key for key in env if key.lower() == "psmodulepath"), "PSModulePath")
    module_path = env.get(module_path_key)
    if not module_path:
        return env
    parts = [part for part in module_path.split(os.pathsep) if part]
    windows_parts = [part for part in parts if "windowspowershell" in part.lower()]
    other_parts = [part for part in parts if part not in windows_parts]
    if windows_parts:
        env[module_path_key] = os.pathsep.join(windows_parts + other_parts)
    return env


def parse_matrix(raw_matrix: str | Sequence[str] | None) -> tuple[str, ...]:
    """Parses a comma-separated Windows VM target matrix."""

    if raw_matrix is None:
        return SUPPORTED_TARGETS
    if isinstance(raw_matrix, str):
        parts = [part.strip() for part in raw_matrix.split(",")]
    else:
        parts = []
        for item in raw_matrix:
            parts.extend(part.strip() for part in str(item).split(","))
    matrix = tuple(part for part in parts if part)
    if not matrix:
        raise RuntimeError("Windows VM matrix must contain at least one target.")
    unknown = sorted(set(matrix) - set(SUPPORTED_TARGETS))
    if unknown:
        raise RuntimeError(f"Unsupported Windows VM matrix target(s): {', '.join(unknown)}.")
    return matrix


def load_windows_vm_profile_catalog(layout: WorkspaceLayout) -> ModuleType:
    """Loads the test-owned Windows VM profile catalog from emulebb-build-tests."""

    return _load_harness_module(
        layout,
        "windows_vm_profiles",
        description="Windows VM profile catalog",
        required=(
            "SUPPORTED_TEST_PROFILES",
            "WINDOWS_VM_PROFILE_BY_NAME",
            "LOCAL_ED2K_REQUIRED_TARGETS",
            "HIDEME_LIVE_REQUIRED_TARGETS",
            "build_windows_vm_profile_matrix",
        ),
    )


def build_windows_vm_profile_matrix(layout: WorkspaceLayout) -> dict[str, object]:
    """Returns the harness-owned Windows VM profile registry."""

    return load_windows_vm_profile_catalog(layout).build_windows_vm_profile_matrix()


def load_windows_vm_host_contracts(layout: WorkspaceLayout) -> ModuleType:
    """Loads host-side Windows VM contracts from emulebb-build-tests."""

    return _load_harness_module(
        layout,
        "windows_vm_host",
        description="Windows VM host harness module",
        required=(
            "load_guest_script",
            "guest_runner_path",
            "profile_helper_path",
            "build_local_ed2k_target_payloads",
            "build_hideme_live_target_payloads",
        ),
    )


def _load_harness_module(
    layout: WorkspaceLayout,
    module_name: str,
    *,
    description: str,
    required: tuple[str, ...],
) -> ModuleType:
    module_path = layout.tests_repo_root / "emule_test_harness" / f"{module_name}.py"
    if not module_path.is_file():
        raise RuntimeError(f"{description} is missing: {module_path}")
    spec = importlib.util.spec_from_file_location(f"emulebb_{module_name}", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {description}: {module_path}")
    module = importlib.util.module_from_spec(spec)
    added_repo_root = False
    repo_root = str(layout.tests_repo_root)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
        added_repo_root = True
    try:
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        if added_repo_root:
            try:
                sys.path.remove(repo_root)
            except ValueError:
                pass
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        raise RuntimeError(f"{description} is missing field(s): {', '.join(missing)}")
    return module


def load_vm_lab_config(layout: WorkspaceLayout, config_file: str | None = None) -> VmLabConfig:
    """Loads and validates the ignored local VM lab configuration."""

    config_path = _resolve_config_path(layout, config_file)
    payload = _load_json_object(config_path, "Windows VM lab config")
    if payload.get("schema") != VM_LAB_SCHEMA:
        raise RuntimeError(f"Windows VM lab config schema must be {VM_LAB_SCHEMA!r}: {config_path}")

    raw_hyperv = _optional_object(payload, "hyperv")
    raw_guest = _optional_object(payload, "guest")
    raw_targets = _required_object(payload, "targets")
    hyperv = HyperVLabSettings(
        switch_name=_optional_string(raw_hyperv, "switch_name", DEFAULT_SWITCH_NAME),
        provisioning_switch_name=_optional_string(
            raw_hyperv,
            "provisioning_switch_name",
            DEFAULT_PROVISIONING_SWITCH_NAME,
        ),
        provisioning_nat_prefix=_optional_string(
            raw_hyperv,
            "provisioning_nat_prefix",
            DEFAULT_PROVISIONING_NAT_PREFIX,
        ),
        provisioning_gateway=_optional_string(
            raw_hyperv,
            "provisioning_gateway",
            DEFAULT_PROVISIONING_GATEWAY,
        ),
        provisioning_prefix_length=_optional_positive_int(
            raw_hyperv,
            "provisioning_prefix_length",
            DEFAULT_PROVISIONING_PREFIX_LENGTH,
        ),
        provisioning_dns=_optional_string_tuple(
            raw_hyperv,
            "provisioning_dns",
            DEFAULT_PROVISIONING_DNS,
        ),
        provisioning_guest_ips=_optional_string_map(
            raw_hyperv,
            "provisioning_guest_ips",
            DEFAULT_PROVISIONING_GUEST_IPS,
        ),
        vpn_switch_name=_optional_string(raw_hyperv, "vpn_switch_name", DEFAULT_VPN_SWITCH_NAME),
        checkpoint_name=_optional_string(raw_hyperv, "checkpoint_name", DEFAULT_CHECKPOINT_NAME),
        memory_mb=_optional_positive_int(raw_hyperv, "memory_mb", DEFAULT_MEMORY_MB),
        disk_gb=_optional_positive_int(raw_hyperv, "disk_gb", DEFAULT_DISK_GB),
        processor_count=_optional_positive_int(raw_hyperv, "processor_count", DEFAULT_PROCESSOR_COUNT),
    )
    guest = GuestSettings(
        username=_optional_string(raw_guest, "username", DEFAULT_GUEST_USERNAME),
        password_env=_optional_string(raw_guest, "password_env", DEFAULT_GUEST_PASSWORD_ENV),
        password=_optional_string(raw_guest, "password", ""),
    )
    targets = {
        key: _parse_target(config_path, key, raw_targets.get(key))
        for key in SUPPORTED_TARGETS
        if key in raw_targets
    }
    missing = [key for key in SUPPORTED_TARGETS if key not in targets]
    if missing:
        raise RuntimeError(f"Windows VM lab config is missing target(s): {', '.join(missing)}.")
    return VmLabConfig(config_path=config_path, hyperv=hyperv, guest=guest, targets=targets)


def prepare_vm_lab(layout: WorkspaceLayout, options: VmPrepareOptions) -> dict[str, object]:
    """Prepares Hyper-V Windows guest images and clean checkpoints."""

    config = load_vm_lab_config(layout, options.config_file)
    matrix = parse_matrix(options.matrix)
    runner = PowerShellRunner(cwd=layout.emule_workspace_root, dry_run=options.dry_run)
    preflight_hyperv(config, runner=runner, require_password=not options.dry_run)
    image_root = _vm_image_root(layout)
    image_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for key in matrix:
        target = config.targets[key]
        rows.append(
            prepare_vm_target(
                layout,
                config,
                target,
                runner=runner,
                rebuild_images=options.rebuild_images,
            )
        )
    result = {
        "schema": "emulebb.windows-vm-prepare-result.v1",
        "status": "planned" if options.dry_run else "passed",
        "generatedAtUtc": _now_utc(),
        "configFile": str(config.config_path),
        "matrix": list(matrix),
        "dryRun": options.dry_run,
        "targets": rows,
        "commandCount": len(runner.commands),
    }
    print(json.dumps(result, indent=2))
    return result


def audit_vm_lab(
    layout: WorkspaceLayout,
    *,
    config_file: str | None = None,
    matrix: Sequence[str] = SUPPORTED_TARGETS,
    vm_name_pattern: str = "emulebb-*",
) -> dict[str, object]:
    """Audits local Hyper-V VM registrations for stale eMuleBB lab disks."""

    config = load_vm_lab_config(layout, config_file)
    target_keys = tuple(matrix)
    missing_targets = [key for key in target_keys if key not in config.targets]
    if missing_targets:
        raise RuntimeError(f"Windows VM lab config has no target(s): {', '.join(missing_targets)}")

    configured_vm_names = tuple(config.targets[key].vm_name for key in target_keys)
    records = _query_vm_lab_hyperv_disks(configured_vm_names=configured_vm_names, vm_name_pattern=vm_name_pattern)
    workspace_state_root = (layout.workspace_root / "state").resolve()
    findings: list[dict[str, object]] = []
    audited_records: list[dict[str, object]] = []
    for record in records:
        vm_name = str(record.get("vmName") or "")
        disk_path = Path(str(record.get("diskPath") or "")).resolve()
        disk_exists = bool(record.get("diskExists"))
        reasons: list[str] = []
        if _path_is_relative_to(disk_path, workspace_state_root):
            reasons.append("workspace-state-vhd")
        if not disk_exists:
            reasons.append("missing-vhd")
        audited_record = {
            "vmName": vm_name,
            "state": str(record.get("state") or ""),
            "vmPath": str(record.get("vmPath") or ""),
            "diskPath": str(disk_path),
            "diskExists": disk_exists,
            "reasons": reasons,
        }
        audited_records.append(audited_record)
        for reason in reasons:
            findings.append(
                {
                    "vmName": vm_name,
                    "diskPath": str(disk_path),
                    "reason": reason,
                }
            )

    result = {
        "schema": VM_LAB_AUDIT_SCHEMA,
        "status": "failed" if findings else "passed",
        "generatedAtUtc": _now_utc(),
        "configFile": str(config.config_path),
        "matrix": list(target_keys),
        "vmNamePattern": vm_name_pattern,
        "expectedImageRoot": str(_vm_image_root(layout)),
        "workspaceStateRoot": str(workspace_state_root),
        "records": audited_records,
        "findings": findings,
    }
    print_vm_lab_audit(result)
    return result


def print_vm_lab_audit(result: dict[str, object]) -> None:
    """Prints a compact VM lab audit summary."""

    if result.get("status") == "passed":
        print("Windows VM lab audit passed.")
    else:
        print("Windows VM lab audit failed.")
    print(f"Expected image root: {result.get('expectedImageRoot')}")
    findings = result.get("findings")
    if isinstance(findings, list):
        print(f"Findings: {len(findings)}")
        for finding in findings[:40]:
            if isinstance(finding, dict):
                print(f"  {finding.get('vmName')}: {finding.get('reason')} -> {finding.get('diskPath')}")
        if len(findings) > 40:
            print(f"  ... {len(findings) - 40} more")


def launch_manual_vm(
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    options: VmManualOptions,
) -> dict[str, object]:
    """Restores prepared VM(s), stages eMuleBB, and leaves them running for manual use."""

    if workspace_options.platform != "x64":
        raise RuntimeError("vm-lab manual supports x64 packages only in v1.")
    if workspace_options.configuration != "Release":
        raise RuntimeError("vm-lab manual requires --config Release.")
    config = load_vm_lab_config(layout, options.config_file)
    matrix = parse_matrix(options.matrix)
    runner = PowerShellRunner(cwd=layout.emule_workspace_root, dry_run=options.dry_run)
    preflight_hyperv(config, runner=runner, require_password=not options.dry_run)
    if not options.skip_build and not options.dry_run:
        create_release_package(
            layout,
            workspace_options,
            ReleasePackageOptions(release_version=options.release_version, clean=False, require_signing=False),
        )
    package_zip = _release_package_zip(layout, options.release_version, workspace_options.platform)
    if not options.dry_run and not package_zip.is_file():
        raise RuntimeError(f"Manual VM launch is missing release package: {package_zip}")
    run_id = utc_run_id()
    report_root = _vm_manual_report_root(layout)
    run_report_dir = report_root / run_id
    run_report_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for key in matrix:
        rows.append(
            launch_manual_vm_target(
                layout,
                config,
                config.targets[key],
                package_zip=package_zip,
                release_version=options.release_version,
                run_id=run_id,
                run_report_dir=run_report_dir,
                install_root=options.install_root,
                start_app=options.start_app,
                runner=runner,
            )
        )
    result = {
        "schema": "emulebb.windows-vm-manual-result.v1",
        "status": "planned" if options.dry_run else "passed",
        "generatedAtUtc": _now_utc(),
        "releaseVersion": options.release_version,
        "platform": workspace_options.platform,
        "configFile": str(config.config_path),
        "packageZip": str(package_zip),
        "matrix": list(matrix),
        "dryRun": options.dry_run,
        "installRoot": options.install_root,
        "startApp": options.start_app,
        "reportDir": str(run_report_dir),
        "targets": rows,
        "commandCount": len(runner.commands),
    }
    _write_json(run_report_dir / "manual-session-result.json", result)
    _refresh_latest(run_report_dir, report_root / "latest")
    print(json.dumps(result, indent=2))
    return result


def invoke_windows_vm_tests(
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    options: WindowsVmTestOptions,
) -> dict[str, object]:
    """Runs package-smoke tests inside clean Windows Hyper-V guests."""

    if workspace_options.platform != "x64":
        raise RuntimeError("test windows-vm supports x64 packages only in v1.")
    if workspace_options.configuration != "Release":
        raise RuntimeError("test windows-vm requires --config Release.")
    profile_catalog = load_windows_vm_profile_catalog(layout)
    supported_profiles = tuple(profile_catalog.SUPPORTED_TEST_PROFILES)
    if options.profile not in supported_profiles:
        raise RuntimeError(f"Unsupported Windows VM test profile: {options.profile!r}.")
    config = load_vm_lab_config(layout, options.config_file)
    matrix = parse_matrix(options.matrix)
    profile_spec = profile_catalog.WINDOWS_VM_PROFILE_BY_NAME[options.profile]
    if tuple(matrix) != profile_spec.required_targets:
        expected = ",".join(profile_spec.required_targets)
        raise RuntimeError(f"{options.profile} requires --matrix {expected}.")
    uses_local_swarm = bool(getattr(profile_spec, "uses_local_swarm", False))
    effective_dry_run = options.dry_run or (uses_local_swarm and options.local_swarm_mode == "plan")
    runner = PowerShellRunner(cwd=layout.emule_workspace_root, dry_run=effective_dry_run)
    preflight_hyperv(config, runner=runner, require_password=not effective_dry_run)
    if not options.skip_build and not effective_dry_run:
        create_release_package(
            layout,
            workspace_options,
            ReleasePackageOptions(release_version=options.release_version, clean=False, require_signing=False),
        )
    package_zip = _release_package_zip(layout, options.release_version, workspace_options.platform)
    if not effective_dry_run and not package_zip.is_file():
        raise RuntimeError(f"Windows VM package-smoke is missing release package: {package_zip}")
    local_swarm_release_asset_paths = (
        _local_swarm_release_asset_paths(layout, options.release_version, workspace_options.platform)
        if uses_local_swarm
        else ()
    )
    local_swarm_dependency_asset_paths = (
        _local_swarm_dependency_asset_paths(layout)
        if uses_local_swarm
        else ()
    )
    if not effective_dry_run and uses_local_swarm:
        missing_release_assets = [path for path in local_swarm_release_asset_paths if not path.is_file()]
        if missing_release_assets:
            formatted = ", ".join(str(path) for path in missing_release_assets)
            raise RuntimeError(f"Windows VM local-swarm profile is missing suite installer release asset(s): {formatted}")

    run_id = utc_run_id()
    report_root = _windows_vm_report_root(layout)
    run_report_dir = report_root / run_id
    run_report_dir.mkdir(parents=True, exist_ok=True)
    if options.profile == "hideme-live-wire":
        rows = run_windows_vm_hideme_live_wire(
            layout,
            config,
            package_zip=package_zip,
            run_id=run_id,
            run_report_dir=run_report_dir,
            keep_running=options.keep_running,
            runner=runner,
        )
    elif options.profile == "package-smoke":
        rows = []
        for key in matrix:
            rows.append(
                run_windows_vm_package_smoke(
                    layout,
                    config,
                    config.targets[key],
                    package_zip=package_zip,
                    run_id=run_id,
                    run_report_dir=run_report_dir,
                    keep_running=options.keep_running,
                    runner=runner,
                )
            )
    else:
        rows = []
        for key in matrix:
            rows.append(
                run_windows_vm_profile_smoke(
                    layout,
                    config,
                    config.targets[key],
                    profile=options.profile,
                    package_zip=package_zip,
                    local_swarm_release_asset_paths=local_swarm_release_asset_paths,
                    local_swarm_dependency_asset_paths=local_swarm_dependency_asset_paths,
                    release_version=options.release_version,
                    platform=workspace_options.platform,
                    run_id=run_id,
                    run_report_dir=run_report_dir,
                    keep_running=options.keep_running,
                    fixture_size_bytes=options.fixture_size_bytes,
                    swarm_tier=options.swarm_tier,
                    local_swarm_mode=options.local_swarm_mode,
                    runner=runner,
                )
            )
    status = "passed" if all(row.get("status") == "passed" for row in rows) else "failed"
    if effective_dry_run:
        status = "planned"
    campaign_scenario = _windows_vm_campaign_scenario_metadata(profile_spec)
    result = {
        "schema": "emulebb.windows-vm-result.v1",
        "status": status,
        "generatedAtUtc": _now_utc(),
        "profile": options.profile,
        "swarmTier": options.swarm_tier,
        "localSwarmMode": options.local_swarm_mode,
        "releaseVersion": options.release_version,
        "platform": workspace_options.platform,
        "configFile": str(config.config_path),
        "packageZip": str(package_zip),
        "packageSha256": _sha256(package_zip) if package_zip.is_file() else None,
        "matrix": list(matrix),
        "dryRun": effective_dry_run,
        "requestedDryRun": options.dry_run,
        "targets": rows,
        "commandCount": len(runner.commands),
    }
    if campaign_scenario:
        result["campaignScenario"] = campaign_scenario
    summary = {
        "schema": "emulebb.windows-vm-summary.v1",
        "status": status,
        "generatedAtUtc": result["generatedAtUtc"],
        "profile": options.profile,
        "swarmTier": options.swarm_tier,
        "localSwarmMode": options.local_swarm_mode,
        "matrix": list(matrix),
        "passed": [row["target"] for row in rows if row.get("status") == "passed"],
        "failed": [row["target"] for row in rows if row.get("status") not in {"passed", "planned"}],
        "planned": [row["target"] for row in rows if row.get("status") == "planned"],
    }
    if campaign_scenario:
        summary["campaignScenario"] = campaign_scenario
    _write_json(run_report_dir / WINDOWS_VM_RESULT_FILE_NAME, result)
    _write_json(run_report_dir / WINDOWS_VM_SUMMARY_FILE_NAME, summary)
    _refresh_latest(run_report_dir, report_root / "latest")
    print(json.dumps(summary, indent=2))
    if status == "failed":
        raise RuntimeError(f"Windows VM profile {options.profile} failed. See {run_report_dir}.")
    return result


def _windows_vm_campaign_scenario_metadata(profile_spec: object) -> dict[str, object]:
    """Returns reusable campaign scenario metadata for migrated VM profiles."""

    execution_modes = tuple(str(mode) for mode in getattr(profile_spec, "execution_modes", ("vm",)))
    local_suites = tuple(str(suite) for suite in getattr(profile_spec, "local_suites", ()))
    uses_local_swarm = bool(getattr(profile_spec, "uses_local_swarm", False))
    if "local" not in execution_modes and not local_suites and not uses_local_swarm:
        return {}
    return {
        "scenarioId": str(getattr(profile_spec, "scenario_id", "")),
        "vmProfile": str(getattr(profile_spec, "name", "")),
        "localProfile": str(getattr(profile_spec, "local_profile", "")),
        "localSuites": list(local_suites),
        "executionModes": list(execution_modes),
        "usesLocalSwarm": uses_local_swarm,
        "controlBindScope": str(getattr(profile_spec, "control_bind_scope", "")),
        "p2pMode": str(getattr(profile_spec, "p2p_mode", "")),
        "p2pBindScope": str(getattr(profile_spec, "p2p_bind_scope", "")),
    }


def preflight_hyperv(config: VmLabConfig, *, runner: PowerShellRunner, require_password: bool) -> None:
    """Verifies host prerequisites for Hyper-V VM automation."""

    if require_password and not resolve_guest_password(config):
        raise RuntimeError(f"Guest password environment variable is required: {config.guest.password_env}")
    script = _ps_with_payload(
        {
            "cmdlets": [
                "Get-VM",
                "New-VM",
                "Start-VM",
                "Stop-VM",
                "Checkpoint-VM",
                "Get-VMSnapshot",
                "Restore-VMSnapshot",
                "New-VMSwitch",
                "Get-VMSwitch",
                "Connect-VMNetworkAdapter",
                "New-VHD",
                "Mount-VHD",
                "Dismount-VHD",
                "Mount-DiskImage",
                "Dismount-DiskImage",
                "Get-WindowsImage",
                "Invoke-Command",
                "New-PSSession",
                "Copy-Item",
            ]
        },
        r"""
$ErrorActionPreference = 'Stop'
$principal = [Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw 'Windows VM lab requires an elevated PowerShell host.'
}
$missing = @()
foreach ($name in $payload.cmdlets) {
  if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
    $missing += $name
  }
}
if ($missing.Count -gt 0) {
  throw ('Missing Hyper-V/Windows imaging command(s): ' + ($missing -join ', '))
}
$service = Get-Service vmms -ErrorAction Stop
if ($service.Status -ne 'Running') {
  throw ('Hyper-V Virtual Machine Management service is not running: ' + $service.Status)
}
""",
    )
    runner.run(script, label="Windows VM Hyper-V preflight")


def prepare_vm_target(
    layout: WorkspaceLayout,
    config: VmLabConfig,
    target: VmTargetSettings,
    *,
    runner: PowerShellRunner,
    rebuild_images: bool,
) -> dict[str, object]:
    """Prepares one Hyper-V VM and its clean checkpoint."""

    vhd_path = _vm_image_root(layout) / f"{file_token(target.key)}.vhdx"
    baseline_report_path = _vm_prepare_report_root(layout) / f"{file_token(target.key)}-baseline.json"
    provisioning_guest_ips = config.hyperv.provisioning_guest_ips or DEFAULT_PROVISIONING_GUEST_IPS
    provisioning_guest_ip = provisioning_guest_ips.get(target.key)
    if not provisioning_guest_ip:
        raise RuntimeError(f"Windows VM lab config is missing provisioning guest IP for target: {target.key}")
    if runner.dry_run:
        python_installer = python_installer_cache_path(layout)
        hide_me_installer = hide_me_installer_cache_path(layout)
        hide_me_settings = default_hide_me_settings_path()
        pwsh_installer = pwsh_installer_cache_path(layout)
        dotnet_desktop_runtime = dotnet_desktop_runtime_cache_path(layout)
        dotnet_desktop_runtime_x86 = dotnet_desktop_runtime_x86_cache_path(layout)
        vc_redist_x64 = vc_redist_x64_cache_path(layout)
    else:
        python_installer = ensure_python_installer(layout)
        hide_me_installer = ensure_hide_me_installer(layout)
        hide_me_settings = resolve_hide_me_settings_path()
        pwsh_installer = ensure_pwsh_installer(layout)
        dotnet_desktop_runtime = ensure_dotnet_desktop_runtime_installer(layout)
        dotnet_desktop_runtime_x86 = ensure_dotnet_desktop_runtime_x86_installer(layout)
        vc_redist_x64 = ensure_vc_redist_x64_installer(layout)
    script = _ps_with_payload(
        {
            "target": target.key,
            "vmName": target.vm_name,
            "isoPath": str(target.iso_path),
            "edition": target.edition,
            "vhdPath": str(vhd_path),
            "switchName": config.hyperv.switch_name,
            "provisioningSwitchName": config.hyperv.provisioning_switch_name,
            "provisioningNatName": f"{config.hyperv.provisioning_switch_name}-nat",
            "provisioningNatPrefix": config.hyperv.provisioning_nat_prefix,
            "provisioningGateway": config.hyperv.provisioning_gateway,
            "provisioningPrefixLength": config.hyperv.provisioning_prefix_length,
            "provisioningDns": list(config.hyperv.provisioning_dns),
            "provisioningGuestIp": provisioning_guest_ip,
            "checkpointName": config.hyperv.checkpoint_name,
            "memoryBytes": config.hyperv.memory_mb * 1024 * 1024,
            "diskBytes": config.hyperv.disk_gb * 1024 * 1024 * 1024,
            "processorCount": config.hyperv.processor_count,
            "username": config.guest.username,
            "password": resolve_guest_password(config),
            "rebuildImages": rebuild_images,
            "pythonInstallerPath": str(python_installer),
            "pythonInstallerSha256": PYTHON_INSTALLER_SHA256,
            "pythonInstallDir": PYTHON_INSTALL_DIR,
            "pythonLivePackages": list(VM_GUEST_LIVE_PYTHON_PACKAGES),
            "playwrightBrowser": VM_GUEST_PLAYWRIGHT_BROWSER,
            "hideMeInstallerPath": str(hide_me_installer),
            "hideMeSettingsPath": str(hide_me_settings),
            "hideMeInstallDir": HIDE_ME_INSTALL_DIR,
            "pwshInstallerPath": str(pwsh_installer),
            "pwshInstallDir": PWSH_INSTALL_DIR,
            "dotnetDesktopRuntimePath": str(dotnet_desktop_runtime),
            "dotnetDesktopRuntimeDir": DOTNET_DESKTOP_RUNTIME_DIR,
            "dotnetDesktopRuntimeX86Path": str(dotnet_desktop_runtime_x86),
            "dotnetDesktopRuntimeX86Dir": DOTNET_DESKTOP_RUNTIME_X86_DIR,
            "vcRedistX64Path": str(vc_redist_x64),
            "vcRedistX64RuntimeDll": VC_REDIST_X64_RUNTIME_DLL,
            "baselineReportPath": str(baseline_report_path),
        },
        _prepare_vm_target_script(),
    )
    runner.run(script, label=f"prepare Windows VM {target.key}")
    return {
        "target": target.key,
        "vmName": target.vm_name,
        "status": "planned" if runner.dry_run else "passed",
        "vhdPath": str(vhd_path),
        "isoPath": str(target.iso_path),
        "edition": target.edition,
        "checkpointName": config.hyperv.checkpoint_name,
        "baselineReportPath": str(baseline_report_path),
    }


def launch_manual_vm_target(
    layout: WorkspaceLayout,
    config: VmLabConfig,
    target: VmTargetSettings,
    *,
    package_zip: Path,
    release_version: str,
    run_id: str,
    run_report_dir: Path,
    install_root: str,
    start_app: bool,
    runner: PowerShellRunner,
) -> dict[str, object]:
    """Launches one restored VM as an operator-owned manual session."""

    target_report_dir = run_report_dir / target.key
    target_report_dir.mkdir(parents=True, exist_ok=True)
    provisioning = _target_provisioning_payload(config, target.key)
    if runner.dry_run:
        return {
            "target": target.key,
            "vmName": target.vm_name,
            "status": "planned",
            "checkpointName": config.hyperv.checkpoint_name,
            "guestIp": provisioning["guestIp"],
            "installRoot": install_root,
            "reportDir": str(target_report_dir),
        }
    script = _ps_with_payload(
        {
            "target": target.key,
            "vmName": target.vm_name,
            "switchName": config.hyperv.provisioning_switch_name,
            "provisioning": provisioning,
            "checkpointName": config.hyperv.checkpoint_name,
            "username": config.guest.username,
            "password": resolve_guest_password(config),
            "packageZip": str(package_zip),
            "releaseVersion": release_version,
            "runId": run_id,
            "hostReportDir": str(target_report_dir),
            "installRoot": install_root,
            "startApp": start_app,
        },
        _manual_vm_target_script(),
    )
    stdout = runner.run(script, label=f"manual Windows VM {target.key}", capture_json=True)
    payload = _parse_json_output(stdout, f"manual Windows VM {target.key}")
    _write_json(target_report_dir / f"{target.key}-manual-session.json", payload)
    return {
        "target": target.key,
        "vmName": target.vm_name,
        "status": payload.get("status", "failed"),
        "checkpointName": config.hyperv.checkpoint_name,
        "guestIp": payload.get("guestIp", provisioning["guestIp"]),
        "installRoot": payload.get("installRoot", install_root),
        "appExe": payload.get("appExe"),
        "profileDir": payload.get("profileDir"),
        "desktopShortcut": payload.get("desktopShortcut"),
        "reportDir": str(target_report_dir),
    }


def run_windows_vm_package_smoke(
    layout: WorkspaceLayout,
    config: VmLabConfig,
    target: VmTargetSettings,
    *,
    package_zip: Path,
    run_id: str,
    run_report_dir: Path,
    keep_running: bool,
    runner: PowerShellRunner,
) -> dict[str, object]:
    """Runs the package-smoke profile inside one restored Windows guest."""

    target_report_dir = run_report_dir / target.key
    target_report_dir.mkdir(parents=True, exist_ok=True)
    if runner.dry_run:
        return {
            "target": target.key,
            "vmName": target.vm_name,
            "status": "planned",
            "checkpointName": config.hyperv.checkpoint_name,
            "reportDir": str(target_report_dir),
        }
    host_contracts = load_windows_vm_host_contracts(layout)
    provisioning_guest_ips = config.hyperv.provisioning_guest_ips or DEFAULT_PROVISIONING_GUEST_IPS
    lan_bind_addr = provisioning_guest_ips.get(target.key, "")
    script = _ps_with_payload(
        {
            "target": target.key,
            "vmName": target.vm_name,
            "switchName": config.hyperv.provisioning_switch_name,
            "provisioning": _target_provisioning_payload(config, target.key),
            "checkpointName": config.hyperv.checkpoint_name,
            "username": config.guest.username,
            "password": resolve_guest_password(config),
            "packageZip": str(package_zip),
            "runId": run_id,
            "hostReportDir": str(target_report_dir),
            "keepRunning": keep_running,
            "lanBindAddr": lan_bind_addr,
        },
        host_contracts.load_guest_script(layout.tests_repo_root, "package-smoke"),
    )
    stdout = runner.run(script, label=f"Windows VM package-smoke {target.key}", capture_json=True)
    payload = _parse_json_output(stdout, f"Windows VM package-smoke {target.key}")
    _write_json(target_report_dir / f"{target.key}-result.json", payload)
    return {
        "target": target.key,
        "vmName": target.vm_name,
        "status": payload.get("status", "failed"),
        "checkpointName": config.hyperv.checkpoint_name,
        "reportDir": str(target_report_dir),
        "guest": payload.get("guest", {}),
        "checks": payload.get("checks", []),
        "errors": payload.get("errors", []),
    }


def run_windows_vm_profile_smoke(
    layout: WorkspaceLayout,
    config: VmLabConfig,
    target: VmTargetSettings,
    *,
    profile: str,
    package_zip: Path,
    local_swarm_release_asset_paths: Sequence[Path],
    local_swarm_dependency_asset_paths: Sequence[Path],
    release_version: str,
    platform: str,
    run_id: str,
    run_report_dir: Path,
    keep_running: bool,
    fixture_size_bytes: int,
    swarm_tier: int,
    local_swarm_mode: str,
    runner: PowerShellRunner,
) -> dict[str, object]:
    """Runs one generic Python-backed profile smoke inside a restored Windows guest."""

    target_report_dir = run_report_dir / target.key
    target_report_dir.mkdir(parents=True, exist_ok=True)
    if runner.dry_run:
        return {
            "target": target.key,
            "vmName": target.vm_name,
            "status": "planned",
            "profile": profile,
            "swarmTier": swarm_tier,
            "localSwarmMode": local_swarm_mode,
            "checkpointName": config.hyperv.checkpoint_name,
            "reportDir": str(target_report_dir),
        }
    host_contracts = load_windows_vm_host_contracts(layout)
    is_local_swarm_profile = profile in set(host_contracts.LOCAL_SWARM_VM_PROFILES)
    local_swarm_harness_archive_path = _stage_local_swarm_harness_payload_archive(
        layout,
        run_report_dir,
        host_contracts,
    )
    local_swarm_client2_app_exe = local_swarm_tracing_harness_app_exe(layout) if is_local_swarm_profile else None
    local_swarm_amule_daemon_exe, local_swarm_amule_control_exe = (
        local_swarm_amule_exes(layout)
        if is_local_swarm_profile
        else (None, None)
    )
    local_swarm_rest_openapi_path = (
        layout.tooling_repo_root / LOCAL_SWARM_REST_OPENAPI_RELATIVE_PATH
        if is_local_swarm_profile
        else None
    )
    local_swarm_app_source_paths = (
        local_swarm_rest_source_contract_paths(layout)
        if is_local_swarm_profile
        else ()
    )
    local_swarm_dependency_manifest_entries = (
        _local_swarm_dependency_manifest_entries(layout)
        if is_local_swarm_profile
        else {}
    )
    provisioning_guest_ips = config.hyperv.provisioning_guest_ips or DEFAULT_PROVISIONING_GUEST_IPS
    local_swarm_lan_bind_addr = provisioning_guest_ips.get(target.key, "")
    script = _ps_with_payload(
        {
            "target": target.key,
            "vmName": target.vm_name,
            "profileName": profile,
            "switchName": config.hyperv.provisioning_switch_name,
            "provisioning": _target_provisioning_payload(config, target.key),
            "checkpointName": config.hyperv.checkpoint_name,
            "username": config.guest.username,
            "password": resolve_guest_password(config),
            "packageZip": str(package_zip),
            "runnerPath": str(host_contracts.guest_runner_path(layout.tests_repo_root, profile)),
            "profileHelperPath": str(host_contracts.profile_helper_path(layout.tests_repo_root)),
            "localSwarmHarnessArchivePath": (
                str(local_swarm_harness_archive_path)
                if local_swarm_harness_archive_path is not None
                else ""
            ),
            "localSwarmReleaseAssetPaths": [str(path) for path in local_swarm_release_asset_paths],
            "localSwarmDependencyAssetPaths": [str(path) for path in local_swarm_dependency_asset_paths],
            "localSwarmDependencyManifestEntries": local_swarm_dependency_manifest_entries,
            "releaseVersion": release_version,
            "platform": platform,
            "localSwarmRestOpenApiPath": (
                str(local_swarm_rest_openapi_path)
                if local_swarm_rest_openapi_path is not None
                else ""
            ),
            "localSwarmAppSourcePaths": [str(path) for path in local_swarm_app_source_paths],
            "localSwarmClient2AppExe": (
                str(local_swarm_client2_app_exe)
                if local_swarm_client2_app_exe is not None
                else ""
            ),
            "localSwarmAmuleDaemonExe": (
                str(local_swarm_amule_daemon_exe)
                if local_swarm_amule_daemon_exe is not None
                else ""
            ),
            "localSwarmAmuleControlExe": (
                str(local_swarm_amule_control_exe)
                if local_swarm_amule_control_exe is not None
                else ""
            ),
            "runId": run_id,
            "hostReportDir": str(target_report_dir),
            "keepRunning": keep_running,
            "fixtureSizeBytes": fixture_size_bytes,
            "swarmTier": swarm_tier,
            "localSwarmMode": local_swarm_mode,
            "lanBindAddr": local_swarm_lan_bind_addr,
            "localSwarmLanBindAddr": local_swarm_lan_bind_addr,
        },
        host_contracts.load_guest_script(layout.tests_repo_root, profile),
    )
    stdout = runner.run(script, label=f"Windows VM {profile} {target.key}", capture_json=True)
    payload = _parse_json_output(stdout, f"Windows VM {profile} {target.key}")
    _write_json(target_report_dir / f"{target.key}-result.json", payload)
    return {
        "target": target.key,
        "vmName": target.vm_name,
        "status": payload.get("status", "failed"),
        "profile": profile,
        "swarmTier": swarm_tier,
        "localSwarmMode": local_swarm_mode,
        "checkpointName": config.hyperv.checkpoint_name,
        "reportDir": str(target_report_dir),
        "guest": payload.get("guest", {}),
        "checks": payload.get("checks", []),
        "errors": payload.get("errors", []),
    }


def local_swarm_tracing_harness_app_exe(layout: WorkspaceLayout) -> Path | None:
    """Returns the built tracing-harness executable staged into reusable VM swarm payloads."""

    root = layout.workspace_root / "app" / "emulebb-community-tracing-harness" / "srchybrid" / "x64" / "Release"
    for name in ("emule.exe", "emulebb.exe"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def local_swarm_rest_source_contract_paths(layout: WorkspaceLayout) -> tuple[Path, ...]:
    """Returns native REST contract source files staged into reusable VM swarm payloads."""

    source_root = layout.workspace_root / LOCAL_SWARM_APP_SOURCE_RELATIVE_DIR
    return tuple(source_root / file_name for file_name in LOCAL_SWARM_APP_SOURCE_FILES)


def local_swarm_amule_exes(layout: WorkspaceLayout) -> tuple[Path | None, Path | None]:
    """Returns the staged aMule daemon/control binaries for reusable VM swarm payloads."""

    root = layout.output_tools_root / "amule" / "bin"
    daemon = root / "amuled.exe"
    control = root / "amulecmd.exe"
    return (
        daemon if daemon.is_file() else None,
        control if control.is_file() else None,
    )


def _target_provisioning_payload(config: VmLabConfig, target_key: str) -> dict[str, object]:
    provisioning_guest_ips = config.hyperv.provisioning_guest_ips or DEFAULT_PROVISIONING_GUEST_IPS
    guest_ip = provisioning_guest_ips.get(target_key, "")
    if not guest_ip:
        raise RuntimeError(f"Windows VM lab config is missing provisioning guest IP for target: {target_key}")
    return {
        "guestIp": guest_ip,
        "prefixLength": config.hyperv.provisioning_prefix_length,
        "gateway": config.hyperv.provisioning_gateway,
        "dns": list(config.hyperv.provisioning_dns),
    }


def _with_target_provisioning_payloads(
    config: VmLabConfig,
    payloads: dict[str, object],
) -> dict[str, object]:
    updated = dict(payloads)
    for target_key in ("win10", "win11"):
        payload = updated.get(target_key)
        if isinstance(payload, dict):
            updated[target_key] = {
                **payload,
                "provisioning": _target_provisioning_payload(config, target_key),
            }
    return updated


def _stage_local_swarm_harness_payload_archive(
    layout: WorkspaceLayout,
    run_report_dir: Path,
    host_contracts: ModuleType,
) -> Path:
    """Packages the curated local-swarm harness files copied into clean VM guests."""

    payload = host_contracts.local_swarm_payload_paths(layout.tests_repo_root)
    archive_path = run_report_dir / "local-swarm-harness-payload.zip"
    archive_path.unlink(missing_ok=True)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_payload_tree(
            archive,
            Path(payload["harnessPackage"]),
            Path("emule_test_harness"),
        )
        manifests_path = Path(payload["manifests"])
        if manifests_path.is_dir():
            _write_payload_tree(archive, manifests_path, Path("manifests"))
        for script_path in payload["scripts"]:
            script = Path(script_path)
            if not script.is_file():
                raise RuntimeError(f"Windows VM local-swarm payload script is missing: {script}")
            _write_payload_file(archive, script, Path("scripts") / script.name)
        local_swarm_inputs_path = host_contracts.write_local_swarm_payload_inputs(
            layout.tests_repo_root,
            run_report_dir / "local-swarm-live-wire-inputs.generated.json",
        )
        archive_inputs_name = str(payload.get("archiveLiveWireInputsName") or "live-wire-inputs.local.json")
        _write_payload_file(archive, Path(local_swarm_inputs_path), Path(archive_inputs_name))
    return archive_path


def _write_payload_tree(archive: zipfile.ZipFile, source_root: Path, archive_root: Path) -> None:
    if not source_root.is_dir():
        raise RuntimeError(f"Windows VM local-swarm payload root is missing: {source_root}")
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(source_root)
        if _skip_payload_file(relative_path):
            continue
        _write_payload_file(archive, path, archive_root / relative_path)


def _write_payload_file(archive: zipfile.ZipFile, source_path: Path, archive_path: Path) -> None:
    _assert_local_swarm_payload_archive_path(archive_path)
    archive.write(source_path, archive_path.as_posix())


def _skip_payload_file(relative_path: Path) -> bool:
    parts = {part.casefold() for part in relative_path.parts}
    return "__pycache__" in parts or relative_path.suffix.casefold() in {".pyc", ".pyo"}


def _assert_local_swarm_payload_archive_path(archive_path: Path) -> None:
    raw = archive_path.as_posix()
    parts = {part.casefold() for part in archive_path.parts}
    if archive_path.is_absolute() or ".." in archive_path.parts or raw.startswith("/"):
        raise RuntimeError(f"Windows VM local-swarm payload path escapes archive root: {raw}")
    forbidden = {".git", ".hg", ".svn", "node_modules", "workspaces", "workspace"}
    if forbidden.intersection(parts):
        raise RuntimeError(f"Windows VM local-swarm payload path is not package-like: {raw}")


def run_windows_vm_hideme_live_wire(
    layout: WorkspaceLayout,
    config: VmLabConfig,
    *,
    package_zip: Path,
    run_id: str,
    run_report_dir: Path,
    keep_running: bool,
    runner: PowerShellRunner,
) -> list[dict[str, object]]:
    """Runs one real hide.me live-wire scenario on win10 and win11."""

    required_targets = tuple(load_windows_vm_profile_catalog(layout).HIDEME_LIVE_REQUIRED_TARGETS)
    target_report_dirs = {
        key: run_report_dir / key
        for key in required_targets
    }
    for directory in target_report_dirs.values():
        directory.mkdir(parents=True, exist_ok=True)
    if runner.dry_run:
        return [
            {
                "target": key,
                "vmName": config.targets[key].vm_name,
                "status": "planned",
                "checkpointName": config.hyperv.checkpoint_name,
                "reportDir": str(target_report_dirs[key]),
            }
            for key in required_targets
        ]
    host_contracts = load_windows_vm_host_contracts(layout)
    target_payloads = host_contracts.build_hideme_live_target_payloads(
        {key: config.targets[key].vm_name for key in required_targets}
    )
    script = _ps_with_payload(
        {
            **target_payloads,
            "checkpointName": config.hyperv.checkpoint_name,
            "vpnSwitchName": config.hyperv.vpn_switch_name,
            "username": config.guest.username,
            "password": resolve_guest_password(config),
            "packageZip": str(package_zip),
            "runnerPath": str(host_contracts.guest_runner_path(layout.tests_repo_root, "hideme-live-wire")),
            "profileHelperPath": str(host_contracts.profile_helper_path(layout.tests_repo_root)),
            "runId": run_id,
            "hostReportDir": str(run_report_dir),
            "keepRunning": keep_running,
            "apiKey": "vm-hideme-live-api-key",
        },
        host_contracts.load_guest_script(layout.tests_repo_root, "hideme-live-wire"),
    )
    stdout = runner.run(script, label="Windows VM hide.me live-wire", capture_json=True)
    payload = _parse_json_output(stdout, "Windows VM hide.me live-wire")
    _write_json(run_report_dir / "hideme-live-wire-result.json", payload)
    rows: list[dict[str, object]] = []
    target_results = payload.get("targets", {})
    for key in required_targets:
        target_payload = target_results.get(key, {}) if isinstance(target_results, dict) else {}
        _write_json(target_report_dirs[key] / f"{key}-result.json", target_payload)
        rows.append(
            {
                "target": key,
                "vmName": config.targets[key].vm_name,
                "status": target_payload.get("status", payload.get("status", "failed"))
                if isinstance(target_payload, dict)
                else payload.get("status", "failed"),
                "checkpointName": config.hyperv.checkpoint_name,
                "reportDir": str(target_report_dirs[key]),
                "guest": target_payload.get("guest", {}) if isinstance(target_payload, dict) else {},
                "vpn": target_payload.get("vpn", {}) if isinstance(target_payload, dict) else {},
                "checks": target_payload.get("checks", []) if isinstance(target_payload, dict) else [],
                "errors": target_payload.get("errors", []) if isinstance(target_payload, dict) else [],
            }
        )
    return rows


def ensure_python_installer(layout: WorkspaceLayout) -> Path:
    """Downloads and verifies the official Python Windows installer for guest setup."""

    output_path = python_installer_cache_path(layout)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.is_file() and _sha256(output_path).casefold() == PYTHON_INSTALLER_SHA256.casefold():
        return output_path
    if output_path.exists():
        output_path.unlink()
    with urllib.request.urlopen(PYTHON_INSTALLER_URL, timeout=120) as response:
        with output_path.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    actual = _sha256(output_path)
    if actual.casefold() != PYTHON_INSTALLER_SHA256.casefold():
        output_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Python installer SHA256 mismatch for {PYTHON_INSTALLER_URL}. "
            f"Expected {PYTHON_INSTALLER_SHA256}, got {actual}."
        )
    return output_path


def ensure_hide_me_installer(layout: WorkspaceLayout) -> Path:
    """Downloads and verifies the official hide.me Windows installer for guest setup."""

    output_path = hide_me_installer_cache_path(layout)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.is_file() and _is_trusted_hide_me_installer(output_path):
        return output_path
    if output_path.exists():
        output_path.unlink()
    final_url = _download_url_following_meta_refresh(HIDE_ME_INSTALLER_URL, output_path)
    if not _is_trusted_hide_me_installer(output_path):
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"hide.me installer did not have a trusted Authenticode signature: {final_url}")
    return output_path


def ensure_pwsh_installer(layout: WorkspaceLayout) -> Path:
    """Downloads and verifies the official PowerShell 7 Windows x64 MSI."""

    output_path = pwsh_installer_cache_path(layout)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.is_file() and _is_trusted_pwsh_installer(output_path):
        return output_path
    if output_path.exists():
        output_path.unlink()
    asset = _latest_pwsh_win_x64_msi_asset()
    with urllib.request.urlopen(asset["browser_download_url"], timeout=180) as response:
        with output_path.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    if not _is_trusted_pwsh_installer(output_path):
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"PowerShell installer did not have a trusted Authenticode signature: {asset['browser_download_url']}")
    return output_path


def ensure_dotnet_desktop_runtime_installer(layout: WorkspaceLayout) -> Path:
    """Downloads and verifies the .NET Desktop Runtime required by hide.me."""

    output_path = dotnet_desktop_runtime_cache_path(layout)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.is_file() and _is_trusted_dotnet_installer(output_path):
        return output_path
    if output_path.exists():
        output_path.unlink()
    with urllib.request.urlopen(DOTNET_DESKTOP_RUNTIME_URL, timeout=180) as response:
        with output_path.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    if not _is_trusted_dotnet_installer(output_path):
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f".NET Desktop Runtime installer did not have a trusted Authenticode signature: {DOTNET_DESKTOP_RUNTIME_URL}")
    return output_path


def ensure_dotnet_desktop_runtime_x86_installer(layout: WorkspaceLayout) -> Path:
    """Downloads and verifies the x86 .NET Desktop Runtime required by hide.me services."""

    output_path = dotnet_desktop_runtime_x86_cache_path(layout)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.is_file() and _is_trusted_dotnet_installer(output_path):
        return output_path
    if output_path.exists():
        output_path.unlink()
    with urllib.request.urlopen(DOTNET_DESKTOP_RUNTIME_X86_URL, timeout=180) as response:
        with output_path.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    if not _is_trusted_dotnet_installer(output_path):
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f".NET Desktop Runtime x86 installer did not have a trusted Authenticode signature: {DOTNET_DESKTOP_RUNTIME_X86_URL}")
    return output_path


def ensure_vc_redist_x64_installer(layout: WorkspaceLayout) -> Path:
    """Downloads and verifies the Microsoft VC++ x64 redistributable for guest browser automation."""

    output_path = vc_redist_x64_cache_path(layout)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.is_file() and _is_trusted_vc_redist_installer(output_path):
        return output_path
    if output_path.exists():
        output_path.unlink()
    with urllib.request.urlopen(VC_REDIST_X64_URL, timeout=180) as response:
        with output_path.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    if not _is_trusted_vc_redist_installer(output_path):
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"VC++ x64 redistributable did not have a trusted Authenticode signature: {VC_REDIST_X64_URL}")
    return output_path


def python_installer_cache_path(layout: WorkspaceLayout) -> Path:
    """Returns the host cache path for the Python guest installer."""

    return layout.output_tools_root / "python" / PYTHON_INSTALLER_FILE_NAME


def hide_me_installer_cache_path(layout: WorkspaceLayout) -> Path:
    """Returns the host cache path for the hide.me guest installer."""

    return layout.output_tools_root / "hide-me" / HIDE_ME_INSTALLER_FILE_NAME


def pwsh_installer_cache_path(layout: WorkspaceLayout) -> Path:
    """Returns the host cache path for the PowerShell 7 guest installer."""

    return layout.output_tools_root / "pwsh" / PWSH_INSTALLER_FILE_NAME


def dotnet_desktop_runtime_cache_path(layout: WorkspaceLayout) -> Path:
    """Returns the host cache path for the .NET Desktop Runtime guest installer."""

    return layout.output_tools_root / "dotnet" / DOTNET_DESKTOP_RUNTIME_FILE_NAME


def dotnet_desktop_runtime_x86_cache_path(layout: WorkspaceLayout) -> Path:
    """Returns the host cache path for the x86 .NET Desktop Runtime guest installer."""

    return layout.output_tools_root / "dotnet" / DOTNET_DESKTOP_RUNTIME_X86_FILE_NAME


def vc_redist_x64_cache_path(layout: WorkspaceLayout) -> Path:
    """Returns the host cache path for the Microsoft VC++ x64 redistributable."""

    return layout.output_tools_root / "vc-redist" / VC_REDIST_X64_FILE_NAME


def default_hide_me_settings_path() -> Path:
    """Returns the default host hide.me settings file path."""

    configured = os.environ.get(HIDE_ME_SETTINGS_PATH_ENV, "")
    if configured:
        return Path(configured).expanduser().resolve()
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        return (Path(appdata) / "Hide.me" / "vpn.settings").resolve()
    return (Path.home() / "AppData" / "Roaming" / "Hide.me" / "vpn.settings").resolve()


def resolve_hide_me_settings_path() -> Path:
    """Returns the host hide.me settings file that should be copied into the guests."""

    settings_path = default_hide_me_settings_path()
    if not settings_path.is_file():
        raise RuntimeError(
            f"hide.me settings file is missing: {settings_path}. "
            f"Set {HIDE_ME_SETTINGS_PATH_ENV} to the host vpn.settings path."
        )
    return settings_path


def _is_trusted_hide_me_installer(path: Path) -> bool:
    signature = _authenticode_signature(path)
    if signature.get("Status") != "Valid":
        return False
    subject = str(signature.get("Subject", ""))
    return any(token.casefold() in subject.casefold() for token in HIDE_ME_SIGNER_TOKENS)


def _is_trusted_pwsh_installer(path: Path) -> bool:
    signature = _authenticode_signature(path)
    if signature.get("Status") != "Valid":
        return False
    subject = str(signature.get("Subject", ""))
    return any(token.casefold() in subject.casefold() for token in PWSH_SIGNER_TOKENS)


def _is_trusted_dotnet_installer(path: Path) -> bool:
    signature = _authenticode_signature(path)
    if signature.get("Status") != "Valid":
        return False
    subject = str(signature.get("Subject", ""))
    return any(token.casefold() in subject.casefold() for token in DOTNET_SIGNER_TOKENS)


def _is_trusted_vc_redist_installer(path: Path) -> bool:
    signature = _authenticode_signature(path)
    if signature.get("Status") != "Valid":
        return False
    subject = str(signature.get("Subject", ""))
    return any(token.casefold() in subject.casefold() for token in VC_REDIST_SIGNER_TOKENS)


def _latest_pwsh_win_x64_msi_asset() -> dict[str, str]:
    request = urllib.request.Request(PWSH_RELEASE_API_URL, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assets = payload.get("assets", [])
    if not isinstance(assets, list):
        raise RuntimeError("PowerShell latest release API did not return an asset list.")
    candidates: list[dict[str, str]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", ""))
        download_url = str(asset.get("browser_download_url", ""))
        if re.fullmatch(r"PowerShell-\d+\.\d+\.\d+-win-x64\.msi", name) and download_url:
            candidates.append({"name": name, "browser_download_url": download_url})
    if not candidates:
        raise RuntimeError("PowerShell latest release did not include a stable win-x64 MSI asset.")
    return sorted(candidates, key=lambda item: item["name"], reverse=True)[0]


def _authenticode_signature(path: Path) -> dict[str, object]:
    executable = shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"
    script = (
        "$signature = Get-AuthenticodeSignature -LiteralPath "
        + json.dumps(str(path))
        + "; [pscustomobject]@{ Status = $signature.Status.ToString(); "
        + "Subject = $signature.SignerCertificate.Subject } | ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        env=_powershell_subprocess_env(executable),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return {}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _download_url_following_meta_refresh(url: str, output_path: Path) -> str:
    """Downloads a URL, following simple HTML meta-refresh download redirects."""

    current_url = url
    for _ in range(4):
        with urllib.request.urlopen(current_url, timeout=120) as response:
            content_type = response.headers.get("Content-Type", "")
            payload = response.read()
        if _looks_like_html_download_page(content_type, payload):
            next_url = _extract_meta_refresh_url(payload.decode("utf-8", errors="ignore"))
            if next_url:
                current_url = urllib.parse.urljoin(current_url, next_url)
                continue
        output_path.write_bytes(payload)
        return current_url
    raise RuntimeError(f"Too many meta-refresh redirects while downloading {url}.")


def _looks_like_html_download_page(content_type: str, payload: bytes) -> bool:
    head = payload[:2048].lstrip().lower()
    return "text/html" in content_type.casefold() or head.startswith(b"<!doctype html") or b"<html" in head


def _extract_meta_refresh_url(html: str) -> str | None:
    for match in re.finditer(r"<meta\b[^>]*>", html, flags=re.IGNORECASE):
        tag = match.group(0)
        if "refresh" not in tag.casefold():
            continue
        url_match = re.search(r"url\s*=\s*([^\"'>\s;]+)", tag, flags=re.IGNORECASE)
        if url_match:
            return url_match.group(1).strip()
    return None


def _prepare_vm_target_script() -> str:
    return r"""
$ErrorActionPreference = 'Stop'
$secure = ConvertTo-SecureString $payload.password -AsPlainText -Force
$credential = [pscredential]::new($payload.username, $secure)
$vm = Get-VM -Name $payload.vmName -ErrorAction SilentlyContinue
if ($vm -and -not $payload.rebuildImages) {
  $snapshot = Get-VMSnapshot -VMName $payload.vmName -Name $payload.checkpointName -ErrorAction SilentlyContinue
  if ($snapshot) {
    Write-Output ('VM already prepared: ' + $payload.vmName)
    return
  }
}
if ($vm) {
  Stop-VM -Name $payload.vmName -TurnOff -Force -ErrorAction SilentlyContinue
  Remove-VM -Name $payload.vmName -Force
}
if (Test-Path -LiteralPath $payload.vhdPath) {
  Remove-Item -LiteralPath $payload.vhdPath -Force
}
$vhdParent = Split-Path -Parent $payload.vhdPath
$vhdStem = [IO.Path]::GetFileNameWithoutExtension($payload.vhdPath)
Get-ChildItem -Path $vhdParent -Filter ($vhdStem + '_*.avhdx') -ErrorAction SilentlyContinue |
  Remove-Item -Force -ErrorAction SilentlyContinue
if (-not (Test-Path -LiteralPath $payload.isoPath)) {
  throw ('Windows ISO is missing: ' + $payload.isoPath)
}
if (-not (Get-VMSwitch -Name $payload.switchName -ErrorAction SilentlyContinue)) {
  New-VMSwitch -Name $payload.switchName -SwitchType Private | Out-Null
}
function Ensure-ProvisioningNatSwitch {
  param(
    [string] $SwitchName,
    [string] $NatName,
    [string] $NatPrefix,
    [string] $Gateway,
    [int] $PrefixLength
  )
  $adapterName = 'vEthernet (' + $SwitchName + ')'
  $switch = Get-VMSwitch -Name $SwitchName -ErrorAction SilentlyContinue
  if ($switch) {
    $existingAdapter = Get-NetAdapter -Name $adapterName -IncludeHidden -ErrorAction SilentlyContinue
    if ($existingAdapter -and $existingAdapter.Status -eq 'Not Present') {
      Remove-VMSwitch -Name $SwitchName -Force
      $switch = $null
    }
  }
  if (-not $switch) {
    New-VMSwitch -Name $SwitchName -SwitchType Internal | Out-Null
  }
  $adapter = $null
  for ($attempt = 1; $attempt -le 30; $attempt++) {
    $adapter = Get-NetAdapter -Name $adapterName -IncludeHidden -ErrorAction SilentlyContinue |
      Where-Object { $_.Status -ne 'Not Present' } |
      Select-Object -First 1
    if ($adapter) { break }
    Start-Sleep -Seconds 1
  }
  if (-not $adapter) {
    throw ('Hyper-V provisioning switch adapter is missing: ' + $adapterName)
  }
  $address = Get-NetIPAddress -InterfaceIndex $adapter.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -eq $Gateway } |
    Select-Object -First 1
  if (-not $address) {
    New-NetIPAddress -InterfaceIndex $adapter.InterfaceIndex -IPAddress $Gateway -PrefixLength $PrefixLength | Out-Null
  }
  if (-not (Get-NetNat -Name $NatName -ErrorAction SilentlyContinue)) {
    New-NetNat -Name $NatName -InternalIPInterfaceAddressPrefix $NatPrefix | Out-Null
  }
}

function Set-OfflineRegistryDword {
  param([string] $Path, [string] $Name, [int] $Value)
  try {
    New-Item -Path $Path -Force -ErrorAction SilentlyContinue | Out-Null
    if (Test-Path -LiteralPath $Path) {
      New-ItemProperty -Path $Path -Name $Name -PropertyType DWord -Value $Value -Force -ErrorAction SilentlyContinue | Out-Null
    }
  } catch {}
}

function Set-OfflineRegistryString {
  param([string] $Path, [string] $Name, [string] $Value)
  try {
    New-Item -Path $Path -Force -ErrorAction SilentlyContinue | Out-Null
    if (Test-Path -LiteralPath $Path) {
      New-ItemProperty -Path $Path -Name $Name -PropertyType String -Value $Value -Force -ErrorAction SilentlyContinue | Out-Null
    }
  } catch {}
}

function Set-OfflineLabServiceDisabled {
  param([string] $SystemHiveRoot, [string] $ServiceName)
  foreach ($controlSet in Get-ChildItem -Path $SystemHiveRoot -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -match '^ControlSet\d+$' }) {
    $servicePath = Join-Path $controlSet.PSPath ("Services\" + $ServiceName)
    if (Test-Path -LiteralPath $servicePath) {
      Set-OfflineRegistryDword -Path $servicePath -Name Start -Value 4
      Set-OfflineRegistryDword -Path $servicePath -Name LaunchProtected -Value 0
      Set-OfflineRegistryDword -Path $servicePath -Name DelayedAutoStart -Value 0
    }
  }
}

function Set-OfflineLabUltraLeanBaselineForVhd {
  param([string] $VhdPath)
  $mounted = $null
  for ($attempt = 1; $attempt -le 30; $attempt++) {
    try {
      $mounted = Mount-VHD -Path $VhdPath -Passthru -ErrorAction Stop
      break
    } catch {
      if ($attempt -eq 30) { throw }
      Start-Sleep -Seconds 2
    }
  }
  try {
    $disk = $mounted | Get-Disk
    $windowsRoot = $null
    foreach ($partition in Get-Partition -DiskNumber $disk.Number -ErrorAction SilentlyContinue) {
      $volume = $partition | Get-Volume -ErrorAction SilentlyContinue
      if ($volume -and -not $volume.DriveLetter) {
        try {
          Add-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber $partition.PartitionNumber -AssignDriveLetter -ErrorAction Stop
          $volume = $partition | Get-Volume -ErrorAction SilentlyContinue
        } catch {}
      }
      if ($volume -and $volume.DriveLetter) {
        $candidate = "$($volume.DriveLetter):"
        if (Test-Path -LiteralPath (Join-Path $candidate 'Windows\System32\Config\SYSTEM')) {
          $windowsRoot = $candidate
          break
        }
      }
    }
    if (-not $windowsRoot) {
      throw ('Could not find Windows root in VHD: ' + $VhdPath)
    }
    Set-OfflineLabUltraLeanBaseline -WindowsRoot $windowsRoot
  } finally {
    Dismount-VHD -Path $VhdPath
  }
}

function Set-OfflineLabUltraLeanBaselineForVmVhd {
  param([string] $VmName, [string] $VhdPath)
  $allDrives = @(
    Get-VMHardDiskDrive -VMName $VmName -ErrorAction Stop | ForEach-Object {
      [pscustomobject]@{
        Path = [string] $_.Path
        ControllerType = $_.ControllerType
        ControllerNumber = $_.ControllerNumber
        ControllerLocation = $_.ControllerLocation
      }
    }
  )
  $drives = @(
    $allDrives |
      Where-Object { [string]::Equals($_.Path, $VhdPath, [StringComparison]::OrdinalIgnoreCase) }
  )
  if ($drives.Count -eq 0) {
    $drives = $allDrives
  }
  if ($drives.Count -eq 0) {
    Set-OfflineLabUltraLeanBaselineForVhd -VhdPath $VhdPath
    return
  }
  foreach ($drive in $drives) {
    Remove-VMHardDiskDrive -VMName $VmName -ControllerType $drive.ControllerType -ControllerNumber $drive.ControllerNumber -ControllerLocation $drive.ControllerLocation
  }
  try {
    foreach ($drive in $drives) {
      $drivePath = if ($drive.Path) { $drive.Path } else { $VhdPath }
      Set-OfflineLabUltraLeanBaselineForVhd -VhdPath $drivePath
    }
  } finally {
    foreach ($drive in $drives) {
      $drivePath = if ($drive.Path) { $drive.Path } else { $VhdPath }
      Add-VMHardDiskDrive -VMName $VmName -ControllerType $drive.ControllerType -ControllerNumber $drive.ControllerNumber -ControllerLocation $drive.ControllerLocation -Path $drivePath
    }
  }
}

function Set-OfflineLabUltraLeanBaseline {
  param([string] $WindowsRoot)
  $loadedHives = @()
  function Mount-OfflineHive {
    param([string] $Name, [string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
      return $null
    }
    reg.exe load ("HKLM\" + $Name) $Path | Out-Null
    if ($LASTEXITCODE -ne 0) {
      return $null
    }
    $script:loadedHives += $Name
    return ("HKLM:\" + $Name)
  }
  try {
    $software = Mount-OfflineHive -Name 'EMULEBB_OFFLINE_SOFTWARE' -Path (Join-Path $WindowsRoot 'Windows\System32\Config\SOFTWARE')
    $system = Mount-OfflineHive -Name 'EMULEBB_OFFLINE_SYSTEM' -Path (Join-Path $WindowsRoot 'Windows\System32\Config\SYSTEM')
    $defaultUser = Mount-OfflineHive -Name 'EMULEBB_OFFLINE_DEFAULT_USER' -Path (Join-Path $WindowsRoot 'Users\Default\NTUSER.DAT')
    if ($software) {
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows Defender') -Name DisableAntiSpyware -Value 1
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows Defender') -Name DisableAntiVirus -Value 1
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows Defender') -Name ServiceKeepAlive -Value 0
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows Defender\Real-Time Protection') -Name DisableRealtimeMonitoring -Value 1
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows Defender\Real-Time Protection') -Name DisableBehaviorMonitoring -Value 1
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows Defender\Real-Time Protection') -Name DisableOnAccessProtection -Value 1
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows Defender\Real-Time Protection') -Name DisableScanOnRealtimeEnable -Value 1
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows Defender\Spynet') -Name SpynetReporting -Value 0
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows Defender\Spynet') -Name SubmitSamplesConsent -Value 2
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows Defender Security Center\Notifications') -Name DisableNotifications -Value 1
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows Defender Security Center\Systray') -Name HideSystray -Value 1
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows\CloudContent') -Name DisableWindowsConsumerFeatures -Value 1
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows\CloudContent') -Name DisableWindowsSpotlightFeatures -Value 1
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows\DataCollection') -Name AllowTelemetry -Value 0
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows\AdvertisingInfo') -Name DisabledByGroupPolicy -Value 1
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows\System') -Name EnableActivityFeed -Value 0
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows\System') -Name PublishUserActivities -Value 0
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows\System') -Name UploadUserActivities -Value 0
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\Windows\Windows Search') -Name AllowCortana -Value 0
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\WindowsStore') -Name AutoDownload -Value 2
      Set-OfflineRegistryDword -Path (Join-Path $software 'Policies\Microsoft\WindowsStore') -Name RemoveWindowsStore -Value 1
    }
    if ($system) {
      foreach ($serviceName in @(
        'WdBoot', 'WdFilter', 'WdNisDrv', 'WinDefend', 'WdNisSvc',
        'Sense', 'SecurityHealthService', 'wscsvc',
        'AppXSvc', 'ClipSVC', 'InstallService', 'edgeupdate', 'edgeupdatem',
        'DiagTrack', 'dmwappushservice', 'WerSvc', 'WSearch', 'SysMain',
        'DoSvc', 'WaaSMedicSvc', 'UsoSvc', 'wuauserv', 'BITS',
        'MapsBroker', 'RetailDemo', 'XblAuthManager', 'XblGameSave',
        'XboxGipSvc', 'XboxNetApiSvc', 'WpnService', 'CDPSvc'
      )) {
        Set-OfflineLabServiceDisabled -SystemHiveRoot $system -ServiceName $serviceName
      }
    }
    if ($defaultUser) {
      Set-OfflineRegistryDword -Path (Join-Path $defaultUser 'Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo') -Name Enabled -Value 0
      Set-OfflineRegistryDword -Path (Join-Path $defaultUser 'Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects') -Name VisualFXSetting -Value 2
      Set-OfflineRegistryDword -Path (Join-Path $defaultUser 'Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced') -Name TaskbarAnimations -Value 0
      Set-OfflineRegistryDword -Path (Join-Path $defaultUser 'Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager') -Name SilentInstalledAppsEnabled -Value 0
      Set-OfflineRegistryString -Path (Join-Path $defaultUser 'Control Panel\Desktop') -Name Wallpaper -Value ''
      Set-OfflineRegistryString -Path (Join-Path $defaultUser 'Control Panel\Desktop') -Name FontSmoothing -Value '2'
      Set-OfflineRegistryDword -Path (Join-Path $defaultUser 'Control Panel\Desktop') -Name FontSmoothingType -Value 2
      Set-OfflineRegistryString -Path (Join-Path $defaultUser 'Control Panel\Desktop\WindowMetrics') -Name MinAnimate -Value '0'
    }
  } finally {
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
    foreach ($name in @($loadedHives | Sort-Object -Descending)) {
      reg.exe unload ("HKLM\" + $name) | Out-Null
    }
  }
}

$provisioningSwitchName = $payload.provisioningSwitchName
if (-not $provisioningSwitchName) {
  $provisioningSwitchName = $payload.switchName
}
Ensure-ProvisioningNatSwitch -SwitchName $provisioningSwitchName -NatName $payload.provisioningNatName -NatPrefix $payload.provisioningNatPrefix -Gateway $payload.provisioningGateway -PrefixLength ([int] $payload.provisioningPrefixLength)
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $payload.vhdPath) | Out-Null
New-VHD -Path $payload.vhdPath -SizeBytes ([int64] $payload.diskBytes) -Dynamic | Out-Null
$mountedVhd = Mount-VHD -Path $payload.vhdPath -Passthru
$disk = $mountedVhd | Get-Disk
try {
  Initialize-Disk -Number $disk.Number -PartitionStyle GPT
  $efi = New-Partition -DiskNumber $disk.Number -Size 260MB -GptType '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}'
  $windows = New-Partition -DiskNumber $disk.Number -UseMaximumSize
  Format-Volume -Partition $efi -FileSystem FAT32 -NewFileSystemLabel System -Confirm:$false | Out-Null
  Format-Volume -Partition $windows -FileSystem NTFS -NewFileSystemLabel Windows -Confirm:$false | Out-Null
  Add-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber $efi.PartitionNumber -AssignDriveLetter
  Add-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber $windows.PartitionNumber -AssignDriveLetter
  $efiVolume = Get-Partition -DiskNumber $disk.Number -PartitionNumber $efi.PartitionNumber | Get-Volume
  $windowsVolume = Get-Partition -DiskNumber $disk.Number -PartitionNumber $windows.PartitionNumber | Get-Volume
  if (-not $efiVolume.DriveLetter) {
    throw 'EFI partition did not receive a drive letter.'
  }
  if (-not $windowsVolume.DriveLetter) {
    throw 'Windows partition did not receive a drive letter.'
  }
  $efiRoot = "$($efiVolume.DriveLetter):"
  $windowsRoot = "$($windowsVolume.DriveLetter):"
  Mount-DiskImage -ImagePath $payload.isoPath | Out-Null
  $iso = Get-DiskImage -ImagePath $payload.isoPath | Get-Volume
  $isoRoot = "$($iso.DriveLetter):"
  $installImage = Join-Path $isoRoot 'sources\install.wim'
  if (-not (Test-Path -LiteralPath $installImage)) {
    $installImage = Join-Path $isoRoot 'sources\install.esd'
  }
  if (-not (Test-Path -LiteralPath $installImage)) {
    throw 'Windows ISO does not contain sources\install.wim or sources\install.esd.'
  }
  $image = Get-WindowsImage -ImagePath $installImage | Where-Object { $_.ImageName -eq $payload.edition } | Select-Object -First 1
  if (-not $image) {
    throw ('Windows edition not found in ISO: ' + $payload.edition)
  }
  dism.exe /Apply-Image /ImageFile:$installImage /Index:$($image.ImageIndex) /ApplyDir:$windowsRoot\ | Out-Host
  if ($LASTEXITCODE -ne 0) {
    throw ('DISM Apply-Image failed with exit code ' + $LASTEXITCODE)
  }
  $unattendDir = Join-Path $windowsRoot 'Windows\Panther\Unattend'
  New-Item -ItemType Directory -Force -Path $unattendDir | Out-Null
  $escapedPassword = [Security.SecurityElement]::Escape($payload.password)
  $escapedUser = [Security.SecurityElement]::Escape($payload.username)
$unattend = @"
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
  <settings pass="specialize">
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <InputLocale>en-US</InputLocale>
      <SystemLocale>en-US</SystemLocale>
      <UILanguage>en-US</UILanguage>
      <UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <ComputerName>*</ComputerName>
      <RegisteredOwner>eMuleBB</RegisteredOwner>
      <TimeZone>UTC</TimeZone>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <InputLocale>en-US</InputLocale>
      <SystemLocale>en-US</SystemLocale>
      <UILanguage>en-US</UILanguage>
      <UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <SkipMachineOOBE>true</SkipMachineOOBE>
        <SkipUserOOBE>true</SkipUserOOBE>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
      <UserAccounts>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>$escapedUser</Name>
            <Group>Administrators</Group>
            <Password><Value>$escapedPassword</Value><PlainText>true</PlainText></Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>
      <AutoLogon>
        <Enabled>true</Enabled>
        <LogonCount>999</LogonCount>
        <Username>$escapedUser</Username>
        <Password><Value>$escapedPassword</Value><PlainText>true</PlainText></Password>
      </AutoLogon>
    </component>
  </settings>
</unattend>
"@
  Set-Content -Path (Join-Path $unattendDir 'Unattend.xml') -Value $unattend -Encoding UTF8
  Set-OfflineLabUltraLeanBaseline -WindowsRoot $windowsRoot
  New-Item -ItemType Directory -Force -Path "$efiRoot\EFI\Microsoft\Boot" | Out-Null
  New-Item -ItemType Directory -Force -Path "$efiRoot\EFI\Microsoft\Recovery" | Out-Null
  bcdboot.exe "$windowsRoot\Windows" /s $efiRoot /f UEFI | Out-Host
  if ($LASTEXITCODE -ne 0) {
    throw ('BCDBoot failed with exit code ' + $LASTEXITCODE)
  }
  $sourceBootMgr = Join-Path $windowsRoot 'Windows\Boot\EFI\bootmgfw.efi'
  $microsoftBootMgr = Join-Path $efiRoot 'EFI\Microsoft\Boot\bootmgfw.efi'
  $fallbackBootMgr = Join-Path $efiRoot 'EFI\Boot\bootx64.efi'
  if (-not (Test-Path -LiteralPath $microsoftBootMgr)) {
    if (-not (Test-Path -LiteralPath $sourceBootMgr)) {
      throw ('Windows image is missing EFI boot manager: ' + $sourceBootMgr)
    }
    Copy-Item -LiteralPath $sourceBootMgr -Destination $microsoftBootMgr -Force
  }
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $fallbackBootMgr) | Out-Null
  Copy-Item -LiteralPath $microsoftBootMgr -Destination $fallbackBootMgr -Force
}
finally {
  Dismount-DiskImage -ImagePath $payload.isoPath -ErrorAction SilentlyContinue
  Dismount-VHD -Path $payload.vhdPath -ErrorAction SilentlyContinue
}
New-VM -Name $payload.vmName -Generation 2 -MemoryStartupBytes ([int64] $payload.memoryBytes) -VHDPath $payload.vhdPath -SwitchName $provisioningSwitchName | Out-Null
Set-VMProcessor -VMName $payload.vmName -Count ([int] $payload.processorCount)
Set-VMFirmware -VMName $payload.vmName -EnableSecureBoot On -SecureBootTemplate 'MicrosoftWindows'
Start-VM -Name $payload.vmName
$deadline = (Get-Date).AddMinutes(30)
do {
  Start-Sleep -Seconds 10
  try {
    $ready = Invoke-Command -VMName $payload.vmName -Credential $credential -ScriptBlock { 'ready' } -ErrorAction Stop
    if ($ready -eq 'ready') { break }
  } catch {
    if ((Get-Date) -gt $deadline) { throw }
  }
} while ((Get-Date) -lt $deadline)
$session = New-PSSession -VMName $payload.vmName -Credential $credential
try {
  Invoke-Command -Session $session -ScriptBlock {
    New-Item -ItemType Directory -Force -Path C:\eMuleBBVmTest | Out-Null
  } | Out-Null
  Invoke-Command -Session $session -ScriptBlock {
    param([string] $GuestIp, [int] $PrefixLength, [string] $Gateway, [string[]] $DnsServers)
    $adapter = Get-NetAdapter | Where-Object { $_.Status -eq 'Up' } | Sort-Object InterfaceIndex | Select-Object -First 1
    if (-not $adapter) {
      $adapter = Get-NetAdapter | Sort-Object InterfaceIndex | Select-Object -First 1
    }
    if (-not $adapter) {
      throw 'Guest provisioning network adapter was not found.'
    }
    Get-NetIPAddress -InterfaceIndex $adapter.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
      Where-Object { $_.IPAddress -ne $GuestIp } |
      Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
    if (-not (Get-NetIPAddress -InterfaceIndex $adapter.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -eq $GuestIp })) {
      New-NetIPAddress -InterfaceIndex $adapter.InterfaceIndex -IPAddress $GuestIp -PrefixLength $PrefixLength -DefaultGateway $Gateway | Out-Null
    }
    Set-DnsClientServerAddress -InterfaceIndex $adapter.InterfaceIndex -ServerAddresses $DnsServers
    for ($attempt = 1; $attempt -le 30; $attempt++) {
      try {
        Resolve-DnsName -Name 'pypi.org' -ErrorAction Stop | Out-Null
        return
      } catch {
        Start-Sleep -Seconds 2
      }
    }
    throw 'Guest provisioning DNS did not become ready.'
  } -ArgumentList $payload.provisioningGuestIp, ([int] $payload.provisioningPrefixLength), $payload.provisioningGateway, $payload.provisioningDns | Out-Null
  Copy-Item -ToSession $session -Path $payload.pythonInstallerPath -Destination 'C:\eMuleBBVmTest\python-installer.exe'
  Copy-Item -ToSession $session -Path $payload.hideMeInstallerPath -Destination 'C:\eMuleBBVmTest\hide-me-installer.exe'
  Copy-Item -ToSession $session -Path $payload.pwshInstallerPath -Destination 'C:\eMuleBBVmTest\pwsh-installer.msi'
  Copy-Item -ToSession $session -Path $payload.dotnetDesktopRuntimePath -Destination 'C:\eMuleBBVmTest\dotnet-desktop-runtime.exe'
  Copy-Item -ToSession $session -Path $payload.dotnetDesktopRuntimeX86Path -Destination 'C:\eMuleBBVmTest\dotnet-desktop-runtime-x86.exe'
  Copy-Item -ToSession $session -Path $payload.vcRedistX64Path -Destination 'C:\eMuleBBVmTest\vc-redist-x64.exe'
  Copy-Item -ToSession $session -Path $payload.hideMeSettingsPath -Destination 'C:\eMuleBBVmTest\hide-me-vpn.settings'
  Invoke-Command -Session $session -ScriptBlock {
  param($pythonInstallerSha256, $pythonInstallDir, [string[]] $pythonLivePackages, $playwrightBrowser, $hideMeInstallDir, $pwshInstallDir, $dotnetDesktopRuntimeDir, $dotnetDesktopRuntimeX86Dir, $vcRedistX64RuntimeDll, $guestUsername, $guestPassword)
  function Add-LabDefenderExclusion {
    param([string] $Path)
    if (Test-Path -LiteralPath $Path) {
      try { Add-MpPreference -ExclusionPath $Path -ErrorAction Stop } catch {}
    }
  }

  function Disable-LabScheduledTask {
    param([string] $TaskPath, [string] $TaskName)
    try { Disable-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction Stop | Out-Null } catch {}
  }

  function Set-LabRegistryDword {
    param([string] $Path, [string] $Name, [int] $Value)
    try {
      New-Item -Path $Path -Force -ErrorAction SilentlyContinue | Out-Null
      if (Test-Path -LiteralPath $Path) {
        New-ItemProperty -Path $Path -Name $Name -PropertyType DWord -Value $Value -Force -ErrorAction SilentlyContinue | Out-Null
      }
    } catch {}
  }

  function Set-LabRegistryString {
    param([string] $Path, [string] $Name, [string] $Value)
    try {
      New-Item -Path $Path -Force -ErrorAction SilentlyContinue | Out-Null
      if (Test-Path -LiteralPath $Path) {
        New-ItemProperty -Path $Path -Name $Name -PropertyType String -Value $Value -Force -ErrorAction SilentlyContinue | Out-Null
      }
    } catch {}
  }

  function Disable-LabService {
    param([string] $ServiceName)
    foreach ($service in Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
      try { Stop-Service -Name $service.Name -Force -ErrorAction SilentlyContinue } catch {}
      try { Set-Service -Name $service.Name -StartupType Disabled -ErrorAction Stop } catch {}
      try { sc.exe config $service.Name start= disabled | Out-Null } catch {}
    }
  }

  function Set-LabWindowsUpdateContainment {
    foreach ($serviceName in @(
      'wuauserv', 'UsoSvc', 'BITS', 'InstallService', 'WaaSMedicSvc',
      'uhssvc', 'wisvc', 'edgeupdate', 'edgeupdatem'
    )) {
      foreach ($service in Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
        try { Stop-Service -Name $service.Name -Force -ErrorAction SilentlyContinue } catch {}
        try { Set-Service -Name $service.Name -StartupType Disabled -ErrorAction Stop } catch {}
        try { sc.exe config $service.Name start= disabled | Out-Null } catch {}
      }
    }
    try {
      New-Item -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate' -Force | Out-Null
      New-Item -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' -Force | Out-Null
      New-ItemProperty -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' -Name NoAutoUpdate -PropertyType DWord -Value 1 -Force | Out-Null
      New-ItemProperty -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' -Name AUOptions -PropertyType DWord -Value 1 -Force | Out-Null
      New-ItemProperty -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate' -Name DoNotConnectToWindowsUpdateInternetLocations -PropertyType DWord -Value 1 -Force | Out-Null
    } catch {}
    foreach ($task in @(
      @{ Path = '\Microsoft\Windows\WindowsUpdate\'; Name = 'Scheduled Start' },
      @{ Path = '\Microsoft\Windows\WindowsUpdate\'; Name = 'sih' },
      @{ Path = '\Microsoft\Windows\WindowsUpdate\'; Name = 'sihboot' },
      @{ Path = '\Microsoft\Windows\UpdateOrchestrator\'; Name = 'Schedule Scan' },
      @{ Path = '\Microsoft\Windows\UpdateOrchestrator\'; Name = 'Schedule Scan Static Task' },
      @{ Path = '\Microsoft\Windows\UpdateOrchestrator\'; Name = 'USO_UxBroker' },
      @{ Path = '\Microsoft\Windows\UpdateOrchestrator\'; Name = 'UpdateModelTask' },
      @{ Path = '\Microsoft\Windows\UpdateOrchestrator\'; Name = 'Maintenance Install' },
      @{ Path = '\Microsoft\Windows\UpdateOrchestrator\'; Name = 'Reboot' },
      @{ Path = '\Microsoft\Windows\UpdateOrchestrator\'; Name = 'Reboot_AC' },
      @{ Path = '\Microsoft\Windows\UpdateOrchestrator\'; Name = 'Reboot_Battery' }
    )) {
      Disable-LabScheduledTask -TaskPath $task.Path -TaskName $task.Name
    }
  }

  function Set-LabLeanBaseline {
    param([string] $PythonPath, [string] $HideMePath, [string] $Username, [string] $Password)
    Set-LabAutoLogin -Username $Username -Password $Password
    Set-LabNoLock
    Set-LabMinimalUiAndPrivacyBaseline
    Remove-LabAppxBloat
    Set-LabWindowsUpdateContainment
    Add-LabDefenderExclusion -Path 'C:\eMuleBBVmTest'
    Add-LabDefenderExclusion -Path $PythonPath
    Add-LabDefenderExclusion -Path $HideMePath
    try { Set-MpPreference -DisableRealtimeMonitoring $true -ErrorAction Stop } catch {}
    try { Set-MpPreference -DisableArchiveScanning $true -MAPSReporting Disabled -SubmitSamplesConsent NeverSend -ErrorAction Stop } catch {}
    Disable-LabDefenderAndSecurityStack
    $leanServiceNames = @(
      'SysMain', 'WSearch', 'DiagTrack', 'DoSvc', 'WaaSMedicSvc', 'WerSvc',
      'RetailDemo', 'MapsBroker', 'lfsvc', 'WMPNetworkSvc', 'RemoteRegistry',
      'Fax', 'Spooler', 'BTAGService', 'bthserv', 'PhoneSvc', 'WalletService',
      'WbioSrvc', 'TabletInputService', 'XblAuthManager', 'XblGameSave',
      'XboxGipSvc', 'XboxNetApiSvc', 'dmwappushservice', 'PcaSvc',
      'CDPSvc', 'CDPUserSvc_*', 'PimIndexMaintenanceSvc_*', 'OneSyncSvc_*',
      'UnistoreSvc_*', 'UserDataSvc_*', 'MessagingService_*',
      'WpnService', 'WpnUserService_*', 'diagnosticshub.standardcollector.service',
      'DPS', 'DusmSvc', 'TrkWks', 'SCardSvr', 'SEMgrSvc', 'SensorService',
      'SensrSvc', 'stisvc', 'icssvc', 'SharedAccess', 'RemoteAccess',
      'RemoteAccessAutoConnectionManager', 'RasAuto',
      'AppXSvc', 'ClipSVC', 'InstallService', 'edgeupdate', 'edgeupdatem',
      'WdiServiceHost', 'WdiSystemHost', 'wercplsupport', 'shpamsvc'
    )
    foreach ($serviceName in $leanServiceNames) {
      Disable-LabService -ServiceName $serviceName
    }
    foreach ($task in Get-LabLeanScheduledTasks) {
      Disable-LabScheduledTask -TaskPath $task.Path -TaskName $task.Name
    }
    try { powercfg.exe /hibernate off | Out-Null } catch {}
    try { powercfg.exe /setactive SCHEME_MIN | Out-Null } catch {}
    try { powercfg.exe /change monitor-timeout-ac 0 | Out-Null } catch {}
    try { powercfg.exe /change standby-timeout-ac 0 | Out-Null } catch {}
    try { powercfg.exe /change disk-timeout-ac 0 | Out-Null } catch {}
  }

  function Disable-LabDefenderAndSecurityStack {
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender' -Name DisableAntiSpyware -Value 1
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender' -Name DisableAntiVirus -Value 1
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender' -Name ServiceKeepAlive -Value 0
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender\Real-Time Protection' -Name DisableRealtimeMonitoring -Value 1
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender\Real-Time Protection' -Name DisableBehaviorMonitoring -Value 1
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender\Real-Time Protection' -Name DisableOnAccessProtection -Value 1
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender\Real-Time Protection' -Name DisableScanOnRealtimeEnable -Value 1
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender\Spynet' -Name SpynetReporting -Value 0
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender\Spynet' -Name SubmitSamplesConsent -Value 2
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender Security Center\Notifications' -Name DisableNotifications -Value 1
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender Security Center\Systray' -Name HideSystray -Value 1
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer' -Name HideSCAHealth -Value 1
    try { Get-Process -Name SecurityHealthSystray -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue } catch {}
    try { Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run' -Name SecurityHealth -ErrorAction SilentlyContinue } catch {}
    foreach ($serviceName in @('WdBoot', 'WdFilter', 'WdNisDrv', 'WinDefend', 'WdNisSvc', 'Sense', 'SecurityHealthService', 'wscsvc')) {
      Set-LabRegistryDword -Path ('HKLM:\SYSTEM\CurrentControlSet\Services\' + $serviceName) -Name Start -Value 4
      Set-LabRegistryDword -Path ('HKLM:\SYSTEM\CurrentControlSet\Services\' + $serviceName) -Name LaunchProtected -Value 0
      Disable-LabService -ServiceName $serviceName
    }
  }

  function Get-LabLeanScheduledTasks {
    return @(
      @{ Path = '\Microsoft\Windows\Application Experience\'; Name = 'Microsoft Compatibility Appraiser' },
      @{ Path = '\Microsoft\Windows\Application Experience\'; Name = 'PcaPatchDbTask' },
      @{ Path = '\Microsoft\Windows\Application Experience\'; Name = 'ProgramDataUpdater' },
      @{ Path = '\Microsoft\Windows\Application Experience\'; Name = 'StartupAppTask' },
      @{ Path = '\Microsoft\Windows\Autochk\'; Name = 'Proxy' },
      @{ Path = '\Microsoft\Windows\Customer Experience Improvement Program\'; Name = 'Consolidator' },
      @{ Path = '\Microsoft\Windows\Customer Experience Improvement Program\'; Name = 'KernelCeipTask' },
      @{ Path = '\Microsoft\Windows\Customer Experience Improvement Program\'; Name = 'UsbCeip' },
      @{ Path = '\Microsoft\Windows\DiskDiagnostic\'; Name = 'Microsoft-Windows-DiskDiagnosticDataCollector' },
      @{ Path = '\Microsoft\Windows\Feedback\Siuf\'; Name = 'DmClient' },
      @{ Path = '\Microsoft\Windows\Feedback\Siuf\'; Name = 'DmClientOnScenarioDownload' },
      @{ Path = '\Microsoft\Windows\Maps\'; Name = 'MapsToastTask' },
      @{ Path = '\Microsoft\Windows\Maps\'; Name = 'MapsUpdateTask' },
      @{ Path = '\Microsoft\Windows\PushToInstall\'; Name = 'LoginCheck' },
      @{ Path = '\Microsoft\Windows\PushToInstall\'; Name = 'Registration' },
      @{ Path = '\Microsoft\Windows\InstallService\'; Name = 'ScanForUpdates' },
      @{ Path = '\Microsoft\Windows\InstallService\'; Name = 'ScanForUpdatesAsUser' },
      @{ Path = '\Microsoft\Windows\InstallService\'; Name = 'SmartRetry' },
      @{ Path = '\Microsoft\Windows\InstallService\'; Name = 'WakeUpAndContinueUpdates' },
      @{ Path = '\Microsoft\Windows\InstallService\'; Name = 'WakeUpAndScanForUpdates' },
      @{ Path = '\Microsoft\Windows\Shell\'; Name = 'FamilySafetyMonitor' },
      @{ Path = '\Microsoft\Windows\Shell\'; Name = 'FamilySafetyRefreshTask' },
      @{ Path = '\Microsoft\Windows\Windows Defender\'; Name = 'Windows Defender Cache Maintenance' },
      @{ Path = '\Microsoft\Windows\Windows Defender\'; Name = 'Windows Defender Cleanup' },
      @{ Path = '\Microsoft\Windows\Windows Defender\'; Name = 'Windows Defender Scheduled Scan' },
      @{ Path = '\Microsoft\Windows\Windows Defender\'; Name = 'Windows Defender Verification' },
      @{ Path = '\Microsoft\Windows\Windows Error Reporting\'; Name = 'QueueReporting' },
      @{ Path = '\Microsoft\Windows\Maintenance\'; Name = 'WinSAT' },
      @{ Path = '\Microsoft\EdgeUpdate\'; Name = 'MicrosoftEdgeUpdateTaskMachineCore' },
      @{ Path = '\Microsoft\EdgeUpdate\'; Name = 'MicrosoftEdgeUpdateTaskMachineUA' }
    )
  }

  function Set-LabMinimalUiAndPrivacyBaseline {
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' -Name DisableWindowsConsumerFeatures -Value 1
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' -Name DisableSoftLanding -Value 1
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' -Name DisableTailoredExperiencesWithDiagnosticData -Value 1
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' -Name DisableThirdPartySuggestions -Value 1
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' -Name DisableWindowsSpotlightFeatures -Value 1
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' -Name DisableConsumerAccountStateContent -Value 1
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\AdvertisingInfo' -Name DisabledByGroupPolicy -Value 1
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection' -Name AllowTelemetry -Value 0
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\System' -Name EnableActivityFeed -Value 0
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\System' -Name PublishUserActivities -Value 0
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\System' -Name UploadUserActivities -Value 0
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Windows Search' -Name AllowCortana -Value 0
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Windows Search' -Name AllowSearchToUseLocation -Value 0
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Windows Search' -Name DisableWebSearch -Value 1
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\WindowsInkWorkspace' -Name AllowSuggestedAppsInWindowsInkWorkspace -Value 0
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\WindowsStore' -Name AutoDownload -Value 2
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\WindowsStore' -Name RemoveWindowsStore -Value 1
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\EdgeUpdate' -Name UpdateDefault -Value 0
    Set-LabRegistryDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\EdgeUpdate' -Name AutoUpdateCheckPeriodMinutes -Value 0

    Set-LabRegistryDword -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo' -Name Enabled -Value 0
    Set-LabRegistryDword -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Privacy' -Name TailoredExperiencesWithDiagnosticDataEnabled -Value 0
    Set-LabRegistryDword -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Search' -Name BingSearchEnabled -Value 0
    Set-LabRegistryDword -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Search' -Name CortanaConsent -Value 0
    Set-LabRegistryDword -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects' -Name VisualFXSetting -Value 2
    Set-LabRegistryDword -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced' -Name TaskbarAnimations -Value 0
    Set-LabRegistryDword -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced' -Name ListviewAlphaSelect -Value 0
    Set-LabRegistryDword -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced' -Name ListviewShadow -Value 0
    Set-LabRegistryDword -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced' -Name ShowSyncProviderNotifications -Value 0
    Set-LabRegistryDword -Path 'HKCU:\Software\Microsoft\Windows\DWM' -Name EnableAeroPeek -Value 0
    Set-LabRegistryDword -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize' -Name EnableTransparency -Value 0

    foreach ($name in @(
      'ContentDeliveryAllowed',
      'FeatureManagementEnabled',
      'OemPreInstalledAppsEnabled',
      'PreInstalledAppsEnabled',
      'PreInstalledAppsEverEnabled',
      'RotatingLockScreenEnabled',
      'RotatingLockScreenOverlayEnabled',
      'SilentInstalledAppsEnabled',
      'SoftLandingEnabled',
      'SystemPaneSuggestionsEnabled',
      'SubscribedContent-310093Enabled',
      'SubscribedContent-338387Enabled',
      'SubscribedContent-338388Enabled',
      'SubscribedContent-338389Enabled',
      'SubscribedContent-338393Enabled',
      'SubscribedContent-353694Enabled',
      'SubscribedContent-353696Enabled'
    )) {
      Set-LabRegistryDword -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager' -Name $name -Value 0
    }

    Set-LabRegistryString -Path 'HKCU:\Control Panel\Desktop' -Name DragFullWindows -Value '0'
    Set-LabRegistryString -Path 'HKCU:\Control Panel\Desktop' -Name FontSmoothing -Value '2'
    Set-LabRegistryDword -Path 'HKCU:\Control Panel\Desktop' -Name FontSmoothingType -Value 2
    Set-LabRegistryString -Path 'HKCU:\Control Panel\Desktop' -Name MenuShowDelay -Value '0'
    Set-LabRegistryString -Path 'HKCU:\Control Panel\Desktop' -Name Wallpaper -Value ''
    Set-LabRegistryString -Path 'HKCU:\Control Panel\Desktop' -Name WallpaperStyle -Value '0'
    Set-LabRegistryString -Path 'HKCU:\Control Panel\Desktop' -Name TileWallpaper -Value '0'
    Set-LabRegistryString -Path 'HKCU:\Control Panel\Desktop\WindowMetrics' -Name MinAnimate -Value '0'
    try { rundll32.exe user32.dll,UpdatePerUserSystemParameters 1, True | Out-Null } catch {}
  }

  function Set-LabAutoLogin {
    param([string] $Username, [string] $Password)
    $winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    New-ItemProperty -Path $winlogon -Name AutoAdminLogon -PropertyType String -Value '1' -Force | Out-Null
    New-ItemProperty -Path $winlogon -Name ForceAutoLogon -PropertyType String -Value '1' -Force | Out-Null
    New-ItemProperty -Path $winlogon -Name DefaultUserName -PropertyType String -Value $Username -Force | Out-Null
    New-ItemProperty -Path $winlogon -Name DefaultPassword -PropertyType String -Value $Password -Force | Out-Null
    New-ItemProperty -Path $winlogon -Name DefaultDomainName -PropertyType String -Value $env:COMPUTERNAME -Force | Out-Null
  }

  function Set-LabNoLock {
    try {
      New-Item -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' -Force | Out-Null
      New-ItemProperty -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' -Name NoLockScreen -PropertyType DWord -Value 1 -Force | Out-Null
    } catch {}
    try {
      New-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -Name InactivityTimeoutSecs -PropertyType DWord -Value 0 -Force | Out-Null
    } catch {}
    try {
      New-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name ScreenSaveActive -PropertyType String -Value '0' -Force | Out-Null
      New-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name ScreenSaverIsSecure -PropertyType String -Value '0' -Force | Out-Null
      New-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name ScreenSaveTimeOut -PropertyType String -Value '0' -Force | Out-Null
    } catch {}
  }

  function Remove-LabAppxBloat {
    $pattern = 'Xbox|Bing|Zune|Clipchamp|Teams|Solitaire|Todos|Weather|GetHelp|Getstarted|YourPhone|3DViewer|MixedReality|People|Skype|OfficeHub|FeedbackHub|WindowsMaps|News|CommunicationsApps|OneConnect|Getstarted|MicrosoftOfficeHub|MicrosoftTeams'
    try {
      Get-AppxPackage -AllUsers | Where-Object { $_.Name -match $pattern } | ForEach-Object {
        try { Remove-AppxPackage -Package $_.PackageFullName -AllUsers -ErrorAction Stop } catch {}
      }
    } catch {}
    try {
      Get-AppxProvisionedPackage -Online | Where-Object { $_.DisplayName -match $pattern } | ForEach-Object {
        try { Remove-AppxProvisionedPackage -Online -PackageName $_.PackageName -ErrorAction Stop | Out-Null } catch {}
      }
    } catch {}
  }

  function Get-LabBaselineReport {
    $serviceNames = @(
      'WinDefend', 'WdNisSvc', 'Sense', 'SecurityHealthService', 'wscsvc',
      'AppXSvc', 'ClipSVC', 'InstallService', 'edgeupdate', 'edgeupdatem',
      'SysMain', 'WSearch', 'DiagTrack', 'DoSvc', 'WaaSMedicSvc', 'WerSvc',
      'RetailDemo', 'MapsBroker', 'WpnService', 'diagnosticshub.standardcollector.service',
      'DPS', 'DusmSvc', 'TrkWks', 'Spooler', 'XblAuthManager', 'XboxNetApiSvc',
      'dmwappushservice', 'PcaSvc'
    )
    $services = @()
    foreach ($serviceName in $serviceNames) {
      $services += Get-Service -Name $serviceName -ErrorAction SilentlyContinue |
        Select-Object Name, Status, StartType
    }
    $tasks = @()
    foreach ($task in Get-LabLeanScheduledTasks) {
      $tasks += Get-ScheduledTask -TaskPath $task.Path -TaskName $task.Name -ErrorAction SilentlyContinue |
        Select-Object TaskPath, TaskName, State
    }
    $registryChecks = [ordered]@{}
    foreach ($item in @(
      @{ Key = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent'; Name = 'DisableWindowsConsumerFeatures' },
      @{ Key = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection'; Name = 'AllowTelemetry' },
      @{ Key = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\AdvertisingInfo'; Name = 'DisabledByGroupPolicy' },
      @{ Key = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender'; Name = 'DisableAntiSpyware' },
      @{ Key = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender'; Name = 'DisableAntiVirus' },
      @{ Key = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender Security Center\Systray'; Name = 'HideSystray' },
      @{ Key = 'HKLM:\SOFTWARE\Policies\Microsoft\WindowsStore'; Name = 'RemoveWindowsStore' },
      @{ Key = 'HKLM:\SOFTWARE\Policies\Microsoft\EdgeUpdate'; Name = 'UpdateDefault' },
      @{ Key = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\System'; Name = 'EnableActivityFeed' },
      @{ Key = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Windows Search'; Name = 'AllowCortana' },
      @{ Key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo'; Name = 'Enabled' },
      @{ Key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager'; Name = 'SilentInstalledAppsEnabled' },
      @{ Key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects'; Name = 'VisualFXSetting' },
      @{ Key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced'; Name = 'TaskbarAnimations' },
      @{ Key = 'HKCU:\Control Panel\Desktop'; Name = 'FontSmoothing' },
      @{ Key = 'HKCU:\Control Panel\Desktop'; Name = 'Wallpaper' },
      @{ Key = 'HKCU:\Control Panel\Desktop\WindowMetrics'; Name = 'MinAnimate' }
    )) {
      $value = $null
      try { $value = (Get-ItemProperty -Path $item.Key -Name $item.Name -ErrorAction Stop).($item.Name) } catch {}
      $registryChecks[($item.Key + '\' + $item.Name)] = $value
    }
    return [ordered]@{
      schema = 'emulebb.windows-vm-guest-baseline.v1'
      generatedAtUtc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
      computerName = $env:COMPUTERNAME
      os = Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, BuildNumber
      services = $services
      scheduledTasks = $tasks
      registry = $registryChecks
    }
  }

  function Install-HideMe {
    param([string] $InstallDir, [string] $Username)
    $installer = 'C:\eMuleBBVmTest\hide-me-installer.exe'
    $hideMeExe = Join-Path $InstallDir 'Hide.me.exe'
    if (-not (Test-Path -LiteralPath $hideMeExe -PathType Leaf)) {
      $startInfo = [Diagnostics.ProcessStartInfo]::new()
      $startInfo.FileName = $installer
      $startInfo.Arguments = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-'
      $startInfo.UseShellExecute = $false
      $process = [Diagnostics.Process]::Start($startInfo)
      $process.WaitForExit()
      if ($process.ExitCode -notin @(0, 3010)) {
        throw ('hide.me installer failed with exit code ' + $process.ExitCode)
      }
      Get-Process -Name 'Hide.me' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    }
    $programFilesX86 = [Environment]::GetFolderPath('ProgramFilesX86')
    $programFiles = [Environment]::GetFolderPath('ProgramFiles')
    $candidates = @(
      $hideMeExe,
      (Join-Path $programFilesX86 'hide.me VPN\Hide.me.exe'),
      (Join-Path $programFiles 'hide.me VPN\Hide.me.exe')
    )
    $resolvedExe = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $resolvedExe) {
      throw 'hide.me executable was not found after installation.'
    }
    $settingsDir = Join-Path (Join-Path (Join-Path 'C:\Users' $Username) 'AppData\Roaming') 'Hide.me'
    New-Item -ItemType Directory -Force -Path $settingsDir | Out-Null
    Copy-Item -LiteralPath 'C:\eMuleBBVmTest\hide-me-vpn.settings' -Destination (Join-Path $settingsDir 'vpn.settings') -Force
  }

  function Install-Pwsh {
    param([string] $InstallDir)
    $pwshExe = Join-Path $InstallDir 'pwsh.exe'
    if (-not (Test-Path -LiteralPath $pwshExe -PathType Leaf)) {
      $arguments = @(
        '/i',
        'C:\eMuleBBVmTest\pwsh-installer.msi',
        '/qn',
        '/norestart',
        'ADD_PATH=1',
        'REGISTER_MANIFEST=1',
        'ENABLE_PSREMOTING=1',
        'USE_MU=0',
        'ENABLE_MU=0'
      )
      $process = Start-Process -FilePath 'msiexec.exe' -ArgumentList $arguments -Wait -PassThru
      if ($process.ExitCode -notin @(0, 3010)) {
        throw ('PowerShell 7 installer failed with exit code ' + $process.ExitCode)
      }
    }
    & $pwshExe -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion.ToString()' | Out-Null
  }

  function Install-DotNetDesktopRuntime {
    param([string] $RuntimeDir, [string] $InstallerPath)
    if (-not (Test-Path -LiteralPath $RuntimeDir -PathType Container)) {
      $process = Start-Process -FilePath $InstallerPath -ArgumentList @('/install', '/quiet', '/norestart') -Wait -PassThru
      if ($process.ExitCode -notin @(0, 3010)) {
        throw ('.NET Desktop Runtime installer failed with exit code ' + $process.ExitCode)
      }
    }
    if (-not (Test-Path -LiteralPath $RuntimeDir -PathType Container)) {
      throw ('.NET Desktop Runtime was not found after installation: ' + $RuntimeDir)
    }
  }

  function Install-VcRedistX64 {
    param([string] $RuntimeDll, [string] $InstallerPath)
    if (-not (Test-Path -LiteralPath $RuntimeDll -PathType Leaf)) {
      $process = Start-Process -FilePath $InstallerPath -ArgumentList @('/install', '/quiet', '/norestart') -Wait -PassThru
      if ($process.ExitCode -notin @(0, 3010, 1638)) {
        throw ('VC++ x64 redistributable installer failed with exit code ' + $process.ExitCode)
      }
    }
    if (-not (Test-Path -LiteralPath $RuntimeDll -PathType Leaf)) {
      throw ('VC++ x64 runtime DLL was not found after installation: ' + $RuntimeDll)
    }
  }

  function Install-PythonLiveHarnessDependencies {
    param([string] $PythonExe, [string[]] $Packages)
    if ($Packages.Count -eq 0) {
      return
    }
    $arguments = @('-m', 'pip', 'install', '--disable-pip-version-check') + $Packages
    $output = & $PythonExe @arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
      $details = ($output | ForEach-Object { $_.ToString() }) -join "`n"
      throw ('Python live harness dependency install failed with exit code ' + $LASTEXITCODE + ":`n" + $details)
    }
    $output | ForEach-Object { Write-Output $_ }
  }

  function Install-PlaywrightBrowserRuntime {
    param([string] $PythonExe, [string] $BrowserName)
    $output = & $PythonExe -m playwright install $BrowserName 2>&1
    if ($LASTEXITCODE -ne 0) {
      $details = ($output | ForEach-Object { $_.ToString() }) -join "`n"
      throw ('Playwright browser runtime install failed with exit code ' + $LASTEXITCODE + ":`n" + $details)
    }
    $output | ForEach-Object { Write-Output $_ }
  }

  Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope LocalMachine -Force
  New-Item -ItemType Directory -Force -Path C:\eMuleBBVmTest | Out-Null
  $pythonExe = Join-Path $pythonInstallDir 'python.exe'
  if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    $installer = 'C:\eMuleBBVmTest\python-installer.exe'
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash.ToLowerInvariant()
    if ($actualHash -ne $pythonInstallerSha256) {
      throw ('Python installer SHA256 mismatch. Expected ' + $pythonInstallerSha256 + ', got ' + $actualHash)
    }
    $process = Start-Process -FilePath $installer -ArgumentList @(
      '/quiet',
      'InstallAllUsers=1',
      'PrependPath=1',
      'Include_pip=1',
      'Include_test=0',
      ('TargetDir=' + $pythonInstallDir)
    ) -Wait -PassThru
    if ($process.ExitCode -ne 0) {
      throw ('Python installer failed with exit code ' + $process.ExitCode)
    }
  }
  & $pythonExe -m pip --version | Out-Null
  Install-PythonLiveHarnessDependencies -PythonExe $pythonExe -Packages $pythonLivePackages
  Install-VcRedistX64 -RuntimeDll $vcRedistX64RuntimeDll -InstallerPath 'C:\eMuleBBVmTest\vc-redist-x64.exe'
  Install-PlaywrightBrowserRuntime -PythonExe $pythonExe -BrowserName $playwrightBrowser
  Install-Pwsh -InstallDir $pwshInstallDir
  Install-DotNetDesktopRuntime -RuntimeDir $dotnetDesktopRuntimeDir -InstallerPath 'C:\eMuleBBVmTest\dotnet-desktop-runtime.exe'
  Install-DotNetDesktopRuntime -RuntimeDir $dotnetDesktopRuntimeX86Dir -InstallerPath 'C:\eMuleBBVmTest\dotnet-desktop-runtime-x86.exe'
  Install-HideMe -InstallDir $hideMeInstallDir -Username $guestUsername
  Set-LabLeanBaseline -PythonPath $pythonInstallDir -HideMePath $hideMeInstallDir -Username $guestUsername -Password $guestPassword
  Get-LabBaselineReport | ConvertTo-Json -Depth 8 | Set-Content -Path 'C:\eMuleBBVmTest\baseline-report.json' -Encoding UTF8
  } -ArgumentList $payload.pythonInstallerSha256, $payload.pythonInstallDir, $payload.pythonLivePackages, $payload.playwrightBrowser, $payload.hideMeInstallDir, $payload.pwshInstallDir, $payload.dotnetDesktopRuntimeDir, $payload.dotnetDesktopRuntimeX86Dir, $payload.vcRedistX64RuntimeDll, $payload.username, $payload.password | Out-Null
  if ($payload.baselineReportPath) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $payload.baselineReportPath) | Out-Null
    Copy-Item -FromSession $session -Path 'C:\eMuleBBVmTest\baseline-report.json' -Destination $payload.baselineReportPath -Force
  }
}
finally {
  if ($session) { Remove-PSSession $session }
}
Stop-VM -Name $payload.vmName -Force
Set-OfflineLabUltraLeanBaselineForVmVhd -VmName $payload.vmName -VhdPath $payload.vhdPath
Checkpoint-VM -Name $payload.vmName -SnapshotName $payload.checkpointName
"""


def _manual_vm_target_script() -> str:
    return r"""
$ErrorActionPreference = 'Stop'
$secure = ConvertTo-SecureString $payload.password -AsPlainText -Force
$credential = [pscredential]::new($payload.username, $secure)
$vm = Get-VM -Name $payload.vmName -ErrorAction Stop
if ($payload.switchName) {
  Connect-VMNetworkAdapter -VMName $payload.vmName -SwitchName $payload.switchName | Out-Null
}
if ($vm.State -ne 'Off') {
  Stop-VM -Name $payload.vmName -TurnOff -Force
}
$snapshot = Get-VMSnapshot -VMName $payload.vmName -Name $payload.checkpointName -ErrorAction Stop
Restore-VMSnapshot -VMSnapshot $snapshot -Confirm:$false | Out-Null
if ($payload.switchName) {
  Connect-VMNetworkAdapter -VMName $payload.vmName -SwitchName $payload.switchName | Out-Null
}
Start-VM -Name $payload.vmName | Out-Null
$deadline = (Get-Date).AddMinutes(10)
$session = $null
do {
  Start-Sleep -Seconds 3
  try {
    $session = New-PSSession -VMName $payload.vmName -Credential $credential -ErrorAction Stop
  } catch {
    if ((Get-Date) -gt $deadline) { throw }
  }
} while (-not $session)
try {
  $guestResult = Invoke-Command -Session $session -ScriptBlock {
    param($Provisioning, $InstallRoot, $ReleaseVersion, $StartApp)
    $ErrorActionPreference = 'Stop'
    $guestIp = [string] $Provisioning.guestIp
    $prefixLength = [int] $Provisioning.prefixLength
    $gateway = [string] $Provisioning.gateway
    $dnsServers = @($Provisioning.dns | ForEach-Object { [string] $_ } | Where-Object { $_ })
    $adapter = Get-NetAdapter | Where-Object { $_.Status -eq 'Up' } | Sort-Object InterfaceIndex | Select-Object -First 1
    if (-not $adapter) {
      $adapter = Get-NetAdapter | Sort-Object InterfaceIndex | Select-Object -First 1
    }
    if (-not $adapter) {
      throw 'Manual VM session could not find a network adapter.'
    }
    if ($guestIp -and $gateway -and $dnsServers.Count -gt 0) {
      Get-NetIPAddress -InterfaceIndex $adapter.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
      Get-NetRoute -InterfaceIndex $adapter.InterfaceIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
      New-NetIPAddress -InterfaceIndex $adapter.InterfaceIndex -IPAddress $guestIp -PrefixLength $prefixLength -DefaultGateway $gateway | Out-Null
      Set-DnsClientServerAddress -InterfaceIndex $adapter.InterfaceIndex -ServerAddresses $dnsServers
    }
    $packagesRoot = Join-Path $InstallRoot 'packages'
    $expandedRoot = Join-Path $InstallRoot 'package'
    $profileRoot = Join-Path $InstallRoot 'profile'
    $configRoot = Join-Path $profileRoot 'config'
    $incomingRoot = Join-Path $profileRoot 'incoming'
    $tempRoot = Join-Path $profileRoot 'temp'
    if (Test-Path -LiteralPath $InstallRoot) {
      Remove-Item -LiteralPath $InstallRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $packagesRoot, $expandedRoot, $configRoot, $incomingRoot, $tempRoot | Out-Null
    [ordered]@{
      schema = 'emulebb.windows-vm-manual-guest-preinstall.v1'
      releaseVersion = $ReleaseVersion
      generatedAtUtc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    } | ConvertTo-Json -Depth 5 | Set-Content -Path (Join-Path $InstallRoot 'manual-session.preinstall.json') -Encoding UTF8
  } -ArgumentList $payload.provisioning, $payload.installRoot, $payload.releaseVersion, $payload.startApp
  $guestZip = Invoke-Command -Session $session -ScriptBlock {
    param($InstallRoot, $PackageFileName)
    Join-Path (Join-Path $InstallRoot 'packages') $PackageFileName
  } -ArgumentList $payload.installRoot, (Split-Path -Leaf $payload.packageZip)
  Copy-Item -ToSession $session -Path $payload.packageZip -Destination $guestZip -Force
  $guestResult = Invoke-Command -Session $session -ScriptBlock {
    param($InstallRoot, $GuestZip, $ReleaseVersion, $StartApp)
    $ErrorActionPreference = 'Stop'
    $expandedRoot = Join-Path $InstallRoot 'package'
    $profileRoot = Join-Path $InstallRoot 'profile'
    $configRoot = Join-Path $profileRoot 'config'
    $incomingRoot = Join-Path $profileRoot 'incoming'
    $tempRoot = Join-Path $profileRoot 'temp'
    Expand-Archive -LiteralPath $GuestZip -DestinationPath $expandedRoot -Force
    $appRoot = Join-Path $expandedRoot 'eMuleBB'
    $appExe = Join-Path $appRoot 'emulebb.exe'
    if (-not (Test-Path -LiteralPath $appExe -PathType Leaf)) {
      throw ('Manual VM package did not contain eMuleBB\emulebb.exe: ' + $GuestZip)
    }
    @"
[eMule]
ConfirmExit=0
IncomingDir=$incomingRoot
TempDir=$tempRoot
BindAddr=
BindInterface=
NetworkED2K=0
NetworkKademlia=0
[WebServer]
Enabled=1
ApiKey=manual-vm-api-key
Port=4711
BindAddr=127.0.0.1
UseHTTPS=0
[UPnP]
EnableUPnP=0
"@ | Set-Content -Path (Join-Path $configRoot 'preferences.ini') -Encoding Unicode
    $desktop = [Environment]::GetFolderPath('Desktop')
    $shortcutPath = Join-Path $desktop 'eMuleBB Manual.lnk'
    $rootShortcutPath = Join-Path $desktop 'eMuleBB Manual Root.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $appExe
    $shortcut.Arguments = '-ignoreinstances -c "' + $profileRoot + '"'
    $shortcut.WorkingDirectory = $appRoot
    $shortcut.Save()
    $rootShortcut = $shell.CreateShortcut($rootShortcutPath)
    $rootShortcut.TargetPath = 'explorer.exe'
    $rootShortcut.Arguments = '"' + $InstallRoot + '"'
    $rootShortcut.Save()
    $processId = $null
    if ($StartApp) {
      $process = Start-Process -FilePath $appExe -ArgumentList @('-ignoreinstances', '-c', $profileRoot) -WorkingDirectory $appRoot -PassThru
      $processId = $process.Id
    }
    $ipv4 = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
      Where-Object { $_.IPAddress -notlike '169.254.*' } |
      Select-Object IPAddress, PrefixLength, InterfaceAlias
    $result = [ordered]@{
      schema = 'emulebb.windows-vm-manual-guest-session.v1'
      status = 'passed'
      generatedAtUtc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
      releaseVersion = $ReleaseVersion
      computerName = $env:COMPUTERNAME
      username = $env:USERNAME
      guestIp = ($ipv4 | Select-Object -First 1).IPAddress
      installRoot = $InstallRoot
      packageZip = $GuestZip
      appRoot = $appRoot
      appExe = $appExe
      profileDir = $profileRoot
      desktopShortcut = $shortcutPath
      rootShortcut = $rootShortcutPath
      startedProcessId = $processId
      ipv4 = $ipv4
    }
    $result | ConvertTo-Json -Depth 8 | Set-Content -Path (Join-Path $InstallRoot 'manual-session.json') -Encoding UTF8
    $result
  } -ArgumentList $payload.installRoot, $guestZip, $payload.releaseVersion, $payload.startApp
  New-Item -ItemType Directory -Force -Path $payload.hostReportDir | Out-Null
  Copy-Item -FromSession $session -Path (Join-Path $payload.installRoot 'manual-session.json') -Destination (Join-Path $payload.hostReportDir 'manual-session.json') -Force
  $guestResult | ConvertTo-Json -Depth 8
}
finally {
  if ($session) { Remove-PSSession $session }
}
"""


def _release_package_zip(layout: WorkspaceLayout, release_version: str, platform: str) -> Path:
    arch = "arm64" if platform == "ARM64" else "x64"
    return layout.output_release_root / f"emulebb-v{release_version}" / f"emulebb-{release_version}-{arch}.zip"


def _local_swarm_release_asset_paths(layout: WorkspaceLayout, release_version: str, platform: str) -> tuple[Path, ...]:
    arch = "arm64" if platform == "ARM64" else "x64"
    release_root = layout.output_release_root / f"emulebb-v{release_version}"
    return (
        release_root / f"emulebb-{release_version}-{arch}.zip",
        release_root / f"emulebb-{release_version}-{arch}.manifest.json",
    )


def _local_swarm_dependency_asset_paths(layout: WorkspaceLayout) -> tuple[Path, ...]:
    return tuple(Path(entry["path"]) for entry in _local_swarm_dependency_manifest_entries(layout).values())


def _local_swarm_dependency_manifest_entries(layout: WorkspaceLayout) -> dict[str, dict[str, str]]:
    live_wire_inputs = layout.tests_repo_root / "live-wire-inputs.local.json"
    if not live_wire_inputs.is_file():
        return {}
    try:
        payload = json.loads(live_wire_inputs.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Windows VM local-swarm live-wire inputs are invalid JSON: {live_wire_inputs}") from exc
    local_install = payload.get("local_package_install")
    if not isinstance(local_install, dict):
        return {}
    raw_manifest = local_install.get("dependency_manifest")
    if not isinstance(raw_manifest, str) or not raw_manifest.strip():
        return {}
    manifest_path = Path(raw_manifest).expanduser()
    if not manifest_path.is_absolute():
        manifest_path = (layout.emule_workspace_root / manifest_path).resolve()
    if not manifest_path.is_file():
        raise RuntimeError(f"Windows VM local-swarm dependency manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Windows VM local-swarm dependency manifest is invalid JSON: {manifest_path}") from exc
    entries: dict[str, dict[str, str]] = {}
    for name in ("prowlarr", "radarr", "sonarr"):
        item = manifest.get(name)
        if not isinstance(item, dict):
            raise RuntimeError(f"Windows VM local-swarm dependency manifest is missing {name!r}: {manifest_path}")
        raw_path = item.get("path")
        raw_url = item.get("url")
        raw_location = raw_path if isinstance(raw_path, str) and raw_path.strip() else raw_url
        if not isinstance(raw_location, str) or not raw_location.strip():
            raise RuntimeError(f"Windows VM local-swarm dependency {name!r} is missing a local path or URL: {manifest_path}")
        dependency_path = _path_from_local_dependency_url(raw_location)
        if not dependency_path.is_file():
            raise RuntimeError(f"Windows VM local-swarm dependency asset is missing: {dependency_path}")
        entries[name] = {
            "path": str(dependency_path),
            "fileName": dependency_path.name,
            "repo": str(item.get("repo") or ""),
            "tag": str(item.get("tag") or "local"),
            "assetPattern": str(item.get("assetPattern") or ".*\\.zip$"),
            "exeName": str(item.get("exeName") or ""),
            "sha256": str(item.get("sha256") or ""),
        }
    return entries


def _path_from_local_dependency_url(raw_url: str) -> Path:
    candidate = Path(raw_url)
    if candidate.is_file():
        return candidate.resolve()
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme.lower() == "file":
        return Path(urllib.request.url2pathname(parsed.path)).resolve()
    raise RuntimeError(f"Windows VM local-swarm dependency URL must be a local file path: {raw_url}")






def _query_vm_lab_hyperv_disks(
    *,
    configured_vm_names: Sequence[str],
    vm_name_pattern: str,
) -> list[dict[str, object]]:
    names_json = json.dumps(list(configured_vm_names))
    escaped_pattern = _escape_powershell_single_quoted(vm_name_pattern)
    script = rf"""
$ErrorActionPreference = 'Stop'
$names = ConvertFrom-Json @'
{names_json}
'@
$vms = @()
foreach ($name in $names) {{
  $vm = Get-VM -Name $name -ErrorAction SilentlyContinue
  if ($vm) {{ $vms += $vm }}
}}
$patternVms = Get-VM -Name '{escaped_pattern}' -ErrorAction SilentlyContinue
if ($patternVms) {{ $vms += $patternVms }}
$seen = @{{}}
$rows = @()
foreach ($vm in $vms) {{
  if ($seen.ContainsKey($vm.Name)) {{ continue }}
  $seen[$vm.Name] = $true
  foreach ($drive in Get-VMHardDiskDrive -VMName $vm.Name) {{
    $diskPath = [string]$drive.Path
    $rows += [pscustomobject]@{{
      vmName = [string]$vm.Name
      state = [string]$vm.State
      vmPath = [string]$vm.Path
      diskPath = $diskPath
      diskExists = [bool](Test-Path -LiteralPath $diskPath)
    }}
  }}
}}
$rows | ConvertTo-Json -Depth 4
"""
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Unable to query Hyper-V VM lab disks: {completed.stderr.strip()}")
    payload = (completed.stdout or "").strip()
    if not payload:
        return []
    parsed = json.loads(payload)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise RuntimeError("Hyper-V VM lab disk query returned unexpected JSON.")
    return [entry for entry in parsed if isinstance(entry, dict)]


def _escape_powershell_single_quoted(value: str) -> str:
    return value.replace("'", "''")


def _vm_image_root(layout: WorkspaceLayout) -> Path:
    return layout.output_artifacts_root / "vm-lab" / "images"


def _vm_prepare_report_root(layout: WorkspaceLayout) -> Path:
    return layout.output_reports_root / "vm-lab" / "prepare"


def _vm_manual_report_root(layout: WorkspaceLayout) -> Path:
    return layout.output_reports_root / "vm-lab" / "manual"


def _windows_vm_report_root(layout: WorkspaceLayout) -> Path:
    return layout.output_reports_root / WINDOWS_VM_SUITE_NAME


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_config_path(layout: WorkspaceLayout, config_file: str | None) -> Path:
    raw = config_file or DEFAULT_CONFIG_FILE_NAME
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve()
    workspace_relative = (layout.emule_workspace_root / candidate).resolve()
    if workspace_relative.exists():
        return workspace_relative
    return (layout.build_repo_root / candidate).resolve()


def _parse_target(config_path: Path, key: str, raw: object) -> VmTargetSettings:
    if not isinstance(raw, dict):
        raise RuntimeError(f"Windows VM lab config target {key!r} must be an object: {config_path}")
    return VmTargetSettings(
        key=key,
        vm_name=_required_string(raw, "vm_name"),
        iso_path=Path(_required_string(raw, "iso_path")).expanduser().resolve(),
        edition=_required_string(raw, "edition"),
    )


def resolve_guest_password(config: VmLabConfig) -> str:
    """Returns the configured guest password from environment or local config."""

    if config.guest.password_env:
        value = os.environ.get(config.guest.password_env, "")
        if value:
            return value
    return config.guest.password


def _ps_with_payload(payload: dict[str, object], body: str) -> str:
    payload_json = json.dumps(payload)
    return "$payload = @'\n" + payload_json + "\n'@ | ConvertFrom-Json\n" + body


def _parse_json_output(stdout: str, label: str) -> dict[str, object]:
    text = stdout.strip()
    if not text:
        raise RuntimeError(f"{label} did not return JSON output.")
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError(f"{label} did not return a JSON object:\n{text}")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} returned non-object JSON.")
    return payload


def _refresh_latest(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _load_json_object(path: Path, description: str) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(
            f"{description} is missing: {path}. "
            f"Copy {EXAMPLE_CONFIG_FILE_NAME} to {DEFAULT_CONFIG_FILE_NAME} and update local ISO paths."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{description} is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{description} must contain one JSON object: {path}")
    return payload


def _required_object(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise RuntimeError(f"Windows VM lab config field {key!r} must be an object.")
    return value


def _optional_object(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, {})
    if not isinstance(value, dict):
        raise RuntimeError(f"Windows VM lab config field {key!r} must be an object.")
    return value


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Windows VM lab config field {key!r} must be a non-empty string.")
    return value.strip()


def _optional_string(payload: dict[str, Any], key: str, default: str) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str) or (default and not value.strip()):
        raise RuntimeError(f"Windows VM lab config field {key!r} must be a non-empty string.")
    return value.strip()


def _optional_string_tuple(payload: dict[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = payload.get(key, list(default))
    if not isinstance(value, list) or not value:
        raise RuntimeError(f"Windows VM lab config field {key!r} must be a non-empty string list.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise RuntimeError(f"Windows VM lab config field {key!r} must contain only non-empty strings.")
        result.append(item.strip())
    return tuple(result)


def _optional_string_map(payload: dict[str, Any], key: str, default: dict[str, str]) -> dict[str, str]:
    value = payload.get(key, default)
    if not isinstance(value, dict) or not value:
        raise RuntimeError(f"Windows VM lab config field {key!r} must be a non-empty string map.")
    result: dict[str, str] = {}
    for map_key, map_value in value.items():
        if not isinstance(map_key, str) or not map_key.strip():
            raise RuntimeError(f"Windows VM lab config field {key!r} must contain only non-empty string keys.")
        if not isinstance(map_value, str) or not map_value.strip():
            raise RuntimeError(f"Windows VM lab config field {key!r} must contain only non-empty string values.")
        result[map_key.strip()] = map_value.strip()
    return result


def _optional_positive_int(payload: dict[str, Any], key: str, default: int) -> int:
    value = payload.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeError(f"Windows VM lab config field {key!r} must be a positive integer.")
    return value


def _sha256(path: Path) -> str:
    hasher = __import__("hashlib").sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
