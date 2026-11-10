"""The prepared launcher preserves sources, provenance and the native package path."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from click.testing import CliRunner

import pytest

from emule_workspace import prepared_native_ci as prepared
from emule_workspace import cli
from emule_workspace import release


@pytest.fixture
def host(tmp_path, monkeypatch):
    workspace, output = tmp_path / "workspace", tmp_path / "output"
    workspace.mkdir()
    output.mkdir()
    build = workspace / ".ci/build"
    build.mkdir(parents=True)
    (workspace / "tools").mkdir()
    tools = prepared.tool_inputs()
    (workspace / "tools/ci-toolchain.json").write_text(json.dumps({
        "node": tools["node"]["version"], "npm": tools["npm"]["version"], "python": "3.15.2"}), encoding="utf-8")
    (workspace / "rust-toolchain.toml").write_text('[toolchain]\nchannel="1.98.1"\n', encoding="utf-8")
    sources = {"rust": "a" * 40, "build": "b" * 40, "tooling": "c" * 40,
               "miniupnp": "d" * 40, "pcpnatpmp": "e" * 40}
    for name in ("EMULEBB_TOOLING_REPO", "MINIUPNP_ROOT", "PCPNATPMP_ROOT"):
        path = workspace / name.lower()
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("EMULEBB_RUST_REPO", str(workspace))
    monkeypatch.setenv("EMULEBB_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("EMULEBB_WORKSPACE_OUTPUT_ROOT", str(output))
    target = output / "builds/rust/target"
    target.mkdir(parents=True)
    monkeypatch.setenv("CARGO_TARGET_DIR", str(target))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(workspace))
    monkeypatch.setenv("GITHUB_WORKFLOW_SHA", sources["rust"])
    monkeypatch.setenv("EMULEBB_SOURCE_REVISION", sources["rust"])
    monkeypatch.setenv("GH_TOKEN", "synthetic-private-token")
    monkeypatch.setenv("GIT_DIR", "synthetic-wrong-git-context")
    monkeypatch.setattr(prepared, "ROOT", build)
    monkeypatch.setattr(prepared.platform, "system", lambda: "Linux")
    monkeypatch.setattr(prepared.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(prepared, "resolve_required_workspace_roots", lambda: (workspace, output))
    monkeypatch.setattr(prepared, "resolve_required_cargo_target_dir", lambda root: target)
    monkeypatch.setattr(prepared, "snapshot", lambda *a, **k: dict(sources))
    monkeypatch.setattr(prepared, "_builder_user", lambda docker: "1001:1001")
    image = prepared.PREPARED + "@sha256:" + "1" * 64
    config = "sha256:" + "2" * 64
    calls, executed = [], []
    clock = [0.0]
    def verify(*args, **kwargs):
        calls.append(("attestation", kwargs))
        clock[0] += 180
        return {"cryptographicallyVerified": True, "sourceSha": sources["build"], "signer": prepared.SIGNER}
    monkeypatch.setattr(prepared, "verify_attestation", verify)
    def public(repository, digest, **kwargs):
        assert kwargs["deadline"] == 2010
        clock[0] += 20
        if digest.endswith("1" * 64):
            return {"manifests": [{"digest": "sha256:" + "3" * 64, "platform": {"os": "linux", "architecture": "amd64"}},
                                  {"digest": "sha256:" + "4" * 64, "platform": {"os": "linux", "architecture": "arm64"}}]}
        return {"config": {"digest": config}}
    monkeypatch.setattr(prepared, "public_index", public)
    monkeypatch.setattr(prepared.time, "monotonic", lambda: clock[0])
    class Docker:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def run(self, *args, **kwargs):
            calls.append((args, kwargs))
            if args[0] == "version":
                return SimpleNamespace(stdout="amd64")
            if args[0] == "pull":
                clock[0] += 600
                return SimpleNamespace(stdout="")
            if args[0] == "image":
                return SimpleNamespace(stdout=json.dumps({"Architecture": "amd64", "Os": "linux", "Id": config,
                    "RepoDigests": [image], "Config": {"Labels": {
                        "org.opencontainers.image.revision": sources["build"],
                        "io.emulebb.ci.recipe-sha256": prepared.material_hashes()[prepared.RECIPE]}}}))
            if args[0] == "run":
                clock[0] += 180
                return SimpleNamespace(stdout=json.dumps({"status": "passed", "architecture": "amd64", "uid": 1001}))
            pytest.fail(str(args))
    monkeypatch.setattr(prepared, "LinuxDocker", Docker)
    monkeypatch.setattr(prepared, "run_owned_container", lambda docker, args, log, **kwargs: executed.append((args, log, kwargs)))
    return SimpleNamespace(workspace=workspace, output=output, build=build, sources=sources,
                           image=image, calls=calls, executed=executed, clock=clock)


def test_existing_pipeline_same_paths_nonroot_and_no_credential_forwarding(host):
    receipt = prepared.package(image=host.image, release_version="synthetic-version", target_platform="x64")
    assert host.calls[0][0] == "attestation"
    args, log, kwargs = host.executed[0]
    assert f"type=bind,source={host.workspace},target={host.workspace},readonly" in args
    assert f"type=bind,source={host.output},target={host.output}" in args
    assert args[args.index("--workdir") + 1] == str(host.build)
    assert args[args.index("--user") + 1] == "1001:1001"
    assert "CARGO_TARGET_DIR=" + str(host.output / "builds/rust/target") in args
    assert any(value.startswith("CARGO_HOME=" + str(host.output)) for value in args)
    assert any(value.startswith("HOME=" + str(host.output)) for value in args)
    assert not any("GH_TOKEN" in value or "GIT_DIR" in value or "synthetic-private-token" in value for value in args)
    assert "GIT_OPTIONAL_LOCKS=0" in args
    assert "package-emulebb-rust-ci" in args and "--prepared-image-receipt" in args
    assert args[-1] == str(receipt) and "--skip-build" not in args
    assert kwargs["timeout"] == 1010  # 2100 - 180 attest - 40 HTTP - 600 pull - 180 probe - 90 cleanup
    assert log.resolve().is_relative_to(host.output)
    value = json.loads(receipt.read_text())
    assert value["buildEnvironment"]["producer"]["sourceSha"] == host.sources["build"]
    assert "buildRoot" not in value["buildEnvironment"]


def test_attestation_failure_starts_no_docker_or_product(host, monkeypatch):
    monkeypatch.setattr(prepared, "verify_attestation", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad signature")))
    with pytest.raises(RuntimeError, match="signature"):
        prepared.package(image=host.image, release_version="synthetic", target_platform="x64")
    assert not host.calls and not host.executed


def test_report_reparse_ancestor_is_rejected_before_write(host, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (host.output / "reports").mkdir()
    try:
        (host.output / "reports/prepared-native").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating a fixture symlink is unavailable on this Windows host")
    with pytest.raises(RuntimeError, match="owned output"):
        prepared.package(image=host.image, release_version="synthetic", target_platform="x64")
    assert list(outside.iterdir()) == [] and not host.executed


def test_moving_image_ref_is_rejected_before_any_external_action(host):
    with pytest.raises(RuntimeError, match="exact dedicated image digest"):
        prepared.package(image=prepared.PREPARED + ":latest", release_version="synthetic", target_platform="x64")
    assert not host.calls


def test_changed_source_invalidates_completed_package(host, monkeypatch):
    snapshots = iter([host.sources, {**host.sources, "rust": "f" * 40}])
    monkeypatch.setattr(prepared, "snapshot", lambda *a, **k: next(snapshots))
    with pytest.raises(RuntimeError, match="source changed"):
        prepared.package(image=host.image, release_version="synthetic", target_platform="x64")


def test_outer_deadline_is_shared_not_restarted_after_slow_pull(host, monkeypatch):
    original = prepared.verify_attestation
    def slow(*args, **kwargs):
        value = original(*args, **kwargs)
        host.clock[0] += 1900
        return value
    monkeypatch.setattr(prepared, "verify_attestation", slow)
    with pytest.raises(RuntimeError, match="overall deadline"):
        prepared.package(image=host.image, release_version="synthetic", target_platform="x64")
    assert not host.executed


def test_verified_baked_runtime_seeds_existing_cache_without_download(tmp_path, monkeypatch):
    tools, output = tmp_path / "tools", tmp_path / "output"
    tools.mkdir()
    output.mkdir()
    runtime = tools / "runtime-x86_64"
    runtime.write_bytes(b"synthetic verified runtime")
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps({"version": "20251108", "sha256": {
        "x86_64": hashlib.sha256(runtime.read_bytes()).hexdigest()}}))
    monkeypatch.setattr(prepared, "BUNDLED_TOOLS", tools)
    monkeypatch.setattr(prepared, "RUNTIME", config)
    prepared.seed_runtime(output, "x86_64")
    assert (output / "tools/appimage/type2-runtime/20251108/runtime-x86_64").read_bytes() == runtime.read_bytes()


@pytest.fixture
def certificate(monkeypatch):
    source = "b" * 40
    image = prepared.PREPARED + "@sha256:" + "1" * 64
    certificate = {"subjectAlternativeName": prepared.SIGNER, "sourceRepositoryDigest": source,
                   "buildSignerDigest": source, "sourceRepositoryURI": "https://github.com/emulebb/emulebb-build",
                   "sourceRepositoryOwnerURI": "https://github.com/emulebb", "sourceRepositoryRef": "refs/heads/main",
                   "sourceRepositoryIdentifier": "499553807", "sourceRepositoryOwnerIdentifier": "281581430",
                   "runnerEnvironment": "github-hosted",
                   "runInvocationURI": "https://github.com/emulebb/emulebb-build/actions/runs/123/attempts/2"}
    repository = {"id": 499553807, "full_name": "emulebb/emulebb-build", "private": False,
                  "owner": {"id": 281581430, "html_url": "https://github.com/emulebb"}}
    statement = {"predicateType": "https://slsa.dev/provenance/v1",
                 "subject": [{"name": prepared.PREPARED, "digest": {"sha256": "1" * 64}}],
                 "predicate": {"sourceRepositoryIdentifier": "untrusted predicate value"}}
    calls = []
    def metadata(request, **kwargs):
        assert request.full_url == "https://api.github.com/repos/emulebb/emulebb-build"
        assert not request.has_header("Authorization")
        return json.dumps(repository).encode()
    def gh(command, **kwargs):
        calls.append(command)
        return json.dumps([{"verificationResult": {"signature": {"certificate": certificate}, "statement": statement}}])
    monkeypatch.setattr(prepared, "public_metadata_bytes", metadata)
    monkeypatch.setattr(prepared.subprocess, "check_output", gh)
    return SimpleNamespace(source=source, image=image, certificate=certificate, repository=repository,
                           statement=statement, calls=calls)


def test_oci_bundle_verification_restores_immutable_certificate_identity(certificate):
    result = prepared.verify_attestation(certificate.image, certificate.source)
    assert result["repositoryId"] == "499553807" and result["ownerId"] == "281581430"
    assert result["bundleSource"] == "oci-registry"
    assert result["runInvocationURI"].endswith("/attempts/2")
    command = certificate.calls[0]
    for option in ("--bundle-from-oci", "--deny-self-hosted-runners", "--source-digest", "--signer-digest", "--source-ref"):
        assert option in command
    assert command[command.index("--source-digest") + 1] == certificate.source


@pytest.mark.parametrize("field,value", [
    ("sourceRepositoryIdentifier", "999"), ("sourceRepositoryOwnerIdentifier", "999"),
    ("sourceRepositoryURI", "https://github.com/foreign/repository"),
    ("sourceRepositoryOwnerURI", "https://github.com/foreign"),
    ("sourceRepositoryDigest", "c" * 40), ("buildSignerDigest", "c" * 40),
    ("sourceRepositoryRef", "refs/heads/topic"), ("runnerEnvironment", "self-hosted"),
])
def test_verified_certificate_not_predicate_must_match_current_authority(certificate, field, value):
    certificate.certificate[field] = value
    certificate.statement["predicate"] = {"sourceRepositoryIdentifier": "499553807",
                                           "sourceRepositoryOwnerIdentifier": "281581430"}
    with pytest.raises(RuntimeError, match="exact selected Build producer"):
        prepared.verify_attestation(certificate.image, certificate.source)


@pytest.mark.parametrize("field,value", [("id", 999), ("private", True), ("full_name", "foreign/build")])
def test_repository_rename_or_visibility_change_fails_before_crypto_fetch(certificate, field, value):
    certificate.repository[field] = value
    with pytest.raises(RuntimeError, match="immutable authority"):
        prepared.verify_attestation(certificate.image, certificate.source)
    assert not certificate.calls


def test_package_provenance_embeds_image_authority_separate_from_rust_origin(host, monkeypatch):
    receipt = prepared.package(image=host.image, release_version="synthetic", target_platform="x64")
    monkeypatch.setattr(prepared, "git", lambda *args, **kwargs: host.sources["build"])
    monkeypatch.setattr(release, "_assert_clean_emulebb_rust_package_inputs", lambda *args: None)
    monkeypatch.setattr(release, "_emulebb_rust_source_provenance", lambda *args: {
        "emulebbRust": {"commit": host.sources["rust"]}, "emulebbBuild": {"commit": host.sources["build"]}})
    monkeypatch.setattr(release, "_emulebb_rust_artifact_origin", lambda: {
        "origin": "github-actions", "workflow": {"workflowSha": host.sources["rust"]}})
    layout = SimpleNamespace(output_root=host.output)
    result = release._emulebb_rust_package_provenance(layout, host.workspace, prepared_image_receipt=receipt)
    assert result["workflow"]["workflowSha"] == host.sources["rust"]
    assert result["buildEnvironment"]["producer"]["sourceSha"] == host.sources["build"]
    assert "buildRoot" not in result["buildEnvironment"]
    changed = json.loads(receipt.read_text())
    changed["buildEnvironment"]["runtimeProbe"]["uid"] = 999
    receipt.write_text(json.dumps(changed))
    with pytest.raises(RuntimeError, match="identity changed"):
        release._assert_emulebb_rust_package_provenance(layout, host.workspace, result, prepared_image_receipt=receipt)


def test_prepared_cli_forwards_exact_reserved_consumer_interface(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(prepared, "package", lambda **kwargs: calls.append(kwargs) or tmp_path / "evidence.json")
    image = prepared.PREPARED + "@sha256:" + "1" * 64
    result = CliRunner().invoke(cli.main, ["package-emulebb-rust-prepared-ci", "--image", image,
                                         "--release-version", "0.1.0-beta.2", "--platform", "ARM64"])
    assert result.exit_code == 0, result.output
    assert calls == [{"image": image, "release_version": "0.1.0-beta.2", "target_platform": "ARM64"}]


@pytest.mark.parametrize("libc,arch,host", [("gnu", "amd64", "amd64"), ("gnu", "arm64", "arm64"),
                                          ("musl", "amd64", "amd64"), ("musl", "arm64", "arm64")])
def test_native_mirror_configuration_preserves_original_digest(libc, arch, host):
    from emule_workspace import linux_containers
    from emule_workspace.ci_image_sources import NAMESPACE, UPSTREAM_RUST
    base, actual_host, cross = linux_containers.build_configuration(libc, arch, host, use_ci_image_mirrors=True)
    assert base == NAMESPACE + "/rust-" + libc + "@" + UPSTREAM_RUST[libc].rsplit("@", 1)[1]
    assert actual_host == host and not cross


def test_producer_upstream_authority_cannot_recursively_become_a_consumer_mirror(monkeypatch):
    from emule_workspace import ci_linux_images, linux_containers
    from emule_workspace.ci_image_sources import UPSTREAM_RUST
    original = UPSTREAM_RUST["gnu"]
    monkeypatch.setitem(linux_containers.RUST_IMAGES, "gnu", "ghcr.io/synthetic/mirror@sha256:" + "1" * 64)
    assert ci_linux_images.SOURCES["rust-gnu"] == original
    assert linux_containers.build_configuration("musl", "arm64", "amd64", use_ci_image_mirrors=True)[0] == linux_containers.MUSL_CROSS_IMAGE
