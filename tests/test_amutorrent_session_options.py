from __future__ import annotations

from pathlib import Path

import pytest

from emule_workspace.config import AmutorrentSessionOptions, WorkspaceOptions
from emule_workspace.layout import AppVariant, TestTargets as LayoutTestTargets, WorkspaceLayout
from emule_workspace import network_context, test_runs


def make_layout(tmp_path: Path) -> WorkspaceLayout:
    emule_workspace_root = tmp_path
    workspace_root = emule_workspace_root / "workspaces" / "workspace"
    tests_repo_root = emule_workspace_root / "repos" / "emulebb-build-tests"
    app_root = workspace_root / "app" / "emulebb-main"
    rust_repo_root = emule_workspace_root / "repos" / "emulebb-rust"
    (tests_repo_root / "scripts").mkdir(parents=True)
    (tests_repo_root / "scripts" / "amutorrent-interactive-session.py").write_text("# test runner\n", encoding="utf-8")
    app_root.mkdir(parents=True)
    rust_repo_root.mkdir(parents=True)
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
        emulebb_rust_repo_root=rust_repo_root,
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


def test_amutorrent_session_rust_backend_forwards_package_and_lan_bind(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)
        captured["label"] = label
        captured["cwd"] = cwd
        captured["env"] = dict(env or {})

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setenv(network_context.LAN_IP_ENV, "192.168.56.11")
    monkeypatch.setenv(network_context.X_LOCAL_IP_ENV, "192.168.56.11")

    test_runs.invoke_amutorrent_interactive_session(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        AmutorrentSessionOptions(backend="rust", live_network=True),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert command[1].endswith("amutorrent-interactive-session.py")
    assert captured["label"] == "aMuTorrent interactive session"
    assert option_values(command, "--backend") == ["rust"]
    assert option_values(command, "--rust-repo") == [str(layout.emulebb_rust_repo_root)]
    assert option_values(command, "--rust-exe") == [
        str(layout.output_tools_root / "emulebb-rust" / "bin" / "emulebb-rust.exe")
    ]
    assert option_values(command, "--lan-bind-addr") == ["192.168.56.11"]
    assert "--live-network" in command
    assert captured["env"]["X_LOCAL_IP"] == "192.168.56.11"
    assert captured["env"]["EMULEBB_WORKSPACE_OUTPUT_ROOT"] == str(layout.output_root)
    assert captured["env"]["CARGO_TARGET_DIR"] == str(layout.output_rust_target_root)


def test_amutorrent_session_native_backend_forwards_app_root_and_lan_bind(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        captured["command"] = list(command)
        captured["env"] = dict(env or {})

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setenv(network_context.LAN_IP_ENV, "192.168.56.11")
    monkeypatch.setenv(network_context.X_LOCAL_IP_ENV, "192.168.56.11")

    test_runs.invoke_amutorrent_interactive_session(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, platform="x64"),
        AmutorrentSessionOptions(),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert option_values(command, "--backend") == ["native"]
    assert option_values(command, "--app-root") == [str(layout.get_app_variant("main").path)]
    assert option_values(command, "--lan-bind-addr") == ["192.168.56.11"]
    assert "--rust-repo" not in command
    assert captured["env"]["X_LOCAL_IP"] == "192.168.56.11"
