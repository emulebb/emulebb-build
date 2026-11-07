"""Enforce both image CPUs, exact loaded identities, and hard CVE failures."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile

import pytest

from emule_workspace import image_security


def write_platform_export(path, architecture):
    config = json.dumps({"os": "linux", "architecture": architecture}).encode()
    with tarfile.open(path, "w") as archive:
        for name, data in (("manifest.json", json.dumps([{"Config": "config.json"}]).encode()), ("config.json", config)):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return "sha256:" + hashlib.sha256(config).hexdigest()


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
    identities = {}
    for arch, digest in (("amd64", "a"), ("arm64", "b")):
        fixture = output / f"{arch}.tar"
        identities[f"linux/{arch}"] = {"imageManifestDigest": "sha256:" + digest * 64,
                                       "imageConfigDigest": write_platform_export(fixture, arch)}
    smoke.write_text(json.dumps({"status": "passed", "archiveSha256": manifest["archiveSha256"],
                                 "platforms": [{"platform": cpu, **identity} for cpu, identity in identities.items()]}))
    calls = []
    faults = {"vulnerability": False, "stale_tag": False, "wrong_sbom_arch": False}
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
            if arguments[:2] == ("image", "save"):
                write_platform_export(Path(arguments[arguments.index("--output") + 1]), platform.split("/")[1])
                return SimpleNamespace(stdout="")
            return SimpleNamespace(stdout=json.dumps({"digest": "sha256:" + "c" * 64 if faults["stale_tag"] else identities[platform]["imageManifestDigest"]}))
    def run(docker, command, log, **kwargs):
        calls.append(command)
        log.write_text("scanner evidence\n")
        if "--format" in command:
            name = Path(command[command.index("--output") + 1]).name
            arch = command[command.index("--platform", command.index("image")) + 1].split("/")[1]
            if faults["wrong_sbom_arch"]:
                arch = "amd64"
            (evidence / name).write_text(json.dumps({"spdxVersion": "SPDX-2.3", "packages": [
                {"name": "libc6", "externalRefs": [{"referenceType": "purl", "referenceLocator": f"pkg:deb/ubuntu/libc6@2.43?arch={arch}"}]}]}))
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
        assert command[command.index("--input") + 1] in ("/scan/amd64.tar", "/scan/arm64.tar")
        assert not any("docker.sock" in argument for argument in command)
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


def test_duplicate_host_cpu_inventory_cannot_satisfy_arm64_gate(scanner):
    scanner.faults["wrong_sbom_arch"] = True
    with pytest.raises(RuntimeError, match="Image validation failed"):
        image_security.validate_image(scanner.layout, archive=scanner.archive, image="local/test:beta",
                                      architectures=("amd64", "arm64"), security_dir=scanner.evidence)
    report = json.loads(next(scanner.evidence.glob("*-security.json")).read_text())
    assert report["status"] == "failed"
    assert [item["status"] for item in report["platforms"]] == ["passed", "failed"]
    assert "correct Ubuntu OS package architecture" in report["platforms"][1]["error"]


def test_export_must_match_exact_config_and_cpu(tmp_path):
    archive = tmp_path / "platform.tar"
    expected = write_platform_export(archive, "amd64")
    image_security.verify_platform_export(archive, architecture="amd64", expected_config=expected)
    with pytest.raises(RuntimeError, match="architecture"):
        image_security.verify_platform_export(archive, architecture="arm64", expected_config=expected)
    write_platform_export(archive, "arm64")
    with pytest.raises(RuntimeError, match="config does not match"):
        image_security.verify_platform_export(archive, architecture="arm64", expected_config=expected)
