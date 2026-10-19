from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from emule_workspace.config import LiveE2eOptions, ReleaseCampaignOptions, WorkspaceOptions
from emule_workspace.layout import AppVariant, TestTargets as LayoutTestTargets, WorkspaceLayout
from emule_workspace import hide_me_split_tunnel, test_runs


def make_layout(tmp_path: Path) -> WorkspaceLayout:
    """Builds a minimal layout with the live E2E runner script present."""

    emule_workspace_root = tmp_path
    workspace_root = emule_workspace_root / "workspaces" / "workspace"
    tests_repo_root = emule_workspace_root / "repos" / "emulebb-build-tests"
    app_root = workspace_root / "app" / "emulebb-main"
    (tests_repo_root / "scripts").mkdir(parents=True)
    (tests_repo_root / "scripts" / "run-live-e2e-suite.py").write_text("# test runner\n", encoding="utf-8")
    (tests_repo_root / "scripts" / "show-release-campaigns.py").write_text("# campaign reporter\n", encoding="utf-8")
    app_root.mkdir(parents=True)
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
        output_root=emule_workspace_root.parent / f"{emule_workspace_root.name}-output",
    )


def option_values(command: list[str], option: str) -> list[str]:
    """Returns values that immediately follow an option in a captured command."""

    return [command[index + 1] for index, value in enumerate(command[:-1]) if value == option]


@pytest.fixture(autouse=True)
def fake_network_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_root = tmp_path.parent / f"{tmp_path.name}-output"
    cargo_target_dir = output_root / "builds" / "rust" / "target"
    cargo_target_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EMULEBB_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("EMULEBB_WORKSPACE_OUTPUT_ROOT", str(output_root))
    monkeypatch.setenv("CARGO_TARGET_DIR", str(cargo_target_dir))
    monkeypatch.delenv("X_LOCAL_IP", raising=False)
    monkeypatch.setattr(test_runs, "build_apps", lambda *_args, **_kwargs: None)

    def resolve(*, workspace_root, output_root, test_network, vpn_interface_name=None, require_vpn=False, require_lan=False):
        values = {
            "EMULEBB_TEST_NETWORK": test_network,
            "EMULEBB_TEST_NETWORK_CONTEXT_JSON": str(Path(output_root) / "reports" / "network-context" / "fake.json"),
        }
        if require_lan:
            values["EMULEBB_TEST_LAN_IP_RESOLVED"] = "192.0.2.11"
        return SimpleNamespace(env=lambda: values)

    monkeypatch.setattr(test_runs, "resolve_workspace_network_context", resolve)


def test_live_e2e_rebuilds_workspace_app_with_startup_diagnostics_when_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_build_apps(_layout, _options, **kwargs):
        captured.update(kwargs)

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "build_apps", fake_build_apps)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("startup-diagnostics",), startup_trace_mode="required"),
    )

    assert captured["clean"] is False
    assert captured["app_variant_names"] == ("main",)
    assert captured["enable_startup_diagnostics"] is True
    assert "--startup-trace-mode" in captured["command"]


def test_live_e2e_forwards_plan_only_option(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("local-ed2k-search-soak",), plan_only=True),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--plan-only" in command


def test_live_e2e_allows_arm64_on_arm64_host(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setenv("RUNNER_ARCH", "ARM64")
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="ARM64"),
        LiveE2eOptions(suites=("command-line-smoke",), test_network="offline"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--app-root") == [str(layout.get_app_variant("main").path)]
    assert option_values(command, "--configuration") == ["Release"]
    assert option_values(command, "--suite") == ["command-line-smoke"]


def test_live_e2e_rejects_arm64_on_non_arm64_host(tmp_path: Path, monkeypatch) -> None:
    layout = make_layout(tmp_path)
    monkeypatch.setenv("RUNNER_ARCH", "X64")
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)
    monkeypatch.setattr(test_runs.platform, "machine", lambda: "AMD64")

    with pytest.raises(RuntimeError, match="Live E2E ARM64 execution requires an ARM64 host"):
        test_runs.invoke_live_e2e_suite(
            layout,
            WorkspaceOptions(workspace_root=tmp_path, platform="ARM64"),
            LiveE2eOptions(suites=("command-line-smoke",), test_network="offline"),
        )


def test_live_e2e_forwards_campaign_scenario_metadata(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)
        captured["env"] = dict(env or {})

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            suites=("local-ed2k-search-soak", "godzilla-local-swarm"),
            campaign_scenario_key="search-ui-local-swarm",
            campaign_scenario_id="emulebb.flow.ui.search.local-swarm.v1",
            campaign_scenario_vm_profile="search-ui-local-swarm-vm",
            campaign_scenario_local_suites=("local-ed2k-search-soak", "godzilla-local-swarm"),
            campaign_scenario_uses_local_swarm=True,
        ),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--campaign-scenario-key") == ["search-ui-local-swarm"]
    assert option_values(command, "--campaign-scenario-id") == ["emulebb.flow.ui.search.local-swarm.v1"]
    assert option_values(command, "--campaign-scenario-vm-profile") == ["search-ui-local-swarm-vm"]
    assert option_values(command, "--campaign-scenario-local-suite") == [
        "local-ed2k-search-soak",
        "godzilla-local-swarm",
    ]
    assert "--campaign-scenario-uses-local-swarm" in command
    assert captured["env"]["X_LOCAL_IP"] == "192.0.2.11"


def test_live_e2e_forwards_cold_stress_cpu_profile_options(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)
        captured["label"] = label
        captured["cwd"] = cwd
        captured["env"] = dict(env or {})

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            suites=("rest-cold-start-dump-stress",),
            profile_cpu=True,
            profile_cpu_max_file_mb=96,
            profile_cpu_stack=True,
            profile_cpu_stack_min_hits=15,
            profile_symbols_required=False,
            profile_memory=True,
            profile_resource_interval_seconds=1.5,
            rest_cold_start_dump_stress_enable_umdh=True,
            rest_cold_start_dump_stress_cpu_profile=True,
            rest_cold_start_dump_stress_cpu_profile_max_file_mb=64,
            rest_cold_start_dump_stress_cpu_profile_stack=True,
            rest_cold_start_dump_stress_cpu_profile_stack_min_hits=25,
            rest_cold_start_dump_stress_cpu_profile_symbols_required=False,
            rest_cold_start_dump_stress_max_missing_download_triggers=1,
            rest_cold_start_dump_stress_synthetic_queue_fill_count=5,
            rest_cold_start_dump_stress_synthetic_queue_fill_size_bytes=4096,
            rest_cold_start_dump_stress_synthetic_queue_fill_batch_size=3,
            rest_cold_start_dump_stress_search_observation_timeout_seconds=12.0,
            rest_cold_start_dump_stress_allow_required_zero_result_searches=True,
            rest_cold_start_dump_stress_skip_transfer_cleanup=True,
            rest_cold_start_dump_stress_skip_umdh_diffs=True,
        ),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--profile-cpu" in command
    assert option_values(command, "--profile-cpu-max-file-mb") == ["96"]
    assert "--profile-cpu-stack" in command
    assert option_values(command, "--profile-cpu-stack-min-hits") == ["15"]
    assert "--no-profile-symbols-required" in command
    assert "--profile-memory" in command
    assert option_values(command, "--profile-resource-interval-seconds") == ["1.5"]
    assert "--rest-cold-start-dump-stress-enable-umdh" in command
    assert "--rest-cold-start-dump-stress-cpu-profile" in command
    assert option_values(command, "--rest-cold-start-dump-stress-cpu-profile-max-file-mb") == ["64"]
    assert "--rest-cold-start-dump-stress-cpu-profile-stack" in command
    assert option_values(command, "--rest-cold-start-dump-stress-cpu-profile-stack-min-hits") == ["25"]
    assert option_values(command, "--rest-cold-start-dump-stress-max-missing-download-triggers") == ["1"]
    assert option_values(command, "--rest-cold-start-dump-stress-synthetic-queue-fill-count") == ["5"]
    assert option_values(command, "--rest-cold-start-dump-stress-synthetic-queue-fill-size-bytes") == ["4096"]
    assert option_values(command, "--rest-cold-start-dump-stress-synthetic-queue-fill-batch-size") == ["3"]
    assert option_values(command, "--rest-cold-start-dump-stress-search-observation-timeout-seconds") == ["12.0"]
    assert "--rest-cold-start-dump-stress-allow-required-zero-result-searches" in command
    assert "--rest-cold-start-dump-stress-skip-transfer-cleanup" in command
    assert "--rest-cold-start-dump-stress-skip-umdh-diffs" in command
    assert "--no-rest-cold-start-dump-stress-cpu-profile-symbols-required" in command
    assert "--rest-cold-start-dump-stress-skip-dumps" not in command
    assert captured["env"]["EMULEBB_WORKSPACE_ROOT"] == str(layout.emule_workspace_root)
    assert captured["env"]["EMULEBB_TEST_NETWORK"] == "default"


def test_live_e2e_forwards_vpn_guard_options(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            suites=("rest-api",),
            vpn_guard_live_config="vpn-guard-live.local.json",
            vpn_guard_allowed_public_ip_cidrs="8.8.8.8/32",
            vpn_guard_scenario="not-allowlisted",
            vpn_guard_expected_startup_block=True,
        ),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--vpn-guard-live-config") == ["vpn-guard-live.local.json"]
    assert option_values(command, "--vpn-guard-allowed-public-ip-cidrs") == ["8.8.8.8/32"]
    assert option_values(command, "--vpn-guard-scenario") == ["not-allowlisted"]
    assert "--vpn-guard-expected-startup-block" in command


def test_live_e2e_forwards_preference_directory_tree_stress(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            suites=("preference-ui",),
            shared_root=r"C:\tmp\large-shared-root",
            preference_ui_directories_tree_stress=True,
        ),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--preference-ui-directories-tree-stress" in command
    assert option_values(command, "--shared-root") == [r"C:\tmp\large-shared-root"]


def test_live_e2e_forwards_local_kad_bootstrap_mode(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            suites=("local-kad-swarm",),
            local_kad_bootstrap_mode="preseed",
            local_kad_nodes_dat_fixture_mode="stale",
        ),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--local-kad-bootstrap-mode") == ["preseed"]
    assert option_values(command, "--local-kad-nodes-dat-fixture-mode") == ["stale"]


def test_live_e2e_forwards_admin_volume_fixture_options(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            suites=("shared-cache-volume-identity",),
            admin_volume_fixtures=True,
            vhd_size_mb=384,
            mount_root=r"C:\tmp\emulebb-admin-mounts",
            keep_admin_fixtures=True,
        ),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--admin-volume-fixtures" in command
    assert option_values(command, "--vhd-size-mb") == ["384"]
    assert option_values(command, "--mount-root") == [r"C:\tmp\emulebb-admin-mounts"]
    assert "--keep-admin-fixtures" in command


def test_live_e2e_forwards_dependency_resolution_options(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    cache_root = r"state\arr-cache"
    prowlarr_exe = r"tools\Prowlarr\Prowlarr.exe"
    radarr_exe = r"tools\Radarr\Radarr.exe"
    sonarr_exe = r"tools\Sonarr\Sonarr.exe"
    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            profile="package-helpers",
            dependency_mode="auto-download",
            dependency_channel="latest",
            dependency_cache_root=cache_root,
            refresh_dependencies=True,
            prowlarr_exe=prowlarr_exe,
            radarr_exe=radarr_exe,
            sonarr_exe=sonarr_exe,
        ),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--profile") == ["package-helpers"]
    assert option_values(command, "--dependency-mode") == ["auto-download"]
    assert option_values(command, "--dependency-channel") == ["latest"]
    assert option_values(command, "--dependency-cache-root") == [str((layout.emule_workspace_root / cache_root).resolve())]
    assert "--refresh-dependencies" in command
    assert option_values(command, "--prowlarr-exe") == [str((layout.emule_workspace_root / prowlarr_exe).resolve())]
    assert option_values(command, "--radarr-exe") == [str((layout.emule_workspace_root / radarr_exe).resolve())]
    assert option_values(command, "--sonarr-exe") == [str((layout.emule_workspace_root / sonarr_exe).resolve())]


def test_live_e2e_forwards_search_ui_live_stress_options(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            suites=("search-ui-live",),
            search_ui_search_rounds=4,
            search_ui_download_lifecycle_count=3,
        ),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--search-ui-search-rounds") == ["4"]
    assert option_values(command, "--search-ui-download-lifecycle-count") == ["3"]


def test_live_e2e_forwards_godzilla_visible_ui_and_lan_bind(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            suites=("godzilla-local-swarm",),
            p2p_bind_interface_name="Ethernet",
            godzilla_p2p_bind_interface_address="192.0.2.10",
            godzilla_visible_ui=True,
            godzilla_cpu_profile=True,
            godzilla_stage="launch-scale",
            godzilla_vhd_runtime_root="drive-letter",
            godzilla_total_client_count=12,
            godzilla_peer_transfer_count=444,
            godzilla_harness_transfer_count=222,
            godzilla_emulebb_files=700,
            godzilla_extra_emulebb_files=70,
            godzilla_harness_files=500,
            godzilla_adverse_kill_cycles=3,
            godzilla_adverse_kill_warmup_seconds=0.5,
            godzilla_adverse_recovery_timeout_seconds=45.0,
        ),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--p2p-bind-interface-name") == ["Ethernet"]
    assert option_values(command, "--godzilla-p2p-bind-interface-address") == ["192.0.2.10"]
    assert option_values(command, "--godzilla-stage") == ["launch-scale"]
    assert option_values(command, "--godzilla-vhd-runtime-root") == ["drive-letter"]
    assert option_values(command, "--godzilla-total-client-count") == ["12"]
    assert option_values(command, "--godzilla-peer-transfer-count") == ["444"]
    assert option_values(command, "--godzilla-harness-transfer-count") == ["222"]
    assert option_values(command, "--godzilla-emulebb-files") == ["700"]
    assert option_values(command, "--godzilla-extra-emulebb-files") == ["70"]
    assert option_values(command, "--godzilla-harness-files") == ["500"]
    assert "--godzilla-amule-files" not in command
    assert option_values(command, "--godzilla-adverse-kill-cycles") == ["3"]
    assert option_values(command, "--godzilla-adverse-kill-warmup-seconds") == ["0.5"]
    assert option_values(command, "--godzilla-adverse-recovery-timeout-seconds") == ["45.0"]
    assert "--godzilla-visible-ui" in command
    assert "--godzilla-cpu-profile" in command


def test_live_e2e_leaves_godzilla_stage_unset_for_profile_defaults(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(profile="release-expanded"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--godzilla-stage" not in command


def test_live_e2e_can_run_against_materialized_installer_test_install(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}
    materialize_calls: list[object] = []

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)
        captured["env"] = dict(env or {})

    def fake_materialize(layout, workspace_options, install_options, *, run_id, suite_name, client_id, lan_bind_address=None):
        materialize_calls.append(
            SimpleNamespace(
                layout=layout,
                workspace_options=workspace_options,
                install_options=install_options,
                run_id=run_id,
                suite_name=suite_name,
                client_id=client_id,
                lan_bind_address=lan_bind_address,
            )
        )
        install_root = layout.output_tmp_root / "test-installs" / "run" / "live-e2e-suite" / "main"
        manifests = install_root / "manifests"
        manifests.mkdir(parents=True)
        (manifests / "suite-config.json").write_text(
            json.dumps(
                {
                    "services": {
                        "prowlarr": {"bindAddress": "192.0.2.11", "port": 9696, "apiKey": "prowlarr-secret"},
                        "radarr": {"bindAddress": "192.0.2.11", "port": 7878, "apiKey": "radarr-secret"},
                        "sonarr": {"bindAddress": "192.0.2.11", "port": 8989, "apiKey": "sonarr-secret"},
                    }
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            target_path=install_root,
            app_root=install_root / "apps" / "eMuleBB",
            app_exe=install_root / "apps" / "eMuleBB" / "emulebb.exe",
            profile_dir=install_root / "profiles" / "emulebb",
            profile_config_dir=install_root / "profiles" / "emulebb" / "config",
            profile_seed_config_dir=install_root / "harness-profile-seed" / "config",
        )

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setattr(test_runs, "materialize_test_local_install", fake_materialize)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            suites=("godzilla-local-swarm",),
            live_wire_inputs_file="repos/emulebb-build-tests/live-wire-inputs.local.json",
            materialize_test_install=True,
            materialize_test_install_release_version="0.7.4-rc.2",
            materialize_test_install_clean=True,
            materialize_test_install_skip_build=True,
        ),
    )

    command = captured["command"]
    call = materialize_calls[0]
    assert isinstance(command, list)
    assert call.install_options.live_wire_inputs_file == "repos/emulebb-build-tests/live-wire-inputs.local.json"
    assert call.install_options.release_version == "0.7.4-rc.2"
    assert call.install_options.clean is True
    assert call.install_options.skip_build is True
    assert call.suite_name == "live-e2e-suite"
    assert call.client_id == "main"
    assert call.lan_bind_address is None
    assert call.run_id.endswith("-pid" + str(os.getpid()))
    install_root = call.layout.output_tmp_root / "test-installs" / "run" / "live-e2e-suite" / "main"
    assert option_values(command, "--app-root") == [str(install_root / "apps" / "eMuleBB")]
    assert option_values(command, "--app-exe") == [
        str(install_root / "apps" / "eMuleBB" / "emulebb.exe")
    ]
    assert option_values(command, "--profile-seed-dir") == [str(install_root / "harness-profile-seed" / "config")]
    assert option_values(command, "--live-process-monitor-profile-dir") == [str(install_root / "profiles" / "emulebb")]
    assert option_values(command, "--live-wire-inputs-file") == ["repos/emulebb-build-tests/live-wire-inputs.local.json"]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PROWLARR_URL"] == "http://192.0.2.11:9696"
    assert env["PROWLARR_API_KEY"] == "prowlarr-secret"
    assert env["RADARR_URL"] == "http://192.0.2.11:7878"
    assert env["RADARR_API_KEY"] == "radarr-secret"
    assert env["SONARR_URL"] == "http://192.0.2.11:8989"
    assert env["SONARR_API_KEY"] == "sonarr-secret"


def test_live_e2e_materialized_vpn_uses_lan_bind(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}
    materialize_calls: list[object] = []
    events: list[str] = []

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)
        captured["env"] = dict(env or {})

    def fake_materialize(layout, workspace_options, install_options, *, run_id, suite_name, client_id, lan_bind_address=None):
        events.append("materialize")
        materialize_calls.append(SimpleNamespace(lan_bind_address=lan_bind_address))
        install_root = layout.output_tmp_root / "test-installs" / "run" / "live-e2e-suite" / "main"
        app_exe = install_root / "apps" / "eMuleBB" / "emulebb.exe"
        app_exe.parent.mkdir(parents=True)
        app_exe.write_bytes(b"exe")
        return SimpleNamespace(
            target_path=install_root,
            app_root=install_root / "apps" / "eMuleBB",
            app_exe=app_exe,
            profile_dir=install_root / "profiles" / "emulebb",
            profile_config_dir=install_root / "profiles" / "emulebb" / "config",
            profile_seed_config_dir=install_root / "harness-profile-seed" / "config",
        )

    def fake_register(paths, **kwargs):
        events.append("register")
        assert kwargs == {"required": True}
        assert Path(paths[0]).name == "emulebb.exe"
        return {"enabled": True, "changed": True, "restart": {"requested": True, "vpn_ipv4": "10.8.0.9"}}

    def fake_resolve(*, workspace_root, output_root, test_network, vpn_interface_name=None, require_vpn=False, require_lan=False):
        events.append(f"resolve:{test_network}")
        values = {
            "EMULEBB_TEST_NETWORK": test_network,
            "EMULEBB_TEST_NETWORK_CONTEXT_JSON": str(Path(output_root) / "reports" / "network-context" / f"{test_network}.json"),
        }
        if require_lan:
            values["EMULEBB_TEST_LAN_IP_RESOLVED"] = "192.0.2.11"
        if test_network in {"vpn", "all"} or require_vpn:
            values["EMULEBB_TEST_VPN_IP_RESOLVED"] = "10.8.0.9"
        return SimpleNamespace(env=lambda: values)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setattr(test_runs, "materialize_test_local_install", fake_materialize)
    monkeypatch.setattr(test_runs, "ensure_split_tunnel_apps", fake_register)
    monkeypatch.setattr(test_runs, "resolve_workspace_network_context", fake_resolve)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            suites=("rest-api",),
            test_network="vpn",
            materialize_test_install=True,
            materialize_test_install_skip_build=True,
        ),
    )

    assert materialize_calls[0].lan_bind_address == "192.0.2.11"
    assert captured["env"]["X_LOCAL_IP"] == "192.0.2.11"
    assert captured["env"]["EMULEBB_TEST_VPN_IP_RESOLVED"] == "10.8.0.9"
    assert events == ["resolve:lan", "materialize", "register", "resolve:vpn"]


def test_process_x_local_ip_overrides_resolved_lan_bind_for_host_runs(monkeypatch) -> None:
    monkeypatch.setenv("X_LOCAL_IP", "192.168.1.42")

    lan_bind_addr = test_runs._lan_bind_address_from_env(
        {
            "EMULEBB_TEST_LAN_INTERFACE": "hide.me",
            "EMULEBB_TEST_LAN_IP_RESOLVED": "10.8.0.9",
        }
    )

    assert lan_bind_addr == "192.168.1.42"


def test_live_e2e_installer_controller_surface_profile_materializes_by_default(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}
    materialize_calls: list[object] = []

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)
        captured["env"] = dict(env or {})

    def fake_materialize(layout, workspace_options, install_options, *, run_id, suite_name, client_id, lan_bind_address=None):
        materialize_calls.append(SimpleNamespace(lan_bind_address=lan_bind_address))
        install_root = layout.output_tmp_root / "test-installs" / "run" / "live-e2e-suite" / "main"
        app_exe = install_root / "apps" / "eMuleBB" / "emulebb.exe"
        app_exe.parent.mkdir(parents=True)
        app_exe.write_bytes(b"exe")
        manifests = install_root / "manifests"
        manifests.mkdir(parents=True)
        (manifests / "suite-config.json").write_text(
            json.dumps(
                {
                    "services": {
                        "prowlarr": {"bindAddress": "192.0.2.11", "port": 9696, "apiKey": "prowlarr-secret"},
                        "radarr": {"bindAddress": "192.0.2.11", "port": 7878, "apiKey": "radarr-secret"},
                        "sonarr": {"bindAddress": "192.0.2.11", "port": 8989, "apiKey": "sonarr-secret"},
                    }
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            target_path=install_root,
            app_root=app_exe.parent,
            app_exe=app_exe,
            profile_dir=install_root / "profiles" / "emulebb",
            profile_config_dir=install_root / "profiles" / "emulebb" / "config",
            profile_seed_config_dir=install_root / "harness-profile-seed" / "config",
        )

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setattr(test_runs, "materialize_test_local_install", fake_materialize)
    monkeypatch.setattr(test_runs, "ensure_split_tunnel_apps", lambda paths, **_kwargs: {"enabled": True})
    monkeypatch.setattr(test_runs, "_start_materialized_arr_services", lambda *_args: [])

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(profile="installer-controller-surface"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert materialize_calls[0].lan_bind_address == "192.0.2.11"
    assert option_values(command, "--profile") == ["installer-controller-surface"]
    assert option_values(command, "--test-network") == ["vpn"]
    assert option_values(command, "--app-exe")[0].endswith("emulebb.exe")
    assert captured["env"]["X_LOCAL_IP"] == "192.0.2.11"
    assert captured["env"]["PROWLARR_URL"] == "http://192.0.2.11:9696"


def test_live_e2e_installer_controller_surface_soak_materializes_by_default(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}
    materialize_calls: list[object] = []

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    def fake_materialize(layout, workspace_options, install_options, *, run_id, suite_name, client_id, lan_bind_address=None):
        materialize_calls.append(SimpleNamespace(lan_bind_address=lan_bind_address))
        install_root = layout.output_tmp_root / "test-installs" / "run" / "live-e2e-suite" / "main"
        app_exe = install_root / "apps" / "eMuleBB" / "emulebb.exe"
        app_exe.parent.mkdir(parents=True)
        app_exe.write_bytes(b"exe")
        return SimpleNamespace(
            target_path=install_root,
            app_root=app_exe.parent,
            app_exe=app_exe,
            profile_dir=install_root / "profiles" / "emulebb",
            profile_config_dir=install_root / "profiles" / "emulebb" / "config",
            profile_seed_config_dir=install_root / "harness-profile-seed" / "config",
        )

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setattr(test_runs, "materialize_test_local_install", fake_materialize)
    monkeypatch.setattr(test_runs, "ensure_split_tunnel_apps", lambda paths, **_kwargs: {"enabled": True})
    monkeypatch.setattr(test_runs, "_start_materialized_arr_services", lambda *_args: [])

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(profile="installer-controller-surface-soak"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert materialize_calls[0].lan_bind_address == "192.0.2.11"
    assert option_values(command, "--profile") == ["installer-controller-surface-soak"]
    assert option_values(command, "--test-network") == ["vpn"]
    assert option_values(command, "--live-process-monitor-profile-dir")[0].endswith("profiles\\emulebb")


def test_live_e2e_plan_only_skips_installer_profile_materialization(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    def fail_materialize(*_args, **_kwargs):
        raise AssertionError("plan-only live E2E runs should only resolve child commands")

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setattr(test_runs, "materialize_test_local_install", fail_materialize)
    monkeypatch.setattr(test_runs, "ensure_split_tunnel_apps", lambda paths, **_kwargs: {"enabled": False})

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            profile="installer-controller-surface",
            suites=("command-line-smoke", "package-helper-integration"),
            plan_only=True,
        ),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--plan-only" in command
    assert option_values(command, "--profile") == ["installer-controller-surface"]
    assert option_values(command, "--test-network") == ["default"]
    assert option_values(command, "--suite") == ["command-line-smoke", "package-helper-integration"]
    assert "--profile-seed-dir" not in command


def test_live_e2e_starts_materialized_arr_services_for_arr_suites(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}
    started_commands: list[tuple[list[str], str]] = []
    stopped: list[str] = []
    ready_urls: set[str] = set()
    exe_url_map = {
        "Prowlarr.exe": "http://192.0.2.11:9696",
        "Radarr.exe": "http://192.0.2.11:7878",
        "Sonarr.exe": "http://192.0.2.11:8989",
    }

    class FakeProcess:
        def __init__(self, command, *, cwd=None, stdout=None, stderr=None, text=None, creationflags=0):
            self.command = [str(part) for part in command]
            self.returncode = None
            started_commands.append((self.command, str(cwd)))
            ready_urls.add(exe_url_map[Path(self.command[0]).name])

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0
            stopped.append(Path(self.command[0]).name)

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = -9

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)
        captured["env"] = dict(env or {})

    def fake_materialize(layout, workspace_options, install_options, *, run_id, suite_name, client_id, lan_bind_address=None):
        install_root = layout.output_tmp_root / "test-installs" / "run" / "live-e2e-suite" / "main"
        manifests = install_root / "manifests"
        manifests.mkdir(parents=True)
        (manifests / "suite-config.json").write_text(
            json.dumps(
                {
                    "services": {
                        "prowlarr": {"bindAddress": "192.0.2.11", "port": 9696, "apiKey": "prowlarr-secret"},
                        "radarr": {"bindAddress": "192.0.2.11", "port": 7878, "apiKey": "radarr-secret"},
                        "sonarr": {"bindAddress": "192.0.2.11", "port": 8989, "apiKey": "sonarr-secret"},
                    }
                }
            ),
            encoding="utf-8",
        )
        for app_name, exe_name, data_name in (
            ("prowlarr", "Prowlarr.exe", "prowlarr"),
            ("radarr", "Radarr.exe", "radarr"),
            ("sonarr", "Sonarr.exe", "sonarr"),
        ):
            app_dir = install_root / "apps" / app_name
            app_dir.mkdir(parents=True)
            (app_dir / exe_name).write_text("stub\n", encoding="utf-8")
            (install_root / "data" / data_name).mkdir(parents=True)
        return SimpleNamespace(
            target_path=install_root,
            app_root=install_root / "apps" / "eMuleBB",
            app_exe=install_root / "apps" / "eMuleBB" / "emulebb.exe",
            profile_dir=install_root / "profiles" / "emulebb",
            profile_config_dir=install_root / "profiles" / "emulebb" / "config",
            profile_seed_config_dir=install_root / "harness-profile-seed" / "config",
        )

    def fake_ready(url, api_key, status_api_path):
        return url in ready_urls

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setattr(test_runs, "materialize_test_local_install", fake_materialize)
    monkeypatch.setattr(test_runs.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(test_runs, "_materialized_service_ready", fake_ready)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("prowlarr-emulebb",), materialize_test_install=True),
    )

    command_names = [Path(command[0]).name for command, _cwd in started_commands]
    assert command_names == ["Prowlarr.exe", "Radarr.exe", "Sonarr.exe"]
    assert all(any(part.startswith("/data=") for part in command) for command, _cwd in started_commands)
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PROWLARR_URL"] == "http://192.0.2.11:9696"
    assert stopped == ["Sonarr.exe", "Radarr.exe", "Prowlarr.exe"]


def test_live_e2e_does_not_start_materialized_arr_services_for_non_arr_suites(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}
    start_calls: list[object] = []

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    def fake_materialize(layout, workspace_options, install_options, *, run_id, suite_name, client_id, lan_bind_address=None):
        install_root = layout.output_tmp_root / "test-installs" / "run" / "live-e2e-suite" / "main"
        return SimpleNamespace(
            target_path=install_root,
            app_root=install_root / "apps" / "eMuleBB",
            app_exe=install_root / "apps" / "eMuleBB" / "emulebb.exe",
            profile_dir=install_root / "profiles" / "emulebb",
            profile_config_dir=install_root / "profiles" / "emulebb" / "config",
            profile_seed_config_dir=install_root / "harness-profile-seed" / "config",
        )

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setattr(test_runs, "materialize_test_local_install", fake_materialize)
    monkeypatch.setattr(test_runs, "_start_materialized_arr_services", lambda *args: start_calls.append(args) or [])

    def fake_materialized_arr_service_env(_materialized, *, require_explicit_lan=True):
        assert require_explicit_lan is False
        return {}

    monkeypatch.setattr(test_runs, "_materialized_arr_service_env", fake_materialized_arr_service_env)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("command-line-smoke",), materialize_test_install=True),
    )

    assert captured["command"]
    assert start_calls == []


def test_materialized_arr_service_ready_path_does_not_spawn_process(tmp_path: Path, monkeypatch) -> None:
    install_root = tmp_path / "suite"
    (install_root / "apps" / "prowlarr").mkdir(parents=True)
    (install_root / "data" / "prowlarr").mkdir(parents=True)
    materialized = SimpleNamespace(target_path=install_root, app_root=install_root / "apps" / "eMuleBB")
    service_env = {
        "PROWLARR_URL": "http://192.0.2.11:9696",
        "PROWLARR_API_KEY": "prowlarr-secret",
    }

    def fail_popen(*_args, **_kwargs):
        raise AssertionError("ready materialized services should not be spawned")

    monkeypatch.setattr(test_runs, "_materialized_service_ready", lambda *_args: True)
    monkeypatch.setattr(test_runs.subprocess, "Popen", fail_popen)

    assert test_runs._start_materialized_arr_services(materialized, service_env) == []


def test_materialized_arr_service_env_can_ignore_loopback_when_optional(tmp_path: Path) -> None:
    install_root = tmp_path / "suite"
    manifest_dir = install_root / "manifests"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "suite-config.json").write_text(
        json.dumps(
            {
                "services": {
                    "prowlarr": {"bindAddress": "127.0.0.1", "port": 9696, "apiKey": "prowlarr-secret"},
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    materialized = SimpleNamespace(target_path=install_root, app_root=install_root / "apps" / "eMuleBB")

    assert test_runs._materialized_arr_service_env(materialized, require_explicit_lan=False) == {}
    with pytest.raises(RuntimeError, match="explicit LAN address"):
        test_runs._materialized_arr_service_env(materialized, require_explicit_lan=True)


def test_materialized_arr_service_timeout_reports_log_tail(tmp_path: Path, monkeypatch) -> None:
    install_root = tmp_path / "suite"
    app_dir = install_root / "apps" / "prowlarr"
    data_dir = install_root / "data" / "prowlarr"
    app_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    (app_dir / "Prowlarr.exe").write_text("stub\n", encoding="utf-8")
    materialized = SimpleNamespace(target_path=install_root, app_root=install_root / "apps" / "eMuleBB")
    service_env = {
        "PROWLARR_URL": "http://192.0.2.11:9696",
        "PROWLARR_API_KEY": "prowlarr-secret",
    }

    class FakeProcess:
        def __init__(self, command, *, cwd=None, stdout=None, stderr=None, text=None, creationflags=0):
            self.returncode = None
            if stdout is not None:
                stdout.write("arr boot failed before binding\n")
                stdout.flush()

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(test_runs.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(test_runs, "_materialized_service_ready", lambda *_args: False)
    monkeypatch.setattr(test_runs, "MATERIALIZED_ARR_SERVICE_WAIT_SECONDS", 0.001)
    monkeypatch.setattr(test_runs.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="arr boot failed before binding"):
        test_runs._start_materialized_arr_services(materialized, service_env)


def test_live_e2e_forwards_explicit_live_process_monitor_profile_dir(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setattr(test_runs, "ensure_split_tunnel_apps", lambda paths, **_kwargs: {"enabled": False})

    profile_dir = tmp_path / "operator-profile"
    monkeypatch.setenv("EMULEBB_LOCAL_TEST_PROFILE_PATH", str(profile_dir))
    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            suites=("live-process-monitor",),
            test_network="vpn",
            live_process_monitor_profile_dir="%EMULEBB_LOCAL_TEST_PROFILE_PATH%",
        ),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--live-process-monitor-profile-dir") == [str(profile_dir)]


def test_live_e2e_registers_materialized_exe_for_developer_hide_me_split_tunnel(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}
    registered: list[Path] = []

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    def fake_materialize(layout_arg, workspace_options, install_options, *, run_id, suite_name, client_id, lan_bind_address=None):
        install_root = layout.output_tmp_root / "test-installs" / run_id / suite_name / client_id
        app_exe = install_root / "apps" / "eMuleBB" / "emulebb.exe"
        seed_config = install_root / "harness-profile-seed" / "config"
        profile_dir = install_root / "profiles" / "emulebb"
        app_exe.parent.mkdir(parents=True)
        seed_config.mkdir(parents=True)
        profile_dir.mkdir(parents=True)
        app_exe.write_bytes(b"exe")
        return SimpleNamespace(
            app_root=app_exe.parent,
            app_exe=app_exe,
            profile_dir=profile_dir,
            profile_config_dir=profile_dir / "config",
            profile_seed_config_dir=seed_config,
        )

    def fake_register(paths, **kwargs):
        registered.extend(paths)
        captured["register_kwargs"] = dict(kwargs)
        return {"enabled": True}

    layout = make_layout(tmp_path)
    node_exe = tmp_path / "runtime" / "node" / "node.exe"
    node_exe.parent.mkdir(parents=True)
    node_exe.write_bytes(b"node")
    browser_exe = tmp_path / "local-appdata" / "ms-playwright" / "chromium-1234" / "chrome-win64" / "chrome.exe"
    browser_exe.parent.mkdir(parents=True)
    browser_exe.write_bytes(b"chrome")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-appdata"))
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setattr(test_runs, "materialize_test_local_install", fake_materialize)
    monkeypatch.setattr(test_runs, "ensure_split_tunnel_apps", fake_register)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            suites=("prowlarr-emulebb",),
            test_network="vpn",
            materialize_test_install=True,
            materialize_test_install_skip_build=True,
        ),
    )

    assert registered == [Path(option_values(captured["command"], "--app-exe")[0])]
    assert captured["register_kwargs"] == {"required": True}
    assert node_exe not in registered
    assert browser_exe not in registered


def test_live_e2e_hide_me_registration_updates_only_whitelist_for_materialized_exe(tmp_path: Path, monkeypatch) -> None:
    settings = tmp_path / "vpn.settings"
    unrelated_limit_entry = {"Name": "Other", "Path": r"C:\Tools\other.exe", "Paths": None, "Icon": None}
    settings.write_text(
        json.dumps(
            {
                "KillswitchWhitelist": [],
                "SplitTunneling": {
                    "Mode": 1,
                    "Whitelisted": [],
                    "LimitToVpn": [unrelated_limit_entry],
                },
            }
        ),
        encoding="utf-8",
    )
    events: list[str] = []

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        events.append("run")

    def fake_materialize(layout_arg, workspace_options, install_options, *, run_id, suite_name, client_id, lan_bind_address=None):
        events.append("materialize")
        install_root = layout.output_tmp_root / "test-installs" / run_id / suite_name / client_id
        app_exe = install_root / "apps" / "eMuleBB" / "emulebb.exe"
        app_exe.parent.mkdir(parents=True)
        app_exe.write_bytes(b"exe")
        (install_root / "harness-profile-seed" / "config").mkdir(parents=True)
        (install_root / "profiles" / "emulebb").mkdir(parents=True)
        return SimpleNamespace(
            target_path=install_root,
            app_root=app_exe.parent,
            app_exe=app_exe,
            profile_dir=install_root / "profiles" / "emulebb",
            profile_config_dir=install_root / "profiles" / "emulebb" / "config",
            profile_seed_config_dir=install_root / "harness-profile-seed" / "config",
        )

    def fake_stop():
        events.append("stop")
        assert json.loads(settings.read_text(encoding="utf-8"))["SplitTunneling"]["Whitelisted"] == []
        return {"requested": True, "returncode": 0}

    def fake_start():
        events.append("start")
        payload = json.loads(settings.read_text(encoding="utf-8"))
        assert payload["SplitTunneling"]["Whitelisted"][0]["Path"].endswith("emulebb.exe")
        return {"requested": True, "returncode": 0, "vpn_ipv4": "10.8.0.9"}

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setattr(test_runs, "materialize_test_local_install", fake_materialize)
    monkeypatch.setattr(test_runs, "ensure_split_tunnel_apps", hide_me_split_tunnel.ensure_split_tunnel_apps)
    monkeypatch.setattr(hide_me_split_tunnel, "stop_hide_me", fake_stop)
    monkeypatch.setattr(hide_me_split_tunnel, "start_hide_me", fake_start)
    monkeypatch.setenv(hide_me_split_tunnel.SETTINGS_PATH_ENV, str(settings))
    monkeypatch.setenv(hide_me_split_tunnel.ALLOW_LOOPBACK_ENV, "0")

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            suites=("rest-api",),
            test_network="vpn",
            materialize_test_install=True,
            materialize_test_install_skip_build=True,
        ),
    )

    payload = json.loads(settings.read_text(encoding="utf-8"))
    whitelisted = payload["SplitTunneling"]["Whitelisted"]
    assert payload["SplitTunneling"]["Mode"] == 2
    assert len(whitelisted) == 1
    assert whitelisted[0]["Name"] == "eMuleBB"
    assert whitelisted[0]["Path"].endswith("emulebb.exe")
    assert payload["SplitTunneling"]["LimitToVpn"] == [unrelated_limit_entry]
    assert events == ["materialize", "stop", "start", "run"]


def test_live_e2e_restarts_hide_me_when_failed_report_points_at_upnp(tmp_path: Path, monkeypatch) -> None:
    layout = make_layout(tmp_path)
    reports = layout.output_reports_root / "live-e2e-suite" / "run"
    reports.mkdir(parents=True)
    (reports / "rest-api-smoke-result.json").write_text(
        '{"status":"failed","failed_phase":"nat_backend_order","error":"Timed out waiting for UPnP NAT backend order"}',
        encoding="utf-8",
    )
    recovery_inputs: list[str] = []

    class Completed:
        returncode = 1

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        assert allow_failure is True
        return Completed()

    def fake_recover(failure_text):
        recovery_inputs.append(failure_text)
        return {"requested": True}

    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setattr(test_runs, "ensure_split_tunnel_apps", lambda paths, **_kwargs: {"enabled": False})
    monkeypatch.setattr(test_runs, "restart_hide_me_after_upnp_failure_if_requested", fake_recover)

    with pytest.raises(RuntimeError, match="live E2E suite failed with exit code 1"):
        test_runs.invoke_live_e2e_suite(
            layout,
            WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
            LiveE2eOptions(suites=("rest-api",), test_network="vpn"),
        )

    assert len(recovery_inputs) == 1
    assert "nat_backend_order" in recovery_inputs[0]


def test_live_e2e_restarts_hide_me_when_app_log_points_at_upnp(tmp_path: Path, monkeypatch) -> None:
    layout = make_layout(tmp_path)
    logs = layout.output_reports_root / "live-e2e-suite" / "run" / "rest-api" / "logs"
    logs.mkdir(parents=True)
    (logs / "emulebb-verbose.log").write_text(
        "UPnP failed to setup port forwarding\nAdding PortMapping failed for port 9447 (TCP), Error Code 501\n",
        encoding="utf-8",
    )
    recovery_inputs: list[str] = []

    class Completed:
        returncode = 1

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        assert allow_failure is True
        return Completed()

    def fake_recover(failure_text):
        recovery_inputs.append(failure_text)
        return {"requested": True}

    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setattr(test_runs, "ensure_split_tunnel_apps", lambda paths, **_kwargs: {"enabled": False})
    monkeypatch.setattr(test_runs, "restart_hide_me_after_upnp_failure_if_requested", fake_recover)

    with pytest.raises(RuntimeError, match="live E2E suite failed with exit code 1"):
        test_runs.invoke_live_e2e_suite(
            layout,
            WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
            LiveE2eOptions(suites=("rest-api",), test_network="vpn"),
        )

    assert len(recovery_inputs) == 1
    assert "PortMapping failed" in recovery_inputs[0]


def test_live_e2e_retries_once_after_hide_me_upnp_recovery(tmp_path: Path, monkeypatch) -> None:
    layout = make_layout(tmp_path)
    logs = layout.output_reports_root / "live-e2e-suite" / "run" / "rest-api" / "logs"
    logs.mkdir(parents=True)
    (logs / "emulebb-verbose.log").write_text(
        "UPnP failed to setup port forwarding\n",
        encoding="utf-8",
    )
    calls = 0
    recovery_inputs: list[str] = []

    class Completed:
        def __init__(self, returncode: int):
            self.returncode = returncode

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        nonlocal calls
        calls += 1
        assert allow_failure is True
        return Completed(1 if calls == 1 else 0)

    def fake_recover(failure_text):
        recovery_inputs.append(failure_text)
        return {"requested": True}

    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setattr(test_runs, "ensure_split_tunnel_apps", lambda paths, **_kwargs: {"enabled": False})
    monkeypatch.setattr(test_runs, "restart_hide_me_after_upnp_failure_if_requested", fake_recover)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("rest-api",), test_network="vpn"),
    )

    assert calls == 2
    assert len(recovery_inputs) == 1


def test_live_e2e_forwards_multi_client_required_optional_clients(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            suites=("multi-client-p2p-matrix",),
            multi_client_require_optional_clients=True,
        ),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--multi-client-require-optional-clients" in command
    assert option_values(command, "--app-exe") == [
        str(layout.output_root / "builds" / "app" / "main" / "x64" / "Release" / "standard" / "bin" / "emulebb.exe")
    ]


def test_live_e2e_forwards_output_root_tracing_harness_when_available(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    tracing_root = layout.workspace_root / "app" / "emulebb-community-tracing-harness"
    tracing_root.mkdir(parents=True)
    layout = replace(
        layout,
        app_variants=(
            *layout.app_variants,
            AppVariant(name="tracing-harness", path=tracing_root, branch="tracing-harness/community-0.72a"),
        ),
    )
    harness_exe = layout.output_root / "builds" / "app" / "tracing-harness" / "x64" / "Release" / "standard" / "bin" / "emule.exe"
    harness_exe.parent.mkdir(parents=True)
    harness_exe.write_bytes(b"exe")
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("multi-client-p2p-matrix",)),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--client2-app-exe") == [str(harness_exe)]


def test_live_e2e_deterministic_transfer_prefers_output_root_main_client2(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    tracing_root = layout.workspace_root / "app" / "emulebb-community-tracing-harness"
    tracing_root.mkdir(parents=True)
    layout = replace(
        layout,
        app_variants=(
            *layout.app_variants,
            AppVariant(name="tracing-harness", path=tracing_root, branch="tracing-harness/community-0.72a"),
        ),
    )
    harness_exe = layout.output_root / "builds" / "app" / "tracing-harness" / "x64" / "Release" / "standard" / "bin" / "emule.exe"
    harness_exe.parent.mkdir(parents=True)
    harness_exe.write_bytes(b"exe")
    main_exe = layout.output_root / "builds" / "app" / "main" / "x64" / "Release" / "standard" / "bin" / "emulebb.exe"
    main_exe.parent.mkdir(parents=True)
    main_exe.write_bytes(b"exe")
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("deterministic-two-client-transfer",)),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--client2-app-exe") == [str(main_exe)]


def test_live_e2e_deterministic_transfer_prefers_package_build_main_client2(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    tracing_root = layout.workspace_root / "app" / "emulebb-community-tracing-harness"
    tracing_root.mkdir(parents=True)
    layout = replace(
        layout,
        app_variants=(
            *layout.app_variants,
            AppVariant(name="tracing-harness", path=tracing_root, branch="tracing-harness/community-0.72a"),
        ),
    )
    harness_exe = layout.output_root / "builds" / "app" / "tracing-harness" / "x64" / "Release" / "standard" / "bin" / "emule.exe"
    harness_exe.parent.mkdir(parents=True)
    harness_exe.write_bytes(b"exe")
    package_exe = (
        layout.output_root
        / "packages"
        / "build"
        / "emulebb-v0.7.4-rc.2"
        / "x64"
        / "standard"
        / "app"
        / "emulebb.exe"
    )
    package_exe.parent.mkdir(parents=True)
    package_exe.write_bytes(b"exe")
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            suites=("deterministic-two-client-transfer",),
            materialize_test_install_release_version="0.7.4-rc.2",
        ),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--client2-app-exe") == [str(package_exe)]


def test_live_e2e_forwards_standardized_output_root_tracing_harness_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    tracing_root = layout.workspace_root / "app" / "emulebb-community-tracing-harness"
    tracing_root.mkdir(parents=True)
    layout = replace(
        layout,
        app_variants=(
            *layout.app_variants,
            AppVariant(name="tracing-harness", path=tracing_root, branch="tracing-harness/community-0.72a"),
        ),
    )
    harness_exe = layout.output_root / "builds" / "app" / "tracing-harness" / "x64" / "Release" / "standard" / "bin" / "emulebb.exe"
    harness_exe.parent.mkdir(parents=True)
    harness_exe.write_bytes(b"exe")
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("deterministic-two-client-transfer",)),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--client2-app-exe") == [str(harness_exe)]


def test_live_e2e_forwards_radarr_movie_root_only_when_configured(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("radarr-sonarr-emulebb",)),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--radarr-movie-root" not in command

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("radarr-sonarr-emulebb",), radarr_movie_root="/media/radarr-import-root"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--radarr-movie-root") == ["/media/radarr-import-root"]


def test_live_e2e_forwards_sonarr_series_root_only_when_configured(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("sonarr-emulebb",)),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--sonarr-series-root" not in command

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("sonarr-emulebb",), sonarr_series_root="/media/sonarr-import-root"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--sonarr-series-root") == ["/media/sonarr-import-root"]


def test_live_e2e_forwards_acquisition_timeout_only_when_configured(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("sonarr-emulebb",)),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--media-acquisition-timeout-minutes" not in command

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("sonarr-emulebb",), acquisition_timeout_minutes=90.0),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--media-acquisition-timeout-minutes") == ["90.0"]


def test_live_e2e_forwards_arr_download_proof_mode(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("radarr-emulebb",)),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--arr-download-proof-mode" not in command

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("radarr-emulebb",), arr_download_proof_mode="handoff"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--arr-download-proof-mode") == ["handoff"]

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            suites=("radarr-emulebb",),
            arr_download_proof_mode="complete",
            arr_download_proof_mode_explicit=True,
        ),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--arr-download-proof-mode") == ["complete"]


def test_live_e2e_forwards_profile_only_when_configured(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--profile" not in command

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(profile="controller-surface"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--profile") == ["controller-surface"]

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(profile="release-expanded"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--profile") == ["release-expanded"]

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(profile="release-expanded-quick"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--profile") == ["release-expanded-quick"]

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(profile="stabilization-stress"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--profile") == ["stabilization-stress"]

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(profile="stabilization-stress-quick"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--profile") == ["stabilization-stress-quick"]

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(profile="cpu-heavy"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--profile") == ["cpu-heavy"]

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(profile="cpu-heavy-quick"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--profile") == ["cpu-heavy-quick"]

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(profile="ui-resource-depth"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--profile") == ["ui-resource-depth"]

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(profile="multi-client-p2p-required"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--profile") == ["multi-client-p2p-required"]

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(profile="controller-local"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--profile") == ["controller-local"]

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(profile="diagnostics-soak"),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--profile") == ["diagnostics-soak"]


def test_live_e2e_runs_pre_run_cleanup_when_requested(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        calls.append("run")

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_pre_test_cleanup", lambda _layout: calls.append("cleanup"))
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(profile="release-expanded", pre_run_cleanup=True),
    )

    assert calls == ["cleanup", "run"]


def test_live_e2e_uses_env_workspace_root(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("disk-space-guard-live",)),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--workspace-root" not in command
    assert "--artifacts-dir" not in command


def test_live_e2e_forwards_live_wire_inputs_file_only_when_configured(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("prowlarr-emulebb",)),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--live-wire-inputs-file" not in command

    live_wire_inputs_file = str(tmp_path / "repos" / "emulebb-build-tests" / "live-wire-inputs.local.json")
    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(suites=("prowlarr-emulebb",), live_wire_inputs_file=live_wire_inputs_file),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--live-wire-inputs-file") == [live_wire_inputs_file]


def test_live_e2e_resolves_existing_workspace_relative_live_wire_inputs_file(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    inputs_file = layout.tests_repo_root / "live-wire-inputs.local.json"
    inputs_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_live_e2e_suite(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        LiveE2eOptions(
            suites=("rest-api",),
            live_wire_inputs_file=r"repos\emulebb-build-tests\live-wire-inputs.local.json",
        ),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--live-wire-inputs-file") == [str(inputs_file.resolve())]


def test_release_campaign_report_forwards_campaign_phase_and_json_options(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)
        captured["label"] = label
        captured["cwd"] = cwd
        captured["env"] = dict(env or {})

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_release_campaign_report(
        layout,
        ReleaseCampaignOptions(
            campaign="emulebb-0.7.3",
            phase="live-wire-release",
            json_output=True,
        ),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert captured["label"] == "release campaign report"
    assert option_values(command, "--campaign") == ["emulebb-0.7.3"]
    assert option_values(command, "--phase") == ["live-wire-release"]
    assert option_values(command, "--test-repo-root") == [str(layout.tests_repo_root)]
    assert "--workspace-root" not in command
    assert "--workspace-state-root" not in command
    assert "--json" in command
    assert captured["env"] == layout.subprocess_environment()
