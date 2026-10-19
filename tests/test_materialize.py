from __future__ import annotations

from pathlib import Path

import pytest

from emule_workspace import materialize
from emule_workspace.topology import AppRepo, ManagedRepo, WorkspaceTopology


def test_bootstrap_root_allows_only_required_build_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "workspace"
    build_repo = root / "repos" / "emulebb-build"
    build_repo.mkdir(parents=True)
    monkeypatch.setattr(materialize, "build_repo_root", lambda: build_repo)

    materialize.assert_materialize_bootstrap_root(root)

    unexpected_repo = root / "repos" / "emulebb-tooling"
    unexpected_repo.mkdir()
    with pytest.raises(RuntimeError, match="Refusing populated root"):
        materialize.assert_materialize_bootstrap_root(root)


def test_bootstrap_root_rejects_unexpected_top_level_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "workspace"
    build_repo = root / "repos" / "emulebb-build"
    build_repo.mkdir(parents=True)
    (root / "notes.txt").write_text("not part of bootstrap\n", encoding="utf-8")
    monkeypatch.setattr(materialize, "build_repo_root", lambda: build_repo)

    with pytest.raises(RuntimeError, match="notes.txt"):
        materialize.assert_materialize_bootstrap_root(root)


def test_assert_clean_repo_rejects_local_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(materialize, "run_captured", lambda *args, **kwargs: " M README.md\n")

    with pytest.raises(RuntimeError, match="local changes"):
        materialize.assert_clean_repo(tmp_path / "repo", "demo", tmp_path)


def test_optional_missing_branch_leaves_checkout_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []
    repo = ManagedRepo(
        name="optional-reference",
        url="https://example.invalid/optional-reference.git",
        relative_path="repos\\optional-reference",
        branch="main",
        branch_optional=True,
    )
    monkeypatch.setattr(materialize, "branch_ref_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(materialize, "assert_clean_repo", lambda *args, **kwargs: calls.append(args))

    materialize.ensure_managed_repo_branch(tmp_path, tmp_path / "optional-reference", repo)

    assert calls == []


def test_print_repo_provenance_reports_head_and_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    calls: list[tuple[str, ...]] = []

    def fake_run_captured(command, **_kwargs):
        calls.append(tuple(str(part) for part in command))
        return "abc123\n" if command[-1] == "HEAD" else "main\n"

    monkeypatch.setattr(materialize, "run_captured", fake_run_captured)

    materialize.print_repo_provenance(tmp_path, repo, "emulebb-build-tests")

    assert calls == [
        ("git", "-C", str(repo), "rev-parse", "HEAD"),
        ("git", "-C", str(repo), "branch", "--show-current"),
    ]
    assert "Managed repository 'emulebb-build-tests' at abc123 (main)." in capsys.readouterr().out


def test_seed_overlay_tracks_and_removes_stale_seed_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    topology = WorkspaceTopology(
        profiles=("core",),
        root_directories=(),
        app_repo=AppRepo(name="emulebb", url="https://example.invalid/emulebb.git", relative_path="repos\\emulebb", branch="main"),
        repos=(),
        analysis_repos=(),
        third_party_repos=(
            ManagedRepo(
                name="eMule-demo-lib",
                url="https://example.invalid/demo.git",
                relative_path="repos\\third_party\\eMule-demo-lib",
                branch="main",
            ),
        ),
    )
    root = tmp_path / "workspace"
    output_root = tmp_path / "output"
    output_root.mkdir(parents=True)
    monkeypatch.setenv("EMULEBB_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("EMULEBB_WORKSPACE_OUTPUT_ROOT", str(output_root))
    seed_root = tmp_path / "seed"
    source = seed_root / "eMule-demo-lib"
    destination = root / "repos" / "third_party" / "eMule-demo-lib"
    source_file = source / "bin" / "seed.dll"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("one\n", encoding="utf-8")
    preserved_file = destination / "src" / "preserved.cpp"
    preserved_file.parent.mkdir(parents=True)
    preserved_file.write_text("keep\n", encoding="utf-8")

    materialize.overlay_seed_artifacts(root, topology, str(seed_root), "workspace")

    assert (destination / "bin" / "seed.dll").read_text(encoding="utf-8") == "one\n"
    assert (output_root / "artifacts" / "materialize" / "workspace" / "artifact-seed-overlays.json").is_file()
    assert not (root / "workspaces" / "workspace" / "state" / "artifact-seed-overlays.json").exists()
    source_file.unlink()
    replacement_file = source / "include" / "seed.h"
    replacement_file.parent.mkdir(parents=True)
    replacement_file.write_text("#pragma once\n", encoding="utf-8")

    materialize.overlay_seed_artifacts(root, topology, str(seed_root), "workspace")

    assert not (destination / "bin" / "seed.dll").exists()
    assert (destination / "include" / "seed.h").read_text(encoding="utf-8") == "#pragma once\n"
    assert preserved_file.read_text(encoding="utf-8") == "keep\n"
