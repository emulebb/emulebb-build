"""APK-only release gates composed into the existing candidate validator."""
from __future__ import annotations

import math
from pathlib import Path
import re
import runpy
import tempfile

from .android_apk import APPLICATION_ID, MIN_SDK, TARGET_SDK, public_names
from .android_apk_notices import APACHE_SHA, KOTLIN_NOTICES, audit_packaged_notices, spdx_components
from .android_inputs import emulator_inputs, emulator_subset, tool_inputs
from .android_validation import TARGETS, SYSTEM_LIBRARIES, audit_android_elf, needed_libraries
from .image_security import _material_commits
from .release import _sha256
from .release_candidate import (HASH, CandidateContext, _checked_checkout, _checked_reader, _fields,
                                _hosted, _object, _positive, _read, _same)

APIS = frozenset({34, 35, 36, 37})


def apk_contract(context: CandidateContext) -> dict:
    """Load fresh stdlib-only contracts from the separately pinned APK Harness."""
    context.checked()
    if context.apk_sources is None:
        raise RuntimeError("APK validation requires its explicit source context")
    root = _checked_checkout(context.apk_harness_root, context.apk_sources.harness_sha, "APK Harness")
    contract = {}
    for filename in ("android_apk_lifecycle.py", "android_apk.py"):
        source = root / "emule_test_harness" / filename
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(root):
            raise RuntimeError("APK contract must belong to the selected Harness checkout")
        contract.update(runpy.run_path(str(source)))
    if (contract.get("SUPPORTED_ANDROID_APIS") != APIS
            or not callable(contract.get("required_apk_cases"))
            or not callable(contract.get("audit_native_payloads"))
            or not isinstance(contract.get("APK_FILE_LINK_CASES"), frozenset)
            or not isinstance(contract.get("FILE_LINK_BROWSER_REQUIRED_CHECKS"), frozenset)
            or type(contract.get("MIN_SCREEN_OFF_DWELL_SECONDS")) is not int or contract["MIN_SCREEN_OFF_DWELL_SECONDS"] < 60
            or type(contract.get("MIN_SCREEN_OFF_SAMPLES")) is not int or contract["MIN_SCREEN_OFF_SAMPLES"] < 3):
        raise RuntimeError("Selected APK Harness lacks the complete Android 14–17 qualification contract")
    _checked_checkout(root, context.apk_sources.harness_sha, "APK Harness")
    return contract


def _cases(value: object, required: set | frozenset, label: str) -> None:
    if (not isinstance(value, list) or len(value) != len(required)
            or any(not isinstance(item, dict) or not isinstance(item.get("name"), str) or item.get("status") != "passed" for item in value)
            or {item.get("name") for item in value} != required):
        raise RuntimeError(label + " requires every exact successful source-owned case")


def validate_runtime(execution: dict, *, api: int, identity: dict, apk_sha256: str,
                     expected_cert_sha256: str, version_code: int, contract: dict) -> None:
    """Check one actual lifecycle/transfer/browser proof without accepting its own authority."""
    if type(api) is not int or api not in APIS:
        raise RuntimeError("APK qualification requires one supported Android API")
    _fields(execution, {"schema": "emulebb.android.apk-lifecycle/1", "status": "passed",
        "apkSha256": apk_sha256, "sourceRevision": identity["sourceRevision"],
        "signerSha256": expected_cert_sha256, "versionCode": version_code, "androidApiLevel": api, "runtimeExecution": "executed",
        "deviceQualification": "emulator-only", "physicalDevice": "not-run"}, "APK lifecycle")
    _cases(execution.get("cases"), contract["required_apk_cases"](api), "APK lifecycle")
    transfer = _object(execution.get("localTransfer"), "APK local transfer")
    _fields(transfer, {"status": "passed", "collisionPreserved": True, "deliveredInSharedStorage": True,
                      "completedDeliverySurvivesRestart": True,
                      "peerSourceRevision": identity["sourceRevision"]}, "APK local transfer")
    _positive(transfer.get("sizeBytes"), "APK delivered size")
    if not isinstance(transfer.get("sha256"), str) or HASH.fullmatch(transfer["sha256"]) is None:
        raise RuntimeError("APK local transfer requires actual completed-byte evidence")
    links = _object(execution.get("fileLinks"), "APK file-link proof")
    _fields(links, {"schema": "emulebb.android.apk-file-links/1", "status": "passed"}, "APK file-link proof")
    _cases(links.get("cases"), contract["APK_FILE_LINK_CASES"], "APK file-link proof")
    browser = _object(links.get("browser"), "APK browser proof")
    _fields(browser, {"schema": "emulebb-rust.webui-file-link-proof/1", "status": "passed",
                      "buildIdentity": identity}, "APK browser proof")
    confirmation = _object(browser.get("confirmation"), "APK browser confirmation")
    _fields(confirmation, {"status": "passed", "sizeBytes": 1, "state": "paused",
                          **{name: True for name in contract["FILE_LINK_BROWSER_REQUIRED_CHECKS"]}},
            "APK browser confirmation")
    if not isinstance(confirmation.get("transferHash"), str) or re.fullmatch(r"[0-9a-f]{32}", confirmation["transferHash"]) is None:
        raise RuntimeError("APK browser must confirm its real neutral queue fixture")
    _fields(_object(browser.get("cleanup"), "APK browser cleanup"), {"status": "passed"}, "APK browser cleanup")
    endurance = _object(execution.get("endurance"), "APK screen-off observation")
    minimum_dwell, minimum_samples = contract["MIN_SCREEN_OFF_DWELL_SECONDS"], contract["MIN_SCREEN_OFF_SAMPLES"]
    _fields(endurance, {"minimumDwellSeconds": minimum_dwell, "minimumSamples": minimum_samples}, "APK screen-off observation")
    requested, observed, samples = (endurance.get(name) for name in ("requestedSeconds", "observedSeconds", "samples"))
    if (type(requested) is not int or not 0 <= requested <= 7 * 86400
            or type(observed) not in (int, float) or not math.isfinite(observed) or observed < max(minimum_dwell, requested)
            or type(samples) is not int or samples < minimum_samples
            or endurance.get("over24Hours") is not (observed > 86400)):
        raise RuntimeError("APK screen-off proof requires measured, bounded observation")


def guest_inputs(api: int) -> dict:
    return emulator_subset(emulator_inputs(), ("apk" + str(api),))


def validate_guest(receipt: dict, *, context: CandidateContext, identity: dict,
                   apk_sha256: str, manifest_sha256: str, api: int, contract: dict) -> None:
    """Bind a guest receipt to the caller's source, run, signer and exact APK bytes."""
    _hosted(receipt, context, "APK guest receipt")
    _fields(receipt, {"schema": "emulebb.android.guest-validation/1", "status": "passed",
        "scope": "apk-lifecycle", "applicationQualification": "apk-lifecycle", "selectedGuest": "apk" + str(api),
        "apkSha256": apk_sha256, "apkManifestSha256": manifest_sha256,
        "signerSha256": context.apk_signer_sha256, "versionCode": context.apk_version_code,
        "buildIdentity": identity, "kvm": True}, "APK guest receipt")
    _same(receipt.get("source"), {"build": {"commit": context.apk_sources.build_sha},
                                  "harness": {"commit": context.apk_sources.harness_sha}}, "APK guest sources")
    _same(receipt.get("inputs"), guest_inputs(api), "APK guest input pins")
    if not isinstance(receipt.get("imageId"), str) or re.fullmatch(r"sha256:[0-9a-f]{64}", receipt["imageId"]) is None:
        raise RuntimeError("APK guest receipt requires its actual prepared image identity")
    _same(receipt.get("guest"), {"sdk": api, "codename": "REL", "machine": "x86_64",
          "pageSize": 16384 if api == 37 else 4096, "emulator": True}, "APK actual guest facts")
    validate_runtime(_object(receipt.get("apk"), "APK guest execution"), api=api, identity=identity,
        apk_sha256=apk_sha256, expected_cert_sha256=context.apk_signer_sha256,
        version_code=context.apk_version_code, contract=contract)


def _sbom(sbom: dict, metadata: dict, context: CandidateContext, apk_hash: str) -> None:
    _fields(sbom, {"spdxVersion": "SPDX-2.3"}, "APK SPDX")
    packages = sbom.get("packages")
    if not isinstance(packages, list) or any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in packages):
        raise RuntimeError("APK SPDX requires its runtime and source components")
    by_name = {item.get("name"): item for item in packages}
    if len(by_name) != len(packages):
        raise RuntimeError("APK SPDX repeats a component")
    sources = {"emulebb-rust source": "emulebbRust", "eMule build orchestration": "emulebbBuild",
        "eMule tooling docs": "emulebbTooling", "eMuleBB MiniUPnP fork": "emulebbMiniupnp",
        "eMuleBB libpcpnatpmp fork": "emulebbLibpcpnatpmp"}
    runtime = _object(metadata.get("runtimeClasspath"), "APK runtime inventory")
    _fields(runtime, {"schema": "emulebb.android.runtime-notices/1", "configuration": "releaseRuntimeClasspath"}, "APK runtime inventory")
    components = runtime.get("components")
    if not isinstance(components, list) or not components or len(components) > 128:
        raise RuntimeError("APK SPDX requires its actual bounded runtime graph")
    expected_runtime = spdx_components(runtime)
    required = set(sources) | {item["name"] for item in expected_runtime} | {"emulebb-rust-android-universal"}
    if any(metadata["native"][abi]["runtimeDependencies"]["bundled"] for abi in TARGETS):
        required.add("Android NDK libc++ runtime")
    if set(by_name) != required:
        raise RuntimeError("APK SPDX must contain exactly selected source/runtime components")
    if "Android NDK libc++ runtime" in required:
        _fields(by_name["Android NDK libc++ runtime"], {"licenseDeclared": "Apache-2.0 WITH LLVM-exception",
                "versionInfo": tool_inputs()["assets"]["ndk"]["version"]}, "APK SPDX NDK runtime")
    for name, source in sources.items():
        _fields(by_name[name], {"versionInfo": context.apk_materials[source]}, "APK SPDX source")
    _fields(by_name["eMuleBB libpcpnatpmp fork"], {"licenseDeclared": "BSD-2-Clause"}, "APK SPDX COPYING license")
    for expected in expected_runtime:
        _fields(by_name[expected["name"]], expected, "APK SPDX runtime")
    _fields(by_name["emulebb-rust-android-universal"], {"versionInfo": context.version}, "APK SPDX distribution")
    files = sbom.get("files")
    if (not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict)
            or {"algorithm": "SHA256", "checksumValue": apk_hash} not in files[0].get("checksums", [])):
        raise RuntimeError("APK SPDX must bind the exact distributed APK bytes")


def verify_apk(root: Path, context: CandidateContext) -> dict:
    """Require all four real emulator proofs in addition to native candidate gates."""
    context.checked()
    _checked_reader(context)
    contract = apk_contract(context)
    names = public_names(context.version)
    metadata = _read(root / names["manifest"])
    apk = root / names["apk"]
    apk_hash, manifest_hash = _sha256(apk), _sha256(root / names["manifest"])
    identity = {"sourceRevision": context.source["emulebbRust"], "version": context.version, "channel": "nightly"}
    _hosted(metadata, context, "APK producer")
    _fields(metadata, {"schema": "emulebb.android.apk-package/1", "status": "passed", "debug": False,
        "applicationId": APPLICATION_ID, "minSdk": MIN_SDK, "targetSdk": TARGET_SDK, "sourceDirty": False, "experimental": True,
        "signerSha256": context.apk_signer_sha256, "versionCode": context.apk_version_code,
        "buildIdentity": identity, "runtimeExecution": "not-run",
        "toolInputs": tool_inputs(), "abis": list(TARGETS), "sbom": names["sbom"],
        "sbomSha256": _sha256(root / names["sbom"])}, "APK producer")
    _positive(metadata.get("versionCode"), "APK version code")
    if metadata["versionCode"] > 2_100_000_000:
        raise RuntimeError("APK version code exceeds Android's supported range")
    _same(_material_commits(metadata.get("source")), context.apk_materials, "APK producer sources")
    _same(metadata.get("apk"), {"path": names["apk"], "sha256": apk_hash, "sizeBytes": apk.stat().st_size}, "APK distributed bytes")
    native = _object(metadata.get("native"), "APK native inventory")
    if set(native) != set(TARGETS):
        raise RuntimeError("APK producer requires both native ABIs")
    runtime = {}
    for abi in TARGETS:
        dependencies = _object(native[abi].get("runtimeDependencies"), "APK native runtime closure")
        needed = needed_libraries(dependencies.get("readelf", ""))
        _same(dependencies.get("needed"), needed, "APK native DT_NEEDED")
        if set(needed) - SYSTEM_LIBRARIES - {"libc++_shared.so"}:
            raise RuntimeError("APK native payload has unresolved runtime dependencies")
        bundled = dependencies.get("bundled")
        expected_names = {"libc++_shared.so"} if "libc++_shared.so" in needed else set()
        if (not isinstance(bundled, list) or len(bundled) != len(expected_names)
                or any(not isinstance(item, dict) for item in bundled)
                or {item.get("name") for item in bundled} != expected_names):
            raise RuntimeError("APK must declare its exact reviewed native runtime closure")
        ndk = tool_inputs()["assets"]["ndk"]
        for item in bundled:
            _fields(item, {"ndkVersion": ndk["version"], "ndkArchiveSha256": ndk["sha256"]}, "APK pinned NDK runtime")
        runtime[abi] = {item["name"]: item["sha256"] for item in bundled}
    with tempfile.TemporaryDirectory(prefix="apk-candidate-", dir=root.parent) as temporary:
        audited = contract["audit_native_payloads"](apk, scratch=Path(temporary).resolve(),
                    elf_auditor=audit_android_elf, runtime_libraries=runtime)
    for abi in TARGETS:
        _fields(audited["native"][abi], {"sha256": native[abi]["sha256"], "elf": native[abi]["elfAudit"]}, "APK actual native bytes")
        for item in native[abi]["runtimeDependencies"]["bundled"]:
            _fields(audited["runtime"][abi][item["name"]], {"sha256": item["sha256"], "elf": item["elfAudit"]}, "APK actual NDK runtime bytes")
    notices = _object(metadata.get("notices"), "APK license inventory")
    if (not {"eMuleBB-LICENSE", "MiniUPnPc-LICENSE", "libpcpnatpmp-COPYING", "Apache-2.0-LICENSE"} <= set(notices)
            or notices["Apache-2.0-LICENSE"] != APACHE_SHA
            or any(not isinstance(name, str) or Path(name).name != name or "\\" in name
                   or not isinstance(value, str) or HASH.fullmatch(value) is None for name, value in notices.items())):
        raise RuntimeError("APK must include its exact complete source/runtime license assets")
    for component in metadata.get("runtimeClasspath", {}).get("components", []):
        if component.get("group") == "org.jetbrains.kotlin" and component.get("name") == "kotlin-stdlib":
            for name, (_source, digest) in KOTLIN_NOTICES.items():
                if notices.get(name) != digest:
                    raise RuntimeError("APK Kotlin JVM third-party notices differ from reviewed inputs")
    audit_packaged_notices(apk, notices)
    for abi in TARGETS:
        for item in native[abi]["runtimeDependencies"]["bundled"]:
            if item.get("licenseSha256") != notices.get("Android-NDK-NOTICE"):
                raise RuntimeError("APK native runtime NOTICE differs from packaged bytes")
    _sbom(_read(root / names["sbom"]), metadata, context, apk_hash)
    qualification = _read(root / names["proof"])
    _hosted(qualification, context, "APK qualification")
    _fields(qualification, {"schema": "emulebb.android.apk-qualification/1", "status": "passed",
        "apkSha256": apk_hash, "apkManifestSha256": manifest_hash, "signerSha256": context.apk_signer_sha256,
        "buildIdentity": identity, "versionCode": context.apk_version_code,
        "runtimeExecution": "executed", "deviceQualification": "emulator-only"}, "APK qualification")
    _same(qualification.get("source"), {"build": {"commit": context.apk_sources.build_sha},
        "harness": {"commit": context.apk_sources.harness_sha}}, "APK qualification sources")
    proofs = qualification.get("proofs")
    if not isinstance(proofs, list) or len(proofs) != 4 or any(not isinstance(item, dict) for item in proofs):
        raise RuntimeError("APK requires separate Android 14, 15, 16 and 17 guest proofs")
    actual_apis = [_object(item.get("guest"), "APK guest facts").get("sdk") for item in proofs]
    if any(type(api) is not int for api in actual_apis) or set(actual_apis) != APIS:
        raise RuntimeError("APK requires separate Android 14, 15, 16 and 17 guest proofs")
    for proof in proofs:
        validate_guest(proof, context=context, identity=identity, apk_sha256=apk_hash,
                       manifest_sha256=manifest_hash, api=proof["guest"]["sdk"], contract=contract)
    _checked_checkout(context.apk_harness_root, context.apk_sources.harness_sha, "APK Harness")
    _checked_reader(context)
    return {"asset": names["apk"], "sha256": apk_hash, "manifest": names["manifest"],
            "manifestSha256": manifest_hash, "qualification": names["proof"],
            "qualificationSha256": _sha256(root / names["proof"]), "androidApis": sorted(APIS),
            "signerSha256": context.apk_signer_sha256, "versionCode": context.apk_version_code, "runtimeExecution": "executed"}
