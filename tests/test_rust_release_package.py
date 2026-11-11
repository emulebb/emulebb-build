"""Exercise package proof admission/source fences without launching a daemon."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from emule_workspace import local_checks, test_runs, workstreams
from emule_workspace.process import PythonInvocation


@pytest.fixture
def package_context(tmp_path, monkeypatch):
    workspace, output = tmp_path / "workspace", tmp_path / "output"
    root = workspace / "workspaces/agents/sample"
    paths = {name: root / name for name in
             ("emulebb-build", "emulebb-rust", "emulebb-build-tests", "emulebb-tooling")}
    for path in paths.values():
        path.mkdir(parents=True)
    script = paths["emulebb-build-tests"] / "scripts/smoke-rust-release-package.py"
    script.parent.mkdir()
    script.write_text("fixture", encoding="utf-8")
    release = output / "release/candidate"
    release.mkdir(parents=True)
    layout = SimpleNamespace(emule_workspace_root=workspace, output_root=output,
                             build_repo_root=paths["emulebb-build"], emulebb_rust_repo_root=paths["emulebb-rust"],
                             tests_repo_root=paths["emulebb-build-tests"], tooling_repo_root=paths["emulebb-tooling"])
    snapshot = {name: {"path": str(path), "revision": "a" * 40, "dirty": ""} for name, path in paths.items()}
    monkeypatch.setattr(local_checks, "_source_snapshot", lambda selected, **kwargs: json.loads(json.dumps(snapshot)))
    monkeypatch.setattr(workstreams, "resolve_workstream", lambda *args, **kwargs: {"id": "sample"})
    monkeypatch.setattr(test_runs, "_workspace_env", lambda selected: {"EMULEBB_WORKSPACE_OUTPUT_ROOT": str(output)})
    monkeypatch.setattr(test_runs, "get_python_invocation", lambda: PythonInvocation(Path("private-python")))
    calls = []

    def smoke(command, **kwargs):
        calls.append((command, kwargs))
        report = Path(command[command.index("--report-path") + 1])
        report.write_text('{"status":"passed"}', encoding="utf-8")

    monkeypatch.setattr(test_runs, "run_native", smoke)
    return layout, release, snapshot, calls


def invoke(context, **kwargs):
    layout, release, _, _ = context
    return test_runs.invoke_rust_release_package(layout, SimpleNamespace(platform="x64"),
        release_dir=release, release_version="0.1.0-beta.2", target_os="windows", **kwargs)


def test_package_adapter_binds_current_sources_and_retains_both_proofs(package_context):
    layout, _, snapshot, calls = package_context
    report = invoke(package_context, verify_profile_ownership=True, render_webui=True)
    command, context = calls[0]
    assert command[command.index("--expected-source-sha") + 1] == snapshot["emulebb-rust"]["revision"]
    assert command[command.index("--rust-repo") + 1] == str(layout.emulebb_rust_repo_root)
    assert command[2] == str(layout.tests_repo_root / "scripts/smoke-rust-release-package.py")
    assert "--verify-profile-ownership" in command and "--render-webui" in command
    assert context["cwd"] == layout.tests_repo_root
    assert Path(context["env"]["TEMP"]).is_relative_to(layout.output_root / "tmp/rust-release-package")
    receipt = json.loads((report / "sources.json").read_text())
    assert receipt["sources"] == snapshot and receipt["status"] == "passed"


@pytest.mark.parametrize("fault", ["outside", "script", "dirty"])
def test_invalid_package_proof_source_is_rejected_before_any_launch(package_context, monkeypatch, tmp_path, fault):
    layout, _, _, calls = package_context
    if fault == "outside":
        outside = tmp_path / "unowned"
        outside.mkdir()
        package_context = (layout, outside, {}, calls)
    elif fault == "script":
        (layout.tests_repo_root / "scripts/smoke-rust-release-package.py").unlink()
    else:
        def dirty(*args, **kwargs):
            raise RuntimeError("source requires clean checkout")
        monkeypatch.setattr(local_checks, "_source_snapshot", dirty)
    with pytest.raises(RuntimeError):
        invoke(package_context)
    assert not calls and not (layout.output_root / "reports").exists()


def test_changed_clean_support_head_cannot_borrow_successful_package_result(package_context, monkeypatch):
    layout, _, snapshot, calls = package_context
    original = test_runs.run_native
    def changed(command, **kwargs):
        original(command, **kwargs)
        snapshot["emulebb-tooling"]["revision"] = "b" * 40
    monkeypatch.setattr(test_runs, "run_native", changed)
    with pytest.raises(RuntimeError, match="source set changed"):
        invoke(package_context)
    report = Path(calls[0][0][calls[0][0].index("--report-path") + 1])
    assert json.loads(report.with_name("sources.json").read_text())["status"] == "failed"
    assert json.loads(report.read_text())["status"] == "passed"


def test_missing_persisted_result_cannot_count_as_success(package_context, monkeypatch):
    monkeypatch.setattr(test_runs, "run_native", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="did not retain"):
        invoke(package_context)
