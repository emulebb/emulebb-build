"""Enforce both image CPUs, exact loaded identities, and hard CVE failures."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from emule_workspace import image_security


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir()
    archive = output / "image.oci.tar"
    archive.write_bytes(b"OCI fixture")
    manifest = {"version": "0.1.0-beta.2", "sourceSha": "a" * 40,
                "origin": "local-development", "archiveSha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    archive.with_name(archive.name + ".manifest.json").write_text(json.dumps(manifest))
    evidence = output / "security"
    smoke = output / "fresh-smoke.json"
    identities = {"linux/amd64": "sha256:" + "a" * 64, "linux/arm64": "sha256:" + "b" * 64}
    smoke.write_text(json.dumps({"status": "passed", "archiveSha256": manifest["archiveSha256"],
                                 "platforms": [{"platform": cpu, "imageManifestDigest": digest} for cpu, digest in identities.items()]}))
    calls = []
    faults = {"vulnerability": False, "stale_tag": False}
    class Docker:
        prefix = []
        def __init__(self, *args):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def path(self, path):
            return str(path)
        def run(self, *arguments, **kwargs):
            if arguments[0] == "context":
                return SimpleNamespace(stdout="unix:///var/run/docker.sock")
            if arguments[0] == "version":
                return SimpleNamespace(stdout="amd64")
            if arguments[0] == "run":
                return SimpleNamespace(stdout="Version: 0.75.0\n")
            platform = arguments[arguments.index("--platform") + 1]
            return SimpleNamespace(stdout=json.dumps({"digest": "sha256:" + "c" * 64 if faults["stale_tag"] else identities[platform]}))
    def run(docker, command, log, **kwargs):
        calls.append(command)
        log.write_text("scanner evidence\n")
        if "--format" in command:
            name = Path(command[command.index("--output") + 1]).name
            (evidence / name).write_text(json.dumps({"spdxVersion": "SPDX-2.3", "packages": [{"name": "libc6"}]}))
        elif faults["vulnerability"] and command[command.index("--platform", command.index("image")) + 1] == "linux/amd64":
            raise RuntimeError("fixable HIGH CVE")
    monkeypatch.setattr(image_security, "LinuxDocker", Docker)
    monkeypatch.setattr(image_security, "smoke_image", lambda *args, **kwargs: smoke)
    monkeypatch.setattr(image_security, "run_owned_container", run)
    return SimpleNamespace(layout=SimpleNamespace(output_root=output), archive=archive,
                           evidence=evidence, faults=faults, calls=calls)


def test_both_cpu_scans_require_inventory_and_hard_vulnerability_gate(scanner):
    report = image_security.validate_image(scanner.layout, archive=scanner.archive, image="local/test:beta",
                                            architectures=("amd64", "arm64"), security_dir=scanner.evidence)
    data = json.loads(report.read_text())
    assert data["status"] == "passed"
    assert [item["runtimeExecution"] for item in data["platforms"]] == ["native", "qemu"]
    assert len(scanner.calls) == 4
    for command in scanner.calls:
        assert image_security.TRIVY_IMAGE in command
        assert command[command.index("--image-src") + 1] == "docker"
    vulnerabilities = [command for command in scanner.calls if "--scanners" in command]
    assert len(vulnerabilities) == 2
    for command in vulnerabilities:
        assert command[command.index("--severity") + 1] == "HIGH,CRITICAL"
        assert command[command.index("--exit-code") + 1] == "1"
        assert "--ignore-unfixed" in command


def test_cve_failure_blocks_validation_and_still_collects_other_cpu(scanner):
    scanner.faults["vulnerability"] = True
    with pytest.raises(RuntimeError, match="Image validation failed"):
        image_security.validate_image(scanner.layout, archive=scanner.archive, image="local/test:beta",
                                      architectures=("amd64", "arm64"), security_dir=scanner.evidence)
    assert len(scanner.calls) == 4
    report = next(scanner.evidence.glob("*-security.json"))
    data = json.loads(report.read_text())
    assert data["status"] == "failed"
    assert [item["status"] for item in data["platforms"]] == ["failed", "passed"]
    assert (scanner.evidence / "emulebb-rust-v0.1.0-beta.2-image-amd64.vulnerabilities.txt").exists()


def test_changed_loaded_tag_is_rejected_before_scanning(scanner):
    scanner.faults["stale_tag"] = True
    with pytest.raises(RuntimeError, match="Image validation failed"):
        image_security.validate_image(scanner.layout, archive=scanner.archive, image="local/test:beta",
                                      architectures=("amd64", "arm64"), security_dir=scanner.evidence)
    assert scanner.calls == []
    assert json.loads(next(scanner.evidence.glob("*-security.json")).read_text())["status"] == "failed"
