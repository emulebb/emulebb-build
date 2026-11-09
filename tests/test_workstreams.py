"""Exercise real Git worktree boundaries using local disposable repositories."""

from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from emule_workspace import workstreams as ws


@dataclass
class Layout:
    emule_workspace_root: Path
    output_root: Path
    build_repo_root: Path
    tooling_repo_root: Path
    tests_repo_root: Path
    emulebb_rust_repo_root: Path


@pytest.fixture
def layout(tmp_path, monkeypatch):
    root = tmp_path / "source"
    anchors = {}
    for name in ws.REPOSITORY_FIELDS:
        remote = tmp_path / "remotes" / f"{name}.git"
        remote.mkdir(parents=True)
        ws._git(remote, "init", "--bare", "--initial-branch=main")
        repo = root / "repos" / name
        repo.mkdir(parents=True)
        ws._git(repo, "init", "--initial-branch=main")
        ws._git(repo, "config", "user.email", "fixture@example.invalid")
        ws._git(repo, "config", "user.name", "Fixture Author")
        ws._git(repo, "config", "core.hooksPath", str(tmp_path / "empty-hooks"))
        (repo / "sample.txt").write_text("base\n", encoding="utf-8")
        ws._git(repo, "add", "sample.txt")
        ws._git(repo, "commit", "-m", "fixture base")
        ws._git(repo, "remote", "add", "origin", str(remote))
        ws._git(repo, "push", "-u", "origin", "main")
        anchors[ws.REPOSITORY_FIELDS[name]] = repo
    monkeypatch.setattr(ws, "_open_pull_requests", lambda entry: [])
    return Layout(root, tmp_path / "output", **anchors)


def create(layout, selected="alpha", repositories=("emulebb-rust",)):
    return ws.create_workstream(layout, selected, f"chore/{selected}", "parent", repositories)


def test_parallel_checkouts_keep_independent_indexes_branches_and_canonical_state(layout):
    first = create(layout)
    second = create(layout, "beta")
    a = Path(first["repositories"]["emulebb-rust"]["path"])
    b = Path(second["repositories"]["emulebb-rust"]["path"])
    (a / "sample.txt").write_text("alpha\n", encoding="utf-8")
    ws._git(a, "add", "sample.txt")
    ws._git(a, "commit", "-m", "alpha outcome")
    assert (b / "sample.txt").read_text() == "base\n"
    assert not ws._git(b, "status", "--porcelain")
    assert ws._git(layout.emulebb_rust_repo_root, "branch", "--show-current") == "main"
    assert ws.resolve_workstream(layout.emule_workspace_root, layout.output_root, a)["id"] == "alpha"


def test_add_repository_preserves_existing_head_and_selects_both_sources(layout):
    registration = create(layout, repositories=("emulebb-rust", "emulebb-build"))
    original = registration["repositories"]["emulebb-rust"]["base_revision"]
    updated = ws.add_repository(layout, "alpha", "emulebb-tooling")
    selected = ws.apply_workstream_layout(layout, updated)
    assert selected.tooling_repo_root == Path(updated["repositories"]["emulebb-tooling"]["path"])
    assert selected.emulebb_rust_repo_root == Path(updated["repositories"]["emulebb-rust"]["path"])
    assert selected.output_root == layout.output_root
    assert updated["repositories"]["emulebb-rust"]["base_revision"] == original
    assert ws._sources(selected)["emulebb-build"] == layout.build_repo_root
    assert ws.resolve_workstream(layout.emule_workspace_root, layout.output_root,
                                 layout.emulebb_rust_repo_root) is None


def test_duplicate_id_existing_path_and_branch_are_not_adopted(layout):
    create(layout)
    with pytest.raises(RuntimeError, match="already registered"):
        create(layout)
    with pytest.raises(RuntimeError, match="Branch already exists"):
        ws.create_workstream(layout, "beta", "chore/alpha", "parent", ("emulebb-rust",))
    path = layout.emule_workspace_root / "workspaces" / "agents" / "gamma" / "emulebb-rust"
    path.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="refusing adoption"):
        create(layout, "gamma")


def test_interrupted_creation_records_incomplete_state_without_adopting_it(layout, monkeypatch):
    original = ws._git
    def fail_add(repo, *args, **kwargs):
        if args[:2] == ("worktree", "add"):
            raise RuntimeError("injected interruption")
        return original(repo, *args, **kwargs)
    monkeypatch.setattr(ws, "_git", fail_add)
    with pytest.raises(RuntimeError, match="injected interruption"):
        create(layout)
    status = ws.workstream_status(layout, "alpha")
    assert status[0]["verification"] == "invalid"
    assert "incomplete" in status[0]["error"]
    with pytest.raises(RuntimeError, match="already registered"):
        create(layout)


def test_resolve_rejects_changed_branch_and_foreign_repository(layout):
    registration = create(layout)
    path = Path(registration["repositories"]["emulebb-rust"]["path"])
    with pytest.raises(RuntimeError, match="does not belong"):
        ws.resolve_workstream(layout.emule_workspace_root, layout.output_root,
                              layout.tooling_repo_root, "alpha")
    ws._git(path, "checkout", "--detach")
    with pytest.raises(RuntimeError, match="branch changed"):
        ws.resolve_workstream(layout.emule_workspace_root, layout.output_root, path)


def test_cleanup_refuses_dirty_unpublished_open_pr_and_wrong_owner(layout, monkeypatch):
    registration = create(layout)
    path = Path(registration["repositories"]["emulebb-rust"]["path"])
    with pytest.raises(RuntimeError, match="assigned parent"):
        ws.remove_workstream(layout, "alpha", "worker")
    (path / "sample.txt").write_text("new\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Dirty"):
        ws.remove_workstream(layout, "alpha", "parent")
    ws._git(path, "add", "sample.txt")
    ws._git(path, "commit", "-m", "new outcome")
    with pytest.raises(RuntimeError, match="Unpublished"):
        ws.remove_workstream(layout, "alpha", "parent")
    ws._git(path, "push", "-u", "origin", "chore/alpha")
    with pytest.raises(RuntimeError, match="Unmerged"):
        ws.remove_workstream(layout, "alpha", "parent")
    monkeypatch.setattr(ws, "_open_pull_requests", lambda entry: [{"number": 1}])
    with pytest.raises(RuntimeError, match="Open pull"):
        ws.remove_workstream(layout, "alpha", "parent", abandoned=True)
    assert path.is_dir()


def test_explicit_abandonment_removes_checkout_but_preserves_published_branch(layout):
    registration = create(layout)
    path = Path(registration["repositories"]["emulebb-rust"]["path"])
    (path / "sample.txt").write_text("published outcome\n", encoding="utf-8")
    ws._git(path, "add", "sample.txt")
    ws._git(path, "commit", "-m", "published outcome")
    ws._git(path, "push", "-u", "origin", "chore/alpha")
    head = ws._git(path, "rev-parse", "HEAD")
    ws.remove_workstream(layout, "alpha", "parent", abandoned=True)
    assert not path.exists()
    assert ws._git(layout.emulebb_rust_repo_root, "rev-parse", "chore/alpha") == head
    assert not ws.workstream_status(layout)


def test_corrupt_registration_and_path_escape_are_reported(layout):
    create(layout)
    manifest = layout.output_root / "tmp" / "workstreams" / "alpha.json"
    value = json.loads(manifest.read_text())
    value["repositories"]["emulebb-rust"]["path"] = str(layout.emulebb_rust_repo_root)
    manifest.write_text(json.dumps(value), encoding="utf-8")
    assert ws.workstream_status(layout, "alpha")[0]["verification"] == "invalid"
    with pytest.raises(ValueError, match="IDs"):
        create(layout, "../outside")


def test_bootstrap_requires_exact_identity_and_explicit_extension(layout):
    registration = create(layout)
    manifest = layout.output_root / "tmp" / "workstreams" / "alpha.json"
    manifest.unlink()
    base = registration["repositories"]["emulebb-rust"]["base_revision"]
    selected = ws.register_bootstrap(layout, "alpha", "chore/alpha", "parent", {"emulebb-rust": base})
    with pytest.raises(RuntimeError, match="already registered"):
        ws.register_bootstrap(layout, "alpha", "chore/alpha", "parent", {"emulebb-rust": base})
    with pytest.raises(RuntimeError, match="different owner"):
        ws.register_bootstrap(layout, "alpha", "chore/alpha", "worker", {"emulebb-rust": base}, extend=True)
    extended = ws.register_bootstrap(layout, "alpha", "chore/alpha", "parent", {"emulebb-rust": base}, extend=True)
    assert extended == selected


def test_cleanup_accepts_rebased_merge_without_deleting_branch(layout):
    registration = create(layout)
    path = Path(registration["repositories"]["emulebb-rust"]["path"])
    (path / "sample.txt").write_text("completed outcome\n", encoding="utf-8")
    ws._git(path, "add", "sample.txt")
    ws._git(path, "commit", "-m", "completed outcome")
    original_head = ws._git(path, "rev-parse", "HEAD")
    anchor = layout.emulebb_rust_repo_root
    (anchor / "other.txt").write_text("independent main change\n", encoding="utf-8")
    ws._git(anchor, "add", "other.txt")
    ws._git(anchor, "commit", "-m", "independent main change")
    ws._git(anchor, "cherry-pick", original_head)
    ws._git(anchor, "push", "origin", "main")
    ws.remove_workstream(layout, "alpha", "parent")
    assert not path.exists()
    assert ws._git(anchor, "rev-parse", "chore/alpha") == original_head


def test_cleanup_preserves_ignored_operator_files(layout):
    registration = create(layout)
    path = Path(registration["repositories"]["emulebb-rust"]["path"])
    (path / ".gitignore").write_text("operator-state.txt\n", encoding="utf-8")
    ws._git(path, "add", ".gitignore")
    ws._git(path, "commit", "-m", "fixture ignore rule")
    state = path / "operator-state.txt"
    state.write_text("operator state\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Dirty"):
        ws.remove_workstream(layout, "alpha", "parent")
    assert state.read_text(encoding="utf-8") == "operator state\n"
