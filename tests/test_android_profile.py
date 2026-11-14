"""Owned-update prerequisites compare immutable schema blobs without profile access."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from emule_workspace import android_profile as profile

OLD, NEW = "1" * 40, "2" * 40


@pytest.fixture
def source(tmp_path, monkeypatch):
    rust, output = tmp_path / "rust", tmp_path / "output"
    rust.mkdir()
    output.mkdir()
    monkeypatch.setattr(profile, "resolve_required_workspace_roots", lambda: (tmp_path, output))
    def git(repo, *arguments):
        assert repo == rust
        if arguments == ("rev-parse", "--show-toplevel"):
            return str(rust)
        if arguments[0] == "status":
            return ""
        if arguments == ("rev-parse", "HEAD"):
            return NEW
        return arguments[-1].removesuffix("^{commit}")
    monkeypatch.setattr(profile, "git_output", git)
    blobs = {(revision, path): (b"synthetic immutable schema\n" + path.encode())
             for revision in (OLD, NEW) for path in profile.SCHEMA_PATHS}
    calls = []
    def read(command, **kwargs):
        assert command[:2] == ["git", "--no-replace-objects"]
        assert kwargs["timeout"] == 30
        revision, path = command[-1].split(":", 1)
        calls.append((revision, path))
        return blobs[(revision, path)]
    monkeypatch.setattr(profile.subprocess, "check_output", read)
    return SimpleNamespace(output_root=output, emulebb_rust_repo_root=rust), blobs, calls


def test_exact_git_blob_materials_bind_the_two_caller_owned_revisions(source):
    layout, blobs, calls = source
    path = profile.prove_profile_compatibility(layout, from_source_sha=OLD, to_source_sha=NEW)
    data = json.loads(path.read_text())
    materials = {name: hashlib.sha256(blobs[(NEW, name)]).hexdigest() for name in profile.SCHEMA_PATHS}
    assert data["schema"] == "emulebb.android.profile-compatibility/1"
    assert data["status"] == "passed" and data["fromSourceRevision"] == OLD and data["toSourceRevision"] == NEW
    assert data["materials"] == materials
    assert data["metadataSchemaSha256"] == hashlib.sha256(json.dumps(materials, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert set(calls) == set(blobs)
    assert path.is_relative_to(layout.output_root)


@pytest.mark.parametrize("path", profile.SCHEMA_PATHS)
def test_either_changed_material_refuses_compatibility_and_writes_no_receipt(source, path):
    layout, blobs, _calls = source
    blobs[(NEW, path)] += b"changed shape"
    with pytest.raises(RuntimeError, match="unchanged persisted schema"):
        profile.prove_profile_compatibility(layout, from_source_sha=OLD, to_source_sha=NEW)
    assert not list(layout.output_root.rglob("*.json"))


@pytest.mark.parametrize("old,new", [("main", NEW), (OLD, "HEAD"), ("a" * 39, NEW)])
def test_floating_or_malformed_source_identity_fails_before_git_execution(source, monkeypatch, old, new):
    layout, _blobs, _calls = source
    monkeypatch.setattr(profile, "git_output", lambda *args: pytest.fail("unvalidated source identity"))
    with pytest.raises(ValueError, match="explicit full"):
        profile.prove_profile_compatibility(layout, from_source_sha=old, to_source_sha=new)


def test_dirty_or_different_selected_checkout_cannot_issue_receipt(source, monkeypatch):
    layout, _blobs, calls = source
    git = profile.git_output
    monkeypatch.setattr(profile, "git_output", lambda repo, *args: "dirty" if args[0] == "status" else git(repo, *args))
    with pytest.raises(RuntimeError, match="clean selected"):
        profile.prove_profile_compatibility(layout, from_source_sha=OLD, to_source_sha=NEW)
    assert not calls
