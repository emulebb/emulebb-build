"""Installer failures must preserve operator files and never emit success evidence."""
from __future__ import annotations

import hashlib
import io
import tarfile
from unittest.mock import Mock
import zipfile

import pytest

from emule_workspace import android_install as install


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
