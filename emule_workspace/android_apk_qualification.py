"""Prepare source-bound peers and compose the existing APK guest qualification owner."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
from uuid import uuid4
import zipfile

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


def prepare_peer(layout, *, artifacts: Path, abi: str, version: str, source_sha: str, peer_build_ref: str,
                 producer_context: CandidateContext | None = None) -> Path:
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
    if producer_context is not None:
        _hosted(metadata, producer_context, "Diagnostic original peer producer")
    elif release._emulebb_rust_artifact_origin()["origin"] == "github-actions":
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


def _github_json(endpoint: str) -> dict:
    """Read public same-repository Actions authority without retaining credentials."""
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, check=True, timeout=60)
    if len(result.stdout.encode("utf-8")) > 4 * 1024**2:
        raise RuntimeError("Diagnostic GitHub response exceeds its bound")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("Diagnostic GitHub response must be an object")
    return value


def _original_pins(rust: Path) -> dict:
    """Read immutable source-owned pins with the Builder's pinned YAML parser."""
    import yaml
    value = yaml.safe_load((rust / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    keys = {"emulebbBuild": "EMULEBB_BUILD_REF", "emulebbBuildTests": "EMULEBB_APK_BUILD_TESTS_REF",
        "apkBuild": "EMULEBB_APK_BUILD_REF", "emulebbTooling": "EMULEBB_TOOLING_REF",
        "emulebbMiniupnp": "MINIUPNP_REF", "emulebbLibpcpnatpmp": "PCPNATPMP_REF"}
    environment = value.get("env", {}) if isinstance(value, dict) else {}
    pins = {name: environment.get(key) for name, key in keys.items()}
    if any(not isinstance(ref, str) or re.fullmatch(r"[0-9a-f]{40}", ref) is None for ref in pins.values()):
        raise RuntimeError("Original workflow requires every immutable producer source pin")
    return pins


def _download_diagnostic_artifact(artifact: dict, destination: Path, *, repository: str,
                                  source_sha: str, run_id: str, names: set[str] | None = None) -> dict:
    """Verify GitHub's whole ZIP digest before extracting only bounded flat files."""
    workflow = artifact.get("workflow_run", {})
    if (type(artifact.get("id")) is not int or artifact["id"] <= 0 or artifact.get("expired") is not False
            or type(artifact.get("size_in_bytes")) is not int or not 0 < artifact["size_in_bytes"] <= 512 * 1024**2
            or re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.get("digest", "")) is None
            or workflow.get("head_sha") != source_sha or str(workflow.get("id")) != run_id):
        raise RuntimeError("Diagnostic artifact lacks the exact original run/source/digest authority")
    destination.mkdir(exist_ok=False)
    envelope = destination.parent / (destination.name + ".zip")
    with envelope.open("xb") as stream:
        subprocess.run(["gh", "api", f"repos/{repository}/actions/artifacts/{artifact['id']}/zip"],
                       stdout=stream, stderr=subprocess.PIPE, check=True, timeout=300)
    if envelope.stat().st_size != artifact["size_in_bytes"] or "sha256:" + release._sha256(envelope) != artifact["digest"]:
        raise RuntimeError("Diagnostic artifact ZIP differs from GitHub's independent digest")
    with zipfile.ZipFile(envelope) as archive:
        entries = archive.infolist()
        actual_names = {entry.filename for entry in entries}
        if (len(entries) not in {3, 4} or len(actual_names) != len(entries)
                or names is not None and actual_names != names
                or sum(entry.file_size for entry in entries) > 768 * 1024**2
                or any(re.fullmatch(r"[A-Za-z0-9_.+-]+", entry.filename) is None or entry.filename in {".", ".."}
                       or Path(entry.filename).name != entry.filename or "\\" in entry.filename
                       or entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16)
                       or entry.flag_bits & 1 or not 0 < entry.file_size <= 512 * 1024**2 for entry in entries)):
            raise RuntimeError("Diagnostic artifact requires only its bounded canonical flat payload files")
        for entry in entries:
            with archive.open(entry) as source, (destination / entry.filename).open("xb") as target:
                shutil.copyfileobj(source, target)
    return {key: artifact[key] for key in ("id", "name", "digest")}


def diagnose(layout, *, producer_run_id: str, producer_source_sha: str, manifest_sha256: str,
             image: str, signer_sha256: str, build_ref: str, harness_ref: str) -> Path:
    """Replay verified old bytes under new diagnostics; never create publication evidence."""
    from .android_toolchain import probe_emulator_guest
    if (re.fullmatch(r"[1-9][0-9]*", producer_run_id) is None
            or re.fullmatch(r"[0-9a-f]{40}", producer_source_sha) is None
            or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
            or re.fullmatch(r"[0-9a-f]{64}", signer_sha256) is None
            or image not in {"apk34", "apk35", "apk36", "apk37"}):
        raise ValueError("Diagnostic replay requires exact producer identities, signer and one APK API")
    current = release._emulebb_rust_artifact_origin()
    repository = "emulebb/emulebb-rust"
    if current["origin"] != "github-actions" or current["workflow"]["repository"] != repository:
        raise RuntimeError("Diagnostic replay requires the actual same-repository GitHub caller")
    rust = _checked_checkout(layout.emulebb_rust_repo_root, producer_source_sha, "Diagnostic original Rust source")
    _checked_checkout(Path(__file__).resolve().parents[1], build_ref, "Diagnostic executor Build")
    _checked_checkout(layout.tests_repo_root, harness_ref, "Diagnostic executor Harness")
    pins = _original_pins(rust)
    provenance = release._emulebb_rust_package_provenance(layout, rust)
    materials = _material_commits(provenance["source"])
    for name in ("emulebbTooling", "emulebbMiniupnp", "emulebbLibpcpnatpmp"):
        if materials[name] != pins[name]:
            raise RuntimeError("Diagnostic support checkout differs from the original source-owned pin")
    run = _github_json(f"repos/{repository}/actions/runs/{producer_run_id}")
    if (run.get("head_sha") != producer_source_sha or run.get("repository", {}).get("full_name") != repository
            or run.get("head_repository", {}).get("full_name") != repository
            or run.get("path") != ".github/workflows/nightly.yml" or run.get("status") != "completed"
            or type(run.get("run_attempt")) is not int or run["run_attempt"] <= 0):
        raise RuntimeError("Diagnostic producer must be a completed exact-source official Nightly run")
    jobs = _github_json(f"repos/{repository}/actions/runs/{producer_run_id}/jobs?filter=all&per_page=100")
    required_jobs = {"Build and optionally publish nightly / Signed universal Android application",
        "Build and optionally publish nightly / Experimental Android developer bundle (x86_64)"}
    if (type(jobs.get("total_count")) is not int or jobs["total_count"] > 100
            or not required_jobs <= {job.get("name") for job in jobs.get("jobs", [])
                if job.get("status") == "completed" and job.get("conclusion") == "success"}):
        raise RuntimeError("Diagnostic original APK and peer producer jobs must have succeeded")
    listing = _github_json(f"repos/{repository}/actions/runs/{producer_run_id}/artifacts?per_page=100")
    if type(listing.get("total_count")) is not int or listing["total_count"] > 100:
        raise RuntimeError("Diagnostic artifact inventory exceeds its bound")
    selected = {}
    for name in ("emulebb-rust-android-apk-payload", "emulebb-rust-android-developer-payload-x86_64"):
        matches = [item for item in listing.get("artifacts", []) if item.get("name") == name]
        if len(matches) != 1:
            raise RuntimeError("Diagnostic replay requires one exact original artifact per producer")
        selected[name] = matches[0]
    root = layout.output_root.resolve() / "packages/android-apk-diagnostic" / uuid4().hex
    if not root.resolve().is_relative_to(layout.output_root.resolve()):
        raise RuntimeError("Diagnostic replay output must remain within the inherited output root")
    root.mkdir(parents=True, exist_ok=False)
    payload = root / "apk"
    authority = [_download_diagnostic_artifact(selected["emulebb-rust-android-apk-payload"], payload,
        repository=repository, source_sha=producer_source_sha, run_id=producer_run_id)]
    manifests = list(payload.glob("*.apk.manifest.json"))
    if len(manifests) != 1 or release._sha256(manifests[0]) != manifest_sha256:
        raise RuntimeError("Diagnostic APK receipt differs from the independently selected manifest hash")
    metadata = _read(manifests[0])
    # WHY: the caller selected this exact immutable receipt hash independently.
    # It binds the original version/code; source-owned pins and GitHub's run
    # authority below remain independent of every declaration inside that file.
    version = metadata.get("buildIdentity", {}).get("version", "")
    android_package.identity(version, "nightly", producer_source_sha)
    if not version.startswith(release._emulebb_rust_workspace_version(rust) + ".nightly."):
        raise RuntimeError("Diagnostic APK version differs from its exact Cargo source")
    names = public_names(version)
    if {item.name for item in payload.iterdir()} != {names["apk"], names["manifest"], names["sbom"]}:
        raise RuntimeError("Diagnostic APK artifact inventory is not canonical")
    original_workflow = {"repository": repository, "runId": producer_run_id,
        "runAttempt": str(run["run_attempt"]), "workflowSha": producer_source_sha}
    original = CandidateContext(version, "nightly", {**materials, "emulebbBuild": pins["emulebbBuild"],
        "emulebbBuildTests": pins["emulebbBuildTests"]}, original_workflow,
        apk_sources=ApkSourceContext(pins["apkBuild"], pins["emulebbBuildTests"]),
        apk_signer_sha256=signer_sha256, apk_version_code=metadata.get("versionCode"),
        release_validator_build_sha=build_ref, apk_harness_root=layout.tests_repo_root.resolve()).checked()
    admitted_apk(payload, manifest_name=names["manifest"], manifest_sha256=manifest_sha256, context=original)
    _fields(metadata, {"sbom": names["sbom"], "sbomSha256": release._sha256(payload / names["sbom"])}, "Diagnostic original SPDX bytes")
    native_names = android_package.names(version, "x86_64")
    native = root / "peer"
    authority.append(_download_diagnostic_artifact(selected["emulebb-rust-android-developer-payload-x86_64"], native,
        repository=repository, source_sha=producer_source_sha, run_id=producer_run_id, names=set(native_names.values())))
    peer = prepare_peer(layout, artifacts=native, abi="x86_64", version=version,
        source_sha=producer_source_sha, peer_build_ref=pins["emulebbBuild"], producer_context=original)
    executor = qualification_context(layout, version=version, source_sha=producer_source_sha,
        signer_sha256=signer_sha256, version_code=metadata["versionCode"], build_ref=build_ref, harness_ref=harness_ref)
    return probe_emulator_guest(layout, image=image, apk_payload={"context": executor, "producerContext": original,
        "diagnosticReplay": {"producerWorkflow": original_workflow, "producerSources": original.apk_materials,
            "executorWorkflow": executor.workflow, "executorSources": {"build": build_ref, "harness": harness_ref},
            "artifacts": authority, "publicationEligible": False}, "metadata": metadata,
        "payload": payload.resolve(), "manifestName": names["manifest"], "manifestSha256": manifest_sha256,
        "peer": peer, "peerManifestSha256": release._sha256(peer / "manifest.json"), "soakSeconds": 0,
        "provenance": provenance})


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
