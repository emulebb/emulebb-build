"""Prepare source-bound peers and compose the existing APK guest qualification owner."""
from __future__ import annotations

import json
from pathlib import Path
import re
import tarfile
from uuid import uuid4

from . import android_package, release
from .android_apk import public_names
from .android_apk_candidate import APIS, apk_contract, validate_guest
from .android_inputs import tool_inputs
from .image_security import _material_commits
from .release_candidate import ApkSourceContext, CandidateContext, _checked_checkout, _fields, _hosted, _read, _same


def qualification_context(layout, *, version: str, source_sha: str, signer_sha256: str,
                          version_code: int, build_ref: str, harness_ref: str) -> CandidateContext:
    """Use selected clean source checkouts and caller pins, never artifact-chosen identities."""
    provenance = release._emulebb_rust_package_provenance(layout, layout.emulebb_rust_repo_root)
    materials = _material_commits(provenance["source"])
    if materials["emulebbRust"] != source_sha or materials["emulebbBuild"] != build_ref:
        raise RuntimeError("APK qualification source differs from the caller's exact Rust/Build revisions")
    _checked_checkout(layout.tests_repo_root, harness_ref, "APK qualification Harness")
    origin = release._emulebb_rust_artifact_origin()
    if origin["origin"] != "github-actions":
        raise RuntimeError("Hosted APK qualification requires the actual GitHub workflow context")
    return CandidateContext(version, "nightly", {**materials, "emulebbBuildTests": harness_ref}, origin["workflow"],
        apk_sources=ApkSourceContext(build_ref, harness_ref), apk_signer_sha256=signer_sha256,
        release_validator_build_sha=build_ref, apk_harness_root=layout.tests_repo_root.resolve(), apk_version_code=version_code).checked()


def admitted_apk(payload: Path, *, manifest_name: str, manifest_sha256: str, context: CandidateContext) -> dict:
    names = public_names(context.version)
    if manifest_name != names["manifest"] or not isinstance(manifest_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
        raise RuntimeError("APK qualification requires the canonical independently hashed producer receipt")
    path = payload / manifest_name
    if payload.is_symlink() or path.is_symlink() or not path.is_file() or release._sha256(path) != manifest_sha256:
        raise RuntimeError("APK producer receipt differs from its caller-bound hash")
    value = _read(path)
    _hosted(value, context, "APK qualification producer")
    _fields(value, {"schema": "emulebb.android.apk-package/1", "status": "passed", "debug": False, "sourceDirty": False,
        "signerSha256": context.apk_signer_sha256, "versionCode": context.apk_version_code,
        "buildIdentity": {"sourceRevision": context.source["emulebbRust"], "version": context.version, "channel": "nightly"}}, "APK qualification producer")
    _same(_material_commits(value.get("source")), context.apk_materials, "APK qualification materials")
    apk = payload / names["apk"]
    if apk.is_symlink() or not apk.is_file():
        raise RuntimeError("APK qualification needs its exact regular producer payload")
    _same(value.get("apk"), {"path": names["apk"], "sha256": release._sha256(apk), "sizeBytes": apk.stat().st_size}, "APK qualification bytes")
    return value


def prepare_peer(layout, *, artifacts: Path, abi: str, version: str, source_sha: str, peer_build_ref: str) -> Path:
    """Flatten only an already canonically inspected native archive, without compilation."""
    android_package.identity(version, "nightly", source_sha)
    if abi not in android_package.TARGETS or re.fullmatch(r"[0-9a-f]{40}", peer_build_ref) is None:
        raise ValueError("APK peer preparation requires a supported ABI and explicit native producer revision")
    provenance = release._emulebb_rust_package_provenance(layout, layout.emulebb_rust_repo_root)
    expected = {**_material_commits(provenance["source"]), "emulebbBuild": peer_build_ref}
    if expected["emulebbRust"] != source_sha:
        raise RuntimeError("APK peer profile schema requires the exact selected Rust source")
    names = android_package.names(version, abi)
    archive = artifacts / names["archive"]
    metadata = _read(artifacts / names["manifest"])
    _fields(metadata, {"schema": "emulebb.rust.package/2", "version": version, "channel": "nightly", "abi": abi,
        "target": android_package.TARGETS[abi][0], "sourceDirty": False, "asset": names["archive"],
        "sha256": release._sha256(archive), "sbom": names["sbom"], "sbomSha256": release._sha256(artifacts / names["sbom"])}, "APK native peer archive")
    _same(_material_commits(metadata.get("source")), expected, "APK native peer source authority")
    if release._emulebb_rust_artifact_origin()["origin"] == "github-actions":
        release._assert_emulebb_rust_hosted_artifact(metadata)
    android_package.inspect_archive(archive, abi=abi, files=metadata["files"], sbom_sha256=metadata["sbomSha256"])
    directory = layout.output_root.resolve() / "packages/android-apk-peer" / uuid4().hex
    if not directory.resolve().is_relative_to(layout.output_root.resolve()):
        raise RuntimeError("APK peer output must remain below the inherited output root")
    directory.mkdir(parents=True)
    selected = {"emulebb-rust": "emulebb-rust/emulebb-rust"}
    bundled = metadata.get("runtimeDependencies", {}).get("bundled", [])
    if not isinstance(bundled, list) or len(bundled) > 1 or any(item.get("name") != "libc++_shared.so" for item in bundled):
        raise RuntimeError("APK peer requires the exact reviewed native runtime closure")
    for item in bundled:
        selected[item["name"]] = "emulebb-rust/lib/" + item["name"]
    files = {}
    with tarfile.open(archive, "r:zst") as bundle:
        for name, member in selected.items():
            content = bundle.extractfile(member).read()
            target = directory / name
            target.write_bytes(content)
            if release._sha256(target) != metadata["files"][member]:
                raise RuntimeError("APK peer bytes changed after canonical archive inspection")
            files[name] = {"sha256": release._sha256(target), "sizeBytes": len(content)}
    if release._sha256(archive) != metadata["sha256"]:
        raise RuntimeError("APK peer archive changed during preparation")
    manifest = {"schema": "emulebb.android.apk-peer/1", "sourceRevision": source_sha,
        "buildIdentity": {"sourceRevision": source_sha, "version": version, "channel": "nightly"},
        "abi": abi, "target": android_package.TARGETS[abi][0], "files": files}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    release._assert_emulebb_rust_package_provenance(layout, layout.emulebb_rust_repo_root, provenance)
    return directory


def qualify(layout, *, image: str, payload: Path, manifest_name: str, manifest_sha256: str,
            source_sha: str, signer_sha256: str, version_code: int, version: str,
            peer_artifacts: Path, peer_build_ref: str, build_ref: str, harness_ref: str, soak_seconds: int = 0) -> Path:
    from .android_toolchain import probe_emulator_guest
    if image not in {"apk34", "apk35", "apk36", "apk37"}:
        raise ValueError("Select one of the four supported APK guest APIs")
    context = qualification_context(layout, version=version, source_sha=source_sha, signer_sha256=signer_sha256,
        version_code=version_code, build_ref=build_ref, harness_ref=harness_ref)
    metadata = admitted_apk(payload, manifest_name=manifest_name, manifest_sha256=manifest_sha256, context=context)
    if type(soak_seconds) is not int or not 0 <= soak_seconds <= 7 * 86400:
        raise ValueError("APK observation requires an explicit bounded duration")
    peer = prepare_peer(layout, artifacts=peer_artifacts, abi="x86_64", version=version, source_sha=source_sha, peer_build_ref=peer_build_ref)
    return probe_emulator_guest(layout, image=image, apk_payload={"context": context, "metadata": metadata,
        "payload": payload.resolve(), "manifestName": manifest_name, "manifestSha256": manifest_sha256,
        "peer": peer, "peerManifestSha256": release._sha256(peer / "manifest.json"), "soakSeconds": soak_seconds,
        "provenance": release._emulebb_rust_package_provenance(layout, layout.emulebb_rust_repo_root)})


def assemble(layout, *, payload: Path, manifest_name: str, manifest_sha256: str, evidence_dir: Path,
             release_dir: Path, source_sha: str, signer_sha256: str, version_code: int, version: str,
             build_ref: str, harness_ref: str) -> Path:
    context = qualification_context(layout, version=version, source_sha=source_sha, signer_sha256=signer_sha256,
        version_code=version_code, build_ref=build_ref, harness_ref=harness_ref)
    metadata = admitted_apk(payload, manifest_name=manifest_name, manifest_sha256=manifest_sha256, context=context)
    contract = apk_contract(context)
    paths = sorted(evidence_dir.rglob("validation.json"))
    if len(paths) != 4 or any(path.is_symlink() or not path.resolve().is_relative_to(evidence_dir.resolve()) for path in paths):
        raise RuntimeError("APK assembly requires exactly four owned guest receipt files")
    proofs = [_read(path) for path in paths]
    if {proof.get("guest", {}).get("sdk") for proof in proofs} != APIS:
        raise RuntimeError("APK assembly requires Android APIs 34, 35, 36 and 37 separately")
    for proof in proofs:
        validate_guest(proof, context=context, identity=metadata["buildIdentity"], apk_sha256=metadata["apk"]["sha256"],
            manifest_sha256=manifest_sha256, api=proof["guest"]["sdk"], contract=contract)
    if not release_dir.resolve().is_relative_to(layout.output_root.resolve()):
        raise RuntimeError("APK qualification assembly must remain below the inherited output root")
    release_dir.mkdir(parents=True, exist_ok=True)
    output = release_dir / public_names(version)["proof"]
    value = {"schema": "emulebb.android.apk-qualification/1", "status": "passed", "apkSha256": metadata["apk"]["sha256"],
        "apkManifestSha256": manifest_sha256, "signerSha256": signer_sha256, "versionCode": version_code,
        "buildIdentity": metadata["buildIdentity"], "source": {"build": {"commit": build_ref}, "harness": {"commit": harness_ref}},
        "runtimeExecution": "executed", "deviceQualification": "emulator-only", "proofs": sorted(proofs, key=lambda proof: proof["guest"]["sdk"]),
        **release._emulebb_rust_artifact_origin()}
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return output
