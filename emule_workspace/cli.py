"""Click command surface for eMule workspace orchestration."""

from __future__ import annotations

import os
import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar

import click

from .build_tests import invoke_build_tests
from .artifact_audit import audit_workspace_artifacts, print_artifact_audit
from .build import build_apps as invoke_build_apps
from .build import build_clients as invoke_build_clients
from .build import build_ed2k_server as invoke_build_ed2k_server
from .build import build_libs as invoke_build_libs
from .certification import invoke_certification
from .cleanup import cleanup_workspace
from .config import (
    ACTIVE_EMULEBB_RELEASE_VERSION,
    BROAD_LIVE_E2E_PRE_RUN_CLEANUP_PROFILES,
    BuildClientsOptions,
    BuildTestsOptions,
    CampaignScenarioOptions,
    CertificationOptions,
    CleanupOptions,
    CommunityCoverageOptions,
    EmulebbRustPackageOptions,
    FakeKadTrustSoakOptions,
    LiveE2eOptions,
    LocalHammerCampaignOptions,
    LocalPackageInstallOptions,
    MiniupnpcPackageOptions,
    PythonTestOptions,
    ReleasePackageOptions,
    ReleaseCampaignOptions,
    VariantComparisonOptions,
    WorkspaceOptions,
    resolve_required_workspace_roots,
    resolve_workspace_options,
)
from .campaign_scenario_runner import invoke_campaign_scenario
from .evidence import build_heavy_evidence_index, print_heavy_evidence_index, write_heavy_evidence_index
from .config import WORKSPACE_OUTPUT_ROOT_ENV
from .layout import build_ci_layout, load_layout
from .local_hammer_campaign import invoke_local_hammer_campaign
from .local_package_install import install_local_package
from .locks import WORKSPACE_LOCK_TOKEN_ENV, WorkspaceLock
from .materialize import materialize_workspace, sync_workspace
from .miniupnpc_release import create_miniupnpc_package
from .product_family import audit_product_family_toolchain, prepare_product_family_repos, print_product_family_toolchain
from .process import get_python_invocation, run_native
from .python_tests import invoke_python_tests
from .release import (
    assemble_emulebb_rust_release_assets,
    build_emulebb_rust_image_ci,
    build_emulebb_rust_image_local,
    create_emulebb_rust_package,
    create_release_package,
)
from .release_campaign_runner import invoke_release_campaign
from .setup_commands import run_compare, write_dependency_update_report, write_materialization_status
from .status import write_dependency_status, write_workspace_repo_status, write_workspace_summary
from .test_runs import (
    invoke_builder_unit_tests,
    invoke_community_core_coverage,
    invoke_fake_kad_trust_soak,
    invoke_rust_network_proof,
    invoke_rust_rest_openapi,
    invoke_rust_release_package,
    invoke_rust_unit_tests,
    invoke_rust_webui_tests,
    invoke_live_diff_runs,
    invoke_live_e2e_suite,
    invoke_native_test_suites,
    invoke_protocol_parity,
    invoke_test_runs,
)
from .topology import OPTIONAL_WORKSPACE_PROFILES
from .workstreams import REPOSITORY_FIELDS, OPTIONAL_REPOSITORY_FIELDS
from .validation import validate_workspace
from .windows_vm_lab import (
    VmManualOptions,
    VmPrepareOptions,
    WindowsVmTestOptions,
    audit_vm_lab,
    invoke_windows_vm_tests,
    launch_manual_vm,
    parse_matrix,
    prepare_vm_lab,
)

F = TypeVar("F", bound=Callable[..., Any])

APP_VARIANT_HELP = "Configured app variant key (for example: main, community). This is not the worktree folder name."
APP_VARIANT_BUILD_HELP = f"{APP_VARIANT_HELP} Defaults to all variants, or main when --diagnostics is set."
TEST_RUN_VARIANT_HELP = f"{APP_VARIANT_HELP} Defaults to the workspace test-run variant."
BASELINE_VARIANT_HELP = f"{APP_VARIANT_HELP} Defaults to the workspace baseline variant."


def _common_options(function: F) -> F:
    @click.option("--workstream", default=None, help="Registered parent-coordinated source workstream.")
    @click.option("--workspace-name", default=None, help="Workspace name. Defaults to build manifest value.")
    @click.option("--config", "configuration", type=click.Choice(["Debug", "Release"]), default="Release", show_default=True)
    @click.option("--platform", type=click.Choice(["x64", "ARM64"]), default="x64", show_default=True)
    @click.option(
        "--build-output-mode",
        type=click.Choice(["Full", "Warnings", "ErrorsOnly"]),
        default="ErrorsOnly",
        show_default=True,
    )
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        workstream_id = kwargs.pop("workstream", None)
        try:
            workspace_options = resolve_workspace_options(
                workspace_name=kwargs.pop("workspace_name"),
                configuration=kwargs.pop("configuration"),
                platform=kwargs.pop("platform"),
                build_output_mode=kwargs.pop("build_output_mode"),
            )
            layout = load_layout(
                workspace_options.workspace_root,
                workspace_options.workspace_name,
                output_root=workspace_options.output_root,
            )
            from .workstreams import apply_workstream_layout, resolve_workstream

            registration = None
            if workstream_id or Path.cwd().resolve().is_relative_to(
                workspace_options.workspace_root / "workspaces" / "agents"
            ):
                registration = resolve_workstream(
                    workspace_options.workspace_root, workspace_options.output_root,
                    repo=None if workstream_id else Path.cwd(), workstream_id=workstream_id,
                )
            if registration is not None:
                layout = apply_workstream_layout(layout, registration)
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc
        if function.__name__ == "local_check":
            kwargs["workstream"] = workstream_id
        return function(*args, workspace_options=workspace_options, layout=layout, **kwargs)

    return wrapper  # type: ignore[return-value]


def _locked(command_name: str, function: F) -> F:
    @wraps(function)
    def wrapper(*args: Any, workspace_options: WorkspaceOptions, layout, **kwargs: Any) -> Any:
        lock = WorkspaceLock(layout=layout, command=command_name, options=workspace_options)
        if not lock.acquire():
            raise click.ClickException(
                f"Workspace busy: command '{command_name}' cannot start for "
                f"{layout.emule_workspace_root}. Active owner: {lock.active_owner_text()}."
            )
        previous_lock_token = os.environ.get(WORKSPACE_LOCK_TOKEN_ENV)
        if lock.delegation_token:
            os.environ[WORKSPACE_LOCK_TOKEN_ENV] = lock.delegation_token
        try:
            try:
                return function(*args, workspace_options=workspace_options, layout=layout, **kwargs)
            except click.ClickException:
                raise
            except Exception as exc:
                raise click.ClickException(str(exc)) from exc
        finally:
            if previous_lock_token is None:
                os.environ.pop(WORKSPACE_LOCK_TOKEN_ENV, None)
            else:
                os.environ[WORKSPACE_LOCK_TOKEN_ENV] = previous_lock_token
            lock.release()

    return wrapper  # type: ignore[return-value]


def _comparison_options(function: F) -> F:
    @click.option("--test-run-variant", default=None, help=TEST_RUN_VARIANT_HELP)
    @click.option("--baseline-variant", default=None, help=BASELINE_VARIANT_HELP)
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        comparison_options = VariantComparisonOptions(
            test_run_variant=kwargs.pop("test_run_variant"),
            baseline_variant=kwargs.pop("baseline_variant"),
        )
        return function(*args, comparison_options=comparison_options, **kwargs)

    return wrapper  # type: ignore[return-value]


def _live_e2e_options(function: F) -> F:
    @click.option("--suite", "suites", multiple=True, help="Live E2E suite to run.")
    @click.option(
        "--test-network",
        type=click.Choice(["default", "offline", "lan", "vpn", "all"]),
        default="default",
        show_default=True,
        help="Network scope for live E2E suite selection.",
    )
    @click.option(
        "--profile",
        type=click.Choice([
            "default",
            "multi-client-p2p",
            "multi-client-p2p-required",
            "protocol-parity",
            "beta-green",
            "controller-surface",
            "installer-controller-surface",
            "installer-controller-surface-soak",
            "controller-local",
            "package-helpers",
            "beta-release",
            "release-expanded",
            "release-expanded-quick",
            "stabilization-stress",
            "stabilization-stress-quick",
            "cpu-heavy",
            "cpu-heavy-quick",
            "ui-resource-depth",
            "diagnostics-soak",
        ]),
        default="default",
        show_default=True,
        help="Named live E2E suite profile.",
    )
    @click.option("--fail-fast", is_flag=True, help="Stop the live E2E suite after the first failure.")
    @click.option("--skip-live-seed-refresh", is_flag=True, help="Reuse the existing live seed state.")
    @click.option("--startup-trace-mode", type=click.Choice(["required", "optional"]), default="required", show_default=True)
    @click.option("--shared-root", default=None, help="Shared file root for live UI scenarios.")
    @click.option("--admin-volume-fixtures", is_flag=True, help="Enable admin-only VHD drive-letter and folder-mount live fixtures.")
    @click.option("--vhd-size-mb", default=256, show_default=True, type=int)
    @click.option("--mount-root", default=None, help="Parent directory for admin-only folder mount fixtures.")
    @click.option("--keep-admin-fixtures", is_flag=True, help="Leave admin VHD fixture files after a live suite for inspection.")
    @click.option("--dependency-mode", type=click.Choice(["cache-only", "auto-download", "off"]), default="cache-only", show_default=True)
    @click.option("--dependency-channel", type=click.Choice(["pinned", "latest"]), default="pinned", show_default=True)
    @click.option("--dependency-cache-root", default=None, help="Workspace-owned cache root for portable live-test dependencies.")
    @click.option("--refresh-dependencies", is_flag=True, help="Refresh cached portable live-test dependencies before running selected suites.")
    @click.option("--prowlarr-exe", default=None, help="Explicit Prowlarr executable for package-helper live tests.")
    @click.option("--radarr-exe", default=None, help="Explicit Radarr executable for package-helper live tests.")
    @click.option("--sonarr-exe", default=None, help="Explicit Sonarr executable for package-helper live tests.")
    @click.option("--ed2k-server-exe", default=None, help="Explicit Rust ED2K server executable for live tests.")
    @click.option("--preference-ui-directories-tree-stress", is_flag=True, help="Exercise the Preferences Directories tree with a large shared-directory profile.")
    @click.option("--shared-files-ui-scenario", "shared_files_ui_scenarios", multiple=True)
    @click.option("--shared-files-tree-stress-churn-cycles", default=-1, show_default=True, type=int)
    @click.option("--profile-cpu", is_flag=True, help="Capture bounded ETW CPU profiles for suites selected by broad diagnostics.")
    @click.option("--profile-cpu-max-file-mb", default=512, show_default=True, type=int)
    @click.option("--profile-cpu-stack", is_flag=True, help="Export stack-attributed CPU profile summaries when CPU profiling is enabled.")
    @click.option("--profile-cpu-stack-min-hits", default=10, show_default=True, type=int)
    @click.option("--profile-symbols-required/--no-profile-symbols-required", default=True, show_default=True)
    @click.option("--profile-memory", is_flag=True, help="Surface child suite resource monitor and memory delta diagnostics in aggregate reports.")
    @click.option("--profile-resource-interval-seconds", default=2.0, show_default=True, type=float)
    @click.option("--live-wire-inputs-file", default=None, help="Runtime live-wire search input JSON.")
    @click.option("--materialize-test-install", is_flag=True, help="Materialize an installer-backed isolated test install before live E2E.")
    @click.option("--materialize-test-install-release-version", default=ACTIVE_EMULEBB_RELEASE_VERSION, show_default=True)
    @click.option("--materialize-test-install-clean", is_flag=True, help="Clean package outputs before materializing the test install.")
    @click.option("--materialize-test-install-skip-build", is_flag=True, help="Reuse existing package artifacts when materializing the test install.")
    @click.option("--live-process-monitor-profile-dir", default=None, help="Existing profile directory for the live-process-monitor suite.")
    @click.option("--radarr-movie-root", default=None, help="Radarr-visible movie root for Radarr import live checks.")
    @click.option("--sonarr-series-root", default=None, help="Sonarr-visible series root for Sonarr import live checks.")
    @click.option("--acquisition-timeout-minutes", default=None, type=float, help="Arr acquisition timeout forwarded to live suites.")
    @click.option("--arr-download-proof-mode", type=click.Choice(["complete", "handoff"]), default="complete", show_default=True)
    @click.option("--p2p-bind-interface-name", default="hide.me", show_default=True)
    @click.option("--rest-server-search-count", default=6, show_default=True, type=int)
    @click.option("--rest-kad-search-count", default=6, show_default=True, type=int)
    @click.option("--rest-download-trigger-count", default=1, show_default=True, type=int)
    @click.option("--rest-search-method-override", type=click.Choice(["", "automatic", "server", "global", "kad"]), default="")
    @click.option("--rest-webserver-scheme", type=click.Choice(["http", "https"]), default="https", show_default=True)
    @click.option("--local-kad-bootstrap-mode", type=click.Choice(["rest", "preseed", "both"]), default="rest", show_default=True)
    @click.option("--local-kad-nodes-dat-fixture-mode", type=click.Choice(["valid", "truncated", "stale"]), default="valid", show_default=True)
    @click.option("--rest-coverage-budget", type=click.Choice(["smoke", "contract", "contract-stress"]), default="contract")
    @click.option("--rest-stress-budget", type=click.Choice(["off", "smoke", "soak"]), default="smoke")
    @click.option("--rest-stress-duration-seconds", default=30.0, show_default=True, type=float)
    @click.option("--rest-stress-concurrency", default=4, show_default=True, type=int)
    @click.option("--rest-stress-max-failures", default=1, show_default=True, type=int)
    @click.option("--rest-stress-request-timeout-seconds", default=5.0, show_default=True, type=float)
    @click.option("--rest-socket-adversity-budget", type=click.Choice(["off", "smoke"]), default="off")
    @click.option("--rest-tls-handshake-adversity-budget", type=click.Choice(["off", "smoke"]), default="off")
    @click.option("--rest-leak-churn-budget", type=click.Choice(["off", "smoke", "soak"]), default="off")
    @click.option("--rest-leak-churn-cycles", default=-1, show_default=True, type=int)
    @click.option("--rest-stop-start-after-churn", is_flag=True)
    @click.option("--vpn-guard-live-config", default=None)
    @click.option("--vpn-guard-allowed-public-ip-cidrs", default="")
    @click.option("--vpn-guard-scenario", type=click.Choice(["off", "success", "not-allowlisted", "vpn-off"]), default="success")
    @click.option("--vpn-guard-expected-startup-block", is_flag=True)
    @click.option("--multi-client-require-optional-clients", is_flag=True)
    @click.option("--search-ui-search-rounds", default=1, show_default=True, type=int)
    @click.option("--search-ui-download-lifecycle-count", default=1, show_default=True, type=int)
    @click.option("--godzilla-visible-ui", is_flag=True, help="Run the Godzilla local swarm with visible eMuleBB windows.")
    @click.option("--godzilla-p2p-bind-interface-address", default=None, help="Explicit LAN IPv4 address forwarded to the Godzilla local swarm.")
    @click.option("--godzilla-cpu-profile", is_flag=True, help="Capture a bounded ETW CPU profile for the Godzilla local swarm.")
    @click.option(
        "--godzilla-stage",
        type=click.Choice(["full", "launch-scale"]),
        default=None,
        help="Godzilla campaign boundary to run through.",
    )
    @click.option("--godzilla-total-client-count", default=30, show_default=True, type=int)
    @click.option("--godzilla-peer-transfer-count", default=300, show_default=True, type=int)
    @click.option("--godzilla-harness-transfer-count", default=300, show_default=True, type=int)
    @click.option("--godzilla-emulebb-files", default=600, show_default=True, type=int)
    @click.option("--godzilla-extra-emulebb-files", default=50, show_default=True, type=int)
    @click.option("--godzilla-harness-files", default=400, show_default=True, type=int)
    @click.option("--godzilla-adverse-kill-cycles", default=2, show_default=True, type=int)
    @click.option("--godzilla-adverse-kill-warmup-seconds", default=45.0, show_default=True, type=float)
    @click.option("--godzilla-adverse-recovery-timeout-seconds", default=300.0, show_default=True, type=float)
    @click.option(
        "--godzilla-vhd-runtime-root",
        type=click.Choice(["drive-letter"]),
        default="drive-letter",
        show_default=True,
        help="Runtime root used by Godzilla VHD fixtures; mixed aMule/MFC-oracle runs must stay on the short VHD drive-letter root.",
    )
    @click.option("--rest-cold-start-dump-stress-waves", default=4, show_default=True, type=int)
    @click.option("--rest-cold-start-dump-stress-searches-per-wave", default=12, show_default=True, type=int)
    @click.option("--rest-cold-start-dump-stress-max-concurrent-searches", default=8, show_default=True, type=int)
    @click.option("--rest-cold-start-dump-stress-search-observation-timeout-seconds", default=60.0, show_default=True, type=float)
    @click.option("--rest-cold-start-dump-stress-downloads-per-wave", default=600, show_default=True, type=int)
    @click.option("--rest-cold-start-dump-stress-downloads-per-search", default=50, show_default=True, type=int)
    @click.option("--rest-cold-start-dump-stress-max-missing-download-triggers", default=0, show_default=True, type=int)
    @click.option("--rest-cold-start-dump-stress-synthetic-queue-fill-count", default=0, show_default=True, type=int)
    @click.option("--rest-cold-start-dump-stress-synthetic-queue-fill-size-bytes", default=1024 * 1024, show_default=True, type=int)
    @click.option("--rest-cold-start-dump-stress-synthetic-queue-fill-batch-size", default=50, show_default=True, type=int)
    @click.option("--rest-cold-start-dump-stress-target-completed-downloads", default=0, show_default=True, type=int)
    @click.option("--rest-cold-start-dump-stress-completion-timeout-seconds", default=1800.0, show_default=True, type=float)
    @click.option("--rest-cold-start-dump-stress-max-active-downloads", default=512, show_default=True, type=int)
    @click.option("--rest-cold-start-dump-stress-allow-required-zero-result-searches", is_flag=True)
    @click.option("--rest-cold-start-dump-stress-skip-transfer-cleanup", is_flag=True)
    @click.option("--rest-cold-start-dump-stress-download-churn-interval-seconds", default=0.0, show_default=True, type=float)
    @click.option("--rest-cold-start-dump-stress-download-remove-count-per-churn", default=0, show_default=True, type=int)
    @click.option("--rest-cold-start-dump-stress-resource-monitor-interval-seconds", default=5.0, show_default=True, type=float)
    @click.option("--rest-cold-start-dump-stress-post-drain-seconds", default=30.0, show_default=True, type=float)
    @click.option("--rest-cold-start-dump-stress-tool-timeout-seconds", default=60.0, show_default=True, type=float)
    @click.option("--rest-cold-start-dump-stress-enable-umdh", is_flag=True)
    @click.option("--rest-cold-start-dump-stress-skip-umdh-diffs", is_flag=True)
    @click.option("--rest-cold-start-dump-stress-cpu-profile", is_flag=True)
    @click.option("--rest-cold-start-dump-stress-cpu-profile-max-file-mb", default=512, show_default=True, type=int)
    @click.option("--rest-cold-start-dump-stress-cpu-profile-stack", is_flag=True)
    @click.option("--rest-cold-start-dump-stress-cpu-profile-stack-min-hits", default=10, show_default=True, type=int)
    @click.option(
        "--rest-cold-start-dump-stress-cpu-profile-symbols-required/--no-rest-cold-start-dump-stress-cpu-profile-symbols-required",
        default=True,
        show_default=True,
    )
    @click.option(
        "--pre-run-cleanup/--skip-pre-run-cleanup",
        default=None,
        help="Prune old generated outcomes before broad live E2E profiles.",
    )
    @click.option("--plan-only", is_flag=True, help="Plan live E2E child commands without executing them.")
    @click.option("--rest-cold-start-dump-stress-skip-dumps", is_flag=True)
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        defaults = LiveE2eOptions()
        pre_run_cleanup = kwargs.pop("pre_run_cleanup", None)
        context = click.get_current_context(silent=True)
        arr_download_proof_mode_explicit = (
            context is not None
            and context.get_parameter_source("arr_download_proof_mode") == click.core.ParameterSource.COMMANDLINE
        )
        values = {
            key: kwargs.pop(key, getattr(defaults, key))
            for key in LiveE2eOptions.model_fields
            if key != "pre_run_cleanup"
        }
        values["arr_download_proof_mode_explicit"] = arr_download_proof_mode_explicit
        if pre_run_cleanup is None:
            pre_run_cleanup = values["profile"] in BROAD_LIVE_E2E_PRE_RUN_CLEANUP_PROFILES
        live_options = LiveE2eOptions(**values, pre_run_cleanup=pre_run_cleanup)
        return function(*args, live_options=live_options, **kwargs)

    return wrapper  # type: ignore[return-value]


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def main() -> None:
    """Build, validate, test, and package an eMuleBB workspace."""


@main.command("provision-ubuntu-dependencies-ci")
@click.option("--profile", type=click.Choice(["rust-native-release", "rust-prepared-builder"]), required=True)
@click.option("--deadline-seconds", type=click.IntRange(10, 900), default=300, show_default=True)
@click.option("--network-timeout-seconds", type=click.IntRange(1, 60), default=20, show_default=True)
@click.option("--attempts", type=click.IntRange(1, 5), default=3, show_default=True)
@click.option("--report", type=click.Path(path_type=Path), default=None)
def provision_ubuntu_dependencies_ci(profile, deadline_seconds, network_timeout_seconds, attempts, report):
    """Provision signed Ubuntu dependencies before workspace materialization."""
    import platform
    from .ubuntu_ci import ProvisioningError, provision_dependencies, write_receipt

    if os.environ.get("GITHUB_ACTIONS") != "true" or platform.system() != "Linux":
        raise click.ClickException("Ubuntu CI provisioning requires GitHub Actions on Linux")
    scratch = Path(os.environ.get("RUNNER_TEMP", ""))
    source = Path(__file__).resolve().parents[1]
    if (not scratch.is_absolute() or not scratch.is_dir() or scratch.resolve().is_relative_to(source)
            or (report is not None and (not report.is_absolute() or report.resolve().is_relative_to(source)))):
        raise click.ClickException("Provisioning scratch/report paths must be absolute and outside the checkout")
    try:
        receipt = provision_dependencies(profile=profile, deadline_seconds=deadline_seconds,
                                         network_timeout_seconds=network_timeout_seconds,
                                         attempts=attempts, scratch_root=scratch)
    except ProvisioningError as error:
        click.echo(write_receipt(error.receipt, report))
        raise click.ClickException(str(error)) from error
    click.echo(write_receipt(receipt, report))


@main.group("workstream")
def workstream() -> None:
    """Manage parent-assigned source checkouts; shared validation remains serialized."""


def _print_workstream(value: Any) -> None:
    import json
    click.echo(json.dumps(value, indent=2))


@workstream.command("create")
@click.argument("workstream_id")
@click.option("--branch", required=True)
@click.option("--owner", required=True, help="Accountable parent/coordinator identifier.")
@click.option("--repo", "repositories", multiple=True, required=True,
              type=click.Choice(tuple(REPOSITORY_FIELDS)))
@click.option("--issue", "issues", multiple=True)
@click.option("--pull-request", "pull_requests", multiple=True)
@_common_options
def workstream_create(*, workstream_id, branch, owner, repositories, issues, pull_requests,
                      workspace_options, layout) -> None:
    """Create unrelated worktrees from origin's advertised main tip during builds."""
    from .workstreams import create_workstream
    try:
        _print_workstream(create_workstream(layout, workstream_id, branch, owner,
                                          repositories, issues=issues, pull_requests=pull_requests))
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@workstream.command("add-repo")
@click.argument("workstream_id")
@click.option("--repo", required=True,
              type=click.Choice(tuple(REPOSITORY_FIELDS)))
@_common_options
def workstream_add_repo(*, workstream_id, repo, workspace_options, layout) -> None:
    """Add one previously unassigned repository to a workstream."""
    from .workstreams import add_repository
    def execute(**kwargs):
        _print_workstream(add_repository(kwargs["layout"], workstream_id, repo))
    _locked("workstream add-repo", execute)(workspace_options=workspace_options, layout=layout)


@workstream.command("status")
@click.argument("workstream_id", required=False)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable registration status.")
@_common_options
def workstream_status_command(*, workstream_id, as_json, workspace_options, layout) -> None:
    """Inspect registrations without changing checkouts or acquiring the build lock."""
    from .workstreams import workstream_status
    statuses = workstream_status(layout, workstream_id)
    if as_json:
        _print_workstream(statuses)
    else:
        for status in statuses:
            click.echo(f"{status['id']}: {status['verification']}" +
                       (f" ({status['error']})" if "error" in status else ""))


@workstream.command("remove")
@click.argument("workstream_id")
@click.option("--owner", required=True, help="Assigned parent owner; workers must not invoke removal.")
@click.option("--abandoned", is_flag=True,
              help="Parent withdraws work with no open PR, archiving exact heads in local recovery refs before removal.")
@_common_options
def workstream_remove(*, workstream_id, owner, abandoned, workspace_options, layout) -> None:
    """Remove safe worktrees; retained local branches preserve their history."""
    from .workstreams import remove_workstream
    def execute(**kwargs):
        remove_workstream(kwargs["layout"], workstream_id, owner, abandoned=abandoned)
        click.echo(f"Removed workstream {workstream_id}; local branches retained.")
    _locked("workstream remove", execute)(workspace_options=workspace_options, layout=layout)


@workstream.command("remove-repo")
@click.argument("workstream_id")
@click.option("--repo", required=True, type=click.Choice(tuple(REPOSITORY_FIELDS)))
@click.option("--owner", required=True, help="Assigned parent owner; workers must not invoke removal.")
@_common_options
def workstream_remove_repo(*, workstream_id, repo, owner, workspace_options, layout) -> None:
    """Retire one integrated companion; repeat outside the workstream to resume."""
    from .workstreams import remove_repository
    def execute(**kwargs):
        _print_workstream(remove_repository(kwargs["layout"], workstream_id, repo, owner))
    _locked("workstream remove-repo", execute)(workspace_options=workspace_options, layout=layout)


@main.command("bootstrap-python")
@click.option("--update-lock", is_flag=True, help="Explicitly regenerate the reviewed dependency lock")
@click.option("--reinstall", is_flag=True, help="Reinstall only the private managed interpreter")
def bootstrap_python(*, update_lock: bool, reinstall: bool) -> None:
    """Install the workspace-only Python runtime and locked development environment."""
    from .python_runtime import bootstrap

    try:
        bootstrap(update_lock=update_lock, reinstall=reinstall)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        raise click.ClickException(str(error)) from error


@main.command("fetch-rust-android-tools")
@_common_options
def fetch_rust_android_tools(*, workspace_options: WorkspaceOptions, layout) -> None:
    """Download verified Android build inputs for Linux/WSL; do not install or execute them."""
    from .android_toolchain import fetch_tool_inputs

    def fetch(**kwargs: Any) -> None:
        print(f"Verified Android input cache: {fetch_tool_inputs()}")

    _locked("fetch-rust-android-tools", fetch)(workspace_options=workspace_options, layout=layout)


@main.command("fetch-rust-android-emulator-tools")
@_common_options
@click.option("--image", "images", type=click.Choice(["min", "current"]), multiple=True,
              help="Also fetch this pinned system image; default: emulator and ADB only.")
def fetch_rust_android_emulator_tools(*, images: tuple[str, ...], workspace_options: WorkspaceOptions, layout) -> None:
    """Cache pinned emulator inputs without executing tools or accepting licenses."""
    from .android_toolchain import fetch_emulator_inputs

    def fetch(**kwargs: Any) -> None:
        print(f"Verified Android emulator input cache: {fetch_emulator_inputs(images=images)}")

    _locked("fetch-rust-android-emulator-tools", fetch)(workspace_options=workspace_options, layout=layout)


@main.command("prepare-rust-android-toolchain")
@_common_options
@click.option("--wsl-distro", default="Ubuntu", show_default=True)
def prepare_rust_android_toolchain(*, wsl_distro: str, workspace_options: WorkspaceOptions, layout) -> None:
    """Build and probe the pinned Android toolchain through native Linux/WSL Docker."""
    from .android_toolchain import prepare_toolchain

    def prepare(**kwargs: Any) -> None:
        print(f"Android toolchain probe passed: {prepare_toolchain(kwargs['layout'], wsl_distro=wsl_distro)}")

    _locked("prepare-rust-android-toolchain", prepare)(workspace_options=workspace_options, layout=layout)


@main.command("prepare-rust-android-emulator")
@_common_options
@click.option("--image", "images", type=click.Choice(["min", "current"]), multiple=True)
@click.option("--wsl-distro", default="Ubuntu", show_default=True)
def prepare_rust_android_emulator(*, images: tuple[str, ...], wsl_distro: str,
                                  workspace_options: WorkspaceOptions, layout) -> None:
    """Build/probe emulator tools offline without booting a device or requiring local KVM."""
    from .android_toolchain import prepare_emulator_toolchain

    def prepare(**kwargs: Any) -> None:
        report = prepare_emulator_toolchain(kwargs["layout"], images=images, wsl_distro=wsl_distro)
        print(f"Android emulator tool probe passed: {report}")

    _locked("prepare-rust-android-emulator", prepare)(workspace_options=workspace_options, layout=layout)


@main.command("probe-rust-android-guest")
@_common_options
@click.option("--image", type=click.Choice(["min", "current"]), required=True)
@click.option("--tls-startup", is_flag=True, help="Also execute the six fixed source-bound Android TLS cases.")
@click.option("--wsl-distro", default="Ubuntu", show_default=True)
def probe_rust_android_guest(*, image: str, tls_startup: bool, wsl_distro: str, workspace_options: WorkspaceOptions, layout) -> None:
    """Boot one pinned Android guest with KVM; APK qualification is separate."""
    from .android_toolchain import probe_emulator_guest

    def probe(**kwargs: Any) -> None:
        extra = {"tls_startup": True} if tls_startup else {}
        print(f"Android guest boot passed: {probe_emulator_guest(kwargs['layout'], image=image, wsl_distro=wsl_distro, **extra)}")

    _locked("probe-rust-android-guest", probe)(workspace_options=workspace_options, layout=layout)


@main.command("probe-rust-android-guest-ci")
@click.option("--image", type=click.Choice(["min", "current"]), required=True)
@click.option("--tls-startup", is_flag=True, help="Also execute the six fixed source-bound Android TLS cases.")
def probe_rust_android_guest_ci(*, image: str, tls_startup: bool) -> None:
    """Use the same guest boot owner on minimal hosted build/harness checkouts."""
    from .android_toolchain import probe_emulator_guest
    from .release import _emulebb_rust_artifact_origin

    if _emulebb_rust_artifact_origin()["origin"] != "github-actions":
        raise click.ClickException("CI Android guest boot requires GitHub Actions; use probe-rust-android-guest locally")
    workspace, output = resolve_required_workspace_roots()
    value = os.environ.get("EMULEBB_TESTS_REPO", "").strip()
    if not value:
        raise click.ClickException("CI Android guest boot requires inherited EMULEBB_TESTS_REPO")
    harness = Path(value).resolve()
    if not harness.is_dir() or not harness.is_relative_to(workspace):
        raise click.ClickException("EMULEBB_TESTS_REPO must name an existing checkout below the workspace root")
    layout = replace(build_ci_layout(output_root=output), emule_workspace_root=workspace,
                     tests_repo_root=harness, build_repo_root=Path(__file__).resolve().parents[1])
    options = WorkspaceOptions(workspace_root=workspace, output_root=output, workspace_name="ci")
    if tls_startup:
        options, compile_layout = _android_ci_layout()
        layout = replace(compile_layout, tests_repo_root=harness)

    def probe(**kwargs: Any) -> None:
        extra = {"tls_startup": True} if tls_startup else {}
        print(f"Android guest boot passed: {probe_emulator_guest(kwargs['layout'], image=image, **extra)}")

    _locked("probe-rust-android-guest-ci", probe)(workspace_options=options, layout=layout)


@main.command("validate-rust-android")
@_common_options
@click.option("--abi", "abis", type=click.Choice(["arm64-v8a", "x86_64"]), multiple=True,
              default=("arm64-v8a", "x86_64"), show_default=True)
@click.option("--wsl-distro", default="Ubuntu", show_default=True)
def validate_rust_android(*, abis: tuple[str, ...], wsl_distro: str, workspace_options: WorkspaceOptions, layout) -> None:
    """Run locked offline Rust target checks and Android ELF audits via Linux/WSL Docker."""
    from .android_validation import validate_android

    def validate(**kwargs: Any) -> None:
        print(f"Android Rust target validation passed: {validate_android(kwargs['layout'], abis=abis, wsl_distro=wsl_distro)}")

    _locked("validate-rust-android", validate)(workspace_options=workspace_options, layout=layout)


@main.command("validate-rust-android-ci")
@click.option("--abi", "abis", type=click.Choice(["arm64-v8a", "x86_64"]), multiple=True,
              default=("arm64-v8a", "x86_64"), show_default=True)
def validate_rust_android_ci(*, abis: tuple[str, ...]) -> None:
    """Run the local Android validator on explicit, minimal GitHub checkouts."""
    from .android_validation import validate_android

    options, layout = _android_ci_layout()

    def validate(**kwargs: Any) -> None:
        print(f"Android Rust target validation passed: {validate_android(kwargs['layout'], abis=abis)}")

    _locked("validate-rust-android-ci", validate)(workspace_options=options, layout=layout)


@main.command("package-rust-android-ci")
@click.option("--abi", required=True, type=click.Choice(["arm64-v8a", "x86_64"]))
@click.option("--release-version", required=True)
@click.option("--channel", required=True, type=click.Choice(["nightly"]))
@click.option("--expected-source-sha", required=True)
def package_rust_android_ci(*, abi: str, release_version: str, channel: str, expected_source_sha: str) -> None:
    """Build source-bound experimental Android daemon archives; never an APK."""
    from .android_package import package_android

    options, layout = _android_ci_layout()

    def package(**kwargs: Any) -> None:
        archive = package_android(kwargs["layout"], abi=abi, release_version=release_version,
                                  channel=channel, expected_source_sha=expected_source_sha)
        print(f"Experimental Android developer bundle: {archive}")

    _locked("package-rust-android-ci", package)(workspace_options=options, layout=layout)


@main.command("package-rust-android")
@click.option("--abi", required=True, type=click.Choice(["arm64-v8a", "x86_64"]))
@click.option("--release-version", required=True)
@click.option("--channel", required=True, type=click.Choice(["nightly"]))
@click.option("--expected-source-sha", required=True)
@_common_options
def package_rust_android(*, abi: str, release_version: str, channel: str, expected_source_sha: str,
                         workspace_options, layout) -> None:
    """Use the existing canonical native producer for one exact local peer archive."""
    from .android_package import package_android
    def package(**kwargs):
        print(package_android(kwargs["layout"], abi=abi, release_version=release_version,
                              channel=channel, expected_source_sha=expected_source_sha))
    _locked("package-rust-android", package)(workspace_options=workspace_options, layout=layout)


@main.command("package-rust-android-apk-ci")
@click.option("--release-version", required=True)
@click.option("--channel", required=True, type=click.Choice(["nightly"]))
@click.option("--expected-source-sha", required=True)
@click.option("--expected-cert-sha256", required=True)
@click.option("--version-code", required=True, type=click.IntRange(1, 2_100_000_000))
@click.option("--github-output", type=click.Path(path_type=Path, dir_okay=False))
def package_rust_android_apk_ci(*, release_version: str, channel: str,
                               expected_source_sha: str, expected_cert_sha256: str, version_code: int,
                               github_output: Path | None) -> None:
    """Build and sign the source-bound universal Android application."""
    from .android_apk import package_apk, write_github_outputs
    options, layout = _android_ci_layout()
    def package(**kwargs):
        artifact = package_apk(kwargs["layout"], release_version=release_version, channel=channel,
              expected_source_sha=expected_source_sha, expected_cert_sha256=expected_cert_sha256,
              version_code=version_code)
        if github_output is not None:
            write_github_outputs(artifact, github_output, version=release_version,
                                 source_sha=expected_source_sha, signer_sha256=expected_cert_sha256, version_code=version_code)
        print(artifact)
    _locked("package-rust-android-apk-ci", package)(workspace_options=options, layout=layout)


@main.command("prepare-rust-android-apk-gradle")
@_common_options
def prepare_rust_android_apk_gradle(*, workspace_options, layout) -> None:
    """Probe pinned Gradle/JDK and exact Android Gradle plugin resolution."""
    from .android_apk import probe_gradle
    def probe(**kwargs):
        print(probe_gradle(kwargs["layout"]))
    _locked("prepare-rust-android-apk-gradle", probe)(workspace_options=workspace_options, layout=layout)


@main.command("author-rust-android-apk-guest-inputs")
@_common_options
def author_rust_android_apk_guest_inputs(*, workspace_options, layout) -> None:
    """Download official API34–36 archives and retain their proposed SHA-256 pins."""
    from .android_toolchain import author_apk_guest_inputs
    def author(**kwargs):
        print(author_apk_guest_inputs(kwargs["layout"]))
    _locked("author-rust-android-apk-guest-inputs", author)(workspace_options=workspace_options, layout=layout)


@main.command("prepare-rust-android-apk-emulator")
@click.option("--image", required=True, type=click.Choice(["apk34", "apk35", "apk36", "apk37"]))
@_common_options
def prepare_rust_android_apk_emulator(*, image: str, workspace_options, layout) -> None:
    """Prepare pinned emulator/browser tools without booting or qualifying an APK."""
    from .android_toolchain import prepare_apk_emulator_toolchain
    def prepare(**kwargs):
        print(prepare_apk_emulator_toolchain(kwargs["layout"], image=image))
    _locked("prepare-rust-android-apk-emulator", prepare)(workspace_options=workspace_options, layout=layout)


def _apk_evidence_options(function):
    for name, options in (
        ("--apk-payload", {"type": click.Path(path_type=Path, exists=True, file_okay=False)}),
        ("--apk-manifest-name", {}), ("--apk-manifest-sha256", {}), ("--apk-source-sha", {}),
        ("--expected-cert-sha256", {}), ("--apk-version-code", {"type": click.IntRange(1, 2_100_000_000)}),
        ("--release-version", {}), ("--apk-build-ref", {}), ("--apk-harness-ref", {}),
    ):
        function = click.option(name, required=True, **options)(function)
    return click.option("--github-output", type=click.Path(path_type=Path, dir_okay=False))(function)


def _apk_evidence_arguments(values):
    return {"payload": values["apk_payload"], "manifest_name": values["apk_manifest_name"],
        "manifest_sha256": values["apk_manifest_sha256"], "source_sha": values["apk_source_sha"],
        "signer_sha256": values["expected_cert_sha256"], "version_code": values["apk_version_code"],
        "version": values["release_version"], "build_ref": values["apk_build_ref"], "harness_ref": values["apk_harness_ref"]}


def _apk_evidence_layout():
    options, layout = _android_ci_layout()
    value = os.environ.get("EMULEBB_TESTS_REPO", "")
    harness = Path(value).resolve()
    if not value or not harness.is_dir() or not harness.is_relative_to(layout.emule_workspace_root):
        raise click.ClickException("APK qualification requires EMULEBB_TESTS_REPO below the inherited workspace")
    return options, replace(layout, tests_repo_root=harness)


def _apk_evidence_output(path, destination, name):
    if destination is not None:
        if "\n" in str(path) or "\r" in str(path):
            raise click.ClickException("APK evidence output must be one generated path")
        with destination.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(name + "=" + str(path) + "\n")
    print(path)


@main.command("qualify-rust-android-apk-ci")
@click.option("--image", required=True, type=click.Choice(["apk34", "apk35", "apk36", "apk37"]))
@click.option("--peer-artifacts", required=True, type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option("--peer-build-ref", required=True)
@click.option("--soak-seconds", default=0, show_default=True, type=click.IntRange(0, 7 * 86400))
@_apk_evidence_options
def qualify_rust_android_apk_ci(**values) -> None:
    """Qualify one exact signed APK on one pinned API through the existing guest owner."""
    from .android_apk_qualification import qualify
    options, layout = _apk_evidence_layout()
    def probe(**kwargs):
        report = qualify(kwargs["layout"], **_apk_evidence_arguments(values), image=values["image"],
            peer_artifacts=values["peer_artifacts"], peer_build_ref=values["peer_build_ref"], soak_seconds=values["soak_seconds"])
        _apk_evidence_output(report, values["github_output"], "qualification_path")
    _locked("qualify-rust-android-apk-ci", probe)(workspace_options=options, layout=layout)


@main.command("assemble-rust-android-apk-evidence-ci")
@click.option("--evidence-dir", required=True, type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option("--release-dir", required=True, type=click.Path(path_type=Path, file_okay=False))
@_apk_evidence_options
def assemble_rust_android_apk_evidence_ci(**values) -> None:
    """Bind all four API receipts into the single canonical public APK proof."""
    from .android_apk_qualification import assemble
    options, layout = _apk_evidence_layout()
    def combine(**kwargs):
        report = assemble(kwargs["layout"], **_apk_evidence_arguments(values),
            evidence_dir=values["evidence_dir"], release_dir=values["release_dir"])
        _apk_evidence_output(report, values["github_output"], "qualification_path")
    _locked("assemble-rust-android-apk-evidence-ci", combine)(workspace_options=options, layout=layout)


@main.command("prepare-rust-android-apk-peer")
@click.option("--abi", required=True, type=click.Choice(["arm64-v8a", "x86_64"]))
@click.option("--peer-artifacts", required=True, type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option("--release-version", required=True)
@click.option("--expected-source-sha", required=True)
@click.option("--peer-build-ref", required=True)
@_common_options
def prepare_rust_android_apk_peer(*, abi: str, peer_artifacts: Path, release_version: str,
        expected_source_sha: str, peer_build_ref: str, workspace_options, layout) -> None:
    """Inspect and flatten a canonical same-source native archive without compiling."""
    from .android_apk_qualification import prepare_peer
    from .release import _sha256
    def prepare(**kwargs):
        directory = prepare_peer(kwargs["layout"], artifacts=peer_artifacts, abi=abi, version=release_version,
            source_sha=expected_source_sha, peer_build_ref=peer_build_ref)
        print(json.dumps({"peer_payload": str(directory), "peer_manifest_sha256": _sha256(directory / "manifest.json")}))
    _locked("prepare-rust-android-apk-peer", prepare)(workspace_options=workspace_options, layout=layout)


@main.command("probe-rust-android-apk-device")
@click.option("--adb", required=True, type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--serial", required=True, help="Exact operator-selected connected Wi-Fi serial.")
@click.option("--server-port", default=5037, show_default=True, type=click.IntRange(1, 65535))
@click.option("--apk-payload", required=True, type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option("--apk-manifest-name", required=True)
@click.option("--apk-manifest-sha256", required=True)
@click.option("--apk-source-sha", required=True)
@click.option("--expected-cert-sha256", required=True)
@click.option("--report-dir", required=True, type=click.Path(path_type=Path, file_okay=False))
@click.option("--execute", is_flag=True, help="Perform the bounded owned-installation runtime smoke.")
@click.option("--installation-receipt", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--installation-receipt-sha256")
@click.option("--dwell-seconds", default=30, show_default=True, type=click.IntRange(15, 300))
@click.option("--render-webui", is_flag=True)
@click.option("--configure-storage", is_flag=True)
@click.option("--probe-local-listeners", is_flag=True)
@click.option("--allow-owned-compatible-update", is_flag=True)
@click.option("--compatibility-receipt", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--compatibility-receipt-sha256")
@click.option("--configuration-receipt", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--configuration-receipt-sha256")
@click.option("--peer-payload", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option("--peer-manifest-sha256")
@click.option("--peer-abi", type=click.Choice(["arm64-v8a", "x86_64"]))
@click.option("--rust-source", type=click.Path(path_type=Path, exists=True, file_okay=False))
@_common_options
def probe_rust_android_apk_device(*, adb: Path, serial: str, server_port: int, apk_payload: Path,
        apk_manifest_name: str, apk_manifest_sha256: str, apk_source_sha: str, expected_cert_sha256: str,
        report_dir: Path, execute: bool, installation_receipt: Path | None,
        installation_receipt_sha256: str | None, dwell_seconds: int, render_webui: bool,
        configure_storage: bool, probe_local_listeners: bool, allow_owned_compatible_update: bool, compatibility_receipt: Path | None,
        compatibility_receipt_sha256: str | None, configuration_receipt: Path | None,
        configuration_receipt_sha256: str | None,
        peer_payload: Path | None, peer_manifest_sha256: str | None, peer_abi: str | None, rust_source: Path | None,
        workspace_options, layout) -> None:
    """Use the persisted physical-device owner with one explicit connected phone."""
    def probe(**kwargs):
        if rust_source is not None:
            from .release_candidate import _checked_checkout
            _checked_checkout(rust_source, apk_source_sha, "Physical APK peer Rust source")
        arguments = [kwargs["layout"].tests_repo_root / "scripts/probe-android-apk-device.py"]
        values = {"adb": adb, "serial": serial, "server-port": server_port, "apk-payload": apk_payload,
            "apk-manifest-name": apk_manifest_name, "apk-manifest-sha256": apk_manifest_sha256,
            "apk-source-sha": apk_source_sha, "expected-cert-sha256": expected_cert_sha256,
            "report-dir": report_dir, "dwell-seconds": dwell_seconds,
            "installation-receipt": installation_receipt,
            "installation-receipt-sha256": installation_receipt_sha256,
            "compatibility-receipt": compatibility_receipt, "compatibility-receipt-sha256": compatibility_receipt_sha256,
            "configuration-receipt": configuration_receipt, "configuration-receipt-sha256": configuration_receipt_sha256,
            "peer-payload": peer_payload, "peer-manifest-sha256": peer_manifest_sha256, "peer-abi": peer_abi, "rust-source": rust_source}
        for name, value in values.items():
            if value is not None:
                arguments.extend(("--" + name, str(value)))
        arguments.extend(flag for flag, enabled in (("--execute", execute), ("--render-webui", render_webui),
            ("--configure-storage", configure_storage), ("--probe-local-listeners", probe_local_listeners),
            ("--allow-owned-compatible-update", allow_owned_compatible_update)) if enabled)
        # WHY: the exact Wi-Fi endpoint is operator state. This process owner
        # executes without printing or retaining argv; do not add argv logging.
        run_native(get_python_invocation().command(arguments), label="physical Android APK probe",
                   cwd=kwargs["layout"].tests_repo_root)
    _locked("probe-rust-android-apk-device", probe)(workspace_options=workspace_options, layout=layout)


@main.command("prove-rust-android-profile-compatibility")
@click.option("--from-source-sha", required=True)
@click.option("--to-source-sha", required=True)
@_common_options
def prove_rust_android_profile_compatibility(*, from_source_sha: str, to_source_sha: str,
                                              workspace_options, layout) -> None:
    """Prove exact schema equality for a separately authorized owned-phone update."""
    from .android_profile import prove_profile_compatibility
    def prove(**kwargs):
        print(prove_profile_compatibility(kwargs["layout"], from_source_sha=from_source_sha, to_source_sha=to_source_sha))
    _locked("prove-rust-android-profile-compatibility", prove)(workspace_options=workspace_options, layout=layout)


@main.command("provision-rust-android-signing")
@_common_options
def provision_rust_android_signing(*, workspace_options, layout) -> None:
    """Create retained release key once and configure the four GitHub signing secrets."""
    from .android_signing import provision_signing
    def provision(**kwargs):
        print(json.dumps(provision_signing(kwargs["layout"]), sort_keys=True))
    _locked("provision-rust-android-signing", provision)(workspace_options=workspace_options, layout=layout)


@main.command("package-rust-android-apk")
@click.option("--release-version", required=True)
@click.option("--expected-source-sha", required=True)
@click.option("--expected-cert-sha256", help="Trusted public release certificate SHA-256; required for production.")
@click.option("--version-code", required=True, type=click.IntRange(1, 2_100_000_000))
@click.option("--debug", is_flag=True, help="Explicit local test APK; never publication evidence.")
@click.option("--retained-signing", is_flag=True, help="Explicitly use the verified operator-owned retained release identity.")
@click.option("--author-gradle-verification", is_flag=True, help="Debug only: retain resolved dependency checksums for source review.")
@click.option("--kotlin-only", is_flag=True, help="Compile the staged app only; no native build, APK or runtime proof.")
@click.option("--lint-only", is_flag=True, help="Compile and lint the staged app; no native build, APK or runtime proof.")
@_common_options
def package_rust_android_apk(*, release_version: str, expected_source_sha: str,
                            expected_cert_sha256: str | None, version_code: int, debug: bool, retained_signing: bool,
                            author_gradle_verification: bool, kotlin_only: bool, lint_only: bool, workspace_options, layout) -> None:
    """Build the universal APK through the selected registered source set."""
    from .android_apk import package_apk
    def package(**kwargs):
        print(package_apk(kwargs["layout"], release_version=release_version, channel="nightly",
              expected_source_sha=expected_source_sha, version_code=version_code, debug=debug,
              retained_signing=retained_signing, author_gradle_verification=author_gradle_verification,
              kotlin_only=kotlin_only, lint_only=lint_only, expected_cert_sha256=expected_cert_sha256))
    _locked("package-rust-android-apk", package)(workspace_options=workspace_options, layout=layout)


def _android_ci_layout():
    """Share the validated minimal hosted checkout boundary for Android owners."""
    from .release import _emulebb_rust_artifact_origin

    if _emulebb_rust_artifact_origin()["origin"] != "github-actions":
        raise click.ClickException("CI Android validation requires GitHub Actions; use validate-rust-android locally")
    workspace, output = resolve_required_workspace_roots()
    paths = {}
    for name in ("EMULEBB_RUST_REPO", "EMULEBB_TOOLING_REPO", "MINIUPNP_ROOT", "PCPNATPMP_ROOT"):
        value = os.environ.get(name, "").strip()
        if not value:
            raise click.ClickException(f"CI Android validation requires inherited {name}")
        path = Path(value).resolve()
        if not path.is_dir() or not path.is_relative_to(workspace):
            raise click.ClickException(f"{name} must name an existing checkout below the workspace root")
        paths[name] = path
    layout = replace(build_ci_layout(output_root=output), emule_workspace_root=workspace,
                     emulebb_rust_repo_root=paths["EMULEBB_RUST_REPO"],
                     tooling_repo_root=paths["EMULEBB_TOOLING_REPO"])
    options = WorkspaceOptions(workspace_root=workspace, output_root=output, workspace_name="ci")

    return options, layout


@main.command("local-check")
@click.option("--repo", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--stage", type=click.Choice(["commit", "push", "full"]), default="push",
              help="commit=staged checks; push=fast host checks; full=locked validation of all tracked HEAD inputs.")
@click.option("--remote", default="origin")
@click.option("--push-stdin", is_flag=True, help="Read Git pre-push ref updates from stdin.")
@_common_options
def local_check(*, repo: Path, stage: str, remote: str, push_stdin: bool, workspace_options, layout,
                workstream: str | None = None) -> None:
    """Run fast commit/push checks or explicit full local validation."""
    import sys
    from .local_checks import run_checks

    if push_stdin and stage != "push":
        raise click.ClickException("--push-stdin is only valid with --stage push")
    updates = sys.stdin.read() if push_stdin else None

    def execute(**kwargs):
        return run_checks(
            repo=repo, stage=stage, remote=remote, updates=updates,
            root=workspace_options.workspace_root.resolve(), output=workspace_options.output_root.resolve(),
            workstream=workstream,
        )

    if stage in {"commit", "push"}:
        try:
            execute()
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        _locked("local-check", execute)(workspace_options=workspace_options, layout=layout)


@main.command("rust-runtime-stage")
@click.argument("action", type=click.Choice(["prepare", "publish", "verify", "discard"]))
@click.option("--rust-repo", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--target", default=None, help="Selected native Rust target, defaulting to the compiler host.")
@click.option("--flavor", type=click.Choice(["fresh", "regular", "diagnostics"]), default="fresh")
@_common_options
def rust_runtime_stage(*, action, rust_repo, target, flavor, workspace_options, layout) -> None:
    """Control a registered runtime bundle under the caller's existing workspace lock."""
    import json
    import sys
    from .rust_runtime_stage import discard_stage, prepare_stage, publish_stage, verify_stage

    if layout.emulebb_rust_repo_root is None or rust_repo.resolve() != layout.emulebb_rust_repo_root.resolve():
        raise click.ClickException("Runtime source must match the selected registered layout")
    try:
        if action == "prepare":
            result = prepare_stage(layout, target=target, flavor=flavor)
        elif action == "verify":
            result = verify_stage(layout, target=target, diagnostics=flavor == "diagnostics")
        else:
            description = json.load(sys.stdin)
            if action == "publish":
                result = publish_stage(layout, description)
            else:
                discard_stage(layout, description)
                result = {"discarded": True}
    except (OSError, ValueError, RuntimeError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(result, sort_keys=True))


@main.command()
@click.option("--workspace-name", default=None, help="Workspace name. Defaults to canonical topology.")
@click.option("--artifacts-seed-root", default=None, help="Optional third-party artifact seed root.")
@click.option(
    "--profile",
    "profiles",
    multiple=True,
    type=click.Choice(OPTIONAL_WORKSPACE_PROFILES),
    help="Add an optional repository profile. The core profile is always materialized.",
)
def materialize(*, workspace_name: str | None, artifacts_seed_root: str | None, profiles: tuple[str, ...]) -> None:
    """Materialize a new canonical workspace around this emulebb-build clone."""

    try:
        workspace_root, _output_root = resolve_required_workspace_roots()
        materialize_workspace(
            workspace_root=str(workspace_root),
            workspace_name=workspace_name,
            artifacts_seed_root=artifacts_seed_root,
            profiles=profiles,
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.option("--workspace-name", default=None, help="Workspace name. Defaults to canonical topology.")
@click.option("--artifacts-seed-root", default=None, help="Optional third-party artifact seed root.")
@click.option(
    "--profile",
    "profiles",
    multiple=True,
    type=click.Choice(OPTIONAL_WORKSPACE_PROFILES),
    help="Add an optional repository profile. The core profile is always synchronized.",
)
@click.option("--optional-only", is_flag=True,
              help="Admit only explicit optional profiles in an existing workspace; leave core sources unchanged.")
def sync(*, workspace_name: str | None, artifacts_seed_root: str | None, profiles: tuple[str, ...],
         optional_only: bool = False) -> None:
    """Synchronize setup-owned workspace state."""

    try:
        workspace_root, _output_root = resolve_required_workspace_roots()
        options = dict(
            workspace_root=str(workspace_root),
            workspace_name=workspace_name,
            artifacts_seed_root=artifacts_seed_root,
            profiles=profiles,
        )
        if optional_only:
            options["optional_only"] = True
        sync_workspace(**options)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@_common_options
@click.option(
    "--include-product-family",
    is_flag=True,
    help="Also run repo-native checks for materialized product-family repositories.",
)
@click.option(
    "--product-family-tier",
    type=click.Choice(["quick", "quality", "full"]),
    default="quality",
    show_default=True,
    help="Repo-native product-family validation depth used with --include-product-family.",
)
@click.pass_context
def validate(
    ctx: click.Context,
    *,
    include_product_family: bool,
    product_family_tier: str,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Run workspace validation and centralized policy audits."""

    del ctx
    _locked(
        "validate",
        lambda **kwargs: validate_workspace(
            kwargs["layout"],
            include_product_family=include_product_family,
            product_family_tier=product_family_tier,
        ),
    )(
        workspace_options=workspace_options,
        layout=layout,
    )


@main.command("prepare-product-family")
@_common_options
def prepare_product_family(*, workspace_options: WorkspaceOptions, layout) -> None:
    """Install or fetch dependencies for product-family repositories."""

    _locked("prepare-product-family", lambda **kwargs: prepare_product_family_repos(kwargs["layout"]))(
        workspace_options=workspace_options,
        layout=layout,
    )


@main.command("product-family-toolchain")
@_common_options
@click.option("--strict", is_flag=True, help="Fail when any product-family runtime is missing or outside policy.")
def product_family_toolchain(*, strict: bool, workspace_options: WorkspaceOptions, layout) -> None:
    """Report product-family runtime and tool version policy."""

    _locked(
        "product-family-toolchain",
        lambda **kwargs: print_product_family_toolchain(audit_product_family_toolchain(strict=strict)),
    )(
        workspace_options=workspace_options,
        layout=layout,
    )


@main.command("cleanup")
@_common_options
@click.option("--apply", "apply_cleanup", is_flag=True, help="Delete selected artifacts. Omit for a dry run.")
@click.option("--profile", type=click.Choice(["routine", "deep"]), default="routine", show_default=True)
@click.option("--report-payload-retention-hours", default=24.0, show_default=True, type=float)
@click.option("--report-run-retention-days", default=3.0, show_default=True, type=float)
@click.option("--arr-acquisition-retention-hours", default=24.0, show_default=True, type=float)
@click.option("--build-log-retention-days", default=7.0, show_default=True, type=float)
@click.option("--keep-build-log-runs", default=25, show_default=True, type=int)
@click.option("--include-build-outputs", is_flag=True, help="Also prune generated app/test/dependency build outputs.")
@click.option("--include-release-state", is_flag=True, help="Also prune superseded release rehearsal state.")
@click.option("--include-product-family-outputs", is_flag=True, help="Also prune generated outputs from product-family repos.")
@click.option("--include-root-legacy-state", is_flag=True, help="Also prune legacy generated state directories at workspace root.")
@click.option("--include-legacy-root-logs", is_flag=True, help="Also prune retired root-level workspace log files.")
@click.option(
    "--include-profiling-artifacts/--skip-profiling-artifacts",
    default=True,
    show_default=True,
    help="Prune profiling diagnostics, dumps, ETW, UMDH, and pageheap output.",
)
@click.option(
    "--include-legacy-test-reports/--skip-legacy-test-reports",
    default=True,
    show_default=True,
    help="Prune legacy generated reports under repos/emulebb-build-tests/reports.",
)
def cleanup(
    *,
    apply_cleanup: bool,
    profile: str,
    report_payload_retention_hours: float,
    report_run_retention_days: float,
    arr_acquisition_retention_hours: float,
    build_log_retention_days: float,
    keep_build_log_runs: int,
    include_build_outputs: bool,
    include_release_state: bool,
    include_product_family_outputs: bool,
    include_root_legacy_state: bool,
    include_legacy_root_logs: bool,
    include_profiling_artifacts: bool,
    include_legacy_test_reports: bool,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Prune generated workspace artifacts, dry-run by default."""

    cleanup_options = CleanupOptions(
        apply=apply_cleanup,
        profile=profile,
        report_payload_retention_hours=report_payload_retention_hours,
        report_run_retention_days=report_run_retention_days,
        arr_acquisition_retention_hours=arr_acquisition_retention_hours,
        build_log_retention_days=build_log_retention_days,
        keep_build_log_runs=keep_build_log_runs,
        include_build_outputs=include_build_outputs,
        include_release_state=include_release_state,
        include_product_family_outputs=include_product_family_outputs,
        include_root_legacy_state=include_root_legacy_state,
        include_legacy_root_logs=include_legacy_root_logs,
        include_profiling_artifacts=include_profiling_artifacts,
        include_legacy_test_reports=include_legacy_test_reports,
    )
    _locked("cleanup", lambda **kwargs: cleanup_workspace(kwargs["layout"], cleanup_options))(
        workspace_options=workspace_options,
        layout=layout,
    )


@main.command("audit-artifacts")
@_common_options
def audit_artifacts(*, workspace_options: WorkspaceOptions, layout) -> None:
    """Fail when generated build artifacts are present below repos or workspaces."""

    def run(**kwargs):
        target_layout = kwargs["layout"]
        findings = audit_workspace_artifacts(target_layout)
        print_artifact_audit(target_layout, findings)
        if findings:
            raise click.ClickException("Generated build artifacts remain below repos or workspaces.")

    _locked("audit-artifacts", run)(workspace_options=workspace_options, layout=layout)


@main.command("evidence-index")
@_common_options
@click.option("--threshold-mb", default=512.0, show_default=True, type=float, help="Minimum directory size to include.")
@click.option("--write/--no-write", default=True, show_default=True, help="Write state/heavy-evidence-index.json.")
def evidence_index(*, threshold_mb: float, write: bool, workspace_options: WorkspaceOptions, layout) -> None:
    """Index large generated evidence directories before cleanup decisions."""

    def run(**kwargs):
        target_layout = kwargs["layout"]
        payload = (
            write_heavy_evidence_index(target_layout, threshold_mb=threshold_mb)
            if write
            else build_heavy_evidence_index(target_layout, threshold_mb=threshold_mb)
        )
        print_heavy_evidence_index(target_layout, payload)

    _locked("evidence-index", run)(workspace_options=workspace_options, layout=layout)


@main.command("dep-status")
@_common_options
def dep_status(*, workspace_options: WorkspaceOptions, layout) -> None:
    """Report dependency and app worktree status."""

    _locked("dep-status", lambda **kwargs: write_dependency_status(kwargs["layout"]))(
        workspace_options=workspace_options,
        layout=layout,
    )


@main.command("workspace-status")
@_common_options
def workspace_status(*, workspace_options: WorkspaceOptions, layout) -> None:
    """Report branch, upstream, and dirty-state for every managed workspace repo."""

    _locked("workspace-status", lambda **kwargs: write_workspace_repo_status(kwargs["layout"]))(
        workspace_options=workspace_options,
        layout=layout,
    )


@main.command("status")
def materialization_status() -> None:
    """Report setup-managed repository status."""

    try:
        workspace_root, _output_root = resolve_required_workspace_roots()
        write_materialization_status(workspace_root=str(workspace_root))
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("dep-updates")
@click.option("--workspace-name", default=None, help="Workspace name. Defaults to canonical topology.")
def dep_updates(*, workspace_name: str | None) -> None:
    """Report advisory third-party dependency updates."""

    try:
        workspace_root, _output_root = resolve_required_workspace_roots()
        write_dependency_update_report(workspace_root=str(workspace_root), workspace_name=workspace_name)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("compare")
@click.argument("preset_key", required=False)
def compare_command(*, preset_key: str | None) -> None:
    """Show or launch WinMerge comparison presets."""

    try:
        workspace_root, _output_root = resolve_required_workspace_roots()
        run_compare(preset_key=preset_key, workspace_root=str(workspace_root))
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("analyze-diagnostic-logs")
@_common_options
@click.option(
    "--logs-dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    required=True,
    help="eMuleBB profile logs directory containing emulebb-diagnostics-*.log files.",
)
@click.option("--window-minutes", type=float, default=15.0, show_default=True, help="Bad-peer analysis window.")
@click.option("--top", "top_count", type=int, default=12, show_default=True, help="Maximum rows per top list.")
@click.option("--json", "json_output", is_flag=True, help="Write machine-readable JSON.")
def analyze_diagnostic_logs_command(
    *,
    logs_dir: Path,
    window_minutes: float,
    top_count: int,
    json_output: bool,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Analyze eMuleBB diagnostics logs from one running or stopped profile."""

    script_path = layout.tests_repo_root / "scripts" / "analyze-diagnostic-logs.py"
    command = get_python_invocation().command(
        [
            script_path,
            "--logs-dir",
            logs_dir,
            "--window-minutes",
            str(window_minutes),
            "--top",
            str(top_count),
            *(("--json",) if json_output else ()),
        ]
    )
    _locked(
        "analyze diagnostic logs",
        lambda **kwargs: run_native(command, label="diagnostic log analysis", cwd=kwargs["layout"].tests_repo_root),
    )(workspace_options=workspace_options, layout=layout)


@main.group()
def build() -> None:
    """Build workspace targets."""


@build.command("libs")
@_common_options
@click.option("--clean", is_flag=True, help="Clean selected dependency outputs before building.")
def build_libs(
    *,
    clean: bool,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Build the shared dependency set."""

    _locked(
        "build libs",
        lambda **kwargs: invoke_build_libs(kwargs["layout"], kwargs["workspace_options"], clean=clean),
    )(workspace_options=workspace_options, layout=layout)


@build.command("app")
@_common_options
@click.option("--clean", is_flag=True, help="Clean selected app outputs before building.")
@click.option("--variant", "app_variants", multiple=True, help=APP_VARIANT_BUILD_HELP)
@click.option(
    "--diagnostics",
    is_flag=True,
    help="Build the Release diagnostics executable with all release diagnostics flags enabled.",
)
def build_app(
    *,
    clean: bool,
    app_variants: tuple[str, ...],
    diagnostics: bool,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Build selected app worktrees."""

    _locked(
        "build app",
        lambda **kwargs: invoke_build_apps(
            kwargs["layout"],
            kwargs["workspace_options"],
            clean=clean,
            app_variant_names=app_variants,
            enable_diagnostics=diagnostics,
        ),
    )(workspace_options=workspace_options, layout=layout)


@build.command("tests")
@_common_options
@click.option("--clean", is_flag=True, help="Clean native test intermediates before building.")
@click.option("--test-run-variant", default=None, help=TEST_RUN_VARIANT_HELP)
def build_tests(
    *,
    clean: bool,
    test_run_variant: str | None,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Build the shared native emule-tests executable."""

    build_options = BuildTestsOptions(clean=clean, test_run_variant=test_run_variant)
    _locked(
        "build tests",
        lambda **kwargs: invoke_build_tests(kwargs["layout"], kwargs["workspace_options"], build_options),
    )(workspace_options=workspace_options, layout=layout)


@build.command("clients")
@_common_options
@click.option("--clean", is_flag=True, help="Clean selected client outputs before building.")
@click.option("--diagnostics", is_flag=True, help="Build diagnostics-flavored clients when supported.")
@click.option(
    "--target-os",
    type=click.Choice(["windows", "linux", "macos"]),
    default="windows",
    show_default=True,
    help="Target OS for emulebb-rust; other clients remain Windows-only.",
)
@click.option(
    "--client",
    "clients",
    multiple=True,
    type=click.Choice(["amule", "emulebb-rust"]),
    help=(
        "Optional P2P client to build. Defaults to emulebb-rust. "
        "aMule uses the managed official upstream checkout."
    ),
)
def build_clients(
    *,
    clean: bool,
    diagnostics: bool,
    target_os: str,
    clients: tuple[str, ...],
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Build opt-in third-party P2P clients for local multi-client tests."""

    build_options = BuildClientsOptions(
        clean=clean,
        clients=clients,
        diagnostics=diagnostics,
        target_os=target_os,
    )
    _locked(
        "build clients",
        lambda **kwargs: invoke_build_clients(kwargs["layout"], kwargs["workspace_options"], build_options),
    )(workspace_options=workspace_options, layout=layout)


@build.command("ed2k-server")
@_common_options
@click.option("--clean", is_flag=True, help="Clean the staged server and executable before building.")
def build_ed2k_server(
    *,
    clean: bool,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Build and stage the managed Linux ed2k-server Service/Lab fork."""

    _locked(
        "build ed2k-server",
        lambda **kwargs: invoke_build_ed2k_server(kwargs["layout"], kwargs["workspace_options"], clean=clean),
    )(workspace_options=workspace_options, layout=layout)


@build.command("all")
@_common_options
@click.option("--clean", is_flag=True, help="Clean selected build outputs before building.")
@click.option("--variant", "app_variants", multiple=True, help=APP_VARIANT_BUILD_HELP)
@click.option("--test-run-variant", default=None, help=TEST_RUN_VARIANT_HELP)
def build_all(
    *,
    clean: bool,
    app_variants: tuple[str, ...],
    test_run_variant: str | None,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Build dependencies, app variants, and the native test executable."""

    build_options = BuildTestsOptions(clean=clean, test_run_variant=test_run_variant)

    def run_all(**kwargs: Any) -> None:
        invoke_build_libs(kwargs["layout"], kwargs["workspace_options"], clean=clean)
        invoke_build_apps(kwargs["layout"], kwargs["workspace_options"], clean=clean, app_variant_names=app_variants)
        invoke_build_tests(kwargs["layout"], kwargs["workspace_options"], build_options)

    _locked("build all", run_all)(workspace_options=workspace_options, layout=layout)


@main.group()
def test() -> None:
    """Run workspace test suites."""


@test.command("python", context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@_common_options
@click.option("--quiet", "-q", is_flag=True, help="Pass -q to pytest.")
@click.option("--path", "paths", multiple=True, help="Pytest path to run, relative to emulebb-build-tests.")
@click.option("--expression", "-k", default=None, help="Pytest -k expression.")
@click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)
def test_python(
    *,
    quiet: bool,
    paths: tuple[str, ...],
    expression: str | None,
    extra_args: tuple[str, ...],
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Run the fast pytest harness suite."""

    test_options = PythonTestOptions(
        quiet=quiet,
        paths=paths,
        expression=expression,
        extra_args=extra_args,
    )
    _locked(
        "test python",
        lambda **kwargs: invoke_python_tests(kwargs["layout"], test_options),
    )(workspace_options=workspace_options, layout=layout)


@test.command("builder-unit")
@_common_options
@click.option("--path", "paths", multiple=True, required=True,
              help="Selected Builder tests/test_NAME.py file; repeat for a scoped suite.")
def test_builder_unit(*, paths: tuple[str, ...], workspace_options: WorkspaceOptions, layout) -> None:
    """Run scoped Builder unit regressions with external evidence and one owner."""
    _locked("test builder-unit", lambda **kwargs: invoke_builder_unit_tests(
        kwargs["layout"], paths=paths))(workspace_options=workspace_options, layout=layout)


@test.command("native")
@_common_options
@click.option("--test-run-variant", default=None, help=TEST_RUN_VARIANT_HELP)
@click.option("--suite-name", multiple=True, help="Native doctest suite to run. Defaults to parity and web_api.")
def test_native(
    *,
    test_run_variant: str | None,
    suite_name: tuple[str, ...],
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Run native emule-tests suites without live-diff or live E2E work."""

    _locked(
        "test native",
        lambda **kwargs: invoke_native_test_suites(
            kwargs["layout"],
            kwargs["workspace_options"],
            test_run_variant,
            suite_name,
        ),
    )(workspace_options=workspace_options, layout=layout)


@test.command("all")
@_common_options
def test_all(*, workspace_options: WorkspaceOptions, layout) -> None:
    """Run native parity, coverage, and live-diff checks."""

    _locked("test all", lambda **kwargs: invoke_test_runs(kwargs["layout"], kwargs["workspace_options"]))(
        workspace_options=workspace_options,
        layout=layout,
    )


@test.command("live-diff")
@_common_options
@_comparison_options
def test_live_diff(
    *,
    comparison_options: VariantComparisonOptions,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Compare two configured app variants."""

    _locked(
        "test live-diff",
        lambda **kwargs: invoke_live_diff_runs(kwargs["layout"], kwargs["workspace_options"], comparison_options),
    )(workspace_options=workspace_options, layout=layout)


@test.command("protocol-parity")
@_common_options
@_comparison_options
def test_protocol_parity(
    *,
    comparison_options: VariantComparisonOptions,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Run focused Kad/eD2K protocol parity checks against the community baseline."""

    _locked(
        "test protocol-parity",
        lambda **kwargs: invoke_protocol_parity(kwargs["layout"], kwargs["workspace_options"], comparison_options),
    )(workspace_options=workspace_options, layout=layout)


@test.command("community-core-coverage")
@_common_options
@click.option("--test-run-variant", default=None, help=TEST_RUN_VARIANT_HELP)
@click.option("--baseline-variant", default=None, help=BASELINE_VARIANT_HELP)
@click.option("--rest-coverage-budget", type=click.Choice(["smoke", "contract", "contract-stress"]), default="contract")
@click.option("--rest-stress-budget", type=click.Choice(["off", "smoke", "soak"]), default="smoke")
def test_community_core_coverage(
    *,
    test_run_variant: str | None,
    baseline_variant: str | None,
    rest_coverage_budget: str,
    rest_stress_budget: str,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Run community-core coverage checks."""

    coverage_options = CommunityCoverageOptions(
        test_run_variant=test_run_variant,
        baseline_variant=baseline_variant,
        rest_coverage_budget=rest_coverage_budget,
        rest_stress_budget=rest_stress_budget,
    )
    _locked(
        "test community-core-coverage",
        lambda **kwargs: invoke_community_core_coverage(kwargs["layout"], kwargs["workspace_options"], coverage_options),
    )(workspace_options=workspace_options, layout=layout)


@test.command("live-e2e")
@_common_options
@_live_e2e_options
def test_live_e2e(
    *,
    live_options: LiveE2eOptions,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Run aggregate live E2E suites."""

    _locked(
        "test live-e2e",
        lambda **kwargs: invoke_live_e2e_suite(kwargs["layout"], kwargs["workspace_options"], live_options),
    )(workspace_options=workspace_options, layout=layout)


@test.command("windows-vm")
@_common_options
@click.option("--config-file", default=None, help="Ignored Windows VM lab JSON config. Defaults to vm-lab.local.json.")
@click.option("--matrix", default="win10,win11", show_default=True, help="Comma-separated Windows VM targets.")
@click.option(
    "--profile",
    default="package-smoke",
    show_default=True,
    help="Windows VM test profile from emulebb-build-tests.",
)
@click.option("--release-version", default=ACTIVE_EMULEBB_RELEASE_VERSION, show_default=True)
@click.option("--skip-build", is_flag=True, help="Reuse an existing release package artifact.")
@click.option("--keep-running", is_flag=True, help="Leave the guest running after the test for debugging.")
@click.option("--dry-run", is_flag=True, help="Plan the VM test commands without changing VMs.")
@click.option("--fixture-size-bytes", default=25 * 1024 * 1024, show_default=True, type=int)
@click.option("--local-swarm-mode", type=click.Choice(["plan", "execute"]), default="plan", show_default=True)
def test_windows_vm(
    *,
    config_file: str | None,
    matrix: str,
    profile: str,
    release_version: str,
    skip_build: bool,
    keep_running: bool,
    dry_run: bool,
    fixture_size_bytes: int,
    local_swarm_mode: str,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Run clean Windows Hyper-V package and live tests."""

    vm_options = WindowsVmTestOptions(
        config_file=config_file,
        matrix=parse_matrix(matrix),
        profile=profile,
        release_version=release_version,
        skip_build=skip_build,
        keep_running=keep_running,
        dry_run=dry_run,
        fixture_size_bytes=fixture_size_bytes,
        local_swarm_mode=local_swarm_mode,
    )
    _locked(
        "test windows-vm",
        lambda **kwargs: invoke_windows_vm_tests(kwargs["layout"], kwargs["workspace_options"], vm_options),
    )(workspace_options=workspace_options, layout=layout)


@test.command("campaign-scenario")
@_common_options
@click.option("--scenario", required=True, help="Reusable campaign scenario key, id, or VM profile.")
@click.option("--mode", type=click.Choice(["local", "vm"]), default="local", show_default=True)
@click.option("--release-version", default=ACTIVE_EMULEBB_RELEASE_VERSION, show_default=True)
@click.option("--skip-build/--build", default=True, show_default=True, help="Reuse or rebuild the release package in VM mode.")
@click.option("--dry-run", is_flag=True, help="Plan VM commands without changing VMs.")
@click.option("--fixture-size-bytes", default=25 * 1024 * 1024, show_default=True, type=int)
@click.option("--matrix", default="win10,win11", show_default=True, help="Comma-separated VM targets for VM mode.")
@click.option("--swarm-tier", type=click.Choice(["1", "2", "3"]), default="1", show_default=True)
@click.option("--local-swarm-mode", type=click.Choice(["plan", "execute"]), default="execute", show_default=True)
def test_campaign_scenario(
    *,
    scenario: str,
    mode: str,
    release_version: str,
    skip_build: bool,
    dry_run: bool,
    fixture_size_bytes: int,
    matrix: str,
    swarm_tier: str,
    local_swarm_mode: str,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Run one reusable campaign scenario locally or in Hyper-V."""

    scenario_options = CampaignScenarioOptions(
        scenario=scenario,
        mode=mode,  # type: ignore[arg-type]
        release_version=release_version,
        skip_build=skip_build,
        dry_run=dry_run,
        fixture_size_bytes=fixture_size_bytes,
        vm_matrix=parse_matrix(matrix),
        swarm_tier=int(swarm_tier),  # type: ignore[arg-type]
        local_swarm_mode=local_swarm_mode,  # type: ignore[arg-type]
    )
    _locked(
        "test campaign-scenario",
        lambda **kwargs: invoke_campaign_scenario(kwargs["layout"], kwargs["workspace_options"], scenario_options),
    )(workspace_options=workspace_options, layout=layout)


@test.command("overnight-local-hammer")
@_common_options
@click.option("--until-local", default=None, help="Local wall-clock deadline, for example 2026-05-30T05:00:00.")
@click.option("--timezone", "timezone_str", default="Europe/Berlin", show_default=True)
@click.option("--max-cycles", default=0, show_default=True, type=int, help="Maximum full smoke-to-heavy cycles; 0 means until deadline.")
@click.option("--cycle-pause-seconds", default=0.0, show_default=True, type=float)
@click.option("--dry-run", is_flag=True, help="Write the planned campaign matrix without running phases.")
@click.option("--live-wire-inputs-file", default=None, help="Runtime live-wire JSON with local_package_install settings.")
@click.option("--release-version", default=ACTIVE_EMULEBB_RELEASE_VERSION, show_default=True)
@click.option("--clean", is_flag=True, help="Clean package outputs before installer materialization.")
@click.option("--skip-build", is_flag=True, help="Reuse existing package artifacts during installer materialization.")
@click.option("--p2p-bind-interface-name", default="hide.me", show_default=True)
@click.option("--godzilla-p2p-bind-interface-address", default=None, help="Explicit LAN IPv4 address for Godzilla swarm binding.")
@click.option("--profile-symbols-required/--no-profile-symbols-required", default=True, show_default=True)
def test_overnight_local_hammer(
    *,
    until_local: str | None,
    timezone_str: str,
    max_cycles: int,
    cycle_pause_seconds: float,
    dry_run: bool,
    live_wire_inputs_file: str | None,
    release_version: str,
    clean: bool,
    skip_build: bool,
    p2p_bind_interface_name: str,
    godzilla_p2p_bind_interface_address: str | None,
    profile_symbols_required: bool,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Run the installer-backed local overnight heavy hammer campaign."""

    campaign_options = LocalHammerCampaignOptions(
        until_local=until_local,
        timezone_str=timezone_str,
        max_cycles=max_cycles,
        cycle_pause_seconds=cycle_pause_seconds,
        dry_run=dry_run,
        live_wire_inputs_file=live_wire_inputs_file,
        release_version=release_version,
        clean=clean,
        skip_build=skip_build,
        p2p_bind_interface_name=p2p_bind_interface_name,
        godzilla_p2p_bind_interface_address=godzilla_p2p_bind_interface_address,
        profile_symbols_required=profile_symbols_required,
    )
    _locked(
        "test overnight-local-hammer",
        lambda **kwargs: invoke_local_hammer_campaign(kwargs["layout"], kwargs["workspace_options"], campaign_options),
    )(workspace_options=workspace_options, layout=layout)


@test.command("release-campaign")
@_common_options
@click.option("--campaign", default="emulebb-0.7.3", show_default=True, help="Release campaign manifest id.")
@click.option("--phase", default=None, help="Optional strict taxonomy phase id to show.")
@click.option("--template", "show_template", is_flag=True, help="Show the generic eMuleBB release campaign template.")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON instead of a terminal table.")
@click.option("--execute", is_flag=True, help="Run the selected campaign commands instead of only reporting evidence.")
@click.option(
    "--local-vm-swarm-mode",
    type=click.Choice(["manifest", "local", "vm"]),
    default="manifest",
    show_default=True,
    help="Override reusable local/VM swarm rows to run in local or VM mode during execution.",
)
@click.option(
    "--local-vm-swarm-execution-mode",
    type=click.Choice(["manifest", "plan", "execute"]),
    default="manifest",
    show_default=True,
    help="Override selected reusable local/VM swarm rows to plan or execute.",
)
@click.option("--include-nonblocking", is_flag=True, help="Include nonblocking optional campaign scenarios during execution.")
@click.option("--continue-on-failure", is_flag=True, help="Run remaining campaign commands after a failure.")
@click.option("--dry-run", is_flag=True, help="Write an execution plan report without running campaign commands.")
@click.option("--skip-pre-run-cleanup", is_flag=True, help="Do not prune old generated outcomes before executing campaign commands.")
@click.option("--test-network", type=click.Choice(["default", "offline", "lan", "vpn", "all"]), default="default", show_default=True)
@click.option("--live-wire-inputs-file", default=None, help="Runtime live-wire search/download input JSON for campaign commands.")
@click.option("--radarr-movie-root", default=None, help="Radarr-visible movie root for campaign live checks.")
@click.option("--sonarr-series-root", default=None, help="Sonarr-visible series root for campaign live checks.")
@click.option("--acquisition-timeout-minutes", default=None, type=float, help="Arr acquisition timeout forwarded to campaign live checks.")
@click.option("--p2p-bind-interface-name", default="hide.me", show_default=True)
@click.option("--vpn-guard-live-config", default=None, help="Runtime VPN Guard config forwarded to campaign live checks.")
@click.option("--skip-live-seed-refresh", is_flag=True, help="Reuse the existing live seed state in campaign live checks.")
def test_release_campaign(
    *,
    campaign: str,
    phase: str | None,
    show_template: bool,
    json_output: bool,
    execute: bool,
    local_vm_swarm_mode: str,
    local_vm_swarm_execution_mode: str,
    include_nonblocking: bool,
    continue_on_failure: bool,
    dry_run: bool,
    skip_pre_run_cleanup: bool,
    test_network: str,
    live_wire_inputs_file: str | None,
    radarr_movie_root: str | None,
    sonarr_series_root: str | None,
    acquisition_timeout_minutes: float | None,
    p2p_bind_interface_name: str,
    vpn_guard_live_config: str | None,
    skip_live_seed_refresh: bool,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Show or run release campaign phases, feature flows, and evidence status."""

    campaign_options = ReleaseCampaignOptions(
        campaign=campaign,
        test_network=test_network,
        phase=phase,
        show_template=show_template,
        json_output=json_output,
        execute=execute,
        local_vm_swarm_mode=local_vm_swarm_mode,  # type: ignore[arg-type]
        local_vm_swarm_execution_mode=local_vm_swarm_execution_mode,  # type: ignore[arg-type]
        include_nonblocking=include_nonblocking,
        continue_on_failure=continue_on_failure,
        dry_run=dry_run,
        pre_run_cleanup=not skip_pre_run_cleanup,
        live_wire_inputs_file=live_wire_inputs_file,
        radarr_movie_root=radarr_movie_root,
        sonarr_series_root=sonarr_series_root,
        acquisition_timeout_minutes=acquisition_timeout_minutes,
        p2p_bind_interface_name=p2p_bind_interface_name,
        vpn_guard_live_config=vpn_guard_live_config,
        skip_live_seed_refresh=skip_live_seed_refresh,
    )
    _locked(
        "test release-campaign",
        lambda **kwargs: invoke_release_campaign(kwargs["layout"], kwargs["workspace_options"], campaign_options),
    )(workspace_options=workspace_options, layout=layout)


@test.command("certification")
@_common_options
@click.option("--profile", type=click.Choice(["quick", "fast", "overnight"]), default="fast", show_default=True)
@click.option("--test-network", type=click.Choice(["default", "offline", "lan", "vpn", "all"]), default="default", show_default=True)
@click.option("--live-wire-inputs-file", default=None, help="Runtime live-wire search/download input JSON.")
@click.option("--radarr-movie-root", default=None, help="Radarr-visible movie root for Radarr import live checks.")
@click.option("--sonarr-series-root", default=None, help="Sonarr-visible series root for Sonarr import live checks.")
@click.option("--acquisition-timeout-minutes", default=None, type=float, help="Arr acquisition timeout forwarded to live suites.")
@click.option("--p2p-bind-interface-name", default="hide.me", show_default=True)
@click.option("--vpn-guard-live-config", default=None, help="Runtime VPN Guard config forwarded to certification live checks.")
@click.option("--skip-live-seed-refresh", is_flag=True, help="Reuse the existing live seed state.")
@click.option("--continue-on-failure", is_flag=True, help="Run remaining certification steps after a failed or inconclusive step.")
@click.option("--skip-pre-run-cleanup", is_flag=True, help="Do not prune old generated outcomes before the certification run.")
def test_certification(
    *,
    profile: str,
    test_network: str,
    live_wire_inputs_file: str | None,
    radarr_movie_root: str | None,
    sonarr_series_root: str | None,
    acquisition_timeout_minutes: float | None,
    p2p_bind_interface_name: str,
    vpn_guard_live_config: str | None,
    skip_live_seed_refresh: bool,
    continue_on_failure: bool,
    skip_pre_run_cleanup: bool,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Run the release-certification test matrix."""

    certification_options = CertificationOptions(
        profile=profile,
        test_network=test_network,
        pre_run_cleanup=not skip_pre_run_cleanup,
        continue_on_failure=continue_on_failure,
        live_wire_inputs_file=live_wire_inputs_file,
        radarr_movie_root=radarr_movie_root,
        sonarr_series_root=sonarr_series_root,
        acquisition_timeout_minutes=acquisition_timeout_minutes,
        p2p_bind_interface_name=p2p_bind_interface_name,
        vpn_guard_live_config=vpn_guard_live_config,
        skip_live_seed_refresh=skip_live_seed_refresh,
    )
    _locked(
        "test certification",
        lambda **kwargs: invoke_certification(kwargs["layout"], kwargs["workspace_options"], certification_options),
    )(workspace_options=workspace_options, layout=layout)




@test.command("public-presence")
@_common_options
@click.option("--repo", "repository", type=click.Choice(tuple(OPTIONAL_REPOSITORY_FIELDS)), required=True)
def test_public_presence(*, repository: str, workspace_options: WorkspaceOptions, layout) -> None:
    """Validate optional public sources using their selected Tooling authority."""
    from .public_presence_checks import validate_public_presence

    _locked("test public-presence", lambda **kwargs: validate_public_presence(
        kwargs["layout"], repository=repository,
    ))(workspace_options=workspace_options, layout=layout)


@test.command("rust-rest-openapi")
@_common_options
def test_rust_rest_openapi(*, workspace_options: WorkspaceOptions, layout) -> None:
    """Gate the selected staged daemon through the persisted REST harness."""
    _locked("test rust-rest-openapi", lambda **kwargs: invoke_rust_rest_openapi(
        kwargs["layout"]))(workspace_options=workspace_options, layout=layout)


@test.command("rust-release-package")
@_common_options
@click.option("--release-dir", type=click.Path(path_type=Path, exists=True, file_okay=False), required=True)
@click.option("--release-version", required=True)
@click.option("--target-os", type=click.Choice(["windows", "linux", "macos"]), required=True)
@click.option("--linux-libc", type=click.Choice(["gnu", "musl"]), default=None)
@click.option("--verify-profile-ownership", is_flag=True)
@click.option("--render-webui", is_flag=True)
@click.option("--expected-channel", type=click.Choice(["development", "candidate", "beta", "nightly"]), default=None)
@click.option("--capture-public-webui", is_flag=True, help="Capture neutral Search/About from this fresh strict package proof.")
def test_rust_release_package(*, release_dir, release_version, target_os, linux_libc,
                              verify_profile_ownership, render_webui, expected_channel, capture_public_webui,
                              workspace_options, layout) -> None:
    """Smoke exact-source native packages with disposable profiles and the existing lock."""
    _locked("test rust-release-package", lambda **kwargs: invoke_rust_release_package(
        kwargs["layout"], kwargs["workspace_options"], release_dir=release_dir,
        release_version=release_version, target_os=target_os, linux_libc=linux_libc,
        verify_profile_ownership=verify_profile_ownership, render_webui=render_webui,
        expected_channel=expected_channel,
        capture_public_webui=capture_public_webui,
    ))(workspace_options=workspace_options, layout=layout)


@test.command("rust-unit")
@_common_options
@click.option("--package", default=None, help="Limit to one Rust workspace package.")
@click.option("--refresh-workspace-lock", is_flag=True,
              help="Authoring only: refresh workspace lock entries offline before locked tests.")
def test_rust_unit(*, package: str | None, refresh_workspace_lock: bool,
                   workspace_options: WorkspaceOptions, layout) -> None:
    """Run Rust tests through workspace orchestration."""

    _locked(
        "test rust-unit",
        lambda **kwargs: invoke_rust_unit_tests(
            kwargs["layout"], package=package, refresh_workspace_lock=refresh_workspace_lock,
        ),
    )(workspace_options=workspace_options, layout=layout)


@test.command("rust-webui")
@_common_options
def test_rust_webui(*, workspace_options: WorkspaceOptions, layout) -> None:
    """Run the Rust WebUI unit, stateful Playwright, type, and build gates."""

    _locked(
        "test rust-webui",
        lambda **kwargs: invoke_rust_webui_tests(kwargs["layout"]),
    )(workspace_options=workspace_options, layout=layout)


@test.command("rust-network-proof")
@_common_options
@click.option("--lane", type=click.Choice(["local-cross", "local-kad", "local-protocol", "local-reask", "windows-direct", "windows-consumer", "prepare-corpus"]), required=True)
@click.option("--inputs", default=None, help="Operator-local safe transfer allowlist for Windows direct mode.")
@click.option("--release-zip", default=None, help="Exact hosted Windows release ZIP for consumer mode.")
@click.option("--search-term", default=None, help="Explicit runtime query for consumer mode; not retained in reports.")
@click.option(
    "--replace-running-profile-settings",
    default=None,
    help="Suspend an idle port-owning Rust daemon and restore it after consumer mode.",
)
@click.option(
    "--restore-operator-executable",
    default=None,
    help="Restore this operator executable when it was already stopped before consumer mode.",
)
@click.option("--source-root", default=None, help="Operator-approved source files for corpus preparation.")
@click.option("--max-candidates", default=50, type=int, show_default=True)
@click.option("--max-transfer-bytes", default=5 * 1024 * 1024 - 1, type=int, show_default=True)
@click.option("--max-completion-bytes", default=5 * 1024 * 1024 - 1, type=int, show_default=True)
@click.option("--complete-transfers", is_flag=True, help="Download and SHA-256 verify the allowlisted transfers.")
@click.option("--probe-count", default=0, type=int, show_default=True, help="Queue up to 50 allowlisted transfers for bounded source observation.")
@click.option("--network-timeout-seconds", default=240.0, type=float, show_default=True)
@click.option("--observe-seconds", default=60.0, type=float, show_default=True)
@click.option("--transfer-timeout-seconds", default=3600.0, type=float, show_default=True)
def test_rust_network_proof(
    *,
    lane: str,
    inputs: str | None,
    release_zip: str | None,
    search_term: str | None,
    replace_running_profile_settings: str | None,
    restore_operator_executable: str | None,
    source_root: str | None,
    max_candidates: int,
    max_transfer_bytes: int,
    max_completion_bytes: int,
    complete_transfers: bool,
    probe_count: int,
    network_timeout_seconds: float,
    observe_seconds: float,
    transfer_timeout_seconds: float,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Run a diagnostics-first Rust network proof with persisted harnesses."""

    _locked(
        "test rust-network-proof",
        lambda **kwargs: invoke_rust_network_proof(
            kwargs["layout"], kwargs["workspace_options"], lane=lane, inputs=inputs,
            release_zip=release_zip, search_term=search_term,
            replace_running_profile_settings=replace_running_profile_settings,
            restore_operator_executable=restore_operator_executable,
            source_root=source_root,
            max_candidates=max_candidates, max_transfer_bytes=max_transfer_bytes,
            max_completion_bytes=max_completion_bytes, complete_transfers=complete_transfers,
            probe_count=probe_count, network_timeout_seconds=network_timeout_seconds,
            observe_seconds=observe_seconds,
            transfer_timeout_seconds=transfer_timeout_seconds,
        ),
    )(workspace_options=workspace_options, layout=layout)


@test.command("fake-kad-trust-soak")
@_common_options
@click.option("--live-wire-inputs-file", default=None, help="Runtime live-wire search input JSON.")
@click.option("--test-network", type=click.Choice(["vpn", "all"]), default="vpn", show_default=True)
@click.option("--keep-artifacts", is_flag=True, help="Keep source artifacts after the soak run.")
@click.option("--keep-running", is_flag=True, help="Leave eMule running when the soak passes.")
@click.option("--skip-live-seed-refresh", is_flag=True, help="Reuse the existing live seed state.")
@click.option("--duration-seconds", default=3 * 60 * 60, show_default=True, type=float)
@click.option("--cycle-pause-seconds", default=10.0, show_default=True, type=float)
@click.option("--search-observation-timeout-seconds", default=90.0, show_default=True, type=float)
@click.option("--resource-sample-interval-seconds", default=60.0, show_default=True, type=float)
@click.option("--min-result-rows", default=1, show_default=True, type=int)
@click.option("--min-kad-publish-info-rows", default=1, show_default=True, type=int)
@click.option("--max-failed-cycles", default=0, show_default=True, type=int)
@click.option("--require-kad-connected", is_flag=True, help="Require full Kad connected state before soaking.")
@click.option("--p2p-bind-interface-name", default="hide.me", show_default=True)
def test_fake_kad_trust_soak(
    *,
    live_wire_inputs_file: str | None,
    test_network: str,
    keep_artifacts: bool,
    keep_running: bool,
    skip_live_seed_refresh: bool,
    duration_seconds: float,
    cycle_pause_seconds: float,
    search_observation_timeout_seconds: float,
    resource_sample_interval_seconds: float,
    min_result_rows: int,
    min_kad_publish_info_rows: int,
    max_failed_cycles: int,
    require_kad_connected: bool,
    p2p_bind_interface_name: str,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Run a focused fake-file risk and Kad trust live soak."""

    soak_options = FakeKadTrustSoakOptions(
        live_wire_inputs_file=live_wire_inputs_file,
        test_network=test_network,
        keep_artifacts=keep_artifacts,
        keep_running=keep_running,
        skip_live_seed_refresh=skip_live_seed_refresh,
        duration_seconds=duration_seconds,
        cycle_pause_seconds=cycle_pause_seconds,
        search_observation_timeout_seconds=search_observation_timeout_seconds,
        resource_sample_interval_seconds=resource_sample_interval_seconds,
        min_result_rows=min_result_rows,
        min_kad_publish_info_rows=min_kad_publish_info_rows,
        max_failed_cycles=max_failed_cycles,
        require_kad_connected=require_kad_connected,
        p2p_bind_interface_name=p2p_bind_interface_name,
    )
    _locked(
        "test fake-kad-trust-soak",
        lambda **kwargs: invoke_fake_kad_trust_soak(kwargs["layout"], kwargs["workspace_options"], soak_options),
    )(workspace_options=workspace_options, layout=layout)








@main.command()
@_common_options
@click.option("--clean", is_flag=True, help="Clean selected build outputs before building.")
@click.option("--variant", "app_variants", multiple=True, help=APP_VARIANT_BUILD_HELP)
@click.option("--test-run-variant", default=None, help=TEST_RUN_VARIANT_HELP)
def full(
    *,
    clean: bool,
    app_variants: tuple[str, ...],
    test_run_variant: str | None,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Run build all, test all, and a workspace summary."""

    build_options = BuildTestsOptions(clean=clean, test_run_variant=test_run_variant)

    def run_full(**kwargs: Any) -> None:
        invoke_build_libs(kwargs["layout"], kwargs["workspace_options"], clean=clean)
        invoke_build_apps(kwargs["layout"], kwargs["workspace_options"], clean=clean, app_variant_names=app_variants)
        invoke_build_tests(kwargs["layout"], kwargs["workspace_options"], build_options)
        invoke_test_runs(kwargs["layout"], kwargs["workspace_options"])
        write_workspace_summary(kwargs["layout"])

    _locked("full", run_full)(workspace_options=workspace_options, layout=layout)


@main.command("package-release")
@_common_options
@click.option("--clean", is_flag=True, help="Clean selected package build outputs before building.")
@click.option("--require-signing", is_flag=True, help="Require configured Authenticode signing for release package files.")
@click.option(
    "--release-version",
    default=ACTIVE_EMULEBB_RELEASE_VERSION,
    show_default=True,
    help="Release version in MAJOR.MINOR.PATCH[-rc.N|-beta.N|-nightly.YYYYMMDD.SHA] form.",
)
def package_release(
    *,
    clean: bool,
    require_signing: bool,
    release_version: str,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Build the main app and create release package artifacts."""

    package_options = ReleasePackageOptions(release_version=release_version, clean=clean, require_signing=require_signing)
    _locked(
        "package release",
        lambda **kwargs: create_release_package(kwargs["layout"], kwargs["workspace_options"], package_options),
    )(workspace_options=workspace_options, layout=layout)


@main.command("package-emulebb-rust")
@_common_options
@click.option("--clean", is_flag=True, help="Clean selected Rust build/staging outputs before building.")
@click.option(
    "--release-version",
    default="0.1.0-beta.1",
    show_default=True,
    help="Rust release version in MAJOR.MINOR.PATCH[-rc.N|-beta.N|-nightly.YYYYMMDD.SHA] form.",
)
@click.option("--skip-build", is_flag=True, help="Reuse the staged regular Rust runtime instead of rebuilding it.")
@click.option(
    "--target-os",
    type=click.Choice(["windows", "linux", "macos"]),
    default="windows",
    show_default=True,
    help="Package Windows ZIP, Linux DEB/AppImage, or macOS DMG assets.",
)
@click.option(
    "--linux-format",
    "linux_formats",
    type=click.Choice(["deb", "appimage"]),
    multiple=True,
    default=("deb", "appimage"),
    show_default=True,
    help="Linux package formats to create when --target-os linux.",
)
def package_emulebb_rust(
    *,
    clean: bool,
    release_version: str,
    skip_build: bool,
    target_os: str,
    linux_formats: tuple[str, ...],
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Build or reuse the regular emulebb-rust daemon and create release artifacts."""

    package_options = EmulebbRustPackageOptions(
        release_version=release_version,
        clean=clean,
        skip_build=skip_build,
        target_os=target_os,
        linux_formats=linux_formats,
    )
    _locked(
        "package emulebb-rust",
        lambda **kwargs: create_emulebb_rust_package(kwargs["layout"], kwargs["workspace_options"], package_options),
    )(workspace_options=workspace_options, layout=layout)


@main.command("validate-emulebb-rust-linux")
@_common_options
@click.option("--scope", type=click.Choice(["musl", "full", "image"]), default="musl", show_default=True)
def validate_emulebb_rust_linux(*, scope: str, workspace_options: WorkspaceOptions, layout) -> None:
    """Run local GNU/musl gates and name the native GitHub checks still required."""
    from .linux_validation import validate_linux

    def validate(**kwargs: Any) -> None:
        report = validate_linux(kwargs["layout"], scope=scope)
        print(f"Selected local Linux gates passed: {report}")

    _locked("validate emulebb-rust linux", validate)(workspace_options=workspace_options, layout=layout)


@main.command("smoke-emulebb-rust-linux")
@_common_options
@click.option("--archive", type=click.Path(path_type=Path), required=True)
@click.option("--libc", type=click.Choice(["gnu", "musl"]), required=True)
@click.option("--architecture", type=click.Choice(["amd64", "arm64"]), required=True)
def smoke_emulebb_rust_linux(*, archive: Path, libc: str, architecture: str,
                            workspace_options: WorkspaceOptions, layout) -> None:
    """Test an existing source-verified archive without rebuilding its bytes."""
    from .linux_containers import smoke_linux_archive

    def smoke(**kwargs: Any) -> None:
        smoke_linux_archive(kwargs["layout"], archive=archive, libc=libc, architecture=architecture)

    _locked("smoke emulebb-rust linux", smoke)(workspace_options=workspace_options, layout=layout)


@main.command("package-emulebb-rust-linux")
@_common_options
@click.option("--libc", type=click.Choice(["gnu", "musl"]), required=True)
@click.option("--architecture", type=click.Choice(["amd64", "arm64"]), required=True)
@click.option("--webui-dist", type=click.Path(path_type=Path))
@click.option("--release-version")
@click.option("--smoke", is_flag=True, help="Run the shared non-root runtime and local-transfer gate.")
@click.option("--test-scope", type=click.Choice(["portable", "full"]), default="portable", show_default=True,
              help="Full compiles every test; native CPUs execute the workspace, cross CPUs execute portable libraries.")
def package_emulebb_rust_linux(*, libc: str, architecture: str, webui_dist: Path | None,
                              release_version: str | None, smoke: bool, test_scope: str, workspace_options: WorkspaceOptions, layout) -> None:
    """Build a verified native GNU/musl archive through Linux/WSL Docker."""
    from .linux_containers import build_linux_artifacts

    def package(**kwargs: Any) -> None:
        build_linux_artifacts(kwargs["layout"], libc=libc, architecture=architecture,
                              webui_dist=webui_dist, release_version=release_version, smoke=smoke, test_scope=test_scope)

    _locked("package emulebb-rust linux", package)(workspace_options=workspace_options, layout=layout)


@main.command("package-emulebb-rust-linux-ci")
@click.option("--use-ci-image-mirrors", is_flag=True, help="Use verified exact-digest Build mirrors; never fall back to upstream.")
@click.option("--libc", type=click.Choice(["gnu", "musl"]), required=True)
@click.option("--architecture", type=click.Choice(["amd64", "arm64"]), required=True)
@click.option("--webui-dist", type=click.Path(path_type=Path))
@click.option("--release-version", required=True)
@click.option("--smoke", is_flag=True)
@click.option("--smoke-report-path", type=click.Path(path_type=Path),
              help="Retain the passed lifecycle receipt at this new path below the output root.")
@click.option("--test-scope", type=click.Choice(["portable", "full"]), default="portable", show_default=True)
def package_emulebb_rust_linux_ci(*, libc: str, architecture: str, webui_dist: Path | None,
                                 release_version: str, smoke: bool, smoke_report_path: Path | None,
                                 test_scope: str, use_ci_image_mirrors: bool = False) -> None:
    """Use the local Linux container recipe on a minimal native CI checkout."""
    from .linux_containers import build_linux_artifacts, smoke_linux_archive
    from .release import _emulebb_rust_artifact_origin
    if _emulebb_rust_artifact_origin()["origin"] != "github-actions":
        raise click.ClickException("CI Linux packaging requires GitHub Actions; use package-emulebb-rust-linux locally")
    if smoke_report_path is not None and not smoke:
        raise click.ClickException("--smoke-report-path requires --smoke")
    required = [WORKSPACE_OUTPUT_ROOT_ENV, "EMULEBB_RUST_REPO", "EMULEBB_TOOLING_REPO", "MINIUPNP_ROOT", "PCPNATPMP_ROOT"]
    if smoke:
        required.append("EMULEBB_BUILD_TESTS_REPO")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise click.ClickException(f"Linux packaging requires inherited values: {', '.join(missing)}")
    layout = replace(build_ci_layout(output_root=Path(os.environ[WORKSPACE_OUTPUT_ROOT_ENV])),
                     emulebb_rust_repo_root=Path(os.environ["EMULEBB_RUST_REPO"]).resolve(),
                     tooling_repo_root=Path(os.environ["EMULEBB_TOOLING_REPO"]).resolve())
    if smoke:
        layout = replace(layout, tests_repo_root=Path(os.environ["EMULEBB_BUILD_TESTS_REPO"]).resolve())
    report_target = None
    if smoke_report_path is not None:
        report_target = smoke_report_path.resolve()
        if (smoke_report_path.is_symlink() or report_target.exists()
                or not report_target.is_relative_to(layout.output_root)):
            raise click.ClickException("Smoke report requires a new regular path below the inherited output root")
    try:
        options = WorkspaceOptions(workspace_root=layout.emule_workspace_root, output_root=layout.output_root,
                                   workspace_name="ci")
        archive = _locked("package emulebb-rust linux ci", lambda **kwargs: build_linux_artifacts(
            kwargs["layout"], libc=libc, architecture=architecture, webui_dist=webui_dist,
            release_version=release_version, smoke=False, test_scope=test_scope,
            **({"use_ci_image_mirrors": True} if use_ci_image_mirrors else {})))(
                workspace_options=options, layout=layout)
        if smoke:
            receipt = smoke_linux_archive(layout, archive=archive, libc=libc, architecture=architecture,
                                          **({"use_ci_image_mirrors": True} if use_ci_image_mirrors else {}))
            if report_target is not None:
                if receipt.is_symlink() or not receipt.is_file() or not receipt.resolve().is_relative_to(layout.output_root):
                    raise click.ClickException("Lifecycle receipt must be a regular file below the output root")
                report_target.parent.mkdir(parents=True, exist_ok=True)
                # WHY: CI needs one explicit artifact path, not a guess at a
                # UUID report directory. Exclusive creation preserves an older
                # receipt if another producer races this retention step.
                with receipt.open("rb") as source, report_target.open("xb") as destination:
                    shutil.copyfileobj(source, destination)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("linux-containers")
@_common_options
@click.option("--wsl-distro", default="Ubuntu", show_default=True)
@click.option("--setup-arm64", is_flag=True, help="Register pinned ARM64 QEMU in the Docker host.")
def linux_containers(*, wsl_distro: str, setup_arm64: bool, workspace_options: WorkspaceOptions, layout) -> None:
    """Verify local WSL/Linux Docker and actual ARM64 execution."""
    from .linux_containers import LinuxDocker

    def verify(**kwargs: Any) -> None:
        LinuxDocker(wsl_distro).preflight(kwargs["layout"].output_root, setup_arm64=setup_arm64)

    _locked("linux containers", verify)(workspace_options=workspace_options, layout=layout)


@main.command("verify-linux-profile-scratch")
@_common_options
def verify_linux_profile_scratch(*, workspace_options: WorkspaceOptions, layout) -> None:
    """Prove native GNU/AMD64 profile permissions and alias isolation only."""
    from .linux_containers import verify_linux_profile_scratch as verify

    _locked("verify Linux profile scratch", lambda **kwargs: verify(kwargs["layout"]))(
        workspace_options=workspace_options, layout=layout)


@main.command("validate-emulebb-rust-image")
@_common_options
@click.option("--archive", type=click.Path(path_type=Path), required=True)
@click.option("--image", required=True)
@click.option("--architecture", "architectures", type=click.Choice(["amd64", "arm64"]), multiple=True,
              default=("amd64", "arm64"), show_default=True)
@click.option("--security-dir", type=click.Path(path_type=Path))
@click.option("--wsl-distro", default="Ubuntu", show_default=True)
def validate_emulebb_rust_image(*, archive: Path, image: str, architectures: tuple[str, ...], security_dir: Path | None,
                                wsl_distro: str, workspace_options: WorkspaceOptions, layout) -> None:
    """Run fresh image lifecycle, SBOM and fixable HIGH/CRITICAL CVE gates."""
    from .image_security import validate_image

    def validate(**kwargs: Any) -> None:
        report = validate_image(kwargs["layout"], archive=archive, image=image, architectures=architectures,
                                security_dir=security_dir, wsl_distro=wsl_distro)
        print(f"Image validation passed: {report}")

    _locked("validate emulebb-rust image", validate)(workspace_options=workspace_options, layout=layout)


@main.command("smoke-emulebb-rust-image")
@_common_options
@click.option("--archive", type=click.Path(path_type=Path), required=True)
@click.option("--image", required=True)
@click.option("--architecture", "architectures", type=click.Choice(["amd64", "arm64"]), multiple=True,
              default=("amd64", "arm64"), show_default=True)
@click.option("--wsl-distro", default="Ubuntu", show_default=True)
def smoke_emulebb_rust_image(*, archive: Path, image: str, architectures: tuple[str, ...], wsl_distro: str,
                             workspace_options: WorkspaceOptions, layout) -> None:
    """Validate a source-verified OCI archive through Linux/WSL Docker."""
    from .rust_images import smoke_image

    def smoke(**kwargs: Any) -> None:
        report = smoke_image(kwargs["layout"], archive=archive, image=image,
                             architectures=architectures, wsl_distro=wsl_distro)
        print(f"Image smoke passed: {report}")

    _locked("smoke emulebb-rust image", smoke)(workspace_options=workspace_options, layout=layout)


@main.command("package-emulebb-rust-image-local")
@_common_options
@click.option("--variant", type=click.Choice(["ubuntu", "alpine"]), default="ubuntu", show_default=True)
@click.option("--release-version", required=True)
@click.option("--assets-dir", type=click.Path(path_type=Path))
@click.option("--image")
@click.option("--wsl-distro", default="Ubuntu", show_default=True)
@click.option("--architecture", "architectures", type=click.Choice(["amd64", "arm64"]), multiple=True,
              default=("amd64", "arm64"), show_default=True)
def package_emulebb_rust_image_local(
    *,
    variant: str,
    release_version: str,
    assets_dir: Path | None,
    image: str | None,
    wsl_distro: str,
    architectures: tuple[str, ...],
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Build a verified multiarch OCI archive using Linux/WSL Docker."""

    selected_image = image or f"local/emulebb-rust:{release_version}-{variant}"

    def build_local_image(**kwargs: Any) -> None:
        from .rust_images import collect_linux_packages

        selected_assets = assets_dir or collect_linux_packages(kwargs["layout"], version=release_version,
                                                               architectures=architectures, variant=variant)
        built = build_emulebb_rust_image_local(
            rust_root=kwargs["layout"].emulebb_rust_repo_root,
            output_root=kwargs["layout"].output_root,
            assets_dir=selected_assets,
            version=release_version,
            image=selected_image,
            wsl_distro=wsl_distro,
            architectures=architectures,
            variant=variant,
        )
        print(f"Local Rust image archived: {built}")

    _locked("package emulebb-rust image local", build_local_image)(
        workspace_options=workspace_options,
        layout=layout,
    )


@main.command("package-emulebb-rust-ci")
@click.option("--prepared-image-receipt", type=click.Path(path_type=Path, exists=True, dir_okay=False), default=None, hidden=True)
@click.option(
    "--release-version",
    default="0.1.0-beta.1",
    show_default=True,
    help="Rust release version in MAJOR.MINOR.PATCH[-rc.N|-beta.N|-nightly.YYYYMMDD.SHA] form.",
)
@click.option("--clean", is_flag=True, help="Clean selected Rust build/staging outputs before building.")
@click.option("--target-os", type=click.Choice(["windows", "linux", "macos"]), required=True)
@click.option("--platform", type=click.Choice(["x64", "ARM64"]), required=True)
def package_emulebb_rust_ci(*, release_version: str, clean: bool, target_os: str, platform: str,
                           prepared_image_receipt: Path | None = None) -> None:
    """Build Rust assets from fork checkouts on a clean native CI runner."""

    output_root_value = os.environ.get(WORKSPACE_OUTPUT_ROOT_ENV, "").strip()
    rust_repo_value = os.environ.get("EMULEBB_RUST_REPO", "").strip()
    tooling_repo_value = os.environ.get("EMULEBB_TOOLING_REPO", "").strip()
    missing = [
        name
        for name, value in (
            (WORKSPACE_OUTPUT_ROOT_ENV, output_root_value),
            ("EMULEBB_RUST_REPO", rust_repo_value),
            ("EMULEBB_TOOLING_REPO", tooling_repo_value),
        )
        if not value
    ]
    if missing:
        raise click.ClickException(f"CI Rust packaging requires: {', '.join(missing)}.")

    layout = replace(
        build_ci_layout(output_root=Path(output_root_value)),
        emulebb_rust_repo_root=Path(rust_repo_value).resolve(),
        tooling_repo_root=Path(tooling_repo_value).resolve(),
    )
    options = WorkspaceOptions(
        workspace_root=layout.emule_workspace_root,
        output_root=layout.output_root,
        workspace_name="ci",
        configuration="Release",
        platform=platform,
    )
    package_options = EmulebbRustPackageOptions(
        release_version=release_version,
        clean=clean,
        target_os=target_os,
        prepared_image_receipt=prepared_image_receipt,
    )
    if prepared_image_receipt:
        from .prepared_native_ci import load_environment, seed_runtime
        if target_os != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
            raise click.ClickException("Prepared image evidence belongs only to hosted native Linux packages")
        environment = load_environment(prepared_image_receipt, layout.output_root)
        architecture = "amd64" if platform == "x64" else "arm64"
        if environment.get("runtimeProbe", {}).get("architecture") != architecture:
            raise click.ClickException("Prepared package evidence has the wrong native CPU")
        seed_runtime(layout.output_root, "x86_64" if platform == "x64" else "aarch64")
    try:
        _locked("package emulebb-rust native ci", lambda **kwargs: create_emulebb_rust_package(
            kwargs["layout"], kwargs["workspace_options"], package_options))(
                workspace_options=options, layout=layout)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("package-emulebb-rust-prepared-ci")
@click.option("--image", required=True)
@click.option("--release-version", required=True)
@click.option("--platform", type=click.Choice(["x64", "ARM64"]), required=True)
def package_emulebb_rust_prepared_ci(image, release_version, platform):
    """Run the unchanged native Linux package pipeline in the verified CI image."""
    from .prepared_native_ci import package
    try:
        print(f"Prepared native package evidence: {package(image=image, release_version=release_version, target_platform=platform)}")
    except Exception as error:
        raise click.ClickException(str(error)) from error


@main.command("assemble-emulebb-rust-release-ci")
@click.option("--release-version", required=True)
@click.option("--assets-dir", type=click.Path(path_type=Path), required=True)
@click.option("--platform-profile", type=click.Choice(["full", "windows-linux"]), default="full", show_default=True)
def assemble_emulebb_rust_release_ci(*, release_version: str, assets_dir: Path,
                                    platform_profile: str) -> None:
    """Verify native/musl packages and checksum all assembled public evidence."""

    output_root_value = os.environ.get(WORKSPACE_OUTPUT_ROOT_ENV, "").strip()
    if not output_root_value:
        raise click.ClickException(f"{WORKSPACE_OUTPUT_ROOT_ENV} must be inherited for release assembly.")
    output_root = Path(output_root_value).resolve()
    assets_dir = assets_dir.resolve()
    if not assets_dir.is_relative_to(output_root):
        raise click.ClickException("Rust release assets must be under the inherited output root.")
    try:
        sums = assemble_emulebb_rust_release_assets(assets_dir, release_version,
                                                   platform_profile=platform_profile)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    print(f"Rust release checksums: {sums}")


@main.command("package-emulebb-rust-image-ci")
@click.option("--use-ci-image-mirrors", is_flag=True, help="Use exact-digest unpack-stage mirror; GHCR runtime bases stay unchanged.")
@click.option("--variant", type=click.Choice(["ubuntu", "alpine"]), default="ubuntu", show_default=True)
@click.option("--release-version", required=True)
@click.option("--assets-dir", type=click.Path(path_type=Path), required=True)
@click.option("--control-rust-repo", type=click.Path(path_type=Path, exists=True, file_okay=False), required=True)
def package_emulebb_rust_image_ci(
    *,
    variant: str,
    release_version: str,
    assets_dir: Path,
    control_rust_repo: Path,
    use_ci_image_mirrors: bool = False,
) -> None:
    """Build a verified multi-architecture Ubuntu or Alpine image."""

    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise click.ClickException("CI image production requires GitHub Actions; use package-emulebb-rust-image-local for local validation.")
    output_value = os.environ.get(WORKSPACE_OUTPUT_ROOT_ENV, "").strip()
    rust_value = os.environ.get("EMULEBB_RUST_REPO", "").strip()
    if not output_value or not rust_value:
        raise click.ClickException(f"{WORKSPACE_OUTPUT_ROOT_ENV} and EMULEBB_RUST_REPO must be inherited.")
    output_root = Path(output_value).resolve()
    assets_dir = assets_dir.resolve()
    if not assets_dir.is_relative_to(output_root):
        raise click.ClickException("Rust image assets must be under the inherited output root.")
    try:
        archive = build_emulebb_rust_image_ci(
            rust_root=Path(rust_value),
            output_root=output_root,
            assets_dir=assets_dir,
            version=release_version,
            control_rust_root=control_rust_repo.resolve(),
            variant=variant,
            **({"use_ci_image_mirrors": True} if use_ci_image_mirrors else {}),
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    print(f"Rust image archived: {archive}")


@main.command("validate-emulebb-rust-image-ci")
@click.option("--archive", type=click.Path(path_type=Path, exists=True, dir_okay=False), required=True)
@click.option("--image", required=True)
@click.option("--architecture", type=click.Choice(["amd64", "arm64"]), required=True)
@click.option("--security-dir", type=click.Path(path_type=Path), required=True)
@click.option("--control-rust-repo", type=click.Path(path_type=Path, exists=True, file_okay=False), required=True)
def validate_emulebb_rust_image_ci(*, archive: Path, image: str, architecture: str,
                                  security_dir: Path, control_rust_repo: Path) -> None:
    """Run fresh image proof on the requested native GitHub Linux runner."""
    from .image_security import validate_image_ci

    required = (WORKSPACE_OUTPUT_ROOT_ENV, "EMULEBB_RUST_REPO", "EMULEBB_BUILD_TESTS_REPO")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise click.ClickException("CI image validation requires inherited values: " + ", ".join(missing))
    layout = replace(build_ci_layout(output_root=Path(os.environ[WORKSPACE_OUTPUT_ROOT_ENV])),
                     emulebb_rust_repo_root=Path(os.environ["EMULEBB_RUST_REPO"]).resolve(),
                     tests_repo_root=Path(os.environ["EMULEBB_BUILD_TESTS_REPO"]).resolve())
    try:
        evidence = validate_image_ci(layout, archive=archive, image=image, architecture=architecture,
                                     security_dir=security_dir, control_rust_root=control_rust_repo.resolve())
    except Exception as error:
        raise click.ClickException(str(error)) from error
    print(f"Native GitHub image validation passed: {evidence}")


@main.command("assemble-emulebb-rust-image-evidence-ci")
@click.option("--archive", type=click.Path(path_type=Path, exists=True, dir_okay=False), required=True)
@click.option("--evidence-dir", type=click.Path(path_type=Path, exists=True, file_okay=False), required=True)
@click.option("--release-dir", type=click.Path(path_type=Path, exists=True, file_okay=False), required=True)
@click.option("--release-version", required=True)
@click.option("--source-sha", required=True)
@click.option("--harness-repo", type=click.Path(path_type=Path, exists=True, file_okay=False), required=True)
@click.option("--harness-sha", required=True)
@click.option("--control-rust-repo", type=click.Path(path_type=Path, exists=True, file_okay=False), required=True)
def assemble_emulebb_rust_image_evidence_ci(*, archive: Path, evidence_dir: Path, release_dir: Path,
                                           release_version: str, source_sha: str, harness_repo: Path,
                                           harness_sha: str, control_rust_repo: Path) -> None:
    """Bind both native image proofs to the exact candidate GNU packages."""
    from .image_security import assemble_image_security_ci

    output_value = os.environ.get(WORKSPACE_OUTPUT_ROOT_ENV, "").strip()
    if not output_value:
        raise click.ClickException(f"{WORKSPACE_OUTPUT_ROOT_ENV} must be inherited for image evidence assembly.")
    try:
        evidence = assemble_image_security_ci(
            output_root=Path(output_value), archive=archive, evidence_dir=evidence_dir, release_dir=release_dir,
            version=release_version, source_sha=source_sha, harness_root=harness_repo,
            harness_sha=harness_sha, control_rust_root=control_rust_repo)
    except Exception as error:
        raise click.ClickException(str(error)) from error
    print(f"Native image evidence assembled: {evidence}")


@main.group("vm-lab")
def vm_lab() -> None:
    """Prepare local Hyper-V Windows VM test images."""


@vm_lab.command("prepare")
@_common_options
@click.option("--config-file", default=None, help="Ignored Windows VM lab JSON config. Defaults to vm-lab.local.json.")
@click.option("--matrix", default="win10,win11", show_default=True, help="Comma-separated Windows VM targets.")
@click.option("--rebuild-images", is_flag=True, help="Remove and rebuild existing VM images before checkpointing.")
@click.option("--dry-run", is_flag=True, help="Plan the VM preparation commands without changing VMs.")
def vm_lab_prepare(
    *,
    config_file: str | None,
    matrix: str,
    rebuild_images: bool,
    dry_run: bool,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Build Windows VM images and clean checkpoints from configured ISOs."""

    prepare_options = VmPrepareOptions(
        config_file=config_file,
        matrix=parse_matrix(matrix),
        rebuild_images=rebuild_images,
        dry_run=dry_run,
    )
    _locked(
        "vm-lab prepare",
        lambda **kwargs: prepare_vm_lab(kwargs["layout"], prepare_options),
    )(workspace_options=workspace_options, layout=layout)


@vm_lab.command("audit")
@_common_options
@click.option("--config-file", default=None, help="Ignored Windows VM lab JSON config. Defaults to vm-lab.local.json.")
@click.option("--matrix", default="win10,win11", show_default=True, help="Comma-separated configured Windows VM targets.")
@click.option("--vm-name-pattern", default="emulebb-*", show_default=True, help="Additional Hyper-V VM name pattern to audit.")
def vm_lab_audit(
    *,
    config_file: str | None,
    matrix: str,
    vm_name_pattern: str,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Audit Hyper-V VM lab registrations for stale workspace-state disks."""

    def run_audit(**kwargs: Any) -> None:
        result = audit_vm_lab(
            kwargs["layout"],
            config_file=config_file,
            matrix=parse_matrix(matrix),
            vm_name_pattern=vm_name_pattern,
        )
        if result.get("status") != "passed":
            raise RuntimeError("Windows VM lab audit found stale or missing VM disks.")

    _locked("vm-lab audit", run_audit)(workspace_options=workspace_options, layout=layout)


@vm_lab.command("manual")
@_common_options
@click.option("--config-file", default=None, help="Ignored Windows VM lab JSON config. Defaults to vm-lab.local.json.")
@click.option("--matrix", default="win10", show_default=True, help="Comma-separated Windows VM targets to leave running.")
@click.option(
    "--release-version",
    default=ACTIVE_EMULEBB_RELEASE_VERSION,
    show_default=True,
    help="Release version in MAJOR.MINOR.PATCH[-rc.N|-beta.N|-nightly.YYYYMMDD.SHA] form.",
)
@click.option("--skip-build", is_flag=True, help="Use an existing release package instead of building one first.")
@click.option("--install-root", default=r"C:\eMuleBBManual", show_default=True, help="Guest path for the manual install.")
@click.option("--start-app", is_flag=True, help="Start eMuleBB after staging the manual profile.")
@click.option("--dry-run", is_flag=True, help="Plan the manual VM launch commands without changing VMs.")
def vm_lab_manual(
    *,
    config_file: str | None,
    matrix: str,
    release_version: str,
    skip_build: bool,
    install_root: str,
    start_app: bool,
    dry_run: bool,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Restore prepared VM(s), install eMuleBB, and leave them running."""

    manual_options = VmManualOptions(
        config_file=config_file,
        matrix=parse_matrix(matrix),
        release_version=release_version,
        skip_build=skip_build,
        install_root=install_root,
        start_app=start_app,
        dry_run=dry_run,
    )
    _locked(
        "vm-lab manual",
        lambda **kwargs: launch_manual_vm(kwargs["layout"], kwargs["workspace_options"], manual_options),
    )(workspace_options=workspace_options, layout=layout)




@main.command("package-miniupnpc")
@_common_options
@click.option("--clean", is_flag=True, help="Clean selected package build outputs before building.")
@click.option(
    "--release-version",
    default="2.3.3-emulebb.1",
    show_default=True,
    help="MiniUPnP release version in MAJOR.MINOR.PATCH-emulebb.N form.",
)
def package_miniupnpc(
    *,
    clean: bool,
    release_version: str,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Build the optional MiniUPnP upnpc Windows package artifact."""

    package_options = MiniupnpcPackageOptions(release_version=release_version, clean=clean)
    _locked(
        "package miniupnpc",
        lambda **kwargs: create_miniupnpc_package(kwargs["layout"], kwargs["workspace_options"], package_options),
    )(workspace_options=workspace_options, layout=layout)


@main.command("install-local-package")
@_common_options
@click.option("--clean", is_flag=True, help="Clean selected package build outputs before building.")
@click.option("--skip-build", is_flag=True, help="Deploy existing package artifacts without rebuilding them.")
@click.option("--live-wire-inputs-file", default=None, help="Ignored live-wire JSON containing local_package_install settings.")
@click.option(
    "--package-flavor",
    type=click.Choice(["standard", "diagnostics"]),
    default="standard",
    show_default=True,
    help="eMuleBB release package flavor to install locally.",
)
@click.option(
    "--release-version",
    default=ACTIVE_EMULEBB_RELEASE_VERSION,
    show_default=True,
    help="Release version in MAJOR.MINOR.PATCH[-rc.N|-beta.N|-nightly.YYYYMMDD.SHA] form.",
)
def install_local_package_command(
    *,
    clean: bool,
    skip_build: bool,
    live_wire_inputs_file: str | None,
    package_flavor: str,
    release_version: str,
    workspace_options: WorkspaceOptions,
    layout,
) -> None:
    """Build and refresh a local package install from ignored live-wire JSON."""

    install_options = LocalPackageInstallOptions(
        release_version=release_version,
        clean=clean,
        skip_build=skip_build,
        live_wire_inputs_file=live_wire_inputs_file,
        package_flavor=package_flavor,
    )
    _locked(
        "install local-package",
        lambda **kwargs: install_local_package(kwargs["layout"], kwargs["workspace_options"], install_options),
    )(workspace_options=workspace_options, layout=layout)


@main.command("env-check")
@_common_options
def env_check(*, workspace_options: WorkspaceOptions, layout) -> None:
    """Verify basic tool discovery and manifest loading."""

    from .validation import env_check as run_env_check

    _locked("env-check", lambda **kwargs: run_env_check(kwargs["layout"]))(
        workspace_options=workspace_options,
        layout=layout,
    )
