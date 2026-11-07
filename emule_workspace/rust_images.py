"""Verified OCI image builds shared by local WSL Docker and GitHub runners."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

from .git import git_output
from .linux_containers import LinuxDocker

BUILDKIT_IMAGE = "docker.io/moby/buildkit:v0.34.2@sha256:c5f8862f52499773af85bc07695b2379ef08e8002352b0df2c4864c91d78dabe"


def verified_debs(rust_root: Path, assets: Path, version: str, architectures: tuple[str, ...]) -> list[dict]:
    """Require exact, clean source and matching payload/SBOM bytes for every CPU."""
    from .release import _sha256

    if not architectures or len(set(architectures)) != len(architectures) or set(architectures) - {"amd64", "arm64"}:
        raise ValueError("Image builds require distinct AMD64/ARM64 architectures")
    if git_output(rust_root, "status", "--porcelain").strip():
        raise RuntimeError("Image builds require a clean current Rust source checkout")
    source_sha = git_output(rust_root, "rev-parse", "HEAD").strip()
    inputs = []
    for arch in architectures:
        name = f"emulebb-rust-v{version}-linux-{arch}.deb"
        asset = assets / name
        manifest = assets / (name + ".manifest.json")
        if not asset.is_file() or not manifest.is_file():
            raise RuntimeError(f"Image input or provenance is missing: {name}")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        sbom_name = data.get("sbom")
        if (data.get("asset") != name or data.get("sha256") != _sha256(asset)
                or data.get("source", {}).get("emulebbRust", {}).get("commit") != source_sha
                or data.get("sourceDirty", False) is not False):
            raise RuntimeError(f"Image input does not match the clean current Rust revision: {name}")
        if not isinstance(sbom_name, str) or Path(sbom_name).name != sbom_name:
            raise RuntimeError(f"Image SBOM path is invalid: {name}")
        sbom = assets / sbom_name
        if not sbom.is_file() or _sha256(sbom) != data.get("sbomSha256"):
            raise RuntimeError(f"Image SBOM bytes do not match provenance: {name}")
        inputs.append({"asset": name, "sha256": data["sha256"], "sbom": sbom_name,
                       "sbomSha256": data["sbomSha256"], "source": data["source"]})
    return inputs


def build_image(*, rust_root: Path, output_root: Path, assets_dir: Path, version: str, image: str,
                architectures: tuple[str, ...] = ("amd64", "arm64"), wsl_distro: str = "Ubuntu") -> Path:
    """Build an inventoried archive with an owned builder and reusable layer cache."""
    from .release import _is_release_version, _sha256

    rust_root, output_root, assets_dir = rust_root.resolve(), output_root.resolve(), assets_dir.resolve()
    if not _is_release_version(version):
        raise RuntimeError("Image version must be valid SemVer")
    if output_root.is_relative_to(rust_root) or not assets_dir.is_relative_to(output_root):
        raise RuntimeError("Image inputs and output must be below the external operator output root")
    inputs = verified_debs(rust_root, assets_dir, version, architectures)
    context = output_root / "packages/build/emulebb-rust-image"
    if not context.resolve().is_relative_to(output_root):
        raise RuntimeError("Image context must remain below the operator output root")
    source = rust_root / "packaging/docker"
    if not (source / "Dockerfile").is_file():
        raise RuntimeError(f"Image Dockerfile is missing: {source}")
    if context.exists():
        shutil.rmtree(context)
    shutil.copytree(source, context)
    (context / "dist").mkdir()
    for item in inputs:
        shutil.copy2(assets_dir / item["asset"], context / "dist" / item["asset"])
    suffix = "multiarch" if len(architectures) > 1 else architectures[0]
    archive = output_root / "artifacts" / f"emulebb-rust-v{version}-{suffix}.oci.tar"
    if not archive.resolve().is_relative_to(output_root):
        raise RuntimeError("Image archive must remain below the operator output root")
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.unlink(missing_ok=True)
    run_id = uuid4().hex
    reports = output_root / "reports/rust-image-build" / run_id
    reports.mkdir(parents=True)
    cache = output_root / "cache/rust-image-buildkit"
    cache.mkdir(parents=True, exist_ok=True)
    report = {"status": "running", "origin": "github-actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local-development",
              "image": image, "architectures": architectures, "buildkitImage": BUILDKIT_IMAGE, "inputs": inputs,
              "dockerfileSha256": _sha256(source / "Dockerfile")}
    path = reports / "inputs.json"
    log = reports / "build.log"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    with LinuxDocker(wsl_distro) as docker:
        docker.path(output_root)
        builder = f"emulebb-image-{run_id}"
        command = [*docker.prefix, "docker", "buildx", "build", "--builder", builder,
                   "--platform", ",".join(f"linux/{arch}" for arch in architectures),
                   "--file", docker.path(context) + "/Dockerfile", "--build-arg", f"VERSION={version}",
                   "--tag", image, "--progress", "plain", "--provenance", "mode=max",
                   "--output", f"type=oci,dest={docker.path(archive.parent)}/{archive.name}",
                   "--cache-to", f"type=local,dest={docker.path(cache)},mode=max"]
        if (cache / "index.json").is_file():
            command.extend(["--cache-from", f"type=local,src={docker.path(cache)}"])
        command.append(docker.path(context))
        print(f"Building {image}; diagnostics: {log}", flush=True)
        try:
            docker.run("buildx", "create", "--name", builder, "--driver", "docker-container",
                       "--driver-opt", f"image={BUILDKIT_IMAGE}", capture=True, timeout=60)
            with log.open("w", encoding="utf-8") as stream:
                subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=2700)
            if not archive.is_file():
                raise RuntimeError("Image builder returned without producing the candidate archive")
            report.update(status="passed", archive=archive.name, archiveSha256=_sha256(archive))
        except BaseException as error:
            report.update(status="failed", error=str(error))
            if log.is_file():
                print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]), flush=True)
            raise RuntimeError(f"Image build failed; diagnostics: {reports}") from error
        finally:
            # Stop only this invocation's worker, including after a WSL client timeout.
            cleanup_error = None
            try:
                cleanup = subprocess.run([*docker.prefix, "docker", "buildx", "rm", "--force", builder],
                                         capture_output=True, timeout=60)
                if cleanup.returncode:
                    cleanup_error = f"Docker returned {cleanup.returncode} removing {builder}"
            except (OSError, subprocess.TimeoutExpired) as error:
                cleanup_error = str(error)
            failed_cleanup_only = bool(cleanup_error) and report["status"] == "passed"
            if cleanup_error:
                report.update(status="failed", cleanupError=cleanup_error)
            path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            if failed_cleanup_only:
                raise RuntimeError(f"Could not remove owned image builder {builder}; diagnostics: {reports}")
    return archive
