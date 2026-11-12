"""Exact graph replication and native-only prepared compiler publication."""
import hashlib
import json
from pathlib import Path
import sys
import tomllib
import urllib.error

import pytest
import yaml

from emule_workspace import ci_linux_images as images


def encoded(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest(), raw


class Registry:
    def __init__(self, *, config_arch=None):
        self.objects = {}
        self.commands = []
        descriptors = []
        for arch in ("amd64", "arm64"):
            digest, raw = encoded({"architecture": config_arch or arch, "os": "linux"})
            self.objects[digest] = raw
            manifest, raw = encoded({"schemaVersion": 2, "config": {"digest": digest}, "layers": []})
            self.objects[manifest] = raw
            descriptors.append({"digest": manifest, "size": len(raw),
                                "platform": {"architecture": arch, "os": "linux"}})
        self.index, raw = encoded({"schemaVersion": 2, "manifests": descriptors})
        self.objects[self.index] = raw

    def run(self, arguments, **kwargs):
        self.commands.append(arguments)
        if arguments[1:3] == ["image", "copy"]:
            return ""
        if arguments[1:3] == ["image", "digest"]:
            return self.index
        if arguments[1:3] == ["manifest", "get"]:
            return self.objects[arguments[3].rsplit("@", 1)[1]]
        if arguments[1:3] == ["blob", "get"]:
            return self.objects[arguments[4]]
        raise AssertionError(arguments)


def test_replication_preserves_exact_index_children_and_configs(monkeypatch):
    registry = Registry()
    upstream = "docker.io/library/rust:synthetic@" + registry.index
    monkeypatch.setattr(images, "SOURCES", {"rust-gnu": upstream})
    result = images.replicate_sources(client=registry.run)
    assert result[0]["upstream"] == upstream
    assert result[0]["mirror"] == images.NAMESPACE + "/rust-gnu@" + registry.index
    assert set(result[0]["platforms"]) == {"amd64", "arm64"}
    copy = next(command for command in registry.commands if command[1:3] == ["image", "copy"])
    assert copy[3] == "--force-recursive" and copy[4] == upstream
    assert not any("--platform" in command for command in registry.commands)
    # Both actual config blobs and all source+destination CPU manifests were read.
    assert len([command for command in registry.commands if command[1:3] == ["blob", "get"]]) == 2
    assert len([command for command in registry.commands if command[1:3] == ["manifest", "get"]]) == 6


def test_replication_rejects_config_cpu_mismatch(monkeypatch):
    registry = Registry(config_arch="amd64")
    monkeypatch.setattr(images, "SOURCES", {"rust-gnu": "docker.io/library/rust:synthetic@" + registry.index})
    with pytest.raises(RuntimeError, match="wrong native CPU"):
        images.replicate_sources(client=registry.run)


def test_raw_registry_hash_mismatch_cannot_be_accepted():
    with pytest.raises(RuntimeError, match="exact source digest"):
        images.digest_bytes(b'{"modified":true}', "sha256:" + "a" * 64)


@pytest.mark.parametrize("descriptors", [[], [{"digest": "sha256:" + "a" * 64,
                                              "platform": {"os": "linux", "architecture": "amd64"}}]])
def test_missing_cpu_fails_before_replication(descriptors):
    with pytest.raises(RuntimeError, match="both amd64 and arm64"):
        images.cpu_descriptors({"manifests": descriptors})


def test_ghcr_rust_cross_is_not_misrepresented_as_a_hub_mirror():
    assert all(source.startswith("docker.io/library/") for source in images.SOURCES.values())
    assert not any("rust-cross" in source for source in images.SOURCES.values())


def test_trusted_context_rejects_pr_before_any_git_or_registry_operation(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "emulebb/emulebb-build")
    monkeypatch.setenv("GITHUB_REF", "refs/pull/123/merge")
    monkeypatch.setattr(images.subprocess, "check_output", lambda *a, **k: pytest.fail("Untrusted source ran a command"))
    with pytest.raises(RuntimeError, match="trusted main"):
        images.context()


def test_private_package_is_actionable_failure_without_credential_changes(monkeypatch):
    def unavailable(*args, **kwargs):
        raise urllib.error.HTTPError("https://ghcr.io/token", 403, "synthetic anonymous denial", {}, None)
    monkeypatch.setattr(images.urllib.request, "urlopen", unavailable)
    with pytest.raises(RuntimeError, match="operator must review public visibility"):
        images.public_index(images.PREPARED, "sha256:" + "a" * 64)


@pytest.mark.parametrize("tampered", [False, True])
def test_anonymous_config_blob_is_hash_verified_without_exposing_its_transient_token(monkeypatch, tampered):
    value = {"architecture": "arm64", "os": "linux", "rootfs": {"type": "layers", "diff_ids": []}}
    expected, raw = encoded(value)
    calls = []
    def read(request, **kwargs):
        calls.append(request)
        if isinstance(request, str):
            return json.dumps({"token": "synthetic-public-token"}).encode()
        assert request.full_url == "https://ghcr.io/v2/emulebb/emulebb-build-ci/ubuntu-rust/blobs/" + expected
        assert request.get_header("Authorization") == "Bearer synthetic-public-token"
        return b"{}" if tampered else raw
    monkeypatch.setattr(images, "public_metadata_bytes", read)
    if tampered:
        with pytest.raises(RuntimeError, match="exact source digest") as caught:
            images.public_blob(images.PREPARED, expected)
        assert "synthetic-public-token" not in str(caught.value)
    else:
        assert images.public_blob(images.PREPARED, expected) == value
    assert len(calls) == 2


def test_native_builder_uses_actual_buildx_digest_and_nonroot_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(images.platform, "system", lambda: "Linux")
    monkeypatch.setattr(images.platform, "machine", lambda: "x86_64")
    digest = "sha256:" + "b" * 64
    commands = []
    def client(arguments, **kwargs):
        commands.append(arguments)
        if arguments[:2] == ["docker", "version"]:
            return "amd64"
        if arguments[:3] == ["docker", "buildx", "build"]:
            metadata = Path(arguments[arguments.index("--metadata-file") + 1])
            metadata.write_text(json.dumps({"containerimage.digest": digest}), encoding="utf-8")
            return ""
        if arguments[:2] == ["docker", "run"]:
            return json.dumps({"status": "passed", "architecture": "amd64"})
        raise AssertionError(arguments)
    result = images.build_platform("amd64", tmp_path, {"sourceSha": "a" * 40}, client=client)
    assert result["digest"] == digest
    assert result["bases"] == images.prepared_bases()
    build = commands[1]
    assert build[build.index("--platform") + 1] == "linux/amd64"
    assert "mode=max" in build
    assert any("push-by-digest=true" in argument for argument in build)
    assert not any("BUILD_UID" in argument or "BUILD_GID" in argument for argument in build)
    probe = commands[2]
    assert probe[probe.index("--user") + 1] == "10000:10000"
    assert probe[probe.index("--network") + 1] == "none"
    assert images.PREPARED + "@" + digest in probe


@pytest.mark.parametrize("architecture,source", [("armv7", "a" * 40), ("amd64", "main"), ("amd64", None)])
def test_shared_native_build_arguments_require_an_exact_cpu_and_source(tmp_path, architecture, source):
    with pytest.raises(RuntimeError, match="supported CPU and exact source SHA"):
        images.native_build_arguments(architecture, tmp_path / "metadata.json", source)


def test_shared_recipe_arguments_bind_materials_without_selecting_publication(tmp_path):
    command = images.native_build_arguments("arm64", tmp_path / "metadata.json", "a" * 40)
    assert command[:5] == ["docker", "buildx", "build", "--platform", "linux/arm64"]
    assert command[command.index("--file") + 1] == str(images.ROOT / images.RECIPE)
    assert "io.emulebb.ci.recipe-sha256=" + images.material_hashes()[images.RECIPE] in command
    for name, value in images.prepared_bases().items():
        assert name + "=" + value in command
    assert not any(item in command for item in ("--output", "--push", "--load", "--tag", "--provenance"))


def test_cross_runner_cannot_claim_native_builder_proof(tmp_path, monkeypatch):
    monkeypatch.setattr(images.platform, "system", lambda: "Linux")
    monkeypatch.setattr(images.platform, "machine", lambda: "x86_64")
    with pytest.raises(RuntimeError, match="matching native Linux runner"):
        images.build_platform("arm64", tmp_path, {}, client=lambda *a, **k: pytest.fail("Cross builder started"))


@pytest.mark.parametrize("mutation", ["source", "bases", "root-probe", "wrong-python"])
def test_mixed_or_unproven_native_receipt_refuses_index_publication(mutation):
    identity = {"repository": "emulebb/emulebb-build", "sourceSha": "a" * 40, "runId": "123", "runAttempt": "1"}
    records = [{**identity, "status": "passed", "architecture": arch, "image": images.PREPARED,
                "digest": "sha256:" + "b" * 64, "materials": images.material_hashes(), "bases": images.prepared_bases(),
                "probe": {"status": "passed", "architecture": arch, "uid": 10000,
                          "python": "3.15.2", "rustc": "rustc 1.98.1 (synthetic)"}}
               for arch in ("amd64", "arm64")]
    if mutation == "source":
        records[0]["sourceSha"] = "c" * 40
    elif mutation == "bases":
        records[0]["bases"] = {}
    elif mutation == "root-probe":
        records[0]["probe"]["uid"] = 0
    else:
        records[0]["probe"]["python"] = "3.14.0"
    with pytest.raises(RuntimeError, match="exact producer"):
        images.assemble_platforms(records, identity, client=lambda *a, **k: pytest.fail("Bad proof reached publication"))


class PreparedRegistry:
    def __init__(self, *, wrong_label=False, extra_cpu=False):
        self.identity = {"repository": "emulebb/emulebb-build", "sourceSha": "a" * 40,
                         "runId": "123", "runAttempt": "1"}
        self.objects, self.records, self.commands = {}, [], []
        descriptors = []
        for arch in ("amd64", "arm64"):
            config, raw = encoded({"os": "linux", "architecture": arch, "config": {"Labels": {
                "org.opencontainers.image.source": "https://github.com/emulebb/emulebb-build",
                "org.opencontainers.image.revision": "c" * 40 if wrong_label else self.identity["sourceSha"],
                "io.emulebb.ci.recipe-sha256": images.material_hashes()[images.RECIPE]}}})
            self.objects[config] = raw
            manifest, raw = encoded({"schemaVersion": 2, "config": {"digest": config}, "layers": []})
            self.objects[manifest] = raw
            descriptor = {"digest": manifest, "size": len(raw), "platform": {"os": "linux", "architecture": arch}}
            descriptors.append(descriptor)
            native_index, raw = encoded({"schemaVersion": 2, "manifests": [descriptor]})
            self.objects[native_index] = raw
            self.records.append({**self.identity, "status": "passed", "architecture": arch,
                                 "image": images.PREPARED, "digest": native_index,
                                 "materials": images.material_hashes(), "bases": images.prepared_bases(),
                                 "probe": {"status": "passed", "architecture": arch, "uid": 10000,
                                           "python": "3.15.2", "rustc": "rustc 1.98.1 (synthetic)"}})
        if extra_cpu:
            descriptors.append({"digest": "sha256:" + "c" * 64,
                                "platform": {"os": "linux", "architecture": "ppc64le"}})
        self.index, raw = encoded({"schemaVersion": 2, "manifests": descriptors})
        self.objects[self.index] = raw

    def run(self, arguments, **kwargs):
        self.commands.append(arguments)
        if arguments[:3] == ["regctl", "tag", "ls"]:
            return ""
        if arguments[:4] == ["docker", "buildx", "imagetools", "create"]:
            return ""
        if arguments[:3] == ["regctl", "image", "digest"]:
            return self.index
        if arguments[:3] == ["regctl", "manifest", "get"]:
            return self.objects[arguments[3].rsplit("@", 1)[1]]
        if arguments[:3] == ["regctl", "blob", "get"]:
            return self.objects[arguments[4]]
        raise AssertionError(arguments)


def test_actual_prepared_graph_matches_both_native_tested_outputs():
    registry = PreparedRegistry()
    result = images.assemble_platforms(registry.records, registry.identity, client=registry.run)
    assert result["digest"] == registry.index
    assert result["image"] == images.PREPARED + "@" + registry.index
    assert result["status"] == "assembled-pending-public-read"
    assert result["anonymousPublicReadVerified"] is False
    assert set(result["platforms"]) == {"amd64", "arm64"}
    create = next(command for command in registry.commands if command[0] == "docker")
    assert create[-2:] == [images.PREPARED + "@" + record["digest"] for record in registry.records]


def test_same_run_prior_native_attempts_are_preserved_on_public_gate_rerun():
    registry = PreparedRegistry()
    current = {**registry.identity, "runAttempt": "2"}
    result = images.assemble_platforms(registry.records, current, client=registry.run)
    assert result["runAttempt"] == "2"
    assert [record["runAttempt"] for record in result["nativeReceipts"]] == ["1", "1"]


@pytest.mark.parametrize("field,value", [("runAttempt", "3"), ("runAttempt", "0"), ("runAttempt", True),
                                        ("runId", "999"), ("repository", "foreign/example"),
                                        ("sourceSha", "c" * 40), ("workflowSha", "c" * 40)])
def test_partial_rerun_cannot_adopt_foreign_future_or_changed_source_receipt(field, value):
    registry = PreparedRegistry()
    registry.identity["workflowSha"] = registry.identity["sourceSha"]
    for record in registry.records:
        record["workflowSha"] = registry.identity["sourceSha"]
    registry.records[0][field] = value
    with pytest.raises(RuntimeError, match="exact producer"):
        images.assemble_platforms(registry.records, {**registry.identity, "runAttempt": "2"},
                                  client=lambda *a, **k: pytest.fail("Invalid retained receipt reached publication"))


@pytest.mark.parametrize("kwargs,diagnostic", [({"wrong_label": True}, "native config"),
                                               ({"extra_cpu": True}, "unexpected runnable CPUs")])
def test_prepared_graph_rejects_wrong_source_labels_or_extra_runnable_cpu(kwargs, diagnostic):
    registry = PreparedRegistry(**kwargs)
    with pytest.raises(RuntimeError, match=diagnostic):
        images.assemble_platforms(registry.records, registry.identity, client=registry.run)


def test_existing_prepared_tag_refuses_blind_rewrite():
    registry = PreparedRegistry()
    def existing(arguments, **kwargs):
        if arguments[:3] == ["regctl", "tag", "ls"]:
            return "run-123-attempt-1"
        pytest.fail("Existing producer tag was rewritten")
    with pytest.raises(RuntimeError, match="parent inspection"):
        images.assemble_platforms(registry.records, registry.identity, client=existing)


def test_private_prepared_output_retains_actual_digest_without_emitting_consumer_pin(tmp_path, monkeypatch):
    registry = PreparedRegistry()
    report = tmp_path / "prepared-index.json"
    output = tmp_path / "github-output"
    monkeypatch.setattr(sys, "argv", ["ci-linux-images", "assemble", "--records", str(tmp_path),
                                      "--report", str(report), "--github-output", str(output)])
    monkeypatch.setattr(images, "context", lambda: registry.identity)
    monkeypatch.setattr(images, "assemble_platforms", lambda *a, **k: {
        "status": "assembled-pending-public-read", "digest": registry.index,
        "image": images.PREPARED + "@" + registry.index, "anonymousPublicReadVerified": False})
    def denied(*args, **kwargs):
        raise RuntimeError("operator must review public visibility")
    monkeypatch.setattr(images, "public_index", denied)
    with pytest.raises(RuntimeError, match="public visibility"):
        images.main()
    retained = json.loads(report.read_text(encoding="utf-8"))
    assert retained["status"] == "blocked-public-access"
    assert retained["digest"] == registry.index and retained["anonymousPublicReadVerified"] is False
    assert not output.exists()


def test_producer_permissions_and_native_matrix_have_no_untrusted_publication_path():
    workflow = yaml.safe_load((images.ROOT / ".github/workflows/ci-linux-images.yml").read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_dispatch", "pull_request", "push"}
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        if name == "tests":
            assert "permissions" not in job
            assert set(job["strategy"]["matrix"]["runner"]) == {"ubuntu-24.04", "ubuntu-24.04-arm"}
            packages = tomllib.loads((images.ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
            pytest_pin = next(item["version"] for item in packages if item["name"] == "pytest")
            install = next(step["run"] for step in job["steps"] if "pytest==" in step.get("run", ""))
            assert "pytest==" + pytest_pin in install
            continue
        if name == "docker-conformance":
            assert "permissions" not in job
            assert job["needs"] == "tests"
            assert job["strategy"]["matrix"] == workflow["jobs"]["native"]["strategy"]["matrix"]
            assert not any("login-action" in step.get("uses", "") for step in job["steps"])
            checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
            assert checkout["with"]["persist-credentials"] is False
            assert checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha || github.sha }}"
            assert job["timeout-minutes"] == 35
            execution = next(step for step in job["steps"] if "--docker-conformance" in step.get("run", ""))
            assert execution["env"] == {"PYTHONPATH": "${{ github.workspace }}"}
            assert "--architecture ${{ matrix.architecture }}" in execution["run"]
            assert "${{ runner.temp }}/native-image-conformance/" in execution["run"]
            prepared = next(step for step in job["steps"]
                            if "tests/test_ci_linux_image_probe.py --docker-conformance" in step.get("run", ""))
            assert "--architecture ${{ matrix.architecture }}" in prepared["run"]
            assert "native-image-conformance/prepared-${{ matrix.architecture }}.json" in prepared["run"]
            buildx = next(step for step in job["steps"] if step.get("uses", "").startswith("docker/setup-buildx-action@"))
            from emule_workspace.rust_images import BUILDKIT_IMAGE
            assert buildx["with"]["driver-opts"] == "image=" + BUILDKIT_IMAGE
            assert "qemu" not in str(job).lower()
            artifact = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
            assert artifact["if"] == "always()"
            continue
        assert "github.event_name == 'workflow_dispatch'" in job["if"]
        assert "github.ref == 'refs/heads/main'" in job["if"]
        assert "github.repository == 'emulebb/emulebb-build'" in job["if"]
        assert job["permissions"]["packages"] == "write"
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["ref"] == "${{ github.workflow_sha }}"
                assert step["with"]["persist-credentials"] is False
    native = workflow["jobs"]["native"]
    assert workflow["jobs"]["mirrors"]["needs"] == ["tests", "docker-conformance"]
    for name in ("native", "verify", "docker-conformance"):
        docker = next(step for step in workflow["jobs"][name]["steps"]
                      if step.get("uses", "").startswith("docker/setup-docker-action@"))
        assert docker["with"]["version"] == "v29.6.1"
        assert json.loads(docker["with"]["daemon-config"])["features"]["containerd-snapshotter"] is True
    from emule_workspace.rust_images import BUILDKIT_IMAGE
    buildx = next(step for step in native["steps"] if step.get("uses", "").startswith("docker/setup-buildx-action@"))
    assert buildx["with"]["driver-opts"] == "image=" + BUILDKIT_IMAGE
    assert native["strategy"]["matrix"]["include"] == [
        {"architecture": "amd64", "runner": "ubuntu-24.04"},
        {"architecture": "arm64", "runner": "ubuntu-24.04-arm"}]
    assert not any("qemu" in str(step).lower() for step in native["steps"])
    verify = workflow["jobs"]["verify"]
    assert verify["needs"] == ["mirrors", "native"]
    assert verify["permissions"]["attestations"] == "write"
    assert verify["permissions"]["id-token"] == "write"
    assemble = next(i for i, step in enumerate(verify["steps"]) if step.get("id") == "prepared")
    attest = next(i for i, step in enumerate(verify["steps"]) if "subject-digest" in step.get("with", {}))
    assert assemble < attest
