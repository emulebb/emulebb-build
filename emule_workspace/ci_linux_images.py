"""Trusted-main CI image replication and prepared compiler publication.

Consumers activate only a verified produced digest, never a guessed reference.
The dedicated Build namespace needs explicit package access/public visibility;
this module never changes registry credentials or package settings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import time
import urllib.request
import urllib.error

from .git_environment import git_environment
from .ci_image_sources import NAMESPACE, UPSTREAM_ALPINE, UPSTREAM_RUNTIME, UPSTREAM_RUST
from .ci_linux_tools import tool_inputs

ROOT = Path(__file__).resolve().parents[1]
PREPARED = NAMESPACE + "/ubuntu-rust"
UBUNTU_BUILDER = "docker.io/library/ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852"
PYTHON_IMAGE = "docker.io/library/python:3.15.2-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9"
SOURCES = {"rust-gnu": UPSTREAM_RUST["gnu"], "rust-musl": UPSTREAM_RUST["musl"],
           "ubuntu-runtime": UPSTREAM_RUNTIME["gnu"], "alpine-runtime": UPSTREAM_ALPINE,
           "ubuntu-builder-base": UBUNTU_BUILDER, "python-toolchain": PYTHON_IMAGE,
           "node-toolchain": tool_inputs()["node"]["image"]}
RECIPE = "emule_workspace/release_assets/ci_linux/Dockerfile"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
ACCEPT = ", ".join(("application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.docker.distribution.manifest.v2+json"))


def execute(arguments: list[str], *, timeout: int = 600, raw: bool = False):
    """Use installed pinned registry/Docker clients without shell interpolation."""
    result = subprocess.check_output(arguments, timeout=timeout)
    return result if raw else result.decode("utf-8").strip()


def context() -> dict:
    """Permit publication code only from the actual trusted Build main revision."""
    sha = os.environ.get("GITHUB_SHA", "")
    if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REPOSITORY") != "emulebb/emulebb-build"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or os.environ.get("GITHUB_WORKFLOW_SHA") != sha):
        raise RuntimeError("CI images require the trusted main workflow and exact Build source")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(ROOT), *args], env=git_environment(), text=True).strip()
    if git("rev-parse", "HEAD") != sha or git("status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("CI image publisher must use clean exact workflow sources")
    if any(not re.fullmatch(r"[1-9][0-9]*", os.environ.get(name, "")) for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")):
        raise RuntimeError("CI image receipts require actual positive run identities")
    from .python_runtime import python_version
    if python_version() != "3.15.2":
        raise RuntimeError("Update the prepared Python image with the workspace interpreter pin")
    return {"repository": "emulebb/emulebb-build", "sourceSha": sha,
            "workflowSha": sha, "workflow": ".github/workflows/ci-linux-images.yml",
            "runId": os.environ["GITHUB_RUN_ID"], "runAttempt": os.environ["GITHUB_RUN_ATTEMPT"]}


def digest_bytes(raw: bytes, expected: str) -> dict:
    """Hash registry response bytes before interpreting descriptor JSON."""
    if not DIGEST.fullmatch(expected) or "sha256:" + hashlib.sha256(raw).hexdigest() != expected:
        raise RuntimeError("Registry object differs from its exact source digest")
    return json.loads(raw)


def cpu_descriptors(index: dict) -> dict[str, dict]:
    """Require exactly one runnable descriptor for each required CPU."""
    result = {}
    for item in index.get("manifests", []):
        cpu = item.get("platform", {})
        if cpu.get("os") == "linux" and cpu.get("architecture") in {"amd64", "arm64"}:
            arch = cpu["architecture"]
            if arch in result or not DIGEST.fullmatch(item.get("digest", "")):
                raise RuntimeError("Ambiguous or invalid upstream CPU descriptor")
            result[arch] = item
    if set(result) != {"amd64", "arm64"}:
        raise RuntimeError("CI image requires both amd64 and arm64")
    return result


def registry_object(reference: str, digest: str, *, client=execute) -> dict:
    return digest_bytes(client(["regctl", "manifest", "get", reference + "@" + digest,
                                "--format", "raw-body"], raw=True), digest)


def replicate_sources(*, client=execute, progress=None) -> list[dict]:
    """Copy the complete original index, including both CPUs, without relabelling."""
    records = []
    for role, source in SOURCES.items():
        repository, digest = source.rsplit("@", 1)
        upstream = cpu_descriptors(registry_object(repository, digest, client=client))
        target = NAMESPACE + "/" + role
        reference = target + ":upstream-" + digest.replace(":", "-")
        # WHY: --force-recursive repairs an incomplete transfer even if the top
        # index exists; no platform filter or label rewrite may alter upstream bytes.
        client(["regctl", "image", "copy", "--force-recursive", source, reference], timeout=1800)
        actual = client(["regctl", "image", "digest", reference])
        if actual != digest:
            raise RuntimeError("Replicated index differs from upstream")
        downstream = cpu_descriptors(registry_object(target, digest, client=client))
        if upstream != downstream:
            raise RuntimeError("Replicated CPU graph differs from upstream")
        for arch, descriptor in downstream.items():
            source_manifest = registry_object(repository, descriptor["digest"], client=client)
            copied_manifest = registry_object(target, descriptor["digest"], client=client)
            if copied_manifest != source_manifest:
                raise RuntimeError("Replicated CPU manifest differs from upstream")
            config = copied_manifest["config"]["digest"]
            raw = client(["regctl", "blob", "get", target, config], raw=True)
            value = digest_bytes(raw, config)
            if value.get("architecture") != arch or value.get("os") != "linux":
                raise RuntimeError("Replicated config has the wrong native CPU")
        records.append({"role": role, "upstream": source, "mirror": target + "@" + digest,
                        "indexDigest": digest, "platforms": downstream})
        if progress is not None:
            progress(records)
    return records


def public_metadata_bytes(request, *, deadline: float | None = None) -> bytes:
    """Bound unauthenticated public metadata by size and an absolute deadline."""
    deadline = time.monotonic() + 20 if deadline is None else deadline
    def remaining():
        value = min(20, deadline - time.monotonic())
        if value <= 0:
            raise TimeoutError("Public registry metadata deadline expired")
        return value
    with urllib.request.urlopen(request, timeout=remaining()) as response:
        chunks, size = [], 0
        while True:
            remaining()
            # read1 performs at most one underlying read; read(n) may otherwise
            # keep filling indefinitely when a server trickles small responses.
            chunk = response.read1(min(65536, 1024 * 1024 + 1 - size))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > 1024 * 1024:
                raise RuntimeError("Public registry metadata exceeds its bound")


def public_index(reference: str, expected: str, *, deadline: float | None = None) -> dict:
    """Prove anonymous GHCR readability; credentials remain transient in memory."""
    deadline = time.monotonic() + 60 if deadline is None else deadline
    repository = reference.removeprefix("ghcr.io/").split("@", 1)[0]
    url = "https://ghcr.io/token?service=ghcr.io&scope=repository:" + repository + ":pull"
    try:
        token = json.loads(public_metadata_bytes(url, deadline=deadline))["token"]
        request = urllib.request.Request("https://ghcr.io/v2/" + repository + "/manifests/" + expected,
                                         headers={"Authorization": "Bearer " + token, "Accept": ACCEPT})
        raw = public_metadata_bytes(request, deadline=deadline)
        if len(raw) > 1024 * 1024:
            raise RuntimeError("Public registry index exceeds its metadata bound")
        return digest_bytes(raw, expected)
    except urllib.error.HTTPError as error:
        if error.code not in {401, 403}:
            raise RuntimeError("Public CI registry metadata request failed") from error
        raise RuntimeError("Dedicated Build CI package is not anonymously readable; operator must review public visibility and Build Actions package access") from error
    except TimeoutError as error:
        raise RuntimeError("Public registry metadata deadline expired") from error
    except (OSError, KeyError, ValueError) as error:
        raise RuntimeError("Public CI registry metadata could not be verified") from error


def material_hashes() -> dict[str, str]:
    paths = (RECIPE, "emule_workspace/ubuntu_ci.py", "emule_workspace/ci_linux_image_probe.py",
             "emule_workspace/ci_linux_tools.py", "emule_workspace/python_runtime.py",
             "emule_workspace/release_assets/ci_linux/requirements.txt",
             "emule_workspace/release_assets/ci_linux/toolchain.json",
             "emule_workspace/release_assets/appimage/type2-runtime.json")
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in paths}


def prepared_bases() -> dict[str, str]:
    """Use the exact replicated source identities, never moving mirror tags."""
    return {"UBUNTU_IMAGE": NAMESPACE + "/ubuntu-builder-base@" + UBUNTU_BUILDER.rsplit("@", 1)[1],
            "RUST_IMAGE": NAMESPACE + "/rust-gnu@" + UPSTREAM_RUST["gnu"].rsplit("@", 1)[1],
            "PYTHON_IMAGE": NAMESPACE + "/python-toolchain@" + PYTHON_IMAGE.rsplit("@", 1)[1],
            "NODE_IMAGE": NAMESPACE + "/node-toolchain@" + tool_inputs()["node"]["image"].rsplit("@", 1)[1]}


def build_platform(arch: str, report_dir: Path, identity: dict, *, client=execute) -> dict:
    """Build and probe a native compiler image, publishing by actual digest only."""
    native = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine().lower())
    if platform.system() != "Linux" or arch != native:
        raise RuntimeError("Prepared compiler image requires the matching native Linux runner")
    if client(["docker", "version", "--format", "{{.Server.Arch}}"]).strip() != arch:
        raise RuntimeError("Prepared compiler image requires matching Docker CPU")
    report_dir.mkdir(parents=True, exist_ok=True)
    metadata = report_dir / ("buildx-metadata-" + arch + ".json")
    materials = material_hashes()
    bases = prepared_bases()
    command = ["docker", "buildx", "build", "--platform", "linux/" + arch,
               "--file", str(ROOT / RECIPE), "--metadata-file", str(metadata), "--provenance", "mode=max",
               "--label", "org.opencontainers.image.source=https://github.com/emulebb/emulebb-build",
               "--label", "org.opencontainers.image.revision=" + identity["sourceSha"],
               "--label", "io.emulebb.ci.recipe-sha256=" + materials[RECIPE],
               "--output", "type=image,name=" + PREPARED + ",push-by-digest=true,name-canonical=true,push=true"]
    for name, value in bases.items():
        command += ["--build-arg", name + "=" + value]
    client([*command, str(ROOT)], timeout=1800)
    digest = json.loads(metadata.read_text(encoding="utf-8"))["containerimage.digest"]
    if not DIGEST.fullmatch(digest):
        raise RuntimeError("Buildx did not produce an actual prepared-image digest")
    probe = json.loads(client(["docker", "run", "--rm", "--platform", "linux/" + arch,
                              "--network", "none", "--read-only", "--cap-drop", "ALL",
                              "--security-opt", "no-new-privileges", "--user", "10000:10000",
                              "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=1g,mode=1777",
                              "--env", "HOME=/tmp", "--env", "NPM_CONFIG_CACHE=/tmp/npm", PREPARED + "@" + digest,
                              "python3", "-B", "-m", "emule_workspace.ci_linux_image_probe"], timeout=120))
    if probe.get("status") != "passed" or probe.get("architecture") != arch:
        raise RuntimeError("Prepared compiler native probe did not pass")
    return {"schema": "emulebb.prepared-linux-platform/1", "status": "passed", **identity,
            "architecture": arch, "image": PREPARED, "digest": digest, "bases": bases,
            "materials": materials, "probe": probe}


def assemble_platforms(records: list[dict], identity: dict, *, client=execute) -> dict:
    """Promote no consumer pin: assemble and verify the actual two-CPU output."""
    if len(records) != 2 or {item.get("architecture") for item in records} != {"amd64", "arm64"}:
        raise RuntimeError("Prepared index requires exactly two native platform receipts")
    for record in records:
        probe = record.get("probe", {})
        attempt = record.get("runAttempt", "")
        current_attempt = identity.get("runAttempt", "")
        if (any(record.get(key) != value for key, value in identity.items() if key != "runAttempt")
                or not isinstance(attempt, str) or not re.fullmatch(r"[1-9][0-9]*", attempt)
                or not isinstance(current_attempt, str) or not re.fullmatch(r"[1-9][0-9]*", current_attempt)
                or int(attempt) > int(current_attempt)
                or record.get("materials") != material_hashes() or record.get("status") != "passed"
                or record.get("bases") != prepared_bases() or record.get("image") != PREPARED
                or not DIGEST.fullmatch(record.get("digest", ""))
                or probe.get("status") != "passed" or probe.get("architecture") != record["architecture"]
                or type(probe.get("uid")) is not int or probe["uid"] <= 0
                or probe.get("python") != "3.15.2" or not probe.get("rustc", "").startswith("rustc 1.98.1 ")):
            raise RuntimeError("Prepared platform receipt differs from this exact producer")
    tag = "run-" + identity["runId"] + "-attempt-" + identity["runAttempt"]
    if tag in client(["regctl", "tag", "ls", PREPARED]).splitlines():
        raise RuntimeError("Prepared producer tag already exists; parent inspection required")
    reference = PREPARED + ":" + tag
    client(["docker", "buildx", "imagetools", "create", "--tag", reference,
            *[PREPARED + "@" + item["digest"] for item in records]], timeout=300)
    digest = client(["regctl", "image", "digest", reference])
    index = registry_object(PREPARED, digest, client=client)
    descriptors = cpu_descriptors(index)
    runnable = {item.get("platform", {}).get("architecture") for item in index["manifests"]
                if item.get("platform", {}).get("os") == "linux"}
    if runnable != {"amd64", "arm64"}:
        raise RuntimeError("Prepared index has unexpected runnable CPUs")
    for record in records:
        built = registry_object(PREPARED, record["digest"], client=client)
        expected = [item["digest"] for item in built.get("manifests", [])
                    if item.get("platform", {}).get("architecture") == record["architecture"]]
        # Buildx provenance creates a one-CPU index plus non-runnable attestations.
        if expected != [descriptors[record["architecture"]]["digest"]]:
            raise RuntimeError("Prepared index differs from the native tested output")
        manifest = registry_object(PREPARED, expected[0], client=client)
        config_digest = manifest["config"]["digest"]
        config = digest_bytes(client(["regctl", "blob", "get", PREPARED, config_digest], raw=True), config_digest)
        labels = config.get("config", {}).get("Labels", {})
        if (config.get("os") != "linux" or config.get("architecture") != record["architecture"]
                or labels.get("org.opencontainers.image.revision") != identity["sourceSha"]
                or labels.get("org.opencontainers.image.source") != "https://github.com/emulebb/emulebb-build"
                or labels.get("io.emulebb.ci.recipe-sha256") != material_hashes()[RECIPE]):
            raise RuntimeError("Prepared native config differs from its recipe/source/CPU proof")
    return {"schema": "emulebb.prepared-linux-index/1", "status": "assembled-pending-public-read", **identity,
            "image": PREPARED + "@" + digest, "digest": digest, "platforms": descriptors,
            "nativeReceipts": records, "anonymousPublicReadVerified": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("mirror", "build-platform", "assemble"))
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--architecture", choices=("amd64", "arm64"))
    parser.add_argument("--records", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if not args.report.is_absolute() or args.report.resolve().is_relative_to(ROOT):
        raise RuntimeError("CI image evidence must be outside the source checkout")
    identity = context()
    if args.operation == "mirror":
        result = {"schema": "emulebb.ci-image-mirrors/1", **identity, "status": "copying", "images": []}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        def progress(records):
            result["images"] = records
            args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        progress([])
        try:
            result["images"] = replicate_sources(progress=progress)
        except Exception:
            result["status"] = "failed"
            progress(result["images"])
            raise
        result["status"] = "copied-pending-public-read"
    elif args.operation == "build-platform":
        result = build_platform(args.architecture, args.report.parent, identity)
    else:
        records = [json.loads(path.read_text(encoding="utf-8")) for path in args.records.glob("platform-*.json")]
        result = assemble_platforms(records, identity)
        # Retain the actual output even if a new package is initially private.
        # It is diagnostic only until the explicit anonymous checks all pass.
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            public_index(PREPARED, result["digest"])
            for role, source in SOURCES.items():
                public_index(NAMESPACE + "/" + role, source.rsplit("@", 1)[1])
        except RuntimeError as error:
            result["status"] = "blocked-public-access"
            result["diagnostic"] = str(error)
            args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            raise
        result["status"] = "passed"
        result["anonymousPublicReadVerified"] = True
        result["mirrorsAnonymousPublicReadVerified"] = True
    if context() != identity:
        raise RuntimeError("CI image producer identity changed during construction")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.github_output and "digest" in result:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write("digest=" + result["digest"] + "\n")


if __name__ == "__main__":
    main()
