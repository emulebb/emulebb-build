from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import os
import subprocess

import pytest

from emule_workspace import test_runs
from emule_workspace import process
from emule_workspace.layout import TestTargets as LayoutTestTargets, WorkspaceLayout


def make_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkspaceLayout:
    workspace_root = tmp_path / "workspace-root"
    output_root = tmp_path / "workspace-output"
    rust_root = workspace_root / "repos" / "emulebb-rust"
    (output_root / "builds" / "rust" / "target").mkdir(parents=True)
    rust_root.mkdir(parents=True)
    monkeypatch.setenv("EMULEBB_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("EMULEBB_WORKSPACE_OUTPUT_ROOT", str(output_root))
    monkeypatch.setenv("CARGO_TARGET_DIR", str(output_root / "builds" / "rust" / "target"))
    return WorkspaceLayout(
        emule_workspace_root=workspace_root,
        workspace_name="workspace",
        workspace_root=workspace_root / "workspaces" / "workspace",
        build_repo_root=workspace_root / "repos" / "emulebb-build",
        tests_repo_root=workspace_root / "repos" / "emulebb-build-tests",
        tooling_repo_root=workspace_root / "repos" / "emulebb-tooling",
        ed2k_server_repo_root=workspace_root / "repos" / "ed2k-server",
        amule_stable_repo_root=workspace_root / "repos" / "amule-stable",
        amule_head_repo_root=workspace_root / "repos" / "amule",
        seed_repo_path=workspace_root / "repos" / "emulebb",
        seed_repo_branch="main",
        dependencies=(),
        app_variants=(),
        test_targets=LayoutTestTargets(
            test_build_variant="main",
            test_run_variant="main",
            baseline_variant="community",
        ),
        toolset_override_variable="EMULEBB_VS_PLATFORM_TOOLSET",
        emulebb_rust_repo_root=rust_root,
        output_root=output_root,
    )


@pytest.mark.parametrize("exit_code", [0, 7])
def test_rust_webui_delegates_to_selected_persisted_owner_and_preserves_environment_and_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
) -> None:
    layout = make_layout(tmp_path, monkeypatch)
    script = layout.emulebb_rust_repo_root / "tools/test_webui.py"
    script.parent.mkdir()
    script.write_text("# Synthetic selected source owner\n", encoding="utf-8")
    selected_python = tmp_path / "selected-python.exe"
    monkeypatch.setattr(test_runs, "get_python_invocation", lambda: process.PythonInvocation(selected_python, ("-I",)))
    monkeypatch.setenv("OPERATOR_SENTINEL", "preserved")
    before = os.environ.copy()
    calls = []
    def execute(command, *, cwd, env, text, check):
        calls.append(command)
        assert command == [str(selected_python), "-I", "-B", str(script)]
        assert cwd == str(layout.emulebb_rust_repo_root) and text is True and check is False
        assert env["OPERATOR_SENTINEL"] == "preserved"
        for name in ("EMULEBB_WORKSPACE_ROOT", "EMULEBB_WORKSPACE_OUTPUT_ROOT", "CARGO_TARGET_DIR"):
            assert env[name] == before[name]
        return subprocess.CompletedProcess(command, exit_code)
    monkeypatch.setattr(process.subprocess, "run", execute)
    if exit_code:
        with pytest.raises(RuntimeError, match="Rust WebUI tests failed with exit code 7"):
            test_runs.invoke_rust_webui_tests(layout)
    else:
        test_runs.invoke_rust_webui_tests(layout)
    assert len(calls) == 1 and os.environ == before


def test_rust_webui_requires_selected_rust_owner_before_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="persisted Rust tools/test_webui.py"):
        test_runs.invoke_rust_webui_tests(layout)
    with pytest.raises(RuntimeError, match="emulebb-rust repository"):
        test_runs.invoke_rust_webui_tests(replace(layout, emulebb_rust_repo_root=None))
