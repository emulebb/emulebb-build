"""Workspace test and live-test orchestration."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import platform
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

from .config import (
    AmutorrentCleanStartupOptions,
    AmutorrentEmulebbUiOptions,
    AmutorrentResilienceOptions,
    AmutorrentSessionOptions,
    CommunityCoverageOptions,
    FakeKadTrustSoakOptions,
    LiveE2eOptions,
    LocalPackageInstallOptions,
    ReleaseCampaignOptions,
    VariantComparisonOptions,
    WorkspaceOptions,
)
from .build import APP_EXE_NAME, app_build_binary_path, build_apps
from .cleanup import run_pre_test_cleanup
from .hide_me_split_tunnel import ensure_split_tunnel_apps, restart_hide_me_after_upnp_failure_if_requested
from .layout import WorkspaceLayout, get_test_build_tag
from .local_package_install import materialize_test_local_install
from .network_context import LAN_IP_RESOLVED_ENV, TestNetwork, resolve_workspace_network_context
from .process import get_python_invocation, run_native

MATERIALIZED_ARR_SERVICE_WAIT_SECONDS = 120.0
MATERIALIZED_ARR_SERVICE_POLL_SECONDS = 1.0
TRACING_HARNESS_EXE_NAMES = ("emule.exe", APP_EXE_NAME)
MAIN_CLIENT2_SUITE_NAMES = frozenset({"deterministic-two-client-transfer"})

# Native doctest suites run by ``test all`` across every certification tier. The
# core three (parity, protocol-parity, web_api) carry the bulk of parity/REST
# coverage; the rest are fast, seam-driven behavior suites that were previously
# dormant (reachable only via ``test native --suite-name``) and are wired in here
# so they actually gate. Each was verified green and fast before inclusion.
# Deliberately excluded: divergence (a 0.8.0 scheduler-removal guard that stays
# red until the legacy removal lands), performance suites (benchmark, pipeline,
# pipeline-benchmark), and the frozen MFC UI-shortcut suites.
# Keep in sync with TIER_NATIVE_SUITES in emulebb-build-tests test_inventory.py.
TEST_ALL_NATIVE_SUITES = (
    "parity",
    "protocol-parity",
    "web_api",
    "async_dns_resolve",
    "background_refresh",
    "diagnostic_snapshot",
    "fake_file_detector",
    "kad-base",
    "known_file_hash_open",
    "packets",
    "part_file_hash_launch",
    "part_file_majority_name",
    "process_launch",
    "restart_app",
    "search_trust_hint",
    "server_connect",
    "server_info",
    "standby_prevention",
    "startup",
    "startup_storage",
    "version_check_launch",
    "windows_firewall_repair",
)


@dataclass(frozen=True)
class MaterializedArrServiceSpec:
    """One ARR-compatible service staged by the suite installer."""

    name: str
    config_name: str
    app_dir_name: str
    exe_name: str
    data_dir_name: str
    status_api_path: str


@dataclass
class StartedMaterializedService:
    """One service process started for a materialized live E2E run."""

    spec: MaterializedArrServiceSpec
    process: subprocess.Popen[str]
    log_handle: object


MATERIALIZED_ARR_SERVICES = (
    MaterializedArrServiceSpec(
        name="Prowlarr",
        config_name="prowlarr",
        app_dir_name="prowlarr",
        exe_name="Prowlarr.exe",
        data_dir_name="prowlarr",
        status_api_path="/api/v1/system/status",
    ),
    MaterializedArrServiceSpec(
        name="Radarr",
        config_name="radarr",
        app_dir_name="radarr",
        exe_name="Radarr.exe",
        data_dir_name="radarr",
        status_api_path="/api/v3/system/status",
    ),
    MaterializedArrServiceSpec(
        name="Sonarr",
        config_name="sonarr",
        app_dir_name="sonarr",
        exe_name="Sonarr.exe",
        data_dir_name="sonarr",
        status_api_path="/api/v3/system/status",
    ),
)

ARR_LIVE_E2E_SUITES = frozenset(
    {
        "prowlarr-emulebb",
        "radarr-emulebb",
        "sonarr-emulebb",
        "radarr-emulebb-local",
        "sonarr-emulebb-local",
    }
)
ARR_LIVE_E2E_PROFILES = frozenset({"controller-local"})
INSTALLER_BACKED_LIVE_E2E_PROFILES = frozenset(
    {
        "installer-controller-surface",
        "installer-controller-surface-soak",
    }
)


def invoke_test_runs(layout: WorkspaceLayout, options: WorkspaceOptions, *, coverage: bool = True) -> None:
    """Runs native parity/web_api suites, optional coverage, and live-diff.

    The coverage pass re-executes the native suites under instrumentation, so it
    doubles native runtime. Pass ``coverage=False`` for the lean quick tier where
    coverage reporting is not needed; release-gating tiers keep it on.
    """

    invoke_native_test_suites(layout, options, None, TEST_ALL_NATIVE_SUITES)

    if coverage:
        python = get_python_invocation()
        test_run_variant = layout.test_targets.test_run_variant
        app_root = layout.get_app_variant(test_run_variant).path
        run_native(
            python.command(
                [
                    layout.tests_repo_root / "scripts" / "run-native-coverage.py",
                    "--test-repo-root",
                    layout.tests_repo_root,
                    "--app-root",
                    app_root,
                    "--configuration",
                    options.configuration,
                    "--platform",
                    options.platform,
                    "--suite-name",
                    "parity",
                    "--suite-name",
                    "protocol-parity",
                    "--suite-name",
                    "web_api",
                ]
            ),
            label="native coverage",
            cwd=layout.emule_workspace_root,
            env=_workspace_env(layout),
        )
    invoke_live_diff_runs(layout, options, VariantComparisonOptions())


def invoke_native_test_suites(
    layout: WorkspaceLayout,
    options: WorkspaceOptions,
    test_run_variant: str | None,
    suite_names: Sequence[str],
) -> None:
    """Runs the native emule-tests executable without live-diff or live E2E work."""

    _assert_test_execution_platform_supported(options)
    selected_variant = test_run_variant or layout.test_targets.test_run_variant
    app_root = layout.get_app_variant(selected_variant).path
    build_tag = get_test_build_tag(layout.workspace_root, app_root)
    binary_path = (
        layout.output_build_root
        / "tests"
        / build_tag
        / options.platform
        / options.configuration
        / "bin"
        / "emule-tests.exe"
    )
    if not binary_path.is_file():
        raise RuntimeError(f"Built test executable not found: {binary_path}")

    suites = tuple(suite_names) if suite_names else ("parity", "web_api")
    for suite_name in suites:
        run_native(
            [binary_path, f"--test-suite={suite_name}"],
            label=f"{suite_name} tests {selected_variant} {options.configuration}/{options.platform}",
            cwd=layout.tests_repo_root,
        )


def invoke_live_diff_runs(
    layout: WorkspaceLayout,
    options: WorkspaceOptions,
    comparison_options: VariantComparisonOptions,
) -> None:
    """Runs live-diff between two configured app variants."""

    _assert_test_execution_platform_supported(options)
    test_run_variant = comparison_options.test_run_variant or layout.test_targets.test_run_variant
    baseline_variant = comparison_options.baseline_variant or layout.test_targets.baseline_variant
    test_run_app_root = layout.get_app_variant(test_run_variant).path
    baseline_app_root = layout.get_app_variant(baseline_variant).path
    python = get_python_invocation()
    run_native(
        python.command(
            [
                layout.tests_repo_root / "scripts" / "run-live-diff.py",
                "--test-repo-root",
                layout.tests_repo_root,
                "--test-run-workspace-root",
                layout.workspace_root,
                "--baseline-workspace-root",
                layout.workspace_root,
                "--test-run-app-root",
                test_run_app_root,
                "--baseline-app-root",
                baseline_app_root,
                "--configuration",
                options.configuration,
                "--platform",
                options.platform,
            ]
        ),
        label=f"live diff {test_run_variant} vs {baseline_variant}",
        cwd=layout.emule_workspace_root,
        env=_workspace_env(layout),
    )


def invoke_protocol_parity(
    layout: WorkspaceLayout,
    options: WorkspaceOptions,
    comparison_options: VariantComparisonOptions,
) -> None:
    """Runs the focused Kad/eD2K protocol parity gate."""

    _assert_test_execution_platform_supported(options)
    test_run_variant = comparison_options.test_run_variant or layout.test_targets.test_run_variant
    baseline_variant = comparison_options.baseline_variant or layout.test_targets.baseline_variant
    test_run_app_root = layout.get_app_variant(test_run_variant).path
    baseline_app_root = layout.get_app_variant(baseline_variant).path
    python = get_python_invocation()

    run_native(
        python.command(
            [
                layout.tests_repo_root / "scripts" / "run-protocol-surface-diff.py",
                "--test-repo-root",
                layout.tests_repo_root,
                "--test-run-app-root",
                test_run_app_root,
                "--baseline-app-root",
                baseline_app_root,
            ]
        ),
        label=f"protocol surface diff {test_run_variant} vs {baseline_variant}",
        cwd=layout.emule_workspace_root,
        env=_workspace_env(layout),
    )
    run_native(
        python.command(
            [
                layout.tests_repo_root / "scripts" / "validate-protocol-goldens.py",
                "--test-repo-root",
                layout.tests_repo_root,
            ]
        ),
        label="protocol oracle golden validation",
        cwd=layout.emule_workspace_root,
        env=_workspace_env(layout),
    )
    run_native(
        python.command(
            [
                layout.tests_repo_root / "scripts" / "run-live-diff.py",
                "--test-repo-root",
                layout.tests_repo_root,
                "--test-run-workspace-root",
                layout.workspace_root,
                "--baseline-workspace-root",
                layout.workspace_root,
                "--test-run-app-root",
                test_run_app_root,
                "--baseline-app-root",
                baseline_app_root,
                "--configuration",
                options.configuration,
                "--platform",
                options.platform,
                "--suite-name",
                "protocol-parity",
            ]
        ),
        label=f"protocol parity live diff {test_run_variant} vs {baseline_variant}",
        cwd=layout.emule_workspace_root,
        env=_workspace_env(layout),
    )


def invoke_community_core_coverage(
    layout: WorkspaceLayout,
    options: WorkspaceOptions,
    coverage_options: CommunityCoverageOptions,
) -> None:
    """Runs community-core coverage checks between two variants."""

    _assert_test_execution_platform_supported(options)
    test_run_variant = coverage_options.test_run_variant or layout.test_targets.test_run_variant
    baseline_variant = coverage_options.baseline_variant or layout.test_targets.baseline_variant
    test_run_app_root = layout.get_app_variant(test_run_variant).path
    baseline_app_root = layout.get_app_variant(baseline_variant).path
    python = get_python_invocation()
    run_native(
        python.command(
            [
                layout.tests_repo_root / "scripts" / "run-community-core-coverage.py",
                "--test-repo-root",
                layout.tests_repo_root,
                "--main-app-root",
                test_run_app_root,
                "--community-app-root",
                baseline_app_root,
                "--configuration",
                options.configuration,
                "--platform",
                options.platform,
                "--include-live-rest-e2e",
                "--rest-coverage-budget",
                coverage_options.rest_coverage_budget,
                "--rest-stress-budget",
                coverage_options.rest_stress_budget,
            ]
        ),
        label=f"community core coverage {test_run_variant} vs {baseline_variant}",
        cwd=layout.emule_workspace_root,
        env=_workspace_env(layout),
    )


def invoke_live_e2e_suite(layout: WorkspaceLayout, options: WorkspaceOptions, live_options: LiveE2eOptions) -> None:
    """Runs the aggregate live E2E suite."""

    _assert_live_e2e_execution_platform_supported(options)
    if live_options.pre_run_cleanup:
        run_pre_test_cleanup(layout)
    effective_test_network = _live_e2e_effective_test_network(live_options)
    materialize_test_install = _live_e2e_materialize_test_install(live_options)
    lan_bind_address = _pre_materialize_lan_bind_address(
        layout,
        live_options,
        materialize_test_install=materialize_test_install,
        test_network=effective_test_network,
    )
    app_root = layout.get_app_variant(layout.test_targets.test_run_variant).path
    app_exe: Path | None = None
    profile_seed_config_dir: Path | None = None
    live_process_monitor_profile_dir: Path | None = None
    if materialize_test_install:
        materialized = materialize_test_local_install(
            layout,
            options,
            LocalPackageInstallOptions(
                live_wire_inputs_file=live_options.live_wire_inputs_file,
                release_version=live_options.materialize_test_install_release_version,
                clean=live_options.materialize_test_install_clean,
                skip_build=live_options.materialize_test_install_skip_build,
            ),
            run_id=_live_e2e_test_install_run_id(),
            suite_name="live-e2e-suite",
            client_id=layout.test_targets.test_run_variant,
            lan_bind_address=lan_bind_address,
        )
        app_root = materialized.app_root
        app_exe = materialized.app_exe
        profile_seed_config_dir = materialized.profile_seed_config_dir
        live_process_monitor_profile_dir = materialized.profile_dir
    elif _live_e2e_requires_startup_diagnostics_workspace_build(live_options):
        build_apps(
            layout,
            options,
            clean=False,
            app_variant_names=(layout.test_targets.test_run_variant,),
            enable_startup_diagnostics=True,
        )
    if live_options.live_process_monitor_profile_dir:
        live_process_monitor_profile_dir = Path(
            _resolve_workspace_path_argument(layout, live_options.live_process_monitor_profile_dir)
        )
    resolved_app_exe = _resolve_live_e2e_app_exe(layout, app_root, app_exe, options)
    resolved_client2_app_exe = _resolve_live_e2e_client2_app_exe(layout, options, live_options)
    _ensure_hide_me_split_tunnel_for_live(
        test_network=live_options.test_network,
        p2p_bind_interface_name=live_options.p2p_bind_interface_name,
        app_exe=resolved_app_exe,
    )
    network_env = _test_network_env(
        layout,
        test_network=effective_test_network,
        vpn_interface_name=live_options.p2p_bind_interface_name,
        require_lan=(
            live_options.campaign_scenario_uses_local_swarm
            or (materialize_test_install and effective_test_network in {"lan", "vpn", "all"})
        ),
    )
    script_path = layout.tests_repo_root / "scripts" / "run-live-e2e-suite.py"
    if not script_path.is_file():
        raise RuntimeError(f"Missing live E2E suite runner: {script_path}")

    args: list[str | Path | int | float] = [
        script_path,
        "--app-root",
        app_root,
        "--configuration",
        options.configuration,
        "--startup-trace-mode",
        live_options.startup_trace_mode,
        "--test-network",
        effective_test_network,
        "--profile-cpu-max-file-mb",
        live_options.profile_cpu_max_file_mb,
        "--profile-cpu-stack-min-hits",
        live_options.profile_cpu_stack_min_hits,
        "--profile-resource-interval-seconds",
        live_options.profile_resource_interval_seconds,
        "--rest-server-search-count",
        live_options.rest_server_search_count,
        "--rest-kad-search-count",
        live_options.rest_kad_search_count,
        "--rest-download-trigger-count",
        live_options.rest_download_trigger_count,
        "--rest-coverage-budget",
        live_options.rest_coverage_budget,
        "--rest-stress-budget",
        live_options.rest_stress_budget,
        "--rest-stress-duration-seconds",
        live_options.rest_stress_duration_seconds,
        "--rest-stress-concurrency",
        live_options.rest_stress_concurrency,
        "--rest-stress-max-failures",
        live_options.rest_stress_max_failures,
        "--rest-stress-request-timeout-seconds",
        live_options.rest_stress_request_timeout_seconds,
        "--rest-socket-adversity-budget",
        live_options.rest_socket_adversity_budget,
        "--rest-tls-handshake-adversity-budget",
        live_options.rest_tls_handshake_adversity_budget,
        "--rest-leak-churn-budget",
        live_options.rest_leak_churn_budget,
        "--search-ui-search-rounds",
        live_options.search_ui_search_rounds,
        "--search-ui-download-lifecycle-count",
        live_options.search_ui_download_lifecycle_count,
        "--p2p-bind-interface-name",
        live_options.p2p_bind_interface_name,
        "--rest-cold-start-dump-stress-waves",
        live_options.rest_cold_start_dump_stress_waves,
        "--rest-cold-start-dump-stress-searches-per-wave",
        live_options.rest_cold_start_dump_stress_searches_per_wave,
        "--rest-cold-start-dump-stress-max-concurrent-searches",
        live_options.rest_cold_start_dump_stress_max_concurrent_searches,
        "--rest-cold-start-dump-stress-search-observation-timeout-seconds",
        live_options.rest_cold_start_dump_stress_search_observation_timeout_seconds,
        "--rest-cold-start-dump-stress-downloads-per-wave",
        live_options.rest_cold_start_dump_stress_downloads_per_wave,
        "--rest-cold-start-dump-stress-downloads-per-search",
        live_options.rest_cold_start_dump_stress_downloads_per_search,
        "--rest-cold-start-dump-stress-max-missing-download-triggers",
        live_options.rest_cold_start_dump_stress_max_missing_download_triggers,
        "--rest-cold-start-dump-stress-synthetic-queue-fill-count",
        live_options.rest_cold_start_dump_stress_synthetic_queue_fill_count,
        "--rest-cold-start-dump-stress-synthetic-queue-fill-size-bytes",
        live_options.rest_cold_start_dump_stress_synthetic_queue_fill_size_bytes,
        "--rest-cold-start-dump-stress-synthetic-queue-fill-batch-size",
        live_options.rest_cold_start_dump_stress_synthetic_queue_fill_batch_size,
        "--rest-cold-start-dump-stress-target-completed-downloads",
        live_options.rest_cold_start_dump_stress_target_completed_downloads,
        "--rest-cold-start-dump-stress-completion-timeout-seconds",
        live_options.rest_cold_start_dump_stress_completion_timeout_seconds,
        "--rest-cold-start-dump-stress-max-active-downloads",
        live_options.rest_cold_start_dump_stress_max_active_downloads,
        "--rest-cold-start-dump-stress-download-churn-interval-seconds",
        live_options.rest_cold_start_dump_stress_download_churn_interval_seconds,
        "--rest-cold-start-dump-stress-download-remove-count-per-churn",
        live_options.rest_cold_start_dump_stress_download_remove_count_per_churn,
        "--rest-cold-start-dump-stress-resource-monitor-interval-seconds",
        live_options.rest_cold_start_dump_stress_resource_monitor_interval_seconds,
        "--rest-cold-start-dump-stress-post-drain-seconds",
        live_options.rest_cold_start_dump_stress_post_drain_seconds,
        "--rest-cold-start-dump-stress-tool-timeout-seconds",
        live_options.rest_cold_start_dump_stress_tool_timeout_seconds,
        "--rest-cold-start-dump-stress-cpu-profile-max-file-mb",
        live_options.rest_cold_start_dump_stress_cpu_profile_max_file_mb,
        "--rest-cold-start-dump-stress-cpu-profile-stack-min-hits",
        live_options.rest_cold_start_dump_stress_cpu_profile_stack_min_hits,
    ]
    if app_exe is not None:
        args.extend(["--app-exe", app_exe])
    elif resolved_app_exe is not None:
        args.extend(["--app-exe", resolved_app_exe])
    if profile_seed_config_dir is not None:
        args.extend(["--profile-seed-dir", profile_seed_config_dir])
    if resolved_client2_app_exe is not None:
        args.extend(["--client2-app-exe", resolved_client2_app_exe])
    if live_process_monitor_profile_dir is not None:
        args.extend(["--live-process-monitor-profile-dir", live_process_monitor_profile_dir])
    _append_optional_flag(args, live_options.profile_cpu, "--profile-cpu")
    _append_optional_flag(args, live_options.profile_cpu_stack, "--profile-cpu-stack")
    _append_optional_flag(args, live_options.multi_client_require_optional_clients, "--multi-client-require-optional-clients")
    _append_optional_flag(args, live_options.godzilla_visible_ui, "--godzilla-visible-ui")
    _append_optional_flag(args, live_options.godzilla_cpu_profile, "--godzilla-cpu-profile")
    if live_options.godzilla_p2p_bind_interface_address:
        args.extend(["--godzilla-p2p-bind-interface-address", live_options.godzilla_p2p_bind_interface_address])
    if live_options.godzilla_stage is not None:
        args.extend(["--godzilla-stage", live_options.godzilla_stage])
    args.extend(["--godzilla-vhd-runtime-root", live_options.godzilla_vhd_runtime_root])
    args.extend(["--godzilla-total-client-count", live_options.godzilla_total_client_count])
    args.extend(["--godzilla-peer-transfer-count", live_options.godzilla_peer_transfer_count])
    args.extend(["--godzilla-harness-transfer-count", live_options.godzilla_harness_transfer_count])
    args.extend(["--godzilla-emulebb-files", live_options.godzilla_emulebb_files])
    args.extend(["--godzilla-extra-emulebb-files", live_options.godzilla_extra_emulebb_files])
    args.extend(["--godzilla-harness-files", live_options.godzilla_harness_files])
    args.extend(["--godzilla-adverse-kill-cycles", live_options.godzilla_adverse_kill_cycles])
    args.extend(["--godzilla-adverse-kill-warmup-seconds", live_options.godzilla_adverse_kill_warmup_seconds])
    args.extend(["--godzilla-adverse-recovery-timeout-seconds", live_options.godzilla_adverse_recovery_timeout_seconds])
    if not live_options.profile_symbols_required:
        args.append("--no-profile-symbols-required")
    _append_optional_flag(args, live_options.profile_memory, "--profile-memory")
    _append_optional_flag(args, live_options.rest_cold_start_dump_stress_enable_umdh, "--rest-cold-start-dump-stress-enable-umdh")
    _append_optional_flag(args, live_options.rest_cold_start_dump_stress_cpu_profile, "--rest-cold-start-dump-stress-cpu-profile")
    _append_optional_flag(args, live_options.rest_cold_start_dump_stress_cpu_profile_stack, "--rest-cold-start-dump-stress-cpu-profile-stack")
    _append_optional_flag(
        args,
        live_options.rest_cold_start_dump_stress_allow_required_zero_result_searches,
        "--rest-cold-start-dump-stress-allow-required-zero-result-searches",
    )
    _append_optional_flag(
        args,
        live_options.rest_cold_start_dump_stress_skip_transfer_cleanup,
        "--rest-cold-start-dump-stress-skip-transfer-cleanup",
    )
    _append_optional_flag(args, live_options.rest_cold_start_dump_stress_skip_umdh_diffs, "--rest-cold-start-dump-stress-skip-umdh-diffs")
    if not live_options.rest_cold_start_dump_stress_cpu_profile_symbols_required:
        args.append("--no-rest-cold-start-dump-stress-cpu-profile-symbols-required")
    _append_optional_flag(args, live_options.rest_cold_start_dump_stress_skip_dumps, "--rest-cold-start-dump-stress-skip-dumps")
    if live_options.rest_leak_churn_cycles >= 0:
        args.extend(["--rest-leak-churn-cycles", live_options.rest_leak_churn_cycles])
    _append_optional_flag(args, live_options.rest_stop_start_after_churn, "--rest-stop-start-after-churn")
    if live_options.vpn_guard_live_config:
        args.extend(["--vpn-guard-live-config", _resolve_workspace_argument(layout, live_options.vpn_guard_live_config)])
    if live_options.vpn_guard_allowed_public_ip_cidrs:
        args.extend(["--vpn-guard-allowed-public-ip-cidrs", live_options.vpn_guard_allowed_public_ip_cidrs])
    args.extend(["--vpn-guard-scenario", live_options.vpn_guard_scenario])
    _append_optional_flag(args, live_options.vpn_guard_expected_startup_block, "--vpn-guard-expected-startup-block")
    if live_options.shared_root:
        args.extend(["--shared-root", live_options.shared_root])
    _append_optional_flag(args, live_options.admin_volume_fixtures, "--admin-volume-fixtures")
    args.extend(["--vhd-size-mb", live_options.vhd_size_mb])
    if live_options.mount_root:
        args.extend(["--mount-root", live_options.mount_root])
    _append_optional_flag(args, live_options.keep_admin_fixtures, "--keep-admin-fixtures")
    args.extend(["--dependency-mode", live_options.dependency_mode])
    args.extend(["--dependency-channel", live_options.dependency_channel])
    if live_options.dependency_cache_root:
        args.extend(["--dependency-cache-root", _resolve_workspace_path_argument(layout, live_options.dependency_cache_root)])
    _append_optional_flag(args, live_options.refresh_dependencies, "--refresh-dependencies")
    if live_options.prowlarr_exe:
        args.extend(["--prowlarr-exe", _resolve_workspace_path_argument(layout, live_options.prowlarr_exe)])
    if live_options.radarr_exe:
        args.extend(["--radarr-exe", _resolve_workspace_path_argument(layout, live_options.radarr_exe)])
    if live_options.sonarr_exe:
        args.extend(["--sonarr-exe", _resolve_workspace_path_argument(layout, live_options.sonarr_exe)])
    _append_optional_flag(args, live_options.preference_ui_directories_tree_stress, "--preference-ui-directories-tree-stress")
    for scenario_name in live_options.shared_files_ui_scenarios:
        args.extend(["--shared-files-ui-scenario", scenario_name])
    if live_options.shared_files_tree_stress_churn_cycles >= 0:
        args.extend(["--shared-files-tree-stress-churn-cycles", live_options.shared_files_tree_stress_churn_cycles])
    if live_options.live_wire_inputs_file:
        args.extend(["--live-wire-inputs-file", _resolve_workspace_argument(layout, live_options.live_wire_inputs_file)])
    if live_options.radarr_movie_root:
        args.extend(["--radarr-movie-root", live_options.radarr_movie_root])
    if live_options.sonarr_series_root:
        args.extend(["--sonarr-series-root", live_options.sonarr_series_root])
    if live_options.acquisition_timeout_minutes is not None:
        args.extend(["--media-acquisition-timeout-minutes", live_options.acquisition_timeout_minutes])
    if live_options.arr_download_proof_mode_explicit or live_options.arr_download_proof_mode != "complete":
        args.extend(["--arr-download-proof-mode", live_options.arr_download_proof_mode])
    if live_options.rest_search_method_override:
        args.extend(["--rest-search-method-override", live_options.rest_search_method_override])
    args.extend(["--rest-webserver-scheme", live_options.rest_webserver_scheme])
    args.extend(["--local-kad-bootstrap-mode", live_options.local_kad_bootstrap_mode])
    args.extend(["--local-kad-nodes-dat-fixture-mode", live_options.local_kad_nodes_dat_fixture_mode])
    if live_options.profile != "default":
        args.extend(["--profile", live_options.profile])
    for suite_name in live_options.suites:
        args.extend(["--suite", suite_name])
    if live_options.campaign_scenario_key:
        args.extend(["--campaign-scenario-key", live_options.campaign_scenario_key])
    if live_options.campaign_scenario_id:
        args.extend(["--campaign-scenario-id", live_options.campaign_scenario_id])
    if live_options.campaign_scenario_vm_profile:
        args.extend(["--campaign-scenario-vm-profile", live_options.campaign_scenario_vm_profile])
    for suite_name in live_options.campaign_scenario_local_suites:
        args.extend(["--campaign-scenario-local-suite", suite_name])
    _append_optional_flag(
        args,
        live_options.campaign_scenario_uses_local_swarm,
        "--campaign-scenario-uses-local-swarm",
    )
    _append_optional_flag(args, live_options.plan_only, "--plan-only")
    _append_optional_flag(args, live_options.fail_fast, "--fail-fast")
    _append_optional_flag(args, live_options.skip_live_seed_refresh, "--skip-live-seed-refresh")

    python = get_python_invocation()
    needs_materialized_arr_services = materialize_test_install and _live_e2e_needs_arr_services(live_options)
    materialized_service_env = (
        _materialized_arr_service_env(
            materialized,
            require_explicit_lan=needs_materialized_arr_services,
        )
        if materialize_test_install
        else {}
    )
    started_services: list[StartedMaterializedService] = []
    try:
        if needs_materialized_arr_services:
            started_services = _start_materialized_arr_services(materialized, materialized_service_env)
        _run_live_native(
            layout,
            python.command(args),
            label="live E2E suite",
            cwd=layout.emule_workspace_root,
            env={
                **network_env,
                **materialized_service_env,
            },
        )
    finally:
        _stop_materialized_arr_services(started_services)


def _materialized_arr_service_env(materialized: object, *, require_explicit_lan: bool = True) -> dict[str, str]:
    """Returns ARR service endpoints from an installer-materialized suite config."""

    app_root = Path(getattr(materialized, "app_root"))
    target_root = Path(getattr(materialized, "target_path", app_root.parent.parent))
    config_path = target_root / "manifests" / "suite-config.json"
    if not config_path.is_file():
        return {}
    payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    services = payload.get("services")
    if not isinstance(services, dict):
        return {}

    mappings = {
        "prowlarr": ("PROWLARR_URL", "PROWLARR_API_KEY"),
        "radarr": ("RADARR_URL", "RADARR_API_KEY"),
        "sonarr": ("SONARR_URL", "SONARR_API_KEY"),
    }
    values: dict[str, str] = {}
    for service_name, (url_key, api_key_key) in mappings.items():
        service = services.get(service_name)
        if not isinstance(service, dict):
            continue
        lan_bind_address = str(service.get("bindAddress") or "").strip()
        port = str(service.get("port") or "").strip()
        api_key = str(service.get("apiKey") or "").strip()
        if not lan_bind_address or not port or not api_key:
            continue
        if lan_bind_address in {"0.0.0.0", "::", "[::]", "localhost", "::1"} or lan_bind_address.startswith("127."):
            if require_explicit_lan:
                raise RuntimeError(f"Materialized {service_name} bindAddress must be an explicit LAN address.")
            continue
        values[url_key] = f"http://{lan_bind_address}:{port}"
        values[api_key_key] = api_key
    return values


def _live_e2e_needs_arr_services(live_options: LiveE2eOptions) -> bool:
    """Returns whether a selected aggregate live run requires local ARR controllers."""

    if live_options.profile in ARR_LIVE_E2E_PROFILES or live_options.profile in INSTALLER_BACKED_LIVE_E2E_PROFILES:
        return True
    if not live_options.suites:
        return True
    return bool(ARR_LIVE_E2E_SUITES.intersection(live_options.suites))


def _start_materialized_arr_services(
    materialized: object, service_env: dict[str, str]
) -> list[StartedMaterializedService]:
    """Starts ARR controller services from an installer-materialized live E2E install."""

    app_root = Path(getattr(materialized, "app_root"))
    target_root = Path(getattr(materialized, "target_path", app_root.parent.parent))
    started: list[StartedMaterializedService] = []
    logs_dir = target_root / "logs" / "live-e2e-services"
    logs_dir.mkdir(parents=True, exist_ok=True)
    try:
        for spec in MATERIALIZED_ARR_SERVICES:
            url = service_env.get(_materialized_service_url_env_name(spec))
            api_key = service_env.get(_materialized_service_api_key_env_name(spec))
            if not url or not api_key:
                continue
            if _materialized_service_ready(url, api_key, spec.status_api_path):
                print(f"Materialized {spec.name} service already available at {url}.")
                continue

            exe_path = _find_materialized_service_exe(target_root, spec)
            if exe_path is None:
                raise RuntimeError(f"Materialized {spec.name} executable not found under {target_root / 'apps' / spec.app_dir_name}.")
            data_dir = target_root / "data" / spec.data_dir_name
            if not data_dir.is_dir():
                raise RuntimeError(f"Materialized {spec.name} data directory not found: {data_dir}")

            log_path = logs_dir / f"{spec.config_name}.log"
            log_handle = log_path.open("a", encoding="utf-8", newline="\n")
            command = [str(exe_path), f"/data={data_dir}", "/nobrowser"]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen(
                command,
                cwd=str(exe_path.parent),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=creationflags,
            )
            started.append(StartedMaterializedService(spec=spec, process=process, log_handle=log_handle))
            print(f"Started materialized {spec.name} service from {exe_path}.")
            _wait_for_materialized_service(spec, url, api_key, log_path=log_path)
    except Exception:
        _stop_materialized_arr_services(started)
        raise
    return started


def _stop_materialized_arr_services(started_services: Sequence[StartedMaterializedService]) -> None:
    """Stops only the materialized service processes started by this runner."""

    for started in reversed(tuple(started_services)):
        process = started.process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=20)
        close = getattr(started.log_handle, "close", None)
        if callable(close):
            close()


def _wait_for_materialized_service(
    spec: MaterializedArrServiceSpec,
    url: str,
    api_key: str,
    *,
    log_path: Path | None = None,
) -> None:
    deadline = time.monotonic() + MATERIALIZED_ARR_SERVICE_WAIT_SECONDS
    while time.monotonic() < deadline:
        if _materialized_service_ready(url, api_key, spec.status_api_path):
            return
        time.sleep(MATERIALIZED_ARR_SERVICE_POLL_SECONDS)
    log_tail = _read_text_tail(log_path, max_chars=4000) if log_path is not None else ""
    detail = f" Recent {spec.name} log tail:\n{log_tail}" if log_tail else ""
    raise RuntimeError(f"Timed out waiting for materialized {spec.name} service at {url}{spec.status_api_path}.{detail}")


def _read_text_tail(path: Path, *, max_chars: int) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except OSError:
        return ""


def _materialized_service_ready(url: str, api_key: str, status_api_path: str) -> bool:
    endpoint = url.rstrip("/") + status_api_path
    request = urllib.request.Request(endpoint, headers={"X-Api-Key": api_key})
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        return False


def _find_materialized_service_exe(target_root: Path, spec: MaterializedArrServiceSpec) -> Path | None:
    root = target_root / "apps" / spec.app_dir_name
    direct = root / spec.exe_name
    if direct.is_file():
        return direct
    matches = sorted(path for path in root.rglob(spec.exe_name) if path.is_file())
    return matches[0] if matches else None


def _materialized_service_url_env_name(spec: MaterializedArrServiceSpec) -> str:
    return f"{spec.config_name.upper()}_URL"


def _materialized_service_api_key_env_name(spec: MaterializedArrServiceSpec) -> str:
    return f"{spec.config_name.upper()}_API_KEY"


def invoke_release_campaign_report(
    layout: WorkspaceLayout,
    campaign_options: ReleaseCampaignOptions,
) -> None:
    """Shows the eMuleBB release campaign matrix and latest evidence status."""

    script_path = layout.tests_repo_root / "scripts" / "show-release-campaigns.py"
    if not script_path.is_file():
        raise RuntimeError(f"Missing release campaign reporter: {script_path}")

    args: list[str | Path] = [
        script_path,
        "--test-repo-root",
        layout.tests_repo_root,
        "--campaign",
        campaign_options.campaign,
    ]
    if campaign_options.phase:
        args.extend(["--phase", campaign_options.phase])
    _append_optional_flag(args, campaign_options.show_template, "--template")
    _append_optional_flag(args, campaign_options.json_output, "--json")

    python = get_python_invocation()
    run_native(
        python.command(args),
        label="release campaign report",
        cwd=layout.emule_workspace_root,
        env=_workspace_env(layout),
    )


def invoke_amutorrent_interactive_session(
    layout: WorkspaceLayout,
    options: WorkspaceOptions,
    session_options: AmutorrentSessionOptions,
) -> None:
    """Starts a disposable interactive aMuTorrent session."""

    _assert_test_execution_platform_supported(options)
    script_path = layout.tests_repo_root / "scripts" / "amutorrent-interactive-session.py"
    if not script_path.is_file():
        raise RuntimeError(f"Missing aMuTorrent interactive session runner: {script_path}")

    args: list[str | Path] = [
        script_path,
        "--backend",
        session_options.backend,
        "--configuration",
        options.configuration,
    ]
    if session_options.backend == "native":
        app_root = layout.get_app_variant(layout.test_targets.test_run_variant).path
        args.extend(["--app-root", app_root])
    else:
        if layout.emulebb_rust_repo_root is None:
            raise RuntimeError("The workspace manifest must define repos.emulebb_rust for Rust aMuTorrent sessions.")
        rust_exe_name = "emulebb-rust.exe" if os.name == "nt" else "emulebb-rust"
        args.extend(
            [
                "--rust-repo",
                layout.emulebb_rust_repo_root,
                "--rust-exe",
                layout.output_tools_root / "emulebb-rust" / "bin" / rust_exe_name,
            ]
        )
    network_env = _test_network_env(layout, test_network="lan", require_lan=True)
    _append_lan_bind_addr(args, network_env)
    _append_optional_flag(args, session_options.live_network, "--live-network")
    python = get_python_invocation()
    run_native(
        python.command(args),
        label="aMuTorrent interactive session",
        cwd=layout.emule_workspace_root,
        env=network_env,
    )


def invoke_rust_network_proof(
    layout: WorkspaceLayout,
    options: WorkspaceOptions,
    *,
    lane: str,
    inputs: str | None,
    source_root: str | None,
    max_candidates: int,
    complete_transfers: bool,
    probe_count: int,
    observe_seconds: float,
    transfer_timeout_seconds: float,
) -> None:
    """Runs one persisted Rust network-proof lane under the workspace lock."""

    _assert_test_execution_platform_supported(options)
    scripts = {
        "local-cross": "emulebb-rust-emulebb-cross-client.py",
        "local-kad": "emulebb-rust-emulebb-cross-client.py",
        "local-protocol": "local-ed2k-rust-protocol-combinations.py",
        "local-reask": "emulebb-rust-reask-cross-client.py",
        "windows-direct": "rust-windows-direct-smoke.py",
        "prepare-corpus": "prepare-rust-direct-corpus.py",
    }
    script = layout.tests_repo_root / "scripts" / scripts[lane]
    if not script.is_file():
        raise RuntimeError(f"Rust network proof runner is missing: {script}")
    if lane == "local-protocol":
        server_repo = layout.emule_workspace_root / "repos" / "goed2k-server"
        if not (server_repo / "go.mod").is_file():
            raise RuntimeError(f"Local ED2K support server is missing: {server_repo}")
        run_native(["go", "test", "./ed2ksrv", "-run", "TestOfferFilesRegistersDynamicSharedEntries", "-count=1"],
                   label="local ED2K support server offer tests",
                   cwd=server_repo, env=_workspace_env(layout))
    args: list[str | Path | float | int] = [script]
    if lane == "prepare-corpus":
        if not inputs or not source_root:
            raise RuntimeError("Corpus preparation requires --inputs and --source-root.")
        args.extend(["--operator-inputs", Path(inputs).resolve(), "--source-root", Path(source_root).resolve(),
                     "--max-candidates", max_candidates])
    elif lane == "windows-direct":
        if not inputs:
            raise RuntimeError("Windows direct proof requires an operator-local --inputs allowlist.")
        args.extend(["--inputs", Path(inputs).resolve(), "--observe-seconds", observe_seconds,
                     "--transfer-timeout-seconds", transfer_timeout_seconds, "--probe-count", probe_count])
        if complete_transfers:
            args.append("--complete-transfers")
    else:
        lan_ip = os.environ.get("X_LOCAL_IP", "").strip()
        if not lan_ip:
            raise RuntimeError("Local Rust network proof requires inherited X_LOCAL_IP.")
        mfc_exe = (
            layout.output_build_root / "app" / "main" / "x64" /
            "Release" / "diagnostics" / "bin" / "emulebb-diagnostics.exe"
        )
        if not mfc_exe.is_file():
            raise RuntimeError(f"Staged MFC diagnostics executable is missing: {mfc_exe}")
        args.extend(["--app-exe", mfc_exe, "--lan-bind-addr", lan_ip, "--diagnostics", "--keep-artifacts"])
        if lane == "local-protocol":
            args.extend(["--client2-app-exe", mfc_exe])
        if lane == "local-kad":
            args.append("--rust-kad-enabled")
    python = get_python_invocation()
    run_native(
        python.command(args),
        label=f"Rust network proof ({lane})",
        cwd=layout.tests_repo_root,
        env=_workspace_env(layout),
    )


def invoke_rust_unit_tests(layout: WorkspaceLayout, *, package: str | None = None) -> None:
    """Run Rust unit/integration tests against the inherited canonical target dir."""

    args = ["cargo", "test", "--locked"]
    if package:
        args.extend(["-p", package])
    else:
        args.append("--workspace")
    run_native(args, label="eMuleBB Rust tests", cwd=layout.emulebb_rust_repo_root,
               env=_workspace_env(layout))


def invoke_fake_kad_trust_soak(
    layout: WorkspaceLayout,
    options: WorkspaceOptions,
    soak_options: FakeKadTrustSoakOptions,
) -> None:
    """Runs the focused fake-file/Kad trust live soak."""

    _assert_test_execution_platform_supported(options)
    app_root = layout.get_app_variant(layout.test_targets.test_run_variant).path
    script_path = layout.tests_repo_root / "scripts" / "fake-kad-trust-soak.py"
    if not script_path.is_file():
        raise RuntimeError(f"Missing fake/Kad trust soak runner: {script_path}")

    args: list[str | Path | float | int] = [
        script_path,
        "--app-root",
        app_root,
        "--configuration",
        options.configuration,
        "--duration-seconds",
        soak_options.duration_seconds,
        "--cycle-pause-seconds",
        soak_options.cycle_pause_seconds,
        "--search-observation-timeout-seconds",
        soak_options.search_observation_timeout_seconds,
        "--resource-sample-interval-seconds",
        soak_options.resource_sample_interval_seconds,
        "--min-result-rows",
        soak_options.min_result_rows,
        "--min-kad-publish-info-rows",
        soak_options.min_kad_publish_info_rows,
        "--max-failed-cycles",
        soak_options.max_failed_cycles,
        "--p2p-bind-interface-name",
        soak_options.p2p_bind_interface_name,
    ]
    if soak_options.live_wire_inputs_file:
        args.extend(["--live-wire-inputs-file", _resolve_workspace_argument(layout, soak_options.live_wire_inputs_file)])
    _append_optional_flag(args, soak_options.keep_artifacts, "--keep-artifacts")
    _append_optional_flag(args, soak_options.keep_running, "--keep-running")
    _append_optional_flag(args, soak_options.skip_live_seed_refresh, "--skip-live-seed-refresh")
    _append_optional_flag(args, soak_options.require_kad_connected, "--require-kad-connected")

    python = get_python_invocation()
    _run_live_native(
        layout,
        python.command(args),
        label="fake/Kad trust soak",
        cwd=layout.emule_workspace_root,
        env=_test_network_env(
            layout,
            test_network=soak_options.test_network,
            vpn_interface_name=soak_options.p2p_bind_interface_name,
            require_vpn=True,
        ),
    )


def invoke_amutorrent_clean_startup(
    layout: WorkspaceLayout,
    options: WorkspaceOptions,
    clean_options: AmutorrentCleanStartupOptions,
) -> None:
    """Runs the automated aMuTorrent first-run wizard integration proof."""

    _assert_test_execution_platform_supported(options)
    app_root = layout.get_app_variant(layout.test_targets.test_run_variant).path
    _ensure_hide_me_split_tunnel_for_live(
        test_network=clean_options.test_network,
        p2p_bind_interface_name=clean_options.p2p_bind_interface_name,
        app_exe=_resolve_live_e2e_app_exe(layout, app_root, None, options),
    )
    script_path = layout.tests_repo_root / "scripts" / "amutorrent-clean-startup.py"
    if not script_path.is_file():
        raise RuntimeError(f"Missing aMuTorrent clean-startup runner: {script_path}")

    args: list[str | Path | float] = [
        script_path,
        "--app-root",
        app_root,
        "--configuration",
        options.configuration,
        "--p2p-bind-interface-name",
        clean_options.p2p_bind_interface_name,
        "--rest-webserver-scheme",
        clean_options.rest_webserver_scheme,
        "--ready-timeout-seconds",
        clean_options.ready_timeout_seconds,
        "--network-ready-timeout-seconds",
        clean_options.network_ready_timeout_seconds,
        "--search-observation-timeout-seconds",
        clean_options.search_observation_timeout_seconds,
    ]
    if clean_options.live_wire_inputs_file:
        args.extend(["--live-wire-inputs-file", _resolve_workspace_argument(layout, clean_options.live_wire_inputs_file)])
    network_env = _test_network_env(
        layout,
        test_network=clean_options.test_network,
        vpn_interface_name=clean_options.p2p_bind_interface_name,
        require_vpn=True,
        require_lan=True,
    )
    _append_lan_bind_addr(args, network_env)
    _append_optional_flag(args, clean_options.keep_artifacts, "--keep-artifacts")

    python = get_python_invocation()
    _run_live_native(
        layout,
        python.command(args),
        label="aMuTorrent clean startup",
        cwd=layout.emule_workspace_root,
        env=network_env,
    )


def invoke_amutorrent_resilience(
    layout: WorkspaceLayout,
    options: WorkspaceOptions,
    resilience_options: AmutorrentResilienceOptions,
) -> None:
    """Runs the automated aMuTorrent resilience live E2E proof."""

    _assert_test_execution_platform_supported(options)
    app_root = layout.get_app_variant(layout.test_targets.test_run_variant).path
    _ensure_hide_me_split_tunnel_for_live(
        test_network=resilience_options.test_network,
        p2p_bind_interface_name=resilience_options.p2p_bind_interface_name,
        app_exe=_resolve_live_e2e_app_exe(layout, app_root, None, options),
    )
    script_path = layout.tests_repo_root / "scripts" / "amutorrent-resilience-live.py"
    if not script_path.is_file():
        raise RuntimeError(f"Missing aMuTorrent resilience live runner: {script_path}")

    args: list[str | Path | float] = [
        script_path,
        "--app-root",
        app_root,
        "--configuration",
        options.configuration,
        "--p2p-bind-interface-name",
        resilience_options.p2p_bind_interface_name,
        "--rest-webserver-scheme",
        resilience_options.rest_webserver_scheme,
        "--ready-timeout-seconds",
        resilience_options.ready_timeout_seconds,
        "--network-ready-timeout-seconds",
        resilience_options.network_ready_timeout_seconds,
        "--search-observation-timeout-seconds",
        resilience_options.search_observation_timeout_seconds,
        "--reconnect-timeout-seconds",
        resilience_options.reconnect_timeout_seconds,
    ]
    if resilience_options.live_wire_inputs_file:
        args.extend(["--live-wire-inputs-file", _resolve_workspace_argument(layout, resilience_options.live_wire_inputs_file)])
    network_env = _test_network_env(
        layout,
        test_network=resilience_options.test_network,
        vpn_interface_name=resilience_options.p2p_bind_interface_name,
        require_vpn=True,
        require_lan=True,
    )
    _append_lan_bind_addr(args, network_env)
    _append_optional_flag(args, resilience_options.keep_artifacts, "--keep-artifacts")

    python = get_python_invocation()
    _run_live_native(
        layout,
        python.command(args),
        label="aMuTorrent resilience live",
        cwd=layout.emule_workspace_root,
        env=network_env,
    )


def invoke_amutorrent_emulebb_ui(
    layout: WorkspaceLayout,
    options: WorkspaceOptions,
    ui_options: AmutorrentEmulebbUiOptions,
) -> None:
    """Runs the automated aMuTorrent eMuleBB UI live E2E proof."""

    _assert_test_execution_platform_supported(options)
    app_root = layout.get_app_variant(layout.test_targets.test_run_variant).path
    _ensure_hide_me_split_tunnel_for_live(
        test_network=ui_options.test_network,
        p2p_bind_interface_name=ui_options.p2p_bind_interface_name,
        app_exe=_resolve_live_e2e_app_exe(layout, app_root, None, options),
    )
    script_path = layout.tests_repo_root / "scripts" / "amutorrent-emulebb-ui-live.py"
    if not script_path.is_file():
        raise RuntimeError(f"Missing aMuTorrent eMuleBB UI live runner: {script_path}")

    args: list[str | Path | float] = [
        script_path,
        "--app-root",
        app_root,
        "--configuration",
        options.configuration,
        "--p2p-bind-interface-name",
        ui_options.p2p_bind_interface_name,
        "--rest-webserver-scheme",
        ui_options.rest_webserver_scheme,
        "--ready-timeout-seconds",
        ui_options.ready_timeout_seconds,
        "--network-ready-timeout-seconds",
        ui_options.network_ready_timeout_seconds,
        "--search-observation-timeout-seconds",
        ui_options.search_observation_timeout_seconds,
    ]
    if ui_options.live_wire_inputs_file:
        args.extend(["--live-wire-inputs-file", _resolve_workspace_argument(layout, ui_options.live_wire_inputs_file)])
    network_env = _test_network_env(
        layout,
        test_network=ui_options.test_network,
        vpn_interface_name=ui_options.p2p_bind_interface_name,
        require_vpn=True,
        require_lan=True,
    )
    _append_lan_bind_addr(args, network_env)
    _append_optional_flag(args, ui_options.keep_artifacts, "--keep-artifacts")

    python = get_python_invocation()
    _run_live_native(
        layout,
        python.command(args),
        label="aMuTorrent eMuleBB UI live",
        cwd=layout.emule_workspace_root,
        env=network_env,
    )


def _append_optional_flag(args: list, enabled: bool, flag: str) -> None:
    if enabled:
        args.append(flag)


def _workspace_env(layout: WorkspaceLayout) -> dict[str, Path]:
    """Returns the mandatory root environment passed to child test scripts."""

    return layout.subprocess_environment()


def _append_lan_bind_addr(args: list[str | Path | float], env: dict[str, str]) -> None:
    lan_bind_address = _lan_bind_address_from_env(env)
    if lan_bind_address:
        args.extend(["--lan-bind-addr", lan_bind_address])


def _run_live_native(
    layout: WorkspaceLayout,
    command: Sequence[str | os.PathLike[str]],
    *,
    label: str,
    cwd: Path,
    env: dict[str, str],
) -> None:
    retried_after_upnp_restart = False
    while True:
        completed = run_native(command, label=label, cwd=cwd, env={**_workspace_env(layout), **env}, allow_failure=True)
        if int(getattr(completed, "returncode", 0) or 0) == 0:
            return

        failure_text = _recent_live_failure_text(layout)
        restart = (
            {"requested": False, "skipped": "UPnP recovery retry already used"}
            if retried_after_upnp_restart
            else restart_hide_me_after_upnp_failure_if_requested(failure_text)
        )
        if bool(restart.get("requested")) and not retried_after_upnp_restart:
            retried_after_upnp_restart = True
            continue
        raise RuntimeError(f"{label} failed with exit code {completed.returncode}.")


def _recent_live_failure_text(layout: WorkspaceLayout) -> str:
    candidates: list[Path] = []
    for root in (layout.output_reports_root, layout.output_artifacts_root):
        if root.is_dir():
            candidates.extend(path for path in root.rglob("*.json") if path.is_file())
            candidates.extend(path for path in root.rglob("emulebb*.log") if path.is_file())

    def sort_key(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    fragments: list[str] = []
    for path in sorted(candidates, key=sort_key, reverse=True)[:32]:
        try:
            fragments.append(path.read_text(encoding="utf-8", errors="replace")[:65536])
        except OSError:
            continue
    return "\n".join(fragments)


def _hide_me_registration_paths(app_exe: Path) -> list[Path]:
    return [app_exe]


def _ensure_hide_me_split_tunnel_for_live(
    *,
    test_network: TestNetwork,
    p2p_bind_interface_name: str,
    app_exe: Path,
) -> dict[str, object]:
    """Ensures hide.me sees the eMuleBB executable before public live tests start."""

    if test_network not in {"vpn", "all"}:
        return {"enabled": False, "reason": f"test_network={test_network} does not use VPN"}
    required = p2p_bind_interface_name.strip().casefold() == "hide.me"
    return ensure_split_tunnel_apps(_hide_me_registration_paths(app_exe), required=required)


def _live_e2e_materialize_test_install(live_options: LiveE2eOptions) -> bool:
    if live_options.plan_only:
        return False
    return live_options.materialize_test_install or live_options.profile in INSTALLER_BACKED_LIVE_E2E_PROFILES


def _live_e2e_requires_startup_diagnostics_workspace_build(live_options: LiveE2eOptions) -> bool:
    """Returns whether workspace-app live E2E needs the startup diagnostics compile flag."""

    return not live_options.plan_only and live_options.startup_trace_mode == "required"


def _live_e2e_effective_test_network(live_options: LiveE2eOptions) -> TestNetwork:
    if (
        live_options.profile in INSTALLER_BACKED_LIVE_E2E_PROFILES
        and live_options.test_network == "default"
        and not live_options.suites
    ):
        return "vpn"
    return live_options.test_network


def _pre_materialize_lan_bind_address(
    layout: WorkspaceLayout,
    live_options: LiveE2eOptions,
    *,
    materialize_test_install: bool,
    test_network: TestNetwork,
) -> str | None:
    """Resolves only the LAN controller bind address needed before materialization."""

    if not materialize_test_install:
        return None
    if test_network not in {"lan", "vpn", "all"}:
        return None
    context = resolve_workspace_network_context(
        workspace_root=layout.workspace_root,
        output_root=layout.output_build_root.parent,
        test_network="lan",
        require_lan=True,
    )
    return _lan_bind_address_from_env(context.env())


def _live_e2e_test_install_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-pid{os.getpid()}"


def _resolve_live_e2e_app_exe(
    layout: WorkspaceLayout,
    app_root: Path,
    app_exe: Path | None,
    options: WorkspaceOptions,
) -> Path:
    """Returns the eMuleBB executable path used by live E2E runners."""

    if app_exe is not None:
        return app_exe
    variant = next((candidate for candidate in layout.app_variants if candidate.path.resolve() == app_root.resolve()), None)
    if variant is None:
        raise RuntimeError(f"Cannot map app root to a configured variant: {app_root}")
    return app_build_binary_path(
        layout,
        variant.name,
        options.configuration,
        options.platform,
        executable_name=APP_EXE_NAME,
    )


def _resolve_live_e2e_client2_app_exe(
    layout: WorkspaceLayout,
    options: WorkspaceOptions,
    live_options: LiveE2eOptions,
) -> Path | None:
    """Returns the built client2 executable when staged under the output root."""

    if _live_e2e_prefers_main_client2(live_options):
        candidate = _resolve_live_e2e_main_client2_app_exe(layout, options, live_options)
        if candidate.is_file():
            return candidate

    try:
        variant = layout.get_app_variant("tracing-harness")
    except RuntimeError:
        return None
    for executable_name in TRACING_HARNESS_EXE_NAMES:
        candidate = app_build_binary_path(
            layout,
            variant.name,
            options.configuration,
            options.platform,
            executable_name=executable_name,
        )
        if candidate.is_file():
            return candidate
    return None


def _resolve_live_e2e_main_client2_app_exe(
    layout: WorkspaceLayout,
    options: WorkspaceOptions,
    live_options: LiveE2eOptions,
) -> Path:
    """Returns the native REST-capable main client preferred by deterministic transfer."""

    arch = "arm64" if options.platform == "ARM64" else "x64"
    package_candidate = (
        layout.output_packages_root
        / "build"
        / f"emulebb-v{live_options.materialize_test_install_release_version}"
        / arch
        / "standard"
        / "app"
        / APP_EXE_NAME
    )
    if package_candidate.is_file():
        return package_candidate
    return app_build_binary_path(
        layout,
        layout.test_targets.test_run_variant,
        options.configuration,
        options.platform,
        executable_name=APP_EXE_NAME,
    )


def _live_e2e_prefers_main_client2(options: LiveE2eOptions) -> bool:
    """Reports whether the selected suite needs the native REST-capable main client."""

    selected = set(options.suites)
    selected.update(options.campaign_scenario_local_suites)
    return any(suite_name in MAIN_CLIENT2_SUITE_NAMES for suite_name in selected)


def _resolve_workspace_argument(layout: WorkspaceLayout, value: str) -> str:
    """Resolves an existing workspace-relative operator input path."""

    path = Path(value)
    if path.is_absolute():
        return str(path)
    workspace_path = layout.emule_workspace_root / path
    if workspace_path.exists():
        return str(workspace_path.resolve())
    return value


def _resolve_workspace_path_argument(layout: WorkspaceLayout, value: str) -> str:
    """Resolves workspace-relative path knobs even when the target is created later."""

    expanded = os.path.expandvars(value)
    path = Path(expanded)
    if path.is_absolute():
        return str(path)
    return str((layout.emule_workspace_root / path).resolve())


def _test_network_env(
    layout: WorkspaceLayout,
    *,
    test_network: TestNetwork,
    vpn_interface_name: str | None = None,
    require_vpn: bool = False,
    require_lan: bool = False,
) -> dict[str, str]:
    """Returns the eMule workspace environment for one network-scoped child run."""

    context = resolve_workspace_network_context(
        workspace_root=layout.workspace_root,
        output_root=layout.output_build_root.parent,
        test_network=test_network,
        vpn_interface_name=vpn_interface_name,
        require_vpn=require_vpn,
        require_lan=require_lan,
    )
    context_env = context.env()
    lan_bind_address = _lan_bind_address_from_env(context_env)
    if lan_bind_address:
        context_env.setdefault("X_LOCAL_IP", lan_bind_address)
    return {**{key: str(value) for key, value in layout.subprocess_environment().items()}, **context_env}


def _lan_bind_address_from_env(env: dict[str, str]) -> str | None:
    """Returns the resolved LAN controller bind address propagated to child launchers."""

    return (env.get("X_LOCAL_IP") or os.environ.get("X_LOCAL_IP", "") or env.get(LAN_IP_RESOLVED_ENV) or "").strip() or None


def _assert_test_execution_platform_supported(options: WorkspaceOptions) -> None:
    if options.platform != "x64":
        raise RuntimeError("Test execution supports x64 only.")


def _assert_live_e2e_execution_platform_supported(options: WorkspaceOptions) -> None:
    if options.platform == "x64":
        return
    if options.platform == "ARM64" and _host_supports_arm64_test_execution():
        return
    raise RuntimeError("Live E2E ARM64 execution requires an ARM64 host.")


def _host_supports_arm64_test_execution() -> bool:
    values = (
        os.environ.get("RUNNER_ARCH", ""),
        os.environ.get("PROCESSOR_ARCHITECTURE", ""),
        os.environ.get("PROCESSOR_ARCHITEW6432", ""),
        platform.machine(),
    )
    return any(value.strip().upper() == "ARM64" for value in values)
