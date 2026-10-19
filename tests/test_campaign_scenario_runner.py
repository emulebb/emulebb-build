from __future__ import annotations

from pathlib import Path

import pytest

from emule_workspace import campaign_scenario_runner
from emule_workspace.config import CampaignScenarioOptions, WorkspaceOptions
from test_release_campaign_runner import make_layout, write_campaign_scenario_catalog


def write_multi_campaign_scenario_catalog(layout) -> None:
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
    local_test_network: str = "default"

SPECS = (
    Spec(
        key="installer-controller-surface",
        scenario_id="emulebb.flow.controller.installer-swarm.v1",
        local_profile="installer-controller-surface",
        local_suites=("command-line-smoke", "package-helper-integration"),
        vm_profile="installer-controller-surface-vm",
    ),
    Spec(
        key="rust-ed2k-transfer",
        scenario_id="emulebb.flow.rust.ed2k-transfer.swarm.v1",
        local_profile="multi-client-p2p",
        local_suites=("deterministic-two-client-transfer",),
        vm_profile="rust-ed2k-transfer-vm",
    ),
    Spec(
        key="prowlarr-controller-handoff",
        scenario_id="emulebb.flow.arr.prowlarr-handoff.swarm.v1",
        local_profile="controller-surface",
        local_suites=("package-helper-integration",),
        vm_profile="prowlarr-controller-handoff-vm",
    ),
    Spec(
        key="search-ui-local-swarm",
        scenario_id="emulebb.flow.ui.search.local-swarm.v1",
        local_profile="multi-client-p2p",
        local_suites=("local-ed2k-search-soak", "local-kad-swarm"),
        vm_profile="search-ui-local-swarm-vm",
    ),
)
REUSABLE_CAMPAIGN_SCENARIO_BY_KEY = {spec.key: spec for spec in SPECS}
REUSABLE_CAMPAIGN_SCENARIO_BY_SCENARIO_ID = {spec.scenario_id: spec for spec in SPECS}
REUSABLE_CAMPAIGN_SCENARIO_BY_VM_PROFILE = {spec.vm_profile: spec for spec in SPECS}
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
}
""",
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


def test_campaign_scenario_local_mode_reuses_local_live_suites_and_tiered_swarm(
    tmp_path: Path,
    monkeypatch,
) -> None:
    layout = make_layout(tmp_path)
    write_campaign_scenario_catalog(layout)
    calls: list[tuple[str, str, str, tuple[str, ...], str, bool, bool, str, bool, int, int, str, bool, bool]] = []

    monkeypatch.setattr(
        campaign_scenario_runner,
        "invoke_live_e2e_suite",
        lambda _layout, _workspace_options, options: calls.append(
            (
                options.profile,
                options.campaign_scenario_key,
                options.campaign_scenario_id,
                options.campaign_scenario_local_suites,
                options.suites,
                options.test_network,
                options.plan_only,
                options.materialize_test_install,
                options.materialize_test_install_release_version,
                options.materialize_test_install_skip_build,
                options.godzilla_total_client_count,
                options.godzilla_amule_files,
                options.godzilla_stage or "",
                options.admin_volume_fixtures,
                options.godzilla_cpu_profile,
            )
        ),
    )

    campaign_scenario_runner.invoke_campaign_scenario(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        CampaignScenarioOptions(scenario="emulebb.flow.ui.search.local-swarm.v1", mode="local", swarm_tier=2),
    )

    assert calls == [
        (
            "multi-client-p2p",
            "search-ui-local-swarm",
            "emulebb.flow.ui.search.local-swarm.v1",
            ("local-ed2k-search-soak", "local-kad-swarm", "godzilla-local-swarm"),
            ("local-ed2k-search-soak", "local-kad-swarm", "godzilla-local-swarm"),
            "default",
            False,
            True,
            "0.7.3-rc.2",
            True,
            12,
            66,
            "launch-scale",
            True,
            True,
        )
    ]


def test_campaign_scenario_local_mode_dispatches_all_reusable_catalog_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    layout = make_layout(tmp_path)
    write_multi_campaign_scenario_catalog(layout)
    calls: list[tuple[str, str, tuple[str, ...], str, bool, bool, str, bool]] = []

    monkeypatch.setattr(
        campaign_scenario_runner,
        "invoke_live_e2e_suite",
        lambda _layout, _workspace_options, options: calls.append(
            (
                options.profile,
                options.test_network,
                options.suites,
                options.godzilla_stage or "",
                options.admin_volume_fixtures,
                options.materialize_test_install,
                options.materialize_test_install_release_version,
                options.materialize_test_install_skip_build,
            )
        ),
    )

    for scenario_id in (
        "emulebb.flow.controller.installer-swarm.v1",
        "emulebb.flow.rust.ed2k-transfer.swarm.v1",
        "emulebb.flow.arr.prowlarr-handoff.swarm.v1",
        "emulebb.flow.ui.search.local-swarm.v1",
    ):
        campaign_scenario_runner.invoke_campaign_scenario(
            layout,
            WorkspaceOptions(workspace_root=tmp_path),
            CampaignScenarioOptions(scenario=scenario_id, mode="local", swarm_tier=1),
        )

    assert calls == [
        (
            "installer-controller-surface",
            "default",
            ("command-line-smoke", "package-helper-integration", "godzilla-local-swarm"),
            "launch-scale",
            True,
            True,
            "0.7.3-rc.2",
            True,
        ),
        (
            "multi-client-p2p",
            "default",
            ("deterministic-two-client-transfer", "godzilla-local-swarm"),
            "launch-scale",
            True,
            True,
            "0.7.3-rc.2",
            True,
        ),
        (
            "controller-surface",
            "default",
            ("package-helper-integration", "godzilla-local-swarm"),
            "launch-scale",
            True,
            True,
            "0.7.3-rc.2",
            True,
        ),
        (
            "multi-client-p2p",
            "default",
            ("local-ed2k-search-soak", "local-kad-swarm", "godzilla-local-swarm"),
            "launch-scale",
            True,
            True,
            "0.7.3-rc.2",
            True,
        ),
    ]


def test_campaign_scenario_local_dry_run_plans_local_live_suites(
    tmp_path: Path,
    monkeypatch,
) -> None:
    layout = make_layout(tmp_path)
    write_campaign_scenario_catalog(layout)
    calls: list[bool] = []

    monkeypatch.setattr(
        campaign_scenario_runner,
        "invoke_live_e2e_suite",
        lambda _layout, _workspace_options, options: calls.append(options.plan_only),
    )

    campaign_scenario_runner.invoke_campaign_scenario(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        CampaignScenarioOptions(
            scenario="emulebb.flow.ui.search.local-swarm.v1",
            mode="local",
            dry_run=True,
        ),
    )

    assert calls == [True]


def test_godzilla_tier_options_come_from_campaign_catalog(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    write_campaign_scenario_catalog(layout)
    catalog = campaign_scenario_runner.load_campaign_scenario_catalog(layout)

    assert campaign_scenario_runner.godzilla_tier_options(catalog, 1)["total_client_count"] == 4
    assert campaign_scenario_runner.godzilla_tier_options(catalog, 2)["total_client_count"] == 12
    assert campaign_scenario_runner.godzilla_tier_options(catalog, 2)["amule_files"] == 66
    assert campaign_scenario_runner.godzilla_tier_options(catalog, 3)["total_client_count"] == 18


def test_campaign_scenario_vm_mode_reuses_vm_profile(tmp_path: Path, monkeypatch) -> None:
    layout = make_layout(tmp_path)
    write_campaign_scenario_catalog(layout)
    calls: list[tuple[tuple[str, ...], str, str, bool, bool, int, int, str]] = []

    monkeypatch.setattr(
        campaign_scenario_runner,
        "invoke_windows_vm_tests",
        lambda _layout, _workspace_options, options: calls.append(
            (
                options.matrix,
                options.profile,
                options.release_version,
                options.skip_build,
                options.dry_run,
                options.fixture_size_bytes,
                options.swarm_tier,
                options.local_swarm_mode,
            )
        ),
    )

    campaign_scenario_runner.invoke_campaign_scenario(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        CampaignScenarioOptions(
            scenario="search-ui-local-swarm",
            mode="vm",
            release_version="0.7.4-rc.2",
            skip_build=True,
            dry_run=True,
            fixture_size_bytes=4096,
            swarm_tier=3,
            local_swarm_mode="execute",
        ),
    )

    assert calls == [(("win10", "win11"), "search-ui-local-swarm-vm", "0.7.4-rc.2", True, True, 4096, 3, "execute")]


def test_campaign_scenario_vm_mode_dispatches_all_reusable_catalog_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    layout = make_layout(tmp_path)
    write_multi_campaign_scenario_catalog(layout)
    calls: list[tuple[tuple[str, ...], str, str, int, str]] = []

    monkeypatch.setattr(
        campaign_scenario_runner,
        "invoke_windows_vm_tests",
        lambda _layout, _workspace_options, options: calls.append(
            (
                options.matrix,
                options.profile,
                options.release_version,
                options.swarm_tier,
                options.local_swarm_mode,
            )
        ),
    )

    for scenario_id in (
        "emulebb.flow.controller.installer-swarm.v1",
        "emulebb.flow.rust.ed2k-transfer.swarm.v1",
        "emulebb.flow.arr.prowlarr-handoff.swarm.v1",
        "emulebb.flow.ui.search.local-swarm.v1",
    ):
        campaign_scenario_runner.invoke_campaign_scenario(
            layout,
            WorkspaceOptions(workspace_root=tmp_path),
            CampaignScenarioOptions(
                scenario=scenario_id,
                mode="vm",
                release_version="0.7.4-rc.2",
                swarm_tier=2,
                local_swarm_mode="execute",
            ),
        )

    assert calls == [
        (("win10", "win11"), "installer-controller-surface-vm", "0.7.4-rc.2", 2, "execute"),
        (("win10", "win11"), "rust-ed2k-transfer-vm", "0.7.4-rc.2", 2, "execute"),
        (("win10", "win11"), "prowlarr-controller-handoff-vm", "0.7.4-rc.2", 2, "execute"),
        (("win10", "win11"), "search-ui-local-swarm-vm", "0.7.4-rc.2", 2, "execute"),
    ]


def test_campaign_scenario_vm_mode_accepts_target_matrix(tmp_path: Path, monkeypatch) -> None:
    layout = make_layout(tmp_path)
    write_campaign_scenario_catalog(layout)
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        campaign_scenario_runner,
        "invoke_windows_vm_tests",
        lambda _layout, _workspace_options, options: calls.append(options.matrix),
    )

    campaign_scenario_runner.invoke_campaign_scenario(
        layout,
        WorkspaceOptions(workspace_root=tmp_path),
        CampaignScenarioOptions(
            scenario="search-ui-local-swarm",
            mode="vm",
            vm_matrix=("win10",),
        ),
    )

    assert calls == [("win10",)]
