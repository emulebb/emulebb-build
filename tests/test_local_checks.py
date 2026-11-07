"""Verify outgoing change selection and local gate routing."""
from pathlib import Path
import subprocess

import pytest

from emule_workspace.local_checks import changed_paths, check_plan
from emule_workspace import local_checks


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "base.txt").write_text("base\n")
    git(tmp_path, "add", "base.txt")
    git(tmp_path, "commit", "-m", "base")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git(tmp_path, "switch", "-c", "chore/checks")
    return tmp_path


def test_first_push_includes_all_branch_commits_and_deletions(repo):
    git(repo, "rm", "base.txt")
    git(repo, "commit", "-m", "delete base")
    (repo / "new.txt").write_text("new\n")
    git(repo, "add", "new.txt")
    git(repo, "commit", "-m", "add new")
    tip = git(repo, "rev-parse", "HEAD")
    update = f"refs/heads/chore/checks {tip} refs/heads/chore/checks {'0'*40}\n"
    assert changed_paths(repo, "push", "origin", update) == {"base.txt", "new.txt"}


def test_deletion_only_push_has_no_checks(repo):
    assert changed_paths(repo, "push", "origin", f"(delete) {'0'*40} refs/heads/old {'1'*40}\n") == set()


def test_annotated_tag_checks_the_commit_it_names(repo):
    (repo / "new.txt").write_text("new\n")
    git(repo, "add", "new.txt")
    git(repo, "commit", "-m", "new")
    git(repo, "tag", "-a", "fixture-tag", "-m", "synthetic tag")
    tag = git(repo, "rev-parse", "fixture-tag")
    update = f"refs/tags/fixture-tag {tag} refs/tags/fixture-tag {'0'*40}\n"
    assert changed_paths(repo, "push", "origin", update) == {"new.txt"}


def test_other_ref_cannot_be_certified_by_head(repo):
    update = f"refs/heads/other {'1'*40} refs/heads/other {'0'*40}\n"
    with pytest.raises(RuntimeError, match="push it separately"):
        changed_paths(repo, "push", "origin", update)


def test_multiple_refs_are_checked_and_deletion_does_not_mask_updates(repo):
    base = git(repo, "rev-parse", "HEAD")
    (repo / "new.txt").write_text("new\n")
    git(repo, "add", "new.txt")
    git(repo, "commit", "-m", "new")
    tip = git(repo, "rev-parse", "HEAD")
    updates = f"(delete) {'0'*40} refs/heads/old {base}\nrefs/heads/chore/checks {tip} refs/heads/chore/checks {base}\n"
    assert changed_paths(repo, "push", "origin", updates) == {"new.txt"}


def test_staged_paths_exclude_unstaged_new_files(repo):
    (repo / "selected.txt").write_text("selected\n")
    (repo / "unselected.txt").write_text("unselected\n")
    git(repo, "add", "selected.txt")
    assert changed_paths(repo, "commit", "origin", None) == {"selected.txt"}


@pytest.mark.parametrize("kind", ["first", "existing", "tag", "multiple"])
def test_clean_tree_with_bad_outgoing_whitespace_is_rejected(repo, kind):
    base = git(repo, "rev-parse", "HEAD")
    (repo / "selected.txt").write_text("trailing space \n")
    git(repo, "add", "selected.txt")
    git(repo, "commit", "-m", "synthetic whitespace fixture")
    tip = git(repo, "rev-parse", "HEAD")
    assert git(repo, "status", "--porcelain") == ""
    local_ref = "refs/heads/chore/checks"
    if kind == "tag":
        git(repo, "tag", "-a", "fixture-tag", "-m", "synthetic tag")
        tip = git(repo, "rev-parse", "fixture-tag")
        local_ref = "refs/tags/fixture-tag"
    remote_sha = base if kind == "existing" else "0" * 40
    updates = f"{local_ref} {tip} {local_ref} {remote_sha}\n"
    if kind == "multiple":
        updates = f"(delete) {'0'*40} refs/heads/old {base}\n" + updates
    with pytest.raises(subprocess.CalledProcessError):
        local_checks.check_whitespace(repo, "push", "origin", updates)


def test_staged_whitespace_is_rejected_and_delete_only_push_is_allowed(repo):
    (repo / "selected.txt").write_text("trailing space \n")
    git(repo, "add", "selected.txt")
    with pytest.raises(subprocess.CalledProcessError):
        local_checks.check_whitespace(repo, "commit", "origin", None)
    local_checks.check_whitespace(repo, "push", "origin", f"(delete) {'0'*40} refs/heads/old {'1'*40}\n")


def test_rust_docs_avoid_native_build_but_source_push_gets_full_gate(tmp_path):
    repo = tmp_path / "repos" / "emulebb-rust"
    docs = check_plan(repo, {"README.md"}, "push", tmp_path, tmp_path / "out")
    source = check_plan(repo, {"src/main.rs"}, "push", tmp_path, tmp_path / "out")
    assert not any(command[-1] == "pre-push" for _, command in docs)
    assert sum(command[-1] == "pre-push" for _, command in source) == 1
    assert not any("deny" in command or command[-1].endswith("rust-rest-openapi-ci.py") for _, command in source)


def test_commit_never_runs_heavy_checks(tmp_path):
    plan = check_plan(tmp_path / "repos" / "emulebb-rust", {"src/main.rs"}, "commit", tmp_path, tmp_path / "out")
    assert not any(command[-1] == "pre-push" or "deny" in command for _, command in plan)


def test_docs_workflow_gets_local_publication_validation(tmp_path):
    plan = check_plan(tmp_path / "repos" / "emulebb-tooling", {".github/workflows/docs-site.yml"}, "push", tmp_path, tmp_path / "out")
    assert any(command[-1] == "scripts/docs-publish-check.py" for _, command in plan)


def test_topology_routes_to_its_existing_test_owners(tmp_path):
    repo = tmp_path / "repos" / "emulebb-build"
    plan = check_plan(repo, {"emule_workspace/topology.py"}, "push", tmp_path, tmp_path / "out")
    command = next(command for _, command in plan if "pytest" in command)
    assert "tests/test_layout.py" in command
    assert "tests/test_materialize.py" in command
    assert "tests/test_validation.py" in command
    assert "tests/test_setup_commands.py" in command
    assert "tests" not in command


def test_failure_blocks_later_checks_and_writes_evidence(tmp_path, monkeypatch):
    repo = tmp_path / "repos" / "emulebb-build"
    repo.mkdir(parents=True)
    monkeypatch.setattr(local_checks, "changed_paths", lambda *args: {"sample.py"})
    monkeypatch.setattr(local_checks, "git", lambda repo, *args: str(repo) if args[0] == "rev-parse" else "")
    monkeypatch.setattr(local_checks, "check_plan", lambda *args: [(repo, ["check", "first"]), (repo, ["check", "second"])])
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 7 if command[-1] == "first" else 0)

    monkeypatch.setattr(local_checks.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Local check failed"):
        local_checks.run_checks(repo=repo, stage="push", remote="origin", updates=None, root=tmp_path, output=tmp_path / "output")
    assert ["check", "second"] not in calls
    assert list((tmp_path / "output" / "reports").rglob("results.json"))


def test_missing_tool_retains_failed_start_evidence(tmp_path, monkeypatch):
    import json
    repo = tmp_path / "repos" / "emulebb-build"
    repo.mkdir(parents=True)
    monkeypatch.setattr(local_checks, "changed_paths", lambda *args: {"sample.py"})
    monkeypatch.setattr(local_checks, "git", lambda repo, *args: str(repo) if args[0] == "rev-parse" else "")
    monkeypatch.setattr(local_checks, "check_plan", lambda *args: [(repo, ["missing-fixture-tool"])])

    def missing(*args, **kwargs):
        raise FileNotFoundError("synthetic missing tool")

    monkeypatch.setattr(local_checks.subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="Cannot start required local check"):
        local_checks.run_checks(repo=repo, stage="push", remote="origin", updates=None, root=tmp_path, output=tmp_path / "output")
    record = json.loads(next((tmp_path / "output" / "reports").rglob("results.json")).read_text())[0]
    assert record["returncode"] is None
    assert record["elapsed_seconds"] >= 0
    assert record["error"] == "synthetic missing tool"


def test_dirty_tree_blocks_push_before_any_check(tmp_path, monkeypatch):
    repo = tmp_path / "repos" / "emulebb-build"
    repo.mkdir(parents=True)
    monkeypatch.setattr(local_checks, "changed_paths", lambda *args: {"sample.py"})
    monkeypatch.setattr(local_checks, "git", lambda repo, *args: str(repo) if args[0] == "rev-parse" else "dirty.txt")
    with pytest.raises(RuntimeError, match="clean worktree"):
        local_checks.run_checks(repo=repo, stage="push", remote="origin", updates=None, root=tmp_path, output=tmp_path / "output")
