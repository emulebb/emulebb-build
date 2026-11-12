"""Execute the existing native package pipeline in an attested prepared image."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tomllib
import time
import urllib.request
from uuid import uuid4

from .ci_linux_images import (
    DIGEST, PREPARED, RECIPE, ROOT, material_hashes, public_index, public_blob, cpu_descriptors, public_metadata_bytes,
)
from .ci_linux_tools import tool_inputs, RUNTIME
from .config import resolve_required_workspace_roots, resolve_required_cargo_target_dir
from .git_environment import git_environment
from .linux_containers import LinuxDocker, _builder_user, run_owned_container

SIGNER = "https://github.com/emulebb/emulebb-build/.github/workflows/ci-linux-images.yml@refs/heads/main"
BUNDLED_TOOLS = Path("/opt/emulebb/tools")
FORWARD = ("EMULEBB_WORKSPACE_ROOT", "EMULEBB_WORKSPACE_OUTPUT_ROOT", "CARGO_TARGET_DIR",
           "EMULEBB_RUST_REPO", "EMULEBB_TOOLING_REPO", "MINIUPNP_ROOT", "PCPNATPMP_ROOT",
           "EMULEBB_RELEASE_VERSION", "EMULEBB_RELEASE_CHANNEL", "EMULEBB_SOURCE_REVISION",
           "GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_WORKFLOW_SHA",
           "GITHUB_SHA", "GITHUB_WORKSPACE")


def git(repo: Path, *args: str, timeout: float = 30) -> str:
    return subprocess.check_output(["git", "--no-optional-locks", "-C", str(repo), *args],
                                   env=git_environment(), text=True, timeout=timeout).strip()


def snapshot(roots: dict[str, Path], *, deadline: float | None = None) -> dict[str, str]:
    """Reject dirty inputs, including another selected checkout changing mid-build."""
    result = {}
    for name, root in roots.items():
        def query(*args):
            available = 30 if deadline is None else min(30, deadline - time.monotonic())
            if available <= 0:
                raise RuntimeError("Prepared launcher overall deadline expired")
            return git(root, *args, timeout=available)
        if (not root.is_dir() or not (root / ".git").is_dir()
                or Path(query("rev-parse", "--show-toplevel")).resolve() != root
                or query("status", "--porcelain", "--untracked-files=all")):
            raise RuntimeError("Prepared native CI requires clean self-contained selected Git checkouts")
        result[name] = query("rev-parse", "HEAD")
    return result


def verify_attestation(image: str, builder_sha: str, *, timeout: int = 180) -> dict:
    """Require cryptographic Build producer identity before pulling/executing code."""
    deadline = time.monotonic() + timeout
    request = urllib.request.Request("https://api.github.com/repos/emulebb/emulebb-build", headers={
        "Accept": "application/vnd.github+json", "User-Agent": "emulebb-ci-image-verifier"})
    repository = json.loads(public_metadata_bytes(request, deadline=min(deadline, time.monotonic() + 20)))
    owner = repository.get("owner", {})
    # WHY: OCI-bundle mode skips GitHub's current-name lookup. Binding the
    # current immutable repository/owner IDs to the verified certificate rejects
    # a renamed or reassigned authority, without cross-repository token grants.
    if (repository.get("full_name") != "emulebb/emulebb-build" or repository.get("private") is not False
            or str(repository.get("id")) != "499553807" or str(owner.get("id")) != "281581430"
            or owner.get("html_url") != "https://github.com/emulebb"):
        raise RuntimeError("Public Build repository identity differs from the reviewed immutable authority")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("Prepared image attestation deadline expired")
    command = ["gh", "attestation", "verify", "oci://" + image, "--bundle-from-oci", "--repo", "emulebb/emulebb-build",
               "--signer-workflow", "emulebb/emulebb-build/.github/workflows/ci-linux-images.yml",
               "--signer-digest", builder_sha, "--source-digest", builder_sha, "--source-ref", "refs/heads/main",
               "--deny-self-hosted-runners", "--format", "json"]
    results = json.loads(subprocess.check_output(command, text=True, timeout=remaining))
    digest = image.rsplit("@sha256:", 1)[1]
    for result in results:
        verified = result["verificationResult"]
        certificate = verified["signature"]["certificate"]
        statement = verified["statement"]
        if (certificate.get("subjectAlternativeName") == SIGNER
                and certificate.get("sourceRepositoryDigest") == builder_sha
                and certificate.get("buildSignerDigest") == builder_sha
                and certificate.get("sourceRepositoryURI") == "https://github.com/emulebb/emulebb-build"
                and certificate.get("sourceRepositoryOwnerURI") == "https://github.com/emulebb"
                and str(certificate.get("sourceRepositoryIdentifier")) == str(repository["id"])
                and str(certificate.get("sourceRepositoryOwnerIdentifier")) == str(owner["id"])
                and certificate.get("runnerEnvironment") == "github-hosted"
                and certificate.get("sourceRepositoryRef") == "refs/heads/main"
                and statement.get("predicateType") == "https://slsa.dev/provenance/v1"
                and any(item.get("name") == PREPARED and item.get("digest", {}).get("sha256") == digest
                        for item in statement.get("subject", []))):
            invocation = certificate.get("runInvocationURI", "")
            if not re.fullmatch(r"https://github.com/emulebb/emulebb-build/actions/runs/[1-9][0-9]*/attempts/[1-9][0-9]*", invocation):
                raise RuntimeError("Prepared image lacks its actual producer run/attempt")
            return {"cryptographicallyVerified": True, "sourceSha": builder_sha, "signer": SIGNER,
                    "runInvocationURI": invocation, "repositoryId": str(repository["id"]),
                    "ownerId": str(owner["id"]), "bundleSource": "oci-registry"}
    raise RuntimeError("Prepared image attestation does not bind the exact selected Build producer")


def load_environment(receipt: Path, output: Path) -> dict:
    """Bind package metadata to the already-verified image, without private paths."""
    if receipt.is_symlink() or not receipt.resolve().is_relative_to(output.resolve()) or receipt.stat().st_size > 2 * 1024 * 1024:
        raise RuntimeError("Prepared package receipt must be bounded regular output evidence")
    value = json.loads(receipt.read_text(encoding="utf-8"))
    environment = value.get("buildEnvironment", {})
    if (value.get("schema") != "emulebb.prepared-native-execution/1" or value.get("status") != "verified"
            or not isinstance(value.get("buildRoot"), str) or not Path(value["buildRoot"]).is_absolute()
            or Path(value["buildRoot"]).resolve() != ROOT
            or environment.get("producer", {}).get("sourceSha") != git(ROOT, "rev-parse", "HEAD")
            or environment.get("producer", {}).get("signer") != SIGNER
            or environment.get("producer", {}).get("cryptographicallyVerified") is not True
            or not re.fullmatch(re.escape(PREPARED) + r"@sha256:[0-9a-f]{64}", environment.get("image", ""))):
        raise RuntimeError("Prepared native package receipt differs from the mounted Build source")
    return environment


def seed_runtime(output: Path, architecture: str) -> None:
    """Seed the existing verified-runtime cache with identical baked bytes."""
    config = json.loads(RUNTIME.read_text(encoding="utf-8"))
    source = BUNDLED_TOOLS / ("runtime-" + architecture)
    if hashlib.sha256(source.read_bytes()).hexdigest() != config["sha256"][architecture]:
        raise RuntimeError("Baked AppImage runtime differs from the selected Build authority")
    destination = output / "tools/appimage/type2-runtime" / config["version"] / ("runtime-" + architecture)
    if destination.is_symlink() or not destination.resolve().is_relative_to(output.resolve()):
        raise RuntimeError("AppImage runtime cache must stay within the inherited output")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def native_image_graph(image: str, architecture: str, *, deadline: float) -> tuple[dict, dict, dict]:
    """Read the hash-verified index, selected native manifest and configuration."""
    repository, index_digest = image.rsplit("@", 1)
    index = public_index(repository, index_digest, deadline=deadline)
    descriptor = cpu_descriptors(index)[architecture]
    manifest = public_index(repository, descriptor["digest"], deadline=deadline)
    configuration = public_blob(repository, manifest["config"]["digest"], deadline=deadline)
    return descriptor, manifest, configuration


class ImageIdentityError(RuntimeError):
    """Expose bounded public identity evidence without an inspect environment dump."""

    def __init__(self, fields: list[str], evidence: dict):
        self.evidence = evidence
        super().__init__("Pulled native image identity differs at " + ", ".join(fields) + ": "
                         + json.dumps(evidence, sort_keys=True))


def _identity_text(value) -> str:
    return value[:200] if isinstance(value, str) else "<" + type(value).__name__ + ">"


def _layer_identity(value) -> dict:
    if not isinstance(value, list):
        return {"type": type(value).__name__}
    return {"count": len(value), "sha256": hashlib.sha256(json.dumps(value).encode()).hexdigest()}


def verify_native_image_identity(inspected: dict, *, image: str, architecture: str,
                                 descriptor: dict, manifest: dict, configuration: dict,
                                 required_labels: dict[str, str]) -> dict:
    """Bind Docker's storage identity and selected configuration to one OCI leaf."""
    def mapping(value):
        return value if isinstance(value, dict) else {}
    def digest(value):
        return isinstance(value, str) and DIGEST.fullmatch(value) is not None
    selected = mapping(descriptor)
    expected_config = mapping(mapping(manifest).get("config"))
    config = mapping(configuration)
    labels = mapping(mapping(config.get("config")).get("Labels"))
    expected_layers = mapping(config.get("rootfs")).get("diff_ids")
    actual = mapping(inspected)
    actual_labels = mapping(mapping(actual.get("Config")).get("Labels"))
    actual_layers = mapping(actual.get("RootFS")).get("Layers")
    local_descriptor = actual.get("Descriptor")
    described = mapping(local_descriptor)
    manifest_digest, config_digest = selected.get("digest"), expected_config.get("digest")
    execution_image = image.rsplit("@", 1)[0] + "@" + str(manifest_digest)
    errors = []
    if (not digest(manifest_digest) or selected.get("mediaType") not in {
            "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}
            or mapping(manifest).get("mediaType") != selected.get("mediaType")
            or type(selected.get("size")) is not int or selected["size"] <= 0):
        errors.append("manifest descriptor")
    if not digest(config_digest) or expected_config.get("mediaType") not in {
            "application/vnd.oci.image.config.v1+json", "application/vnd.docker.container.image.v1+json"}:
        errors.append("config descriptor")
    if config.get("os") != "linux" or config.get("architecture") != architecture:
        errors.append("verified config platform")
    if (mapping(config.get("rootfs")).get("type") != "layers" or not isinstance(expected_layers, list)
            or any(not digest(layer) for layer in expected_layers)):
        errors.append("verified config rootfs")
    if actual.get("Os") != "linux" or actual.get("Architecture") != architecture:
        errors.append("local platform")
    if not isinstance(config.get("config"), dict) or not isinstance(actual.get("Config"), dict):
        errors.append("config types")
    for name, value in (("verified config", mapping(config.get("config")).get("Labels")),
                        ("local config", mapping(actual.get("Config")).get("Labels"))):
        if value is not None and (not isinstance(value, dict)
                                  or any(not isinstance(key, str) or not isinstance(item, str)
                                         for key, item in value.items())):
            errors.append(name + " label types")
    if any(labels.get(name) != value for name, value in required_labels.items()):
        errors.append("verified source labels")
    if actual_labels != labels:
        errors.append("local config labels")
    if mapping(actual.get("RootFS")).get("Type") != "layers" or actual_layers != expected_layers:
        errors.append("local rootfs")
    # WHY: containerd identifies a native manifest; classic Docker identifies its
    # config. Both must belong to this exact verified graph, never another index.
    if local_descriptor is not None:
        identity_kind = "containerd-manifest"
        if (not isinstance(local_descriptor, dict) or described.get("digest") != manifest_digest
                or described.get("mediaType") != selected.get("mediaType")
                or type(described.get("size")) is not int or described.get("size") != selected.get("size")
                or ("platform" in described and (mapping(described["platform"]).get("os") != "linux"
                                                 or mapping(described["platform"]).get("architecture") != architecture))):
            errors.append("local manifest descriptor")
        if actual.get("Id") != manifest_digest:
            errors.append("local manifest Id")
    else:
        identity_kind = "classic-config"
        if actual.get("Id") != config_digest:
            errors.append("local config Id")
    repositories = actual.get("RepoDigests")
    if (not isinstance(repositories, list) or any(not isinstance(item, str) for item in repositories)
            or execution_image not in repositories):
        errors.append("local repository digest")
    evidence = {
        "schema": "emulebb.native-image-identity/1", "kind": identity_kind,
        "image": _identity_text(image), "executionImage": _identity_text(execution_image),
        "platformManifestDigest": _identity_text(manifest_digest), "configDigest": _identity_text(config_digest),
        "expected": {"architecture": architecture, "os": "linux",
                     "manifestMediaType": _identity_text(selected.get("mediaType")),
                     "manifestSize": selected.get("size") if type(selected.get("size")) is int else None,
                     "sourceSha": _identity_text(labels.get("org.opencontainers.image.revision")),
                     "recipeSha256": _identity_text(labels.get("io.emulebb.ci.recipe-sha256"))},
        "observed": {"Id": _identity_text(actual.get("Id")),
                     "manifestDigest": _identity_text(described.get("digest")),
                     "manifestMediaType": _identity_text(described.get("mediaType")),
                     "manifestSize": described.get("size") if type(described.get("size")) is int else None,
                     "architecture": _identity_text(actual.get("Architecture")),
                     "os": _identity_text(actual.get("Os")),
                     "sourceSha": _identity_text(actual_labels.get("org.opencontainers.image.revision")),
                     "recipeSha256": _identity_text(actual_labels.get("io.emulebb.ci.recipe-sha256")),
                     "repoDigests": [_identity_text(item) for item in repositories[:4]]
                     if isinstance(repositories, list) else [], "rootfs": _layer_identity(actual_layers)},
        "expectedRootfs": _layer_identity(expected_layers), "mismatches": errors,
    }
    if errors:
        raise ImageIdentityError(errors, evidence)
    return evidence


def package(*, image: str, release_version: str, target_platform: str) -> Path:
    """No host fallback: preserve package artifacts, source roots and Rust origin."""
    if (os.environ.get("GITHUB_ACTIONS") != "true" or platform.system() != "Linux"
            or not re.fullmatch(re.escape(PREPARED) + r"@sha256:[0-9a-f]{64}", image)):
        raise RuntimeError("Prepared packaging requires native hosted Linux and an exact dedicated image digest")
    # One 35-minute launcher budget fits under the workflow's 40-minute step,
    # reserving time for owned-container cleanup and final source rechecks.
    deadline = time.monotonic() + 2100
    def budget(cap):
        available = deadline - time.monotonic() - 90
        if available <= 0:
            raise RuntimeError("Prepared launcher overall deadline expired")
        return min(cap, available)
    architecture = {"x64": "amd64", "ARM64": "arm64"}.get(target_platform)
    native = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine().lower())
    if architecture != native:
        raise RuntimeError("Prepared native package target must match its actual runner CPU")
    workspace, output = resolve_required_workspace_roots()
    target = resolve_required_cargo_target_dir(output)
    if Path(os.environ.get("GITHUB_WORKSPACE", "")).resolve() != workspace or not target.is_relative_to(output):
        raise RuntimeError("Prepared packaging must preserve inherited CI source/output roots")
    names = {"rust": "EMULEBB_RUST_REPO", "tooling": "EMULEBB_TOOLING_REPO",
             "miniupnp": "MINIUPNP_ROOT", "pcpnatpmp": "PCPNATPMP_ROOT"}
    if any(not os.environ.get(name) for name in names.values()):
        raise RuntimeError("Prepared packaging requires every explicit participating source root")
    roots = {name: Path(os.environ[variable]).resolve() for name, variable in names.items()}
    roots["build"] = ROOT
    if any(not root.is_relative_to(workspace) for root in roots.values()):
        raise RuntimeError("Prepared source roots must fit the same read-only workspace mount")
    sources = snapshot(roots, deadline=deadline)
    if sources["rust"] != os.environ.get("GITHUB_WORKFLOW_SHA") or sources["rust"] != os.environ.get("EMULEBB_SOURCE_REVISION"):
        raise RuntimeError("Prepared source must match the actual Rust workflow source revision")
    inputs = tool_inputs()
    product_tools = json.loads((roots["rust"] / "tools/ci-toolchain.json").read_text(encoding="utf-8"))
    rust_channel = tomllib.loads((roots["rust"] / "rust-toolchain.toml").read_text(encoding="utf-8"))["toolchain"]["channel"]
    if (any(product_tools.get(name) != inputs[name]["version"] for name in ("node", "npm"))
            or product_tools.get("python") != "3.15.2" or rust_channel != "1.98.1"):
        raise RuntimeError("Prepared frontend pins differ from the selected Rust toolchain")
    producer = verify_attestation(image, sources["build"], timeout=budget(180))
    descriptor, manifest, configuration = native_image_graph(image, architecture, deadline=deadline - 90)
    execution_image = PREPARED + "@" + descriptor["digest"]
    report_dir = output / "reports/prepared-native" / uuid4().hex
    if report_dir.exists() or not report_dir.resolve().is_relative_to(output):
        raise RuntimeError("Prepared reports must be new owned output below the inherited root")
    report_dir.mkdir(parents=True)
    receipt = report_dir / "execution.json"
    with LinuxDocker() as docker:
        if docker.run("version", "--format", "{{.Server.Arch}}", capture=True, timeout=budget(30)).stdout.strip() != architecture:
            raise RuntimeError("Prepared package Docker engine must match the native CPU")
        user = _builder_user(docker)
        if user.split(":")[0] == "0":
            raise RuntimeError("Prepared package must use the actual non-root runner UID")
        docker.run("pull", "--platform", "linux/" + architecture, execution_image, timeout=budget(600))
        raw = docker.run("image", "inspect", "--platform", "linux/" + architecture, execution_image,
                         "--format", "{{json .}}", capture=True, timeout=budget(30)).stdout
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError("Prepared image inspection exceeds its metadata bound")
        inspected = json.loads(raw)
        try:
            identity = verify_native_image_identity(
                inspected, image=image, architecture=architecture, descriptor=descriptor, manifest=manifest,
                configuration=configuration, required_labels={
                    "org.opencontainers.image.source": "https://github.com/emulebb/emulebb-build",
                    "org.opencontainers.image.revision": sources["build"],
                    "io.emulebb.ci.recipe-sha256": material_hashes()[RECIPE]})
        except ImageIdentityError as error:
            receipt.write_text(json.dumps({"schema": "emulebb.prepared-native-execution/1", "status": "rejected",
                                          "imageIdentity": error.evidence}, indent=2) + "\n", encoding="utf-8")
            raise
        common = ["--rm", "--platform", "linux/" + architecture, "--user", user,
                  "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                  "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=1g,mode=1777"]
        probe = json.loads(docker.run("run", *common, "--network", "none", "--env", "HOME=/tmp",
                                      "--env", "NPM_CONFIG_CACHE=/tmp/npm", execution_image,
                                      "python3", "-B", "-m", "emule_workspace.ci_linux_image_probe",
                                      capture=True, timeout=budget(180)).stdout)
        if probe.get("status") != "passed" or probe.get("architecture") != architecture or probe.get("uid") != int(user.split(":")[0]):
            raise RuntimeError("Prepared environment failed the actual native non-root packaging probe")
        environment = {"schema": "emulebb.prepared-build-environment/1", "image": image,
                       "executionImage": execution_image, "platformManifestDigest": descriptor["digest"],
                       "configDigest": manifest["config"]["digest"],
                       "producer": producer, "runtimeProbe": probe}
        value = {"schema": "emulebb.prepared-native-execution/1", "status": "verified",
                 "buildRoot": str(ROOT), "buildEnvironment": environment, "imageIdentity": identity}
        receipt.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        owner = user.replace(":", "-")
        cache = output / "cache/prepared-native" / owner
        for directory in (cache / "cargo", cache / "home"):
            if directory.is_symlink() or not directory.resolve().is_relative_to(output):
                raise RuntimeError("Prepared writable cache must remain below the inherited output")
            directory.mkdir(parents=True, exist_ok=True)
        mounts = ["--mount", f"type=bind,source={workspace},target={workspace},readonly",
                  "--mount", f"type=bind,source={output},target={output}"]
        forwarded = [part for name in FORWARD if name in os.environ for part in ("--env", name + "=" + os.environ[name])]
        arguments = [*common, *mounts, *forwarded, "--env", "HOME=" + str(cache / "home"),
                     "--env", "CARGO_HOME=" + str(cache / "cargo"),
                     # A fixed read-only-source policy, never inherited hook context.
                     "--env", "GIT_OPTIONAL_LOCKS=0",
                     "--env", "ARCH=" + ("x86_64" if architecture == "amd64" else "aarch64"),
                     "--workdir", str(ROOT), execution_image, "python3", "-B", "-m", "emule_workspace",
                     "package-emulebb-rust-ci", "--release-version", release_version, "--target-os", "linux",
                     "--platform", target_platform, "--prepared-image-receipt", str(receipt)]
        run_owned_container(docker, arguments, report_dir / "package.log", timeout=budget(2100))
    if snapshot(roots, deadline=deadline) != sources:
        raise RuntimeError("Prepared package source changed during production")
    return receipt
