"""Bind image builds to exact source bytes and clean up owned workers on failure."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from emule_workspace import rust_images


@pytest.fixture
def image_inputs(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    name = "emulebb-rust-v0.1.0-beta.2-linux-amd64.deb"
    (assets / name).write_bytes(b"payload")
    (assets / "package.spdx.json").write_bytes(b"{}")
    manifest = {"asset": name, "sha256": hashlib.sha256(b"payload").hexdigest(),
                "sbom": "package.spdx.json", "sbomSha256": hashlib.sha256(b"{}").hexdigest(),
                "source": {"emulebbRust": {"commit": "a" * 40}}}
    path = assets / (name + ".manifest.json")
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(rust_images, "git_output", lambda repo, *args: "" if args[0] == "status" else "a" * 40)
    return assets, path, manifest


@pytest.mark.parametrize("mutation", ["payload", "revision", "dirty", "sbom", "sbom-path"])
def test_image_rejects_changed_bytes_or_provenance(image_inputs, tmp_path, mutation):
    assets, path, manifest = image_inputs
    assert rust_images.verified_debs(tmp_path, assets, "0.1.0-beta.2", ("amd64",))
    if mutation == "payload":
        (assets / manifest["asset"]).write_bytes(b"changed payload")
    elif mutation == "revision":
        manifest["source"]["emulebbRust"]["commit"] = "b" * 40
    elif mutation == "dirty":
        manifest["sourceDirty"] = True
    elif mutation == "sbom":
        (assets / manifest["sbom"]).write_bytes(b"changed inventory")
    else:
        manifest["sbom"] = "../outside.spdx.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError):
        rust_images.verified_debs(tmp_path, assets, "0.1.0-beta.2", ("amd64",))


def test_image_rejects_dirty_checkout_and_missing_architecture(image_inputs, tmp_path, monkeypatch):
    assets, _, _ = image_inputs
    with pytest.raises(RuntimeError, match="missing"):
        rust_images.verified_debs(tmp_path, assets, "0.1.0-beta.2", ("amd64", "arm64"))
    monkeypatch.setattr(rust_images, "git_output", lambda *args: " M source.rs")
    with pytest.raises(RuntimeError, match="clean current"):
        rust_images.verified_debs(tmp_path, assets, "0.1.0-beta.2", ("amd64",))


def test_timed_out_image_build_retains_logs_and_removes_only_its_worker(tmp_path, monkeypatch):
    rust = tmp_path / "rust"
    (rust / "packaging/docker").mkdir(parents=True)
    (rust / "packaging/docker/Dockerfile").write_text("FROM scratch\n")
    output = tmp_path / "output"
    assets = output / "assets"
    assets.mkdir(parents=True)
    calls = []
    class Docker:
        prefix = ["wsl.exe", "-d", "Ubuntu", "--"]
        def __init__(self, *args):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def path(self, value):
            return str(value)
        def run(self, *arguments, **kwargs):
            calls.append(list(arguments))
    def run(command, **kwargs):
        calls.append(command)
        if "--output" in command:
            kwargs["stdout"].write("original build failure\n")
            raise subprocess.TimeoutExpired(command, 1)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(rust_images, "LinuxDocker", Docker)
    monkeypatch.setattr(rust_images, "verified_debs", lambda *args: [])
    monkeypatch.setattr(rust_images.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="diagnostics"):
        rust_images.build_image(rust_root=rust, output_root=output, assets_dir=assets,
                                version="0.1.0-beta.2", image="local/test:beta")
    name = calls[0][calls[0].index("--name") + 1]
    assert name.startswith("emulebb-image-")
    assert f"image={rust_images.BUILDKIT_IMAGE}" in calls[0]
    assert calls[-1][-3:] == ["rm", "--force", name]
    report_path = next((output / "reports/rust-image-build").glob("*/inputs.json"))
    assert json.loads(report_path.read_text())["status"] == "failed"
    assert report_path.with_name("build.log").read_text() == "original build failure\n"
