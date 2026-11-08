"""Fresh OCI lifecycle, per-platform SBOM, and vulnerability release gates."""
from __future__ import annotations

import json
import hashlib
import platform
import re
import runpy
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .linux_containers import LinuxDocker, run_owned_container
from .rust_images import archive_digest, smoke_image, verified_smoke_platforms, image_prefix, image_recipe, image_package_name
from .git import git_output

TRIVY_IMAGE = "docker.io/aquasec/trivy:0.75.0@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa"


def _verify_spdx_architecture(path: Path, architecture: str, variant: str = "ubuntu") -> None:
    """Require the selected libc, package ecosystem and CPU in the OS inventory."""
    image_recipe(variant)
    document = json.loads(path.read_text(encoding="utf-8"))
    libc, ecosystem, cpu = ("libc6", "deb", architecture) if variant == "ubuntu" else (
        "musl", "apk", {"amd64": "x86_64", "arm64": "aarch64"}[architecture])
    refs = [urlsplit(ref.get("referenceLocator", ""))
            for item in document.get("packages", []) if item.get("name") == libc
            for ref in item.get("externalRefs", []) if ref.get("referenceType") == "purl"]
    if document.get("spdxVersion") != "SPDX-2.3" or not any(
            ref.scheme == "pkg" and ref.path.startswith(ecosystem + "/")
            and cpu in parse_qs(ref.query).get("arch", []) for ref in refs):
        raise RuntimeError(f"SBOM is missing the correct {variant} OS package architecture")


def verify_platform_export(path: Path, *, architecture: str, expected_config: str,
                           expected_source: str, expected_version: str, expected_variant: str = "ubuntu") -> None:
    """Bind a single-platform Docker export to the verified OCI image config."""
    with tarfile.open(path, "r:*") as archive:
        members = archive.getmembers()
        def read(name: str) -> bytes:
            matches = [member for member in members if member.name == name]
            if len(matches) != 1 or not matches[0].isfile() or matches[0].size > 1024 * 1024:
                raise RuntimeError(f"Docker export requires one bounded regular metadata file: {name}")
            stream = archive.extractfile(matches[0])
            if stream is None:
                raise RuntimeError(f"Cannot read Docker export metadata: {name}")
            return stream.read()
        manifest = json.loads(read("manifest.json"))
        if not isinstance(manifest, list) or len(manifest) != 1 or not isinstance(manifest[0], dict):
            raise RuntimeError("Scanner input must export exactly one Docker image")
        config = read(manifest[0]["Config"])
        if "sha256:" + hashlib.sha256(config).hexdigest() != expected_config:
            raise RuntimeError("Docker export config does not match the OCI archive")
        identity = json.loads(config)
        if identity.get("os") != "linux" or identity.get("architecture") != architecture:
            raise RuntimeError("Docker export architecture does not match the requested scanner CPU")
        labels = identity.get("config", {}).get("Labels", {})
        if (labels.get("org.opencontainers.image.revision") != expected_source
                or labels.get("org.opencontainers.image.version") != expected_version
                or labels.get("io.emulebb.image.variant") != expected_variant):
            raise RuntimeError("Docker export revision, version or variant labels do not match the candidate")


def validate_image(layout, *, archive: Path, image: str, architectures: tuple[str, ...],
                   security_dir: Path | None = None, wsl_distro: str = "Ubuntu",
                   control_rust_root: Path | None = None) -> Path:
    """Run all image gates on fresh evidence; never accept a prior pass receipt."""
    from .release import (_is_release_version, _sha256, _write_sha256sums,
                          _emulebb_rust_artifact_origin, _assert_emulebb_rust_hosted_artifact)

    output, archive = layout.output_root.resolve(), archive.resolve()
    if not archive.is_relative_to(output) or not archive.is_file():
        raise RuntimeError("Image validation requires an archive below the operator output root")
    manifest_path = archive.with_name(archive.name + ".manifest.json")
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = json.loads(manifest_bytes)
    variant = manifest.get("variant")
    recipe = image_recipe(variant)
    origin = _emulebb_rust_artifact_origin()
    source_sha = git_output(layout.emulebb_rust_repo_root, "rev-parse", "HEAD").strip()
    builder_sha = git_output(Path(__file__).resolve().parents[1], "rev-parse", "HEAD").strip()
    if (manifest.get("schema") != "emulebb.rust.image/2" or manifest.get("sourceDirty") is not False
            or manifest.get("sourceSha") != source_sha or manifest.get("image") != image
            or manifest.get("archiveSha256") != _sha256(archive)
            or manifest.get("dockerfileSha256") != _sha256(layout.emulebb_rust_repo_root / "packaging/docker" / recipe)):
        raise RuntimeError("Image validation requires exact archive bytes and the current clean source recipe")
    if (not architectures or len(set(architectures)) != len(architectures)
            or set(architectures) - {"amd64", "arm64"}
            or set(manifest.get("architectures", [])) - {"amd64", "arm64"}
            or set(architectures) - set(manifest.get("architectures", []))):
        raise RuntimeError("Image validation requires distinct CPUs present in the archive manifest")
    if origin["origin"] == "github-actions":
        _assert_emulebb_rust_hosted_artifact(manifest)
        if manifest.get("builderSha") != builder_sha:
            raise RuntimeError("Hosted image validation requires the pinned current image builder")
    root_digest = archive_digest(layout.emulebb_rust_repo_root, archive, control_rust_root=control_rust_root)
    if root_digest != manifest.get("ociManifestDigest"):
        raise RuntimeError("Image OCI manifest digest does not match the verified archive")
    version = manifest.get("version")
    if not isinstance(version, str) or not _is_release_version(version):
        raise RuntimeError("Image validation requires an explicitly versioned archive manifest")
    inputs = manifest.get("inputs")
    expected_inputs = {image_package_name(version, arch, variant) for arch in manifest.get("architectures", [])}
    if (not isinstance(inputs, list) or not all(isinstance(item, dict) for item in inputs)
            or len(inputs) != len(expected_inputs) or {item.get("asset") for item in inputs} != expected_inputs
            or any(not isinstance(item.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", item[key]) is None
                   for item in inputs for key in ("sha256", "sbomSha256", "manifestSha256"))):
        raise RuntimeError("Image validation requires a complete hashed package input inventory for its variant")
    if origin["origin"] == "github-actions":
        for item in inputs:
            _assert_emulebb_rust_hosted_artifact(item)
    root = security_dir.resolve() if security_dir else output / "reports/rust-image-security" / uuid4().hex
    if not root.is_relative_to(output) or (root.exists() and any(root.iterdir())):
        raise RuntimeError("Image security evidence requires a fresh empty directory below the output root")
    root.mkdir(parents=True, exist_ok=True)
    suffix = architectures[0] if len(architectures) == 1 else "multiarch"
    stem = image_prefix(version, variant) + "-image"
    metadata = root / f"{stem}-{suffix}-security.json"
    report = {"schema": "emulebb.rust.image-security/2", "status": "running", "image": image,
              "version": version, "variant": variant, **origin, "producerWorkflow": manifest.get("workflow"),
              "sourceSha": source_sha, "sourceDirty": False, "inputs": inputs,
              "ociManifestDigest": root_digest, "imageManifestSha256": manifest_sha256,
              "dockerfileSha256": manifest["dockerfileSha256"], "builderSha": manifest["builderSha"],
              "archiveSha256": manifest.get("archiveSha256"), "scannerImage": TRIVY_IMAGE, "platforms": [],
              "validatorSha": builder_sha,
              "harnessSha": git_output(layout.tests_repo_root, "rev-parse", "HEAD").strip()}
    print(f"Image security evidence: {root}", flush=True)
    try:
        fresh_smoke = smoke_image(layout, archive=archive, image=image, architectures=architectures, wsl_distro=wsl_distro)
        smoke_copy = root / f"{stem}-{suffix}-smoke.json"
        shutil.copy2(fresh_smoke, smoke_copy)
        smoke = json.loads(smoke_copy.read_text(encoding="utf-8"))
        identities = verified_smoke_platforms(smoke, manifest=manifest, architectures=architectures)
        report.update(smoke=smoke_copy.name, smokeSha256=_sha256(smoke_copy))
        scratch_root = output / "tmp/image-scans"
        if not scratch_root.resolve().is_relative_to(output):
            raise RuntimeError("Image scanner scratch must remain below the output root")
        scratch_root.mkdir(parents=True, exist_ok=True)
        failures = []
        with tempfile.TemporaryDirectory(prefix="platform-", dir=scratch_root) as temporary, LinuxDocker(wsl_distro) as docker:
            scratch = Path(temporary)
            if not scratch.resolve().is_relative_to(output):
                raise RuntimeError("Image scanner scratch escaped the output root")
            docker.path(output)
            host = docker.run("version", "--format", "{{.Server.Arch}}", capture=True, timeout=30).stdout.strip()
            uid = subprocess.check_output([*docker.prefix, "id", "-u"], text=True).strip()
            gid = subprocess.check_output([*docker.prefix, "id", "-g"], text=True).strip()
            if not uid.isdigit() or not gid.isdigit():
                raise RuntimeError("Cannot identify the Docker client's export-file owner")
            cache = output / "cache/trivy" / f"{uid}-{gid}"
            cache.mkdir(parents=True, exist_ok=True)
            report["scannerUser"] = f"{uid}:{gid}"
            mounts = ["--mount", f"type=bind,source={docker.path(scratch)},target=/scan,readonly",
                      "--mount", f"type=bind,source={docker.path(cache)},target=/cache",
                      "--mount", f"type=bind,source={docker.path(root)},target=/evidence",
                      "--user", f"{uid}:{gid}", "--env", "HOME=/tmp", "--env", "TRIVY_CACHE_DIR=/cache",
                      "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
            scanner = docker.run("run", "--rm", "--platform", f"linux/{host}", *mounts,
                                 TRIVY_IMAGE, "--version", capture=True, timeout=180).stdout
            if "Version: 0.75.0" not in scanner:
                raise RuntimeError("Pinned Trivy image did not report the required version")
            (root / f"{stem}-{suffix}-scanner.txt").write_text(scanner, encoding="utf-8")
            for arch in architectures:
                platform = f"linux/{arch}"
                entry = {"platform": platform, "status": "failed", "runtimeExecution": "native" if host == arch else "qemu",
                         "imageManifestDigest": identities[platform]["imageManifestDigest"],
                         "imageConfigDigest": identities[platform]["imageConfigDigest"]}
                report["platforms"].append(entry)
                try:
                    # Recheck the selected tag immediately before scanning. The
                    # expected digest comes only from this invocation's smoke.
                    descriptor = json.loads(docker.run("image", "inspect", "--platform", platform, "--format",
                                                       "{{json .Descriptor}}", image, capture=True, timeout=30).stdout)
                    if descriptor.get("digest") != entry["imageManifestDigest"]:
                        raise RuntimeError(f"Loaded {platform} image changed after OCI lifecycle validation")
                    exported = scratch / f"{arch}.tar"
                    docker.run("image", "save", "--platform", platform, "--output", docker.path(scratch) + f"/{arch}.tar",
                               image, capture=True, timeout=180)
                    verify_platform_export(exported, architecture=arch, expected_config=identities[platform]["imageConfigDigest"],
                                           expected_source=source_sha, expected_version=version, expected_variant=variant)
                    entry["scannerInputSha256"] = _sha256(exported)
                    sbom = root / f"{stem}-{arch}.sbom.spdx.json"
                    # Trivy's Docker backend can export the host CPU even when
                    # --platform selects ARM64. A verified single-CPU archive
                    # removes that ambiguity and requires no Docker socket.
                    command = ["--rm", "--platform", f"linux/{host}", *mounts, TRIVY_IMAGE, "image", "--input", f"/scan/{arch}.tar",
                               "--platform", platform, "--no-progress"]
                    run_owned_container(docker, [*command, "--format", "spdx-json", "--output", f"/evidence/{sbom.name}"],
                                         root / f"{stem}-{arch}-sbom.log", timeout=900)
                    _verify_spdx_architecture(sbom, arch, variant)
                    vulnerabilities = root / f"{stem}-{arch}.vulnerabilities.txt"
                    run_owned_container(docker, [*command, "--scanners", "vuln", "--ignore-unfixed",
                                                 "--severity", "HIGH,CRITICAL", "--exit-code", "1"],
                                         vulnerabilities, timeout=900)
                    entry.update(status="passed", sbom=sbom.name, sbomSha256=_sha256(sbom),
                                 vulnerabilities=vulnerabilities.name, vulnerabilitiesSha256=_sha256(vulnerabilities))
                except Exception as error:
                    entry["error"] = str(error)
                    failures.append(platform)
            if failures:
                raise RuntimeError("Image security gates failed for: " + ", ".join(failures))
        if _sha256(archive) != manifest["archiveSha256"] or _sha256(manifest_path) != manifest_sha256:
            raise RuntimeError("Image archive or its provenance changed during validation")
        report["status"] = "passed"
    except BaseException as error:
        report.update(status="failed", error=str(error))
        if isinstance(error, subprocess.CalledProcessError):
            detail = (error.stderr or error.stdout or "")[-4000:]
            report["diagnostics"] = detail
            print(detail, flush=True)
        raise RuntimeError(f"Image validation failed; inspect {root}") from error
    finally:
        metadata.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        # Diagnostic logs have a separate artifact; public inventories cover only
        # the evidence that the candidate actually retains.
        files = tuple(sorted((path for path in root.iterdir() if path.is_file() and path.suffix in {".json", ".txt"}), key=lambda path: path.name))
        _write_sha256sums(root / f"{stem}-{suffix}-security-SHA256SUMS", files)
    return metadata


def validate_image_ci(layout, *, archive: Path, image: str, architecture: str,
                      security_dir: Path, control_rust_root: Path) -> Path:
    """Require real native Linux CPU proof before running hosted image gates."""
    from .release import _emulebb_rust_artifact_origin

    native = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine().lower())
    if (_emulebb_rust_artifact_origin()["origin"] != "github-actions" or platform.system() != "Linux"
            or native is None or architecture != native):
        raise RuntimeError("CI image validation requires GitHub Actions on the requested native Linux CPU")
    with LinuxDocker() as docker:
        host = docker.run("version", "--format", "{{.Server.Arch}}", capture=True, timeout=30).stdout.strip()
    if host != native:
        raise RuntimeError("CI image validation requires Docker and the native runner CPU to match")
    return validate_image(layout, archive=archive, image=image, architectures=(architecture,),
                          security_dir=security_dir, control_rust_root=control_rust_root)


def _material_commits(source: dict) -> dict[str, str]:
    names = {"emulebbRust", "emulebbBuild", "emulebbTooling", "emulebbMiniupnp", "emulebbLibpcpnatpmp"}
    if (not isinstance(source, dict) or set(source) != names
            or any(not isinstance(item, dict) or not isinstance(item.get("commit"), str)
                   or re.fullmatch(r"[0-9a-f]{40}", item["commit"]) is None for item in source.values())):
        raise RuntimeError("Image inputs require complete source and support revisions")
    return {name: item["commit"] for name, item in source.items()}


def assemble_image_security_ci(*, output_root: Path, archive: Path, evidence_dir: Path, release_dir: Path,
                               version: str, source_sha: str, harness_root: Path, harness_sha: str,
                               control_rust_root: Path) -> Path:
    """Combine two native proofs only when they bind the exact assembled inputs."""
    from .release import (_emulebb_rust_artifact_origin, _assert_emulebb_rust_hosted_artifact,
                          _is_release_version, _sha256, _sha256sums_text, _write_sha256sums)

    origin = _emulebb_rust_artifact_origin()
    if (origin["origin"] != "github-actions" or not _is_release_version(version)
            or any(re.fullmatch(r"[0-9a-f]{40}", value) is None for value in (source_sha, harness_sha))):
        raise RuntimeError("Image evidence assembly requires GitHub Actions and exact version/source/harness identities")
    output_root, archive = output_root.resolve(), archive.resolve()
    evidence_dir, release_dir = evidence_dir.resolve(), release_dir.resolve()
    if (any(not path.is_relative_to(output_root) for path in (archive, evidence_dir, release_dir))
            or evidence_dir.is_relative_to(release_dir) or archive.is_relative_to(release_dir)):
        raise RuntimeError("Image proof inputs and release outputs require separate directories below the output root")
    if not release_dir.is_dir():
        raise RuntimeError("Image evidence assembly requires the native release assets first")
    image_manifest = archive.with_name(archive.name + ".manifest.json")
    manifest_bytes = image_manifest.read_bytes()
    metadata = json.loads(manifest_bytes)
    variant = metadata.get("variant")
    stem = image_prefix(version, variant)
    image = f"ghcr.io/emulebb/emulebb-rust:{version}-{variant}"
    if archive.name != f"{stem}-multiarch.oci.tar":
        raise RuntimeError("Image evidence assembly requires the canonical multiarch archive")
    _assert_emulebb_rust_hosted_artifact(metadata)
    builder_root = Path(__file__).resolve().parents[1]
    if git_output(builder_root, "status", "--porcelain").strip():
        raise RuntimeError("Image proof assembly requires clean builder sources")
    builder_sha = git_output(builder_root, "rev-parse", "HEAD").strip()
    archive_sha256, manifest_sha256 = _sha256(archive), hashlib.sha256(manifest_bytes).hexdigest()
    root_digest = archive_digest(control_rust_root, archive, control_rust_root=control_rust_root)
    if (metadata.get("schema") != "emulebb.rust.image/2" or metadata.get("sourceDirty") is not False
            or metadata.get("sourceSha") != source_sha or metadata.get("version") != version or metadata.get("image") != image
            or metadata.get("architectures") != ["amd64", "arm64"] or metadata.get("builderSha") != builder_sha
            or metadata.get("archive") != archive.name
            or metadata.get("archiveSha256") != archive_sha256 or metadata.get("ociManifestDigest") != root_digest):
        raise RuntimeError("Image build identity does not match the exact hosted candidate")
    if (git_output(harness_root, "status", "--porcelain").strip()
            or git_output(harness_root, "rev-parse", "HEAD").strip() != harness_sha):
        raise RuntimeError("Image proof assembly requires the clean pinned harness checkout")
    identities = runpy.run_path(str(harness_root / "scripts/smoke-rust-container.py"))["oci_platform_identities"](archive)
    if set(identities) != {"linux/amd64", "linux/arm64"}:
        raise RuntimeError("Candidate archive must contain exactly the two required image CPUs")
    inputs = metadata.get("inputs")
    if (not isinstance(inputs, list) or len(inputs) != 2 or not all(isinstance(item, dict) for item in inputs)
            or {item.get("asset") for item in inputs} != {image_package_name(version, arch, variant) for arch in ("amd64", "arm64")}):
        raise RuntimeError("Image build must identify both package inputs for its variant")
    for item in inputs:
        _assert_emulebb_rust_hosted_artifact(item)
        if int(item["workflow"]["runAttempt"]) > int(metadata["workflow"]["runAttempt"]):
            raise RuntimeError("Image package producer cannot follow the image build attempt")
        material = _material_commits(item.get("source"))
        if material["emulebbRust"] != source_sha or material["emulebbBuild"] != builder_sha:
            raise RuntimeError("Image inputs use different source or builder revisions")
        asset = release_dir / item["asset"]
        native = json.loads(asset.with_name(asset.name + ".manifest.json").read_text(encoding="utf-8"))
        _assert_emulebb_rust_hosted_artifact(native)
        sbom_name = item.get("sbom")
        if not isinstance(sbom_name, str) or Path(sbom_name).name != sbom_name:
            raise RuntimeError("Image SBOM must be a direct release asset")
        sbom = release_dir / sbom_name
        # WHY: rerunning a package job can change its manifest metadata while
        # reproducing identical payloads. Image proof binds payload/SBOM bytes
        # and material revisions, rather than incidental manifest timestamps.
        if (native.get("schema") != "emulebb.rust.package/2" or native.get("sourceDirty") is not False
                or native.get("asset") != asset.name or native.get("version") != version
                or _material_commits(native.get("source")) != material
                or native.get("sha256") != item.get("sha256") or _sha256(asset) != item.get("sha256")
                or native.get("sbom") != sbom_name or native.get("sbomSha256") != item.get("sbomSha256")
                or _sha256(sbom) != item.get("sbomSha256")):
            raise RuntimeError("Image inputs do not match the currently assembled package and SBOM bytes")

    proof_names = {f"{stem}-image-{arch}{suffix}" for arch in ("amd64", "arm64")
                   for suffix in ("-security.json", "-security-SHA256SUMS", "-smoke.json", "-scanner.txt",
                                  ".sbom.spdx.json", ".vulnerabilities.txt")}
    entries = list(evidence_dir.iterdir())
    if ({path.name for path in entries} != proof_names
            or any(path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024 for path in entries)):
        raise RuntimeError("Image proof inventory must contain only complete public evidence for both CPUs")
    proofs = []
    for arch in ("amd64", "arm64"):
        prefix = f"{stem}-image-{arch}"
        report_path = evidence_dir / f"{prefix}-security.json"
        checksums = evidence_dir / f"{prefix}-security-SHA256SUMS"
        members = tuple(sorted((path for path in entries if path.name.startswith(prefix)
                                 and path != checksums), key=lambda path: path.name))
        if checksums.read_bytes() != _sha256sums_text(evidence_dir, members).encode("ascii"):
            raise RuntimeError("Native image proof checksum inventory does not match its exact public files")
        data = json.loads(report_path.read_text(encoding="utf-8"))
        _assert_emulebb_rust_hosted_artifact(data)
        expected = {"schema": "emulebb.rust.image-security/2", "status": "passed", "image": image, "version": version, "variant": variant,
                    "sourceSha": source_sha, "sourceDirty": False, "archiveSha256": archive_sha256,
                    "ociManifestDigest": root_digest, "imageManifestSha256": manifest_sha256,
                    "dockerfileSha256": metadata["dockerfileSha256"], "builderSha": builder_sha,
                    "validatorSha": builder_sha, "harnessSha": harness_sha, "scannerImage": TRIVY_IMAGE}
        if (any(type(data.get(key)) is not type(value) or data[key] != value for key, value in expected.items())
                or data.get("inputs") != inputs or data.get("producerWorkflow") != metadata["workflow"]
                or int(metadata["workflow"]["runAttempt"]) > int(data["workflow"]["runAttempt"])):
            raise RuntimeError("Native image proof identity does not match the archive, source or pinned validators")
        platforms = data.get("platforms")
        if (not isinstance(platforms, list) or len(platforms) != 1 or not isinstance(platforms[0], dict)
                or platforms[0].get("platform") != f"linux/{arch}" or platforms[0].get("status") != "passed"
                or platforms[0].get("runtimeExecution") != "native"):
            raise RuntimeError("Image release proof requires one passed native result for each CPU")
        cpu = platforms[0]
        identity = identities[f"linux/{arch}"]
        if (cpu.get("imageManifestDigest") != identity["manifestDigest"]
                or cpu.get("imageConfigDigest") != identity["configDigest"]):
            raise RuntimeError("Native image proof platform identity does not match the OCI archive")
        expected_files = {"smoke": f"{prefix}-smoke.json", "sbom": f"{prefix}.sbom.spdx.json",
                          "vulnerabilities": f"{prefix}.vulnerabilities.txt"}
        for key, name in expected_files.items():
            owner = data if key == "smoke" else cpu
            if owner.get(key) != name or owner.get(key + "Sha256") != _sha256(evidence_dir / name):
                raise RuntimeError("Native image proof references different smoke, SBOM or CVE bytes")
        smoke = json.loads((evidence_dir / expected_files["smoke"]).read_text(encoding="utf-8"))
        smoke_cpu = verified_smoke_platforms(smoke, manifest=metadata, architectures=(arch,))[f"linux/{arch}"]
        if (smoke_cpu["imageManifestDigest"] != identity["manifestDigest"]
                or smoke_cpu["imageConfigDigest"] != identity["configDigest"]):
            raise RuntimeError("Native image smoke identity does not match the archive")
        _verify_spdx_architecture(evidence_dir / expected_files["sbom"], arch, variant)
        if "Version: 0.75.0" not in (evidence_dir / f"{prefix}-scanner.txt").read_text(encoding="utf-8"):
            raise RuntimeError("Native image proof requires the pinned scanner version")
        proofs.append({"platform": f"linux/{arch}", "report": report_path.name, "sha256": _sha256(report_path),
                       "workflow": data["workflow"], **identity})

    retained = {path.name: path for path in (*entries, image_manifest)}
    digest_name = f"{stem}-multiarch.oci.digest"
    summary = release_dir / f"{stem}-image-security.json"
    sums = release_dir / f"{stem}-image-security-SHA256SUMS"
    if any((release_dir / name).exists() for name in (*retained, digest_name, summary.name, sums.name)):
        raise RuntimeError("Image proof assembly refuses to overwrite retained candidate evidence")
    records = [{"name": name, "sha256": _sha256(path), "sizeBytes": path.stat().st_size}
               for name, path in sorted(retained.items())]
    for record in records:
        destination = release_dir / record["name"]
        shutil.copy2(retained[record["name"]], destination)
        if _sha256(destination) != record["sha256"]:
            raise RuntimeError("Image evidence changed while being retained")
    digest = release_dir / digest_name
    digest.write_text(root_digest + "\n", encoding="ascii")
    records.append({"name": digest_name, "sha256": _sha256(digest), "sizeBytes": digest.stat().st_size})
    if _sha256(archive) != archive_sha256 or _sha256(image_manifest) != manifest_sha256:
        raise RuntimeError("Image build bytes changed during evidence assembly")
    result = {"schema": "emulebb.rust.image-security-set/1", "status": "passed", **origin,
              "image": image, "version": version, "variant": variant, "sourceSha": source_sha, "sourceDirty": False,
              "archive": archive.name, "archiveSha256": archive_sha256, "ociManifestDigest": root_digest,
              "builderSha": builder_sha, "validatorSha": builder_sha, "harnessSha": harness_sha,
              "inputs": inputs, "nativeEvidence": proofs, "artifacts": sorted(records, key=lambda item: item["name"])}
    summary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    public = tuple(sorted((release_dir / name for name in (*retained, digest_name, summary.name)), key=lambda path: path.name))
    _write_sha256sums(sums, public)
    return summary
