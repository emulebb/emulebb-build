from __future__ import annotations

from pathlib import Path

import pytest

from emule_workspace import setup_commands
from emule_workspace.topology import AppRepo, ManagedRepo, WorkspaceTopology, canonical_topology


def test_compare_presets_include_current_amule_and_stock_references(tmp_path: Path) -> None:
    preset_keys = {
        preset.key for preset in setup_commands.compare_presets(tmp_path, canonical_topology())
    }

    assert preset_keys == {
        "amule-vs-local-072-community",
        "amule-vs-local-072-main",
        "amule-vs-local-072-tracing-harness",
        "emule-stock-vs-local-072-community",
        "emule-stock-vs-local-072-main",
        "emule-stock-vs-local-072-tracing-harness",
    }


def test_dependency_update_report_writes_under_output_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_root = tmp_path / "workspace"
    output_root = tmp_path / "output"
    (workspace_root / "workspaces" / "workspace").mkdir(parents=True)
    output_root.mkdir(parents=True)
    monkeypatch.setenv("EMULEBB_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("EMULEBB_WORKSPACE_OUTPUT_ROOT", str(output_root))
    topology = WorkspaceTopology(
        profiles=("core",),
        root_directories=(),
        app_repo=AppRepo(name="emulebb", url="https://example.invalid/emulebb.git", relative_path="repos\\emulebb", branch="main"),
        repos=(),
        analysis_repos=(),
        third_party_repos=(
            ManagedRepo(
                name="demo-lib",
                url="https://example.invalid/demo.git",
                relative_path="repos\\third_party\\demo-lib",
                branch="main",
            ),
        ),
    )
    monkeypatch.setattr(setup_commands, "resolve_setup_workspace_root", lambda value=None: workspace_root)
    monkeypatch.setattr(setup_commands, "canonical_topology", lambda: topology)
    monkeypatch.setattr(setup_commands, "_dependency_update_entry", lambda repo: {"name": repo.name, "status": "skipped"})

    setup_commands.write_dependency_update_report(workspace_root=str(workspace_root), workspace_name="workspace")

    report_root = output_root / "reports" / "dep-updates"
    assert (report_root / "latest-summary.json").is_file()
    assert not (workspace_root / "workspaces" / "workspace" / "state" / "dep-updates").exists()
