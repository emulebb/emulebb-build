"""SDKmanager may add metadata but must never replace pinned SDK payloads."""
import hashlib
from pathlib import Path
import zipfile

import pytest

from emule_workspace import android_sdk as sdk


@pytest.fixture
def installed(tmp_path, monkeypatch):
    roles = sdk.SDK_ROLES
    inputs = {"assets": {role: {"version": "37.2-r2" if role == "platform" else "1.0"} for role in roles}}
    layout = {role: (role, "sdk/" + role) for role in roles}
    cache, tools = tmp_path / "cache", tmp_path / "tools"
    monkeypatch.setattr(sdk, "installation_layout", lambda _inputs: layout)
    for role in roles:
        archive = tmp_path / (role + ".zip")
        with zipfile.ZipFile(archive, "w") as stream:
            directory = zipfile.ZipInfo(role + "/")
            directory.external_attr = 0o40700 << 16
            stream.writestr(directory, b"")
            member = zipfile.ZipInfo(role + "/tool.bin")
            member.external_attr = (0o100700 if role == "ndk" else 0o100600) << 16
            stream.writestr(member, b"synthetic pinned payload")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        inputs["assets"][role].update(sha256=digest, url="https://example.invalid/" + archive.name)
        target = cache / digest / archive.name
        target.parent.mkdir(parents=True)
        archive.rename(target)
        root = tools / layout[role][1]
        root.mkdir(parents=True)
        (root / "tool.bin").write_bytes(b"synthetic pinned payload")
        (root / "package.xml").write_text('<repository><localPackage path="' + sdk.package_ids(inputs)[role] + '"/></repository>')
    return cache, tools, inputs


def test_sdkmanager_metadata_is_accepted_when_all_pinned_payloads_match(installed):
    result = sdk.verify_sdk_payloads(*installed)
    assert set(result) == set(sdk.SDK_ROLES)
    assert all(value["payloadFiles"] == 1 for value in result.values())


def test_registration_uses_pinned_cli_without_sdkmanager_bootstrap(installed, monkeypatch):
    cache, tools, inputs = installed
    monkeypatch.setattr(sdk, "tool_inputs", lambda: inputs)
    calls = []
    monkeypatch.setattr(sdk.subprocess, "run", lambda command, **kwargs: calls.append(command))
    sdk.register_sdk(cache, tools)
    assert len(calls) == 1
    assert calls[0][0] == str(tools / "bin/android")
    assert "--no-metrics" in calls[0] and "sdk" in calls[0] and "install" in calls[0]
    assert not any("sdkmanager" in argument for argument in calls[0])
    assert "platforms/android-37.2" in calls[0]


def test_sdk_registration_restores_only_reviewed_execute_bits_after_byte_verification(installed, monkeypatch):
    cache, tools, inputs = installed
    monkeypatch.setattr(sdk, "tool_inputs", lambda: inputs)
    monkeypatch.setattr(sdk.subprocess, "run", lambda *args, **kwargs: None)
    modes = {}
    monkeypatch.setattr(Path, "chmod", lambda path, mode: modes.update({path: mode}))
    sdk.register_sdk(cache, tools)
    assert modes[tools / "sdk/ndk/tool.bin"] == 0o755
    assert modes[tools / "sdk/platform/tool.bin"] == 0o644
    for role in sdk.SDK_ROLES:
        assert modes[tools / "sdk" / role] == 0o755
        assert modes[tools / "sdk" / role / "package.xml"] == 0o444


def test_unreviewed_sdk_payload_never_receives_mode_repair(installed, monkeypatch):
    cache, tools, inputs = installed
    (tools / "sdk/ndk/tool.bin").write_bytes(b"unreviewed compiler")
    monkeypatch.setattr(sdk, "tool_inputs", lambda: inputs)
    monkeypatch.setattr(sdk.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(Path, "chmod", lambda *args: pytest.fail("repair before payload verification"))
    with pytest.raises(RuntimeError, match="differs from pinned archive"):
        sdk.register_sdk(cache, tools)


@pytest.mark.parametrize("change", ["changed", "extra", "missing", "metadata", "archive"])
def test_sdkmanager_updates_or_unreviewed_files_fail_closed(installed, change):
    cache, tools, inputs = installed
    root = tools / "sdk/platform"
    if change == "changed":
        (root / "tool.bin").write_bytes(b"new unpinned SDK revision")
    elif change == "extra":
        (root / "unreviewed.bin").write_bytes(b"unexpected executable")
    elif change == "missing":
        (root / "tool.bin").unlink()
    elif change == "metadata":
        (root / "package.xml").write_text('<repository><localPackage path="platforms;android-99"/></repository>')
    else:
        item = inputs["assets"]["platform"]
        (cache / item["sha256"] / "platform.zip").write_bytes(b"tampered archive")
    with pytest.raises(RuntimeError):
        sdk.verify_sdk_payloads(cache, tools, inputs)
