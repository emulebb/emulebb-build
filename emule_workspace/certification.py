"""Release-certification test matrix orchestration."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .artifact_names import certification_result_file_name, utc_run_id
from .build import build_apps
from .build_tests import invoke_build_tests
from .cleanup import CleanupFailedError, CleanupRunSummary, cleanup_summary_payload, run_pre_test_cleanup
from .config import (
    BuildTestsOptions,
    CertificationOptions,
    CommunityCoverageOptions,
    LiveE2eOptions,
    PythonTestOptions,
    VariantComparisonOptions,
    WorkspaceOptions,
)
from .layout import WorkspaceLayout
from .python_tests import invoke_python_tests
from .test_runs import (
    invoke_community_core_coverage,
    invoke_live_e2e_suite,
    invoke_protocol_parity,
    invoke_test_runs,
)
from .validation import validate_workspace


@dataclass(frozen=True)
class CertificationStepPlan:
    """One planned certification step."""

    name: str
    category: str


@dataclass
class CertificationStepResult:
    """Recorded result for one certification step."""

    name: str
    category: str
    status: str
    duration_seconds: float
    error: str = ""
    report_paths: tuple[Path, ...] = ()


QUICK_STEP_PLAN = (
    CertificationStepPlan("validate", "static"),
    CertificationStepPlan("build-app-debug-x64", "build"),
    CertificationStepPlan("build-tests-debug-x64", "build"),
    CertificationStepPlan("python-harness", "static"),
    CertificationStepPlan("test-all-debug-x64", "native"),
    CertificationStepPlan("live-fast-ui-rest", "live"),
)

FAST_STEP_PLAN = (
    CertificationStepPlan("validate", "static"),
    CertificationStepPlan("build-app-debug-x64", "build"),
    CertificationStepPlan("build-app-release-x64", "build"),
    CertificationStepPlan("build-app-release-arm64", "build"),
    CertificationStepPlan("build-tests-debug-x64", "build"),
    CertificationStepPlan("build-tests-release-x64", "build"),
    CertificationStepPlan("python-harness", "static"),
    CertificationStepPlan("test-all-debug-x64", "native"),
    CertificationStepPlan("test-all-release-x64", "native"),
    CertificationStepPlan("live-fast-ui-rest", "live"),
)

# Stock/community parity comparisons (protocol-parity, community-core-coverage)
# are intentionally not in any tier: eMuleBB is protocol-stable and stock-compatible,
# so they run on demand via `test protocol-parity` / `test community-core-coverage`.
OVERNIGHT_EXTRA_STEP_PLAN = (
    CertificationStepPlan("live-controller-surface", "live"),
    CertificationStepPlan("live-release-expanded", "live"),
    CertificationStepPlan("live-ui-resource-depth", "live"),
    CertificationStepPlan("live-stabilization-stress", "stress"),
)


def get_certification_step_plan(profile: str) -> tuple[CertificationStepPlan, ...]:
    """Returns the ordered certification matrix for one profile."""

    if profile == "quick":
        return QUICK_STEP_PLAN
    if profile == "fast":
        return FAST_STEP_PLAN
    if profile == "overnight":
        return FAST_STEP_PLAN + OVERNIGHT_EXTRA_STEP_PLAN
    raise ValueError(f"Unsupported certification profile: {profile}")


def invoke_certification(layout: WorkspaceLayout, options: WorkspaceOptions, certification_options: CertificationOptions) -> None:
    """Runs the selected release-certification matrix and writes one report."""

    report_dir = _new_report_dir(layout, certification_options.profile)
    report_dir.mkdir(parents=True, exist_ok=False)
    steps: list[CertificationStepResult] = []
    pre_run_cleanup: CleanupRunSummary | None = None
    started_at = datetime.now(timezone.utc)
    status = "passed"
    stop_error: RuntimeError | None = None

    try:
        if certification_options.pre_run_cleanup:
            try:
                pre_run_cleanup = run_pre_test_cleanup(layout)
            except CleanupFailedError as exc:
                pre_run_cleanup = exc.summary
                raise
        for step_plan in get_certification_step_plan(certification_options.profile):
            step_result = _run_step(
                layout,
                options,
                certification_options,
                step_plan,
            )
            steps.append(step_result)
            _write_report(report_dir, layout, options, certification_options, started_at, steps, pre_run_cleanup, status="running")
            if step_result.status not in {"passed", "skipped"}:
                if not certification_options.continue_on_failure:
                    stop_error = RuntimeError(f"Certification step '{step_result.name}' {step_result.status}: {step_result.error}")
                    break
        status = _aggregate_status(steps)
        if stop_error is not None:
            raise stop_error
        if status != "passed":
            raise RuntimeError(f"Certification profile '{certification_options.profile}' completed with status {status}.")
    except Exception:
        if not steps:
            status = "failed"
        else:
            status = _aggregate_status(steps)
        _write_report(report_dir, layout, options, certification_options, started_at, steps, pre_run_cleanup, status=status)
        print(f"Certification report: {report_dir / certification_result_file_name()}")
        raise

    _write_report(report_dir, layout, options, certification_options, started_at, steps, pre_run_cleanup, status=status)
    print("")
    print(f"Certification profile: {certification_options.profile}")
    print(f"Status: {status}")
    print(f"Steps: {len(steps)}")
    print(f"Report: {report_dir / certification_result_file_name()}")


def _aggregate_status(steps: list[CertificationStepResult]) -> str:
    """Returns the terminal certification status from collected step results."""

    if any(step.status == "failed" for step in steps):
        return "failed"
    if any(step.status == "inconclusive" for step in steps):
        return "inconclusive"
    return "passed"


def _skip_reason_for_network(step_plan: CertificationStepPlan, certification_options: CertificationOptions) -> str:
    """Returns a skip reason when a certification step is outside the selected test network."""

    return ""


def _run_step(
    layout: WorkspaceLayout,
    options: WorkspaceOptions,
    certification_options: CertificationOptions,
    step_plan: CertificationStepPlan,
) -> CertificationStepResult:
    """Runs one certification step and captures report directories touched by it."""

    reports_root = _test_reports_root(layout)
    before_reports = _report_directory_snapshot(reports_root)
    started = time.monotonic()
    error = ""
    status = "passed"
    skip_reason = _skip_reason_for_network(step_plan, certification_options)
    if skip_reason:
        return CertificationStepResult(
            name=step_plan.name,
            category=step_plan.category,
            status="skipped",
            duration_seconds=0.0,
            error=skip_reason,
        )
    try:
        _invoke_step(layout, options, certification_options, step_plan.name)
    except Exception as exc:
        error = str(exc)
    duration = time.monotonic() - started
    report_paths = _changed_report_paths(reports_root, before_reports)
    if error:
        status = _failure_status_from_child_reports(report_paths)
    return CertificationStepResult(
        name=step_plan.name,
        category=step_plan.category,
        status=status,
        duration_seconds=round(duration, 3),
        error=error,
        report_paths=report_paths,
    )


def _invoke_step(
    layout: WorkspaceLayout,
    options: WorkspaceOptions,
    certification_options: CertificationOptions,
    name: str,
) -> None:
    """Dispatches one named certification step."""

    if name == "validate":
        validate_workspace(layout)
        return
    if name == "build-app-debug-x64":
        build_apps(layout, _step_options(options, configuration="Debug", platform="x64"), clean=False, app_variant_names=("main",))
        return
    if name == "build-app-release-x64":
        build_apps(layout, _step_options(options, configuration="Release", platform="x64"), clean=False, app_variant_names=("main",))
        return
    if name == "build-app-release-arm64":
        build_apps(layout, _step_options(options, configuration="Release", platform="ARM64"), clean=False, app_variant_names=("main",))
        return
    if name == "build-tests-debug-x64":
        invoke_build_tests(
            layout,
            _step_options(options, configuration="Debug", platform="x64"),
            BuildTestsOptions(test_run_variant="main"),
        )
        return
    if name == "build-tests-release-x64":
        invoke_build_tests(
            layout,
            _step_options(options, configuration="Release", platform="x64"),
            BuildTestsOptions(test_run_variant="main"),
        )
        return
    if name == "python-harness":
        invoke_python_tests(layout, PythonTestOptions(quiet=True))
        return
    if name == "test-all-debug-x64":
        invoke_test_runs(
            layout,
            _step_options(options, configuration="Debug", platform="x64"),
            coverage=certification_options.profile != "quick",
        )
        return
    if name == "test-all-release-x64":
        invoke_test_runs(
            layout,
            _step_options(options, configuration="Release", platform="x64"),
            coverage=certification_options.profile != "quick",
        )
        return
    if name == "protocol-parity":
        invoke_protocol_parity(layout, _step_options(options, configuration="Release", platform="x64"), VariantComparisonOptions())
        return
    if name == "community-core-coverage":
        invoke_community_core_coverage(
            layout,
            _step_options(options, configuration="Release", platform="x64"),
            CommunityCoverageOptions(rest_coverage_budget="contract", rest_stress_budget="smoke"),
        )
        return
    if name == "live-fast-ui-rest":
        invoke_live_e2e_suite(
            layout,
            _step_options(options, configuration="Release", platform="x64"),
            _fast_live_options(certification_options),
        )
        return
    if name == "live-controller-surface":
        invoke_live_e2e_suite(
            layout,
            _step_options(options, configuration="Release", platform="x64"),
            _live_options(certification_options, profile="controller-surface", fail_fast=True),
        )
        return
    if name == "live-release-expanded":
        invoke_live_e2e_suite(
            layout,
            _step_options(options, configuration="Release", platform="x64"),
            _live_options(
                certification_options,
                profile="release-expanded",
                fail_fast=True,
                profile_cpu=True,
                profile_cpu_stack=True,
                profile_memory=True,
                admin_volume_fixtures=True,
            ),
        )
        return
    if name == "live-ui-resource-depth":
        invoke_live_e2e_suite(
            layout,
            _step_options(options, configuration="Release", platform="x64"),
            _live_options(certification_options, profile="ui-resource-depth", fail_fast=True),
        )
        return
    if name == "live-stabilization-stress":
        invoke_live_e2e_suite(
            layout,
            _step_options(options, configuration="Release", platform="x64"),
            _live_options(
                certification_options,
                profile="stabilization-stress",
                fail_fast=True,
                profile_cpu=True,
                profile_cpu_stack=True,
                profile_memory=True,
                rest_cold_start_dump_stress_cpu_profile=True,
                rest_cold_start_dump_stress_cpu_profile_stack=True,
            ),
        )
        return
    raise ValueError(f"Unsupported certification step: {name}")


def _step_options(options: WorkspaceOptions, *, configuration: str, platform: str) -> WorkspaceOptions:
    return options.model_copy(update={"configuration": configuration, "platform": platform})


def _fast_live_options(certification_options: CertificationOptions) -> LiveE2eOptions:
    return _live_options(
        certification_options,
        suites=(
            "preference-ui",
            "shared-files-ui",
            "config-stability-ui",
            "shared-hash-ui",
            "startup-diagnostics",
            "shared-directories-rest",
            "rest-api",
        ),
        fail_fast=True,
    )


def _live_options(
    certification_options: CertificationOptions,
    *,
    profile: str = "default",
    suites: tuple[str, ...] = (),
    fail_fast: bool = False,
    preference_ui_directories_tree_stress: bool = False,
    search_ui_search_rounds: int = 1,
    search_ui_download_lifecycle_count: int = 1,
    profile_cpu: bool = False,
    profile_cpu_stack: bool = False,
    profile_memory: bool = False,
    admin_volume_fixtures: bool = False,
    rest_cold_start_dump_stress_cpu_profile: bool = False,
    rest_cold_start_dump_stress_cpu_profile_stack: bool = False,
) -> LiveE2eOptions:
    return LiveE2eOptions(
        suites=suites,
        profile=profile,
        test_network=certification_options.test_network,
        pre_run_cleanup=False,
        fail_fast=fail_fast,
        skip_live_seed_refresh=certification_options.skip_live_seed_refresh,
        preference_ui_directories_tree_stress=preference_ui_directories_tree_stress,
        live_wire_inputs_file=certification_options.live_wire_inputs_file,
        radarr_movie_root=certification_options.radarr_movie_root,
        sonarr_series_root=certification_options.sonarr_series_root,
        acquisition_timeout_minutes=certification_options.acquisition_timeout_minutes,
        p2p_bind_interface_name=certification_options.p2p_bind_interface_name,
        vpn_guard_live_config=certification_options.vpn_guard_live_config,
        rest_coverage_budget="contract",
        rest_stress_budget="smoke",
        search_ui_search_rounds=search_ui_search_rounds,
        search_ui_download_lifecycle_count=search_ui_download_lifecycle_count,
        profile_cpu=profile_cpu,
        profile_cpu_stack=profile_cpu_stack,
        profile_memory=profile_memory,
        admin_volume_fixtures=admin_volume_fixtures,
        rest_cold_start_dump_stress_cpu_profile=rest_cold_start_dump_stress_cpu_profile,
        rest_cold_start_dump_stress_cpu_profile_stack=rest_cold_start_dump_stress_cpu_profile_stack,
    )










def _new_report_dir(layout: WorkspaceLayout, profile: str) -> Path:
    stamp = utc_run_id()
    return layout.output_reports_root / "certification" / f"{stamp}-{profile}"


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


def _failure_status_from_child_reports(report_paths: tuple[Path, ...]) -> str:
    """Classifies external live evidence when child reports mark inconclusive."""

    for report_path in report_paths:
        result_path = _child_result_path(report_path)
        if not result_path.is_file():
            continue
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("status") == "inconclusive" or payload.get("has_inconclusive_suites") is True:
            return "inconclusive"
    return "failed"


def _child_result_path(report_path: Path) -> Path:
    """Returns the canonical latest result path for one changed report family."""

    if report_path.name == "latest":
        suite_name = report_path.parent.name
        return report_path / f"{suite_name}-result.json"
    if (report_path / "latest").is_dir():
        return report_path / "latest" / f"{report_path.name}-result.json"
    return report_path / _report_result_file_name(report_path)


def _write_report(
    report_dir: Path,
    layout: WorkspaceLayout,
    options: WorkspaceOptions,
    certification_options: CertificationOptions,
    started_at: datetime,
    steps: list[CertificationStepResult],
    pre_run_cleanup: CleanupRunSummary | None,
    *,
    status: str,
) -> None:
    completed_at = datetime.now(timezone.utc)
    payload = {
        "status": status,
        "profile": certification_options.profile,
        "workspace_root": str(layout.emule_workspace_root),
        "workspace_name": options.workspace_name,
        "started_utc": started_at.isoformat(),
        "updated_utc": completed_at.isoformat(),
        "duration_seconds": round((completed_at - started_at).total_seconds(), 3),
        "commits": _workspace_commits(layout),
        "options": {
            "test_network": certification_options.test_network,
            "pre_run_cleanup": certification_options.pre_run_cleanup,
            "continue_on_failure": certification_options.continue_on_failure,
            "p2p_bind_interface_name": certification_options.p2p_bind_interface_name,
            "live_wire_inputs_file": certification_options.live_wire_inputs_file or "",
            "radarr_movie_root": certification_options.radarr_movie_root or "",
            "sonarr_series_root": certification_options.sonarr_series_root or "",
            "acquisition_timeout_minutes": certification_options.acquisition_timeout_minutes,
            "skip_live_seed_refresh": certification_options.skip_live_seed_refresh,
        },
        "pre_run_cleanup": cleanup_summary_payload(pre_run_cleanup),
        "steps": [
            {
                "name": step.name,
                "category": step.category,
                "status": step.status,
                "duration_seconds": step.duration_seconds,
                "error": step.error,
                "report_paths": [str(path) for path in step.report_paths],
            }
            for step in steps
        ],
    }
    (report_dir / certification_result_file_name()).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")


def _report_result_file_name(report_path: Path) -> str:
    """Returns the canonical child result name for a test report directory."""

    if report_path.name == "latest":
        suite_name = report_path.parent.name
    else:
        suite_name = report_path.parent.name if report_path.parent.name != "test-reports" else report_path.name
    return f"{suite_name}-result.json"


def _workspace_commits(layout: WorkspaceLayout) -> dict[str, str]:
    paths = {
        "app_main": layout.get_app_variant(layout.test_targets.test_run_variant).path,
        "build": layout.build_repo_root,
        "build_tests": layout.tests_repo_root,
        "tooling": layout.tooling_repo_root,
    }
    return {name: _git_head(path) for name, path in paths.items()}


def _git_head(path: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            return completed.stdout.strip()
    except OSError:
        pass
    return ""
