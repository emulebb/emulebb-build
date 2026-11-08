"""Enforce both image CPUs, exact loaded identities, and hard CVE failures."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile

import pytest

from emule_workspace import image_security


def write_platform_export(path, architecture, *, source="a" * 40, version="0.1.0-beta.2"):
    config = json.dumps({"os": "linux", "architecture": architecture, "config": {"Labels": {
        "org.opencontainers.image.revision": source, "org.opencontainers.image.version": version}}}).encode()
    with tarfile.open(path, "w") as archive:
        for name, data in (("manifest.json", json.dumps([{"Config": "config.json"}]).encode()), ("config.json", config)):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return "sha256:" + hashlib.sha256(config).hexdigest()


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    output = tmp_path / "output"
    output.mkdir()
    archive = output / "image.oci.tar"
    archive.write_bytes(b"OCI fixture")
    rust = tmp_path / "rust"
    (rust / "packaging/docker").mkdir(parents=True)
    (rust / "packaging/docker/Dockerfile").write_text("FROM pinned\n")
    manifest = {"schema": "emulebb.rust.image/2", "version": "0.1.0-beta.2", "sourceSha": "a" * 40,
                "sourceDirty": False, "image": "local/test:beta", "builderSha": "a" * 40,
                "architectures": ["amd64", "arm64"], "workflow": None,
                "dockerfileSha256": hashlib.sha256((rust / "packaging/docker/Dockerfile").read_bytes()).hexdigest(),
                "ociManifestDigest": "sha256:" + "c" * 64,
                "inputs": [{"asset": f"emulebb-rust-v0.1.0-beta.2-linux-{arch}.deb", "sha256": "d" * 64,
                            "sbomSha256": "e" * 64, "manifestSha256": "f" * 64,
                            "origin": "local-development", "workflow": None} for arch in ("amd64", "arm64")],
                "origin": "local-development", "archiveSha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    archive.with_name(archive.name + ".manifest.json").write_text(json.dumps(manifest))
    evidence = output / "security"
    smoke = output / "fresh-smoke.json"
    identities = {}
    for arch, digest in (("amd64", "a"), ("arm64", "b")):
        fixture = output / f"{arch}.tar"
        identities[f"linux/{arch}"] = {"imageManifestDigest": "sha256:" + digest * 64,
                                       "imageConfigDigest": write_platform_export(fixture, arch)}
    smoke.write_text(json.dumps({"schema": "emulebb.rust.image-smoke/4", "status": "passed", "archiveSha256": manifest["archiveSha256"],
                                 "platforms": [{"platform": cpu, **identity} for cpu, identity in identities.items()]}))
    calls = []
    smoke_calls = []
    faults = {"vulnerability": False, "stale_tag": False, "wrong_sbom_arch": False}
    host = SimpleNamespace(architecture="amd64", system="Linux", machine="x86_64")
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
                return SimpleNamespace(stdout=host.architecture)
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
    monkeypatch.setattr(image_security, "git_output", lambda *args: "a" * 40)
    monkeypatch.setattr(image_security, "archive_digest", lambda *args, **kwargs: "sha256:" + "c" * 64)
    monkeypatch.setattr(image_security.platform, "system", lambda: host.system)
    monkeypatch.setattr(image_security.platform, "machine", lambda: host.machine)
    monkeypatch.setattr(image_security.subprocess, "check_output", lambda *args, **kwargs: "1000\n")
    monkeypatch.setattr(image_security, "smoke_image", lambda *args, **kwargs: smoke_calls.append(kwargs) or smoke)
    monkeypatch.setattr(image_security, "run_owned_container", run)
    return SimpleNamespace(layout=SimpleNamespace(output_root=output, tests_repo_root=tmp_path / "harness",
                                                  emulebb_rust_repo_root=rust), archive=archive,
                           evidence=evidence, faults=faults, calls=calls, host=host, manifest=manifest,
                           smoke_calls=smoke_calls)


def test_both_cpu_scans_require_inventory_and_hard_vulnerability_gate(scanner):
    report = image_security.validate_image(scanner.layout, archive=scanner.archive, image="local/test:beta",
                                            architectures=("amd64", "arm64"), security_dir=scanner.evidence)
    data = json.loads(report.read_text())
    assert data["status"] == "passed"
    assert data["schema"] == "emulebb.rust.image-security/2" and data["workflow"] is None
    assert data["origin"] == "local-development" and data["ociManifestDigest"] == "sha256:" + "c" * 64
    assert all(item["imageConfigDigest"].startswith("sha256:") for item in data["platforms"])
    assert [item["runtimeExecution"] for item in data["platforms"]] == ["native", "qemu"]
    assert len(scanner.calls) == 4
    for command in scanner.calls:
        assert image_security.TRIVY_IMAGE in command
        assert command[command.index("--input") + 1] in ("/scan/amd64.tar", "/scan/arm64.tar")
        assert not any("docker.sock" in argument for argument in command)
        assert command[command.index("--user") + 1] == "1000:1000"
        assert "TRIVY_CACHE_DIR=/cache" in command
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
    kwargs = {"expected_source": "a" * 40, "expected_version": "0.1.0-beta.2"}
    image_security.verify_platform_export(archive, architecture="amd64", expected_config=expected, **kwargs)
    with pytest.raises(RuntimeError, match="architecture"):
        image_security.verify_platform_export(archive, architecture="arm64", expected_config=expected, **kwargs)
    write_platform_export(archive, "arm64")
    with pytest.raises(RuntimeError, match="config does not match"):
        image_security.verify_platform_export(archive, architecture="arm64", expected_config=expected, **kwargs)


@pytest.mark.parametrize("source,version", [("b" * 40, "0.1.0-beta.2"), ("a" * 40, "0.1.0-beta.1")])
def test_correct_cpu_and_digest_cannot_hide_wrong_revision_or_version_labels(tmp_path, source, version):
    archive = tmp_path / "platform.tar"
    expected = write_platform_export(archive, "amd64", source=source, version=version)
    with pytest.raises(RuntimeError, match="revision or version labels"):
        image_security.verify_platform_export(archive, architecture="amd64", expected_config=expected,
                                               expected_source="a" * 40, expected_version="0.1.0-beta.2")


@pytest.fixture
def native_scanner(scanner, monkeypatch):
    context = {"repository": "emulebb/emulebb-rust", "runId": "123", "runAttempt": "3", "workflowSha": "b" * 40}
    for name, value in {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": context["repository"],
                        "GITHUB_RUN_ID": context["runId"], "GITHUB_RUN_ATTEMPT": context["runAttempt"],
                        "GITHUB_WORKFLOW_SHA": context["workflowSha"]}.items():
        monkeypatch.setenv(name, value)
    scanner.manifest.update(origin="github-actions", workflow={**context, "runAttempt": "2"})
    for item in scanner.manifest["inputs"]:
        item.update(origin="github-actions", workflow={**context, "runAttempt": "2"})
    scanner.archive.with_name(scanner.archive.name + ".manifest.json").write_text(json.dumps(scanner.manifest))
    scanner.context = context
    return scanner


@pytest.mark.parametrize("architecture,machine", [("amd64", "x86_64"), ("arm64", "aarch64")])
def test_hosted_native_cpu_gate_records_current_validator_and_earlier_producer_attempt(native_scanner, architecture, machine):
    case = native_scanner
    case.host.architecture, case.host.machine = architecture, machine
    smoke = case.layout.output_root / "fresh-smoke.json"
    data = json.loads(smoke.read_text())
    data["platforms"] = [item for item in data["platforms"] if item["platform"] == "linux/" + architecture]
    smoke.write_text(json.dumps(data))
    report = image_security.validate_image_ci(case.layout, archive=case.archive, image="local/test:beta",
                                              architecture=architecture, security_dir=case.evidence,
                                              control_rust_root=case.layout.emulebb_rust_repo_root)
    data = json.loads(report.read_text())
    assert data["status"] == "passed" and data["origin"] == "github-actions"
    assert data["workflow"] == case.context and data["producerWorkflow"]["runAttempt"] == "2"
    assert len(data["platforms"]) == 1 and data["platforms"][0]["runtimeExecution"] == "native"
    assert data["platforms"][0]["platform"] == "linux/" + architecture


@pytest.mark.parametrize("fault", ["local-validation", "wrong-os", "wrong-machine", "wrong-docker-cpu",
                                  "local-image", "wrong-builder", "wrong-run", "wrong-root", "wrong-archive", "wrong-source"])
def test_hosted_native_preflight_rejects_unprovable_inputs_before_smoke(native_scanner, monkeypatch, fault):
    case = native_scanner
    if fault == "local-validation":
        monkeypatch.delenv("GITHUB_ACTIONS")
    elif fault == "wrong-os":
        case.host.system = "Windows"
    elif fault == "wrong-machine":
        case.host.machine = "aarch64"
    elif fault == "wrong-docker-cpu":
        case.host.architecture = "arm64"
    elif fault == "local-image":
        case.manifest["origin"] = "local-development"
    elif fault == "wrong-builder":
        case.manifest["builderSha"] = "b" * 40
    elif fault == "wrong-run":
        case.manifest["workflow"]["runId"] = "124"
    elif fault == "wrong-root":
        case.manifest["ociManifestDigest"] = "sha256:" + "d" * 64
    elif fault == "wrong-archive":
        case.manifest["archiveSha256"] = "d" * 64
    else:
        case.manifest["sourceSha"] = "b" * 40
    case.archive.with_name(case.archive.name + ".manifest.json").write_text(json.dumps(case.manifest))
    with pytest.raises(RuntimeError):
        image_security.validate_image_ci(case.layout, archive=case.archive, image="local/test:beta",
                                         architecture="amd64", security_dir=case.evidence,
                                         control_rust_root=case.layout.emulebb_rust_repo_root)
    assert case.smoke_calls == [] and case.calls == []


def test_archive_changed_during_scanning_cannot_receive_passing_evidence(scanner, monkeypatch):
    scan = image_security.run_owned_container
    def changed(docker, command, log, **kwargs):
        scan(docker, command, log, **kwargs)
        scanner.archive.write_bytes(b"changed archive")
    monkeypatch.setattr(image_security, "run_owned_container", changed)
    with pytest.raises(RuntimeError, match="Image validation failed"):
        image_security.validate_image(scanner.layout, archive=scanner.archive, image="local/test:beta",
                                      architectures=("amd64", "arm64"), security_dir=scanner.evidence)
    report = json.loads(next(scanner.evidence.glob("*-security.json")).read_text())
    assert report["status"] == "failed" and "changed during validation" in report["error"]
