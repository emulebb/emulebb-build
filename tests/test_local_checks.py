"""Verify outgoing change selection and local gate routing."""
from pathlib import Path
import subprocess

import pytest

from emule_workspace.local_checks import changed_paths, check_plan, linux_push_scope
from emule_workspace import local_checks


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def test_hook_repository_context_does_not_redirect_git_queries_or_fixture_init(tmp_path, monkeypatch):
    import os
    first, second, fixture = (tmp_path / name for name in ("first", "second", "fixture"))
    for directory, content in ((first, "first"), (second, "second")):
        directory.mkdir()
        git(directory, "init", "-b", "main")
        git(directory, "config", "user.email", "test@example.invalid")
        git(directory, "config", "user.name", "Test")
        (directory / "sample.txt").write_text(content)
        git(directory, "add", "sample.txt")
        git(directory, "commit", "-m", content)
    original = {name: (first / ".git" / name).read_bytes() for name in ("HEAD", "config", "index")}
    expected = git(second, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", str(first / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(first))
    monkeypatch.setenv("GIT_INDEX_FILE", str(first / ".git" / "index"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "operator.gitconfig"))
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    assert local_checks.git(second, "rev-parse", "HEAD") == expected
    child = local_checks.git_environment()
    assert not {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}.intersection(child)
    assert child["GIT_CONFIG_GLOBAL"] == str(tmp_path / "operator.gitconfig")
    assert child["GIT_TERMINAL_PROMPT"] == "0"
    fixture.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=fixture, env=child, check=True, capture_output=True)
    assert (fixture / ".git" / "HEAD").is_file()
    assert all((first / ".git" / name).read_bytes() == content for name, content in original.items())
    assert os.environ["GIT_DIR"] == str(first / ".git")


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


@pytest.mark.parametrize("path", ["emule_workspace/android_install.py", "emule_workspace/android_inputs.py",
                                   "emule_workspace/android_probe.py", "emule_workspace/android_toolchain.py",
                                   "emule_workspace/android_validation.py", "emule_workspace/release_assets/android/Dockerfile",
                                   "emule_workspace/release_assets/android/toolchain.json"])
def test_android_tool_changes_require_real_wsl_docker_probe_before_push(tmp_path, path):
    repo = tmp_path / "repos/emulebb-build"
    pushed = check_plan(repo, {path}, "push", tmp_path, tmp_path / "out")
    committed = check_plan(repo, {path}, "commit", tmp_path, tmp_path / "out")
    assert sum(command[-1] == "validate-rust-android" for _, command in pushed) == 1
    assert not any(command[-1] == "validate-rust-android" for _, command in committed)


def test_docs_do_not_trigger_android_tool_downloads(tmp_path):
    repo = tmp_path / "repos/emulebb-build"
    plan = check_plan(repo, {"README.md"}, "push", tmp_path, tmp_path / "out")
    assert not any(command[-1] in {"prepare-rust-android-toolchain", "validate-rust-android", "prepare-rust-android-emulator"} for _, command in plan)


@pytest.mark.parametrize("path", ["emule_workspace/android_probe.py", "emule_workspace/android_install.py",
                                  "emule_workspace/release_assets/android/Dockerfile.emulator",
                                  "emule_workspace/release_assets/android/emulator-inputs.json"])
def test_emulator_tool_changes_require_offline_execution_before_push(tmp_path, path):
    repo = tmp_path / "repos/emulebb-build"
    plan = check_plan(repo, {path}, "push", tmp_path, tmp_path / "out")
    command = next(command for _, command in plan if command[-1] == "prepare-rust-android-emulator")
    assert "--image" not in command


def test_rust_implementation_requires_android_compilation_before_push(tmp_path):
    repo = tmp_path / "repos/emulebb-rust"
    source = check_plan(repo, {"crates/emulebb-core/src/lib.rs"}, "push", tmp_path, tmp_path / "out")
    docs = check_plan(repo, {"README.md"}, "push", tmp_path, tmp_path / "out")
    assert sum(command[-1] == "validate-rust-android" for _, command in source) == 1
    assert not any(command[-1] == "validate-rust-android" for _, command in docs)


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
    assert not any("validate-emulebb-rust-linux" in command for _, command in plan)


@pytest.mark.parametrize("path", ["Cargo.lock", "crates/emulebb-core/Cargo.toml",
    "crates/emulebb-daemon/build.rs", "rust-toolchain.toml", ".cargo/config.toml",
    ".github/workflows/ci.yml", "packaging/docker/Dockerfile", "policy/rust-client.toml",
    "tools/release_publish.py", "tools/ci-toolchain.json", "tools/rust_quality_gate.py",
    "crates/emulebb-kad-net/src/socket_opts.rs",
    "crates/emulebb-ed2k/src/ed2k_transfer/fs_durability.rs",
    "crates/emulebb-pcpnatpmp-sys/src/lib.rs"])
def test_platform_inputs_require_full_available_linux_matrix(path):
    assert linux_push_scope({path}) == "full"


def test_rust_push_runs_host_then_native_musl_and_docs_stay_light(tmp_path):
    repo = tmp_path / "repos/emulebb-rust"
    source = repo / "crates/emulebb-core/src/lib.rs"
    source.parent.mkdir(parents=True)
    source.write_text("pub fn ordinary_logic() {}\n")
    plan = check_plan(repo, {"crates/emulebb-core/src/lib.rs"}, "push", tmp_path, tmp_path / "out")
    assert plan[-3][1][-1] == "pre-push"
    assert plan[-2][0] == tmp_path / "repos/emulebb-build"
    assert plan[-2][1][-3:] == ["validate-emulebb-rust-linux", "--scope", "musl"]
    assert plan[-1][0] == tmp_path / "repos/emulebb-build"
    assert plan[-1][1][-1] == "validate-rust-android"
    docs = check_plan(repo, {"README.md"}, "push", tmp_path, tmp_path / "out")
    assert not any("validate-emulebb-rust-linux" in command for _, command in docs)


@pytest.mark.parametrize("content", [
    '#[cfg(any(target_os = "linux", target_os = "android"))]\nfn platform_logic() {}\n',
    "use std::os::unix::ffi::OsStrExt;\n",
    "#[cfg(not(windows))]\nfn platform_logic() {}\n",
    'let enabled = cfg!(unix);\n',
    'use windows_sys::Win32::Foundation;\n',
])
def test_platform_source_outside_named_owners_expands_the_matrix(tmp_path, content):
    source = tmp_path / "arbitrary.rs"
    source.write_text(content)
    assert linux_push_scope({"arbitrary.rs"}, tmp_path) == "full"


def test_deleted_rust_source_cannot_escape_cross_target_checks(tmp_path):
    assert linux_push_scope({"removed.rs"}, tmp_path) == "full"


def test_ordinary_rolling_windows_do_not_trigger_platform_matrix(tmp_path):
    (tmp_path / "tracker.rs").write_text("// Count packets across three full windows.\nfn track() {}\n")
    assert linux_push_scope({"tracker.rs"}, tmp_path) == "musl"


@pytest.mark.parametrize("name,path,scope", [
    ("emulebb-build", "emule_workspace/linux_containers.py", "full"),
    ("emulebb-build", "emule_workspace/linux_validation.py", "full"),
    ("emulebb-build", "emule_workspace/image_security.py", "image"),
    ("emulebb-build", "emule_workspace/rust_images.py", "image"),
    ("emulebb-build", "emule_workspace/release_assets/rust_images/buildkitd.toml", "image"),
    ("emulebb-build", "emule_workspace/release_assets/rust_linux/Dockerfile", "full"),
    ("emulebb-build-tests", "scripts/smoke-rust-release-package.py", "full"),
    ("emulebb-build-tests", "scripts/smoke-rust-container.py", "image"),
])
def test_shared_linux_pipeline_pushes_require_actual_local_integration(tmp_path, name, path, scope):
    plan = check_plan(tmp_path / "repos" / name, {path}, "push", tmp_path, tmp_path / "out")
    assert plan[-1][1][-3:] == ["validate-emulebb-rust-linux", "--scope", scope]
    commit = check_plan(tmp_path / "repos" / name, {path}, "commit", tmp_path, tmp_path / "out")
    assert not any("validate-emulebb-rust-linux" in command for _, command in commit)


@pytest.mark.parametrize("name,paths", [
    ("emulebb-build", {"emule_workspace/rust_images.py", "emule_workspace/linux_validation.py"}),
    ("emulebb-build-tests", {"scripts/smoke-rust-container.py", "scripts/smoke-rust-release-package.py"}),
])
def test_full_linux_gate_covers_mixed_image_and_package_changes_once(tmp_path, name, paths):
    plan = check_plan(tmp_path / "repos" / name, paths, "push", tmp_path, tmp_path / "out")
    gates = [command for _, command in plan if "validate-emulebb-rust-linux" in command]
    assert len(gates) == 1 and gates[0][-1] == "full"


def test_harness_push_selects_the_changed_scripts_test_owner(tmp_path):
    repo = tmp_path / "repos/emulebb-build-tests"
    tests = repo / "tests/python"
    tests.mkdir(parents=True)
    (tests / "test_smoke_rust_release_package.py").touch()
    plan = check_plan(repo, {"scripts/smoke-rust-release-package.py"}, "push", tmp_path, tmp_path / "out")
    command = next(command for _, command in plan if "pytest" in command)
    assert "tests/python/test_smoke_rust_release_package.py" in command
    assert "tests/python" not in command


def test_harness_shared_owner_without_a_single_suite_requires_all_unit_tests(tmp_path):
    plan = check_plan(tmp_path / "repos/emulebb-build-tests", {"emule_test_harness/shared.py"},
                      "push", tmp_path, tmp_path / "out")
    assert any("tests/python" in command for _, command in plan)


def test_harness_docs_and_commit_checks_do_not_run_pytest(tmp_path):
    repo = tmp_path / "repos/emulebb-build-tests"
    for paths, stage in (({"README.md"}, "push"), ({"scripts/smoke-rust-release-package.py"}, "commit")):
        assert not any("pytest" in command for _, command in check_plan(repo, paths, stage, tmp_path, tmp_path / "out"))


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


def test_unicode_failure_log_does_not_hide_check_failure_on_windows_console(tmp_path, monkeypatch):
    import io

    repo = tmp_path / "repos/emulebb-build"
    repo.mkdir(parents=True)
    monkeypatch.setattr(local_checks, "changed_paths", lambda *args: {"sample.py"})
    monkeypatch.setattr(local_checks, "git", lambda repo, *args: str(repo) if args[0] == "rev-parse" else "")
    monkeypatch.setattr(local_checks, "check_plan", lambda *args: [(repo, ["check"])])
    diagnostic = "registry returned 429: retry-after 637.054\ufffds"
    def failed(command, **kwargs):
        kwargs["stdout"].write(diagnostic)
        return subprocess.CompletedProcess(command, 7)
    monkeypatch.setattr(local_checks.subprocess, "run", failed)
    buffer = io.BytesIO()
    console = io.TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(local_checks.sys, "stdout", console)
    with pytest.raises(RuntimeError, match="Local check failed \\(7\\)"):
        local_checks.run_checks(repo=repo, stage="push", remote="origin", updates=None,
                               root=tmp_path, output=tmp_path / "output")
    console.flush()
    assert b"registry returned 429" in buffer.getvalue() and b"\\ufffd" in buffer.getvalue()
    log = next((tmp_path / "output/reports").rglob("00.log"))
    assert log.read_text(encoding="utf-8") == diagnostic


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


@pytest.mark.parametrize("commit_during_check", [False, True])
def test_push_is_bound_to_checked_head_even_when_tree_stays_clean(tmp_path, monkeypatch, commit_during_check):
    repo = tmp_path / "repos/emulebb-build"
    repo.mkdir(parents=True)
    revision = ["a" * 40]
    def git_state(repo, *args):
        if args == ("rev-parse", "--show-toplevel"):
            return str(repo)
        if args == ("rev-parse", "HEAD"):
            return revision[0]
        return ""
    def check(command, **kwargs):
        if commit_during_check:
            revision[0] = "b" * 40
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(local_checks, "git", git_state)
    monkeypatch.setattr(local_checks, "changed_paths", lambda *args: {"sample.py"})
    monkeypatch.setattr(local_checks, "check_whitespace", lambda *args: None)
    monkeypatch.setattr(local_checks, "check_plan", lambda *args: [(repo, ["check"])])
    monkeypatch.setattr(local_checks.subprocess, "run", check)
    if commit_during_check:
        with pytest.raises(RuntimeError, match="HEAD changed during validation"):
            local_checks.run_checks(repo=repo, stage="push", remote="origin", updates=None, root=tmp_path, output=tmp_path / "output")
    else:
        local_checks.run_checks(repo=repo, stage="push", remote="origin", updates=None, root=tmp_path, output=tmp_path / "output")


def test_workstream_routes_nested_checks_to_selected_builder(tmp_path):
    repo = tmp_path / "workspaces/agents/sample/emulebb-build-tests"
    builder = tmp_path / "workspaces/agents/sample/emulebb-build"
    tooling = tmp_path / "workspaces/agents/sample/emulebb-tooling"
    plan = check_plan(repo, {"scripts/smoke-rust-release-package.py"}, "push", tmp_path,
                      tmp_path / "output", {"emulebb-build": builder, "emulebb-tooling": tooling}, "sample")
    nested = [(cwd, command) for cwd, command in plan if "emule_workspace" in command]
    assert nested and all(cwd == builder for cwd, _ in nested)
    assert all(command[-2:] == ["--workstream", "sample"] for _, command in nested)
    assert plan[0][1][1] == str(tooling / "ci/guard-tracked-files.py")


def test_workstream_rust_push_selects_exact_rest_sources(tmp_path):
    repo = tmp_path / "workspaces/agents/sample/emulebb-rust"
    harness = tmp_path / "workspaces/agents/sample/emulebb-build-tests"
    tooling = tmp_path / "workspaces/agents/sample/emulebb-tooling"
    plan = check_plan(repo, {"crates/sample.rs"}, "push", tmp_path, tmp_path / "output",
                      {"emulebb-rust": repo, "emulebb-build-tests": harness, "emulebb-tooling": tooling}, "sample")
    gate = next(command for _, command in plan if "tools/rust_quality_gate.py" in command)
    assert gate[gate.index("--rest-harness-script") + 1] == str(harness / "scripts/rust-rest-openapi-ci.py")
    assert gate[gate.index("--rest-openapi-contract") + 1] == str(tooling / "docs/products/emulebb-rust/api/REST-API-OPENAPI.yaml")


def test_each_commit_check_has_independent_inputs(tmp_path, monkeypatch):
    repo = tmp_path / "repos/emulebb-build"
    repo.mkdir(parents=True)
    monkeypatch.setattr(local_checks, "git", lambda repo, *args: str(repo) if args[0] == "rev-parse" else "")
    monkeypatch.setattr(local_checks, "changed_paths", lambda *args: {"sample.py"})
    monkeypatch.setattr(local_checks, "check_whitespace", lambda *args: None)
    inputs = []
    def plan(*args):
        inputs.append(args[-1])
        return []
    monkeypatch.setattr(local_checks, "check_plan", plan)
    for _ in range(2):
        local_checks.run_checks(repo=repo, stage="commit", remote="origin", updates=None,
                               root=tmp_path, output=tmp_path / "output")
    assert inputs[0] != inputs[1]
    assert all(path.read_text() == '["sample.py"]' for path in inputs)


def test_checks_do_not_export_hook_repository_context_to_test_children(tmp_path, monkeypatch):
    repo = tmp_path / "repos/emulebb-build"
    repo.mkdir(parents=True)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "other.git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "other.index"))
    monkeypatch.setattr(local_checks, "git", lambda repo, *args: str(repo) if args[0] == "rev-parse" else "")
    monkeypatch.setattr(local_checks, "changed_paths", lambda *args: {"sample.py"})
    monkeypatch.setattr(local_checks, "check_whitespace", lambda *args: None)
    monkeypatch.setattr(local_checks, "check_plan", lambda *args: [(repo, ["check"])])
    def check(command, **kwargs):
        assert "GIT_DIR" not in kwargs["env"]
        assert "GIT_INDEX_FILE" not in kwargs["env"]
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(local_checks.subprocess, "run", check)
    local_checks.run_checks(repo=repo, stage="commit", remote="origin", updates=None,
                           root=tmp_path, output=tmp_path / "output")


@pytest.mark.parametrize("change", ["revision", "dirty"])
def test_workstream_push_rejects_changes_to_supporting_sources(tmp_path, monkeypatch, change):
    import json
    root, output = tmp_path, tmp_path / "output"
    repo = root / "workspaces/agents/sample/emulebb-build"
    source = root / "repos/emulebb-tooling"
    state = {"revision": "a" * 40, "dirty": ""}
    registration = {"id": "sample", "repositories": {"emulebb-build": {"path": str(repo)}}}
    monkeypatch.setattr(local_checks, "_resolve_registration", lambda *args: registration)
    monkeypatch.setattr(local_checks, "_workstream_sources", lambda *args: {"emulebb-build": repo, "emulebb-tooling": source})
    def git_state(path, *args):
        if args == ("rev-parse", "--show-toplevel"):
            return str(path)
        if args == ("rev-parse", "HEAD"):
            return state["revision"] if path == source else "a" * 40
        return state["dirty"] if path == source else ""
    monkeypatch.setattr(local_checks, "git", git_state)
    monkeypatch.setattr(local_checks, "changed_paths", lambda *args: {"sample.py"})
    monkeypatch.setattr(local_checks, "check_whitespace", lambda *args: None)
    monkeypatch.setattr(local_checks, "check_plan", lambda *args: [(repo, ["check"])])
    def check(command, **kwargs):
        state[change] = "b" * 40 if change == "revision" else " M sample.py"
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(local_checks.subprocess, "run", check)
    with pytest.raises(RuntimeError, match="Participating sources changed|clean worktree"):
        local_checks.run_checks(repo=repo, stage="push", remote="origin", updates=None, root=root, output=output)
    evidence = json.loads(next((output / "reports").rglob("sources.json")).read_text())
    assert evidence["sources"]["emulebb-tooling"]["revision"] == "a" * 40
    assert evidence["sources"]["emulebb-build"]["path"] == str(repo)
