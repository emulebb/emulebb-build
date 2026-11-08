"""Bind image builds to exact source bytes and clean up owned workers on failure."""
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from emule_workspace import rust_images


@pytest.fixture
def image_inputs(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assets = tmp_path / "assets"
    assets.mkdir()
    name = "emulebb-rust-v0.1.0-beta.2-linux-amd64.deb"
    (assets / name).write_bytes(b"payload")
    (assets / "package.spdx.json").write_bytes(b"{}")
    manifest = {"schema": "emulebb.rust.package/2", "sourceDirty": False, "version": "0.1.0-beta.2",
                "origin": "local-development", "workflow": None,
                "asset": name, "sha256": hashlib.sha256(b"payload").hexdigest(),
                "sbom": "package.spdx.json", "sbomSha256": hashlib.sha256(b"{}").hexdigest(),
                "source": {"emulebbRust": {"commit": "a" * 40}}}
    path = assets / (name + ".manifest.json")
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(rust_images, "git_output", lambda repo, *args: "" if args[0] == "status" else "a" * 40)
    return assets, path, manifest


@pytest.mark.parametrize("mutation", ["payload", "revision", "dirty", "sbom", "sbom-path", "old-schema", "missing-clean", "version"])
def test_image_rejects_changed_bytes_or_provenance(image_inputs, tmp_path, mutation):
    assets, path, manifest = image_inputs
    assert rust_images.verified_debs(tmp_path, assets, "0.1.0-beta.2", ("amd64",))
    if mutation == "payload":
        (assets / manifest["asset"]).write_bytes(b"changed payload")
    elif mutation == "revision":
        manifest["source"]["emulebbRust"]["commit"] = "b" * 40
    elif mutation == "dirty":
        manifest["sourceDirty"] = True
    elif mutation == "sbom":
        (assets / manifest["sbom"]).write_bytes(b"changed inventory")
    elif mutation == "old-schema":
        manifest["schema"] = "emulebb.rust.package/1"
    elif mutation == "missing-clean":
        del manifest["sourceDirty"]
    elif mutation == "version":
        manifest["version"] = "0.1.0-beta.1"
    else:
        manifest["sbom"] = "../outside.spdx.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError):
        rust_images.verified_debs(tmp_path, assets, "0.1.0-beta.2", ("amd64",))


def test_image_rejects_dirty_checkout_and_missing_architecture(image_inputs, tmp_path, monkeypatch):
    assets, _, _ = image_inputs
    with pytest.raises(RuntimeError, match="missing"):
        rust_images.verified_debs(tmp_path, assets, "0.1.0-beta.2", ("amd64", "arm64"))
    monkeypatch.setattr(rust_images, "git_output", lambda *args: " M source.rs")
    with pytest.raises(RuntimeError, match="clean current"):
        rust_images.verified_debs(tmp_path, assets, "0.1.0-beta.2", ("amd64",))


@pytest.fixture
def hosted_image_inputs(image_inputs, monkeypatch):
    assets, path, manifest = image_inputs
    context = {"repository": "emulebb/emulebb-rust", "runId": "123", "runAttempt": "3", "workflowSha": "b" * 40}
    for name, value in {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": context["repository"],
                        "GITHUB_RUN_ID": context["runId"], "GITHUB_RUN_ATTEMPT": context["runAttempt"],
                        "GITHUB_WORKFLOW_SHA": context["workflowSha"]}.items():
        monkeypatch.setenv(name, value)
    manifest.update(origin="github-actions", workflow={**context, "runAttempt": "2"})
    manifest["source"]["emulebbBuild"] = {"commit": "a" * 40}
    path.write_text(json.dumps(manifest))
    return assets, path, manifest, context


def test_hosted_image_accepts_an_earlier_attempt_from_the_same_exact_run(hosted_image_inputs, tmp_path):
    assets, path, manifest, _ = hosted_image_inputs
    result = rust_images.verified_debs(tmp_path, assets, "0.1.0-beta.2", ("amd64",))
    assert result[0]["workflow"]["runAttempt"] == "2"
    assert result[0]["manifestSha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("fault", ["local-origin", "wrong-repo", "wrong-run", "wrong-workflow", "future-attempt",
                                  "invalid-attempt", "missing-workflow", "old-builder"])
def test_hosted_image_rejects_foreign_or_stale_production(hosted_image_inputs, tmp_path, fault):
    assets, path, manifest, _ = hosted_image_inputs
    if fault == "local-origin":
        manifest["origin"] = "local-development"
    elif fault == "old-builder":
        manifest["source"]["emulebbBuild"]["commit"] = "c" * 40
    elif fault == "missing-workflow":
        del manifest["workflow"]
    else:
        key, value = {"wrong-repo": ("repository", "example/other"), "wrong-run": ("runId", "124"),
                      "wrong-workflow": ("workflowSha", "c" * 40), "future-attempt": ("runAttempt", "4"),
                      "invalid-attempt": ("runAttempt", True)}[fault]
        manifest["workflow"][key] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="Artifact must originate|pinned current package builder"):
        rust_images.verified_debs(tmp_path, assets, "0.1.0-beta.2", ("amd64",))


def test_hosted_oci_digest_uses_only_the_clean_workflow_pinned_control_code(hosted_image_inputs, tmp_path, monkeypatch):
    _, _, _, context = hosted_image_inputs
    candidate = tmp_path / "candidate"
    control = tmp_path / "control"
    for root in (candidate, control):
        (root / "tools").mkdir(parents=True)
    (candidate / "tools/release_publish.py").write_text('raise AssertionError("candidate code must not execute")\n')
    (control / "tools/release_publish.py").write_text('def oci_archive_digest(path):\n    return "sha256:" + "c" * 64\n')
    monkeypatch.setattr(rust_images, "git_output", lambda repo, *args:
                        "" if args[0] == "status" else context["workflowSha"] if repo == control else "a" * 40)
    assert rust_images.archive_digest(candidate, tmp_path / "image", control_rust_root=control) == "sha256:" + "c" * 64
    with pytest.raises(RuntimeError, match="workflow SHA"):
        rust_images.archive_digest(candidate, tmp_path / "image", control_rust_root=candidate)
    with pytest.raises(RuntimeError, match="workflow SHA"):
        rust_images.archive_digest(candidate, tmp_path / "image")


@pytest.mark.parametrize("fault", ["timeout", "builder-moved"])
def test_failed_image_build_invalidates_old_sidecars_and_removes_only_its_worker(tmp_path, monkeypatch, fault):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    rust = tmp_path / "rust"
    (rust / "packaging/docker").mkdir(parents=True)
    (rust / "packaging/docker/Dockerfile").write_text("FROM scratch\n")
    output = tmp_path / "output"
    assets = output / "assets"
    assets.mkdir(parents=True)
    archive = output / "artifacts/emulebb-rust-v0.1.0-beta.2-multiarch.oci.tar"
    archive.parent.mkdir()
    previous = (archive, archive.with_name(archive.name + ".manifest.json"), archive.with_suffix(".digest"))
    for path in previous:
        path.write_text("stale successful candidate")
    calls = []
    revision = "a" * 40
    class Docker:
        prefix = ["wsl.exe", "-d", "Ubuntu", "--"]
        def __init__(self, *args):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def path(self, value):
            return str(value)
        def run(self, *arguments, **kwargs):
            calls.append(list(arguments))
    def run(command, **kwargs):
        nonlocal revision
        calls.append(command)
        if "--output" in command:
            kwargs["stdout"].write("retained build diagnostics\n")
            if fault == "timeout":
                raise subprocess.TimeoutExpired(command, 1)
            archive.write_bytes(b"new archive")
            revision = "b" * 40
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(rust_images, "LinuxDocker", Docker)
    monkeypatch.setattr(rust_images, "verified_debs", lambda *args: [])
    monkeypatch.setattr(rust_images, "git_output", lambda repo, *args: "" if args[0] == "status" else revision)
    monkeypatch.setattr(rust_images.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="diagnostics"):
        rust_images.build_image(rust_root=rust, output_root=output, assets_dir=assets,
                                version="0.1.0-beta.2", image="local/test:beta")
    name = calls[0][calls[0].index("--name") + 1]
    assert name.startswith("emulebb-image-")
    assert f"image={rust_images.BUILDKIT_IMAGE}" in calls[0]
    assert calls[-1][-3:] == ["rm", "--force", name]
    report_path = next((output / "reports/rust-image-build").glob("*/inputs.json"))
    assert json.loads(report_path.read_text())["status"] == "failed"
    assert report_path.with_name("build.log").read_text() == "retained build diagnostics\n"
    assert not any(path.exists() for path in previous[1:])
    build = next(command for command in calls if "--output" in command)
    assert "--no-cache" in build and "--cache-from" not in build


@pytest.mark.parametrize("fault", [None, "timeout", "old-schema", "wrong-version", "root-daemon"])
def test_fresh_wsl_smoke_requires_current_release_proof_and_removes_only_labeled_workloads(tmp_path, monkeypatch, fault):
    output = tmp_path / "output"
    output.mkdir()
    archive = output / "image.oci.tar"
    archive.write_bytes(b"archive")
    image = "local/test:beta"
    manifest = {"schema": "emulebb.rust.image/2", "image": image, "sourceSha": "a" * 40, "version": "0.1.0-beta.2",
                "architectures": ["amd64"], "archiveSha256": hashlib.sha256(b"archive").hexdigest()}
    archive.with_name(archive.name + ".manifest.json").write_text(json.dumps(manifest))
    layout = SimpleNamespace(output_root=output, emulebb_rust_repo_root=tmp_path / "rust", tests_repo_root=tmp_path / "harness")
    calls = []
    class Docker:
        prefix = ["wsl.exe", "-d", "Ubuntu", "--"]
        def __init__(self, *args):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def path(self, value):
            return str(value)
        def run(self, *arguments, **kwargs):
            calls.append(list(arguments))
            return SimpleNamespace(stdout="a" * 12 + "\n" if arguments[0] == "ps" else
                                   "b" * 12 + "\n" if arguments[:2] == ("network", "ls") else "")
    def run(command, **kwargs):
        calls.append(command)
        kwargs["stdout"].write("retained runtime diagnostics\n")
        if fault == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        result = {"schema": "emulebb.rust.image-smoke/4", "status": "passed", "archiveSha256": manifest["archiveSha256"],
                  "platforms": [{"platform": "linux/amd64", "appVersion": manifest["version"], "appBuild": "release",
                                 "appPlatform": "x64", "authenticatedStatus": 200, "unauthenticatedStatus": 401,
                                 "daemonUid": 1234, "daemonGid": 1235, "gracefulShutdown": True}]}
        if fault == "old-schema":
            result["schema"] = "emulebb.rust.image-smoke/3"
        elif fault == "wrong-version":
            result["platforms"][0]["appVersion"] = "0.1.0-beta.1"
        elif fault == "root-daemon":
            result["platforms"][0]["daemonUid"] = 0
        Path(command[command.index("--report-path") + 1]).write_text(json.dumps(result))
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(rust_images, "git_output", lambda repo, *args: "" if args[0] == "status" else "a" * 40)
    monkeypatch.setattr(rust_images, "LinuxDocker", Docker)
    monkeypatch.setattr(rust_images.subprocess, "run", run)
    if fault is None:
        rust_images.smoke_image(layout, archive=archive, image=image, architectures=("amd64",))
    else:
        with pytest.raises(RuntimeError, match="Image smoke failed"):
            rust_images.smoke_image(layout, archive=archive, image=image, architectures=("amd64",))
    owner = calls[0][calls[0].index("--run-id") + 1]
    assert f"label=io.emulebb.ci.image-smoke-owner={owner}" in calls[1]
    assert ["rm", "--force", "a" * 12] in calls
    assert calls[-1] == ["network", "rm", "b" * 12]
    report = next((output / "reports/rust-image-smoke").glob("*/inputs.json"))
    assert json.loads(report.read_text())["status"] == ("failed" if fault else "passed")
    assert report.with_name("smoke.log").read_text() == "retained runtime diagnostics\n"


def test_local_deb_collection_requires_complete_current_source_inputs(image_inputs, tmp_path):
    assets, _, _ = image_inputs
    version = "0.1.0-beta.2"
    source = tmp_path / "packages/rust-linux" / version / "gnu-amd64"
    source.parent.mkdir(parents=True)
    assets.rename(source)
    layout = SimpleNamespace(output_root=tmp_path, emulebb_rust_repo_root=tmp_path / "rust")
    destination = rust_images.collect_linux_debs(layout, version=version, architectures=("amd64",))
    assert (destination / f"emulebb-rust-v{version}-linux-amd64.deb").read_bytes() == b"payload"
    assert rust_images.verified_debs(layout.emulebb_rust_repo_root, destination, version, ("amd64",))
    with pytest.raises(RuntimeError, match="missing"):
        rust_images.collect_linux_debs(layout, version=version, architectures=("amd64", "arm64"))
