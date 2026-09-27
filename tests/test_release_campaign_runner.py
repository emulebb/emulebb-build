from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from emule_workspace import release_campaign_runner
from emule_workspace.artifact_names import release_campaign_result_file_name
from emule_workspace.config import ReleaseCampaignOptions, WorkspaceOptions
from emule_workspace.layout import AppVariant, TestTargets as LayoutTestTargets, WorkspaceLayout


def make_layout(tmp_path: Path) -> WorkspaceLayout:
    emule_workspace_root = tmp_path
    workspace_root = emule_workspace_root / "workspaces" / "workspace"
    output_root = emule_workspace_root.parent / f"{emule_workspace_root.name}-output"
    tests_repo_root = emule_workspace_root / "repos" / "emulebb-build-tests"
    rust_repo_root = emule_workspace_root / "repos" / "emulebb-rust"
    app_root = workspace_root / "app" / "emulebb-main"
    for path in (
        tests_repo_root / "manifests" / "release-campaigns",
        output_root / "reports",
        app_root,
        emule_workspace_root / "repos" / "emulebb-build",
        rust_repo_root,
        emule_workspace_root / "repos" / "emulebb-tooling" / "ci",
    ):
        path.mkdir(parents=True, exist_ok=True)
    return WorkspaceLayout(
        emule_workspace_root=emule_workspace_root,
        workspace_name="workspace",
        workspace_root=workspace_root,
        build_repo_root=emule_workspace_root / "repos" / "emulebb-build",
        tests_repo_root=tests_repo_root,
        tooling_repo_root=emule_workspace_root / "repos" / "emulebb-tooling",
        ed2k_server_repo_root=emule_workspace_root / "repos" / "goed2k-server",
        amule_repo_root=emule_workspace_root / "repos" / "amule",
        emulebb_rust_repo_root=rust_repo_root,
        seed_repo_path=emule_workspace_root / "repos" / "emulebb",
        seed_repo_branch="main",
        dependencies=(),
        app_variants=(AppVariant(name="main", path=app_root, branch="main"),),
        test_targets=LayoutTestTargets(test_build_variant="main", test_run_variant="main", baseline_variant="community"),
        toolset_override_variable="",
        output_root=output_root,
    )


def test_workspace_repo_heads_includes_rust_product(tmp_path: Path, monkeypatch) -> None:
    layout = make_layout(tmp_path)
    monkeypatch.setattr(
        release_campaign_runner,
        "_is_git_worktree",
        lambda path: path == layout.emulebb_rust_repo_root,
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "_repo_head_payload",
        lambda path: {"path": str(path), "head": "rust-head"},
    )

    heads = release_campaign_runner._workspace_repo_heads(layout)

    assert heads == {
        "emulebb-rust": {
            "path": str(layout.emulebb_rust_repo_root),
            "head": "rust-head",
        }
    }


def write_campaign(layout: WorkspaceLayout, campaign: dict[str, object]) -> None:
    manifest_path = layout.tests_repo_root / "manifests" / "release-campaigns" / "test-campaign.json"
    manifest_path.write_text(json.dumps(campaign), encoding="utf-8")


def write_campaign_scenario_catalog(layout: WorkspaceLayout) -> None:
    module_dir = layout.tests_repo_root / "emule_test_harness"
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "campaign_scenarios.py").write_text(
        """
from dataclasses import dataclass

@dataclass(frozen=True)
class Spec:
    key: str
    scenario_id: str
    local_profile: str
    local_suites: tuple[str, ...]
    vm_profile: str
    uses_local_swarm: bool = True

SPEC = Spec(
    key="search-ui-local-swarm",
    scenario_id="emulebb.flow.ui.search.local-swarm.v1",
    local_profile="multi-client-p2p",
    local_suites=("local-ed2k-search-soak", "local-kad-swarm"),
    vm_profile="search-ui-local-swarm-vm",
)
REUSABLE_CAMPAIGN_SCENARIO_BY_KEY = {SPEC.key: SPEC}
REUSABLE_CAMPAIGN_SCENARIO_BY_SCENARIO_ID = {SPEC.scenario_id: SPEC}
REUSABLE_CAMPAIGN_SCENARIO_BY_VM_PROFILE = {SPEC.vm_profile: SPEC}
LOCAL_SWARM_TIER_OPTIONS = {
    1: {
        "stage": "launch-scale",
        "total_client_count": 4,
        "peer_transfer_count": 24,
        "harness_transfer_count": 24,
        "emulebb_files": 80,
        "extra_emulebb_files": 8,
        "harness_files": 60,
        "amule_files": 20,
        "adverse_kill_cycles": 0,
        "adverse_kill_warmup_seconds": 0.0,
        "adverse_recovery_timeout_seconds": 180.0,
        "cpu_profile": False,
        "fail_fast": True,
    },
    2: {
        "stage": "launch-scale",
        "total_client_count": 12,
        "peer_transfer_count": 144,
        "harness_transfer_count": 144,
        "emulebb_files": 288,
        "extra_emulebb_files": 28,
        "harness_files": 216,
        "amule_files": 66,
        "adverse_kill_cycles": 1,
        "adverse_kill_warmup_seconds": 5.0,
        "adverse_recovery_timeout_seconds": 210.0,
        "cpu_profile": True,
        "fail_fast": False,
    },
    3: {
        "stage": "full",
        "total_client_count": 18,
        "peer_transfer_count": 360,
        "harness_transfer_count": 360,
        "emulebb_files": 720,
        "extra_emulebb_files": 72,
        "harness_files": 480,
        "amule_files": 120,
        "adverse_kill_cycles": 2,
        "adverse_kill_warmup_seconds": 20.0,
        "adverse_recovery_timeout_seconds": 180.0,
        "cpu_profile": True,
        "fail_fast": False,
    },
}
""".lstrip(),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def workspace_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_root = tmp_path.parent / f"{tmp_path.name}-output"
    cargo_target_dir = output_root / "builds" / "rust" / "target"
    cargo_target_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EMULEBB_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("EMULEBB_WORKSPACE_OUTPUT_ROOT", str(output_root))
    monkeypatch.setenv("CARGO_TARGET_DIR", str(cargo_target_dir))


def campaign_payload() -> dict[str, object]:
    return {
        "kind": "instance",
        "campaignId": "test-campaign",
        "releaseVersion": "0.0.0",
        "phases": [
            {
                "id": "preflight",
                "scenarios": [
                    {
                        "id": "validate",
                        "command": "python -m emule_workspace validate",
                        "blocking": True,
                    },
                    {
                        "id": "python",
                        "command": "python -m emule_workspace test python --quiet",
                        "blocking": True,
                    },
                ],
            },
            {
                "id": "controller-surface",
                "scenarios": [
                    {
                        "id": "rest",
                        "command": "python -m emule_workspace test live-e2e --profile controller-surface",
                        "blocking": True,
                    },
                    {
                        "id": "amutorrent",
                        "command": "python -m emule_workspace test live-e2e --profile controller-surface",
                        "blocking": True,
                    },
                ],
            },
            {
                "id": "stabilization-stress",
                "scenarios": [
                    {
                        "id": "optional",
                        "command": "python -m emule_workspace test certification --profile overnight",
                        "blocking": False,
                    },
                ],
            },
        ],
    }


def test_campaign_execution_plan_dedupes_shared_commands_and_skips_nonblocking() -> None:
    campaign = campaign_payload()

    plan = release_campaign_runner.build_release_campaign_execution_plan(
        campaign,
        ReleaseCampaignOptions(campaign="test-campaign", execute=True),
    )

    assert [item.command for item in plan] == [
        "python -m emule_workspace validate",
        "python -m emule_workspace test python --quiet",
        "python -m emule_workspace test live-e2e --profile controller-surface",
    ]
    assert plan[2].scenario_ids == ("amutorrent", "rest")


def test_campaign_execution_plan_can_include_nonblocking_optional_commands() -> None:
    plan = release_campaign_runner.build_release_campaign_execution_plan(
        campaign_payload(),
        ReleaseCampaignOptions(campaign="test-campaign", execute=True, include_nonblocking=True),
    )

    assert plan[-1].command == "python -m emule_workspace test certification --profile overnight"


def test_campaign_execute_dry_run_writes_planned_report(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    write_campaign(layout, campaign_payload())

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(campaign="test-campaign", execute=True, dry_run=True),
    )

    reports = sorted((layout.output_reports_root / "release-campaign-runs").glob(f"*/{release_campaign_result_file_name()}"))
    assert reports
    payload = json.loads(reports[-1].read_text(encoding="utf-8"))
    assert payload["status"] == "planned"
    assert "repoHeads" in payload
    assert payload["options"]["localVmSwarmExecutionMode"] == "manifest"
    assert len(payload["plannedCommands"]) == 3
    assert all(row["status"] == "planned" for row in payload["commands"])
    assert payload["commands"][0]["scenarioEvidence"][0]["scenarioId"] == "validate"
    assert payload["commands"][0]["scenarioEvidence"][0]["commandStatus"] == "planned"
    assert payload["commands"][0]["scenarioEvidence"][0]["evidenceStatus"] == "planned"


def test_campaign_status_fails_required_blocking_missing_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    layout = make_layout(tmp_path)
    result = release_campaign_runner.CampaignCommandResult(
        command="python proof.py",
        phase_ids=("proof",),
        scenario_ids=("required-proof",),
        status="passed",
        duration_seconds=1.0,
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "_command_scenario_evidence",
        lambda *_args: [
            {
                "required": True,
                "blocking": True,
                "evidenceStatus": "missing-evidence",
            }
        ],
    )

    assert (
        release_campaign_runner._aggregate_status(
            layout,
            {},
            [result],
            dry_run=False,
        )
        == "failed"
    )


def test_campaign_status_ignores_nonblocking_missing_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    layout = make_layout(tmp_path)
    result = release_campaign_runner.CampaignCommandResult(
        command="python optional.py",
        phase_ids=("proof",),
        scenario_ids=("optional-proof",),
        status="passed",
        duration_seconds=1.0,
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "_command_scenario_evidence",
        lambda *_args: [
            {
                "required": False,
                "blocking": False,
                "evidenceStatus": "missing-evidence",
            }
        ],
    )

    assert (
        release_campaign_runner._aggregate_status(
            layout,
            {},
            [result],
            dry_run=False,
        )
        == "passed"
    )


def test_package_manifest_evidence_is_summarized_with_hashes(tmp_path: Path) -> None:
    manifest = tmp_path / "package.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "asset": "emulebb-0.7.3-rc.2-x64.zip",
                "assetPath": "emulebb-0.7.3-rc.2-x64.zip",
                "sha256": "a" * 64,
                "sbomPath": "emulebb-0.7.3-rc.2-x64.sbom.spdx.json",
                "sbomSha256": "b" * 64,
            }
        ),
        encoding="utf-8",
    )

    report = release_campaign_runner._augment_evidence_report(
        {
            "kind": "json-status",
            "required": True,
            "status": "present",
            "path": str(manifest),
        }
    )

    assert report["package"] == {
        "asset": "emulebb-0.7.3-rc.2-x64.zip",
        "assetPath": "emulebb-0.7.3-rc.2-x64.zip",
        "sha256": "a" * 64,
        "sbomPath": "emulebb-0.7.3-rc.2-x64.sbom.spdx.json",
        "sbomSha256": "b" * 64,
    }


def test_campaign_report_records_local_vm_swarm_scenario_context(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    campaign = campaign_payload()
    local_command = (
        "python -m emule_workspace test campaign-scenario "
        "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode local --swarm-tier 1 "
        "--local-swarm-mode execute"
    )
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "emulebb.flow.ui.search.local-swarm.v1",
            "title": "Search UI local swarm",
            "phase": "controller-surface",
            "flowCategory": "local-vm-swarm",
            "command": local_command,
            "localCommand": local_command,
            "vmCommand": (
                "python -m emule_workspace test campaign-scenario "
                "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode vm "
                "--release-version 0.7.4-rc.2 --skip-build --swarm-tier 1 "
                "--local-swarm-mode plan --dry-run"
            ),
            "executionMode": "local",
            "executionModes": ["local", "vm"],
            "localProfile": "multi-client-p2p",
            "vmProfile": "search-ui-local-swarm-vm",
            "controlBindScope": "lan",
            "amutorrentBindScope": "lan",
            "p2pMode": "local-swarm",
            "p2pBindScope": "lan",
            "blocking": True,
        }
    ]
    write_campaign(layout, campaign)

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(campaign="test-campaign", phase="controller-surface", execute=True, dry_run=True),
    )

    reports = sorted((layout.output_reports_root / "release-campaign-runs").glob(f"*/{release_campaign_result_file_name()}"))
    payload = json.loads(reports[-1].read_text(encoding="utf-8"))
    assert payload["plannedCommands"][0]["selectedCampaignScenarioMode"] == "local"
    assert payload["commands"][0]["selectedCampaignScenarioMode"] == "local"
    planned_context = payload["plannedCommands"][0]["scenarioContexts"][0]
    command_context = payload["commands"][0]["scenarioContexts"][0]
    assert planned_context == command_context
    assert planned_context["flowCategory"] == "local-vm-swarm"
    assert planned_context["executionModes"] == ["local", "vm"]
    assert planned_context["localProfile"] == "multi-client-p2p"
    assert planned_context["vmProfile"] == "search-ui-local-swarm-vm"
    assert planned_context["controlBindScope"] == "lan"
    assert planned_context["amutorrentBindScope"] == "lan"
    assert planned_context["p2pMode"] == "local-swarm"
    assert planned_context["p2pBindScope"] == "lan"


def test_campaign_execute_dispatches_supported_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)
    write_campaign(layout, campaign_payload())
    calls: list[str] = []

    monkeypatch.setattr(release_campaign_runner, "validate_workspace", lambda _layout: calls.append("validate"))
    monkeypatch.setattr(
        release_campaign_runner,
        "run_pre_test_cleanup",
        lambda _layout: calls.append("cleanup") or release_campaign_runner.CleanupRunSummary("routine", True, "passed", 0, 0, 0, {}),
    )
    monkeypatch.setattr(release_campaign_runner, "invoke_python_tests", lambda _layout, _options: calls.append("python"))
    monkeypatch.setattr(
        release_campaign_runner,
        "invoke_live_e2e_suite",
        lambda _layout, _workspace_options, live_options: calls.append(f"live:{live_options.profile}"),
    )

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(campaign="test-campaign", execute=True),
    )

    assert calls == ["cleanup", "validate", "python", "live:controller-surface"]


def test_campaign_execute_forwards_focused_python_test_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    campaign = campaign_payload()
    campaign["phases"][0]["scenarios"] = [
        {
            "id": "rust-python",
            "command": (
                "python -m emule_workspace test python "
                "--path tests/python/test_emulebb_rust_local_client.py -q -k local_search"
            ),
            "blocking": True,
        }
    ]
    write_campaign(layout, campaign)
    calls: list[tuple[bool, tuple[str, ...], str | None]] = []

    monkeypatch.setattr(
        release_campaign_runner,
        "run_pre_test_cleanup",
        lambda _layout: release_campaign_runner.CleanupRunSummary("routine", True, "passed", 0, 0, 0, {}),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "invoke_python_tests",
        lambda _layout, options: calls.append((options.quiet, options.paths, options.expression)),
    )

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(campaign="test-campaign", phase="preflight", execute=True),
    )

    assert calls == [(True, ("tests/python/test_emulebb_rust_local_client.py",), "local_search")]


def test_campaign_execute_dispatches_workspace_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)
    campaign = campaign_payload()
    campaign["phases"][0]["scenarios"] = [
        {
            "id": "sync",
            "command": "python -m emule_workspace sync --workspace-name workspace",
            "blocking": True,
        }
    ]
    write_campaign(layout, campaign)
    calls: list[tuple[str, str | None, str | None]] = []

    monkeypatch.setattr(
        release_campaign_runner,
        "run_pre_test_cleanup",
        lambda _layout: release_campaign_runner.CleanupRunSummary("routine", True, "passed", 0, 0, 0, {}),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "sync_workspace",
        lambda **kwargs: calls.append(
            (
                kwargs["workspace_root"],
                kwargs["workspace_name"],
                kwargs["artifacts_seed_root"],
            )
        ),
    )

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(campaign="test-campaign", phase="preflight", execute=True),
    )

    assert calls == [(str(layout.emule_workspace_root), "workspace", None)]


def test_campaign_execute_dispatches_multi_client_matrix_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    campaign = campaign_payload()
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "rust-emulebb",
            "command": (
                "python scripts/multi-client-p2p-matrix.py --lan-bind-addr ${X_LOCAL_IP} "
                "--app-exe ${EMULEBB_WORKSPACE_OUTPUT_ROOT}/builds/app/main/x64/Release/standard/bin/emulebb.exe "
                "--client2-app-exe "
                "${EMULEBB_WORKSPACE_OUTPUT_ROOT}/builds/app/tracing-harness/x64/Release/standard/bin/emule.exe "
                "--require-scenario cl-emulebb-001-cl-emulebb-rust-005-bidirectional-exchange"
            ),
            "blocking": True,
        }
    ]
    write_campaign(layout, campaign)
    calls: list[tuple[list[str], str, Path, dict[str, str]]] = []

    monkeypatch.setenv("X_LOCAL_IP", "192.0.2.44")
    monkeypatch.setattr(
        release_campaign_runner,
        "run_pre_test_cleanup",
        lambda _layout: release_campaign_runner.CleanupRunSummary("routine", True, "passed", 0, 0, 0, {}),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "get_python_invocation",
        lambda: SimpleNamespace(command=lambda args: ["PY", *[str(arg) for arg in args]]),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "run_native",
        lambda command, *, label, cwd, env: calls.append((command, label, cwd, env)),
    )

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(campaign="test-campaign", phase="controller-surface", execute=True),
    )

    assert calls == [
        (
            [
                "PY",
                str(layout.tests_repo_root / "scripts/multi-client-p2p-matrix.py"),
                "--lan-bind-addr",
                "192.0.2.44",
                "--app-exe",
                str(
                    layout.output_root
                    / "builds"
                    / "app"
                    / "main"
                    / "x64"
                    / "Release"
                    / "standard"
                    / "bin"
                    / "emulebb.exe"
                ),
                "--client2-app-exe",
                str(
                    layout.output_root
                    / "builds"
                    / "app"
                    / "tracing-harness"
                    / "x64"
                    / "Release"
                    / "standard"
                    / "bin"
                    / "emule.exe"
                ),
                "--require-scenario",
                "cl-emulebb-001-cl-emulebb-rust-005-bidirectional-exchange",
            ],
            "release multi-client P2P matrix",
            layout.tests_repo_root,
            {
                "EMULEBB_WORKSPACE_ROOT": str(layout.emule_workspace_root),
                "EMULEBB_WORKSPACE_OUTPUT_ROOT": str(layout.output_root),
                "CARGO_TARGET_DIR": str(layout.output_rust_target_root),
                "X_LOCAL_IP": "192.0.2.44",
            },
        )
    ]


def test_campaign_execute_dispatches_approved_rust_overnight_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    campaign = campaign_payload()
    campaign["phases"][0]["scenarios"] = [
        {
            "id": "stock-oracle",
            "command": "python scripts/stock-protocol-oracle-proof.py --execute-rust-proofs",
            "blocking": True,
        }
    ]
    write_campaign(layout, campaign)
    calls: list[tuple[list[str], str, Path, dict[str, str]]] = []

    monkeypatch.delenv("X_LOCAL_IP", raising=False)
    monkeypatch.setattr(
        release_campaign_runner,
        "run_pre_test_cleanup",
        lambda _layout: release_campaign_runner.CleanupRunSummary("routine", True, "passed", 0, 0, 0, {}),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "get_python_invocation",
        lambda: SimpleNamespace(command=lambda args: ["PY", *[str(arg) for arg in args]]),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "run_native",
        lambda command, *, label, cwd, env: calls.append((command, label, cwd, env)),
    )

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(campaign="test-campaign", phase="preflight", execute=True),
    )

    assert calls == [
        (
            [
                "PY",
                str(layout.tests_repo_root / "scripts/stock-protocol-oracle-proof.py"),
                "--execute-rust-proofs",
            ],
            "release campaign helper stock-protocol-oracle-proof",
            layout.tests_repo_root,
            {
                "EMULEBB_WORKSPACE_ROOT": str(layout.emule_workspace_root),
                "EMULEBB_WORKSPACE_OUTPUT_ROOT": str(layout.output_root),
                "CARGO_TARGET_DIR": str(layout.output_rust_target_root),
            },
        )
    ]


def test_campaign_execute_forwards_live_e2e_suite_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)
    campaign = campaign_payload()
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "live-monitor",
            "command": (
                "python -m emule_workspace test live-e2e --suite live-process-monitor "
                "--multi-client-require-optional-clients --godzilla-stage launch-scale --fail-fast "
                "--materialize-test-install --materialize-test-install-release-version 0.7.4-rc.2 "
                "--materialize-test-install-clean --materialize-test-install-skip-build"
            ),
            "blocking": True,
        }
    ]
    write_campaign(layout, campaign)
    calls: list[tuple[str, tuple[str, ...], str, bool, str | None, bool, bool, str, bool, bool]] = []

    monkeypatch.setattr(
        release_campaign_runner,
        "run_pre_test_cleanup",
        lambda _layout: release_campaign_runner.CleanupRunSummary("routine", True, "passed", 0, 0, 0, {}),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "invoke_live_e2e_suite",
        lambda _layout, _workspace_options, live_options: calls.append(
            (
                live_options.profile,
                live_options.suites,
                live_options.test_network,
                live_options.multi_client_require_optional_clients,
                live_options.godzilla_stage,
                live_options.fail_fast,
                live_options.materialize_test_install,
                live_options.materialize_test_install_release_version,
                live_options.materialize_test_install_clean,
                live_options.materialize_test_install_skip_build,
            )
        ),
    )

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(campaign="test-campaign", phase="controller-surface", execute=True),
    )

    assert calls == [
        ("default", ("live-process-monitor",), "default", True, "launch-scale", True, True, "0.7.4-rc.2", True, True)
    ]


def test_campaign_execute_forwards_package_release_versions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)
    campaign = campaign_payload()
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "package-x64",
            "command": "python -m emule_workspace package-release --platform x64 --release-version 0.7.3-rc.2 --clean",
            "blocking": True,
        },
        {
            "id": "package-amutorrent",
            "command": "python -m emule_workspace package-amutorrent --platform x64 --release-version 0.7.3-rc.2 --clean",
            "blocking": True,
        },
    ]
    write_campaign(layout, campaign)
    calls: list[tuple[str, str, str, bool]] = []

    monkeypatch.setattr(
        release_campaign_runner,
        "run_pre_test_cleanup",
        lambda _layout: release_campaign_runner.CleanupRunSummary("routine", True, "passed", 0, 0, 0, {}),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "create_release_package",
        lambda _layout, workspace_options, options: calls.append(
            ("package-release", workspace_options.platform, options.release_version, options.clean)
        ),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "create_amutorrent_package",
        lambda _layout, workspace_options, options: calls.append(
            ("package-amutorrent", workspace_options.platform, options.release_version, options.clean)
        ),
    )

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(campaign="test-campaign", phase="controller-surface", execute=True),
    )

    assert calls == [
        ("package-release", "x64", "0.7.3-rc.2", True),
        ("package-amutorrent", "x64", "0.7.3-rc.2", True),
    ]


def test_campaign_execute_applies_campaign_runtime_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)
    campaign = campaign_payload()
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "live",
            "command": "python -m emule_workspace test live-e2e --profile controller-surface",
            "blocking": True,
        },
        {
            "id": "cert",
            "command": "python -m emule_workspace test certification --profile overnight",
            "blocking": True,
        },
    ]
    write_campaign(layout, campaign)
    live_calls: list[tuple[str | None, str | None, str | None, float | None, str, str | None, bool, str]] = []
    certification_calls: list[tuple[str | None, str | None, str | None, float | None, str, str | None, bool, str]] = []

    monkeypatch.setattr(
        release_campaign_runner,
        "run_pre_test_cleanup",
        lambda _layout: release_campaign_runner.CleanupRunSummary("routine", True, "passed", 0, 0, 0, {}),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "invoke_live_e2e_suite",
        lambda _layout, _workspace_options, options: live_calls.append(
            (
                options.live_wire_inputs_file,
                options.radarr_movie_root,
                options.sonarr_series_root,
                options.acquisition_timeout_minutes,
                options.p2p_bind_interface_name,
                options.vpn_guard_live_config,
                options.skip_live_seed_refresh,
                options.test_network,
            )
        ),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "invoke_certification",
        lambda _layout, _workspace_options, options: certification_calls.append(
            (
                options.live_wire_inputs_file,
                options.radarr_movie_root,
                options.sonarr_series_root,
                options.acquisition_timeout_minutes,
                options.p2p_bind_interface_name,
                options.vpn_guard_live_config,
                options.skip_live_seed_refresh,
                options.test_network,
            )
        ),
    )

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(
            campaign="test-campaign",
            phase="controller-surface",
            execute=True,
            live_wire_inputs_file="inputs.local.json",
            radarr_movie_root="R:/movies",
            sonarr_series_root="S:/series",
            acquisition_timeout_minutes=12.5,
            p2p_bind_interface_name="hide.me",
            vpn_guard_live_config="vpn-guard-live.local.json",
            skip_live_seed_refresh=True,
            test_network="all",
        ),
    )

    assert live_calls == [("inputs.local.json", "R:/movies", "S:/series", 12.5, "hide.me", "vpn-guard-live.local.json", True, "all")]
    assert certification_calls == [
        ("inputs.local.json", "R:/movies", "S:/series", 12.5, "hide.me", "vpn-guard-live.local.json", True, "all")
    ]


def test_campaign_execute_dispatches_amutorrent_live_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)
    campaign = campaign_payload()
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "clean",
            "command": (
                "python -m emule_workspace test amutorrent-clean-startup "
                "--live-wire-inputs-file inputs.json --rest-webserver-scheme https --keep-artifacts"
            ),
            "blocking": True,
        },
        {
            "id": "ui",
            "command": (
                "python -m emule_workspace test amutorrent-emulebb-ui "
                "--live-wire-inputs-file inputs.json --rest-webserver-scheme https --keep-artifacts"
            ),
            "blocking": True,
        },
        {
            "id": "resilience",
            "command": (
                "python -m emule_workspace test amutorrent-resilience "
                "--live-wire-inputs-file inputs.json --rest-webserver-scheme https --keep-artifacts"
            ),
            "blocking": True,
        },
    ]
    write_campaign(layout, campaign)
    calls: list[tuple[str, str | None, str, bool, str]] = []

    monkeypatch.setattr(
        release_campaign_runner,
        "run_pre_test_cleanup",
        lambda _layout: release_campaign_runner.CleanupRunSummary("routine", True, "passed", 0, 0, 0, {}),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "invoke_amutorrent_clean_startup",
        lambda _layout, _workspace_options, options: calls.append(
            ("clean", options.live_wire_inputs_file, options.rest_webserver_scheme, options.keep_artifacts, options.test_network)
        ),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "invoke_amutorrent_emulebb_ui",
        lambda _layout, _workspace_options, options: calls.append(
            ("ui", options.live_wire_inputs_file, options.rest_webserver_scheme, options.keep_artifacts, options.test_network)
        ),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "invoke_amutorrent_resilience",
        lambda _layout, _workspace_options, options: calls.append(
            ("resilience", options.live_wire_inputs_file, options.rest_webserver_scheme, options.keep_artifacts, options.test_network)
        ),
    )

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(campaign="test-campaign", phase="controller-surface", execute=True),
    )

    assert calls == [
        ("clean", "inputs.json", "https", True, "vpn"),
        ("ui", "inputs.json", "https", True, "vpn"),
        ("resilience", "inputs.json", "https", True, "vpn"),
    ]


def test_campaign_execute_dispatches_windows_vm_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)
    campaign = campaign_payload()
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "windows-vm",
            "command": (
                "python -m emule_workspace test windows-vm --matrix win10,win11 "
                "--profile hideme-live-wire --release-version 0.7.3-rc.2 --skip-build --keep-running "
                "--fixture-size-bytes 4096 --local-swarm-mode execute"
            ),
            "blocking": True,
        }
    ]
    write_campaign(layout, campaign)
    calls: list[tuple[tuple[str, ...], str, str, bool, bool, int, str]] = []

    monkeypatch.setattr(
        release_campaign_runner,
        "run_pre_test_cleanup",
        lambda _layout: release_campaign_runner.CleanupRunSummary("routine", True, "passed", 0, 0, 0, {}),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "invoke_windows_vm_tests",
        lambda _layout, _workspace_options, options: calls.append(
            (
                options.matrix,
                options.profile,
                options.release_version,
                options.skip_build,
                options.keep_running,
                options.fixture_size_bytes,
                options.local_swarm_mode,
            )
        ),
    )

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(campaign="test-campaign", phase="controller-surface", execute=True),
    )

    assert calls == [(("win10", "win11"), "hideme-live-wire", "0.7.3-rc.2", True, True, 4096, "execute")]


def test_campaign_execute_dispatches_shared_campaign_scenario_local_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    write_campaign_scenario_catalog(layout)
    campaign = campaign_payload()
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "shared-local",
            "command": (
                "python -m emule_workspace test campaign-scenario "
                "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode local --swarm-tier 1 "
                "--local-swarm-mode execute"
            ),
            "blocking": True,
        }
    ]
    write_campaign(layout, campaign)
    calls: list[tuple[str, tuple[str, ...], str]] = []

    monkeypatch.setattr(
        release_campaign_runner,
        "run_pre_test_cleanup",
        lambda _layout: release_campaign_runner.CleanupRunSummary("routine", True, "passed", 0, 0, 0, {}),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "invoke_campaign_scenario",
        lambda _layout, _workspace_options, options: calls.append((options.scenario, (), options.mode)),
    )

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(campaign="test-campaign", phase="controller-surface", execute=True),
    )

    assert calls == [("emulebb.flow.ui.search.local-swarm.v1", (), "local")]


def test_campaign_execute_dispatches_shared_campaign_scenario_vm_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    write_campaign_scenario_catalog(layout)
    campaign = campaign_payload()
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "shared-vm",
            "command": (
                "python -m emule_workspace test campaign-scenario "
                "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode vm "
                "--release-version 0.7.4-rc.2 --skip-build --dry-run --fixture-size-bytes 4096 "
                "--matrix win10 --swarm-tier 2 --local-swarm-mode execute"
            ),
            "blocking": True,
        }
    ]
    write_campaign(layout, campaign)
    calls: list[tuple[str, str, str, bool, bool, int, tuple[str, ...], int, str]] = []

    monkeypatch.setattr(
        release_campaign_runner,
        "run_pre_test_cleanup",
        lambda _layout: release_campaign_runner.CleanupRunSummary("routine", True, "passed", 0, 0, 0, {}),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "invoke_campaign_scenario",
        lambda _layout, _workspace_options, options: calls.append(
            (
                options.scenario,
                options.mode,
                options.release_version,
                options.skip_build,
                options.dry_run,
                options.fixture_size_bytes,
                options.vm_matrix,
                options.swarm_tier,
                options.local_swarm_mode,
            )
        ),
    )

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(campaign="test-campaign", phase="controller-surface", execute=True),
    )

    assert calls == [("emulebb.flow.ui.search.local-swarm.v1", "vm", "0.7.4-rc.2", True, True, 4096, ("win10",), 2, "execute")]


def test_campaign_execute_can_override_shared_campaign_scenario_to_local_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    write_campaign_scenario_catalog(layout)
    campaign = campaign_payload()
    local_command = (
        "python -m emule_workspace test campaign-scenario "
        "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode local --swarm-tier 1 "
        "--local-swarm-mode execute"
    )
    vm_command = (
        "python -m emule_workspace test campaign-scenario "
        "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode vm "
        "--release-version 0.7.4-rc.2 --skip-build --swarm-tier 2 "
        "--local-swarm-mode plan --dry-run"
    )
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "shared-vm",
            "flowCategory": "local-vm-swarm",
            "command": vm_command,
            "localCommand": local_command,
            "vmCommand": vm_command,
            "executionMode": "vm",
            "blocking": True,
        }
    ]
    write_campaign(layout, campaign)
    calls: list[tuple[str, str, int]] = []

    monkeypatch.setattr(
        release_campaign_runner,
        "run_pre_test_cleanup",
        lambda _layout: release_campaign_runner.CleanupRunSummary("routine", True, "passed", 0, 0, 0, {}),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "invoke_campaign_scenario",
        lambda _layout, _workspace_options, options: calls.append((options.scenario, options.mode, options.swarm_tier)),
    )

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(
            campaign="test-campaign",
            phase="controller-surface",
            execute=True,
            local_vm_swarm_mode="local",
        ),
    )

    assert calls == [("emulebb.flow.ui.search.local-swarm.v1", "local", 1)]


def test_campaign_plan_can_force_shared_local_campaign_scenario_to_plan() -> None:
    campaign = campaign_payload()
    local_command = (
        "python -m emule_workspace test campaign-scenario "
        "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode local --swarm-tier 1 "
        "--local-swarm-mode execute"
    )
    vm_command = (
        "python -m emule_workspace test campaign-scenario "
        "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode vm "
        "--release-version 0.7.4-rc.2 --skip-build --swarm-tier 2 "
        "--local-swarm-mode plan --dry-run"
    )
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "shared-local",
            "flowCategory": "local-vm-swarm",
            "command": local_command,
            "localCommand": local_command,
            "vmCommand": vm_command,
            "executionMode": "local",
            "blocking": True,
        }
    ]

    plan = release_campaign_runner.build_release_campaign_execution_plan(
        campaign,
        ReleaseCampaignOptions(
            campaign="test-campaign",
            phase="controller-surface",
            execute=True,
            local_vm_swarm_mode="local",
            local_vm_swarm_execution_mode="plan",
        ),
    )

    assert len(plan) == 1
    assert "--mode local" in plan[0].command
    assert "--dry-run" in plan[0].command
    assert "--local-swarm-mode plan" in plan[0].command


def test_campaign_plan_can_force_shared_local_campaign_scenario_to_execute() -> None:
    campaign = campaign_payload()
    local_command = (
        "python -m emule_workspace test campaign-scenario "
        "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode local --swarm-tier 1 "
        "--local-swarm-mode plan --dry-run"
    )
    vm_command = (
        "python -m emule_workspace test campaign-scenario "
        "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode vm "
        "--release-version 0.7.4-rc.2 --skip-build --swarm-tier 2 "
        "--local-swarm-mode plan --dry-run"
    )
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "shared-local",
            "flowCategory": "local-vm-swarm",
            "command": local_command,
            "localCommand": local_command,
            "vmCommand": vm_command,
            "executionMode": "local",
            "blocking": True,
        }
    ]

    plan = release_campaign_runner.build_release_campaign_execution_plan(
        campaign,
        ReleaseCampaignOptions(
            campaign="test-campaign",
            phase="controller-surface",
            execute=True,
            local_vm_swarm_mode="local",
            local_vm_swarm_execution_mode="execute",
        ),
    )

    assert len(plan) == 1
    assert "--mode local" in plan[0].command
    assert "--dry-run" not in plan[0].command
    assert "--local-swarm-mode execute" in plan[0].command


def test_campaign_plan_applies_local_vm_swarm_overrides_to_all_reusable_rows() -> None:
    scenario_ids = (
        "emulebb.flow.controller.installer-swarm.v1",
        "emulebb.flow.amutorrent.clean-startup.swarm.v1",
        "emulebb.flow.amutorrent.emulebb-ui.swarm.v1",
        "emulebb.flow.arr.prowlarr-handoff.swarm.v1",
        "emulebb.flow.ui.search.local-swarm.v1",
    )
    campaign = campaign_payload()
    campaign["phases"][1]["scenarios"] = []
    for index, scenario_id in enumerate(scenario_ids, start=1):
        swarm_tier = ((index - 1) % 3) + 1
        local_command = (
            "python -m emule_workspace test campaign-scenario "
            f"--scenario {scenario_id} --mode local --swarm-tier {swarm_tier} "
            "--local-swarm-mode execute"
        )
        vm_command = (
            "python -m emule_workspace test campaign-scenario "
            f"--scenario {scenario_id} --mode vm --release-version 0.7.4-rc.2 "
            f"--skip-build --swarm-tier {swarm_tier} --local-swarm-mode plan --dry-run"
        )
        campaign["phases"][1]["scenarios"].append(
            {
                "id": scenario_id,
                "flowCategory": "local-vm-swarm",
                "command": vm_command,
                "localCommand": local_command,
                "vmCommand": vm_command,
                "executionMode": "vm",
                "executionModes": ["local", "vm"],
                "blocking": True,
            }
        )

    expected_local_plan = [
        (
            "python -m emule_workspace test campaign-scenario "
            f"--scenario {scenario_id} --mode local --swarm-tier {((index - 1) % 3) + 1} "
            "--local-swarm-mode plan --dry-run"
        )
        for index, scenario_id in enumerate(scenario_ids, start=1)
    ]
    expected_local_execute = [
        (
            "python -m emule_workspace test campaign-scenario "
            f"--scenario {scenario_id} --mode local --swarm-tier {((index - 1) % 3) + 1} "
            "--local-swarm-mode execute"
        )
        for index, scenario_id in enumerate(scenario_ids, start=1)
    ]
    expected_vm_plan = [
        (
            "python -m emule_workspace test campaign-scenario "
            f"--scenario {scenario_id} --mode vm --release-version 0.7.4-rc.2 "
            f"--skip-build --swarm-tier {((index - 1) % 3) + 1} --local-swarm-mode plan --dry-run"
        )
        for index, scenario_id in enumerate(scenario_ids, start=1)
    ]
    expected_vm_execute = [
        (
            "python -m emule_workspace test campaign-scenario "
            f"--scenario {scenario_id} --mode vm --release-version 0.7.4-rc.2 "
            f"--skip-build --swarm-tier {((index - 1) % 3) + 1} --local-swarm-mode execute"
        )
        for index, scenario_id in enumerate(scenario_ids, start=1)
    ]

    matrix = {
        ("local", "plan"): expected_local_plan,
        ("local", "execute"): expected_local_execute,
        ("vm", "plan"): expected_vm_plan,
        ("vm", "execute"): expected_vm_execute,
    }
    for (mode, execution_mode), expected_commands in matrix.items():
        plan = release_campaign_runner.build_release_campaign_execution_plan(
            campaign,
            ReleaseCampaignOptions(
                campaign="test-campaign",
                phase="controller-surface",
                execute=True,
                local_vm_swarm_mode=mode,  # type: ignore[arg-type]
                local_vm_swarm_execution_mode=execution_mode,  # type: ignore[arg-type]
            ),
        )

        assert [item.command for item in plan] == expected_commands
        assert [context.scenario_id for item in plan for context in item.scenario_contexts] == list(scenario_ids)
        assert {context.flow_category for item in plan for context in item.scenario_contexts} == {"local-vm-swarm"}


def test_campaign_execute_can_override_shared_campaign_scenario_to_vm_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    write_campaign_scenario_catalog(layout)
    campaign = campaign_payload()
    local_command = (
        "python -m emule_workspace test campaign-scenario "
        "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode local --swarm-tier 1 "
        "--local-swarm-mode execute"
    )
    vm_command = (
        "python -m emule_workspace test campaign-scenario "
        "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode vm "
        "--release-version 0.7.4-rc.2 --skip-build --swarm-tier 2 "
        "--local-swarm-mode plan --dry-run"
    )
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "shared-local",
            "flowCategory": "local-vm-swarm",
            "command": local_command,
            "localCommand": local_command,
            "vmCommand": vm_command,
            "executionMode": "local",
            "blocking": True,
        }
    ]
    write_campaign(layout, campaign)
    calls: list[tuple[str, str, str, bool, int, str]] = []

    monkeypatch.setattr(
        release_campaign_runner,
        "run_pre_test_cleanup",
        lambda _layout: release_campaign_runner.CleanupRunSummary("routine", True, "passed", 0, 0, 0, {}),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "invoke_campaign_scenario",
        lambda _layout, _workspace_options, options: calls.append(
            (
                options.scenario,
                options.mode,
                options.release_version,
                options.skip_build,
                options.swarm_tier,
                options.local_swarm_mode,
            )
        ),
    )

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(
            campaign="test-campaign",
            phase="controller-surface",
            execute=True,
            local_vm_swarm_mode="vm",
        ),
    )

    assert calls == [("emulebb.flow.ui.search.local-swarm.v1", "vm", "0.7.4-rc.2", True, 2, "plan")]


def test_campaign_execute_can_force_shared_vm_campaign_scenario_to_execute_swarm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    write_campaign_scenario_catalog(layout)
    campaign = campaign_payload()
    vm_command = (
        "python -m emule_workspace test campaign-scenario "
        "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode vm "
        "--release-version 0.7.4-rc.2 --skip-build --swarm-tier 2 "
        "--local-swarm-mode plan --dry-run"
    )
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "shared-vm",
            "flowCategory": "local-vm-swarm",
            "command": vm_command,
            "localCommand": (
                "python -m emule_workspace test campaign-scenario "
                "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode local --swarm-tier 1 "
                "--local-swarm-mode execute"
            ),
            "vmCommand": vm_command,
            "executionMode": "vm",
            "blocking": True,
        }
    ]
    write_campaign(layout, campaign)
    calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        release_campaign_runner,
        "run_pre_test_cleanup",
        lambda _layout: release_campaign_runner.CleanupRunSummary("routine", True, "passed", 0, 0, 0, {}),
    )
    monkeypatch.setattr(
        release_campaign_runner,
        "invoke_campaign_scenario",
        lambda _layout, _workspace_options, options: calls.append((options.local_swarm_mode, options.dry_run)),
    )

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(
            campaign="test-campaign",
            phase="controller-surface",
            execute=True,
            local_vm_swarm_mode="vm",
            local_vm_swarm_execution_mode="execute",
        ),
    )

    assert calls == [("execute", False)]


def test_campaign_plan_replaces_existing_shared_vm_swarm_execution_mode() -> None:
    campaign = campaign_payload()
    vm_command = (
        "python -m emule_workspace test campaign-scenario "
        "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode vm "
        "--release-version 0.7.4-rc.2 --skip-build --swarm-tier 2 --local-swarm-mode plan"
    )
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "shared-vm",
            "flowCategory": "local-vm-swarm",
            "command": vm_command,
            "localCommand": (
                "python -m emule_workspace test campaign-scenario "
                "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode local --swarm-tier 1 "
                "--local-swarm-mode execute"
            ),
            "vmCommand": vm_command,
            "executionMode": "vm",
            "blocking": True,
        }
    ]

    plan = release_campaign_runner.build_release_campaign_execution_plan(
        campaign,
        ReleaseCampaignOptions(
            campaign="test-campaign",
            phase="controller-surface",
            execute=True,
            local_vm_swarm_execution_mode="execute",
        ),
    )

    assert len(plan) == 1
    assert "--local-swarm-mode execute" in plan[0].command
    assert "--local-swarm-mode plan" not in plan[0].command
    assert "--dry-run" not in plan[0].command


def test_campaign_plan_forces_shared_vm_swarm_plan_to_dry_run() -> None:
    campaign = campaign_payload()
    vm_command = (
        "python -m emule_workspace test campaign-scenario "
        "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode vm "
        "--release-version 0.7.4-rc.2 --skip-build --swarm-tier 2 --local-swarm-mode execute"
    )
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "shared-vm",
            "flowCategory": "local-vm-swarm",
            "command": vm_command,
            "localCommand": (
                "python -m emule_workspace test campaign-scenario "
                "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode local --swarm-tier 1 "
                "--local-swarm-mode execute"
            ),
            "vmCommand": vm_command,
            "executionMode": "vm",
            "blocking": True,
        }
    ]

    plan = release_campaign_runner.build_release_campaign_execution_plan(
        campaign,
        ReleaseCampaignOptions(
            campaign="test-campaign",
            phase="controller-surface",
            execute=True,
            local_vm_swarm_execution_mode="plan",
        ),
    )

    assert len(plan) == 1
    assert "--dry-run" in plan[0].command
    assert "--local-swarm-mode execute" not in plan[0].command
    assert "--local-swarm-mode plan" in plan[0].command


def test_campaign_plan_rejects_missing_local_vm_swarm_local_override_command() -> None:
    campaign = campaign_payload()
    vm_command = (
        "python -m emule_workspace test campaign-scenario "
        "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode vm "
        "--release-version 0.7.4-rc.2 --skip-build --swarm-tier 2"
    )
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "shared-vm",
            "flowCategory": "local-vm-swarm",
            "command": vm_command,
            "vmCommand": vm_command,
            "executionMode": "vm",
            "blocking": True,
        }
    ]

    with pytest.raises(ValueError, match="shared-vm.*missing localCommand"):
        release_campaign_runner.build_release_campaign_execution_plan(
            campaign,
            ReleaseCampaignOptions(
                campaign="test-campaign",
                phase="controller-surface",
                execute=True,
                local_vm_swarm_mode="local",
            ),
        )


def test_campaign_plan_rejects_missing_local_vm_swarm_vm_override_command() -> None:
    campaign = campaign_payload()
    local_command = (
        "python -m emule_workspace test campaign-scenario "
        "--scenario emulebb.flow.ui.search.local-swarm.v1 --mode local --swarm-tier 1 "
        "--local-swarm-mode execute"
    )
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "shared-local",
            "flowCategory": "local-vm-swarm",
            "command": local_command,
            "localCommand": local_command,
            "executionMode": "local",
            "blocking": True,
        }
    ]

    with pytest.raises(ValueError, match="shared-local.*missing vmCommand"):
        release_campaign_runner.build_release_campaign_execution_plan(
            campaign,
            ReleaseCampaignOptions(
                campaign="test-campaign",
                phase="controller-surface",
                execute=True,
                local_vm_swarm_mode="vm",
            ),
        )


def test_campaign_execute_records_pre_run_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)
    write_campaign(layout, campaign_payload())

    monkeypatch.setattr(
        release_campaign_runner,
        "run_pre_test_cleanup",
        lambda _layout: release_campaign_runner.CleanupRunSummary(
            "routine",
            True,
            "passed",
            2,
            128,
            4,
            {"report-run": {"items": 2, "files": 4, "bytes": 128}},
        ),
    )
    monkeypatch.setattr(release_campaign_runner, "_dispatch_supported_command", lambda *_args: None)

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(campaign="test-campaign", execute=True),
    )

    reports = sorted((layout.output_reports_root / "release-campaign-runs").glob(f"*/{release_campaign_result_file_name()}"))
    payload = json.loads(reports[-1].read_text(encoding="utf-8"))
    assert payload["options"]["preRunCleanup"] is True
    assert payload["preRunCleanup"]["status"] == "passed"
    assert payload["preRunCleanup"]["categories"]["report-run"]["items"] == 2


def test_campaign_execution_rejects_shell_commands() -> None:
    campaign = campaign_payload()
    campaign["phases"][0]["scenarios"][0]["command"] = "cmd /c echo nope"  # type: ignore[index]

    with pytest.raises(ValueError, match="Unsupported release campaign command"):
        release_campaign_runner.build_release_campaign_execution_plan(
            campaign,
            ReleaseCampaignOptions(campaign="test-campaign", execute=True),
        )


@pytest.mark.parametrize(
    "command",
    [
        "python scripts/rust-overnight-pytest-proof.py",
        "python scripts/stock-protocol-oracle-proof.py --execute-rust-proofs",
        (
            "python scripts/local-ed2k-rust-protocol-combinations.py "
            "--lan-bind-addr ${X_LOCAL_IP} --app-exe app.exe "
            "--client2-app-exe peer.exe --ed2k-server-exe server.exe"
        ),
        "python scripts/rust-ed2k-private-parity-modules.py",
        "python scripts/rust-ed2k-total-parity-audit.py",
    ],
)
def test_campaign_execution_accepts_approved_rust_overnight_helpers(command: str) -> None:
    campaign = campaign_payload()
    campaign["phases"][0]["scenarios"][0]["command"] = command  # type: ignore[index]

    plan = release_campaign_runner.build_release_campaign_execution_plan(
        campaign,
        ReleaseCampaignOptions(campaign="test-campaign", phase="preflight", execute=True),
    )

    assert plan[0].command == command


def test_campaign_execution_rejects_unsupported_rust_helper_argument() -> None:
    campaign = campaign_payload()
    campaign["phases"][0]["scenarios"][0]["command"] = (  # type: ignore[index]
        "python scripts/stock-protocol-oracle-proof.py --manifest-path unexpected.json"
    )

    with pytest.raises(ValueError, match="Unsupported release campaign helper argument"):
        release_campaign_runner.build_release_campaign_execution_plan(
            campaign,
            ReleaseCampaignOptions(campaign="test-campaign", phase="preflight", execute=True),
        )


def test_campaign_execution_rejects_unsupported_emule_workspace_command() -> None:
    campaign = campaign_payload()
    campaign["phases"][0]["scenarios"][0]["command"] = "python -m emule_workspace test nope"  # type: ignore[index]

    with pytest.raises(ValueError, match="Unsupported emule_workspace release campaign command"):
        release_campaign_runner.build_release_campaign_execution_plan(
            campaign,
            ReleaseCampaignOptions(campaign="test-campaign", execute=True),
        )


def test_campaign_execution_rejects_unsupported_emule_workspace_option() -> None:
    campaign = campaign_payload()
    campaign["phases"][1]["scenarios"][0]["command"] = (
        "python -m emule_workspace test live-e2e --profile controller-surface --unknown"
    )

    with pytest.raises(ValueError, match="Unsupported release campaign option"):
        release_campaign_runner.build_release_campaign_execution_plan(
            campaign,
            ReleaseCampaignOptions(campaign="test-campaign", execute=True),
        )


def fake_live_e2e_suite_module():
    specs = {
        "rest-api": SimpleNamespace(name="rest-api", category="rest", network_scope="vpn"),
    }

    def resolve_suite_specs(names):
        return tuple(specs[name] for name in names)

    def filter_suite_specs_for_network(resolved_specs, test_network):
        if test_network in {"vpn", "all"}:
            return tuple(resolved_specs), []
        return (), [
            {
                "name": spec.name,
                "category": spec.category,
                "network_scope": spec.network_scope,
                "reason": f"excluded by test_network={test_network}",
            }
            for spec in resolved_specs
        ]

    return SimpleNamespace(
        PROFILE_SUITE_NAMES={"controller-surface": ("rest-api",)},
        resolve_suite_specs=resolve_suite_specs,
        filter_suite_specs_for_network=filter_suite_specs_for_network,
    )


def live_e2e_requirement_campaign() -> dict[str, object]:
    campaign = campaign_payload()
    campaign["phases"][1]["scenarios"] = [
        {
            "id": "rest",
            "command": "python -m emule_workspace test live-e2e --profile controller-surface",
            "blocking": True,
            "liveE2eProfile": "controller-surface",
            "liveE2eSuite": "rest-api",
        }
    ]
    return campaign


def test_campaign_dry_run_fails_required_live_suite_skipped_by_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    write_campaign(layout, live_e2e_requirement_campaign())
    monkeypatch.setattr(
        release_campaign_runner,
        "_load_live_e2e_suite_module",
        lambda _layout: fake_live_e2e_suite_module(),
    )

    with pytest.raises(release_campaign_runner.ReleaseCampaignExecutionError, match="required live E2E suite"):
        release_campaign_runner.invoke_release_campaign(
            layout,
            WorkspaceOptions(workspace_root=tmp_path),
            ReleaseCampaignOptions(campaign="test-campaign", phase="controller-surface", execute=True, dry_run=True),
        )

    reports = sorted((layout.output_reports_root / "release-campaign-runs").glob(f"*/{release_campaign_result_file_name()}"))
    payload = json.loads(reports[-1].read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert "required live E2E suite(s) skipped" in payload["commands"][0]["error"]


def test_campaign_dry_run_accepts_required_live_suite_with_vpn_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    write_campaign(layout, live_e2e_requirement_campaign())
    monkeypatch.setattr(
        release_campaign_runner,
        "_load_live_e2e_suite_module",
        lambda _layout: fake_live_e2e_suite_module(),
    )

    release_campaign_runner.invoke_release_campaign(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        ReleaseCampaignOptions(
            campaign="test-campaign",
            phase="controller-surface",
            execute=True,
            dry_run=True,
            test_network="vpn",
        ),
    )

    reports = sorted((layout.output_reports_root / "release-campaign-runs").glob(f"*/{release_campaign_result_file_name()}"))
    payload = json.loads(reports[-1].read_text(encoding="utf-8"))
    assert payload["status"] == "planned"
    assert payload["commands"][0]["status"] == "planned"
