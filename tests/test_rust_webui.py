from __future__ import annotations

from pathlib import Path

import pytest

from emule_workspace import test_runs
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
        ed2k_server_repo_root=workspace_root / "repos" / "goed2k-server",
        amule_repo_root=workspace_root / "repos" / "amule",
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


def test_rust_webui_runs_locked_tests_and_build_from_external_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path, monkeypatch)
    source = layout.emulebb_rust_repo_root / "webui"
    source.mkdir()
    (source / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (source / "package.json").write_text("{}\n", encoding="utf-8")
    (source / "node_modules").mkdir()
    (source / "node_modules" / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    npm = tmp_path / "npm.cmd"
    npm.write_bytes(b"")
    calls: list[tuple[list[object], str, Path, dict[str, object]]] = []

    monkeypatch.setattr(test_runs, "find_tool", lambda _names: npm)

    def fake_run_native(command, *, label, cwd, env=None, allow_failure=False):
        calls.append((list(command), label, Path(cwd), dict(env or {})))

    monkeypatch.setattr(test_runs, "run_native", fake_run_native)

    test_runs.invoke_rust_webui_tests(layout)

    staged = layout.output_build_root / "emulebb-rust" / "webui-test-source"
    dist = layout.output_build_root / "emulebb-rust" / "webui-test-dist"
    assert (staged / "package-lock.json").is_file()
    assert not (staged / "node_modules").exists()
    assert [call[1] for call in calls] == [
        "Rust WebUI locked install",
        "Rust WebUI unit tests",
        "Rust WebUI Playwright tests",
        "Rust WebUI typecheck",
        "Rust WebUI production build",
    ]
    assert calls[0][0] == [npm, "ci", "--ignore-scripts"]
    assert calls[-1][0] == [npm, "run", "build", "--", "--outDir", dist]
    assert all(call[2] == staged for call in calls)
    assert all(call[3]["NPM_CONFIG_CACHE"] == layout.output_cache_root / "npm" for call in calls)


def test_rust_webui_requires_package_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path, monkeypatch)
    (layout.emulebb_rust_repo_root / "webui").mkdir()
    monkeypatch.setattr(test_runs, "find_tool", lambda _names: tmp_path / "npm.cmd")

    with pytest.raises(RuntimeError, match="package lock"):
        test_runs.invoke_rust_webui_tests(layout)
