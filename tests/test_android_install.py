"""Installer failures must preserve operator files and never emit success evidence."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from unittest.mock import Mock
import zipfile

import pytest

from emule_workspace import android_inputs, android_install as install


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")
    monkeypatch.setattr(install.platform, "machine", lambda: "x86_64")
    inputs = install.tool_inputs()
    layout = install.installation_layout(inputs)
    cache = tmp_path / "cache"
    for role, item in inputs["assets"].items():
        root, _target = layout[role]
        filename = item["url"].rsplit("/", 1)[-1]
        if item["kind"] == "binary":
            payload = b"synthetic native CLI"
        elif filename.endswith(".zip"):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr(root + "/NOTICE", b"synthetic package")
            payload = stream.getvalue()
        else:
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                info = tarfile.TarInfo(root + "/NOTICE")
                info.size = 17
                archive.addfile(info, io.BytesIO(b"synthetic package"))
            payload = stream.getvalue()
        item["sha256"] = hashlib.sha256(payload).hexdigest()
        target = cache / item["sha256"] / filename
        target.parent.mkdir(parents=True)
        target.write_bytes(payload)
    monkeypatch.setattr(install, "tool_inputs", lambda: inputs)
    execute = Mock(side_effect=AssertionError("No extractor may run after invalid preflight"))
    monkeypatch.setattr(install.subprocess, "run", execute)
    return cache, tmp_path / "installed", inputs, execute


def archive_path(cache, item):
    return cache / item["sha256"] / item["url"].rsplit("/", 1)[-1]


def test_all_archives_are_checked_before_installation(installation):
    cache, destination, inputs, execute = installation
    last = next(reversed(inputs["assets"].values()))
    archive_path(cache, last).write_bytes(b"tampered final input")
    with pytest.raises(RuntimeError, match="Unverified Android input"):
        install.install_inputs(cache, destination)
    assert not destination.exists()
    execute.assert_not_called()


def test_existing_installation_is_not_modified(installation):
    cache, destination, _inputs, execute = installation
    destination.mkdir()
    operator = destination / "operator-state"
    operator.write_bytes(b"preserve")
    with pytest.raises(RuntimeError, match="fresh destination"):
        install.install_inputs(cache, destination)
    assert operator.read_bytes() == b"preserve"
    assert list(destination.iterdir()) == [operator]
    execute.assert_not_called()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "wrong-root/file", "cmdline-tools/../../escape"])
def test_pinned_but_unsafe_archive_layout_is_rejected(installation, name):
    cache, destination, inputs, execute = installation
    item = inputs["assets"]["command-line-tools"]
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(name, b"payload")
    payload = stream.getvalue()
    item["sha256"] = hashlib.sha256(payload).hexdigest()
    path = archive_path(cache, item)
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    with pytest.raises(RuntimeError, match="Unexpected Android archive layout"):
        install.install_inputs(cache, destination)
    assert not destination.exists()
    execute.assert_not_called()


def test_unsupported_host_fails_before_installation(installation, monkeypatch):
    cache, destination, _inputs, execute = installation
    monkeypatch.setattr(install.platform, "machine", lambda: "aarch64")
    with pytest.raises(RuntimeError, match="native Linux x86_64"):
        install.install_inputs(cache, destination)
    assert not destination.exists()
    execute.assert_not_called()


def test_extraction_failure_never_creates_success_receipt(installation):
    cache, destination, _inputs, execute = installation
    execute.side_effect = RuntimeError("extractor failed")
    with pytest.raises(RuntimeError, match="extractor failed"):
        install.install_inputs(cache, destination)
    assert not (destination / "tool-inputs.json").exists()
    assert not (destination / ".extract").exists()


@pytest.mark.parametrize("tamper", [False, True])
def test_emulator_and_both_images_use_verified_fresh_installation(tmp_path, monkeypatch, tamper):
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")
    monkeypatch.setattr(install.platform, "machine", lambda: "x86_64")
    inputs = install.emulator_subset(install.emulator_inputs(), ("min", "current"))
    cache, destination = tmp_path / "cache", tmp_path / "emulator"
    for role, (root, _target) in install.emulator_layout(inputs).items():
        item = inputs["assets"][role]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(root + "/source.properties", "synthetic metadata")
        item["sha256"] = hashlib.sha256(buffer.getvalue()).hexdigest()
        path = archive_path(cache, item)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(buffer.getvalue())
    monkeypatch.setattr(install, "emulator_inputs", lambda: inputs)
    monkeypatch.setattr(android_inputs, "tool_inputs", lambda: {"assets": {"platform-tools": inputs["assets"]["platform-tools"]}})
    def extract(command, **kwargs):
        with zipfile.ZipFile(command[2]) as archive:
            archive.extractall(command[-1])
    execute = Mock(side_effect=extract)
    monkeypatch.setattr(install.subprocess, "run", execute)
    if tamper:
        archive_path(cache, inputs["assets"]["system-image-current"]).write_bytes(b"tampered")
        with pytest.raises(RuntimeError, match="Unverified Android input"):
            install.install_emulator_inputs(cache, destination, images=("min", "current"))
        assert not destination.exists()
        execute.assert_not_called()
    else:
        receipt = install.install_emulator_inputs(cache, destination, images=("min", "current"))
        assert json.loads(receipt.read_text()) == inputs
        assert (destination / "system-images/android-28/default/x86_64/source.properties").is_file()
        assert (destination / "system-images/android-37.2/google_apis_ps16k/x86_64/source.properties").is_file()
        assert (destination / "platform-tools/source.properties").is_file()
        assert not (destination / "licenses").exists()


@pytest.mark.parametrize("mode,directory,expected", [(0o700, True, 0o755), (0o600, False, 0o644),
    (0o700, False, 0o755), (0o4777, False, 0o755), (0o666, False, 0o644), (0o444, False, 0o444)])
def test_owner_only_sdk_modes_allow_non_root_read_and_execute_without_write_or_set_id(mode, directory, expected):
    assert install._runtime_package_mode(mode, directory=directory) == expected
