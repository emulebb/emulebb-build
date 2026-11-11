"""Prove source ownership, companion integrity and recoverable bundle publication."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from emule_workspace import release
from emule_workspace import rust_runtime_stage as stage

TARGET = "x86_64-pc-windows-msvc"


@pytest.fixture
def selected(tmp_path, monkeypatch):
    workspace, output = tmp_path / "workspace", tmp_path / "output"
    source = workspace / "workspaces/agents/sample/emulebb-rust"
    native = workspace / "repos/native-fixture"
    for root in (source, native):
        root.mkdir(parents=True)
        (root / "material.txt").write_text("source one", encoding="utf-8")
    layout = SimpleNamespace(emule_workspace_root=workspace, output_root=output,
                             emulebb_rust_repo_root=source, workspace_name="workspace")
    monkeypatch.setattr(stage, "resolve_required_workspace_roots", lambda: (workspace, output))
    monkeypatch.setattr(stage, "_require_lock", lambda layout: None)

    def registration(root, selected_output, *, repo):
        assert (root, selected_output, repo) == (workspace, output, source)
        return {"id": "sample"}

    def materials(roots):
        return [{"root": str(root), "sha256": hashlib.sha256((root / "material.txt").read_bytes()).hexdigest()}
                for root in roots]

    monkeypatch.setattr(stage, "resolve_workstream", registration)
    monkeypatch.setattr(stage, "_source_identity", materials)
    monkeypatch.setattr(stage.subprocess, "check_output", lambda *args, **kwargs: "a" * 40)
    monkeypatch.setattr(release, "_emulebb_rust_native_source_roots", lambda layout: (native,))
    operator = output / "tools/emulebb-rust/bin"
    (operator / "webui").mkdir(parents=True)
    (operator / "emulebb-rust.exe").write_bytes(b"main regular")
    (operator / "emulebb-rust-diagnostics.exe").write_bytes(b"main diagnostics")
    (operator / "webui/index.html").write_bytes(b"main WebUI")
    return layout


def snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def bundle(description, *, flavor="fresh", value=b"candidate"):
    root = Path(description["pendingRoot"])
    (root / "bin/webui").mkdir()
    (root / "bin/webui/index.html").write_bytes(value + b" WebUI")
    names = {"fresh": stage.BINARIES, "regular": (stage.BINARIES[0],),
             "diagnostics": (stage.BINARIES[1],)}[flavor]
    for name in names:
        (root / "bin" / (name + ".exe")).write_bytes(value + name.encode())
        (root / "bin" / (name + ".pdb")).write_bytes(value + b" symbols")
    return root


def test_complete_registered_bundle_preserves_operator_executables_and_webui(selected):
    operator = selected.output_root / "tools/emulebb-rust"
    before = snapshot(operator)
    description = stage.prepare_stage(selected, target=TARGET)
    bundle(description)
    published = stage.publish_stage(selected, description)
    assert snapshot(operator) == before
    verified = stage.verify_stage(selected, target=TARGET)
    assert verified["source"] == description["source"]
    assert verified["workstream"] == "sample"
    assert verified["files"] == published["files"]
    assert len(verified["files"]) == 7
    assert Path(verified["executable"]).parent == Path(published["binDir"])
    assert not Path(description["pendingRoot"]).exists()


def test_partial_flavor_preserves_only_proven_same_source_companions(selected):
    first = stage.prepare_stage(selected, target=TARGET)
    bundle(first)
    stage.publish_stage(selected, first)
    root = Path(first["stageRoot"])
    diagnostics = (root / "bin/emulebb-rust-diagnostics.exe").read_bytes()
    second = stage.prepare_stage(selected, target=TARGET, flavor="regular")
    bundle(second, flavor="regular", value=b"rebuilt")
    stage.publish_stage(selected, second)
    assert (root / "bin/emulebb-rust-diagnostics.exe").read_bytes() == diagnostics
    assert (root / "bin/webui/index.html").read_bytes() == b"rebuilt WebUI"
    stage.verify_stage(selected, target=TARGET)


@pytest.mark.parametrize("flavor,rebuilt,untouched", [
    ("regular", "emulebb-rust", "emulebb-rust-diagnostics"),
    ("diagnostics", "emulebb-rust-diagnostics", "emulebb-rust"),
])
def test_rebuilt_executable_cannot_inherit_previous_pdb_aliases(selected, flavor, rebuilt, untouched):
    first = stage.prepare_stage(selected, target=TARGET)
    pending = bundle(first)
    for name in stage.BINARIES:
        (pending / "bin" / (name.replace("-", "_") + ".pdb")).write_bytes(b"previous symbol alias")
    stage.publish_stage(selected, first)
    second = stage.prepare_stage(selected, target=TARGET, flavor=flavor)
    pending = bundle(second, flavor=flavor, value=b"different executable")
    (pending / "bin" / (rebuilt + ".pdb")).unlink()
    result = stage.publish_stage(selected, second)
    root = Path(second["stageRoot"])
    assert not (root / "bin" / (rebuilt + ".pdb")).exists()
    assert not (root / "bin" / (rebuilt.replace("-", "_") + ".pdb")).exists()
    assert (root / "bin" / (untouched + ".pdb")).is_file()
    assert (root / "bin" / (untouched.replace("-", "_") + ".pdb")).is_file()
    assert result["producerFlavor"] == flavor
    stage.verify_stage(selected, target=TARGET, diagnostics=flavor == "diagnostics")


def test_new_source_cannot_relabel_old_regular_or_diagnostics_files(selected):
    first = stage.prepare_stage(selected, target=TARGET)
    bundle(first)
    stage.publish_stage(selected, first)
    (selected.emulebb_rust_repo_root / "material.txt").write_text("source two", encoding="utf-8")
    second = stage.prepare_stage(selected, target=TARGET, flavor="diagnostics")
    bundle(second, flavor="diagnostics", value=b"new source")
    stage.publish_stage(selected, second)
    with pytest.raises(RuntimeError, match="selected executable flavor"):
        stage.verify_stage(selected, target=TARGET)
    verified = stage.verify_stage(selected, target=TARGET, diagnostics=True)
    assert verified["source"] == second["source"]
    assert not (Path(second["stageRoot"]) / "bin/emulebb-rust.exe").exists()


@pytest.mark.parametrize("relative", ["bin/emulebb-rust.exe", "bin/emulebb-rust.pdb", "bin/webui/index.html"])
def test_modified_native_symbol_or_webui_bytes_fail_admission(selected, relative):
    description = stage.prepare_stage(selected, target=TARGET)
    bundle(description)
    stage.publish_stage(selected, description)
    (Path(description["stageRoot"]) / relative).write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="bundle bytes"):
        stage.verify_stage(selected, target=TARGET)


def test_source_change_during_build_preserves_previous_ready_bundle(selected):
    first = stage.prepare_stage(selected, target=TARGET)
    bundle(first)
    stage.publish_stage(selected, first)
    root = Path(first["stageRoot"])
    before = snapshot(root)
    second = stage.prepare_stage(selected, target=TARGET)
    bundle(second, value=b"new build")
    (selected.emulebb_rust_repo_root / "material.txt").write_text("changed during build", encoding="utf-8")
    with pytest.raises(RuntimeError, match="identity changed"):
        stage.publish_stage(selected, second)
    assert snapshot(root) == before
    stage.discard_stage(selected, second)


@pytest.mark.parametrize("after_move", [False, True])
def test_failed_windows_directory_promotion_restores_ready_stage_and_receipt(selected, monkeypatch, after_move):
    first = stage.prepare_stage(selected, target=TARGET)
    bundle(first)
    stage.publish_stage(selected, first)
    root = Path(first["stageRoot"])
    before = snapshot(root)
    second = stage.prepare_stage(selected, target=TARGET)
    pending = bundle(second, value=b"replacement")
    replace = os.replace
    failed = False

    def fail_promotion(source, destination):
        nonlocal failed
        if Path(source) == pending and not failed:
            failed = True
            if after_move:
                replace(source, destination)
            raise OSError("synthetic promotion failure")
        return replace(source, destination)

    monkeypatch.setattr(stage.os, "replace", fail_promotion)
    with pytest.raises(OSError, match="synthetic promotion failure"):
        stage.publish_stage(selected, second)
    assert snapshot(root) == before
    stage.verify_stage(selected, target=TARGET)
    stage.discard_stage(selected, second)
    assert snapshot(root) == before


def test_cancellation_after_old_stage_rename_restores_previous_ready_bundle(selected, monkeypatch):
    first = stage.prepare_stage(selected, target=TARGET)
    bundle(first)
    stage.publish_stage(selected, first)
    root = Path(first["stageRoot"])
    before = snapshot(root)
    second = stage.prepare_stage(selected, target=TARGET)
    bundle(second, value=b"replacement")
    replace = os.replace
    cancelled = False

    def cancel_after_backup(source, destination):
        nonlocal cancelled
        result = replace(source, destination)
        if Path(source) == root and not cancelled:
            cancelled = True
            raise KeyboardInterrupt("synthetic cancellation after backup rename")
        return result

    monkeypatch.setattr(stage.os, "replace", cancel_after_backup)
    with pytest.raises(KeyboardInterrupt, match="synthetic cancellation"):
        stage.publish_stage(selected, second)
    stage.discard_stage(selected, second)
    assert snapshot(root) == before
    stage.verify_stage(selected, target=TARGET)


def test_another_output_directory_cannot_be_published_or_discarded(selected):
    description = stage.prepare_stage(selected, target=TARGET)
    bundle(description)
    other = selected.output_root / "tools/emulebb-rust"
    before = snapshot(other)
    invalid = {**description, "pendingRoot": str(other)}
    for operation in (stage.publish_stage, stage.discard_stage):
        with pytest.raises(RuntimeError, match="pending directory"):
            operation(selected, invalid)
    assert snapshot(other) == before


def test_changed_receipt_identity_fails_before_executable_admission(selected):
    description = stage.prepare_stage(selected, target=TARGET)
    bundle(description)
    result = stage.publish_stage(selected, description)
    receipt = Path(result["receipt"])
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["source"]["revision"] = "b" * 40
    receipt.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(RuntimeError, match="selected source identity"):
        stage.verify_stage(selected, target=TARGET)


def test_nonregistered_defaults_keep_their_existing_stage_owner(selected, monkeypatch):
    monkeypatch.setattr(stage, "resolve_workstream", lambda *args, **kwargs: None)
    assert stage.stage_root(selected, TARGET) is None


@pytest.mark.parametrize("interrupted", [False, True])
def test_backup_cleanup_failure_keeps_committed_ready_bytes(selected, monkeypatch, capsys, interrupted):
    first = stage.prepare_stage(selected, target=TARGET)
    bundle(first)
    stage.publish_stage(selected, first)
    second = stage.prepare_stage(selected, target=TARGET)
    bundle(second, value=b"replacement")
    backups = []

    def fail_cleanup(path):
        backups.append(Path(path))
        (Path(path) / "bin/emulebb-rust.exe").unlink()
        if interrupted:
            raise KeyboardInterrupt("synthetic interrupted backup cleanup")
        raise PermissionError("synthetic locked previous PDB")

    monkeypatch.setattr(stage.shutil, "rmtree", fail_cleanup)
    if interrupted:
        with pytest.raises(KeyboardInterrupt, match="interrupted backup cleanup"):
            stage.publish_stage(selected, second)
        assert capsys.readouterr().err == ""
    else:
        result = stage.publish_stage(selected, second)
        warning = result["cleanupWarnings"][0]
        assert warning == {"operation": "remove-previous-runtime-stage", "path": str(backups[0]),
                           "error": "synthetic locked previous PDB"}
        assert str(backups[0]) in capsys.readouterr().err
        assert result["receipt"] == str(Path(second["stageRoot"]) / stage.RECEIPT)
    root = Path(second["stageRoot"])
    assert (root / "bin/emulebb-rust.exe").read_bytes() == b"replacementemulebb-rust"
    assert (root / "bin/webui/index.html").read_bytes() == b"replacement WebUI"
    stage.verify_stage(selected, target=TARGET)
    assert not Path(second["pendingRoot"]).exists()
    assert backups[0].parent == root.parent
    assert backups[0].name.startswith("." + root.name + ".")
    assert backups[0].name.endswith(".previous")
    assert backups[0].exists()


def test_runtime_retirement_holds_heavy_lock_and_preserves_operator_bundle(selected, monkeypatch):
    description = stage.prepare_stage(selected, target=TARGET)
    bundle(description)
    stage.publish_stage(selected, description)
    operator = selected.output_root / "tools/emulebb-rust"
    before = snapshot(operator)
    state = {"held": False, "released": False}

    def acquire():
        state["held"] = True
        return True

    def release_lock():
        assert state["held"]
        state["held"] = False
        state["released"] = True

    lock = SimpleNamespace(acquire=acquire, release=release_lock)
    monkeypatch.setattr(stage, "WorkspaceLock", lambda *args, **kwargs: lock)
    with stage.runtime_stage_retirement(selected, "sample") as retire:
        assert state["held"]
        assert Path(description["stageRoot"]).exists()
        retire()
        assert state["held"]
        assert not Path(description["stageRoot"]).exists()
    assert state["released"]
    assert snapshot(operator) == before


def test_runtime_retirement_preflight_failure_preserves_ready_stage(selected, monkeypatch):
    description = stage.prepare_stage(selected, target=TARGET)
    bundle(description)
    stage.publish_stage(selected, description)
    root = Path(description["stageRoot"])
    before = snapshot(root)
    released = []
    lock = SimpleNamespace(acquire=lambda: True, release=lambda: released.append(True))
    monkeypatch.setattr(stage, "WorkspaceLock", lambda *args, **kwargs: lock)
    with pytest.raises(RuntimeError, match="synthetic source preflight"):
        with stage.runtime_stage_retirement(selected, "sample"):
            raise RuntimeError("synthetic source preflight failure")
    assert snapshot(root) == before
    assert released == [True]


def test_busy_runtime_retirement_preserves_the_ready_bundle(selected, monkeypatch):
    description = stage.prepare_stage(selected, target=TARGET)
    bundle(description)
    stage.publish_stage(selected, description)
    root = Path(description["stageRoot"])
    before = snapshot(root)
    lock = SimpleNamespace(acquire=lambda: False)
    monkeypatch.setattr(stage, "WorkspaceLock", lambda *args, **kwargs: lock)
    with pytest.raises(RuntimeError, match="stage is busy"):
        with stage.runtime_stage_retirement(selected, "sample"):
            raise AssertionError("busy runtime entered retirement")
    assert snapshot(root) == before
