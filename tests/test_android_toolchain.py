"""Android inputs must fail closed before any downloaded tool can be used."""
from __future__ import annotations

import copy
import hashlib
import io
import json
from unittest.mock import Mock

import pytest

from emule_workspace import android_toolchain as android
from emule_workspace import python_runtime


def test_reviewed_inputs_are_complete_and_use_stable_platform_tools():
    value = android.tool_inputs()
    assert value["archives"]["platform-tools"]["version"] == "37.0.1"
    assert value["archives"]["platform"]["version"] == "37.2-r2"


@pytest.mark.parametrize("mutation", [
    lambda value: value["archives"].pop("ndk"),
    lambda value: value["archives"]["ndk"].update(version="latest"),
    lambda value: value["archives"]["ndk"].update(sha256="f" * 63),
    lambda value: value["archives"]["ndk"].update(url="http://dl.google.com/android/repository/ndk.zip"),
    lambda value: value["archives"]["ndk"].update(url="https://dl.google.com.evil.test/android/repository/ndk.zip"),
    lambda value: value["archives"]["ndk"].update(url="https://dl.google.com/android/repository/ndk.zip?revision=latest"),
    lambda value: value["archives"]["ndk"].update(url="https://dl.google.com/android/repository/ndk.zip#latest"),
    lambda value: value["archives"]["jdk"].update(url="https://github.com/other/project/jdk.tar.gz"),
    lambda value: value.update(host="linux-aarch64"),
])
def test_invalid_inputs_fail_before_fetch(tmp_path, mutation):
    value = copy.deepcopy(android.tool_inputs())
    mutation(value)
    config = tmp_path / "inputs.json"
    config.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(RuntimeError):
        android.tool_inputs(config)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    payload = b"synthetic official archive bytes"
    value = copy.deepcopy(android.tool_inputs())
    for item in value["archives"].values():
        item["sha256"] = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(android, "resolve_required_workspace_roots", lambda: (tmp_path / "workspace", tmp_path))
    monkeypatch.setattr(android, "tool_inputs", lambda: value)
    # Exercise the real checksum-verifying downloader, with only HTTP mocked.
    download = Mock(side_effect=lambda *args, **kwargs: io.BytesIO(payload))
    monkeypatch.setattr(python_runtime.urllib.request, "urlopen", download)
    return tmp_path, value, payload, download


def test_fetch_records_actual_bytes_and_rechecks_offline_cache(cache):
    output, inputs, payload, download = cache
    receipt = android.fetch_tool_inputs()
    value = json.loads(receipt.read_text(encoding="utf-8"))
    assert value["schema"] == "emulebb.android-tool-cache/1"
    assert set(value["archives"]) == android.ROLES
    for role, item in value["archives"].items():
        assert (output / item["path"]).read_bytes() == payload
        assert item["size"] == len(payload)
        assert item["sha256"] == inputs["archives"][role]["sha256"]
    before = receipt.read_bytes()
    download.reset_mock()
    assert android.fetch_tool_inputs() == receipt
    assert receipt.read_bytes() == before
    download.assert_not_called()


def test_corrupt_cache_does_not_refresh_receipt_or_download_over_it(cache):
    output, _inputs, _payload, download = cache
    receipt = android.fetch_tool_inputs()
    before = receipt.read_bytes()
    first = next(iter(json.loads(before)["archives"].values()))
    (output / first["path"]).write_bytes(b"corrupted cached input")
    download.reset_mock()
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        android.fetch_tool_inputs()
    assert receipt.read_bytes() == before
    download.assert_not_called()


def test_corrupt_download_is_never_published_as_verified(cache, monkeypatch):
    output, _inputs, _payload, _download = cache
    monkeypatch.setattr(python_runtime.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"tampered"))
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        android.fetch_tool_inputs()
    assert not (output / "cache/android-tool-inputs/verified-inputs.json").exists()
    assert not list((output / "cache/android-tool-inputs").rglob("*.zip"))


def test_archive_symlink_is_rejected_without_touching_target(cache):
    output, _inputs, _payload, download = cache
    receipt = android.fetch_tool_inputs()
    item = next(iter(json.loads(receipt.read_text())["archives"].values()))
    archive = output / item["path"]
    external = output / "operator-file"
    external.write_bytes(b"preserve operator data")
    archive.unlink()
    archive.symlink_to(external)
    download.reset_mock()
    with pytest.raises(RuntimeError, match="symlink archives"):
        android.fetch_tool_inputs()
    assert external.read_bytes() == b"preserve operator data"
    download.assert_not_called()


def test_external_cache_directory_is_rejected(cache, tmp_path):
    output, _inputs, _payload, download = cache
    external = tmp_path.parent / (tmp_path.name + "-outside")
    external.mkdir()
    (output / "cache").mkdir()
    (output / "cache/android-tool-inputs").symlink_to(external, target_is_directory=True)
    with pytest.raises(RuntimeError, match="inherited output root"):
        android.fetch_tool_inputs()
    assert not list(external.iterdir())
    download.assert_not_called()
