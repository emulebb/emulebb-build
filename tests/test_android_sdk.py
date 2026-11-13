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
            stream.writestr(role + "/tool.bin", b"synthetic pinned payload")
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
