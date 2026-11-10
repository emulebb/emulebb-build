"""Exercise real Git worktree boundaries using local disposable repositories."""

from dataclasses import dataclass, replace
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from emule_workspace import workstreams as ws
from emule_workspace.git_environment import git_environment


@dataclass
class Layout:
    emule_workspace_root: Path
    output_root: Path
    build_repo_root: Path
    tooling_repo_root: Path
    tests_repo_root: Path
    emulebb_rust_repo_root: Path
    emulebb_pages_repo_root: Path | None = None
    emulebb_org_profile_repo_root: Path | None = None

    @property
    def output_tmp_root(self):
        return self.output_root / "tmp"


@pytest.fixture
def layout(tmp_path, monkeypatch):
    root = tmp_path / "source"
    anchors = {}
    for name in ws.CORE_REPOSITORY_FIELDS:
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


def optional_anchor(layout, tmp_path, name):
    repo = layout.emule_workspace_root / "repos" / name
    remote = tmp_path / "remotes" / f"{name}.git"
    remote.mkdir(parents=True)
    ws._git(remote, "init", "--bare", "--initial-branch=main")
    repo.mkdir(parents=True)
    ws._git(repo, "init", "--initial-branch=main")
    ws._git(repo, "config", "user.name", "Fixture Author")
    ws._git(repo, "config", "user.email", "fixture@example.invalid")
    ws._git(repo, "config", "core.hooksPath", str(tmp_path / "empty-hooks"))
    (repo / "sample.txt").write_text("optional base\n", encoding="utf-8")
    ws._git(repo, "add", "sample.txt")
    ws._git(repo, "commit", "-m", "fixture optional base")
    ws._git(repo, "remote", "add", "origin", str(remote))
    ws._git(repo, "push", "-u", "origin", "main")
    hooks = layout.tooling_repo_root / "hooks"
    hooks.mkdir(exist_ok=True)
    for hook in ("pre-commit", "pre-push"):
        (hooks / hook).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ws._git(repo, "config", "core.hooksPath", str(hooks))
    return replace(layout, **{ws.OPTIONAL_REPOSITORY_FIELDS[name]: repo})


@pytest.mark.parametrize("name", tuple(ws.OPTIONAL_REPOSITORY_FIELDS))
def test_optional_creation_requires_topology_and_hooks_and_preserves_canonical(layout, tmp_path, name):
    admitted = optional_anchor(layout, tmp_path, name)
    anchor = getattr(admitted, ws.OPTIONAL_REPOSITORY_FIELDS[name])
    before = {leaf: (anchor / ".git" / leaf).read_bytes() for leaf in ("HEAD", "index", "config")}
    with pytest.raises(RuntimeError, match="Unsupported or unavailable"):
        create(layout, repositories=(name,))
    assert not (layout.output_root / "tmp/workstreams/alpha.json").exists()
    ws._git(anchor, "config", "--unset", "core.hooksPath")
    with pytest.raises(RuntimeError, match="shared-hook setup"):
        create(admitted, repositories=(name,))
    ws._git(anchor, "config", "core.hooksPath", str(layout.tooling_repo_root / "hooks"))
    registration = create(admitted, repositories=(name,))
    selected = ws.apply_workstream_layout(layout, registration)
    assert getattr(selected, ws.OPTIONAL_REPOSITORY_FIELDS[name]) == Path(registration["repositories"][name]["path"])
    assert before == {leaf: (anchor / ".git" / leaf).read_bytes() for leaf in before}
    # Verified registrations remain diagnosable/removable if a later setup
    # omits optional topology; the common-Git guard still covers their anchor.
    assert ws.workstream_status(layout, "alpha")[0]["verification"] == "valid"
    ws.remove_workstream(layout, "alpha", "parent")
    assert not Path(registration["repositories"][name]["path"]).exists()
    assert ws._git(anchor, "branch", "--show-current") == "main"
    assert ws._git(anchor, "show-ref", "--verify", "refs/heads/chore/alpha")


def test_optional_new_creation_can_overlap_a_heavy_owner(layout, tmp_path):
    admitted = optional_anchor(layout, tmp_path, "emulebb-pages")
    with lifecycle_child(admitted, tmp_path, "heavy"):
        registration = create(admitted, repositories=("emulebb-pages",))
        assert registration["state"] == "ready"


def test_optional_addition_preserves_existing_committed_work_and_sources(layout, tmp_path):
    admitted = optional_anchor(layout, tmp_path, "emulebb-org-profile")
    original = create(admitted)
    selected = Path(original["repositories"]["emulebb-rust"]["path"])
    (selected / "sample.txt").write_text("independent committed work\n", encoding="utf-8")
    ws._git(selected, "add", "sample.txt")
    ws._git(selected, "commit", "-m", "fixture independent work")
    head = ws._git(selected, "rev-parse", "HEAD")
    canonical = admitted.emulebb_org_profile_repo_root
    before = source_state(canonical)
    extended = ws.add_repository(admitted, "alpha", "emulebb-org-profile")
    assert ws._git(selected, "rev-parse", "HEAD") == head
    assert extended["repositories"]["emulebb-rust"] == original["repositories"]["emulebb-rust"]
    assert source_state(canonical) == before
    with pytest.raises(RuntimeError, match="already registered"):
        ws.add_repository(admitted, "alpha", "emulebb-org-profile")


def test_setup_writer_obeys_heavy_and_lifecycle_owners(layout, tmp_path, monkeypatch):
    from emule_workspace import materialize
    layout.output_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(materialize, "resolve_required_workspace_roots", lambda: (layout.emule_workspace_root, layout.output_root))
    with lifecycle_child(layout, tmp_path, "heavy"):
        with pytest.raises(RuntimeError, match="Workspace busy"):
            with materialize._setup_guard(layout.emule_workspace_root, "workspace", "fixture setup"):
                pytest.fail("Setup entered during heavy validation")
    with lifecycle_child(layout, tmp_path, "lifecycle"):
        with pytest.raises(RuntimeError, match="lifecycle busy"):
            with materialize._setup_guard(layout.emule_workspace_root, "workspace", "fixture setup"):
                pytest.fail("Setup entered during lifecycle administration")
    # A failed setup acquisition releases its heavyweight owner as well.
    assert create(layout, "after-setup-contention")["state"] == "ready"


def test_real_optional_sync_cannot_mutate_foreign_hook_repository(layout, tmp_path, monkeypatch):
    from emule_workspace import materialize
    from emule_workspace.topology import build_workspace_manifest, canonical_topology

    pages = optional_anchor(layout, tmp_path, "emulebb-pages")
    admitted = optional_anchor(pages, tmp_path, "emulebb-org-profile")
    urls = {name: ws._git(getattr(admitted, field), "remote", "get-url", "origin")
            for name, field in ws.OPTIONAL_REPOSITORY_FIELDS.items()}
    topology = canonical_topology(profiles=("public-presence",))
    # No app worktrees are needed by this optional-only fixture. Keeping that
    # list empty also makes its generated manifest independent of host syntax.
    topology = topology.model_copy(update={
        "app_repo": topology.app_repo.model_copy(update={"worktrees": ()}),
        "repos": tuple(repo.model_copy(update={"url": urls[repo.name]})
                       if repo.name in urls else repo for repo in topology.repos),
    })
    workspace = layout.emule_workspace_root / "workspaces/workspace"
    workspace.mkdir(parents=True)
    manifest_path = workspace / "deps.json"
    payload = build_workspace_manifest(topology, "workspace")
    payload["workspace"]["profiles"] = ["core"]
    payload["workspace"]["optional_repos"] = {}
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    # Exercise both an existing optional fetch/merge and a missing optional clone.
    admitted.emulebb_org_profile_repo_root.rename(tmp_path / "org-profile-seed")
    foreign = layout.emulebb_rust_repo_root
    ws._git(foreign, "fetch", "origin", "main")
    before = {name: source_state(getattr(layout, field)) for name, field in ws.CORE_REPOSITORY_FIELDS.items()}
    hostile = {"GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
               "GIT_INDEX_FILE": str(foreign / ".git/index")}
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)
    environment = os.environ.copy()
    monkeypatch.setattr(materialize, "canonical_topology", lambda **kwargs: topology)
    monkeypatch.setattr(materialize, "build_repo_root", lambda: layout.build_repo_root)
    monkeypatch.setattr(materialize, "resolve_required_workspace_roots", lambda:
                        (layout.emule_workspace_root, layout.output_root))
    materialize.sync_workspace(workspace_root=str(layout.emule_workspace_root),
                               profiles=("public-presence",), optional_only=True)
    assert os.environ == environment
    assert not (layout.emule_workspace_root / materialize.SETUP_LOG_FILE_NAME).exists()
    log = layout.output_root / "logs/setup/workspace" / materialize.SETUP_LOG_FILE_NAME
    assert "Optional repository sync complete" in log.read_text()
    assert before == {name: source_state(getattr(layout, field)) for name, field in ws.CORE_REPOSITORY_FIELDS.items()}
    after = json.loads(manifest_path.read_text())
    assert set(after["workspace"]["optional_repos"]) == {"pages", "org_profile"}
    assert after["workspace"]["repos"] == payload["workspace"]["repos"]
    assert after["workspace"]["app_repo"] == payload["workspace"]["app_repo"]
    hooks = str(layout.tooling_repo_root / "hooks")
    for name, field in ws.OPTIONAL_REPOSITORY_FIELDS.items():
        path = getattr(admitted, field)
        assert ws._git(path, "config", "--local", "--get", "core.hooksPath") == hooks
        assert ws._git(path, "remote", "get-url", "origin") == urls[name]
        assert ws._git(path, "branch", "--show-current") == "main"


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


def companion(layout):
    return create(layout, repositories=("emulebb-rust", "emulebb-build"))


def test_retire_integrated_companion_preserves_active_work_and_canonical_fallback(layout):
    registration = companion(layout)
    target = Path(registration["repositories"]["emulebb-build"]["path"])
    rust = Path(registration["repositories"]["emulebb-rust"]["path"])
    (rust / "sample.txt").write_text("independent unpublished work\n", encoding="utf-8")
    ws._git(rust, "add", "sample.txt")
    ws._git(rust, "commit", "-m", "fixture independent outcome")
    (rust / "untracked.txt").write_text("keep operator work\n", encoding="utf-8")
    registration["coordination"] = {"reservations": ["independent.rs"]}
    registration["issues"] = ["https://github.com/example/project/issues/1"]
    ws._write(layout.emule_workspace_root, layout.output_root, registration)
    before = source_state(rust)
    branch_head = ws._git(target, "rev-parse", "HEAD")
    (layout.build_repo_root / "new-main.txt").write_text("new canonical producer\n", encoding="utf-8")
    ws._git(layout.build_repo_root, "add", "new-main.txt")
    ws._git(layout.build_repo_root, "commit", "-m", "fixture newer canonical producer")
    ws._git(layout.build_repo_root, "push", "origin", "main")
    result = ws.remove_repository(layout, "alpha", "emulebb-build", "parent")
    assert result == {**registration, "repositories": {"emulebb-rust": registration["repositories"]["emulebb-rust"]}}
    assert source_state(rust) == before and not target.exists()
    assert ws._git(layout.build_repo_root, "rev-parse", "refs/heads/chore/alpha") == branch_head
    verified = ws.resolve_workstream(layout.emule_workspace_root, layout.output_root, workstream_id="alpha")
    selected = ws.apply_workstream_layout(layout, verified)
    assert selected.build_repo_root == layout.build_repo_root
    assert selected.emulebb_rust_repo_root == rust
    # Selection records the actual fallback, rather than treating the retired
    # feature commit or an old producer image as the new canonical revision.
    assert ws._git(selected.build_repo_root, "rev-parse", "HEAD") == ws._git(layout.build_repo_root, "rev-parse", "HEAD")
    assert ws._git(selected.build_repo_root, "rev-parse", "HEAD") != branch_head


@pytest.mark.parametrize("blocker", ["owner", "dirty", "untracked", "ignored", "unpublished", "unmerged", "open-pr", "unknown-pr"])
def test_retirement_refuses_unsafe_target_without_changing_registration(layout, monkeypatch, blocker):
    registration = companion(layout)
    target = Path(registration["repositories"]["emulebb-build"]["path"])
    owner = "worker" if blocker == "owner" else "parent"
    expected = {"owner": "assigned parent", "dirty": "Dirty", "untracked": "Dirty", "ignored": "Dirty",
                "unpublished": "Unpublished", "unmerged": "Unmerged", "open-pr": "Open pull", "unknown-pr": "Cannot establish"}[blocker]
    if blocker in {"dirty", "unpublished", "unmerged"}:
        (target / "sample.txt").write_text("unmerged outcome\n", encoding="utf-8")
        if blocker != "dirty":
            ws._git(target, "add", "sample.txt")
            ws._git(target, "commit", "-m", "fixture unmerged outcome")
        if blocker == "unmerged":
            ws._git(target, "push", "-u", "origin", "chore/alpha")
    elif blocker in {"untracked", "ignored"}:
        if blocker == "ignored":
            exclude = Path(ws._git(target, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
            exclude.write_text("operator.txt\n", encoding="utf-8")
        (target / "operator.txt").write_text("operator data\n", encoding="utf-8")
    elif blocker == "open-pr":
        monkeypatch.setattr(ws, "_open_pull_requests", lambda entry: [{"number": 1}])
    elif blocker == "unknown-pr":
        def unknown(entry):
            raise RuntimeError("Cannot establish PR state")
        monkeypatch.setattr(ws, "_open_pull_requests", unknown)
    manifest = layout.output_root / "tmp/workstreams/alpha.json"
    before = manifest.read_bytes()
    with pytest.raises(RuntimeError, match=expected):
        ws.remove_repository(layout, "alpha", "emulebb-build", owner)
    assert target.exists() and manifest.read_bytes() == before


def test_retirement_refuses_last_repository_and_checkout_cwd(layout, monkeypatch):
    registration = create(layout)
    with pytest.raises(RuntimeError, match="last repository.*workstream remove"):
        ws.remove_repository(layout, "alpha", "emulebb-rust", "parent")
    ws.add_repository(layout, "alpha", "emulebb-build")
    monkeypatch.chdir(Path(registration["repositories"]["emulebb-rust"]["path"]))
    with pytest.raises(RuntimeError, match="outside the checkout"):
        ws.remove_repository(layout, "alpha", "emulebb-rust", "parent")


def test_retirement_of_previously_admitted_optional_companion_does_not_readmit_it(layout, tmp_path):
    admitted = optional_anchor(layout, tmp_path, "emulebb-pages")
    registration = create(admitted, repositories=("emulebb-rust", "emulebb-pages"))
    path = Path(registration["repositories"]["emulebb-pages"]["path"])
    result = ws.remove_repository(layout, "alpha", "emulebb-pages", "parent")
    assert list(result["repositories"]) == ["emulebb-rust"] and not path.exists()
    assert ws.apply_workstream_layout(layout, result).emulebb_pages_repo_root is None


def test_retirement_accepts_rebase_integration_and_retains_original_history(layout):
    registration = companion(layout)
    path = Path(registration["repositories"]["emulebb-build"]["path"])
    (path / "sample.txt").write_text("integrated change\n", encoding="utf-8")
    ws._git(path, "add", "sample.txt")
    ws._git(path, "commit", "-m", "fixture integrated feature")
    original = ws._git(path, "rev-parse", "HEAD")
    canonical = layout.build_repo_root
    (canonical / "other.txt").write_text("independent main\n", encoding="utf-8")
    ws._git(canonical, "add", "other.txt")
    ws._git(canonical, "commit", "-m", "fixture independent main")
    ws._git(canonical, "cherry-pick", original)
    ws._git(canonical, "push", "origin", "main")
    assert ws._git(canonical, "rev-parse", "HEAD") != original
    ws.remove_repository(layout, "alpha", "emulebb-build", "parent")
    assert ws._git(canonical, "rev-parse", "refs/heads/chore/alpha") == original
    assert not path.exists()


@pytest.mark.parametrize("after_removal", [False, True])
def test_retirement_journal_resumes_before_or_after_git_removal(layout, monkeypatch, after_removal):
    registration = companion(layout)
    target = Path(registration["repositories"]["emulebb-build"]["path"])
    original_git = ws._git
    def interrupt(repo, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            if after_removal:
                original_git(repo, *args, **kwargs)
            raise RuntimeError("fixture interrupted removal")
        return original_git(repo, *args, **kwargs)
    monkeypatch.setattr(ws, "_git", interrupt)
    with pytest.raises(RuntimeError, match="interrupted removal"):
        ws.remove_repository(layout, "alpha", "emulebb-build", "parent")
    assert target.exists() is not after_removal
    with pytest.raises(RuntimeError, match="rerun workstream remove-repo"):
        ws.resolve_workstream(layout.emule_workspace_root, layout.output_root, workstream_id="alpha")
    assert ws.workstream_status(layout, "alpha")[0]["verification"] == "invalid"
    with pytest.raises(RuntimeError, match="conflicting.*journal"):
        ws.remove_repository(layout, "alpha", "emulebb-rust", "parent")
    with pytest.raises(RuntimeError, match="assigned parent"):
        ws.remove_repository(layout, "alpha", "emulebb-build", "worker")
    monkeypatch.setattr(ws, "_git", original_git)
    result = ws.remove_repository(layout, "alpha", "emulebb-build", "parent")
    assert result["state"] == "ready" and "retirement" not in result
    assert list(result["repositories"]) == ["emulebb-rust"] and not target.exists()
    assert ws._git(layout.build_repo_root, "rev-parse", "refs/heads/chore/alpha") == registration["repositories"]["emulebb-build"]["base_revision"]


def test_retirement_retry_refuses_changed_branch_and_dangling_git_administration(layout, monkeypatch):
    registration = companion(layout)
    target = Path(registration["repositories"]["emulebb-build"]["path"])
    original_write = ws._write
    def interrupt_final(root, output, value, **kwargs):
        if value["state"] == "ready":
            raise RuntimeError("fixture final write interrupted")
        original_write(root, output, value, **kwargs)
    monkeypatch.setattr(ws, "_write", interrupt_final)
    with pytest.raises(RuntimeError, match="final write interrupted"):
        ws.remove_repository(layout, "alpha", "emulebb-build", "parent")
    monkeypatch.setattr(ws, "_write", original_write)
    assert not target.exists()
    head = ws._git(layout.build_repo_root, "rev-parse", "HEAD")
    (layout.build_repo_root / "other.txt").write_text("new canonical work\n", encoding="utf-8")
    ws._git(layout.build_repo_root, "add", "other.txt")
    ws._git(layout.build_repo_root, "commit", "-m", "fixture new main")
    ws._git(layout.build_repo_root, "push", "origin", "main")
    ws._git(layout.build_repo_root, "update-ref", "refs/heads/chore/alpha", "HEAD")
    with pytest.raises(RuntimeError, match="branch changed"):
        ws.remove_repository(layout, "alpha", "emulebb-build", "parent")
    ws._git(layout.build_repo_root, "update-ref", "refs/heads/chore/alpha", head)
    # A folder removed outside Git is not the successful removal recorded by
    # our journal; keep its administration and refuse implicit pruning.
    ws._git(layout.build_repo_root, "worktree", "add", str(target), "chore/alpha")
    moved = target.with_name("operator-moved-checkout")
    target.rename(moved)
    with pytest.raises(RuntimeError, match="administration remains"):
        ws.remove_repository(layout, "alpha", "emulebb-build", "parent")
    assert moved.is_dir()


# These subprocesses use only disposable fixture roots. A separate process is
# essential: a same-process lock mock cannot prove Windows/POSIX exclusion or
# release after an abrupt exit.
_LIFECYCLE_CHILD = r"""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from emule_workspace import workstreams as ws
from emule_workspace.config import WorkspaceOptions
from emule_workspace.locks import WorkspaceLock

layout = SimpleNamespace(**{key: Path(value) for key, value in json.loads(sys.argv[1]).items()})
layout.output_tmp_root = layout.output_root / 'tmp'
ready, mode = Path(sys.argv[2]), sys.argv[3]
def wait():
    ready.write_text('held', encoding='utf-8')
    sys.stdin.readline()
if mode == 'heavy':
    lock = WorkspaceLock(layout, 'fixture validation', WorkspaceOptions(workspace_root=layout.emule_workspace_root))
    assert lock.acquire(), 'fixture workspace lock not acquired'
    try:
        wait()
    finally:
        lock.release()
elif mode == 'lifecycle':
    with ws._lifecycle_guard(layout):
        wait()
elif mode == 'create':
    original = ws._git
    def pause(repo, *args, **kwargs):
        if args[:2] == ('worktree', 'add'):
            wait()
        return original(repo, *args, **kwargs)
    ws._git = pause
    ws.create_workstream(layout, 'contended', 'chore/contended', 'parent', ('emulebb-rust',))
elif mode in ('retire-before', 'retire-after'):
    original = ws._git
    def pause(repo, *args, **kwargs):
        if args[:2] == ('worktree', 'remove'):
            if mode == 'retire-after':
                result = original(repo, *args, **kwargs)
                wait()
                return result
            wait()
        return original(repo, *args, **kwargs)
    ws._git = pause
    ws._open_pull_requests = lambda entry: []
    ws.remove_repository(layout, 'alpha', 'emulebb-build', 'parent')
"""


@contextmanager
def lifecycle_child(layout, tmp_path, mode, *, crash=False):
    ready = tmp_path / f"{mode}-ready"
    environment = git_environment()
    environment.pop("EMULEBB_WORKSPACE_LOCK_TOKEN", None)
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", _LIFECYCLE_CHILD,
         json.dumps({key: str(value) for key, value in vars(layout).items() if value is not None}), str(ready), mode],
        cwd=Path(ws.__file__).resolve().parents[1], env=environment,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            if process.poll() is not None:
                raise AssertionError(process.communicate()[1])
            if time.monotonic() >= deadline:
                raise AssertionError("Fixture child did not reach its lock barrier")
            time.sleep(0.02)
        yield process
    finally:
        if crash and process.poll() is None:
            process.kill()
            process.wait(timeout=30)
        if process.poll() is None:
            try:
                _, error = process.communicate("continue\n", timeout=30)
                assert process.returncode == 0, error
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
        else:
            _, error = process.communicate()
            assert crash or process.returncode == 0, error


def source_state(repo):
    status = ws._git(repo, "status", "--porcelain")
    common = Path(ws._common_dir(repo))
    return {
        "head": ws._git(repo, "rev-parse", "HEAD"),
        "branch": ws._git(repo, "branch", "--show-current"),
        "tracking": ws._git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes"),
        "tags": ws._git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/tags"),
        "status": status,
        "config": (common / "config").read_bytes(),
        "index": hashlib.sha256((Path(ws._git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index"))).read_bytes()).hexdigest(),
        "fetch_head": (common / "FETCH_HEAD").read_bytes() if (common / "FETCH_HEAD").exists() else None,
    }


@pytest.mark.parametrize("mode", ["retire-before", "retire-after"])
def test_crashed_retirement_releases_guards_and_resumes_with_supported_cli(layout, tmp_path, monkeypatch, mode):
    from click.testing import CliRunner
    from emule_workspace import cli
    from emule_workspace.config import WorkspaceOptions

    original = companion(layout)
    rust = Path(original["repositories"]["emulebb-rust"]["path"])
    rust_before = source_state(rust)
    options = WorkspaceOptions(workspace_root=layout.emule_workspace_root, output_root=layout.output_root)
    monkeypatch.chdir(layout.build_repo_root)
    monkeypatch.setattr(cli, "resolve_workspace_options", lambda **kwargs: options)
    monkeypatch.setattr(cli, "load_layout", lambda *args, **kwargs: layout)
    with lifecycle_child(layout, tmp_path, mode, crash=True):
        receipt = (layout.output_root / "tmp/workstreams/alpha.json").read_bytes()
        assert json.loads(receipt)["state"] == "retiring-repository"
        with pytest.raises(RuntimeError, match="lifecycle busy"):
            ws.remove_repository(layout, "alpha", "emulebb-build", "parent")
    result = CliRunner().invoke(cli.main, ["workstream", "remove-repo", "alpha",
                                          "--repo", "emulebb-build", "--owner", "parent"])
    assert result.exit_code == 0, result.output
    assert list(json.loads(result.output)["repositories"]) == ["emulebb-rust"]
    assert source_state(rust) == rust_before
    assert not (layout.output_root / "tmp/active-command-lock.json").exists()


def test_cli_creates_during_real_heavy_lock_without_changing_active_sources(layout, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from emule_workspace import cli
    from emule_workspace.config import WorkspaceOptions

    active = create(layout)
    active_repo = Path(active["repositories"]["emulebb-rust"]["path"])
    canonical = layout.emulebb_rust_repo_root
    ws._git(canonical, "fetch", "origin", "main")
    # A newer advertised main must not move shared origin/main or FETCH_HEAD.
    writer = tmp_path / "writer"
    ws._git(tmp_path, "clone", ws._git(canonical, "remote", "get-url", "origin"), str(writer))
    for key, value in (("user.email", "fixture@example.invalid"), ("user.name", "Fixture Author"),
                       ("core.hooksPath", str(tmp_path / "empty-hooks"))):
        ws._git(writer, "config", key, value)
    (writer / "sample.txt").write_text("new advertised main\n", encoding="utf-8")
    ws._git(writer, "add", "sample.txt")
    ws._git(writer, "commit", "-m", "fixture newer main")
    ws._git(writer, "push", "origin", "main")
    advertised = ws._git(writer, "rev-parse", "HEAD")
    ws._git(canonical, "config", "fetch.prune", "true")
    ws._git(canonical, "config", "fetch.pruneTags", "true")
    ws._git(canonical, "update-ref", "refs/remotes/origin/retained-proof", active["repositories"]["emulebb-rust"]["base_revision"])
    ws._git(canonical, "tag", "local-proof")
    before = {str(repo): source_state(repo) for repo in [*ws._sources(layout).values(), active_repo]}
    registration_path = layout.output_root / "tmp/workstreams/alpha.json"
    registration_before = registration_path.read_bytes()
    options = WorkspaceOptions(workspace_root=layout.emule_workspace_root, output_root=layout.output_root)
    monkeypatch.chdir(layout.build_repo_root)
    monkeypatch.setattr(cli, "resolve_workspace_options", lambda **kwargs: options)
    monkeypatch.setattr(cli, "load_layout", lambda *args, **kwargs: layout)
    with lifecycle_child(layout, tmp_path, "heavy"):
        metadata = layout.output_root / "tmp/active-command-lock.json"
        owner = metadata.read_bytes()
        result = CliRunner().invoke(cli.main, ["workstream", "create", "beta", "--branch", "chore/beta",
                                               "--owner", "parent", "--repo", "emulebb-rust"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["repositories"]["emulebb-rust"]["base_revision"] == advertised
        assert ws._git(layout.emule_workspace_root / "workspaces/agents/beta/emulebb-rust", "rev-parse", "HEAD") == advertised
        assert metadata.read_bytes() == owner
        assert registration_path.read_bytes() == registration_before
        assert {str(repo): source_state(repo) for repo in [*ws._sources(layout).values(), active_repo]} == before
        for arguments in (["add-repo", "alpha", "--repo", "emulebb-tooling"],
                          ["remove", "alpha", "--owner", "parent"],
                          ["remove-repo", "alpha", "--repo", "emulebb-rust", "--owner", "parent"]):
            blocked = CliRunner().invoke(cli.main, ["workstream", *arguments])
            assert blocked.exit_code != 0 and "Workspace busy" in blocked.output
        assert registration_path.read_bytes() == registration_before


def test_concurrent_real_creator_cannot_replace_reserved_id_or_branch(layout, tmp_path):
    create(layout)
    path = layout.output_root / "tmp/workstreams/contended.json"
    with lifecycle_child(layout, tmp_path, "create"):
        reserved = path.read_bytes()
        assert json.loads(reserved)["state"] == "creating"
        with pytest.raises(RuntimeError, match="lifecycle busy"):
            create(layout, "contended")
        with pytest.raises(RuntimeError, match="lifecycle busy"):
            create(layout, "other")
        with pytest.raises(RuntimeError, match="lifecycle busy"):
            ws.add_repository(layout, "alpha", "emulebb-tooling")
        with pytest.raises(RuntimeError, match="lifecycle busy"):
            ws.remove_workstream(layout, "alpha", "parent")
        with pytest.raises(RuntimeError, match="lifecycle busy"):
            ws.remove_repository(layout, "alpha", "emulebb-rust", "parent")
        assert path.read_bytes() == reserved
    completed = path.read_bytes()
    with pytest.raises(RuntimeError, match="already registered"):
        create(layout, "contended")
    with pytest.raises(RuntimeError, match="Branch already exists"):
        ws.create_workstream(layout, "other", "chore/contended", "parent", ("emulebb-rust",))
    assert path.read_bytes() == completed
    assert not (path.parent / "other.json").exists()


def test_common_git_guard_excludes_other_registry_and_releases_after_crash(layout, tmp_path):
    alternate = SimpleNamespace(**{**vars(layout), "output_root": tmp_path / "other-output"})
    with lifecycle_child(layout, tmp_path, "lifecycle", crash=True):
        with pytest.raises(RuntimeError, match="lifecycle busy"):
            create(alternate)
    registration = create(alternate)
    assert registration["state"] == "ready"
    # Persistent lock files are harmless; there is no stale PID-file recovery.
    assert (layout.output_root / "tmp/workstreams/.lifecycle.lock").is_file()
    assert ws.resolve_workstream(alternate.emule_workspace_root, alternate.output_root,
                                 workstream_id="alpha")["id"] == "alpha"


def test_crashed_creator_keeps_incomplete_receipt_and_refuses_automatic_recovery(layout, tmp_path):
    path = layout.output_root / "tmp/workstreams/contended.json"
    with lifecycle_child(layout, tmp_path, "create", crash=True):
        reserved = path.read_bytes()
    with pytest.raises(RuntimeError, match="already registered"):
        create(layout, "contended")
    assert path.read_bytes() == reserved
    status = ws.workstream_status(layout, "contended")[0]
    assert status["verification"] == "invalid" and "incomplete" in status["error"]
    assert not (layout.emule_workspace_root / "workspaces/agents/contended/emulebb-rust").exists()


def test_advertised_revision_fetch_failure_has_no_moving_ref_fallback(layout, monkeypatch):
    original = ws._git
    requested = "f" * 40
    calls = []
    def stale_advertisement(repo, *args, **kwargs):
        calls.append(args)
        if args[:1] == ("ls-remote",):
            return requested + "\trefs/heads/main"
        return original(repo, *args, **kwargs)
    before = source_state(layout.emulebb_rust_repo_root)
    monkeypatch.setattr(ws, "_git", stale_advertisement)
    with pytest.raises(RuntimeError):
        create(layout)
    assert ("fetch", "--no-tags", "--no-prune", "--no-prune-tags",
            "--no-write-fetch-head", "--refmap=", "origin", requested) in calls
    assert not any("refs/remotes/origin/main" in arguments for arguments in calls)
    assert source_state(layout.emulebb_rust_repo_root) == before
    assert not (layout.output_root / "tmp/workstreams/alpha.json").exists()


def test_create_ignores_inherited_foreign_git_repository_and_index(layout, monkeypatch):
    foreign = layout.tooling_repo_root
    before = source_state(foreign)
    rust_before = source_state(layout.emulebb_rust_repo_root)
    inherited = {"GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
                 "GIT_INDEX_FILE": str(foreign / ".git/index")}
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    registration = create(layout)
    selected = Path(registration["repositories"]["emulebb-rust"]["path"])
    assert ws._git(selected, "rev-parse", "HEAD") == rust_before["head"]
    assert source_state(foreign) == before
    assert source_state(layout.emulebb_rust_repo_root) == rust_before
    assert {name: os.environ[name] for name in inherited} == inherited
