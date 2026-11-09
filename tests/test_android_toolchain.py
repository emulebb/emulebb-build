"""Android inputs must fail closed before any downloaded tool can be used."""
from __future__ import annotations

import copy
import hashlib
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import Mock

import pytest

from emule_workspace import android_toolchain as android
from emule_workspace import android_inputs, config
from emule_workspace import python_runtime


@pytest.fixture
def cache(tmp_path, monkeypatch):
    payload = b"synthetic official archive bytes"
    value = copy.deepcopy(android.tool_inputs())
    for item in value["assets"].values():
        item["sha256"] = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(config, "resolve_required_workspace_roots", lambda: (tmp_path / "workspace", tmp_path))
    monkeypatch.setattr(android, "tool_inputs", lambda: value)
    monkeypatch.setattr(android_inputs, "tool_inputs", lambda: value)
    # Exercise the real checksum-verifying downloader, with only HTTP mocked.
    download = Mock(side_effect=lambda *args, **kwargs: io.BytesIO(payload))
    monkeypatch.setattr(python_runtime.urllib.request, "urlopen", download)
    return tmp_path, value, payload, download


def test_fetch_records_actual_bytes_and_rechecks_offline_cache(cache):
    output, inputs, payload, download = cache
    receipt = android.fetch_tool_inputs()
    value = json.loads(receipt.read_text(encoding="utf-8"))
    assert value["schema"] == "emulebb.android-tool-cache/1"
    assert set(value["assets"]) == android.ROLES
    for role, item in value["assets"].items():
        assert (output / item["path"]).read_bytes() == payload
        assert item["size"] == len(payload)
        assert item["sha256"] == inputs["assets"][role]["sha256"]
    before = receipt.read_bytes()
    download.reset_mock()
    assert android.fetch_tool_inputs() == receipt
    assert receipt.read_bytes() == before
    download.assert_not_called()


def test_corrupt_cache_does_not_refresh_receipt_or_download_over_it(cache):
    output, _inputs, _payload, download = cache
    receipt = android.fetch_tool_inputs()
    before = receipt.read_bytes()
    first = next(iter(json.loads(before)["assets"].values()))
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


def test_http_failure_identifies_the_reviewed_asset_without_creating_a_receipt(cache, monkeypatch):
    output, inputs, _payload, _download = cache
    role = sorted(inputs["assets"])[0]
    item = inputs["assets"][role]
    failed = Mock(side_effect=HTTPError(item["url"], 504, "Gateway Time-out", {}, None))
    monkeypatch.setattr(android, "download_verified", failed)
    with pytest.raises(RuntimeError, match=f"Android input {role} from .* failed: HTTP Error 504"):
        android.fetch_tool_inputs()
    assert not (output / "cache/android-tool-inputs/verified-inputs.json").exists()


def test_archive_symlink_is_rejected_without_touching_target(cache):
    output, _inputs, _payload, download = cache
    receipt = android.fetch_tool_inputs()
    item = next(iter(json.loads(receipt.read_text())["assets"].values()))
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


@pytest.mark.parametrize("images,roles", [
    ((), {"emulator", "platform-tools"}),
    (("min",), {"emulator", "platform-tools", "system-image-min"}),
    (("current",), {"emulator", "platform-tools", "system-image-current"}),
    (("min", "current"), {"emulator", "platform-tools", "system-image-min", "system-image-current"}),
])
def test_emulator_cache_downloads_only_requested_images_and_rechecks_every_byte(cache, monkeypatch, images, roles):
    output, _tools, payload, download = cache
    value = copy.deepcopy(android.emulator_inputs())
    for item in value["assets"].values():
        item["sha256"] = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(android, "emulator_inputs", lambda: value)
    receipt = android.fetch_emulator_inputs(images=images)
    assert receipt == output / "cache/android-emulator-inputs/verified-inputs.json"
    records = json.loads(receipt.read_text())
    assert records["schema"] == "emulebb.android-emulator-cache/1"
    assert set(records["assets"]) == roles
    assert download.call_count == len(roles)
    before = receipt.read_bytes()
    download.reset_mock()
    android.fetch_emulator_inputs(images=images)
    download.assert_not_called()
    first = next(iter(records["assets"].values()))
    (output / first["path"]).write_bytes(b"tampered input")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        android.fetch_emulator_inputs(images=images)
    assert receipt.read_bytes() == before
    download.assert_not_called()


@pytest.mark.parametrize("images", [("unknown",), ("min", "min")])
def test_unknown_or_duplicate_image_selection_fails_before_downloading(monkeypatch, images):
    download = Mock()
    monkeypatch.setattr(android, "download_verified", download)
    with pytest.raises(ValueError, match="unique"):
        android.fetch_emulator_inputs(images=images)
    download.assert_not_called()


def test_emulator_preparation_never_fetches_or_prepares_compiler_tools(tmp_path, monkeypatch):
    from emule_workspace import linux_containers

    monkeypatch.setattr(config, "resolve_required_workspace_roots", lambda: (tmp_path / "workspace", tmp_path))
    compiler = Mock(side_effect=AssertionError("guest boot must not prepare compiler inputs"))
    monkeypatch.setattr(android, "prepare_toolchain", compiler)
    monkeypatch.setattr(android, "fetch_tool_inputs", compiler)
    receipt = tmp_path / "runtime-cache/receipt.json"
    receipt.parent.mkdir()
    fetch = Mock(return_value=receipt)
    monkeypatch.setattr(android, "fetch_emulator_inputs", fetch)
    docker = Mock()
    docker.__enter__ = Mock(return_value=docker)
    docker.__exit__ = Mock(return_value=False)
    docker.prefix = []
    docker.path.side_effect = lambda path: str(path)
    identity = "sha256:" + "5" * 64
    docker.run.side_effect = lambda *args, **kwargs: SimpleNamespace(stdout="amd64" if args[0] == "version" else identity)
    monkeypatch.setattr(linux_containers, "LinuxDocker", lambda distro: docker)
    monkeypatch.setattr(linux_containers, "_builder_user", lambda docker: "1000:1000")
    monkeypatch.setattr(android.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    inputs = android.emulator_subset(android.emulator_inputs(), ())
    def probe(docker, arguments, log, **kwargs):
        assert identity in arguments and "none" in arguments
        log.write_text(json.dumps({"schema": "emulebb.android-emulator-toolchain-probe/1",
            "inputs": inputs, "runtimeExecution": "not-run"}))
    monkeypatch.setattr(linux_containers, "run_owned_container", probe)
    report = android.prepare_emulator_toolchain(SimpleNamespace(output_root=tmp_path))
    value = json.loads(report.read_text())
    assert set(value["inputs"]["assets"]) == {"emulator", "platform-tools"}
    assert value["image"]["id"] == identity
    assert "toolchainReport" not in value
    compiler.assert_not_called()
    fetch.assert_called_once_with(images=())
