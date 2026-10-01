"""Workspace generated-artifact cleanup planning and execution."""

from __future__ import annotations

import os
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .config import ACTIVE_EMULEBB_RELEASE_VERSION, CleanupOptions
from .layout import WorkspaceLayout

MEDIA_SUFFIXES = {
    ".avi",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ts",
    ".wmv",
}
HEAVY_SUFFIXES = MEDIA_SUFFIXES | {".part", ".dmp", ".etl", ".zip"}
CACHE_DIRECTORY_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
CACHE_SCAN_PRUNE_NAMES = {".git", "build", "node_modules", "reports", "tools"}
TEST_ARTIFACTS_DIR_NAME = "test-artifacts"
TEST_REPORTS_DIR_NAME = "test-reports"
LEGACY_LIVE_E2E_ARTIFACTS_DIR_NAME = "live-e2e-artifacts"
REPORT_PAYLOAD_DIRECTORY_NAMES = {
    "dumps",
    "incoming",
    "radarr_movies_cat",
    "sonarr_series_cat",
    "temp",
}
MAX_DIRECTORY_SIZE_SCAN_FILES = 2000
PROFILING_ARTIFACT_NAME_MARKERS = (
    "cpu",
    "crash",
    "diagnostic",
    "dump",
    "live-monitor",
    "memory-profile",
    "pageheap",
    "process-monitor",
    "startup-progress",
    "umdh",
)


@dataclass(frozen=True)
class CleanupCandidate:
    """One generated file or directory that can be pruned."""

    path: Path
    kind: str
    category: str
    reason: str
    bytes: int
    files: int
    estimated: bool = False


@dataclass(frozen=True)
class CleanupRunSummary:
    """Machine-readable summary of one generated-artifact cleanup pass."""

    profile: str
    apply: bool
    status: str
    candidates: int
    bytes: int
    files: int
    categories: dict[str, dict[str, int]]
    error: str = ""


class CleanupFailedError(RuntimeError):
    """Raised when a cleanup pass fails after producing a summary."""

    def __init__(self, summary: CleanupRunSummary) -> None:
        super().__init__(summary.error)
        self.summary = summary


def cleanup_workspace(layout: WorkspaceLayout, options: CleanupOptions) -> CleanupRunSummary:
    """Plans or applies generated-artifact cleanup for one workspace."""

    candidates = plan_cleanup(layout, options)
    action = "Applying" if options.apply else "Dry run"
    print(f"{action} cleanup profile '{options.profile}' for {layout.emule_workspace_root}")
    _print_cleanup_summary(layout, candidates)
    if not options.apply:
        print("Dry run only. Re-run with --apply to delete the listed generated artifacts.")
        return _cleanup_summary(options, candidates, status="planned")
    try:
        for candidate in candidates:
            _delete_candidate(candidate)
    except Exception as exc:
        summary = _cleanup_summary(options, candidates, status="failed", error=str(exc))
        print(f"Cleanup failed: {exc}")
        raise CleanupFailedError(summary) from exc
    print(f"Cleanup applied. Removed {_format_bytes(sum(candidate.bytes for candidate in candidates))}.")
    return _cleanup_summary(options, candidates, status="passed")


def run_pre_test_cleanup(layout: WorkspaceLayout) -> CleanupRunSummary:
    """Prunes old generated outcomes before broad test orchestration starts."""

    return cleanup_workspace(layout, CleanupOptions(apply=True, profile="routine"))


def cleanup_summary_payload(summary: CleanupRunSummary | None) -> dict[str, object] | None:
    """Returns a JSON-friendly cleanup summary payload."""

    if summary is None:
        return None
    return {
        "profile": summary.profile,
        "apply": summary.apply,
        "status": summary.status,
        "candidates": summary.candidates,
        "bytes": summary.bytes,
        "files": summary.files,
        "categories": summary.categories,
        "error": summary.error,
    }


def plan_cleanup(layout: WorkspaceLayout, options: CleanupOptions) -> list[CleanupCandidate]:
    """Returns cleanup candidates without modifying the filesystem."""

    now = datetime.now()
    candidates: list[CleanupCandidate] = []
    candidates.extend(_path_anomaly_candidates(layout))
    candidates.extend(_test_artifact_candidates(layout, options, now))
    candidates.extend(_legacy_live_e2e_artifact_candidates(layout, options, now))
    candidates.extend(_report_payload_candidates(layout, options, now))
    candidates.extend(_old_report_run_candidates(layout, options, now))
    if options.include_legacy_test_reports:
        candidates.extend(_legacy_test_report_candidates(layout, options, now))
    candidates.extend(_arr_acquisition_candidates(layout, options, now))
    candidates.extend(_build_log_candidates(layout, options, now))
    candidates.extend(_cache_candidates(layout))
    if options.profile == "deep" or options.include_build_outputs:
        candidates.extend(_build_output_candidates(layout))
    if options.include_release_state:
        candidates.extend(_release_state_candidates(layout))
    if options.profile == "deep" or options.include_product_family_outputs:
        candidates.extend(_product_family_output_candidates(layout))
    if options.include_root_legacy_state:
        candidates.extend(_root_legacy_state_candidates(layout))
    if options.include_legacy_root_logs:
        candidates.extend(_legacy_root_log_candidates(layout))
    if options.include_profiling_artifacts:
        candidates.extend(_profiling_artifact_candidates(layout, options, now))
    return _dedupe_candidates(candidates)


def _path_anomaly_candidates(layout: WorkspaceLayout) -> list[CleanupCandidate]:
    root = _output_root(layout)
    if not root.is_dir():
        return []
    candidates: list[CleanupCandidate] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            anomalous_name = entry.name != entry.name.rstrip(" .")
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                is_directory = False
                is_file = False
            if anomalous_name:
                kind = "directory" if is_directory else "file"
                candidates.append(_path_anomaly_candidate(path, kind))
                continue
            if is_directory:
                stack.append(path)
    return candidates


def _path_anomaly_candidate(path: Path, kind: str) -> CleanupCandidate:
    reason = "generated state path has trailing spaces or dots that break recursive tools"
    if kind == "directory" and path.is_dir():
        try:
            candidate = _directory_candidate(path, "path-anomaly", reason)
            return CleanupCandidate(
                candidate.path,
                candidate.kind,
                candidate.category,
                candidate.reason,
                candidate.bytes,
                candidate.files,
                candidate.estimated,
            )
        except OSError:
            return CleanupCandidate(path, "directory", "path-anomaly", reason, 0, 0, estimated=True)
    if kind == "file" and path.is_file():
        try:
            return _file_candidate(path, "path-anomaly", reason)
        except OSError:
            return CleanupCandidate(path, "file", "path-anomaly", reason, 0, 1, estimated=True)
    return CleanupCandidate(path, kind, "path-anomaly", reason, 0, 0, estimated=True)


def _test_artifact_candidates(layout: WorkspaceLayout, options: CleanupOptions, now: datetime) -> list[CleanupCandidate]:
    root = _test_artifacts_root(layout)
    cutoff = now - timedelta(hours=options.report_payload_retention_hours)
    if not root.is_dir():
        return []
    candidates: list[CleanupCandidate] = []
    for suite_dir in _child_directories(root):
        if suite_dir.name == "arr-acquisition":
            continue
        for run_dir in _child_directories(suite_dir):
            if run_dir.stat().st_mtime >= cutoff.timestamp():
                continue
            candidates.append(
                _directory_candidate(
                    run_dir,
                    TEST_ARTIFACTS_DIR_NAME,
                    f"test artifact run older than {options.report_payload_retention_hours:g}h",
                )
            )
    return candidates


def _legacy_live_e2e_artifact_candidates(layout: WorkspaceLayout, options: CleanupOptions, now: datetime) -> list[CleanupCandidate]:
    root = layout.workspace_root / "state" / LEGACY_LIVE_E2E_ARTIFACTS_DIR_NAME
    cutoff = now - timedelta(hours=options.report_payload_retention_hours)
    if not root.is_dir():
        return []
    candidates: list[CleanupCandidate] = []
    for run_dir in _child_directories(root):
        if run_dir.stat().st_mtime >= cutoff.timestamp():
            continue
        candidates.append(
            _directory_candidate(
                run_dir,
                "legacy-live-e2e-artifacts",
                f"legacy live E2E source artifact run older than {options.report_payload_retention_hours:g}h",
            )
        )
    return candidates


def _report_payload_candidates(layout: WorkspaceLayout, options: CleanupOptions, now: datetime) -> list[CleanupCandidate]:
    return _report_payload_candidates_for_root(
        _test_reports_root(layout),
        options,
        now,
        category="report-payload",
        reason_label="report",
    )


def _report_payload_candidates_for_root(
    reports_root: Path,
    options: CleanupOptions,
    now: datetime,
    *,
    category: str,
    reason_label: str,
) -> list[CleanupCandidate]:
    cutoff = now - timedelta(hours=options.report_payload_retention_hours)
    candidates: list[CleanupCandidate] = []
    if not reports_root.is_dir():
        return candidates
    for family in _child_directories(reports_root):
        if _skip_generic_report_family(family.name, options):
            continue
        scopes = [family]
        for run_dir in _child_directories(family):
            scopes.extend([run_dir, *_child_directories(run_dir)])
        for scope in scopes:
            candidates.extend(
                _payload_directory_candidates(
                    scope,
                    cutoff,
                    options.report_payload_retention_hours,
                    category=category,
                    reason_label=reason_label,
                )
            )
            candidates.extend(
                _direct_heavy_file_candidates(
                    scope,
                    cutoff,
                    options.report_payload_retention_hours,
                    category=category,
                    reason_label=reason_label,
                )
            )
    return candidates


def _payload_directory_candidates(
    scope: Path,
    cutoff: datetime,
    retention_hours: float,
    *,
    category: str,
    reason_label: str,
) -> list[CleanupCandidate]:
    candidates: list[CleanupCandidate] = []
    for name in REPORT_PAYLOAD_DIRECTORY_NAMES:
        path = scope / name
        if path.is_dir() and path.stat().st_mtime < cutoff.timestamp():
            candidates.append(
                _directory_candidate(
                    path,
                    category,
                    f"{reason_label} payload directory older than {retention_hours:g}h",
                )
            )
    return candidates


def _direct_heavy_file_candidates(
    scope: Path,
    cutoff: datetime,
    retention_hours: float,
    *,
    category: str,
    reason_label: str,
) -> list[CleanupCandidate]:
    candidates: list[CleanupCandidate] = []
    for path in scope.iterdir() if scope.is_dir() else ():
        if not path.is_file():
            continue
        if path.stat().st_mtime >= cutoff.timestamp() or path.suffix.lower() not in HEAVY_SUFFIXES:
            continue
        candidates.append(_file_candidate(path, category, f"heavy {reason_label} payload older than {retention_hours:g}h"))
    return candidates


def _old_report_run_candidates(layout: WorkspaceLayout, options: CleanupOptions, now: datetime) -> list[CleanupCandidate]:
    reports_root = _test_reports_root(layout)
    cutoff = now - timedelta(days=options.report_run_retention_days)
    candidates: list[CleanupCandidate] = []
    if not reports_root.is_dir():
        return candidates
    for family in reports_root.iterdir():
        if not family.is_dir() or family.name.endswith("-latest"):
            continue
        if _skip_generic_report_family(family.name, options):
            continue
        for run_dir in family.iterdir():
            if not run_dir.is_dir() or run_dir.name == "latest" or run_dir.stat().st_mtime >= cutoff.timestamp():
                continue
            candidates.append(
                _directory_candidate(
                    run_dir,
                    "report-run",
                    f"timestamped report run older than {options.report_run_retention_days:g}d",
                )
            )
    return candidates


def _legacy_test_report_candidates(layout: WorkspaceLayout, options: CleanupOptions, now: datetime) -> list[CleanupCandidate]:
    reports_root = _legacy_test_reports_root(layout)
    if not reports_root.is_dir():
        return []
    candidates: list[CleanupCandidate] = []
    candidates.extend(
        _report_payload_candidates_for_root(
            reports_root,
            options,
            now,
            category="legacy-test-reports",
            reason_label="legacy report",
        )
    )
    candidates.extend(_old_legacy_report_run_candidates(reports_root, options, now))
    candidates.extend(_old_legacy_report_root_file_candidates(reports_root, options, now))
    return candidates


def _old_legacy_report_run_candidates(reports_root: Path, options: CleanupOptions, now: datetime) -> list[CleanupCandidate]:
    cutoff = now - timedelta(days=options.report_run_retention_days)
    candidates: list[CleanupCandidate] = []
    for family in reports_root.iterdir():
        if not family.is_dir():
            continue
        if family.name.endswith("-latest"):
            if family.stat().st_mtime < cutoff.timestamp():
                candidates.append(
                    _directory_candidate(
                        family,
                        "legacy-test-reports",
                        f"legacy latest report snapshot older than {options.report_run_retention_days:g}d",
                    )
                )
            continue
        for run_dir in family.iterdir():
            if not run_dir.is_dir() or run_dir.stat().st_mtime >= cutoff.timestamp():
                continue
            candidates.append(
                _directory_candidate(
                    run_dir,
                    "legacy-test-reports",
                    f"legacy timestamped report run older than {options.report_run_retention_days:g}d",
                )
            )
    return candidates


def _old_legacy_report_root_file_candidates(reports_root: Path, options: CleanupOptions, now: datetime) -> list[CleanupCandidate]:
    cutoff = now - timedelta(days=options.report_run_retention_days)
    candidates: list[CleanupCandidate] = []
    for path in reports_root.iterdir():
        if not path.is_file() or path.stat().st_mtime >= cutoff.timestamp():
            continue
        candidates.append(
            _file_candidate(
                path,
                "legacy-test-reports",
                f"legacy root report file older than {options.report_run_retention_days:g}d",
            )
        )
    return candidates


def _test_artifacts_root(layout: WorkspaceLayout) -> Path:
    return _output_root(layout) / "artifacts"


def _test_reports_root(layout: WorkspaceLayout) -> Path:
    return _output_root(layout) / "reports"


def _legacy_test_reports_root(layout: WorkspaceLayout) -> Path:
    return layout.tests_repo_root / "reports"


def _arr_acquisition_candidates(layout: WorkspaceLayout, options: CleanupOptions, now: datetime) -> list[CleanupCandidate]:
    root = _output_root(layout) / "artifacts" / "arr-acquisition"
    cutoff = now - timedelta(hours=options.arr_acquisition_retention_hours)
    candidates: list[CleanupCandidate] = []
    if not root.is_dir():
        return candidates
    for child in root.iterdir():
        if child.name == "logs":
            continue
        if child.stat().st_mtime >= cutoff.timestamp():
            continue
        candidates.append(
            _directory_candidate(
                child,
                "arr-acquisition",
                f"Arr acquisition output older than {options.arr_acquisition_retention_hours:g}h",
            )
        )
    return candidates


def _build_log_candidates(layout: WorkspaceLayout, options: CleanupOptions, now: datetime) -> list[CleanupCandidate]:
    root = _output_root(layout) / "logs" / "builds"
    cutoff = now - timedelta(days=options.build_log_retention_days)
    if not root.is_dir():
        return []
    runs = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.stat().st_mtime, reverse=True)
    protected = set(runs[: max(0, options.keep_build_log_runs)])
    candidates: list[CleanupCandidate] = []
    for run_dir in runs:
        if run_dir in protected or run_dir.stat().st_mtime >= cutoff.timestamp():
            continue
        candidates.append(
            _directory_candidate(
                run_dir,
                "build-logs",
                f"build log run older than {options.build_log_retention_days:g}d and outside newest {options.keep_build_log_runs}",
            )
        )
    return candidates


def _cache_candidates(layout: WorkspaceLayout) -> list[CleanupCandidate]:
    roots = (layout.build_repo_root, layout.tests_repo_root, layout.tooling_repo_root)
    candidates: list[CleanupCandidate] = []
    for root in roots:
        if not root.is_dir():
            continue
        candidates.extend(_cache_candidates_under(root))
    return candidates


def _cache_candidates_under(root: Path) -> list[CleanupCandidate]:
    candidates: list[CleanupCandidate] = []
    stack = [root]
    while stack:
        current = stack.pop()
        for child in current.iterdir():
            if not child.is_dir():
                continue
            if child.name in CACHE_DIRECTORY_NAMES:
                candidates.append(_directory_candidate(child, "caches", "Python/test cache directory"))
                continue
            if child.name in CACHE_SCAN_PRUNE_NAMES:
                continue
            stack.append(child)
    return candidates


def _build_output_candidates(layout: WorkspaceLayout) -> list[CleanupCandidate]:
    candidates: list[CleanupCandidate] = []
    for variant in layout.app_variants:
        srchybrid = variant.path / "srchybrid"
        for name in ("x64", "ARM64"):
            path = srchybrid / name
            if path.is_dir():
                candidates.append(_directory_candidate(path, "build-output", "app build output"))
        for name in ("x64", "ARM64"):
            path = srchybrid / "lang" / name
            if path.is_dir():
                candidates.append(_directory_candidate(path, "build-output", "app language build output"))
    tests_build = layout.tests_repo_root / "build"
    if tests_build.is_dir():
        candidates.append(_directory_candidate(tests_build, "build-output", "native test build output"))
    package_build = _output_root(layout) / "packages" / "build"
    if package_build.is_dir():
        candidates.append(_directory_candidate(package_build, "build-output", "release package app build output"))
    output_builds = _output_root(layout) / "builds"
    if output_builds.is_dir():
        for child in sorted(output_builds.iterdir()):
            if child.is_dir():
                candidates.append(_directory_candidate(child, "build-output", "canonical workspace build output"))
    candidates.extend(_third_party_dependency_build_candidates(layout))
    third_party_builds = getattr(layout, "output_third_party_build_root", None)
    if isinstance(third_party_builds, Path) and third_party_builds.is_dir():
        candidates.append(_directory_candidate(third_party_builds, "build-output", "canonical third-party build output"))
    for dependency in layout.dependencies:
        root = layout.emule_workspace_root / dependency.path
        for child_name in ("x64", "ARM64", "Debug", "Release", "build"):
            path = root / child_name
            if path.is_dir():
                candidates.append(_directory_candidate(path, "build-output", "dependency build output"))
    return candidates


def _third_party_dependency_build_candidates(layout: WorkspaceLayout) -> list[CleanupCandidate]:
    third_party = layout.emule_workspace_root / "repos" / "third_party"
    specs: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
        ("emulebb-cryptopp", ("ARM64", "x64"), ("adhoc.cpp", "adhoc.cpp.copied")),
        ("emulebb-id3lib", ("libprj/ARM64", "libprj/x64", "libprj/id3lib"), ()),
        ("emulebb-libpcpnatpmp", ("cmake-build-arm64", "cmake-build-x64"), ()),
        (
            "emulebb-mbedtls",
            ("visualc/VS2017-ARM64", "visualc/VS2017-x64", "visualc/VS2017/ARM64", "visualc/VS2017/x64"),
            (),
        ),
        ("emulebb-miniupnp", ("miniupnpc/msvc/ARM64", "miniupnpc/msvc/x64"), ("miniupnpc/miniupnpcstrings.h", "miniupnpc/rc_version.h")),
        ("emulebb-resizablelib", ("ResizableLib/ARM64", "ResizableLib/x64"), ()),
        (
            "emulebb-zlib",
            ("cmake-build-ARM64", "cmake-build-x64", "contrib/vstudio/vc/ARM64", "contrib/vstudio/vc/x64", "contrib/vstudio/vc/zlib"),
            (),
        ),
    )
    candidates: list[CleanupCandidate] = []
    for repo_name, directories, files in specs:
        repo_root = third_party / repo_name
        for relative in directories:
            path = repo_root / relative
            if path.is_dir():
                candidates.append(_directory_candidate(path, "build-output", "third-party dependency build output"))
        for relative in files:
            path = repo_root / relative
            if path.is_file():
                candidates.append(_file_candidate(path, "build-output", "third-party dependency generated file"))
    return candidates


def _release_state_candidates(layout: WorkspaceLayout) -> list[CleanupCandidate]:
    root = _output_root(layout) / "release"
    candidates: list[CleanupCandidate] = []
    if not root.is_dir():
        return candidates
    active_release_state = f"emulebb-v{ACTIVE_EMULEBB_RELEASE_VERSION}"
    for child in root.iterdir():
        if child.is_dir() and child.name != active_release_state:
            candidates.append(_directory_candidate(child, "release-state", "superseded release rehearsal state"))
    return candidates


def _product_family_output_candidates(layout: WorkspaceLayout) -> list[CleanupCandidate]:
    candidates: list[CleanupCandidate] = []
    repos_root = layout.emule_workspace_root / "repos"
    candidate_paths = (
        _optional_root(layout, "ed2k_index_server_repo_root") / "target",
        _optional_root(layout, "emulebb_rust_repo_root") / "target",
        _optional_root(layout, "p2p_overlord_agents_repo_root") / "target",
        _optional_root(layout, "p2p_overlord_be_repo_root") / "overlord-be-coordinator" / "node_modules",
        _optional_root(layout, "p2p_overlord_be_repo_root") / "overlord-be-coordinator" / ".svelte-kit",
        repos_root / "amutorrent" / "node_modules",
        repos_root / "amutorrent" / "server" / "node_modules",
        repos_root / "amutorrent" / "server" / "data",
        repos_root / "amutorrent" / "server" / "logs",
        repos_root / "amutorrent" / "website" / "node_modules",
        repos_root / "amutorrent" / "website" / ".astro",
        repos_root / "amutorrent" / "website" / "dist",
        repos_root / "amutorrent" / "website" / "public",
        repos_root / "amutorrent" / "website" / "src" / "assets",
        repos_root / "amutorrent" / "website" / "src" / "content" / "docs" / "docs",
        repos_root / "amule" / "build",
        repos_root / "amule" / "dist",
    )
    for path in candidate_paths:
        if path.is_dir():
            candidates.append(_directory_candidate(path, "product-family-output", "generated product-family repository output"))
    for path in (
        repos_root / "amutorrent" / "static" / "dist" / "app.bundle.js",
        repos_root / "amutorrent" / "static" / "dist" / "app.bundle.js.map",
        repos_root / "amutorrent" / "static" / "output.css",
    ):
        if path.is_file():
            candidates.append(_file_candidate(path, "product-family-output", "generated product-family repository output"))
    return candidates


def _root_legacy_state_candidates(layout: WorkspaceLayout) -> list[CleanupCandidate]:
    root = layout.emule_workspace_root / "state"
    candidates: list[CleanupCandidate] = []
    for name in ("miniupnpc-package-test", "smoke", "test-artifacts", "tools"):
        path = root / name
        if path.is_dir():
            candidates.append(_directory_candidate(path, "root-legacy-state", "legacy generated state directory at workspace root"))
    nested_state = layout.build_repo_root / "workspaces" / getattr(layout, "workspace_name", "workspace") / "state"
    if nested_state.is_dir():
        candidates.append(_directory_candidate(nested_state, "root-legacy-state", "nested generated workspace state under build repo"))
    return candidates


def _legacy_root_log_candidates(layout: WorkspaceLayout) -> list[CleanupCandidate]:
    path = layout.emule_workspace_root / "eMule-workspace.log"
    if path.is_file():
        return [_file_candidate(path, "legacy-root-log", "retired root-level workspace log")]
    return []


def _profiling_artifact_candidates(layout: WorkspaceLayout, options: CleanupOptions, now: datetime) -> list[CleanupCandidate]:
    candidates: list[CleanupCandidate] = []
    output_root = _output_root(layout)
    cutoff = now - timedelta(hours=options.report_payload_retention_hours)
    for path in _child_directories(output_root / "reports" / "diagnostics"):
        if path.stat().st_mtime < cutoff.timestamp():
            candidates.append(
                _directory_candidate(
                    path,
                    "profiling-artifact",
                    f"diagnostic profiling run older than {options.report_payload_retention_hours:g}h",
                )
            )
    for name in ("crash-evidence", "live-process-monitor-launch", "startup-progress-diagnostics"):
        root = output_root / "reports" / name
        for path in _child_directories(root):
            if path.stat().st_mtime < cutoff.timestamp():
                candidates.append(
                    _directory_candidate(
                        path,
                        "profiling-artifact",
                        f"profiling diagnostic state older than {options.report_payload_retention_hours:g}h",
                    )
                )
        candidates.extend(
            _direct_heavy_file_candidates(
                root,
                cutoff,
                options.report_payload_retention_hours,
                category="profiling-artifact",
                reason_label="profiling",
            )
        )
    for root_name in ("test-reports", "test-artifacts"):
        root = _test_reports_root(layout) if root_name == "test-reports" else _test_artifacts_root(layout)
        for path in _child_directories(root):
            if not _is_profiling_artifact_name(path.name):
                continue
            candidates.extend(
                _direct_heavy_file_candidates(
                    path,
                    cutoff,
                    options.report_payload_retention_hours,
                    category="profiling-artifact",
                    reason_label="profiling",
                )
            )
            candidates.extend(
                _payload_directory_candidates(
                    path,
                    cutoff,
                    options.report_payload_retention_hours,
                    category="profiling-artifact",
                    reason_label="profiling",
                )
            )
    return candidates


def _is_profiling_artifact_name(name: str) -> bool:
    normalized = name.lower()
    return any(marker in normalized for marker in PROFILING_ARTIFACT_NAME_MARKERS)


def _skip_generic_report_family(name: str, options: CleanupOptions) -> bool:
    return not options.include_profiling_artifacts and _is_profiling_artifact_name(name)


def _optional_root(layout: WorkspaceLayout, attribute: str) -> Path:
    value = getattr(layout, attribute, None)
    if value is None:
        return layout.emule_workspace_root / "__missing__" / attribute
    return Path(value)


def _output_root(layout: WorkspaceLayout) -> Path:
    output_root = getattr(layout, "output_root", None)
    if output_root is None:
        raise RuntimeError("WorkspaceLayout.output_root must be configured for cleanup output.")
    return Path(output_root)


def _child_directories(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    return [child for child in path.iterdir() if child.is_dir()]


def _dedupe_candidates(candidates: list[CleanupCandidate]) -> list[CleanupCandidate]:
    by_path: dict[Path, CleanupCandidate] = {}
    selected_paths: set[Path] = set()
    for candidate in sorted(candidates, key=lambda item: len(item.path.resolve().parts)):
        resolved = candidate.path.resolve()
        if any(parent in selected_paths for parent in (resolved, *resolved.parents)):
            continue
        by_path[resolved] = candidate
        selected_paths.add(resolved)
    return sorted(by_path.values(), key=lambda candidate: (candidate.category, str(candidate.path).lower()))


def _directory_candidate(path: Path, category: str, reason: str) -> CleanupCandidate:
    total = 0
    file_count = 0
    estimated = False
    stack = [path]
    while stack:
        current = stack.pop()
        for child in current.iterdir():
            if child.is_dir():
                stack.append(child)
                continue
            if child.is_file():
                total += child.stat().st_size
                file_count += 1
                if file_count >= MAX_DIRECTORY_SIZE_SCAN_FILES:
                    estimated = True
                    stack.clear()
                    break
    return CleanupCandidate(
        path=path,
        kind="directory",
        category=category,
        reason=reason,
        bytes=total,
        files=file_count,
        estimated=estimated,
    )


def _file_candidate(path: Path, category: str, reason: str) -> CleanupCandidate:
    return CleanupCandidate(path=path, kind="file", category=category, reason=reason, bytes=path.stat().st_size, files=1)


def _delete_candidate(candidate: CleanupCandidate) -> None:
    path = _delete_path(candidate.path)
    if candidate.kind == "directory":
        shutil.rmtree(path)
    elif candidate.kind == "file":
        os.unlink(path)
    else:
        raise RuntimeError(f"Unsupported cleanup candidate kind: {candidate.kind}")


def _delete_path(path: Path) -> str:
    resolved = str(path.resolve())
    if os.name != "nt" or resolved.startswith("\\\\?\\"):
        return resolved
    if resolved.startswith("\\\\"):
        return "\\\\?\\UNC\\" + resolved[2:]
    return "\\\\?\\" + resolved


def _cleanup_summary(
    options: CleanupOptions,
    candidates: list[CleanupCandidate],
    *,
    status: str,
    error: str = "",
) -> CleanupRunSummary:
    categories: dict[str, dict[str, int]] = defaultdict(lambda: {"items": 0, "files": 0, "bytes": 0})
    for candidate in candidates:
        category = categories[candidate.category]
        category["items"] += 1
        category["files"] += candidate.files
        category["bytes"] += candidate.bytes
    return CleanupRunSummary(
        profile=options.profile,
        apply=options.apply,
        status=status,
        candidates=len(candidates),
        bytes=sum(candidate.bytes for candidate in candidates),
        files=sum(candidate.files for candidate in candidates),
        categories=dict(categories),
        error=error,
    )


def _print_cleanup_summary(layout: WorkspaceLayout, candidates: list[CleanupCandidate]) -> None:
    totals: dict[str, tuple[int, int, int]] = defaultdict(lambda: (0, 0, 0))
    for candidate in candidates:
        bytes_total, files_total, items_total = totals[candidate.category]
        totals[candidate.category] = (bytes_total + candidate.bytes, files_total + candidate.files, items_total + 1)
    if not candidates:
        print("No generated artifacts matched the selected cleanup policy.")
        return
    print("Cleanup candidates:")
    for category in sorted(totals):
        bytes_total, files_total, items_total = totals[category]
        print(f"  {category}: {items_total} item(s), {files_total} file(s), {_format_bytes(bytes_total)}")
    estimate_marker = "~" if any(candidate.estimated for candidate in candidates) else ""
    print(f"Total reclaimable: {estimate_marker}{_format_bytes(sum(candidate.bytes for candidate in candidates))}")
    if estimate_marker:
        print(f"Some directory size scans were capped at {MAX_DIRECTORY_SIZE_SCAN_FILES} files for responsiveness.")
    print("Largest candidates:")
    for candidate in sorted(candidates, key=lambda item: item.bytes, reverse=True)[:20]:
        print(
            f"  {('~' if candidate.estimated else '') + _format_bytes(candidate.bytes):>9}  {candidate.category:<16} "
            f"{_workspace_relative(layout, candidate.path)}"
        )


def _format_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(value)
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024.0


def _workspace_relative(layout: WorkspaceLayout, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(layout.emule_workspace_root.resolve()))
    except ValueError:
        return str(path)
