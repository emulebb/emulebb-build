"""Release evidence must cover every native CPU, libc and image variant."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from emule_workspace import release_candidate as candidate
from emule_workspace.release import _sha256, _sha256sums_text

VERSION = "0.1.0-beta.2"
STEM = f"emulebb-rust-v{VERSION}"
PAYLOADS = {
    "windows-x64": ["windows-x64.zip"],
    "windows-arm64": ["windows-arm64.zip"],
    "linux-x64": ["linux-amd64.deb", "linux-x86_64.AppImage"],
    "linux-arm64": ["linux-arm64.deb", "linux-aarch64.AppImage"],
    "macos-x64": ["macos-x64.dmg"],
    "macos-arm64": ["macos-arm64.dmg"],
    "linux-x64-musl": ["linux-amd64-musl.tar.zst"],
    "linux-arm64-musl": ["linux-arm64-musl.tar.zst"],
}


@pytest.fixture
def evidence(tmp_path):
    root = tmp_path / "release"
    archives = tmp_path / "archives"
    root.mkdir()
    archives.mkdir()
    source = dict(zip(
        ("emulebbRust", "emulebbBuild", "emulebbBuildTests", "emulebbTooling",
         "emulebbMiniupnp", "emulebbLibpcpnatpmp"),
        (str(value) * 40 for value in range(1, 7)),
    ))
    workflow = {"repository": "example/project", "runId": "101",
                "runAttempt": "1", "workflowSha": "7" * 40}
    context = candidate.CandidateContext(VERSION, "candidate", source,
                                         {**workflow, "runAttempt": "2"})

    def save(name, value):
        (root / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return value

    def read(name):
        return json.loads((root / name).read_text(encoding="utf-8"))

    def sums(name, members):
        (root / name).write_text(
            _sha256sums_text(root, tuple(root / item for item in sorted(members))),
            encoding="ascii", newline="\n",
        )

    def records(names):
        return [{"name": name, "sha256": _sha256(root / name),
                 "sizeBytes": (root / name).stat().st_size} for name in sorted(names)]

    material = {key: {"commit": value} for key, value in source.items() if key != "emulebbBuildTests"}
    packages = {}
    for platform, suffixes in PAYLOADS.items():
        results = []
        for suffix in suffixes:
            name = f"{STEM}-{suffix}"
            (root / name).write_bytes(f"Synthetic package {suffix}".encode("ascii"))
            sbom = name.removesuffix(".zip").removesuffix(".tar.zst") + ".sbom.spdx.json"
            save(sbom, {"spdxVersion": "SPDX-2.3", "packages": []})
            metadata = {
                "schema": "emulebb.rust.package/2", "package": "emulebb-rust", "version": VERSION,
                "sourceDirty": False, "source": material, "origin": "github-actions",
                "workflow": workflow, "asset": name, "sha256": _sha256(root / name),
                "sbom": sbom, "sbomSha256": _sha256(root / sbom),
            }
            if platform.endswith("-musl"):
                arch = "aarch64" if "arm64" in platform else "x86_64"
                metadata.update({
                    "target": f"{arch}-unknown-linux-musl", "libc": "musl",
                    "buildExecution": "native", "libraryTestExecution": "native",
                    "testCompilation": "workspace", "testExecution": "workspace", "testProfile": "test",
                    "elfAudit": "Machine: AArch64" if arch == "aarch64" else "Machine: Advanced Micro Devices X86-64",
                })
            save(name + ".manifest.json", metadata)
            packages[name] = metadata
            partial = {"state": "paused", "partsTotal": 2, "verifiedPartIndices": [0],
                       "verifiedBytes": 8, "completedBytes": 10}
            results.append({
                "asset": name, "status": "passed", "assetSha256": metadata["sha256"],
                "packageManifestSha256": _sha256(root / (name + ".manifest.json")),
                "sbomSha256": metadata["sbomSha256"], "sourceSha": source["emulebbRust"],
                "appVersion": VERSION, "appBuild": "release",
                "appPlatform": "arm64" if "arm64" in platform else "x64",
                "restStatus": True, "gracefulShutdown": True, "profile": "fresh-local-only",
                "webuiAssets": 2, "webuiPanelBackends": {"transfers": "/api/v1/transfers"},
                "renderedWebui": platform == "windows-x64",
                "localTransfer": {
                    "payload": {"name": "synthetic.bin", "sizeBytes": 16, "sha256": "a" * 64},
                    "sharedFile": {"name": "synthetic.bin", "sizeBytes": 16, "hash": "b" * 32},
                    "transferAccepted": True, "sourceDiscovery": "ed2k-link-direct-source",
                    "publicDiscoveryDisabled": True, "gracefulShutdowns": 3,
                    "delivery": {"hash": "b" * 32, "state": "completed", "completedBytes": 16,
                                 "deliveredPathWithinIncoming": True, "deliveredSha256": "a" * 64},
                    "partialRestart": {"before": partial, "after": partial, "sameProfile": True,
                                       "sameBinary": True, "resumeRequested": True, "downloadLimitRestored": True},
                },
            })
        save(f"{STEM}-package-smoke-{platform}.json", {
            "schema": "emulebb.rust.package-smoke/3", "status": "passed",
            "sourceSha": source["emulebbRust"], "harnessSha": source["emulebbBuildTests"],
            "origin": "github-actions", "workflow": workflow,
            "runtimeExecution": "native", "platform": platform, "results": results,
        })

    identities_by_archive = {}
    for variant in ("ubuntu", "alpine"):
        prefix = f"{STEM}-{variant}"
        archive_name = prefix + "-multiarch.oci.tar"
        archive = archives / archive_name
        archive.write_bytes(f"Synthetic {variant} OCI archive".encode("ascii"))
        digest = "sha256:" + ("c" if variant == "ubuntu" else "d") * 64
        identities = {
            "linux/amd64": {"manifestDigest": "sha256:" + "1" * 64, "configDigest": "sha256:" + "2" * 64},
            "linux/arm64": {"manifestDigest": "sha256:" + "3" * 64, "configDigest": "sha256:" + "4" * 64},
        }
        identities_by_archive[archive_name] = (digest, identities)
        inputs = []
        for arch in ("amd64", "arm64"):
            asset = f"{STEM}-linux-{arch}" + (".deb" if variant == "ubuntu" else "-musl.tar.zst")
            package = packages[asset]
            inputs.append({
                "asset": asset, "sha256": package["sha256"], "sbom": package["sbom"],
                "sbomSha256": package["sbomSha256"], "manifestSha256": _sha256(root / (asset + ".manifest.json")),
                "source": material, "origin": "github-actions", "workflow": workflow,
            })
        common = {
            "version": VERSION, "variant": variant,
            "image": f"ghcr.io/emulebb/emulebb-rust:{VERSION}-{variant}",
            "sourceSha": source["emulebbRust"], "sourceDirty": False,
            "builderSha": source["emulebbBuild"], "archiveSha256": _sha256(archive),
            "ociManifestDigest": digest,
        }
        metadata = save(archive_name + ".manifest.json", {
            "schema": "emulebb.rust.image/2", **common, "archive": archive_name,
            "architectures": ["amd64", "arm64"], "origin": "github-actions", "workflow": workflow,
            "dockerfileSha256": "e" * 64, "inputs": inputs,
        })
        proofs = []
        for arch in ("amd64", "arm64"):
            cpu_prefix = f"{prefix}-image-{arch}"
            identity = identities[f"linux/{arch}"]
            smoke_name = cpu_prefix + "-smoke.json"
            save(smoke_name, {
                "schema": "emulebb.rust.image-smoke/4", "status": "passed",
                "image": common["image"], "archiveSha256": common["archiveSha256"],
                "platforms": [{
                    "platform": f"linux/{arch}", "imageManifestDigest": identity["manifestDigest"],
                    "imageConfigDigest": identity["configDigest"],
                    "firstRunProfile": True, "webui": True, "uid": 1234, "gid": 1235,
                    "daemonUid": 1234, "daemonGid": 1235, "configOwner": "1234:1235", "dataOwner": "1234:1235",
                    "publishedPorts": 0, "restAuthentication": True, "authenticatedStatus": 200,
                    "unauthenticatedStatus": 401, "appVersion": VERSION, "appBuild": "release",
                    "appPlatform": "x64" if arch == "amd64" else "arm64",
                    "gracefulShutdown": True, "networkIsolation": "internal-bridge", "dns": "loopback-only",
                }],
            })
            cpu_sbom = cpu_prefix + ".sbom.spdx.json"
            ecosystem, libc, package_arch = ("deb", "libc6", arch) if variant == "ubuntu" else (
                "apk", "musl", "x86_64" if arch == "amd64" else "aarch64"
            )
            save(cpu_sbom, {"spdxVersion": "SPDX-2.3", "packages": [{
                "name": libc, "externalRefs": [{"referenceType": "purl",
                    "referenceLocator": f"pkg:{ecosystem}/{libc}@1?arch={package_arch}"}],
            }]})
            vulnerabilities = cpu_prefix + ".vulnerabilities.txt"
            (root / vulnerabilities).write_text("No fixable HIGH or CRITICAL vulnerabilities.\n", encoding="utf-8")
            (root / (cpu_prefix + "-scanner.txt")).write_text("Version: 0.75.0\n", encoding="utf-8")
            report_name = cpu_prefix + "-security.json"
            report = {
                "schema": "emulebb.rust.image-security/2", "status": "passed", **common,
                "origin": "github-actions", "workflow": workflow, "producerWorkflow": workflow,
                "validatorSha": source["emulebbBuild"], "harnessSha": source["emulebbBuildTests"],
                "imageManifestSha256": _sha256(root / (archive_name + ".manifest.json")),
                "dockerfileSha256": metadata["dockerfileSha256"], "scannerImage": candidate.TRIVY_IMAGE,
                "inputs": inputs, "smoke": smoke_name, "smokeSha256": _sha256(root / smoke_name),
                "platforms": [{
                    "platform": f"linux/{arch}", "status": "passed", "runtimeExecution": "native",
                    "imageManifestDigest": identity["manifestDigest"], "imageConfigDigest": identity["configDigest"],
                    "sbom": cpu_sbom, "sbomSha256": _sha256(root / cpu_sbom),
                    "vulnerabilities": vulnerabilities, "vulnerabilitiesSha256": _sha256(root / vulnerabilities),
                }],
            }
            save(report_name, report)
            members = {cpu_prefix + suffix for suffix in
                       ("-security.json", "-smoke.json", "-scanner.txt", ".sbom.spdx.json", ".vulnerabilities.txt")}
            sums(cpu_prefix + "-security-SHA256SUMS", members)
            proofs.append({"platform": f"linux/{arch}", "report": report_name,
                           "sha256": _sha256(root / report_name), "workflow": workflow, **identity})
        digest_name = prefix + "-multiarch.oci.digest"
        (root / digest_name).write_text(digest + "\n", encoding="ascii", newline="\n")
        retained = {archive_name + ".manifest.json", digest_name} | {
            f"{prefix}-image-{arch}{suffix}" for arch in ("amd64", "arm64") for suffix in
            ("-security.json", "-security-SHA256SUMS", "-smoke.json", "-scanner.txt", ".sbom.spdx.json", ".vulnerabilities.txt")
        }
        summary_name = prefix + "-image-security.json"
        save(summary_name, {
            "schema": "emulebb.rust.image-security-set/1", "status": "passed", **common,
            "origin": "github-actions", "workflow": workflow, "archive": archive_name,
            "validatorSha": source["emulebbBuild"], "harnessSha": source["emulebbBuildTests"],
            "inputs": inputs, "nativeEvidence": proofs, "artifacts": records(retained),
        })
        sums(prefix + "-image-security-SHA256SUMS", retained | {summary_name})

    for suffix in ("NOTES", "CHANGELOG"):
        (root / f"RELEASE-{VERSION}-{suffix}.md").write_text("Synthetic release documentation.\n", encoding="utf-8")
    sums("SHA256SUMS", {path.name for path in root.iterdir()})


    def refresh_root():
        sums("SHA256SUMS", {path.name for path in root.iterdir()
                           if path.name != "SHA256SUMS" and not path.name.endswith("-candidate-manifest.json")})

    def rebind_package_manifest(asset):
        for platform, suffixes in PAYLOADS.items():
            if asset not in {f"{STEM}-{suffix}" for suffix in suffixes}:
                continue
            name = f"{STEM}-package-smoke-{platform}.json"
            report = read(name)
            for result in report["results"]:
                if result["asset"] == asset:
                    result["packageManifestSha256"] = _sha256(root / (asset + ".manifest.json"))
            save(name, report)
        refresh_root()

    def refresh_image(variant):
        prefix = f"{STEM}-{variant}"
        proofs = []
        retained = {f"{prefix}-multiarch.oci.tar.manifest.json", f"{prefix}-multiarch.oci.digest"}
        for arch in ("amd64", "arm64"):
            cpu_prefix = f"{prefix}-image-{arch}"
            members = {cpu_prefix + suffix for suffix in
                       ("-security.json", "-smoke.json", "-scanner.txt", ".sbom.spdx.json", ".vulnerabilities.txt")}
            sums(cpu_prefix + "-security-SHA256SUMS", members)
            retained.update(members | {cpu_prefix + "-security-SHA256SUMS"})
            report_name = cpu_prefix + "-security.json"
            report = read(report_name)
            cpu = report["platforms"][0]
            proofs.append({"platform": f"linux/{arch}", "report": report_name,
                           "sha256": _sha256(root / report_name), "workflow": report["workflow"],
                           "manifestDigest": cpu["imageManifestDigest"], "configDigest": cpu["imageConfigDigest"]})
        summary_name = prefix + "-image-security.json"
        summary = read(summary_name)
        summary["nativeEvidence"] = proofs
        summary["artifacts"] = records(retained)
        save(summary_name, summary)
        sums(prefix + "-image-security-SHA256SUMS", retained | {summary_name})
        refresh_root()

    def archive_identity(path):
        return deepcopy(identities_by_archive[path.name])

    return {"release_dir": root, "archives_dir": archives, "context": context,
            "archive_identity": archive_identity, "read": read, "save": save,
            "refresh_root": refresh_root, "rebind_package_manifest": rebind_package_manifest,
            "refresh_image": refresh_image}


def args(evidence):
    return {name: evidence[name] for name in ("release_dir", "archives_dir", "context", "archive_identity")}


def test_complete_candidate_covers_all_native_and_musl_cpus_and_both_images(evidence):
    path = candidate.write_candidate(**args(evidence))
    saved = candidate.verify_candidate(**args(evidence))
    assert len(saved["artifacts"]) == 73
    assert saved["formatVersion"] == 4
    assert saved["platformProfile"] == "full"
    assert {item["platform"] for item in saved["packageSmoke"]} == set(PAYLOADS)
    assert set(saved["images"]) == {"ubuntu", "alpine"}
    assert all(set(image["platforms"]) == {"linux/amd64", "linux/arm64"} for image in saved["images"].values())
    assert not any(item["name"].endswith(".oci.tar") for item in saved["artifacts"])
    assert path.name == f"{STEM}-candidate-manifest.json"
    assert candidate.verify_public_candidate(
        release_dir=evidence["release_dir"], context=evidence["context"],
        verified_candidate_sha256=_sha256(path),
    ) == saved


@pytest.mark.parametrize("name", [
    f"{STEM}-linux-arm64-musl.tar.zst",
    f"{STEM}-package-smoke-macos-arm64.json",
    f"{STEM}-alpine-image-arm64-security.json",
])
def test_missing_supported_platform_proof_is_rejected(evidence, name):
    (evidence["release_dir"] / name).unlink()
    with pytest.raises(RuntimeError, match="exactly the allowed"):
        candidate.write_candidate(**args(evidence))


def _scoped_evidence(evidence):
    """Remove optional producers entirely, exercising the actual strict validator."""
    from dataclasses import replace

    evidence["context"] = replace(evidence["context"], platform_profile="windows-linux")
    root = evidence["release_dir"]
    for path in root.iterdir():
        if "macos-" in path.name:
            path.unlink()
    evidence["refresh_root"]()


def test_scoped_candidate_requires_no_optional_files_but_keeps_both_oci_variants(evidence):
    _scoped_evidence(evidence)
    candidate.write_candidate(**args(evidence))
    saved = candidate.verify_candidate(**args(evidence))
    assert saved["platformProfile"] == "windows-linux"
    assert len(saved["artifacts"]) == 65
    assert {row["platform"] for row in saved["packageSmoke"]} == set(PAYLOADS) - {"macos-x64", "macos-arm64"}
    assert set(saved["images"]) == {"ubuntu", "alpine"}
    assert all(set(image["platforms"]) == {"linux/amd64", "linux/arm64"} for image in saved["images"].values())


@pytest.mark.parametrize("suffix", [
    "windows-x64.zip", "windows-arm64.zip", "linux-amd64.deb", "linux-x86_64.AppImage",
    "linux-arm64.deb", "linux-aarch64.AppImage", "linux-amd64-musl.tar.zst", "linux-arm64-musl.tar.zst",
    "package-smoke-windows-arm64.json", "alpine-image-arm64-security.json",
])
def test_scoped_candidate_still_rejects_every_missing_mandatory_producer(evidence, suffix):
    _scoped_evidence(evidence)
    (evidence["release_dir"] / f"{STEM}-{suffix}").unlink()
    with pytest.raises(RuntimeError):
        candidate.write_candidate(**args(evidence))


def test_scoped_candidate_does_not_infer_profile_or_accept_optional_inventory(evidence):
    from dataclasses import replace

    _scoped_evidence(evidence)
    candidate.write_candidate(**args(evidence))
    path = candidate.candidate_path(evidence["release_dir"], evidence["context"])
    saved = evidence["read"](path.name)
    saved["platformProfile"] = "full"
    evidence["save"](path.name, saved)
    with pytest.raises(RuntimeError, match="Saved candidate"):
        candidate.verify_candidate(**args(evidence))
    evidence["context"] = replace(evidence["context"], platform_profile="full")
    with pytest.raises(RuntimeError):
        candidate.verify_candidate(**args(evidence))


@pytest.mark.parametrize("field,value", [("sourceDirty", True), ("origin", "local-development"),
                                        ("source", {"emulebbRust": {"commit": "f" * 40}})])
def test_scoped_profile_keeps_exact_source_and_hosted_provenance_checks(evidence, field, value):
    _scoped_evidence(evidence)
    asset = f"{STEM}-windows-x64.zip"
    name = asset + ".manifest.json"
    metadata = evidence["read"](name)
    metadata[field] = value
    evidence["save"](name, metadata)
    evidence["rebind_package_manifest"](asset)
    with pytest.raises(RuntimeError):
        candidate.write_candidate(**args(evidence))


@pytest.mark.parametrize("channel", ["nightly", "beta"])
def test_scoped_profile_cannot_be_promoted_to_a_published_channel(evidence, channel):
    from dataclasses import replace

    _scoped_evidence(evidence)
    with pytest.raises(RuntimeError, match="unpublished manual"):
        replace(evidence["context"], channel=channel).checked()


@pytest.mark.parametrize("name", ["diagnostics.log", "synthetic-private.key", "unexpected-directory"])
def test_only_flat_public_allowlist_is_accepted(evidence, name):
    path = evidence["release_dir"] / name
    if name.endswith("directory"):
        path.mkdir()
    else:
        path.write_text("Synthetic unexpected content", encoding="utf-8")
    with pytest.raises(RuntimeError, match="exactly the allowed"):
        candidate.write_candidate(**args(evidence))


@pytest.mark.parametrize("field,value", [
    ("sourceDirty", True), ("origin", "local-development"),
    ("source", {"emulebbRust": {"commit": "1" * 40}}),
    ("sbom", f"{STEM}-windows-arm64.sbom.spdx.json"),
])
def test_package_origin_materials_and_canonical_sbom_are_required(evidence, field, value):
    name = f"{STEM}-windows-x64.zip.manifest.json"
    data = evidence["read"](name)
    data[field] = value
    evidence["save"](name, data)
    evidence["rebind_package_manifest"](name.removesuffix(".manifest.json"))
    with pytest.raises(RuntimeError):
        candidate.write_candidate(**args(evidence))


@pytest.mark.parametrize("field,value", [
    ("repository", "example/other"), ("runId", "102"), ("workflowSha", "8" * 40),
    ("runAttempt", "3"), ("runAttempt", True),
])
def test_cross_run_or_newer_package_proof_cannot_be_promoted(evidence, field, value):
    name = f"{STEM}-macos-arm64.dmg.manifest.json"
    data = evidence["read"](name)
    data["workflow"][field] = value
    evidence["save"](name, data)
    evidence["rebind_package_manifest"](name.removesuffix(".manifest.json"))
    with pytest.raises(RuntimeError):
        candidate.write_candidate(**args(evidence))


@pytest.mark.parametrize("fault", [
    "schema", "emulated", "harness", "manifest-hash", "delivery-hash",
    "shutdown-count", "index-bool", "index-range", "duplicate-index", "part-count",
    "lost-verified", "completed-before-restart", "other-profile",
])
def test_package_restart_proof_is_strict(evidence, fault):
    name = f"{STEM}-package-smoke-linux-x64-musl.json"
    data = evidence["read"](name)
    result = data["results"][0]
    transfer = result["localTransfer"]
    restart = transfer["partialRestart"]
    if fault == "schema":
        data["schema"] = "emulebb.rust.package-smoke/2"
    elif fault == "emulated":
        data["runtimeExecution"] = "emulated"
    elif fault == "harness":
        data["harnessSha"] = "9" * 40
    elif fault == "manifest-hash":
        result["packageManifestSha256"] = "f" * 64
    elif fault == "delivery-hash":
        transfer["delivery"]["deliveredSha256"] = "f" * 64
    elif fault == "shutdown-count":
        transfer["gracefulShutdowns"] = 2
    elif fault == "index-bool":
        restart["before"]["verifiedPartIndices"] = [False]
    elif fault == "index-range":
        restart["before"]["verifiedPartIndices"] = [2]
    elif fault == "duplicate-index":
        restart["before"]["verifiedPartIndices"] = [0, 0]
    elif fault == "part-count":
        restart["after"]["partsTotal"] = 3
    elif fault == "lost-verified":
        restart["after"]["verifiedBytes"] = 7
    elif fault == "completed-before-restart":
        restart["before"]["completedBytes"] = 16
    elif fault == "other-profile":
        restart["sameProfile"] = False
    evidence["save"](name, data)
    evidence["refresh_root"]()
    with pytest.raises(RuntimeError):
        candidate.write_candidate(**args(evidence))


@pytest.mark.parametrize("fault", ["wrong-libc", "cross-build", "limited-tests", "dynamic-elf"])
def test_musl_requires_native_full_workspace_and_static_binary(evidence, fault):
    name = f"{STEM}-linux-arm64-musl.tar.zst.manifest.json"
    data = evidence["read"](name)
    field, value = {
        "wrong-libc": ("libc", "gnu"), "cross-build": ("buildExecution", "cross"),
        "limited-tests": ("testExecution", "selected-libraries"),
        "dynamic-elf": ("elfAudit", "Machine: AArch64\nINTERP NEEDED libc.so\n"),
    }[fault]
    data[field] = value
    evidence["save"](name, data)
    evidence["rebind_package_manifest"](name.removesuffix(".manifest.json"))
    with pytest.raises(RuntimeError):
        candidate.write_candidate(**args(evidence))


@pytest.mark.parametrize("fault", ["qemu", "wrong-config", "wrong-source", "local-origin", "future-attempt"])
def test_each_image_requires_its_exact_native_cpu_proof(evidence, fault):
    name = f"{STEM}-alpine-image-arm64-security.json"
    data = evidence["read"](name)
    if fault == "qemu":
        data["platforms"][0]["runtimeExecution"] = "qemu"
    elif fault == "wrong-config":
        data["platforms"][0]["imageConfigDigest"] = "sha256:" + "f" * 64
    elif fault == "wrong-source":
        data["sourceSha"] = "9" * 40
    elif fault == "local-origin":
        data["origin"] = "local-development"
    elif fault == "future-attempt":
        data["workflow"]["runAttempt"] = "3"
    evidence["save"](name, data)
    evidence["refresh_image"]("alpine")
    with pytest.raises(RuntimeError):
        candidate.write_candidate(**args(evidence))


def test_actual_archive_is_rehashed_on_every_verification(evidence):
    candidate.write_candidate(**args(evidence))
    archive = evidence["archives_dir"] / f"{STEM}-ubuntu-multiarch.oci.tar"
    archive.write_bytes(b"Changed OCI archive with an unchanged declared digest")
    with pytest.raises(RuntimeError):
        candidate.verify_candidate(**args(evidence))


def test_rerun_cannot_substitute_producers_newer_than_original_assembly(evidence):
    candidate.write_candidate(**args(evidence))
    later = replace(evidence["context"], workflow={**evidence["context"].workflow, "runAttempt": "3"})
    assert candidate.verify_candidate(**{**args(evidence), "context": later})["workflow"]["runAttempt"] == "2"
    name = f"{STEM}-windows-x64.zip.manifest.json"
    data = evidence["read"](name)
    data["workflow"]["runAttempt"] = "3"
    evidence["save"](name, data)
    with pytest.raises(RuntimeError, match="after the bound assembly"):
        candidate.verify_candidate(**{**args(evidence), "context": later})


def test_forged_or_incomplete_root_checksum_inventory_is_rejected(evidence):
    (evidence["release_dir"] / "SHA256SUMS").write_text("0" * 64 + "  synthetic.txt\n", encoding="ascii")
    with pytest.raises(RuntimeError, match="Checksum inventory"):
        candidate.write_candidate(**args(evidence))


def test_publication_binds_the_manifest_sha_supplied_by_image_publisher(evidence):
    path = candidate.write_candidate(**args(evidence))
    kwargs = {"release_dir": evidence["release_dir"], "context": evidence["context"]}
    with pytest.raises(RuntimeError, match="differs from the image publisher"):
        candidate.verify_public_candidate(**kwargs, verified_candidate_sha256="f" * 64)
    digest = _sha256(path)
    (evidence["release_dir"] / f"{STEM}-windows-x64.zip").write_bytes(b"Changed public bytes")
    with pytest.raises(RuntimeError, match="artifact bytes"):
        candidate.verify_public_candidate(**kwargs, verified_candidate_sha256=digest)


def test_candidate_writer_refuses_overwrite(evidence):
    path = candidate.write_candidate(**args(evidence))
    original = path.read_bytes()
    with pytest.raises(RuntimeError, match="exactly the allowed"):
        candidate.write_candidate(**args(evidence))
    assert path.read_bytes() == original


def test_symlink_cannot_replace_an_allowed_public_file(evidence, tmp_path):
    path = evidence["release_dir"] / f"RELEASE-{VERSION}-NOTES.md"
    path.unlink()
    target = tmp_path / "outside-public-inventory.md"
    target.write_text("Synthetic outside content", encoding="utf-8")
    try:
        path.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("This host does not permit creation of a test symlink")
    with pytest.raises(RuntimeError, match="exactly the allowed"):
        candidate.write_candidate(**args(evidence))


@pytest.mark.parametrize("fault", ["missing-harness", "mutable-builder", "mutable-workflow", "zero-attempt"])
def test_expected_context_must_pin_every_source_and_the_workflow(evidence, fault):
    source = dict(evidence["context"].source)
    workflow = dict(evidence["context"].workflow)
    if fault == "missing-harness":
        del source["emulebbBuildTests"]
    elif fault == "mutable-builder":
        source["emulebbBuild"] = "main"
    elif fault == "mutable-workflow":
        workflow["workflowSha"] = "main"
    else:
        workflow["runAttempt"] = "0"
    context = replace(evidence["context"], source=source, workflow=workflow)
    with pytest.raises(RuntimeError):
        candidate.write_candidate(**{**args(evidence), "context": context})


@pytest.mark.parametrize("old_format", [2, 3])
def test_old_candidate_format_cannot_be_verified(evidence, old_format):
    path = candidate.write_candidate(**args(evidence))
    data = evidence["read"](path.name)
    data["formatVersion"] = old_format
    evidence["save"](path.name, data)
    with pytest.raises(RuntimeError, match="Saved candidate"):
        candidate.verify_candidate(**args(evidence))


def test_native_publication_uses_verified_candidate_without_redownloading_oci(evidence):
    path = candidate.write_candidate(**args(evidence))
    digest = _sha256(path)
    for archive in evidence["archives_dir"].iterdir():
        archive.unlink()
    context = replace(evidence["context"], workflow={**evidence["context"].workflow, "runAttempt": "3"})
    saved = candidate.verify_public_candidate(
        release_dir=evidence["release_dir"], context=context, verified_candidate_sha256=digest,
    )
    assert saved["workflow"]["runAttempt"] == "2"
    with pytest.raises(RuntimeError, match="exact OCI archive"):
        candidate.verify_candidate(**args(evidence))


def test_native_publication_rejects_different_expected_source_even_with_the_right_manifest_hash(evidence):
    path = candidate.write_candidate(**args(evidence))
    context = replace(evidence["context"], source={**evidence["context"].source, "emulebbRust": "9" * 40})
    with pytest.raises(RuntimeError, match="Saved candidate source"):
        candidate.verify_public_candidate(
            release_dir=evidence["release_dir"], context=context, verified_candidate_sha256=_sha256(path),
        )


def test_candidate_symlink_is_rejected_before_parsing_its_target(evidence, tmp_path):
    path = candidate.write_candidate(**args(evidence))
    path.unlink()
    target = tmp_path / "synthetic-private-data"
    target.write_bytes(b"Synthetic content that is deliberately not JSON")
    try:
        path.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("This host does not permit creation of a test symlink")
    with pytest.raises(RuntimeError, match="exactly the allowed"):
        candidate.verify_candidate(**args(evidence))
