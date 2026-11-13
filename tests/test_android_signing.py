"""Signing provisioning must preserve one identity and never expose private inputs."""
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


@pytest.mark.parametrize("unsafe", ["extra", "owner", "inherited", "rights", "deny"])
def test_windows_acl_verification_rejects_broad_access(tmp_path, monkeypatch, unsafe):
    sid = "S-1-5-21-1000"
    acl = {"owner": sid, "protected": True, "rules": [
        {"sid": sid, "kind": "Allow", "rights": 2032127},
        {"sid": signing.SYSTEM_SID, "kind": "Allow", "rights": 2032127}]}
    if unsafe == "extra":
        acl["rules"].append({"sid": "S-1-1-0", "kind": "Allow", "rights": 1})
    elif unsafe == "owner":
        acl["owner"] = signing.SYSTEM_SID
    elif unsafe == "inherited":
        acl["protected"] = False
    elif unsafe == "rights":
        acl["rules"][0]["rights"] = 1
    else:
        acl["rules"][0]["kind"] = "Deny"
    monkeypatch.setattr(signing.subprocess, "check_output", lambda *args, **kwargs: json.dumps(acl))
    with pytest.raises(RuntimeError, match="ACL"):
        signing._verify_acl(tmp_path, sid)


def test_missing_local_key_cannot_replace_existing_github_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(signing.subprocess, "check_output", lambda *args, **kwargs:
                        json.dumps([{"name": signing.GITHUB_SECRETS[0]}]))
    monkeypatch.setattr(signing, "prepare_toolchain", lambda *_args, **_kwargs: pytest.fail("must not prepare"))
    with pytest.raises(RuntimeError, match="already exist"):
        signing.provision_signing(SimpleNamespace(output_root=tmp_path))


def test_partial_local_state_is_preserved_for_inspection(tmp_path, monkeypatch):
    root = tmp_path / "tools/signing/android"
    root.mkdir(parents=True)
    (root / "release.p12").write_bytes(b"synthetic-retained-key")
    monkeypatch.setattr(signing.subprocess, "check_output", lambda *args, **kwargs: "[]")
    monkeypatch.setattr(signing, "private_directory", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="Partial"):
        signing.provision_signing(SimpleNamespace(output_root=tmp_path))
    assert (root / "release.p12").read_bytes() == b"synthetic-retained-key"


def test_failed_secret_upload_retries_same_retained_key(tmp_path, monkeypatch, capsys):
    root = tmp_path / "tools/signing/android"
    root.mkdir(parents=True)
    (root / "release.p12").write_bytes(b"synthetic-retained-key")
    (root / "credentials.json").write_text(json.dumps({"password": "synthetic-password", "alias": "release"}))
    (root / "identity.json").write_text(json.dumps({"repository": signing.REPOSITORY, "signerSha256": "a" * 64}))
    monkeypatch.setattr(signing.subprocess, "check_output", lambda *args, **kwargs: "[]")
    monkeypatch.setattr(signing, "private_directory", lambda *args, **kwargs: None)
    monkeypatch.setattr(signing, "_verify_material", lambda *_args: None)
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
    assert result["signerSha256"] == "a" * 64
    assert len(calls) == 6
    assert calls[-1][1]["input"] == calls[-2][1]["input"] == "synthetic-password"
    assert "synthetic-password" not in capsys.readouterr().out


def test_unsafe_existing_file_acl_is_rejected_before_reading_or_mounting(tmp_path, monkeypatch):
    root = tmp_path / "tools/signing/android"
    root.mkdir(parents=True)
    for name in ("release.p12", "credentials.json", "identity.json"):
        (root / name).write_bytes(b"synthetic-unread-material")
    monkeypatch.setattr(signing.subprocess, "check_output", lambda *args, **kwargs: "[]")
    monkeypatch.setattr(signing, "private_directory", lambda *args, **kwargs: None)
    def reject(_root):
        raise RuntimeError("unsafe existing file ACL")
    monkeypatch.setattr(signing, "_verify_material", reject)
    monkeypatch.setattr(signing, "prepare_toolchain", lambda *args, **kwargs: pytest.fail("must not prepare or mount"))
    monkeypatch.setattr(signing.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not upload"))
    with pytest.raises(RuntimeError, match="unsafe existing file ACL"):
        signing.provision_signing(SimpleNamespace(output_root=tmp_path))
