"""Fresh OCI lifecycle, per-platform SBOM, and vulnerability release gates."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .linux_containers import LinuxDocker, run_owned_container
from .rust_images import smoke_image
from .git import git_output

TRIVY_IMAGE = "docker.io/aquasec/trivy:0.75.0@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa"


def verify_platform_export(path: Path, *, architecture: str, expected_config: str) -> None:
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


def validate_image(layout, *, archive: Path, image: str, architectures: tuple[str, ...],
                   security_dir: Path | None = None, wsl_distro: str = "Ubuntu") -> Path:
    """Run all image gates on fresh evidence; never accept a prior pass receipt."""
    from .release import _is_release_version, _sha256, _write_sha256sums

    output, archive = layout.output_root.resolve(), archive.resolve()
    if not archive.is_relative_to(output) or not archive.is_file():
        raise RuntimeError("Image validation requires an archive below the operator output root")
    manifest = json.loads(archive.with_name(archive.name + ".manifest.json").read_text(encoding="utf-8"))
    version = manifest.get("version")
    if not isinstance(version, str) or not _is_release_version(version):
        raise RuntimeError("Image validation requires an explicitly versioned archive manifest")
    root = security_dir.resolve() if security_dir else output / "reports/rust-image-security" / uuid4().hex
    if not root.is_relative_to(output) or (root.exists() and any(root.iterdir())):
        raise RuntimeError("Image security evidence requires a fresh empty directory below the output root")
    root.mkdir(parents=True, exist_ok=True)
    suffix = architectures[0] if len(architectures) == 1 else "multiarch"
    stem = f"emulebb-rust-v{version}-image"
    metadata = root / f"{stem}-{suffix}-security.json"
    report = {"schema": "emulebb.rust.image-security/1", "status": "running", "image": image,
              "version": version, "origin": manifest.get("origin"), "sourceSha": manifest.get("sourceSha"),
              "archiveSha256": manifest.get("archiveSha256"), "scannerImage": TRIVY_IMAGE, "platforms": [],
              "validatorSha": git_output(Path(__file__).resolve().parents[1], "rev-parse", "HEAD").strip(),
              "harnessSha": git_output(layout.tests_repo_root, "rev-parse", "HEAD").strip()}
    print(f"Image security evidence: {root}", flush=True)
    try:
        fresh_smoke = smoke_image(layout, archive=archive, image=image, architectures=architectures, wsl_distro=wsl_distro)
        smoke = json.loads(fresh_smoke.read_text(encoding="utf-8"))
        smoke_copy = root / f"{stem}-{suffix}-smoke.json"
        shutil.copy2(fresh_smoke, smoke_copy)
        report.update(smoke=smoke_copy.name, smokeSha256=_sha256(smoke_copy))
        identities = {item["platform"]: item for item in smoke["platforms"]}
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
                         "imageManifestDigest": identities[platform]["imageManifestDigest"]}
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
                    verify_platform_export(exported, architecture=arch, expected_config=identities[platform]["imageConfigDigest"])
                    entry["scannerInputSha256"] = _sha256(exported)
                    sbom = root / f"{stem}-{arch}.sbom.spdx.json"
                    # Trivy's Docker backend can export the host CPU even when
                    # --platform selects ARM64. A verified single-CPU archive
                    # removes that ambiguity and requires no Docker socket.
                    command = ["--rm", "--platform", f"linux/{host}", *mounts, TRIVY_IMAGE, "image", "--input", f"/scan/{arch}.tar",
                               "--platform", platform, "--no-progress"]
                    run_owned_container(docker, [*command, "--format", "spdx-json", "--output", f"/evidence/{sbom.name}"],
                                         root / f"{stem}-{arch}-sbom.log", timeout=900)
                    document = json.loads(sbom.read_text(encoding="utf-8"))
                    libc_arches = [parse_qs(urlsplit(ref.get("referenceLocator", "")).query).get("arch", [])
                                   for item in document.get("packages", []) if item.get("name") == "libc6"
                                   for ref in item.get("externalRefs", []) if ref.get("referenceType") == "purl"]
                    if document.get("spdxVersion") != "SPDX-2.3" or not any(arch in values for values in libc_arches):
                        raise RuntimeError(f"{platform} SBOM is missing the correct Ubuntu OS package architecture")
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
        files = tuple(sorted((path for path in root.iterdir() if path.is_file()), key=lambda path: path.name))
        _write_sha256sums(root / f"{stem}-{suffix}-security-SHA256SUMS", files)
    return metadata
