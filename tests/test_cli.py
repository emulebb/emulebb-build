from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from emule_workspace import cli


def test_cli_requires_workspace_root() -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli.main,
        ["env-check"],
        env={"EMULEBB_WORKSPACE_ROOT": "", "EMULEBB_WORKSPACE_OUTPUT_ROOT": ""},
    )

    assert result.exit_code != 0
    assert "EMULEBB_WORKSPACE_ROOT is required" in result.output


def test_cli_requires_workspace_output_root(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli.main,
        ["env-check"],
        env={"EMULEBB_WORKSPACE_ROOT": str(tmp_path), "EMULEBB_WORKSPACE_OUTPUT_ROOT": ""},
    )

    assert result.exit_code != 0
    assert "EMULEBB_WORKSPACE_OUTPUT_ROOT is required" in result.output


def test_build_tests_help_exposes_clean_architecture_command() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["build", "tests", "--help"])

    assert result.exit_code == 0
    assert "--test-run-variant" in result.output
    assert "Configured app variant key" in result.output
    assert "worktree" in result.output
    assert "folder" in result.output
    assert "name" in result.output
    assert "--build-output-mode" in result.output


def test_build_app_help_exposes_variant_selection() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["build", "app", "--help"])

    assert result.exit_code == 0
    assert "--variant" in result.output
    assert "Configured app variant key" in result.output
    assert "--clean" in result.output
    assert "--diagnostics" in result.output


def test_build_app_diagnostics_flag_is_forwarded(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        cli,
        "resolve_workspace_options",
        lambda **_kwargs: cli.WorkspaceOptions(workspace_root=tmp_path),
    )
    monkeypatch.setattr(cli, "load_layout", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "_locked", lambda _command_name, function: function)
    monkeypatch.setattr(
        cli,
        "invoke_build_apps",
        lambda _layout, _workspace_options, **kwargs: captured.update(kwargs),
    )

    result = runner.invoke(
        cli.main,
        [
            "build",
            "app",
            "--variant",
            "main",
            "--diagnostics",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "clean": False,
        "app_variant_names": ("main",),
        "enable_diagnostics": True,
    }


def test_analyze_diagnostic_logs_command_delegates_to_tests_repo(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    tests_repo_root = tmp_path / "repos" / "emulebb-build-tests"
    tests_repo_root.mkdir(parents=True)

    class FakePython:
        def command(self, args):
            return ["python", *[str(arg) for arg in args]]

    monkeypatch.setattr(
        cli,
        "resolve_workspace_options",
        lambda **_kwargs: cli.WorkspaceOptions(workspace_root=tmp_path),
    )
    monkeypatch.setattr(cli, "load_layout", lambda *_args, **_kwargs: type("Layout", (), {"tests_repo_root": tests_repo_root})())
    monkeypatch.setattr(cli, "_locked", lambda _command_name, function: function)
    monkeypatch.setattr(cli, "get_python_invocation", lambda: FakePython())
    monkeypatch.setattr(
        cli,
        "run_native",
        lambda command, *, label, cwd: captured.update(command=command, label=label, cwd=cwd),
    )

    result = runner.invoke(
        cli.main,
        [
            "analyze-diagnostic-logs",
            "--logs-dir",
            str(logs_dir),
            "--window-minutes",
            "7",
            "--top",
            "3",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["label"] == "diagnostic log analysis"
    assert captured["cwd"] == tests_repo_root
    command = captured["command"]
    assert command[:2] == ["python", str(tests_repo_root / "scripts" / "analyze-diagnostic-logs.py")]
    assert "--logs-dir" in command
    assert str(logs_dir) in command
    assert "--json" in command


def test_build_libs_help_exposes_clean_option() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["build", "libs", "--help"])

    assert result.exit_code == 0
    assert "--clean" in result.output


def test_python_test_help_exposes_pytest_options() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["test", "python", "--help"])

    assert result.exit_code == 0
    assert "--path" in result.output
    assert "--expression" in result.output


def test_test_live_e2e_help_exposes_live_options() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["test", "live-e2e", "--help"])

    assert result.exit_code == 0
    assert "--suite" in result.output
    assert "--profile" in result.output
    assert "multi-client-p2p" in result.output
    assert "multi-client-p2p-required" in result.output
    assert "controller-local" in result.output
    assert "release-expanded" in result.output
    assert "release-expanded-quick" in result.output
    assert "installer-controller-surface" in result.output
    assert "stabilization-stress" in result.output
    assert "stabilization-stress-quick" in result.output
    assert "cpu-heavy-quick" in result.output
    assert "ui-resource-depth" in result.output
    assert "diagnostics-soak" in result.output
    assert "--p2p-bind-interface-name" in result.output
    assert "--live-wire-inputs-file" in result.output
    assert "--materialize-test-install" in result.output
    assert "--materialize-test-install-release-version" in result.output
    assert "--materialize-test-install-clean" in result.output
    assert "--materialize-test-install-skip-build" in result.output
    assert "--plan-only" in result.output
    assert "--arr-download-proof-mode" in result.output
    assert "--pre-run-cleanup" in result.output
    assert "--skip-pre-run-cleanup" in result.output
    assert "--search-ui-search-rounds" in result.output
    assert "--search-ui-download-lifecycle-count" in result.output
    assert "--radarr-movie-root" in result.output
    assert "--sonarr-series-root" in result.output
    assert "--profile-cpu" in result.output
    assert "--profile-cpu-stack" in result.output
    assert "--profile-memory" in result.output
    assert "--profile-resource-interval-seconds" in result.output
    assert "--admin-volume-fixtures" in result.output
    assert "--vhd-size-mb" in result.output
    assert "--mount-root" in result.output
    assert "--keep-admin-fixtures" in result.output
    assert "--rest-cold-start-dump-stress-cpu-profile" in result.output
    assert "--rest-cold-start-dump-stress-cpu-profile-stack" in result.output


def test_amutorrent_session_help_exposes_backend_option() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["test", "amutorrent-session", "--help"])

    assert result.exit_code == 0
    assert "--backend" in result.output
    assert "native" in result.output
    assert "rust" in result.output


def test_live_e2e_command_defaults_model_options(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        cli,
        "resolve_workspace_options",
        lambda **_kwargs: cli.WorkspaceOptions(workspace_root=tmp_path),
    )
    monkeypatch.setattr(cli, "load_layout", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "_locked", lambda _command_name, function: function)
    monkeypatch.setattr(
        cli,
        "invoke_live_e2e_suite",
        lambda _layout, _workspace_options, options: captured.update(
            plan_only=options.plan_only,
            suites=options.suites,
            skip_live_seed_refresh=options.skip_live_seed_refresh,
        ),
    )

    result = runner.invoke(
        cli.main,
        [
            "test",
            "live-e2e",
            "--suite",
            "command-line-smoke",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "plan_only": False,
        "suites": ("command-line-smoke",),
        "skip_live_seed_refresh": False,
    }


def test_live_e2e_command_accepts_plan_only(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        cli,
        "resolve_workspace_options",
        lambda **_kwargs: cli.WorkspaceOptions(workspace_root=tmp_path),
    )
    monkeypatch.setattr(cli, "load_layout", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "_locked", lambda _command_name, function: function)
    monkeypatch.setattr(
        cli,
        "invoke_live_e2e_suite",
        lambda _layout, _workspace_options, options: captured.update(plan_only=options.plan_only),
    )

    result = runner.invoke(
        cli.main,
        [
            "test",
            "live-e2e",
            "--suite",
            "command-line-smoke",
            "--plan-only",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {"plan_only": True}


def test_test_windows_vm_help_exposes_vm_lab_options() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["test", "windows-vm", "--help"])

    assert result.exit_code == 0
    assert "--config-file" in result.output
    assert "--matrix" in result.output
    assert "--profile" in result.output
    assert "package-smoke" in result.output
    assert "local-ed2k-transfer" not in result.output
    assert "hideme-live-wire" not in result.output
    assert "--skip-build" in result.output
    assert "--keep-running" in result.output
    assert "--dry-run" in result.output
    assert "--local-swarm-mode" in result.output


def test_test_campaign_scenario_help_exposes_mode_options() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["test", "campaign-scenario", "--help"])

    assert result.exit_code == 0
    assert "--scenario" in result.output
    assert "--mode" in result.output
    assert "--release-version" in result.output
    assert "--skip-build" in result.output
    assert "--build" in result.output
    assert "--matrix" in result.output
    assert "--swarm-tier" in result.output
    assert "--local-swarm-mode" in result.output


def test_test_release_campaign_help_exposes_local_vm_swarm_mode() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["test", "release-campaign", "--help"])

    assert result.exit_code == 0
    assert "--local-vm-swarm-mode" in result.output
    assert "--local-vm-swarm-execution-mode" in result.output


def test_vm_lab_prepare_help_is_available() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["vm-lab", "prepare", "--help"])

    assert result.exit_code == 0
    assert "--config-file" in result.output
    assert "--matrix" in result.output
    assert "--rebuild-images" in result.output
    assert "--dry-run" in result.output


def test_vm_lab_audit_help_is_available() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["vm-lab", "audit", "--help"])

    assert result.exit_code == 0
    assert "--config-file" in result.output
    assert "--matrix" in result.output
    assert "--vm-name-pattern" in result.output


def test_vm_lab_manual_help_is_available() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["vm-lab", "manual", "--help"])

    assert result.exit_code == 0
    assert "--config-file" in result.output
    assert "--matrix" in result.output
    assert "--release-version" in result.output
    assert "--skip-build" in result.output
    assert "--install-root" in result.output
    assert "--start-app" in result.output
    assert "--dry-run" in result.output


def test_overnight_local_hammer_help_exposes_campaign_options() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["test", "overnight-local-hammer", "--help"])

    assert result.exit_code == 0
    assert "--until-local" in result.output
    assert "--live-wire-inputs-file" in result.output
    assert "--skip-build" in result.output
    assert "--godzilla-p2p-bind-interface-address" in result.output
    assert "--no-profile-symbols-required" in result.output


def test_test_certification_help_exposes_release_gate_options() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["test", "certification", "--help"])

    assert result.exit_code == 0
    assert "--profile" in result.output
    assert "overnight" in result.output
    assert "--live-wire-inputs-file" in result.output
    assert "--radarr-movie-root" in result.output
    assert "--sonarr-series-root" in result.output
    assert "--vpn-guard-live-config" in result.output
    assert "--p2p-bind-interface-name" in result.output
    assert "--continue-on-failure" in result.output
    assert "--skip-pre-run-cleanup" in result.output


def test_test_release_campaign_help_exposes_report_options() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["test", "release-campaign", "--help"])

    assert result.exit_code == 0
    assert "--campaign" in result.output
    assert "--phase" in result.output
    assert "--template" in result.output
    assert "--json" in result.output
    assert "--execute" in result.output
    assert "--include-nonblocking" in result.output
    assert "--continue-on-failure" in result.output
    assert "--dry-run" in result.output
    assert "--skip-pre-run-cleanup" in result.output
    assert "--live-wire-inputs-file" in result.output
    assert "--radarr-movie-root" in result.output
    assert "--sonarr-series-root" in result.output


def test_amutorrent_clean_startup_help_exposes_live_options() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["test", "amutorrent-clean-startup", "--help"])

    assert result.exit_code == 0
    assert "--live-wire-inputs-file" in result.output
    assert "--rest-webserver-scheme" in result.output
    assert "--p2p-bind-interface-name" in result.output
    assert "--search-observation-timeout-seconds" in result.output


def test_amutorrent_resilience_help_exposes_live_options() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["test", "amutorrent-resilience", "--help"])

    assert result.exit_code == 0
    assert "--live-wire-inputs-file" in result.output
    assert "--rest-webserver-scheme" in result.output
    assert "--p2p-bind-interface-name" in result.output
    assert "--search-observation-timeout-seconds" in result.output
    assert "--reconnect-timeout-seconds" in result.output


def test_amutorrent_emulebb_ui_help_exposes_live_options() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["test", "amutorrent-emulebb-ui", "--help"])

    assert result.exit_code == 0
    assert "--live-wire-inputs-file" in result.output
    assert "--rest-webserver-scheme" in result.output
    assert "--p2p-bind-interface-name" in result.output
    assert "--search-observation-timeout-seconds" in result.output
    assert "--keep-artifacts" in result.output


def test_build_all_help_exposes_composed_build_options() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["build", "all", "--help"])

    assert result.exit_code == 0
    assert "--variant" in result.output
    assert "--test-run-variant" in result.output
    assert "Configured app variant key" in result.output


def test_dep_status_help_is_available() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["dep-status", "--help"])

    assert result.exit_code == 0
    assert "Report dependency" in result.output


def test_workspace_status_help_is_available() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["workspace-status", "--help"])

    assert result.exit_code == 0
    assert "dirty-state" in result.output


def test_validate_help_exposes_product_family_option() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["validate", "--help"])

    assert result.exit_code == 0
    assert "--include-product-family" in result.output
    assert "--product-family-tier" in result.output


def test_prepare_product_family_help_is_available() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["prepare-product-family", "--help"])

    assert result.exit_code == 0
    assert "product-family" in result.output


def test_refresh_product_family_rebases_help_is_available() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["refresh-product-family-rebases", "--help"])

    assert result.exit_code == 0
    assert "Refresh manual aMule and aMuTorrent checkouts" in result.output


def test_install_local_package_help_exposes_live_wire_config() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["install-local-package", "--help"])

    assert result.exit_code == 0
    assert "--live-wire-inputs-file" in result.output
    assert "--release-version" in result.output
    assert "--skip-build" in result.output


def test_product_family_toolchain_help_is_available() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["product-family-toolchain", "--help"])

    assert result.exit_code == 0
    assert "--strict" in result.output


def test_evidence_index_help_is_available() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["evidence-index", "--help"])

    assert result.exit_code == 0
    assert "--threshold-mb" in result.output
    assert "--no-write" in result.output


def test_cleanup_help_exposes_retention_toggles() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["cleanup", "--help"])

    assert result.exit_code == 0
    assert "--report-run-retention-days" in result.output
    assert "--skip-profiling-artifacts" in result.output
    assert "--skip-legacy-test-reports" in result.output


def test_package_release_help_exposes_release_version() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["package-release", "--help"])

    assert result.exit_code == 0
    assert "--release-version" in result.output
    assert "--require-signing" in result.output
    assert "0.7.3" in result.output


def test_package_amutorrent_help_exposes_release_version() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["package-amutorrent", "--help"])

    assert result.exit_code == 0
    assert "--release-version" in result.output
    assert "0.7.3" in result.output


def test_package_amule_help_exposes_release_version() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["package-amule", "--help"])

    assert result.exit_code == 0
    assert "--release-version" in result.output
    assert "3.0.0-emulebb.1" in result.output


def test_package_miniupnpc_help_exposes_release_version() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["package-miniupnpc", "--help"])

    assert result.exit_code == 0
    assert "--release-version" in result.output
    assert "2.3.3-emulebb.1" in result.output


def test_materialize_help_exposes_bootstrap_options() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["materialize", "--help"])

    assert result.exit_code == 0
    assert "--artifacts-seed-root" in result.output
    assert "--include-analysis" in result.output


def test_sync_help_exposes_bootstrap_options() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["sync", "--help"])

    assert result.exit_code == 0
    assert "--artifacts-seed-root" in result.output
    assert "--include-analysis" in result.output
    assert "--workspace-root" not in result.output


def test_setup_status_help_is_available() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["status", "--help"])

    assert result.exit_code == 0
    assert "setup-managed" in result.output


def test_dep_updates_help_is_available() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["dep-updates", "--help"])

    assert result.exit_code == 0
    assert "third-party" in result.output


def test_compare_help_is_available() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.main, ["compare", "--help"])

    assert result.exit_code == 0
    assert "WinMerge" in result.output


def test_sync_rejects_workspace_root_outside_current_build_clone(tmp_path: Path) -> None:
    runner = CliRunner()
    output_root = tmp_path.parent / "emulebb-output"
    output_root.mkdir(parents=True)

    result = runner.invoke(
        cli.main,
        ["sync"],
        env={
            "EMULEBB_WORKSPACE_ROOT": str(tmp_path),
            "EMULEBB_WORKSPACE_OUTPUT_ROOT": str(output_root),
        },
    )

    assert result.exit_code != 0
    assert "repos\\emulebb-build" in result.output


def test_rust_network_proof_help_exposes_consumer_live_lane() -> None:
    result = CliRunner().invoke(cli.main, ["test", "rust-network-proof", "--help"])

    assert result.exit_code == 0
    assert "windows-consumer" in result.output
    assert "--release-zip" in result.output
    assert "--search-term" in result.output
    assert "--max-transfer-bytes" in result.output
    assert "--replace-running-profile-settings" in result.output
    assert "--restore-operator-executable" in result.output


def test_rust_webui_help_exposes_stateful_playwright_gate() -> None:
    result = CliRunner().invoke(cli.main, ["test", "rust-webui", "--help"])

    assert result.exit_code == 0
    assert "stateful Playwright" in result.output
