from __future__ import annotations

from pathlib import Path

import pytest

from emule_workspace.config import AmutorrentCleanStartupOptions, WorkspaceOptions
from emule_workspace.layout import AppVariant, TestTargets as LayoutTestTargets, WorkspaceLayout
from emule_workspace import network_context, test_runs


def make_layout(tmp_path: Path) -> WorkspaceLayout:
    emule_workspace_root = tmp_path
    workspace_root = emule_workspace_root / "workspaces" / "workspace"
    tests_repo_root = emule_workspace_root / "repos" / "emulebb-build-tests"
    app_root = workspace_root / "app" / "emulebb-main"
    (tests_repo_root / "scripts").mkdir(parents=True)
    (tests_repo_root / "scripts" / "amutorrent-clean-startup.py").write_text("# test runner\n", encoding="utf-8")
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
    return [command[index + 1] for index, value in enumerate(command[:-1]) if value == option]


@pytest.fixture(autouse=True)
def workspace_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_root = tmp_path.parent / f"{tmp_path.name}-output"
    cargo_target_dir = output_root / "builds" / "rust" / "target"
    cargo_target_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EMULEBB_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("EMULEBB_WORKSPACE_OUTPUT_ROOT", str(output_root))
    monkeypatch.setenv("CARGO_TARGET_DIR", str(cargo_target_dir))


def test_amutorrent_clean_startup_forwards_live_options(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)
        captured["label"] = label
        captured["cwd"] = cwd
        captured["env"] = dict(env or {})

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setattr(test_runs, "ensure_split_tunnel_apps", lambda paths, **_kwargs: {"enabled": False})
    monkeypatch.setenv(network_context.VPN_IP_ENV, "10.8.0.4")
    monkeypatch.setenv(network_context.LAN_IP_ENV, "192.168.56.11")
    monkeypatch.setenv(network_context.X_LOCAL_IP_ENV, "192.168.56.11")

    test_runs.invoke_amutorrent_clean_startup(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        AmutorrentCleanStartupOptions(
            live_wire_inputs_file="inputs.json",
            rest_webserver_scheme="https",
            keep_artifacts=True,
            ready_timeout_seconds=11.0,
            network_ready_timeout_seconds=22.0,
            search_observation_timeout_seconds=33.0,
            p2p_bind_interface_name="hide.me",
        ),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert command[1].endswith("amutorrent-clean-startup.py")
    assert captured["label"] == "aMuTorrent clean startup"
    assert option_values(command, "--live-wire-inputs-file") == ["inputs.json"]
    assert option_values(command, "--rest-webserver-scheme") == ["https"]
    assert option_values(command, "--ready-timeout-seconds") == ["11.0"]
    assert option_values(command, "--network-ready-timeout-seconds") == ["22.0"]
    assert option_values(command, "--search-observation-timeout-seconds") == ["33.0"]
    assert option_values(command, "--p2p-bind-interface-name") == ["hide.me"]
    assert option_values(command, "--lan-bind-addr") == ["192.168.56.11"]
    assert captured["env"]["X_LOCAL_IP"] == "192.168.56.11"
    assert "--keep-artifacts" in command


def test_amutorrent_clean_startup_omits_optional_inputs_by_default(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setattr(test_runs, "ensure_split_tunnel_apps", lambda paths, **_kwargs: {"enabled": False})
    monkeypatch.setenv(network_context.VPN_IP_ENV, "10.8.0.4")
    monkeypatch.setenv(network_context.LAN_IP_ENV, "192.168.56.11")
    monkeypatch.setenv(network_context.X_LOCAL_IP_ENV, "192.168.56.11")

    test_runs.invoke_amutorrent_clean_startup(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        AmutorrentCleanStartupOptions(),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--live-wire-inputs-file" not in command
    assert option_values(command, "--rest-webserver-scheme") == ["https"]
    assert option_values(command, "--lan-bind-addr") == ["192.168.56.11"]
    assert "--keep-artifacts" not in command
