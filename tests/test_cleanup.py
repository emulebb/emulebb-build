from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from emule_workspace import cleanup
from emule_workspace.cleanup import plan_cleanup
from emule_workspace.config import CleanupOptions


def test_routine_cleanup_selects_old_generated_artifacts(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    old_payload = write_file(layout.output_root / "reports" / "rest-api-smoke" / "20260501-run" / "temp" / "001.part", 10)
    recent_payload = write_file(layout.output_root / "reports" / "rest-api-smoke" / "latest" / "temp" / "001.part", 10)
    old_build_log = write_file(layout.output_root / "logs" / "builds" / "20260401T120000Z-build-app" / "build-result.json", 10)
    old_arr_output = write_file(layout.output_root / "artifacts" / "arr-acquisition" / "radarr" / "movie.mkv", 10)
    old_live_artifact = write_file(layout.output_root / "artifacts" / "live-e2e-suite" / "20260501T120000Z-emulebb-main-release-pid100" / "live-e2e-suite-result.json", 10)
    recent_live_artifact = write_file(layout.output_root / "artifacts" / "live-e2e-suite" / "20260521T120000Z-emulebb-main-release-pid100" / "live-e2e-suite-result.json", 10)
    cache_file = write_file(layout.build_repo_root / ".pytest_cache" / "README.md", 10)
    release_rehearsal = write_file(layout.output_root / "release" / "emulebb-v1.0.1" / "package.zip", 10)
    for path in (old_payload, old_build_log, old_arr_output, old_live_artifact, cache_file, release_rehearsal):
        make_old(path, tmp_path)

    candidates = plan_cleanup(layout, CleanupOptions(report_run_retention_days=3650.0, keep_build_log_runs=0))
    candidate_paths = {candidate.path for candidate in candidates}
    categories = {candidate.category for candidate in candidates}

    assert old_payload.parent in candidate_paths
    assert recent_payload.parent not in candidate_paths
    assert old_build_log.parent in candidate_paths
    assert old_arr_output.parent in candidate_paths
    assert old_live_artifact.parent in candidate_paths
    assert recent_live_artifact.parent not in candidate_paths
    assert cache_file.parent in candidate_paths
    assert release_rehearsal.parent not in candidate_paths
    assert categories == {"arr-acquisition", "build-logs", "caches", "report-payload", "test-artifacts"}


def test_release_state_cleanup_is_explicit(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    current_release = write_file(layout.output_root / "release" / "emulebb-v0.7.3-rc.2" / "package.zip", 10)
    rehearsal_release = write_file(layout.output_root / "release" / "emulebb-v1.0.1" / "package.zip", 10)

    candidates = plan_cleanup(layout, CleanupOptions(include_release_state=True))
    candidate_paths = {candidate.path for candidate in candidates}

    assert current_release.parent not in candidate_paths
    assert rehearsal_release.parent in candidate_paths


def test_package_build_outputs_are_explicit_build_output_cleanup(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    package_build_output = write_file(layout.output_root / "packages" / "build" / "emulebb-v0.7.3-rc.2" / "x64" / "app" / "emulebb.exe", 10)
    canonical_third_party_output = write_file(layout.output_root / "builds" / "third_party" / "emulebb-zlib" / "x64" / "Release" / "zlib.lib", 10)
    amule_output = write_file(layout.output_root / "builds" / "amule" / "windows-x64" / "src" / "amuled.exe", 10)
    native_test_output = write_file(layout.output_root / "builds" / "tests" / "tag" / "x64" / "Release" / "bin" / "emule-tests.exe", 10)

    routine_candidates = plan_cleanup(layout, CleanupOptions())
    build_candidates = plan_cleanup(layout, CleanupOptions(include_build_outputs=True))

    assert package_build_output.parents[3] not in {candidate.path for candidate in routine_candidates}
    assert package_build_output.parents[3] in {candidate.path for candidate in build_candidates}
    assert canonical_third_party_output.parents[3] in {candidate.path for candidate in build_candidates}
    assert amule_output.parents[2] in {candidate.path for candidate in build_candidates}
    assert native_test_output.parents[4] in {candidate.path for candidate in build_candidates}


def test_third_party_dependency_outputs_are_explicit_build_output_cleanup(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    third_party = layout.emule_workspace_root / "repos" / "third_party"
    cryptopp_lib = write_file(third_party / "emulebb-cryptopp" / "x64" / "Output" / "Release" / "cryptlib.lib", 10)
    zlib_output = write_file(third_party / "emulebb-zlib" / "cmake-build-x64" / "zlib.lib", 10)
    miniupnp_header = write_file(third_party / "emulebb-miniupnp" / "miniupnpc" / "rc_version.h", 10)

    routine_candidates = plan_cleanup(layout, CleanupOptions())
    build_candidates = plan_cleanup(layout, CleanupOptions(include_build_outputs=True))
    routine_paths = {candidate.path for candidate in routine_candidates}
    build_paths = {candidate.path for candidate in build_candidates}

    assert cryptopp_lib.parents[2] not in routine_paths
    assert zlib_output.parent not in routine_paths
    assert miniupnp_header not in routine_paths
    assert cryptopp_lib.parents[2] in build_paths
    assert zlib_output.parent in build_paths
    assert miniupnp_header in build_paths


def test_product_family_outputs_are_explicit_cleanup(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    rust_target = write_file(layout.emulebb_rust_repo_root / "target" / "debug" / "emulebb-rust.exe", 10)
    coordinator_modules = write_file(
        layout.p2p_overlord_be_repo_root / "overlord-be-coordinator" / "node_modules" / "pkg" / "index.js",
        10,
    )

    routine_candidates = plan_cleanup(layout, CleanupOptions())
    product_candidates = plan_cleanup(layout, CleanupOptions(include_product_family_outputs=True))
    deep_candidates = plan_cleanup(layout, CleanupOptions(profile="deep"))

    assert rust_target.parents[1] not in {candidate.path for candidate in routine_candidates}
    assert rust_target.parents[1] in {candidate.path for candidate in product_candidates}
    assert coordinator_modules.parents[1] not in {candidate.path for candidate in routine_candidates}
    assert coordinator_modules.parents[1] in {candidate.path for candidate in product_candidates}
    assert coordinator_modules.parents[1] in {candidate.path for candidate in deep_candidates}


def test_root_legacy_state_and_logs_are_explicit_cleanup(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    root_state = write_file(layout.emule_workspace_root / "state" / "smoke" / "result.json", 10)
    nested_state = write_file(layout.build_repo_root / "workspaces" / "workspace" / "state" / "split-tunnel-tools" / "settings.json", 10)
    legacy_log = write_file(layout.emule_workspace_root / "eMule-workspace.log", 10)
    current_log = write_file(layout.emule_workspace_root / "emulebb-workspace.log", 10)

    routine_candidates = plan_cleanup(layout, CleanupOptions())
    legacy_candidates = plan_cleanup(
        layout,
        CleanupOptions(include_root_legacy_state=True, include_legacy_root_logs=True),
    )

    assert root_state.parent not in {candidate.path for candidate in routine_candidates}
    assert nested_state.parents[1] not in {candidate.path for candidate in routine_candidates}
    assert legacy_log not in {candidate.path for candidate in routine_candidates}
    assert root_state.parent in {candidate.path for candidate in legacy_candidates}
    assert nested_state.parents[1] in {candidate.path for candidate in legacy_candidates}
    assert legacy_log in {candidate.path for candidate in legacy_candidates}
    assert current_log not in {candidate.path for candidate in legacy_candidates}


def test_profiling_artifacts_are_routine_cleanup_with_retention(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    diagnostic_dump = write_file(layout.output_root / "reports" / "diagnostics" / "pid-100-cpu-spikes" / "sample.dmp", 10)
    recent_diagnostic_dump = write_file(layout.output_root / "reports" / "diagnostics" / "pid-200-cpu-spikes" / "sample.dmp", 10)
    pageheap_report = write_file(layout.output_root / "reports" / "real-live-pageheap" / "trace.etl", 10)
    normal_report = write_file(layout.output_root / "reports" / "live-e2e-suite" / "result.json", 10)
    crash_evidence = write_file(layout.output_root / "reports" / "crash-evidence" / "crash.dmp", 10)
    for path in (diagnostic_dump, pageheap_report, crash_evidence):
        make_old(path, tmp_path)

    routine_candidates = plan_cleanup(layout, CleanupOptions())
    skipped_candidates = plan_cleanup(layout, CleanupOptions(include_profiling_artifacts=False))

    assert diagnostic_dump.parent in {candidate.path for candidate in routine_candidates}
    assert recent_diagnostic_dump.parent not in {candidate.path for candidate in routine_candidates}
    assert pageheap_report in {candidate.path for candidate in routine_candidates}
    assert crash_evidence in {candidate.path for candidate in routine_candidates}
    assert normal_report.parent not in {candidate.path for candidate in routine_candidates}
    assert diagnostic_dump.parent not in {candidate.path for candidate in skipped_candidates}
    assert all(
        candidate.category != "profiling-artifact"
        for candidate in skipped_candidates
        if candidate.path == pageheap_report
    )


def test_legacy_build_test_reports_are_routine_cleanup(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    old_run = write_file(layout.tests_repo_root / "reports" / "rest-api-smoke" / "20260501-run" / "result.json", 10)
    recent_run = write_file(layout.tests_repo_root / "reports" / "rest-api-smoke" / "20260525-run" / "result.json", 10)
    old_latest = write_file(layout.tests_repo_root / "reports" / "rest-api-smoke-latest" / "result.json", 10)
    old_root_file = write_file(layout.tests_repo_root / "reports" / "test-run-parity.log", 10)
    for path in (old_run, old_latest, old_root_file):
        make_old(path, tmp_path)

    routine_candidates = plan_cleanup(layout, CleanupOptions())
    skipped_candidates = plan_cleanup(layout, CleanupOptions(include_legacy_test_reports=False))
    routine_paths = {candidate.path for candidate in routine_candidates}
    skipped_paths = {candidate.path for candidate in skipped_candidates}

    assert old_run.parent in routine_paths
    assert recent_run.parent not in routine_paths
    assert old_latest.parent in routine_paths
    assert old_root_file in routine_paths
    assert old_run.parent not in skipped_paths
    assert old_latest.parent not in skipped_paths
    assert old_root_file not in skipped_paths


def test_cleanup_selects_generated_state_paths_with_trailing_space_or_dot(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    trailing_space_dir = (
        layout.output_root
        / "reports"
        / "shared-directories-rest"
        / "latest"
        / "shared-rest-exact-names. "
    )
    trailing_space_dir.parent.mkdir(parents=True)
    os.mkdir("\\\\?\\" + str(trailing_space_dir))
    trailing_dot_file = layout.output_root / "artifacts" / "suite" / "run" / "result."
    trailing_dot_file.parent.mkdir(parents=True)
    with open("\\\\?\\" + str(trailing_dot_file), "wb") as stream:
        stream.write(b"x" * 10)

    candidates = plan_cleanup(layout, CleanupOptions())
    candidate_paths = {candidate.path for candidate in candidates}
    path_anomalies = {candidate.path for candidate in candidates if candidate.category == "path-anomaly"}

    assert trailing_space_dir in candidate_paths
    assert trailing_dot_file in candidate_paths
    assert {trailing_space_dir, trailing_dot_file} <= path_anomalies
    for candidate in candidates:
        if candidate.path in {trailing_space_dir, trailing_dot_file}:
            cleanup._delete_candidate(candidate)


def test_delete_candidate_uses_windows_long_path_prefix(tmp_path: Path, monkeypatch) -> None:
    layout = make_layout(tmp_path)
    edge_path = layout.output_root / "reports" / "shared-directories-rest" / "20260501-run" / "shared-rest-exact-names. "
    candidate = cleanup.CleanupCandidate(edge_path, "directory", "report-run", "windows path edge case", 10, 1)
    removed_paths: list[str] = []

    monkeypatch.setattr(cleanup.os, "name", "nt")
    monkeypatch.setattr(cleanup.shutil, "rmtree", lambda path: removed_paths.append(path))

    cleanup._delete_candidate(candidate)

    assert removed_paths
    assert removed_paths[0].startswith("\\\\?\\")
    assert "shared-rest-exact-names. " in removed_paths[0]


def write_file(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def make_old(path: Path, stop: Path) -> None:
    old_timestamp = 1_700_000_000
    os.utime(path, (old_timestamp, old_timestamp))
    current = path.parent
    while current != stop.parent:
        os.utime(current, (old_timestamp, old_timestamp))
        current = current.parent


def make_layout(tmp_path: Path):
    workspace_root = tmp_path / "workspaces" / "workspace"
    return SimpleNamespace(
        emule_workspace_root=tmp_path,
        output_root=tmp_path / "output",
        workspace_root=workspace_root,
        build_repo_root=tmp_path / "repos" / "emulebb-build",
        tests_repo_root=tmp_path / "repos" / "emulebb-build-tests",
        tooling_repo_root=tmp_path / "repos" / "emulebb-tooling",
        app_variants=(),
        dependencies=(),
        ed2k_server_repo_root=tmp_path / "repos" / "ed2k-server",
        emulebb_rust_repo_root=tmp_path / "repos" / "emulebb-rust",
        p2p_overlord_agents_repo_root=tmp_path / "repos" / "p2p-overlord-agents",
        p2p_overlord_be_repo_root=tmp_path / "repos" / "p2p-overlord-be",
        output_third_party_build_root=tmp_path / "output" / "builds" / "third_party",
    )
