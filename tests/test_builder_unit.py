"""Builder-owned unit runner boundaries and source/JUnit evidence."""
from pathlib import Path
from types import SimpleNamespace
import json
import os
import subprocess

import pytest

from emule_workspace import test_runs
from emule_workspace.process import PythonInvocation


@pytest.fixture
def builder(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    root = workspace / "repos/emulebb-build"
    (root / "tests").mkdir(parents=True)
    (root / "tests/test_sample.py").write_text("def test_sample(): pass\n", encoding="utf-8")
    output = tmp_path / "output"
    layout = SimpleNamespace(build_repo_root=root, emule_workspace_root=workspace,
                             output_root=output, subprocess_environment=lambda: {})
    monkeypatch.setattr("emule_workspace.workstreams._git", lambda path, _policy, *args:
                        str(root) if args == ("rev-parse", "--show-toplevel") else
                        "a" * 40 if args == ("rev-parse", "HEAD") else "")
    monkeypatch.setattr(test_runs, "get_python_invocation", lambda: PythonInvocation(Path("synthetic-python")))
    calls = []
    def execute(command, **kwargs):
        calls.append((command, kwargs))
        kwargs["stdout"].write("Synthetic scoped unit output\n")
        Path(command[command.index("--junitxml") + 1]).write_text(
            '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"/></testsuites>', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(test_runs.subprocess, "run", execute)
    return layout, calls


def test_selected_builder_runner_retains_hashed_source_and_junit_evidence(builder):
    layout, calls = builder
    report = test_runs.invoke_builder_unit_tests(layout, paths=("tests/test_sample.py",))
    value = json.loads((report / "results.json").read_text())
    command, options = calls[0]
    assert options["cwd"] == layout.build_repo_root
    assert command[1:4] == ["-B", "-m", "pytest"]
    assert command[command.index("--basetemp") + 1].startswith(str(layout.output_root))
    assert value["source"] == {"path": str(layout.build_repo_root), "head": "a" * 40}
    assert value["status"] == "passed" and value["exitCode"] == 0
    assert value["junit"]["tests"] == 1
    assert len(value["stdoutSha256"]) == len(value["junitSha256"]) == 64


@pytest.mark.parametrize("name", ["../test_sample.py", "tests/../tests/test_sample.py", "tests/nested/test_sample.py",
                                  "tests/test_missing.py", "C:/outside/test_sample.py"])
def test_builder_runner_refuses_nonowned_or_missing_paths_before_writes(builder, name):
    layout, calls = builder
    with pytest.raises(ValueError):
        test_runs.invoke_builder_unit_tests(layout, paths=(name,))
    assert calls == []
    assert not layout.output_root.exists()


def test_builder_failure_is_retained_without_running_another_suite(builder, monkeypatch):
    layout, calls = builder
    def failed(command, **kwargs):
        calls.append(command)
        kwargs["stdout"].write("Synthetic failure\n")
        return subprocess.CompletedProcess(command, 7)
    monkeypatch.setattr(test_runs.subprocess, "run", failed)
    with pytest.raises(RuntimeError, match="failed \\(7\\)"):
        test_runs.invoke_builder_unit_tests(layout, paths=("tests/test_sample.py",))
    results = list((layout.output_root / "reports/builder-unit").glob("*/results.json"))
    assert len(results) == 1 and len(calls) == 1
    value = json.loads(results[0].read_text())
    assert value["status"] == "failed" and value["exitCode"] == 7


def test_builder_source_change_invalidates_successful_test_exit(builder, monkeypatch):
    layout, _calls = builder
    count = 0
    def git(path, _policy, *args):
        nonlocal count
        if args == ("rev-parse", "--show-toplevel"):
            return str(layout.build_repo_root)
        if args == ("rev-parse", "HEAD"):
            count += 1
            return ("a" if count == 1 else "b") * 40
        return ""
    monkeypatch.setattr("emule_workspace.workstreams._git", git)
    with pytest.raises(RuntimeError, match="source changed"):
        test_runs.invoke_builder_unit_tests(layout, paths=("tests/test_sample.py",))
    value = json.loads(next((layout.output_root / "reports/builder-unit").glob("*/results.json")).read_text())
    assert value["status"] == "failed" and value["exitCode"] == 0


@pytest.mark.parametrize("content", ['<testsuites/>',
                                    '<testsuite tests="1" failures="0" errors="0" skipped="1"/>',
                                    '<testsuite tests="2" failures="0" errors="0" skipped="1"/>'])
def test_empty_or_all_skipped_junit_cannot_qualify_success(builder, monkeypatch, content):
    layout, _calls = builder
    def execute(command, **kwargs):
        Path(command[command.index("--junitxml") + 1]).write_text(content, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(test_runs.subprocess, "run", execute)
    with pytest.raises(RuntimeError, match="nonempty suite"):
        test_runs.invoke_builder_unit_tests(layout, paths=("tests/test_sample.py",))
    value = json.loads(next((layout.output_root / "reports/builder-unit").glob("*/results.json")).read_text())
    assert value["status"] == "failed" and value["exitCode"] == 0


def test_builder_identity_queries_and_pytest_drop_foreign_git_bindings(tmp_path_factory, monkeypatch):
    from emule_workspace.git_environment import git_environment
    root = tmp_path_factory.mktemp("builder-git")
    workspace, output = root / "workspace", root / "output"
    source, foreign = workspace / "repos/emulebb-build", workspace / "repos/foreign"
    real_run = subprocess.run
    def git(path, *arguments):
        return real_run(["git", "--no-optional-locks", "-C", str(path), *arguments],
                        env=git_environment(), text=True, capture_output=True, check=True).stdout.strip()
    for path, message in ((source, "source"), (foreign, "foreign")):
        (path / "tests").mkdir(parents=True)
        (path / "tests/test_sample.py").write_text("def test_sample(): pass\n", encoding="utf-8")
        git(path, "init", "-q")
        git(path, "add", "--", "tests/test_sample.py")
        git(path, "-c", "user.name=Synthetic Test", "-c", "user.email=test@example.invalid", "commit", "-qm", message)
    selected_head = git(source, "rev-parse", "HEAD")
    assert selected_head != git(foreign, "rev-parse", "HEAD")
    retained = {path: path.read_bytes() for repo in (source, foreign)
                for path in (repo / ".git/index", repo / ".git/config", repo / ".git/HEAD")}
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(source))
    monkeypatch.setenv("GIT_INDEX_FILE", str(foreign / ".git/index"))
    environment = os.environ.copy()
    layout = SimpleNamespace(build_repo_root=source, emule_workspace_root=workspace,
                             output_root=output, subprocess_environment=lambda: {})
    monkeypatch.setattr(test_runs, "get_python_invocation", lambda: PythonInvocation(Path("synthetic-python")))
    def route(command, **kwargs):
        if command[0] == "git":
            return real_run(command, **kwargs)
        assert not {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}.intersection(kwargs["env"])
        Path(command[command.index("--junitxml") + 1]).write_text(
            '<testsuite tests="1" failures="0" errors="0" skipped="0"/>', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(test_runs.subprocess, "run", route)
    report = test_runs.invoke_builder_unit_tests(layout, paths=("tests/test_sample.py",))
    value = json.loads((report / "results.json").read_text())
    assert value["source"]["head"] == selected_head
    assert os.environ == environment
    assert retained == {path: path.read_bytes() for path in retained}


def test_builder_cli_uses_selected_builder_owner_and_existing_lock(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from emule_workspace import cli
    layout = SimpleNamespace(emule_workspace_root=tmp_path, output_root=tmp_path / "output",
                             build_repo_root=tmp_path / "repos/emulebb-build")
    options = SimpleNamespace(workspace_root=tmp_path, workspace_name="workspace", output_root=layout.output_root)
    monkeypatch.setattr(cli, "resolve_workspace_options", lambda **kwargs: options)
    monkeypatch.setattr(cli, "load_layout", lambda *args, **kwargs: layout)
    calls = []
    def locked(name, function):
        def invoke(**kwargs):
            calls.append(name)
            return function(**kwargs)
        return invoke
    monkeypatch.setattr(cli, "_locked", locked)
    monkeypatch.setattr(cli, "invoke_builder_unit_tests", lambda selected, **kwargs: calls.append((selected, kwargs)))
    result = CliRunner().invoke(cli.main, ["test", "builder-unit", "--path", "tests/test_sample.py"])
    assert result.exit_code == 0, result.output
    assert calls == ["test builder-unit", (layout, {"paths": ("tests/test_sample.py",)})]
