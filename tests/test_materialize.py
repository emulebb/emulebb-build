from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from emule_workspace import materialize
from emule_workspace.setup_commands import ComparePreset
from emule_workspace.topology import AppRepo, ManagedRepo, WorkspaceTopology


def test_hook_install_adopts_empty_default_directory_but_preserves_custom_hooks(tmp_path, monkeypatch):
    shared = tmp_path / "repos" / "emulebb-tooling" / "hooks"
    shared.mkdir(parents=True)
    for hook in ("pre-commit", "pre-push"):
        (shared / hook).write_text("#!/bin/sh\n")
    repo = tmp_path / "repos" / "emulebb-rust"
    default = repo / ".git" / "hooks"
    default.mkdir(parents=True)
    topology = SimpleNamespace(repos=[SimpleNamespace(name="emulebb-rust", relative_path="repos/emulebb-rust")],
                               app_repo=SimpleNamespace(worktrees=[]))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(materialize, "run_native", run)
    monkeypatch.setattr(materialize, "run_captured", lambda *args, **kwargs: str(default))
    materialize.install_workspace_hooks(tmp_path, topology)
    assert any(command[-2:] == ["core.hooksPath", str(shared)] for command in calls)
    (default / "pre-push").write_text("custom\n")
    with pytest.raises(RuntimeError, match="unmanaged"):
        materialize.install_workspace_hooks(tmp_path, topology)


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


def test_tag_checkout_is_detached_and_revision_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expected_revision = "02db0d7faecfc377694ff6242bc23346185990ed"
    repo = ManagedRepo(
        name="amule-stable",
        url="https://github.com/amule-org/amule.git",
        relative_path="repos\\amule-stable",
        branch="3.0.1",
        checkout_mode="tag",
        expected_revision=expected_revision,
    )
    repo_path = tmp_path / "repos" / "amule-stable"
    native_calls: list[tuple[str, ...]] = []
    clean_calls: list[tuple[Path, str, Path]] = []
    monkeypatch.setattr(materialize, "branch_ref_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(materialize, "assert_clean_repo", lambda *args: clean_calls.append(args))
    monkeypatch.setattr(
        materialize,
        "run_native",
        lambda command, **_kwargs: native_calls.append(tuple(str(part) for part in command)),
    )
    monkeypatch.setattr(materialize, "run_captured", lambda *_args, **_kwargs: expected_revision)

    materialize.ensure_managed_repo_tag(tmp_path, repo_path, repo)

    assert clean_calls == [(repo_path, "amule-stable", tmp_path)]
    assert native_calls == [("git", "-C", str(repo_path), "checkout", "--detach", "refs/tags/3.0.1")]


def test_tag_checkout_rejects_unexpected_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = ManagedRepo(
        name="amule-stable",
        url="https://github.com/amule-org/amule.git",
        relative_path="repos\\amule-stable",
        branch="3.0.1",
        checkout_mode="tag",
        expected_revision="02db0d7faecfc377694ff6242bc23346185990ed",
    )
    monkeypatch.setattr(materialize, "branch_ref_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(materialize, "assert_clean_repo", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(materialize, "run_native", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(materialize, "run_captured", lambda *_args, **_kwargs: "0" * 40)

    with pytest.raises(RuntimeError, match="expected 02db0d7"):
        materialize.ensure_managed_repo_tag(tmp_path, tmp_path / "repos" / "amule-stable", repo)


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


def test_compare_launcher_sync_removes_stale_generated_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compare_root = tmp_path / "analysis" / "compare"
    stale_directory = compare_root / "retired-source"
    stale_directory.mkdir(parents=True)
    (compare_root / "retired-source-vs-main.cmd").write_text("stale\n", encoding="utf-8")
    (stale_directory / "retired-source-vs-main.cmd").write_text("stale\n", encoding="utf-8")
    preserved_file = compare_root / "README.txt"
    preserved_file.write_text("operator notes\n", encoding="utf-8")
    topology = WorkspaceTopology(
        profiles=("core",),
        root_directories=(),
        app_repo=AppRepo(
            name="emulebb",
            url="https://example.invalid/emulebb.git",
            relative_path="repos\\emulebb",
            branch="main",
        ),
        repos=(),
        analysis_repos=(),
        third_party_repos=(),
    )
    presets = (
        ComparePreset(
            key="emule-stock-vs-local-072-main",
            label="Stock eMule 0.72 vs local-072-main",
            category="Stock eMule 0.72 vs local",
            left_name="emule-stock",
            right_name="local-072-main",
        ),
    )
    monkeypatch.setattr("emule_workspace.setup_commands.compare_presets", lambda _root, _topology: presets)

    materialize.write_compare_launchers(tmp_path, topology)

    assert not (compare_root / "retired-source-vs-main.cmd").exists()
    assert not stale_directory.exists()
    assert preserved_file.read_text(encoding="utf-8") == "operator notes\n"
    assert (compare_root / "open-compare-menu.cmd").is_file()
    assert (compare_root / "emule-stock-vs-local-072-main.cmd").is_file()


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
