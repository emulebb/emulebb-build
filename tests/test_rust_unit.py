from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import os

import pytest

from emule_workspace import test_runs


def _layout(tmp_path: Path) -> SimpleNamespace:
    repo = tmp_path / "repo"
    repo.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    return SimpleNamespace(
        emulebb_rust_repo_root=repo,
        output_root=output,
        output_rust_target_root=output / "builds/rust/target",
    )


@pytest.mark.parametrize("fail", (False, True))
def test_rust_unit_prunes_runnable_cargo_artifacts_even_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail: bool,
) -> None:
    layout = _layout(tmp_path)
    executable = layout.output_rust_target_root / "debug" / "emulebb-rust.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"test-runtime")
    calls: list[list[str]] = []
    parent_environment = os.environ.copy()
    protected = {"EMULEBB_WORKSPACE_ROOT": str(tmp_path / "workspace"),
                 "EMULEBB_WORKSPACE_OUTPUT_ROOT": str(layout.output_root),
                 "CARGO_TARGET_DIR": str(layout.output_rust_target_root), "X_LOCAL_IP": "127.0.0.1"}
    scratch_paths = []

    def fake_run_native(args, **_kwargs) -> None:
        calls.append(list(args))
        environment = _kwargs["env"]
        scratch = Path(environment["TEMP"])
        assert scratch.is_dir() and scratch.is_relative_to(layout.output_root / "tmp/rust-unit")
        assert environment["TMP"] == environment["TMPDIR"] == str(scratch)
        assert all(environment[key] == value for key, value in protected.items())
        scratch_paths.append(scratch)
        if fail:
            raise RuntimeError("test failure")

    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    guarded = []
    monkeypatch.setattr(test_runs, "ensure_rust_source_cache", lambda selected, **kwargs: guarded.append((selected, kwargs)))
    monkeypatch.setattr(test_runs, "_workspace_env", lambda _layout: protected)

    if fail:
        with pytest.raises(RuntimeError, match="test failure"):
            test_runs.invoke_rust_unit_tests(layout, package="emulebb-core")
    else:
        test_runs.invoke_rust_unit_tests(layout, package="emulebb-core")

    assert calls == [["cargo", "test", "--locked", "-p", "emulebb-core"]]
    assert guarded == [(layout, {"prefetch": True})]
    assert not executable.exists()
    assert len(scratch_paths) == 1 and scratch_paths[0].is_dir()
    assert set(os.environ) == set(parent_environment)
    assert all(os.environ[key] == value for key, value in parent_environment.items())


@pytest.mark.parametrize("refresh_fails", [False, True])
def test_explicit_author_refresh_stays_offline_and_cannot_mask_a_failure(tmp_path, monkeypatch, refresh_fails):
    layout = _layout(tmp_path)
    executable = layout.output_rust_target_root / "debug/emulebb-rust.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"test-runtime")
    calls = []
    child_environments = []
    parent_environment = os.environ.copy()
    def invoke(arguments, **kwargs):
        calls.append(arguments)
        child_environments.append(kwargs["env"])
        if refresh_fails:
            raise RuntimeError("offline workspace resolution failed")
    monkeypatch.setattr(test_runs, "run_native", invoke)
    monkeypatch.setattr(test_runs, "ensure_rust_source_cache", lambda selected, **kwargs: None)
    monkeypatch.setattr(test_runs, "_workspace_env", lambda layout: {})
    if refresh_fails:
        with pytest.raises(RuntimeError, match="offline workspace resolution failed"):
            test_runs.invoke_rust_unit_tests(layout, refresh_workspace_lock=True)
        assert len(calls) == 1
    else:
        test_runs.invoke_rust_unit_tests(layout, refresh_workspace_lock=True)
        assert calls[1] == ["cargo", "test", "--locked", "--workspace"]
    assert calls[0] == ["cargo", "update", "--workspace", "--offline"]
    assert not executable.exists()
    assert all(item["TEMP"] == item["TMP"] == item["TMPDIR"] for item in child_environments)
    assert len({item["TEMP"] for item in child_environments}) == 1
    assert Path(child_environments[0]["TEMP"]).is_dir()
    assert set(os.environ) == set(parent_environment)
    assert all(os.environ[key] == value for key, value in parent_environment.items())


def test_rust_unit_runs_have_distinct_owned_scratch_without_changing_parent_temp(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    parent_environment = os.environ.copy()
    observed = []
    monkeypatch.setattr(test_runs, "_workspace_env", lambda _layout: {})
    monkeypatch.setattr(test_runs, "ensure_rust_source_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(test_runs, "run_native", lambda args, **kwargs: observed.append(Path(kwargs["env"]["TEMP"])))
    for _ in range(2):
        test_runs.invoke_rust_unit_tests(layout, package="emulebb-metadata")
    assert len(set(observed)) == 2
    assert all(path.is_dir() and path.is_relative_to(layout.output_root / "tmp/rust-unit") for path in observed)
    assert set(os.environ) == set(parent_environment)
    assert all(os.environ[key] == value for key, value in parent_environment.items())
