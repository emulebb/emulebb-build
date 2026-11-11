"""Prove REST command/source admission without launching a daemon."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from emule_workspace import test_runs, workstreams
from emule_workspace.process import PythonInvocation


def selected_layout(tmp_path):
    workspace, output = tmp_path / "workspace", tmp_path / "output"
    source = workspace / "workspaces/agents/sample"
    rust, harness, tooling = (source / name for name in ("emulebb-rust", "emulebb-build-tests", "emulebb-tooling"))
    rust.mkdir(parents=True)
    script = harness / "scripts/rust-rest-openapi-ci.py"
    contract = tooling / "docs/products/emulebb-rust/api/REST-API-OPENAPI.yaml"
    for path in (script, contract):
        path.parent.mkdir(parents=True)
        path.write_text("fixture", encoding="utf-8")
    return SimpleNamespace(emule_workspace_root=workspace, output_root=output,
                           emulebb_rust_repo_root=rust, tests_repo_root=harness,
                           tooling_repo_root=tooling), script, contract


@pytest.mark.parametrize("registered", [False, True])
def test_rest_adapter_preserves_selected_sources_and_environment(tmp_path, monkeypatch, registered):
    layout, script, contract = selected_layout(tmp_path)
    environment = {"EMULEBB_WORKSPACE_ROOT": str(layout.emule_workspace_root),
                   "EMULEBB_WORKSPACE_OUTPUT_ROOT": str(layout.output_root),
                   "CARGO_TARGET_DIR": str(layout.output_root / "builds/rust/target"),
                   "X_LOCAL_IP": "127.0.0.1", "lock-fixture": "delegated"}
    original = os.environ.copy()
    monkeypatch.setattr(test_runs, "_workspace_env", lambda selected: environment)
    monkeypatch.setattr(test_runs, "get_python_invocation", lambda: PythonInvocation(Path("selected-python")))
    selection = []
    def resolve(root, output, *, repo):
        selection.append((root, output, repo))
        return {"id": "sample"} if registered else None
    monkeypatch.setattr(workstreams, "resolve_workstream", resolve)
    calls = []
    monkeypatch.setattr(test_runs, "run_native", lambda command, **kwargs: calls.append((command, kwargs)))
    test_runs.invoke_rust_rest_openapi(layout)
    expected = ["selected-python", "-B", str(script), "--rust-repo", str(layout.emulebb_rust_repo_root),
                "--openapi-contract", str(contract)] + (["--registered-stage"] if registered else [])
    assert calls == [(expected, {"label": "Rust REST/OpenAPI conformance", "cwd": layout.tests_repo_root,
                                "env": environment})]
    assert selection == [(layout.emule_workspace_root, layout.output_root, layout.emulebb_rust_repo_root)]
    assert os.environ == original


@pytest.mark.parametrize("missing", ["rust", "script", "contract", "outside"])
def test_rest_adapter_rejects_invalid_source_before_launch(tmp_path, monkeypatch, missing):
    layout, script, contract = selected_layout(tmp_path)
    if missing == "rust":
        layout.emulebb_rust_repo_root = None
    elif missing == "outside":
        layout.tests_repo_root = tmp_path / "unowned-harness"
    else:
        (script if missing == "script" else contract).unlink()
    monkeypatch.setattr(test_runs, "run_native", lambda *args, **kwargs:
                        pytest.fail("invalid source reached persisted campaign launch"))
    with pytest.raises(RuntimeError):
        test_runs.invoke_rust_rest_openapi(layout)


def test_rest_adapter_propagates_campaign_failure(tmp_path, monkeypatch):
    layout, _, _ = selected_layout(tmp_path)
    monkeypatch.setattr(workstreams, "resolve_workstream", lambda *args, **kwargs: {"id": "sample"})
    monkeypatch.setattr(test_runs, "_workspace_env", lambda selected: {})
    def fail(*args, **kwargs):
        raise RuntimeError("persisted campaign failed")
    monkeypatch.setattr(test_runs, "run_native", fail)
    with pytest.raises(RuntimeError, match="persisted campaign failed"):
        test_runs.invoke_rust_rest_openapi(layout)
