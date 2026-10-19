from __future__ import annotations

import json
from pathlib import Path

import pytest

from emule_workspace import certification
from emule_workspace.artifact_names import certification_result_file_name
from emule_workspace.config import CertificationOptions, WorkspaceOptions
from emule_workspace.layout import AppVariant, TestTargets as LayoutTestTargets, WorkspaceLayout


def make_layout(tmp_path: Path) -> WorkspaceLayout:
    emule_workspace_root = tmp_path
    workspace_root = emule_workspace_root / "workspaces" / "workspace"
    output_root = emule_workspace_root.parent / f"{emule_workspace_root.name}-output"
    tests_repo_root = emule_workspace_root / "repos" / "emulebb-build-tests"
    app_root = workspace_root / "app" / "emulebb-main"
    for path in (
        output_root / "reports",
        app_root,
        emule_workspace_root / "repos" / "emulebb-build",
        emule_workspace_root / "repos" / "emulebb-tooling",
    ):
        path.mkdir(parents=True)
    return WorkspaceLayout(
        emule_workspace_root=emule_workspace_root,
        workspace_name="workspace",
        workspace_root=workspace_root,
        build_repo_root=emule_workspace_root / "repos" / "emulebb-build",
        tests_repo_root=tests_repo_root,
        tooling_repo_root=emule_workspace_root / "repos" / "emulebb-tooling",
        ed2k_server_repo_root=emule_workspace_root / "repos" / "ed2k-server",
        amule_repo_root=emule_workspace_root / "repos" / "amule",
        seed_repo_path=emule_workspace_root / "repos" / "emulebb",
        seed_repo_branch="main",
        dependencies=(),
        app_variants=(AppVariant(name="main", path=app_root, branch="main"),),
        test_targets=LayoutTestTargets(test_build_variant="main", test_run_variant="main", baseline_variant="community"),
        toolset_override_variable="",
        output_root=output_root,
    )


def latest_certification_report(layout: WorkspaceLayout) -> Path:
    reports = sorted((layout.output_reports_root / "certification").glob(f"*/{certification_result_file_name()}"))
    assert reports
    return reports[-1]


def test_certification_step_plan_is_two_tier() -> None:
    fast = certification.get_certification_step_plan("fast")
    overnight = certification.get_certification_step_plan("overnight")

    assert [step.name for step in fast] == [
        "validate",
        "build-app-debug-x64",
        "build-app-release-x64",
        "build-app-release-arm64",
        "build-tests-debug-x64",
        "build-tests-release-x64",
        "python-harness",
        "test-all-debug-x64",
        "test-all-release-x64",
        "live-fast-ui-rest",
    ]
    assert [step.name for step in overnight[: len(fast)]] == [step.name for step in fast]
    # Stock/community parity comparisons are on-demand only (test protocol-parity /
    # test community-core-coverage), not part of any auto-run tier.
    assert "protocol-parity" not in [step.name for step in overnight]
    assert "community-core-coverage" not in [step.name for step in overnight]
    assert "live-stabilization-stress" in [step.name for step in overnight]
    assert "live-release-expanded" in [step.name for step in overnight]
    assert "live-ui-resource-depth" in [step.name for step in overnight]
    assert "amutorrent-clean-startup" in [step.name for step in overnight]
    assert "amutorrent-emulebb-ui" in [step.name for step in overnight]
    assert "amutorrent-resilience" in [step.name for step in overnight]


def test_certification_quick_tier_is_lean_subset_of_fast() -> None:
    quick = certification.get_certification_step_plan("quick")
    names = [step.name for step in quick]

    assert names == [
        "validate",
        "build-app-debug-x64",
        "build-tests-debug-x64",
        "python-harness",
        "test-all-debug-x64",
        "live-fast-ui-rest",
    ]
    fast_names = [step.name for step in certification.get_certification_step_plan("fast")]
    assert set(names) <= set(fast_names)
    # The inner loop omits the release-config native pass and the ARM64 build.
    assert "test-all-release-x64" not in names
    assert "build-app-release-arm64" not in names


def test_quick_profile_skips_native_coverage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    captured: list[bool] = []

    def fake_test_runs(_layout, _options, *, coverage: bool = True) -> None:
        captured.append(coverage)

    monkeypatch.setattr(certification, "invoke_test_runs", fake_test_runs)
    options = WorkspaceOptions(workspace_root=tmp_path, platform="x64")

    certification._invoke_step(layout, options, CertificationOptions(profile="quick"), "test-all-debug-x64")
    certification._invoke_step(layout, options, CertificationOptions(profile="fast"), "test-all-release-x64")

    # Quick drops the doubling coverage re-run; the fast gate keeps it.
    assert captured == [False, True]


def test_certification_writes_single_passing_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)
    calls: list[str] = []

    def fake_invoke_step(_layout, _options, _certification_options, name):
        calls.append(name)

    monkeypatch.setattr(certification, "_invoke_step", fake_invoke_step)

    certification.invoke_certification(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        CertificationOptions(profile="fast"),
    )

    report = json.loads(latest_certification_report(layout).read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert report["profile"] == "fast"
    assert calls == [step.name for step in certification.get_certification_step_plan("fast")]
    assert [step["status"] for step in report["steps"]] == ["passed"] * len(calls)
    assert report["options"]["pre_run_cleanup"] is True
    assert report["options"]["test_network"] == "default"
    assert report["pre_run_cleanup"]["status"] == "passed"


def test_certification_records_pre_run_cleanup_before_first_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)
    calls: list[str] = []

    def fake_cleanup(_layout):
        calls.append("cleanup")
        return certification.CleanupRunSummary("routine", True, "passed", 0, 0, 0, {})

    def fake_invoke_step(_layout, _options, _certification_options, name):
        calls.append(name)

    monkeypatch.setattr(certification, "run_pre_test_cleanup", fake_cleanup)
    monkeypatch.setattr(certification, "_invoke_step", fake_invoke_step)

    certification.invoke_certification(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        CertificationOptions(profile="fast"),
    )

    assert calls[0] == "cleanup"
    assert calls[1:] == [step.name for step in certification.get_certification_step_plan("fast")]


def test_certification_records_failed_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)
    calls: list[str] = []

    def fake_invoke_step(_layout, _options, _certification_options, name):
        calls.append(name)
        if name == "build-tests-release-x64":
            raise RuntimeError("native test build failed")

    monkeypatch.setattr(certification, "_invoke_step", fake_invoke_step)

    with pytest.raises(RuntimeError, match="build-tests-release-x64"):
        certification.invoke_certification(
            layout,
            WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
            CertificationOptions(profile="fast"),
        )

    report = json.loads(latest_certification_report(layout).read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    failed = [step for step in report["steps"] if step["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["name"] == "build-tests-release-x64"
    assert failed[0]["error"] == "native test build failed"
    assert calls[-1] == "build-tests-release-x64"


def test_certification_can_continue_after_failed_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)
    calls: list[str] = []

    def fake_invoke_step(_layout, _options, _certification_options, name):
        calls.append(name)
        if name == "build-tests-release-x64":
            raise RuntimeError("native test build failed")

    monkeypatch.setattr(certification, "_invoke_step", fake_invoke_step)

    with pytest.raises(RuntimeError, match="completed with status failed"):
        certification.invoke_certification(
            layout,
            WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
            CertificationOptions(profile="fast", continue_on_failure=True),
        )

    expected = [step.name for step in certification.get_certification_step_plan("fast")]
    report = json.loads(latest_certification_report(layout).read_text(encoding="utf-8"))
    assert calls == expected
    assert report["status"] == "failed"
    assert report["options"]["continue_on_failure"] is True
    assert report["steps"][-1]["name"] == "live-fast-ui-rest"
    assert [step["name"] for step in report["steps"]] == expected


def test_certification_preserves_inconclusive_child_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)

    def fake_invoke_step(_layout, _options, _certification_options, name):
        if name == "live-fast-ui-rest":
            child_report = layout.output_reports_root / "live-e2e-suite" / "latest"
            child_report.mkdir(parents=True)
            (child_report / "live-e2e-suite-result.json").write_text(
                json.dumps({"status": "inconclusive", "has_inconclusive_suites": True}) + "\n",
                encoding="utf-8",
            )
            raise RuntimeError("live network evidence was inconclusive")

    monkeypatch.setattr(certification, "_invoke_step", fake_invoke_step)

    with pytest.raises(RuntimeError, match="live-fast-ui-rest"):
        certification.invoke_certification(
            layout,
            WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
            CertificationOptions(profile="fast"),
        )

    report = json.loads(latest_certification_report(layout).read_text(encoding="utf-8"))
    assert report["status"] == "inconclusive"
    assert report["steps"][-1]["name"] == "live-fast-ui-rest"
    assert report["steps"][-1]["status"] == "inconclusive"


def test_fast_live_step_uses_critical_ui_and_rest_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)
    captured: dict[str, object] = {}

    def fake_live_e2e(_layout, _options, live_options):
        captured["live_options"] = live_options

    monkeypatch.setattr(certification, "invoke_live_e2e_suite", fake_live_e2e)

    certification._invoke_step(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        CertificationOptions(profile="fast", live_wire_inputs_file="inputs.json"),
        "live-fast-ui-rest",
    )

    live_options = captured["live_options"]
    assert live_options.suites == (
        "preference-ui",
        "shared-files-ui",
        "config-stability-ui",
        "shared-hash-ui",
        "startup-diagnostics",
        "shared-directories-rest",
        "rest-api",
    )
    assert live_options.fail_fast is True
    assert live_options.test_network == "default"
    assert live_options.live_wire_inputs_file == "inputs.json"


def test_overnight_stress_step_enables_cpu_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)
    captured: dict[str, object] = {}

    def fake_live_e2e(_layout, _options, live_options):
        captured["live_options"] = live_options

    monkeypatch.setattr(certification, "invoke_live_e2e_suite", fake_live_e2e)

    certification._invoke_step(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        CertificationOptions(profile="overnight"),
        "live-stabilization-stress",
    )

    live_options = captured["live_options"]
    assert live_options.profile == "stabilization-stress"
    assert live_options.fail_fast is True
    assert live_options.profile_cpu is True
    assert live_options.profile_cpu_stack is True
    assert live_options.profile_memory is True
    assert live_options.rest_cold_start_dump_stress_cpu_profile is True
    assert live_options.rest_cold_start_dump_stress_cpu_profile_stack is True


def test_overnight_vpn_only_steps_are_skipped_by_default_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)
    calls: list[str] = []

    def fake_invoke_step(_layout, _options, _certification_options, name):
        calls.append(name)

    monkeypatch.setattr(certification, "_invoke_step", fake_invoke_step)

    certification.invoke_certification(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        CertificationOptions(profile="overnight", pre_run_cleanup=False),
    )

    report = json.loads(latest_certification_report(layout).read_text(encoding="utf-8"))
    skipped = [step for step in report["steps"] if step["status"] == "skipped"]
    assert [step["name"] for step in skipped] == [
        "amutorrent-clean-startup",
        "amutorrent-emulebb-ui",
        "amutorrent-resilience",
    ]
    assert all("requires vpn" in step["error"] for step in skipped)
    assert not any(name.startswith("amutorrent-") for name in calls)


def test_overnight_release_steps_use_broad_live_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)
    captured: list[object] = []

    def fake_live_e2e(_layout, _options, live_options):
        captured.append(live_options)

    monkeypatch.setattr(certification, "invoke_live_e2e_suite", fake_live_e2e)

    for step_name in ("live-release-expanded", "live-ui-resource-depth"):
        certification._invoke_step(
            layout,
            WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
            CertificationOptions(profile="overnight"),
            step_name,
        )

    assert [item.profile for item in captured] == ["release-expanded", "ui-resource-depth"]
    assert [item.fail_fast for item in captured] == [True, True]
    assert [item.pre_run_cleanup for item in captured] == [False, False]
    assert captured[0].profile_cpu is True
    assert captured[0].profile_cpu_stack is True
    assert captured[0].profile_memory is True
    assert captured[0].admin_volume_fixtures is True
    assert captured[1].profile_cpu is False
    assert captured[1].profile_memory is False
