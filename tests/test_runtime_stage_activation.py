"""Exercise registered native routing without Cargo, npm or process launches."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from emule_workspace import build, rust_runtime_stage


def test_registered_native_entry_prepares_before_build_and_discards_on_failure(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    session = SimpleNamespace(layout=SimpleNamespace(emulebb_rust_repo_root=source),
                              options=SimpleNamespace(platform="x64"))
    description = {"pendingRoot": str(tmp_path / "pending")}
    calls = []
    monkeypatch.setattr(rust_runtime_stage, "stage_root", lambda *args: tmp_path / "owned")
    monkeypatch.setattr(rust_runtime_stage, "prepare_stage", lambda *args, **kwargs:
                        calls.append("prepare") or description)
    monkeypatch.setattr(rust_runtime_stage, "discard_stage", lambda *args:
                        calls.append("discard"))

    def fail(*args, **kwargs):
        assert calls == ["prepare"]
        assert kwargs["runtime_description"] == description
        raise RuntimeError("synthetic native build failure")

    monkeypatch.setattr(build, "_build_emulebb_rust_client", fail)
    with pytest.raises(RuntimeError, match="synthetic native build failure"):
        build.build_emulebb_rust_client(session, clean=True)
    assert calls == ["prepare", "discard"]


def test_registered_low_level_writers_require_an_unpublished_bundle(tmp_path, monkeypatch):
    layout = SimpleNamespace(output_tools_root=tmp_path / "tools")
    monkeypatch.setattr(rust_runtime_stage, "stage_root", lambda *args: tmp_path / "owned")
    with pytest.raises(RuntimeError, match="unpublished owned bundle"):
        build._rust_runtime_bin_dir(layout, None, target="x86_64-pc-windows-msvc")
    description = {"pendingRoot": str(tmp_path / "pending")}
    monkeypatch.setattr(rust_runtime_stage, "pending_bin_dir",
                        lambda selected, value: Path(value["pendingRoot"]) / "bin")
    assert build._rust_runtime_bin_dir(layout, description) == tmp_path / "pending/bin"


def test_canonical_writer_default_keeps_the_operator_stage(tmp_path, monkeypatch):
    layout = SimpleNamespace(output_tools_root=tmp_path / "tools")
    monkeypatch.setattr(rust_runtime_stage, "stage_root", lambda *args: None)
    assert build._rust_runtime_bin_dir(layout, None) == tmp_path / "tools/emulebb-rust/bin"


@pytest.mark.parametrize("diagnostics", [False, True])
def test_native_producer_rejects_wrong_bundle_target_before_copy(tmp_path, monkeypatch, diagnostics):
    producer_target = "aarch64-pc-windows-msvc"
    root = tmp_path / "cargo-target"
    source = root / producer_target / "release"
    source.mkdir(parents=True)
    name = "emulebb-rust-diagnostics.exe" if diagnostics else "emulebb-rust.exe"
    (source / name).write_bytes(b"ARM64 producer fixture")
    layout = SimpleNamespace(output_rust_target_root=root)
    pending = tmp_path / "x64-pending"
    description = {"target": "x86_64-pc-windows-msvc", "pendingRoot": str(pending)}

    def unexpected(*args, **kwargs):
        raise AssertionError("wrong-target producer reached pending admission or file copying")

    monkeypatch.setattr(rust_runtime_stage, "pending_bin_dir", unexpected)
    monkeypatch.setattr(build, "_copy_rust_runtime_artifact", unexpected)
    with pytest.raises(RuntimeError, match="producer target must match"):
        build.stage_emulebb_rust_runtime(layout, producer_target, diagnostics=diagnostics,
                                        runtime_description=description)
    assert not pending.exists()
    assert (source / name).read_bytes() == b"ARM64 producer fixture"
