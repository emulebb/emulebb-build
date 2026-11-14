"""Experimental Android archives retain exact bytes and honest compile-only claims."""
import json
import shutil

import pytest
from android_package_fixtures import SOURCE, VERSION, WORKFLOW, synthetic_bundle
from emule_workspace import android_package as package, release_candidate as candidate, release
from emule_workspace.android_validation import needed_libraries


def context():
    return candidate.CandidateContext(VERSION, "nightly",
        {**SOURCE, "emulebbBuildTests": "3" * 40}, {**WORKFLOW, "runAttempt": "2"})


def stage_pair(tmp_path, monkeypatch, *, cpp=False):
    public = tmp_path / "public"
    public.mkdir()
    for abi in package.TARGETS:
        directory, _report, _layout, _archive = synthetic_bundle(tmp_path / abi, monkeypatch, abi=abi, cpp=cpp)
        for name in package.names(VERSION, abi).values():
            shutil.copy2(directory / name, public / name)
    return public


@pytest.mark.parametrize("cpp", [False, True])
def test_android_both_abis_are_bound_without_claiming_runtime(tmp_path, monkeypatch, cpp):
    root = stage_pair(tmp_path, monkeypatch, cpp=cpp)
    result = candidate._android_packages(root, context())
    assert {item["abi"] for item in result} == set(package.TARGETS)
    assert all(item["runtimeExecution"] == "not-run" for item in result)
    assert all(item["asset"].endswith(".tar.zst") for item in result)


@pytest.mark.parametrize("field,value", [
    ("minApi", 27), ("pageSize", 4096), ("channel", "development"),
    ("runtimeExecution", "passed"), ("abi", "armeabi-v7a"), ("experimental", False),
])
def test_android_rejects_false_platform_and_runtime_claims(tmp_path, monkeypatch, field, value):
    root = stage_pair(tmp_path, monkeypatch)
    name = package.names(VERSION, "arm64-v8a")["manifest"]
    metadata = json.loads((root/name).read_text())
    metadata[field] = value
    (root/name).write_text(json.dumps(metadata))
    with pytest.raises(RuntimeError):
        candidate._android_packages(root, context())


def test_android_missing_proof_and_changed_payload_fail_closed(tmp_path, monkeypatch):
    root = stage_pair(tmp_path, monkeypatch)
    names = package.names(VERSION, "arm64-v8a")
    (root/names["archive"]).write_bytes(b"changed")
    with pytest.raises(RuntimeError):
        candidate._android_packages(root, context())
    (root/names["proof"]).unlink()
    with pytest.raises(RuntimeError):
        candidate._android_packages(root, context())


def test_unresolved_needed_library_is_not_called_system():
    text = "Dynamic section\n(NEEDED) Shared library: [libprivate.so]"
    assert needed_libraries(text) == ["libprivate.so"]
    with pytest.raises(RuntimeError):
        needed_libraries("unrelated successful command output")


@pytest.mark.parametrize("abi", ["arm64-v8a", "x86_64"])
def test_version_is_compiled_once_before_export(tmp_path, monkeypatch, abi):
    directory, report, layout, archive = synthetic_bundle(tmp_path/"fixture", monkeypatch, abi=abi)
    marker = tmp_path / "validation.json"
    marker.write_text(json.dumps(report))
    monkeypatch.setattr(release, "_emulebb_rust_workspace_version", lambda root: "0.1.0-beta.2")
    calls = []
    def validate(*args, **kwargs):
        calls.append(kwargs)
        return marker
    monkeypatch.setattr(package, "validate_android", validate)
    monkeypatch.setattr(package, "write_bundle", lambda *args, **kwargs: archive)
    monkeypatch.setattr(package.subprocess, "run", lambda *args, **kwargs: None)
    result = package.package_android(layout, abi=abi, release_version=VERSION, channel="nightly",
                                     expected_source_sha=SOURCE["emulebbRust"])
    assert result == archive
    assert len(calls) == 1
    assert calls[0]["release_version"] == VERSION and calls[0]["channel"] == "nightly"
    assert calls[0]["expected_source_sha"] == SOURCE["emulebbRust"]


def test_nightly_inventory_adds_android_without_replacing_existing_proofs():
    nightly = candidate.public_names(context())
    ordinary = candidate.public_names(candidate.CandidateContext("0.1.0-beta.2", "candidate",
                                                                 context().source, context().workflow))
    assert len(candidate.package_layout(context())) == 8
    assert sum("android-" in name for name in nightly) == 8
    assert not any("android-" in name for name in ordinary)
    assert not any("package-smoke-android" in name for name in nightly)


@pytest.mark.parametrize("fault", ["failed-check", "missing-test-link", "wrong-source", "unresolved-library"])
def test_android_proof_failures_cannot_be_hidden_by_refreshing_checksums(tmp_path, monkeypatch, fault):
    root = stage_pair(tmp_path, monkeypatch)
    names = package.names(VERSION, "arm64-v8a")
    manifest = json.loads((root/names["manifest"]).read_text())
    proof = json.loads((root/names["proof"]).read_text())
    if fault == "failed-check":
        proof["checks"][1]["status"] = "failed"
    elif fault == "missing-test-link":
        proof["checks"] = [item for item in proof["checks"] if "workspace-test-link" not in item["label"]]
    elif fault == "wrong-source":
        proof["source"]["emulebbRust"]["commit"] = "9" * 40
    else:
        runtime = {"needed": ["libprivate.so"], "readelf": "Dynamic section\n(NEEDED) Shared library: [libprivate.so]",
                   "bundled": []}
        manifest["runtimeDependencies"] = proof["runtimeDependencies"] = runtime
    (root/names["proof"]).write_text(json.dumps(proof))
    manifest["compileProofSha256"] = release._sha256(root/names["proof"])
    (root/names["manifest"]).write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError):
        candidate._android_packages(root, context())


def test_hosted_android_package_command_keeps_source_identity_and_lock(tmp_path, monkeypatch):
    import os
    from click.testing import CliRunner
    from emule_workspace import cli
    layout = object()
    monkeypatch.setattr(cli, "_android_ci_layout", lambda: (object(), layout))
    called = []
    def locked(name, function):
        def invoke(**kwargs):
            called.append(name)
            return function(**kwargs)
        return invoke
    monkeypatch.setattr(cli, "_locked", locked)
    def produce(selected, **kwargs):
        assert selected is layout
        called.append(kwargs)
        return tmp_path/"developer.tar.zst"
    monkeypatch.setattr(package, "package_android", produce)
    before = os.environ.copy()
    result = CliRunner().invoke(cli.main, ["package-rust-android-ci", "--abi", "arm64-v8a",
        "--release-version", VERSION, "--channel", "nightly", "--expected-source-sha", SOURCE["emulebbRust"]])
    assert result.exit_code == 0, result.output
    assert called[0] == "package-rust-android-ci"
    assert called[1] == {"abi": "arm64-v8a", "release_version": VERSION, "channel": "nightly",
                         "expected_source_sha": SOURCE["emulebbRust"]}
    assert os.environ == before


def test_local_android_package_reuses_existing_producer_without_changing_environment(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    from click.testing import CliRunner
    from emule_workspace import cli
    layout = object()
    options = SimpleNamespace(workspace_root=tmp_path, output_root=tmp_path / "output", workspace_name="workspace")
    monkeypatch.setattr(cli, "resolve_workspace_options", lambda **kwargs: options)
    monkeypatch.setattr(cli, "load_layout", lambda *args, **kwargs: layout)
    events = []
    def locked(name, operation):
        def invoke(**kwargs):
            events.append(name)
            return operation(**kwargs)
        return invoke
    monkeypatch.setattr(cli, "_locked", locked)
    def produce(selected, **kwargs):
        assert selected is layout and events == ["package-rust-android"]
        assert kwargs == {"abi": "arm64-v8a", "release_version": VERSION, "channel": "nightly", "expected_source_sha": SOURCE["emulebbRust"]}
        return tmp_path / "peer.tar.zst"
    monkeypatch.setattr(package, "package_android", produce)
    before = os.environ.copy()
    result = CliRunner().invoke(cli.main, ["package-rust-android", "--abi", "arm64-v8a", "--release-version", VERSION,
        "--channel", "nightly", "--expected-source-sha", SOURCE["emulebbRust"]])
    assert result.exit_code == 0, result.output
    assert os.environ == before
