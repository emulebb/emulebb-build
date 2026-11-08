"""Strict, source-bound release candidates shared by assembly and publication."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import json
from pathlib import Path
import re

from .image_security import TRIVY_IMAGE, _material_commits, _verify_spdx_architecture
from .linux_containers import validate_elf_audit
from .release import _is_release_version, _sha256, _sha256sums_text
from .rust_images import image_package_name, image_prefix, verified_smoke_platforms

SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
HASH = re.compile(r"[0-9a-f]{64}")
SOURCE_NAMES = frozenset({
    "emulebbRust", "emulebbBuild", "emulebbBuildTests", "emulebbTooling",
    "emulebbMiniupnp", "emulebbLibpcpnatpmp",
})
PROOF_SUFFIXES = (
    "-security.json", "-security-SHA256SUMS", "-smoke.json",
    "-scanner.txt", ".sbom.spdx.json", ".vulnerabilities.txt",
)
ArchiveIdentity = Callable[[Path], tuple[str, dict[str, dict[str, str]]]]


def _object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be an object")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise RuntimeError(f"{label} must be a positive integer")
    return value


def _counter(value: object, label: str) -> int:
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise RuntimeError(f"{label} must be a positive decimal string")
    return int(value)


def _fields(value: dict, expected: dict, label: str) -> None:
    if any(type(value.get(key)) is not type(item) or value[key] != item
           for key, item in expected.items()):
        raise RuntimeError(f"{label} does not match the required release identity or proof")


def _same(value: object, expected: object, label: str) -> None:
    # WHY: ordinary dictionary equality treats true, 1 and 1.0 as equal.
    # Comparing standard JSON encodings also preserves the evidence types.
    if json.dumps(value, sort_keys=True, separators=(",", ":")) != json.dumps(
        expected, sort_keys=True, separators=(",", ":")
    ):
        raise RuntimeError(f"{label} does not match the exact bound evidence")


def _read(path: Path) -> dict:
    try:
        if path.stat().st_size > 32 * 1024 * 1024:
            raise RuntimeError(f"Release JSON exceeds the evidence limit: {path.name}")
        return _object(json.loads(path.read_text(encoding="utf-8")), path.name)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cannot read release evidence: {path.name}") from error


@dataclass(frozen=True)
class CandidateContext:
    """Expected caller-owned identities, never identities inferred from payloads."""

    version: str
    channel: str
    source: dict[str, str]
    workflow: dict[str, str]

    def checked(self) -> CandidateContext:
        if not isinstance(self.version, str) or not _is_release_version(self.version):
            raise RuntimeError("Candidate requires a valid release version")
        if self.channel not in {"beta", "candidate", "nightly"}:
            raise RuntimeError("Candidate requires a supported release channel")
        if set(self.source) != SOURCE_NAMES or any(
            not isinstance(value, str) or SHA.fullmatch(value) is None
            for value in self.source.values()
        ):
            raise RuntimeError("Candidate requires every exact source and validator revision")
        if set(self.workflow) != {"repository", "runId", "runAttempt", "workflowSha"}:
            raise RuntimeError("Candidate requires the complete GitHub workflow identity")
        if (not isinstance(self.workflow["repository"], str)
                or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.workflow["repository"]) is None
                or not isinstance(self.workflow["workflowSha"], str)
                or SHA.fullmatch(self.workflow["workflowSha"]) is None):
            raise RuntimeError("Candidate requires an exact repository and workflow revision")
        _counter(self.workflow["runId"], "GitHub run ID")
        _counter(self.workflow["runAttempt"], "GitHub run attempt")
        return self

    @property
    def stem(self) -> str:
        return f"emulebb-rust-v{self.version}"

    @property
    def materials(self) -> dict[str, str]:
        return {name: value for name, value in self.source.items() if name != "emulebbBuildTests"}


def _hosted(data: dict, context: CandidateContext, label: str) -> int:
    _fields(data, {"origin": "github-actions"}, label)
    workflow = _object(data.get("workflow"), f"{label} workflow")
    if set(workflow) != set(context.workflow):
        raise RuntimeError(f"{label} has an incomplete workflow identity")
    _fields(workflow, {key: context.workflow[key] for key in
                      ("repository", "runId", "workflowSha")}, label)
    attempt = _counter(workflow.get("runAttempt"), f"{label} attempt")
    if attempt > _counter(context.workflow["runAttempt"], "assembly attempt"):
        raise RuntimeError(f"{label} was produced after the bound assembly attempt")
    return attempt


def package_layout(context: CandidateContext) -> dict[str, tuple[str, ...]]:
    """Return the exact native and static-musl payloads required for each CPU."""
    suffixes = {
        "windows-x64": ("windows-x64.zip",),
        "windows-arm64": ("windows-arm64.zip",),
        "linux-x64": ("linux-amd64.deb", "linux-x86_64.AppImage"),
        "linux-arm64": ("linux-arm64.deb", "linux-aarch64.AppImage"),
        "macos-x64": ("macos-x64.dmg",),
        "macos-arm64": ("macos-arm64.dmg",),
        "linux-x64-musl": ("linux-amd64-musl.tar.zst",),
        "linux-arm64-musl": ("linux-arm64-musl.tar.zst",),
    }
    return {platform: tuple(f"{context.stem}-{name}" for name in names)
            for platform, names in suffixes.items()}


def package_sbom(asset: str) -> str:
    """Use canonical producer names rather than names selected by a payload."""
    if asset.endswith(".zip"):
        return asset.removesuffix(".zip") + ".sbom.spdx.json"
    if asset.endswith(".tar.zst"):
        return asset.removesuffix(".tar.zst") + ".sbom.spdx.json"
    return asset + ".sbom.spdx.json"


def image_public_names(context: CandidateContext, variant: str) -> set[str]:
    prefix = image_prefix(context.version, variant)
    return {
        f"{prefix}-image-{arch}{suffix}"
        for arch in ("amd64", "arm64") for suffix in PROOF_SUFFIXES
    } | {
        f"{prefix}-multiarch.oci.tar.manifest.json",
        f"{prefix}-multiarch.oci.digest",
        f"{prefix}-image-security.json",
        f"{prefix}-image-security-SHA256SUMS",
    }


def public_names(context: CandidateContext) -> set[str]:
    names = {"SHA256SUMS", f"RELEASE-{context.version}-NOTES.md"}
    if context.channel != "nightly":
        names.add(f"RELEASE-{context.version}-CHANGELOG.md")
    for platform, assets in package_layout(context).items():
        names.add(f"{context.stem}-package-smoke-{platform}.json")
        for asset in assets:
            names.update((asset, asset + ".manifest.json", package_sbom(asset)))
    for variant in ("ubuntu", "alpine"):
        names.update(image_public_names(context, variant))
    return names


def candidate_path(root: Path, context: CandidateContext) -> Path:
    return root / f"{context.stem}-candidate-manifest.json"


def _inventory(root: Path, names: set[str]) -> None:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("Candidate requires a real release directory")
    entries = list(root.iterdir())
    if ({path.name for path in entries} != names
            or any(path.is_symlink() or not path.is_file() for path in entries)):
        raise RuntimeError("Candidate inventory must contain exactly the allowed flat public files")


def _record(path: Path) -> dict:
    return {"name": path.name, "sha256": _sha256(path), "sizeBytes": path.stat().st_size}


def _records(root: Path, names: set[str]) -> list[dict]:
    return [_record(root / name) for name in sorted(names)]


def _sums(root: Path, name: str, members: set[str]) -> None:
    expected = _sha256sums_text(root, tuple(root / item for item in sorted(members)))
    if (root / name).read_bytes() != expected.encode("ascii"):
        raise RuntimeError(f"Checksum inventory does not match its exact public files: {name}")


def _packages(root: Path, context: CandidateContext) -> dict[str, dict]:
    packages = {}
    for platform, assets in package_layout(context).items():
        for asset in assets:
            metadata = _read(root / (asset + ".manifest.json"))
            _hosted(metadata, context, asset)
            _fields(metadata, {
                "schema": "emulebb.rust.package/2", "package": "emulebb-rust",
                "version": context.version, "sourceDirty": False, "asset": asset,
                "sha256": _sha256(root / asset), "sbom": package_sbom(asset),
                "sbomSha256": _sha256(root / package_sbom(asset)),
            }, asset)
            _same(_material_commits(metadata.get("source")), context.materials, f"{asset} source")
            _fields(_read(root / package_sbom(asset)), {"spdxVersion": "SPDX-2.3"}, f"{asset} SBOM")
            if platform.endswith("-musl"):
                arch = "arm64" if platform == "linux-arm64-musl" else "amd64"
                _fields(metadata, {
                    "target": ("aarch64" if arch == "arm64" else "x86_64") + "-unknown-linux-musl",
                    "libc": "musl", "buildExecution": "native",
                    "libraryTestExecution": "native", "testCompilation": "workspace",
                    "testExecution": "workspace", "testProfile": "test",
                }, asset)
                audit = metadata.get("elfAudit")
                if not isinstance(audit, str):
                    raise RuntimeError(f"{asset} requires its ELF audit")
                validate_elf_audit(audit, libc="musl", architecture=arch)
            packages[asset] = metadata
    return packages


def _partial(value: object, payload_size: int, label: str) -> dict:
    snapshot = _object(value, label)
    _fields(snapshot, {"state": "paused"}, label)
    total = _positive(snapshot.get("partsTotal"), f"{label} part count")
    indices = snapshot.get("verifiedPartIndices")
    if (not isinstance(indices, list) or not 0 < len(indices) < total
            or any(type(index) is not int or not 0 <= index < total for index in indices)
            or len(set(indices)) != len(indices)):
        raise RuntimeError(f"{label} requires unique, in-bounds verified part indices")
    verified = _positive(snapshot.get("verifiedBytes"), f"{label} verified bytes")
    completed = _positive(snapshot.get("completedBytes"), f"{label} completed bytes")
    if not verified <= completed < payload_size:
        raise RuntimeError(f"{label} must prove verified data before completion")
    return snapshot


def _transfer(value: object, label: str) -> None:
    transfer = _object(value, label)
    _fields(transfer, {
        "transferAccepted": True, "sourceDiscovery": "ed2k-link-direct-source",
        "publicDiscoveryDisabled": True, "gracefulShutdowns": 3,
    }, label)
    payload = _object(transfer.get("payload"), f"{label} payload")
    size = _positive(payload.get("sizeBytes"), f"{label} payload size")
    if (not isinstance(payload.get("name"), str) or not payload["name"]
            or not isinstance(payload.get("sha256"), str)
            or HASH.fullmatch(payload["sha256"]) is None):
        raise RuntimeError(f"{label} has an invalid payload identity")
    shared = _object(transfer.get("sharedFile"), f"{label} shared file")
    _fields(shared, {"name": payload["name"], "sizeBytes": size}, label)
    shared_hash = shared.get("hash")
    if not isinstance(shared_hash, str) or re.fullmatch(r"[0-9a-f]{32}", shared_hash) is None:
        raise RuntimeError(f"{label} has an invalid eD2k identity")
    _fields(_object(transfer.get("delivery"), f"{label} delivery"), {
        "hash": shared_hash, "state": "completed", "completedBytes": size,
        "deliveredPathWithinIncoming": True, "deliveredSha256": payload["sha256"],
    }, label)
    restart = _object(transfer.get("partialRestart"), f"{label} restart")
    _fields(restart, {"sameProfile": True, "sameBinary": True, "resumeRequested": True,
                      "downloadLimitRestored": True}, label)
    before = _partial(restart.get("before"), size, f"{label} before restart")
    after = _partial(restart.get("after"), size, f"{label} after restart")
    if (before["partsTotal"] != after["partsTotal"]
            or not set(before["verifiedPartIndices"]).issubset(after["verifiedPartIndices"])
            or after["verifiedBytes"] < before["verifiedBytes"]):
        raise RuntimeError(f"{label} lost verified data across its same-profile restart")


def _package_smoke(root: Path, context: CandidateContext, packages: dict[str, dict]) -> list[dict]:
    evidence = []
    for platform, assets in package_layout(context).items():
        name = f"{context.stem}-package-smoke-{platform}.json"
        report = _read(root / name)
        attempt = _hosted(report, context, name)
        _fields(report, {
            "schema": "emulebb.rust.package-smoke/3", "status": "passed",
            "sourceSha": context.source["emulebbRust"], "harnessSha": context.source["emulebbBuildTests"],
            "runtimeExecution": "native", "platform": platform,
        }, name)
        results = report.get("results")
        if (not isinstance(results, list) or len(results) != len(assets)
                or any(not isinstance(item, dict) for item in results)
                or sorted(item.get("asset", "") for item in results) != sorted(assets)):
            raise RuntimeError(f"{name} must cover exactly its expected assets")
        for result in results:
            asset = result["asset"]
            metadata = packages[asset]
            if _counter(metadata["workflow"]["runAttempt"], asset) > attempt:
                raise RuntimeError(f"{name} precedes its tested package")
            _fields(result, {
                "status": "passed", "assetSha256": metadata["sha256"],
                "packageManifestSha256": _sha256(root / (asset + ".manifest.json")),
                "sbomSha256": metadata["sbomSha256"], "sourceSha": context.source["emulebbRust"],
                "appVersion": context.version, "appBuild": "release",
                "appPlatform": "arm64" if "arm64" in platform else "x64",
                "restStatus": True, "gracefulShutdown": True, "profile": "fresh-local-only",
            }, f"{name} {asset}")
            _positive(result.get("webuiAssets"), f"{name} WebUI asset count")
            if not _object(result.get("webuiPanelBackends"), f"{name} panel backends"):
                raise RuntimeError(f"{name} requires WebUI backend proof")
            if platform == "windows-x64":
                _fields(result, {"renderedWebui": True}, name)
            _transfer(result.get("localTransfer"), f"{name} {asset}")
        evidence.append({"platform": platform, "report": name, "sha256": _sha256(root / name),
                         "assets": sorted(assets), "workflow": report["workflow"]})
    return evidence


def _images(root: Path, archives: Path, context: CandidateContext,
            packages: dict[str, dict], archive_identity: ArchiveIdentity) -> dict:
    images = {}
    for variant in ("ubuntu", "alpine"):
        prefix = image_prefix(context.version, variant)
        archive_name = f"{prefix}-multiarch.oci.tar"
        archive = archives / archive_name
        if archive.is_symlink() or not archive.is_file():
            raise RuntimeError(f"Candidate requires its exact OCI archive: {archive_name}")
        archive_hash = _sha256(archive)
        digest, identities = archive_identity(archive)
        if (not isinstance(digest, str) or DIGEST.fullmatch(digest) is None
                or set(identities) != {"linux/amd64", "linux/arm64"}):
            raise RuntimeError(f"{variant} requires a verified two-CPU OCI identity")
        for identity in identities.values():
            if (not isinstance(identity, dict) or set(identity) != {"manifestDigest", "configDigest"}
                    or any(not isinstance(value, str) or DIGEST.fullmatch(value) is None
                           for value in identity.values())):
                raise RuntimeError(f"{variant} has an invalid OCI CPU identity")
        if (root / f"{prefix}-multiarch.oci.digest").read_bytes() != (digest + "\n").encode("ascii"):
            raise RuntimeError(f"{variant} has a conflicting OCI digest sidecar")
        metadata_path = root / (archive_name + ".manifest.json")
        metadata = _read(metadata_path)
        build_attempt = _hosted(metadata, context, f"{variant} image build")
        common = {
            "version": context.version, "variant": variant,
            "image": f"ghcr.io/emulebb/emulebb-rust:{context.version}-{variant}",
            "sourceSha": context.source["emulebbRust"], "sourceDirty": False,
            "builderSha": context.source["emulebbBuild"], "archiveSha256": archive_hash,
            "ociManifestDigest": digest,
        }
        _fields(metadata, {"schema": "emulebb.rust.image/2", **common,
                          "archive": archive_name, "architectures": ["amd64", "arm64"]}, variant)
        recipe = metadata.get("dockerfileSha256")
        if not isinstance(recipe, str) or HASH.fullmatch(recipe) is None:
            raise RuntimeError(f"{variant} requires its source recipe hash")
        inputs = metadata.get("inputs")
        expected_assets = {image_package_name(context.version, arch, variant) for arch in ("amd64", "arm64")}
        if (not isinstance(inputs, list) or len(inputs) != 2
                or any(not isinstance(item, dict) for item in inputs)
                or {item.get("asset") for item in inputs} != expected_assets):
            raise RuntimeError(f"{variant} requires the two exact package inputs")
        for item in inputs:
            if _hosted(item, context, variant) > build_attempt:
                raise RuntimeError(f"{variant} image predates a package input")
            _same(_material_commits(item.get("source")), context.materials, f"{variant} materials")
            native = packages[item["asset"]]
            _fields(item, {key: native[key] for key in ("sha256", "sbom", "sbomSha256")}, variant)
            if not isinstance(item.get("manifestSha256"), str) or HASH.fullmatch(item["manifestSha256"]) is None:
                raise RuntimeError(f"{variant} requires each input manifest identity")
            # A later attempt may regenerate only package metadata; shared
            # source, payload and SBOM bytes still identify the image input.
        summary_name = f"{prefix}-image-security.json"
        summary = _read(root / summary_name)
        assembly_attempt = _hosted(summary, context, summary_name)
        proof_common = {**common, "validatorSha": context.source["emulebbBuild"],
                        "harnessSha": context.source["emulebbBuildTests"]}
        _fields(summary, {"schema": "emulebb.rust.image-security-set/1", "status": "passed",
                          "archive": archive_name, **proof_common}, summary_name)
        _same(summary.get("inputs"), inputs, f"{variant} summary inputs")
        proofs = []
        for arch in ("amd64", "arm64"):
            cpu_prefix = f"{prefix}-image-{arch}"
            members = {cpu_prefix + suffix for suffix in PROOF_SUFFIXES}
            sums = cpu_prefix + "-security-SHA256SUMS"
            _sums(root, sums, members - {sums})
            report_path = root / (cpu_prefix + "-security.json")
            report = _read(report_path)
            proof_attempt = _hosted(report, context, report_path.name)
            if not build_attempt <= proof_attempt <= assembly_attempt:
                raise RuntimeError(f"{variant}/{arch} has impossible producer ordering")
            _fields(report, {"schema": "emulebb.rust.image-security/2", "status": "passed",
                             **proof_common, "dockerfileSha256": recipe,
                             "imageManifestSha256": _sha256(metadata_path),
                             "scannerImage": TRIVY_IMAGE}, report_path.name)
            _same(report.get("inputs"), inputs, f"{variant}/{arch} inputs")
            _same(report.get("producerWorkflow"), metadata["workflow"], f"{variant}/{arch} producer")
            platforms = report.get("platforms")
            identity = identities[f"linux/{arch}"]
            if not isinstance(platforms, list) or len(platforms) != 1:
                raise RuntimeError(f"{variant}/{arch} requires exactly one native result")
            cpu = _object(platforms[0], report_path.name)
            _fields(cpu, {"platform": f"linux/{arch}", "status": "passed", "runtimeExecution": "native",
                          "imageManifestDigest": identity["manifestDigest"],
                          "imageConfigDigest": identity["configDigest"]}, report_path.name)
            files = {"smoke": cpu_prefix + "-smoke.json", "sbom": cpu_prefix + ".sbom.spdx.json",
                     "vulnerabilities": cpu_prefix + ".vulnerabilities.txt"}
            for key, name in files.items():
                _fields(report if key == "smoke" else cpu,
                        {key: name, key + "Sha256": _sha256(root / name)}, report_path.name)
            smoke = verified_smoke_platforms(_read(root / files["smoke"]), manifest=metadata,
                                             architectures=(arch,))[f"linux/{arch}"]
            _fields(smoke, {"imageManifestDigest": identity["manifestDigest"],
                            "imageConfigDigest": identity["configDigest"]}, f"{variant}/{arch} smoke")
            _verify_spdx_architecture(root / files["sbom"], arch, variant)
            if "Version: 0.75.0" not in (root / (cpu_prefix + "-scanner.txt")).read_text(encoding="utf-8"):
                raise RuntimeError(f"{variant}/{arch} requires the pinned scanner version")
            proofs.append({"platform": f"linux/{arch}", "report": report_path.name,
                           "sha256": _sha256(report_path), "workflow": report["workflow"], **identity})
        _same(summary.get("nativeEvidence"), proofs, f"{variant} native evidence")
        names = image_public_names(context, variant)
        sums_name = f"{prefix}-image-security-SHA256SUMS"
        _same(summary.get("artifacts"), _records(root, names - {summary_name, sums_name}),
              f"{variant} public image inventory")
        _sums(root, sums_name, names - {sums_name})
        images[variant] = {"archive": archive_name, "archiveSha256": archive_hash,
                           "ociManifestDigest": digest, "platforms": identities}
    return images


def candidate_manifest(*, release_dir: Path, archives_dir: Path, context: CandidateContext,
                       archive_identity: ArchiveIdentity, existing_manifest: bool = False) -> dict:
    """Validate all public evidence and actual OCI bytes against caller identities."""
    context.checked()
    names = public_names(context)
    manifest = candidate_path(release_dir, context)
    _inventory(release_dir, names | ({manifest.name} if existing_manifest else set()))
    packages = _packages(release_dir, context)
    smoke = _package_smoke(release_dir, context, packages)
    images = _images(release_dir, archives_dir, context, packages, archive_identity)
    _sums(release_dir, "SHA256SUMS", names - {"SHA256SUMS"})
    return {"formatVersion": 3, "releaseVersion": context.version, "channel": context.channel,
            "source": context.source, "workflow": context.workflow,
            "packageSmoke": smoke, "images": images, "artifacts": _records(release_dir, names)}


def write_candidate(*, release_dir: Path, archives_dir: Path, context: CandidateContext,
                    archive_identity: ArchiveIdentity) -> Path:
    """Write a new candidate only after every required producer has passed."""
    data = candidate_manifest(release_dir=release_dir, archives_dir=archives_dir, context=context,
                              archive_identity=archive_identity)
    output = candidate_path(release_dir, context)
    with output.open("x", encoding="utf-8", newline="\n") as destination:
        destination.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return output


def _original_context(saved: dict, context: CandidateContext) -> CandidateContext:
    context.checked()
    _fields(saved, {"formatVersion": 3, "releaseVersion": context.version,
                    "channel": context.channel}, "Saved candidate")
    _same(saved.get("source"), context.source, "Saved candidate source")
    workflow = _object(saved.get("workflow"), "Saved candidate workflow")
    _hosted({"origin": "github-actions", "workflow": workflow}, context, "Saved candidate")
    return replace(context, workflow=workflow)


def verify_candidate(*, release_dir: Path, archives_dir: Path, context: CandidateContext,
                     archive_identity: ArchiveIdentity) -> dict:
    """Recheck bytes against the original assembly attempt, including partial reruns."""
    context.checked()
    _inventory(release_dir, public_names(context) | {candidate_path(release_dir, context).name})
    saved = _read(candidate_path(release_dir, context))
    original = _original_context(saved, context)
    expected = candidate_manifest(release_dir=release_dir, archives_dir=archives_dir, context=original,
                                  archive_identity=archive_identity, existing_manifest=True)
    _same(saved, expected, "Saved candidate")
    return saved


def verify_public_candidate(*, release_dir: Path, context: CandidateContext,
                            verified_candidate_sha256: str) -> dict:
    """Verify native publication without copying OCI archives from the image publisher.

    The expected candidate SHA must come from the image publisher's full
    verification, never from an adjacent downloaded file.
    """
    if not isinstance(verified_candidate_sha256, str) or HASH.fullmatch(verified_candidate_sha256) is None:
        raise RuntimeError("Public verification requires the image publisher's candidate SHA-256")
    context.checked()
    path = candidate_path(release_dir, context)
    _inventory(release_dir, public_names(context) | {path.name})
    if path.is_symlink() or not path.is_file() or _sha256(path) != verified_candidate_sha256:
        raise RuntimeError("Public candidate differs from the image publisher's verified candidate")
    saved = _read(path)
    original = _original_context(saved, context)
    names = public_names(original)
    _inventory(release_dir, names | {path.name})
    _same(saved.get("artifacts"), _records(release_dir, names), "Public candidate artifact bytes")
    _sums(release_dir, "SHA256SUMS", names - {"SHA256SUMS"})
    return saved
