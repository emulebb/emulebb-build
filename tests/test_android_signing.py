"""Retained signing keys are never replaced and uploads do not reveal secrets."""
import json
from types import SimpleNamespace

import pytest

from emule_workspace import android_signing as signing


def test_exclusive_credentials_never_replace_existing_key_material(tmp_path):
    path = tmp_path / "credentials.json"
    signing._exclusive_json(path, {"password": "synthetic-first"})
    with pytest.raises(FileExistsError):
        signing._exclusive_json(path, {"password": "synthetic-second"})
    assert json.loads(path.read_text())["password"] == "synthetic-first"


def test_missing_local_key_cannot_replace_existing_github_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(signing, "signing_root", lambda: tmp_path / "absent")
    monkeypatch.setattr(signing.subprocess, "check_output", lambda *args, **kwargs:
                        json.dumps([{"name": signing.GITHUB_SECRETS[0]}]))
    with pytest.raises(RuntimeError, match="already exist"):
        signing.provision_signing(SimpleNamespace(output_root=tmp_path))


def test_partial_local_state_is_preserved_for_inspection(tmp_path, monkeypatch):
    monkeypatch.setattr(signing, "signing_root", lambda: tmp_path)
    (tmp_path / "release.p12").write_bytes(b"synthetic-retained-key")
    monkeypatch.setattr(signing.subprocess, "check_output", lambda *args, **kwargs: "[]")
    with pytest.raises(RuntimeError, match="Partial"):
        signing.provision_signing(SimpleNamespace(output_root=tmp_path))
    assert (tmp_path / "release.p12").read_bytes() == b"synthetic-retained-key"


def test_failed_secret_upload_retries_same_retained_key(tmp_path, monkeypatch, capsys):
    root = tmp_path / "operator-profile"
    root.mkdir()
    monkeypatch.setattr(signing, "signing_root", lambda: root)
    (root / "release.p12").write_bytes(b"synthetic-retained-key")
    (root / "credentials.json").write_text(json.dumps({"password": "synthetic-password", "alias": "release"}))
    (root / "identity.json").write_text(json.dumps({"schema": "emulebb.android.signing-identity/1",
        "repository": signing.REPOSITORY, "alias": "release", "signerSha256": "a" * 64}))
    monkeypatch.setattr(signing.subprocess, "check_output", lambda *args, **kwargs: "[]")
    monkeypatch.setattr(signing, "prepare_toolchain", lambda *_args, **_kwargs: None)
    class Docker:
        def __init__(self, _distro): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def path(self, value): return str(value)
    monkeypatch.setattr(signing, "LinuxDocker", Docker)
    monkeypatch.setattr(signing, "_builder_user", lambda _docker: "1000:1000")
    monkeypatch.setattr(signing, "_certificate_fingerprint", lambda *args, **kwargs: "a" * 64)
    monkeypatch.setattr(signing, "run_owned_container", lambda *args, **kwargs: pytest.fail("must never regenerate"))
    calls = []
    fail = True
    def upload(command, **kwargs):
        calls.append((command, kwargs))
        assert "synthetic-password" not in " ".join(command)
        if fail and len(calls) == 2:
            raise RuntimeError("synthetic GitHub failure")
    monkeypatch.setattr(signing.subprocess, "run", upload)
    layout = SimpleNamespace(output_root=tmp_path)
    with pytest.raises(RuntimeError, match="GitHub"):
        signing.provision_signing(layout)
    assert (root / "release.p12").read_bytes() == b"synthetic-retained-key"
    fail = False
    result = signing.provision_signing(layout)
    assert result["signerSha256"] == "a" * 64 and len(calls) == 6
    assert calls[-1][1]["input"] == calls[-2][1]["input"] == "synthetic-password"
    assert "synthetic-password" not in capsys.readouterr().out
