"""Installer-backed local hammer campaign orchestration."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .artifact_names import local_hammer_campaign_result_file_name, utc_run_id
from .config import LiveE2eOptions, LocalHammerCampaignOptions, LocalPackageInstallOptions, WorkspaceOptions
from .layout import WorkspaceLayout
from .local_package_install import (
    MaterializedLocalInstall,
    load_local_install_config,
    materialize_test_local_install,
    resolve_install_artifacts,
)
from .process import get_python_invocation
from .test_runs import invoke_live_e2e_suite

REPORT_SCHEMA = "emulebb.local-hammer-campaign.v1"
CAMPAIGN_ID = "overnight-local-heavy-hammer"


@dataclass(frozen=True)
class HammerPhase:
    """One ordered local hammer campaign phase."""

    name: str
    category: str
    command: str
    live_options: LiveE2eOptions | None = None
    refresh_repeatability: bool = False


@dataclass
class HammerPhaseResult:
    """Recorded result for one attempted campaign phase."""

    cycle: int
    name: str
    category: str
    command: str
    status: str
    attempts: int
    duration_seconds: float
    error: str = ""
    diagnostics: tuple[Path, ...] = ()
    report_paths: tuple[Path, ...] = ()
    refresh: dict[str, Any] | None = None


class LocalHammerCampaignError(RuntimeError):
    """Raised when the local hammer campaign stops on a repeated failure."""


def invoke_local_hammer_campaign(
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    campaign_options: LocalHammerCampaignOptions,
) -> None:
    """Runs an installer-backed local-only hammer campaign until deadline or cycle cap."""

    if workspace_options.configuration != "Release" or workspace_options.platform != "x64":
        raise RuntimeError("The local hammer campaign runs Release/x64 only.")

    report_dir = _new_report_dir(layout)
    report_dir.mkdir(parents=True, exist_ok=False)
    latest_dir = layout.output_reports_root / "hammer-campaign-runs" / "latest"
    started_at = datetime.now(timezone.utc)
    deadline = _resolve_deadline(campaign_options)
    phases = build_hammer_phase_plan(campaign_options)
    results: list[HammerPhaseResult] = []
    preflight: dict[str, Any] = {"status": "planned" if campaign_options.dry_run else "pending"}
    status = "running"

    try:
        if campaign_options.dry_run:
            preflight = {"status": "planned"}
        else:
            preflight = _run_preflight(layout, workspace_options, campaign_options)
        _write_report(report_dir, latest_dir, layout, workspace_options, campaign_options, started_at, deadline, phases, results, preflight, status=status)

        cycle = 0
        while _should_start_cycle(cycle, deadline, campaign_options.max_cycles):
            cycle += 1
            for phase in phases:
                if not _should_start_phase(deadline):
                    status = "deadline-reached"
                    break
                result = _run_phase_with_retry(layout, workspace_options, campaign_options, phase, cycle, report_dir)
                results.append(result)
                _write_report(
                    report_dir,
                    latest_dir,
                    layout,
                    workspace_options,
                    campaign_options,
                    started_at,
                    deadline,
                    phases,
                    results,
                    preflight,
                    status="running",
                )
                if result.status == "failed":
                    raise LocalHammerCampaignError(f"Local hammer phase '{phase.name}' failed twice: {result.error}")
            if status == "deadline-reached":
                break
            if campaign_options.cycle_pause_seconds > 0 and _should_start_phase(deadline):
                time.sleep(campaign_options.cycle_pause_seconds)

        if status == "running":
            status = _aggregate_status(results, dry_run=campaign_options.dry_run)
        _write_report(report_dir, latest_dir, layout, workspace_options, campaign_options, started_at, deadline, phases, results, preflight, status=status)
        print("")
        print(f"Local hammer campaign: {CAMPAIGN_ID}")
        print(f"Status: {status}")
        print(f"Cycles: {max((result.cycle for result in results), default=0)}")
        print(f"Report: {report_dir / local_hammer_campaign_result_file_name()}")
        if status == "failed":
            raise LocalHammerCampaignError(f"Local hammer campaign '{CAMPAIGN_ID}' completed with failures.")
    except Exception:
        if not results and status == "running":
            status = "failed"
        elif status == "running":
            status = _aggregate_status(results, dry_run=campaign_options.dry_run)
        _write_report(report_dir, latest_dir, layout, workspace_options, campaign_options, started_at, deadline, phases, results, preflight, status=status)
        print(f"Local hammer campaign report: {report_dir / local_hammer_campaign_result_file_name()}")
        raise


def build_hammer_phase_plan(campaign_options: LocalHammerCampaignOptions) -> tuple[HammerPhase, ...]:
    """Returns the ordered smoke-to-heavy local hammer phase matrix."""

    common = _materialized_common_options(campaign_options)
    return (
        HammerPhase(
            name="installer-command-line-smoke",
            category="smoke",
            command=_live_command(campaign_options, "--suite command-line-smoke --test-network offline --fail-fast"),
            live_options=LiveE2eOptions(
                **common,
                suites=("command-line-smoke",),
                test_network="offline",
                fail_fast=True,
            ),
        ),
        HammerPhase(
            name="quick-godzilla-local-swarm",
            category="swarm",
            command=_live_command(
                campaign_options,
                "--suite godzilla-local-swarm --test-network lan --admin-volume-fixtures --godzilla-stage launch-scale "
                "--godzilla-total-client-count 4 --godzilla-peer-transfer-count 24 --godzilla-harness-transfer-count 24 "
                "--godzilla-emulebb-files 80 --godzilla-extra-emulebb-files 8 --godzilla-harness-files 60 "
                "--fail-fast",
            ),
            live_options=_godzilla_options(
                common,
                campaign_options,
                total_clients=4,
                peer_transfers=24,
                harness_transfers=24,
                emulebb_files=80,
                extra_emulebb_files=8,
                harness_files=60,
                amule_files=20,
                kill_cycles=0,
                stage="launch-scale",
                fail_fast=True,
            ),
        ),
        HammerPhase(
            name="medium-godzilla-local-swarm",
            category="swarm",
            command=_live_command(
                campaign_options,
                "--suite godzilla-local-swarm --test-network lan --admin-volume-fixtures --godzilla-stage launch-scale "
                "--godzilla-total-client-count 10 --godzilla-peer-transfer-count 120 --godzilla-harness-transfer-count 120 "
                "--godzilla-emulebb-files 240 --godzilla-extra-emulebb-files 24 --godzilla-harness-files 180 "
                "--godzilla-cpu-profile",
            ),
            live_options=_godzilla_options(
                common,
                campaign_options,
                total_clients=10,
                peer_transfers=120,
                harness_transfers=120,
                emulebb_files=240,
                extra_emulebb_files=24,
                harness_files=180,
                amule_files=60,
                kill_cycles=0,
                stage="launch-scale",
                fail_fast=False,
                cpu_profile=True,
            ),
        ),
        HammerPhase(
            name="heavy-godzilla-local-swarm",
            category="hammer",
            command=_live_command(
                campaign_options,
                "--suite godzilla-local-swarm --test-network lan --admin-volume-fixtures --godzilla-stage full "
                "--godzilla-total-client-count 18 --godzilla-peer-transfer-count 360 --godzilla-harness-transfer-count 360 "
                "--godzilla-emulebb-files 720 --godzilla-extra-emulebb-files 72 --godzilla-harness-files 480 "
                "--godzilla-adverse-kill-cycles 2 --godzilla-cpu-profile",
            ),
            live_options=_godzilla_options(
                common,
                campaign_options,
                total_clients=18,
                peer_transfers=360,
                harness_transfers=360,
                emulebb_files=720,
                extra_emulebb_files=72,
                harness_files=480,
                amule_files=120,
                kill_cycles=2,
                stage="full",
                fail_fast=False,
                cpu_profile=True,
            ),
        ),
        HammerPhase(
            name="large-library-resource-hammer",
            category="large-library",
            command=_live_command(
                campaign_options,
                "--suite resource-ui-smoke --suite preference-ui --suite shared-files-ui --test-network offline "
                "--preference-ui-directories-tree-stress --shared-files-ui-scenario tree-refresh-stress-50k "
                "--shared-files-tree-stress-churn-cycles 3 --profile-cpu --profile-cpu-stack --profile-memory --fail-fast",
            ),
            live_options=LiveE2eOptions(
                **common,
                suites=("resource-ui-smoke", "preference-ui", "shared-files-ui"),
                test_network="offline",
                fail_fast=True,
                preference_ui_directories_tree_stress=True,
                shared_files_ui_scenarios=("tree-refresh-stress-50k",),
                shared_files_tree_stress_churn_cycles=3,
                profile_cpu=True,
                profile_cpu_stack=True,
                profile_symbols_required=campaign_options.profile_symbols_required,
                profile_memory=True,
            ),
        ),
        HammerPhase(
            name="refresh-repeatability-check",
            category="refresh",
            command="python -m emule_workspace install-local-package refresh-repeatability via materialize_test_local_install",
            refresh_repeatability=True,
        ),
    )


def _materialized_common_options(campaign_options: LocalHammerCampaignOptions) -> dict[str, Any]:
    return {
        "materialize_test_install": True,
        "materialize_test_install_release_version": campaign_options.release_version,
        "materialize_test_install_clean": campaign_options.clean,
        "materialize_test_install_skip_build": campaign_options.skip_build,
        "live_wire_inputs_file": campaign_options.live_wire_inputs_file,
        "skip_live_seed_refresh": True,
        "p2p_bind_interface_name": campaign_options.p2p_bind_interface_name,
    }


def _godzilla_options(
    common: dict[str, Any],
    campaign_options: LocalHammerCampaignOptions,
    *,
    total_clients: int,
    peer_transfers: int,
    harness_transfers: int,
    emulebb_files: int,
    extra_emulebb_files: int,
    harness_files: int,
    amule_files: int,
    kill_cycles: int,
    stage: str,
    fail_fast: bool,
    cpu_profile: bool = False,
) -> LiveE2eOptions:
    return LiveE2eOptions(
        **common,
        suites=("godzilla-local-swarm",),
        test_network="lan",
        fail_fast=fail_fast,
        admin_volume_fixtures=True,
        godzilla_p2p_bind_interface_address=campaign_options.godzilla_p2p_bind_interface_address,
        godzilla_stage=stage,
        godzilla_total_client_count=total_clients,
        godzilla_peer_transfer_count=peer_transfers,
        godzilla_harness_transfer_count=harness_transfers,
        godzilla_emulebb_files=emulebb_files,
        godzilla_extra_emulebb_files=extra_emulebb_files,
        godzilla_harness_files=harness_files,
        godzilla_amule_files=amule_files,
        godzilla_adverse_kill_cycles=kill_cycles,
        godzilla_adverse_kill_warmup_seconds=20.0 if kill_cycles else 0.0,
        godzilla_adverse_recovery_timeout_seconds=180.0,
        godzilla_cpu_profile=cpu_profile,
        profile_symbols_required=campaign_options.profile_symbols_required,
    )


def _live_command(campaign_options: LocalHammerCampaignOptions, suffix: str) -> str:
    command = (
        "python -m emule_workspace test live-e2e --materialize-test-install "
        f"--materialize-test-install-release-version {campaign_options.release_version}"
    )
    if campaign_options.skip_build:
        command += " --materialize-test-install-skip-build"
    if campaign_options.clean:
        command += " --materialize-test-install-clean"
    if campaign_options.live_wire_inputs_file:
        command += f" --live-wire-inputs-file {campaign_options.live_wire_inputs_file}"
    if campaign_options.godzilla_p2p_bind_interface_address and "godzilla-local-swarm" in suffix:
        command += f" --godzilla-p2p-bind-interface-address {campaign_options.godzilla_p2p_bind_interface_address}"
    if not campaign_options.profile_symbols_required:
        command += " --no-profile-symbols-required"
    return f"{command} {suffix}"


def _run_preflight(
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    campaign_options: LocalHammerCampaignOptions,
) -> dict[str, Any]:
    config = load_local_install_config(layout, campaign_options.live_wire_inputs_file)
    artifacts = resolve_install_artifacts(layout, workspace_options, campaign_options.release_version)
    return {
        "status": "passed",
        "liveWireInputsFile": str(config.live_wire_inputs_file),
        "localInstallTargetPath": str(config.target_path),
        "importProfileDir": str(config.import_profile_dir) if config.import_profile_dir else None,
        "releaseVersion": campaign_options.release_version,
        "artifacts": {
            "emuleZip": str(artifacts.emule_zip),
            "packageExe": str(artifacts.package_exe),
            "packagePdb": str(artifacts.package_pdb),
        },
    }


def _run_phase_with_retry(
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    campaign_options: LocalHammerCampaignOptions,
    phase: HammerPhase,
    cycle: int,
    report_dir: Path,
) -> HammerPhaseResult:
    started = time.monotonic()
    reports_root = _test_reports_root(layout)
    before_reports = _report_directory_snapshot(reports_root)
    attempts = 0
    diagnostics: list[Path] = []
    errors: list[str] = []

    while attempts < 2:
        attempts += 1
        try:
            refresh_payload = None
            if not campaign_options.dry_run:
                if phase.refresh_repeatability:
                    refresh_payload = _run_refresh_repeatability(layout, workspace_options, campaign_options, report_dir)
                elif phase.live_options is not None:
                    invoke_live_e2e_suite(layout, workspace_options, phase.live_options)
            return HammerPhaseResult(
                cycle=cycle,
                name=phase.name,
                category=phase.category,
                command=phase.command,
                status="planned" if campaign_options.dry_run else "passed",
                attempts=attempts,
                duration_seconds=round(time.monotonic() - started, 3),
                diagnostics=tuple(diagnostics),
                report_paths=_changed_report_paths(reports_root, before_reports),
                refresh=refresh_payload,
            )
        except Exception as exc:
            errors.append(str(exc))
            diagnostics.append(_collect_failure_diagnostics(layout, report_dir, phase.name, cycle, attempts, str(exc)))
            _stop_running_tests(layout, report_dir, phase.name, cycle, attempts)

    same_failure = len(set(errors)) == 1
    return HammerPhaseResult(
        cycle=cycle,
        name=phase.name,
        category=phase.category,
        command=phase.command,
        status="failed" if same_failure else "intermittent-failed",
        attempts=attempts,
        duration_seconds=round(time.monotonic() - started, 3),
        error=errors[-1],
        diagnostics=tuple(diagnostics),
        report_paths=_changed_report_paths(reports_root, before_reports),
    )


def _run_refresh_repeatability(
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    campaign_options: LocalHammerCampaignOptions,
    report_dir: Path,
) -> dict[str, Any]:
    install_options = LocalPackageInstallOptions(
        live_wire_inputs_file=campaign_options.live_wire_inputs_file,
        release_version=campaign_options.release_version,
        clean=campaign_options.clean,
        skip_build=campaign_options.skip_build,
    )
    run_id = report_dir.name
    first = materialize_test_local_install(
        layout,
        workspace_options,
        install_options,
        run_id=run_id,
        suite_name="refresh-repeatability",
        client_id="primary",
    )
    first_manifest = _load_suite_install_manifest(first)
    second = materialize_test_local_install(
        layout,
        workspace_options,
        install_options,
        run_id=run_id,
        suite_name="refresh-repeatability",
        client_id="primary",
    )
    second_manifest = _load_suite_install_manifest(second)
    _assert_materialized_refresh_ok(second, second_manifest)
    return {
        "installRoot": str(second.target_path),
        "firstProfileImportAction": first_manifest.get("profileImport", {}).get("action"),
        "secondProfileImportAction": second_manifest.get("profileImport", {}).get("action"),
        "adjacentPdb": str(second.app_root / "emulebb.pdb"),
        "profileSeedConfigDir": str(second.profile_seed_config_dir),
        "manifest": str(second.manifest_path),
    }


def _assert_materialized_refresh_ok(materialized: MaterializedLocalInstall, suite_manifest: dict[str, Any]) -> None:
    profile_action = suite_manifest.get("profileImport", {}).get("action")
    if profile_action != "skipped-existing":
        raise RuntimeError(f"Refresh repeatability expected profile import skipped-existing, got {profile_action!r}.")
    adjacent_pdb = materialized.app_root / "emulebb.pdb"
    if not adjacent_pdb.is_file():
        raise RuntimeError(f"Refresh repeatability missing adjacent PDB: {adjacent_pdb}")
    expected_seed_files = {"preferences.ini", "preferences.dat", "server.met", "nodes.dat"}
    seed_files = {path.name for path in materialized.profile_seed_config_dir.iterdir() if path.is_file()}
    if seed_files != expected_seed_files:
        raise RuntimeError(f"Refresh repeatability profile seed drifted: {sorted(seed_files)}")


def _load_suite_install_manifest(materialized: MaterializedLocalInstall) -> dict[str, Any]:
    suite_manifest_path = materialized.target_path / "manifests" / "suite-install.json"
    if not suite_manifest_path.is_file():
        raise RuntimeError(f"Suite install manifest is missing: {suite_manifest_path}")
    return json.loads(suite_manifest_path.read_text(encoding="utf-8-sig"))


def _collect_failure_diagnostics(
    layout: WorkspaceLayout,
    report_dir: Path,
    phase_name: str,
    cycle: int,
    attempt: int,
    error: str,
) -> Path:
    diagnostics_dir = report_dir / "diagnostics" / f"cycle-{cycle:02d}" / phase_name / f"attempt-{attempt:02d}"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "phase": phase_name,
        "cycle": cycle,
        "attempt": attempt,
        "error": error,
        "collectedAtUtc": datetime.now(timezone.utc).isoformat(),
        "processes": _process_snapshot(),
        "latestReports": [str(path) for path in _latest_report_paths(layout)],
        "testInstalls": [str(path) for path in _recent_test_installs(layout)],
    }
    path = diagnostics_dir / "failure-diagnostics.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def _process_snapshot() -> list[dict[str, str]]:
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'emulebb|emule|python' } | "
                "Select-Object Name,ProcessId,CommandLine | ConvertTo-Json -Depth 3",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        return [{"error": str(exc)}]
    if completed.returncode != 0:
        return [{"error": completed.stderr.strip() or completed.stdout.strip()}]
    if not completed.stdout.strip():
        return []
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return [{"raw": completed.stdout.strip()}]
    if isinstance(payload, list):
        return [{str(key): str(value) for key, value in item.items()} for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [{str(key): str(value) for key, value in payload.items()}]
    return [{"raw": str(payload)}]


def _stop_running_tests(layout: WorkspaceLayout, report_dir: Path, phase_name: str, cycle: int, attempt: int) -> None:
    script = layout.tests_repo_root / "scripts" / "stop-running-tests.py"
    if not script.is_file():
        return
    diagnostics_dir = report_dir / "diagnostics" / f"cycle-{cycle:02d}" / phase_name / f"attempt-{attempt:02d}"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    python = get_python_invocation()
    completed = subprocess.run(
        python.command([script, "--json"]),
        cwd=str(layout.emule_workspace_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    (diagnostics_dir / "cleanup-output.txt").write_text(completed.stdout, encoding="utf-8", newline="\n")


def _latest_report_paths(layout: WorkspaceLayout) -> list[Path]:
    reports_root = _test_reports_root(layout)
    if not reports_root.is_dir():
        return []
    return sorted(path for path in reports_root.glob("*/latest") if path.is_dir())


def _recent_test_installs(layout: WorkspaceLayout) -> list[Path]:
    installs_root = layout.output_tmp_root / "test-installs"
    if not installs_root.is_dir():
        return []
    return sorted((path for path in installs_root.iterdir() if path.is_dir()), key=lambda path: path.stat().st_mtime, reverse=True)[:10]


def _resolve_deadline(campaign_options: LocalHammerCampaignOptions) -> datetime:
    tz = ZoneInfo(campaign_options.timezone_str)
    if campaign_options.until_local:
        parsed = datetime.fromisoformat(campaign_options.until_local)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(timezone.utc)
    now = datetime.now(tz)
    candidate = datetime.combine(now.date(), datetime_time(hour=5), tzinfo=tz)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _should_start_cycle(cycle: int, deadline: datetime, max_cycles: int) -> bool:
    if max_cycles > 0 and cycle >= max_cycles:
        return False
    return datetime.now(timezone.utc) < deadline


def _should_start_phase(deadline: datetime) -> bool:
    return datetime.now(timezone.utc) < deadline


def _aggregate_status(results: list[HammerPhaseResult], *, dry_run: bool) -> str:
    if dry_run and all(result.status == "planned" for result in results):
        return "planned"
    if any(result.status == "failed" for result in results):
        return "failed"
    if any(result.status == "intermittent-failed" for result in results):
        return "degraded"
    return "passed"


def _new_report_dir(layout: WorkspaceLayout) -> Path:
    return layout.output_reports_root / "hammer-campaign-runs" / f"{utc_run_id()}-{CAMPAIGN_ID}"


def _test_reports_root(layout: WorkspaceLayout) -> Path:
    return layout.output_reports_root


def _report_directory_snapshot(reports_root: Path) -> dict[Path, float]:
    if not reports_root.is_dir():
        return {}
    return {path: path.stat().st_mtime for path in reports_root.iterdir() if path.is_dir()}


def _changed_report_paths(reports_root: Path, before: dict[Path, float]) -> tuple[Path, ...]:
    if not reports_root.is_dir():
        return ()
    changed: list[Path] = []
    for path in reports_root.iterdir():
        if not path.is_dir():
            continue
        previous_mtime = before.get(path)
        current_mtime = path.stat().st_mtime
        if previous_mtime is None or current_mtime > previous_mtime:
            changed.append(path)
    return tuple(sorted(changed))


def _write_report(
    report_dir: Path,
    latest_dir: Path,
    layout: WorkspaceLayout,
    workspace_options: WorkspaceOptions,
    campaign_options: LocalHammerCampaignOptions,
    started_at: datetime,
    deadline: datetime,
    phases: tuple[HammerPhase, ...],
    results: list[HammerPhaseResult],
    preflight: dict[str, Any],
    *,
    status: str,
) -> None:
    completed_at = datetime.now(timezone.utc)
    payload = {
        "schema": REPORT_SCHEMA,
        "campaignId": CAMPAIGN_ID,
        "status": status,
        "workspaceRoot": str(layout.emule_workspace_root),
        "workspaceName": workspace_options.workspace_name,
        "startedUtc": started_at.isoformat(),
        "updatedUtc": completed_at.isoformat(),
        "deadlineUtc": deadline.isoformat(),
        "durationSeconds": round((completed_at - started_at).total_seconds(), 3),
        "options": {
            "untilLocal": campaign_options.until_local,
            "timezone": campaign_options.timezone_str,
            "maxCycles": campaign_options.max_cycles,
            "cyclePauseSeconds": campaign_options.cycle_pause_seconds,
            "dryRun": campaign_options.dry_run,
            "releaseVersion": campaign_options.release_version,
            "clean": campaign_options.clean,
            "skipBuild": campaign_options.skip_build,
            "liveWireInputsFile": campaign_options.live_wire_inputs_file or "",
            "p2pBindInterfaceName": campaign_options.p2p_bind_interface_name,
            "godzillaP2pBindInterfaceAddress": campaign_options.godzilla_p2p_bind_interface_address or "",
            "profileSymbolsRequired": campaign_options.profile_symbols_required,
        },
        "preflight": preflight,
        "plannedPhases": [
            {"name": phase.name, "category": phase.category, "command": phase.command}
            for phase in phases
        ],
        "phases": [
            {
                "cycle": result.cycle,
                "name": result.name,
                "category": result.category,
                "command": result.command,
                "status": result.status,
                "attempts": result.attempts,
                "durationSeconds": result.duration_seconds,
                "error": result.error,
                "diagnostics": [str(path) for path in result.diagnostics],
                "reportPaths": [str(path) for path in result.report_paths],
                "refresh": result.refresh,
            }
            for result in results
        ],
    }
    report_text = json.dumps(payload, indent=2) + "\n"
    (report_dir / local_hammer_campaign_result_file_name()).write_text(report_text, encoding="utf-8", newline="\n")
    latest_dir.mkdir(parents=True, exist_ok=True)
    (latest_dir / local_hammer_campaign_result_file_name()).write_text(report_text, encoding="utf-8", newline="\n")
