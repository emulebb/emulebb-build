from __future__ import annotations

from pathlib import Path

import pytest

from emule_workspace import test_runs
from emule_workspace.config import VariantComparisonOptions, WorkspaceOptions
from emule_workspace.layout import AppVariant, TestTargets as LayoutTestTargets, WorkspaceLayout, get_test_build_tag


def make_layout(tmp_path: Path) -> WorkspaceLayout:
    """Builds a minimal layout for protocol parity command orchestration tests."""

    emule_workspace_root = tmp_path
    workspace_root = emule_workspace_root / "workspaces" / "workspace"
    tests_repo_root = emule_workspace_root / "repos" / "emulebb-build-tests"
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
        app_variants=(
            AppVariant(name="main", path=workspace_root / "app" / "emulebb-main", branch="main"),
            AppVariant(
                name="community",
                path=workspace_root / "app" / "emulebb-community-baseline",
                branch="baseline/community-0.72a",
            ),
        ),
        test_targets=LayoutTestTargets(test_build_variant="main", test_run_variant="main", baseline_variant="community"),
        toolset_override_variable="",
        output_root=emule_workspace_root.parent / f"{emule_workspace_root.name}-output",
    )


@pytest.fixture(autouse=True)
def workspace_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_root = tmp_path.parent / f"{tmp_path.name}-output"
    cargo_target_dir = output_root / "builds" / "rust" / "target"
    cargo_target_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EMULEBB_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("EMULEBB_WORKSPACE_OUTPUT_ROOT", str(output_root))
    monkeypatch.setenv("CARGO_TARGET_DIR", str(cargo_target_dir))


def test_protocol_parity_runs_surface_goldens_then_live_diff(tmp_path: Path, monkeypatch) -> None:
    commands: list[list[object]] = []
    labels: list[str] = []

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        commands.append(list(command))
        labels.append(label)

    layout = make_layout(tmp_path)
    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_protocol_parity(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, configuration="Release", platform="x64"),
        VariantComparisonOptions(),
    )

    assert labels == [
        "protocol surface diff main vs community",
        "protocol oracle golden validation",
        "protocol parity live diff main vs community",
    ]
    assert any(str(part).endswith("run-protocol-surface-diff.py") for part in commands[0])
    assert any(str(part).endswith("validate-protocol-goldens.py") for part in commands[1])
    assert any(str(part).endswith("run-live-diff.py") for part in commands[2])
    assert "--suite-name" in commands[2]
    assert "protocol-parity" in commands[2]


def test_native_test_suites_run_output_root_binary(tmp_path: Path, monkeypatch) -> None:
    commands: list[list[object]] = []
    labels: list[str] = []
    layout = make_layout(tmp_path)
    binary_path = (
        layout.output_build_root
        / "tests"
        / get_test_build_tag(layout.workspace_root, layout.get_app_variant("main").path)
        / "x64"
        / "Release"
        / "bin"
        / "emule-tests.exe"
    )
    binary_path.parent.mkdir(parents=True)
    binary_path.write_bytes(b"")

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        commands.append(list(command))
        labels.append(label)

    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_native_test_suites(
        layout,
        WorkspaceOptions(workspace_root=tmp_path, configuration="Release", platform="x64"),
        test_run_variant="main",
        suite_names=("parity",),
    )

    assert commands == [[binary_path, "--test-suite=parity"]]
    assert labels == ["parity tests main Release/x64"]
