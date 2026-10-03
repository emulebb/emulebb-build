from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from emule_workspace import test_runs


def _layout(tmp_path: Path) -> SimpleNamespace:
    repo = tmp_path / "repo"
    repo.mkdir()
    return SimpleNamespace(
        emulebb_rust_repo_root=repo,
        output_rust_target_root=tmp_path / "target",
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

    def fake_run_native(args, **_kwargs) -> None:
        calls.append(list(args))
        if fail:
            raise RuntimeError("test failure")

    monkeypatch.setattr(test_runs, "run_native", fake_run_native)
    monkeypatch.setattr(test_runs, "_workspace_env", lambda _layout: {})

    if fail:
        with pytest.raises(RuntimeError, match="test failure"):
            test_runs.invoke_rust_unit_tests(layout, package="emulebb-core")
    else:
        test_runs.invoke_rust_unit_tests(layout, package="emulebb-core")

    assert calls == [["cargo", "test", "--locked", "-p", "emulebb-core"]]
    assert not executable.exists()
