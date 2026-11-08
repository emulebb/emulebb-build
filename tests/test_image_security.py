"""Enforce both image CPUs, exact loaded identities, and hard CVE failures."""
import hashlib
import io
import json
import shutil
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
    smoke.write_text(json.dumps({"schema": "emulebb.rust.image-smoke/4", "status": "passed", "image": manifest["image"],
                                 "archiveSha256": manifest["archiveSha256"],
                                 "platforms": [{"platform": cpu, **identity, "firstRunProfile": True, "webui": True,
                                                "uid": 1234, "gid": 1235, "daemonUid": 1234, "daemonGid": 1235,
                                                "configOwner": "1234:1235", "dataOwner": "1234:1235", "publishedPorts": 0,
                                                "restAuthentication": True, "authenticatedStatus": 200, "unauthenticatedStatus": 401,
                                                "appVersion": manifest["version"], "appBuild": "release",
                                                "appPlatform": "x64" if cpu == "linux/amd64" else "arm64", "gracefulShutdown": True,
                                                "networkIsolation": "internal-bridge", "dns": "loopback-only"}
                                               for cpu, identity in identities.items()]}))
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
            (log.parent / name).write_text(json.dumps({"spdxVersion": "SPDX-2.3", "packages": [
                {"name": "libc6", "externalRefs": [{"referenceType": "purl", "referenceLocator": f"pkg:deb/ubuntu/libc6@2.43?arch={arch}"}]}]}))
        elif faults["vulnerability"] and command[command.index("--platform", command.index("image")) + 1] == "linux/amd64":
            raise RuntimeError("fixable HIGH CVE")
    monkeypatch.setattr(image_security, "LinuxDocker", Docker)
    monkeypatch.setattr(image_security, "git_output", lambda repo, *args: "" if args[0] == "status" else "a" * 40)
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


@pytest.fixture
def candidate_image(native_scanner):
    from emule_workspace import release

    case = native_scanner
    version = case.manifest["version"]
    stem = f"emulebb-rust-v{version}"
    canonical = case.layout.output_root / f"{stem}-multiarch.oci.tar"
    canonical.write_bytes(case.archive.read_bytes())
    case.archive = canonical
    case.manifest.update(archive=canonical.name, image=f"ghcr.io/emulebb/emulebb-rust:{version}")
    case.release_dir = case.layout.output_root / "release"
    case.release_dir.mkdir()
    source = {name: {"commit": "a" * 40} for name in
              ("emulebbRust", "emulebbBuild", "emulebbTooling", "emulebbMiniupnp", "emulebbLibpcpnatpmp")}
    inputs = []
    for arch in ("amd64", "arm64"):
        asset = case.release_dir / f"{stem}-linux-{arch}.deb"
        asset.write_bytes(f"{arch} GNU payload".encode())
        sbom = asset.with_name(asset.name + ".sbom.spdx.json")
        sbom.write_text('{"spdxVersion":"SPDX-2.3","packages":[]}')
        native = {"schema": "emulebb.rust.package/2", "version": version, "sourceDirty": False,
                  "origin": "github-actions", "workflow": case.manifest["workflow"], "source": source,
                  "asset": asset.name, "sha256": release._sha256(asset), "sbom": sbom.name,
                  "sbomSha256": release._sha256(sbom)}
        native_path = asset.with_name(asset.name + ".manifest.json")
        native_path.write_text(json.dumps(native))
        inputs.append({key: native[key] for key in ("asset", "sha256", "sbom", "sbomSha256", "source", "origin", "workflow")})
        inputs[-1]["manifestSha256"] = release._sha256(native_path)
    case.manifest["inputs"] = inputs
    case.archive.with_name(case.archive.name + ".manifest.json").write_text(json.dumps(case.manifest))
    smoke_path = case.layout.output_root / "fresh-smoke.json"
    smoke = json.loads(smoke_path.read_text())
    smoke["image"] = case.manifest["image"]
    identities = {item["platform"]: {"manifestDigest": item["imageManifestDigest"], "configDigest": item["imageConfigDigest"]}
                  for item in smoke["platforms"]}
    case.harness_root = case.layout.tests_repo_root
    (case.harness_root / "scripts").mkdir(parents=True)
    (case.harness_root / "scripts/smoke-rust-container.py").write_text(
        "def oci_platform_identities(path):\n    return " + repr(identities) + "\n")
    case.evidence_dir = case.layout.output_root / "native-proof"
    case.evidence_dir.mkdir()
    for arch, machine in (("amd64", "x86_64"), ("arm64", "aarch64")):
        case.host.architecture, case.host.machine = arch, machine
        selected = {**smoke, "platforms": [item for item in smoke["platforms"] if item["platform"] == "linux/" + arch]}
        smoke_path.write_text(json.dumps(selected))
        cpu_dir = case.layout.output_root / "native-validation" / arch
        image_security.validate_image_ci(case.layout, archive=case.archive, image=case.manifest["image"],
                                         architecture=arch, security_dir=cpu_dir, control_rust_root=case.layout.emulebb_rust_repo_root)
        for path in cpu_dir.iterdir():
            if path.suffix in {".json", ".txt"} or path.name.endswith("-SHA256SUMS"):
                shutil.copy2(path, case.evidence_dir / path.name)
    return case


def assemble_candidate_image(case):
    return image_security.assemble_image_security_ci(
        output_root=case.layout.output_root, archive=case.archive, evidence_dir=case.evidence_dir,
        release_dir=case.release_dir, version=case.manifest["version"], source_sha="a" * 40,
        harness_root=case.harness_root, harness_sha="a" * 40, control_rust_root=case.layout.emulebb_rust_repo_root)


def test_candidate_requires_and_retains_both_native_cpu_proofs(candidate_image):
    from emule_workspace import release

    case = candidate_image
    summary = assemble_candidate_image(case)
    result = json.loads(summary.read_text())
    assert result["schema"] == "emulebb.rust.image-security-set/1" and result["status"] == "passed"
    assert [item["platform"] for item in result["nativeEvidence"]] == ["linux/amd64", "linux/arm64"]
    assert result["workflow"]["runAttempt"] == "3" and result["origin"] == "github-actions"
    assert len(result["artifacts"]) == 14
    assert not any(path.suffix == ".log" for path in case.release_dir.iterdir())
    assert not (case.release_dir / case.archive.name).exists()
    for item in result["artifacts"]:
        assert release._sha256(case.release_dir / item["name"]) == item["sha256"]
    sums = summary.with_name(summary.name.removesuffix(".json") + "-SHA256SUMS")
    public = tuple(sorted((case.release_dir / item["name"] for item in result["artifacts"]), key=lambda path: path.name))
    public = tuple(sorted((*public, summary), key=lambda path: path.name))
    assert sums.read_bytes() == release._sha256sums_text(case.release_dir, public).encode("ascii")
    before = summary.read_bytes()
    with pytest.raises(RuntimeError, match="refuses to overwrite"):
        assemble_candidate_image(case)
    assert summary.read_bytes() == before


@pytest.mark.parametrize("changed", ["payload", "sbom"])
def test_candidate_rejects_rebuilt_native_inputs_even_with_valid_new_package_provenance(candidate_image, changed):
    from emule_workspace import release

    case = candidate_image
    item = case.manifest["inputs"][0]
    asset = case.release_dir / item["asset"]
    path = asset if changed == "payload" else case.release_dir / item["sbom"]
    path.write_bytes(b"new valid job output bytes")
    native_path = asset.with_name(asset.name + ".manifest.json")
    native = json.loads(native_path.read_text())
    native["sha256" if changed == "payload" else "sbomSha256"] = release._sha256(path)
    native["workflow"]["runAttempt"] = "3"
    native_path.write_text(json.dumps(native))
    with pytest.raises(RuntimeError, match="currently assembled package and SBOM bytes"):
        assemble_candidate_image(case)


def test_candidate_allows_later_native_metadata_when_exact_payloads_and_materials_match(candidate_image):
    case = candidate_image
    asset = case.release_dir / case.manifest["inputs"][0]["asset"]
    path = asset.with_name(asset.name + ".manifest.json")
    native = json.loads(path.read_text())
    native["workflow"]["runAttempt"] = "3"
    native["builtUtc"] = "2026-01-01T00:00:00Z"
    path.write_text(json.dumps(native))
    assert json.loads(assemble_candidate_image(case).read_text())["status"] == "passed"


@pytest.mark.parametrize("fault", ["missing-arm", "duplicate-x64", "qemu", "wrong-harness", "foreign-run",
                                  "bad-checksums", "unknown-file", "wrong-cpu-sbom", "bad-smoke-auth", "wrong-config"])
def test_candidate_blocks_incomplete_foreign_or_misbound_native_evidence(candidate_image, fault):
    from emule_workspace import release

    case = candidate_image
    prefix = f'emulebb-rust-v{case.manifest["version"]}-image-arm64'
    report_path = case.evidence_dir / f"{prefix}-security.json"
    report = json.loads(report_path.read_text())
    cpu = report["platforms"][0]
    if fault == "missing-arm":
        report_path.unlink()
    elif fault == "unknown-file":
        (case.evidence_dir / "unexpected.txt").write_text("unlisted evidence")
    elif fault == "bad-checksums":
        (case.evidence_dir / f"{prefix}-security-SHA256SUMS").write_text("0" * 64 + "  ../outside\n")
    else:
        if fault == "duplicate-x64":
            cpu["platform"] = "linux/amd64"
        elif fault == "qemu":
            cpu["runtimeExecution"] = "qemu"
        elif fault == "wrong-harness":
            report["harnessSha"] = "d" * 40
        elif fault == "foreign-run":
            report["workflow"]["runId"] = "124"
        elif fault == "wrong-config":
            cpu["imageConfigDigest"] = "sha256:" + "d" * 64
        elif fault == "bad-smoke-auth":
            path = case.evidence_dir / report["smoke"]
            smoke = json.loads(path.read_text())
            smoke["platforms"][0]["authenticatedStatus"] = 401
            path.write_text(json.dumps(smoke))
            report["smokeSha256"] = release._sha256(path)
        else:
            path = case.evidence_dir / cpu["sbom"]
            sbom = json.loads(path.read_text())
            sbom["packages"][0]["externalRefs"][0]["referenceLocator"] = "pkg:deb/ubuntu/libc6@2.43?arch=amd64"
            path.write_text(json.dumps(sbom))
            cpu["sbomSha256"] = release._sha256(path)
        report_path.write_text(json.dumps(report))
        checksums = case.evidence_dir / f"{prefix}-security-SHA256SUMS"
        members = tuple(sorted((path for path in case.evidence_dir.iterdir()
                                 if path.name.startswith(prefix) and path != checksums), key=lambda path: path.name))
        release._write_sha256sums(checksums, members)
    with pytest.raises(RuntimeError):
        assemble_candidate_image(case)
    assert not list(case.release_dir.glob("*-image-security.json"))
