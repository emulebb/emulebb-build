"""APK publication cannot substitute source, signer or middle-version runtime proof."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import struct
import zipfile

import pytest

from android_package_fixtures import native_elf
from emule_workspace import android_apk_candidate as apk, release_candidate as candidate, release
from emule_workspace.android_apk import public_names, write_github_outputs
from emule_workspace.android_apk_notices import spdx_components

VERSION = "0.1.0-beta.2.nightly.20261113.g1111111"
SOURCE = dict(zip(sorted(candidate.SOURCE_NAMES), (str(value) * 40 for value in range(1, 7))))
SOURCE["emulebbRust"] = "1" * 40
WORKFLOW = {"repository": "example/project", "runId": "101", "runAttempt": "1", "workflowSha": "7" * 40}


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    root = tmp_path / "release"
    root.mkdir()
    context = candidate.CandidateContext(VERSION, "nightly", SOURCE, WORKFLOW,
        apk_sources=candidate.ApkSourceContext("8" * 40, "9" * 40), apk_signer_sha256="c" * 64,
        release_validator_build_sha="a" * 40, apk_harness_root=tmp_path / "harness", apk_version_code=2)
    contract = {"MIN_SCREEN_OFF_DWELL_SECONDS": 60, "MIN_SCREEN_OFF_SAMPLES": 3,
        "required_apk_cases": lambda api: {"start-stop", "delivery-collision", "browser", "doze"} | ({"lan-denial"} if api == 37 else set()),
        "APK_FILE_LINK_CASES": frozenset({"resolve", "pending", "confirm"}),
        "FILE_LINK_BROWSER_REQUIRED_CHECKS": frozenset({"signInPreserved", "cancelWithoutEnqueue", "explicitConfirmation", "repeatedNavigationWithoutEnqueue"})}
    names = public_names(VERSION)
    license_bytes = b"Synthetic source/runtime license text"
    license_hash = hashlib.sha256(license_bytes).hexdigest()
    monkeypatch.setattr(apk, "APACHE_SHA", license_hash)
    notices = {name: license_hash for name in ("eMuleBB-LICENSE", "MiniUPnPc-LICENSE", "libpcpnatpmp-COPYING", "Apache-2.0-LICENSE")}
    native = {}
    with zipfile.ZipFile(root / names["apk"], "w") as archive:
        archive.writestr("AndroidManifest.xml", b"synthetic binary manifest")
        for name in notices:
            archive.writestr("assets/licenses/" + name, license_bytes)
        for abi in apk.TARGETS:
            library = native_elf(tmp_path / abi / "libemulebb_android.so", abi, shared=True)
            native[abi] = {"sha256": release._sha256(library), "elfAudit": apk.audit_android_elf(library, abi, shared_library=True),
                           "runtimeDependencies": {"bundled": [], "needed": ["libc.so"],
                               "readelf": "Dynamic section\n(NEEDED) Shared library: [libc.so]"}}
            name = f"lib/{abi}/libemulebb_android.so"
            entry = zipfile.ZipInfo(name)
            padding = -(archive.fp.tell() + 30 + len(name)) % 16384
            if padding < 4:
                padding += 16384
            entry.extra = struct.pack("<HH", 0xffff, padding - 4) + bytes(padding - 4)
            archive.writestr(entry, library.read_bytes())
    def audit(path, *, scratch, elf_auditor, runtime_libraries):
        assert elf_auditor is apk.audit_android_elf
        assert runtime_libraries == {abi: {} for abi in apk.TARGETS}
        records = {}
        with zipfile.ZipFile(path) as archive:
            for abi in apk.TARGETS:
                library = scratch / (abi + ".so")
                library.write_bytes(archive.read(f"lib/{abi}/libemulebb_android.so"))
                records[abi] = {"sha256": release._sha256(library), "elf": elf_auditor(library, abi, shared_library=True)}
        return {"native": records}
    contract["audit_native_payloads"] = audit
    monkeypatch.setattr(apk, "apk_contract", lambda context: contract)
    monkeypatch.setattr(apk, "_checked_checkout", lambda root, *args: root)
    monkeypatch.setattr(apk, "_checked_reader", lambda context: None)
    monkeypatch.setattr(apk, "guest_inputs", lambda api: {"api": api})
    runtime = {"schema": "emulebb.android.runtime-notices/1", "configuration": "releaseRuntimeClasspath", "components": [
        {"group": "org.jetbrains", "name": "annotations", "version": "13.0", "license": "Apache-2.0",
         "artifacts": [{"path": "annotations.jar", "sha256": "b" * 64, "sizeBytes": 100}]}]}
    identity = {"sourceRevision": SOURCE["emulebbRust"], "version": VERSION, "channel": "nightly"}
    metadata = {"schema": "emulebb.android.apk-package/1", "status": "passed", "debug": False,
        "applicationId": apk.APPLICATION_ID, "minSdk": 34, "targetSdk": 37, "versionCode": 2,
        "sourceDirty": False, "experimental": True, "source": {key: {"commit": value} for key, value in context.apk_materials.items()},
        "origin": "github-actions", "workflow": WORKFLOW, "signerSha256": context.apk_signer_sha256,
        "buildIdentity": identity, "runtimeExecution": "not-run", "toolInputs": apk.tool_inputs(), "abis": list(apk.TARGETS),
        "apk": {"path": names["apk"], "sha256": release._sha256(root / names["apk"]), "sizeBytes": (root / names["apk"]).stat().st_size},
        "native": native, "notices": notices, "runtimeClasspath": runtime, "sbom": names["sbom"]}
    source_names = {"emulebb-rust source": "emulebbRust", "eMule build orchestration": "emulebbBuild", "eMule tooling docs": "emulebbTooling",
                    "eMuleBB MiniUPnP fork": "emulebbMiniupnp", "eMuleBB libpcpnatpmp fork": "emulebbLibpcpnatpmp"}
    sbom = {"spdxVersion": "SPDX-2.3", "packages": [
        {"name": name, "versionInfo": context.apk_materials[source], "licenseDeclared": "BSD-2-Clause" if "libpcpnatpmp" in name else "NOASSERTION"}
        for name, source in source_names.items()] + spdx_components(runtime) + [{"name": "emulebb-rust-android-universal", "versionInfo": VERSION}],
        "files": [{"checksums": [{"algorithm": "SHA256", "checksumValue": metadata["apk"]["sha256"]}]}]}
    def save(name, data):
        (root / name).write_text(json.dumps(data), encoding="utf-8")
    save(names["sbom"], sbom)
    metadata["sbomSha256"] = release._sha256(root / names["sbom"])
    save(names["manifest"], metadata)
    source = {"build": {"commit": context.apk_sources.build_sha}, "harness": {"commit": context.apk_sources.harness_sha}}
    proofs = []
    passed = lambda names: [{"name": name, "status": "passed"} for name in sorted(names)]
    for api in sorted(apk.APIS):
        execution = {"schema": "emulebb.android.apk-lifecycle/1", "status": "passed", "apkSha256": metadata["apk"]["sha256"],
            "sourceRevision": identity["sourceRevision"], "signerSha256": context.apk_signer_sha256, "androidApiLevel": api, "versionCode": 2,
            "runtimeExecution": "executed", "deviceQualification": "emulator-only", "physicalDevice": "not-run",
            "cases": passed(contract["required_apk_cases"](api)),
            "localTransfer": {"status": "passed", "sizeBytes": 1024, "sha256": "d" * 64, "collisionPreserved": True,
                              "deliveredInSharedStorage": True, "peerSourceRevision": identity["sourceRevision"]},
            "fileLinks": {"schema": "emulebb.android.apk-file-links/1", "status": "passed", "cases": passed(contract["APK_FILE_LINK_CASES"]),
                "browser": {"schema": "emulebb-rust.webui-file-link-proof/1", "status": "passed", "buildIdentity": identity,
                    "confirmation": {"status": "passed", "sizeBytes": 1, "state": "paused", "transferHash": "e" * 32,
                                     **{key: True for key in contract["FILE_LINK_BROWSER_REQUIRED_CHECKS"]}}, "cleanup": {"status": "passed"}}},
            "endurance": {"requestedSeconds": 0, "observedSeconds": 60.5, "samples": 3, "minimumDwellSeconds": 60, "minimumSamples": 3, "over24Hours": False}}
        proofs.append({"schema": "emulebb.android.guest-validation/1", "status": "passed", "origin": "github-actions", "workflow": WORKFLOW,
            "scope": "apk-lifecycle", "applicationQualification": "apk-lifecycle", "selectedGuest": "apk" + str(api),
            "apkSha256": metadata["apk"]["sha256"], "apkManifestSha256": release._sha256(root / names["manifest"]),
            "signerSha256": context.apk_signer_sha256, "versionCode": 2, "buildIdentity": identity, "source": source, "kvm": True,
            "inputs": {"api": api}, "imageId": "sha256:" + "f" * 64, "guest": {"sdk": api, "codename": "REL", "machine": "x86_64",
            "pageSize": 16384 if api == 37 else 4096, "emulator": True}, "apk": execution})
    qualification = {"schema": "emulebb.android.apk-qualification/1", "status": "passed", "origin": "github-actions", "workflow": WORKFLOW,
        "apkSha256": metadata["apk"]["sha256"], "apkManifestSha256": release._sha256(root / names["manifest"]),
        "signerSha256": context.apk_signer_sha256, "versionCode": 2, "buildIdentity": identity, "source": source,
        "runtimeExecution": "executed", "deviceQualification": "emulator-only", "proofs": proofs}
    save(names["proof"], qualification)
    return root, context, metadata, qualification, save


def test_apk_binds_four_guest_proofs_to_distinct_producer_and_native_authorities(evidence):
    root, context, _metadata, _proof, _save = evidence
    result = apk.verify_apk(root, context)
    assert result["androidApis"] == [34, 35, 36, 37] and result["signerSha256"] == "c" * 64
    assert context.source["emulebbBuild"] != context.apk_materials["emulebbBuild"]
    assert set(candidate.public_names(context)) - candidate.public_names(replace(context,
        apk_sources=None, apk_signer_sha256=None, apk_harness_root=None, apk_version_code=None)) == set(public_names(VERSION).values())


@pytest.mark.parametrize("fault", ["missing-middle-api", "duplicate-api", "wrong-cert", "wrong-run", "wrong-harness", "wrong-apk",
    "missing-case", "failed-case", "browser-not-executed", "collision-not-preserved", "short-doze", "two-samples", "wrong-page-size", "wrong-input-pins",
    "wrong-version-code", "wrong-installed-code"])
def test_incomplete_or_substituted_runtime_evidence_cannot_publish(evidence, fault):
    root, context, metadata, proof, save = evidence
    guest = proof["proofs"][1]
    if fault == "missing-middle-api": proof["proofs"].pop(1)
    elif fault == "duplicate-api": proof["proofs"][1] = deepcopy(proof["proofs"][0])
    elif fault == "wrong-cert": guest["signerSha256"] = "b" * 64
    elif fault == "wrong-run": guest["workflow"] = {**WORKFLOW, "runId": "102"}
    elif fault == "wrong-harness": guest["source"] = {**guest["source"], "harness": {"commit": "a" * 40}}
    elif fault == "wrong-apk": guest["apkSha256"] = "b" * 64
    elif fault == "missing-case": guest["apk"]["cases"].pop()
    elif fault == "failed-case": guest["apk"]["cases"][0]["status"] = "failed"
    elif fault == "browser-not-executed": guest["apk"]["fileLinks"]["browser"]["confirmation"]["explicitConfirmation"] = False
    elif fault == "collision-not-preserved": guest["apk"]["localTransfer"]["collisionPreserved"] = False
    elif fault == "short-doze": guest["apk"]["endurance"]["observedSeconds"] = 1
    elif fault == "two-samples": guest["apk"]["endurance"]["samples"] = 2
    elif fault == "wrong-version-code": guest["versionCode"] = 3
    elif fault == "wrong-installed-code": guest["apk"]["versionCode"] = 3
    elif fault == "wrong-page-size": proof["proofs"][-1]["guest"]["pageSize"] = 4096
    elif fault == "wrong-input-pins": guest["inputs"] = {"api": 99}
    save(public_names(VERSION)["proof"], proof)
    with pytest.raises(RuntimeError):
        apk.verify_apk(root, context)


@pytest.mark.parametrize("fault", ["native-producer", "local-origin", "unsigned-debug", "wrong-workflow", "native-hash", "version-code"])
def test_changed_producer_authority_or_native_bytes_cannot_be_hidden_by_new_receipt_hash(evidence, fault):
    root, context, metadata, proof, save = evidence
    if fault == "native-producer": metadata["source"]["emulebbBuild"] = {"commit": context.source["emulebbBuild"]}
    elif fault == "local-origin": metadata["origin"] = "local"
    elif fault == "unsigned-debug": metadata["debug"] = True
    elif fault == "wrong-workflow": metadata["workflow"] = {**WORKFLOW, "workflowSha": "b" * 40}
    elif fault == "native-hash": metadata["native"]["arm64-v8a"]["sha256"] = "b" * 64
    elif fault == "version-code": metadata["versionCode"] = 3
    save(public_names(VERSION)["manifest"], metadata)
    digest = release._sha256(root / public_names(VERSION)["manifest"])
    proof["apkManifestSha256"] = digest
    for guest in proof["proofs"]: guest["apkManifestSha256"] = digest
    save(public_names(VERSION)["proof"], proof)
    with pytest.raises(RuntimeError):
        apk.verify_apk(root, context)


def test_missing_apk_authority_is_rejected_without_changing_native_context(evidence):
    _root, context, _metadata, _proof, _save = evidence
    assert replace(context, apk_sources=None, apk_signer_sha256=None, apk_harness_root=None, apk_version_code=None).checked()
    for field in ("apk_sources", "apk_signer_sha256", "release_validator_build_sha", "apk_harness_root", "apk_version_code"):
        with pytest.raises(RuntimeError):
            replace(context, **{field: None}).checked()


def test_release_reader_uses_its_own_revision_instead_of_the_native_producer_pin(evidence, monkeypatch):
    from emule_workspace import git
    _root, context, _metadata, _proof, _save = evidence
    reader = Path(candidate.__file__).resolve().parents[1]
    def query(root, *arguments):
        if arguments == ("rev-parse", "--show-toplevel"): return str(reader)
        if arguments == ("rev-parse", "HEAD"): return context.source["emulebbBuild"]
        return ""
    monkeypatch.setattr(git, "git_output", query)
    with pytest.raises(RuntimeError, match="Release reader"):
        candidate._checked_reader(context)


def test_foreign_apk_harness_revision_is_rejected_before_contract_code_execution(tmp_path, monkeypatch):
    context = candidate.CandidateContext(VERSION, "nightly", SOURCE, WORKFLOW,
        apk_sources=candidate.ApkSourceContext("8" * 40, "9" * 40), apk_signer_sha256="c" * 64,
        release_validator_build_sha="a" * 40, apk_harness_root=tmp_path, apk_version_code=2)
    def reject(root, revision, label):
        assert revision == "9" * 40
        raise RuntimeError("wrong selected Harness revision")
    monkeypatch.setattr(apk, "_checked_checkout", reject)
    monkeypatch.setattr(apk.runpy, "run_path", lambda *args: pytest.fail("untrusted contract executed"))
    with pytest.raises(RuntimeError, match="wrong selected"):
        apk.apk_contract(context)


def test_ci_outputs_bind_actual_verified_apk_and_never_echo_signing_credentials(evidence, monkeypatch):
    root, context, metadata, _proof, _save = evidence
    monkeypatch.setattr(release, "_emulebb_rust_artifact_origin", lambda: {"origin": "github-actions", "workflow": WORKFLOW})
    destination = root.parent / "github-output"
    write_github_outputs(root / public_names(VERSION)["apk"], destination, version=VERSION,
                         source_sha=SOURCE["emulebbRust"], signer_sha256=context.apk_signer_sha256, version_code=2)
    outputs = dict(line.split("=", 1) for line in destination.read_text().splitlines())
    assert outputs["apk_sha256"] == metadata["apk"]["sha256"]
    assert outputs["manifest_sha256"] == release._sha256(root / public_names(VERSION)["manifest"])
    assert outputs["apk_version_code"] == "2"
    with pytest.raises(RuntimeError):
        write_github_outputs(root / public_names(VERSION)["apk"], destination, version=VERSION,
                             source_sha=SOURCE["emulebbRust"], signer_sha256="b" * 64, version_code=2)
