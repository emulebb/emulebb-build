"""Qualification composition retains exact APK, native-peer and four-guest authorities."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from android_package_fixtures import SOURCE, VERSION, synthetic_bundle
from emule_workspace import android_apk_qualification as qualification, cli, release
from emule_workspace import android_apk_candidate as candidate_apk
from emule_workspace.android_apk import public_names
from test_android_apk_candidate import evidence as apk_evidence


@pytest.mark.parametrize("cpp", [False, True])
def test_peer_exports_only_canonically_verified_same_source_native_bytes_and_target(tmp_path, monkeypatch, cpp):
    artifacts, report, layout, _archive = synthetic_bundle(tmp_path / "peer", monkeypatch, abi="x86_64", cpp=cpp)
    monkeypatch.setattr(release, "_emulebb_rust_package_provenance", lambda *args: report["source"])
    monkeypatch.setattr(release, "_emulebb_rust_artifact_origin", lambda: {"origin": "local"})
    directory = qualification.prepare_peer(layout, artifacts=artifacts, abi="x86_64", version=VERSION,
        source_sha=SOURCE["emulebbRust"], peer_build_ref=SOURCE["emulebbBuild"])
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["abi"] == "x86_64" and manifest["target"] == "x86_64-linux-android"
    assert manifest["sourceRevision"] == SOURCE["emulebbRust"]
    assert set(manifest["files"]) == ({"emulebb-rust", "libc++_shared.so"} if cpp else {"emulebb-rust"})
    for name, value in manifest["files"].items():
        assert release._sha256(directory / name) == value["sha256"]
    with pytest.raises(RuntimeError, match="source authority"):
        qualification.prepare_peer(layout, artifacts=artifacts, abi="x86_64", version=VERSION,
            source_sha=SOURCE["emulebbRust"], peer_build_ref="a" * 40)


@pytest.mark.parametrize("missing", [False, True])
def test_aggregation_requires_all_four_actual_source_bound_api_receipts(apk_evidence, monkeypatch, missing):
    root, context, metadata, proof, _save = apk_evidence
    monkeypatch.setattr(qualification, "qualification_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(qualification, "apk_contract", lambda context: candidate_apk.apk_contract(context))
    monkeypatch.setattr(release, "_emulebb_rust_artifact_origin", lambda: {"origin": "github-actions", "workflow": context.workflow})
    path = root / public_names(context.version)["proof"]
    path.unlink()
    evidence = root.parent / "evidence"
    for item in proof["proofs"][:-1] if missing else proof["proofs"]:
        directory = evidence / str(item["guest"]["sdk"])
        directory.mkdir(parents=True)
        (directory / "validation.json").write_text(json.dumps(item))
    parameters = dict(payload=root, manifest_name=public_names(context.version)["manifest"],
        manifest_sha256=release._sha256(root / public_names(context.version)["manifest"]), evidence_dir=evidence,
        release_dir=root, source_sha=context.source["emulebbRust"], signer_sha256=context.apk_signer_sha256,
        version_code=2, version=context.version, build_ref=context.apk_sources.build_sha, harness_ref=context.apk_sources.harness_sha)
    if missing:
        with pytest.raises(RuntimeError, match="exactly four"):
            qualification.assemble(SimpleNamespace(output_root=root.parent), **parameters)
        assert not path.exists()
    else:
        assert qualification.assemble(SimpleNamespace(output_root=root.parent), **parameters) == path
        assert candidate_apk.verify_apk(root, context)["androidApis"] == [34, 35, 36, 37]


def test_hosted_qualifier_cli_passes_explicit_authorities_through_lock_and_reports_verified_output(tmp_path, monkeypatch):
    payload, peers = tmp_path / "payload", tmp_path / "peers"
    payload.mkdir()
    peers.mkdir()
    layout = object()
    monkeypatch.setattr(cli, "_apk_evidence_layout", lambda: (object(), layout))
    events = []
    def locked(name, operation):
        def invoke(**kwargs):
            events.append(name)
            return operation(**kwargs)
        return invoke
    monkeypatch.setattr(cli, "_locked", locked)
    def run(selected, **kwargs):
        assert selected is layout and events == ["qualify-rust-android-apk-ci"]
        assert kwargs["image"] == "apk35" and kwargs["version_code"] == 123
        assert kwargs["peer_build_ref"] == "4" * 40 and kwargs["build_ref"] == "5" * 40
        assert kwargs["harness_ref"] == "6" * 40 and kwargs["signer_sha256"] == "3" * 64
        return tmp_path / "validation.json"
    monkeypatch.setattr(qualification, "qualify", run)
    output = tmp_path / "github-output"
    arguments = ["qualify-rust-android-apk-ci", "--image", "apk35", "--apk-payload", str(payload),
        "--apk-manifest-name", "manifest.json", "--apk-manifest-sha256", "1" * 64, "--apk-source-sha", "2" * 40,
        "--expected-cert-sha256", "3" * 64, "--apk-version-code", "123", "--release-version", VERSION,
        "--peer-artifacts", str(peers), "--peer-build-ref", "4" * 40, "--apk-build-ref", "5" * 40,
        "--apk-harness-ref", "6" * 40, "--github-output", str(output)]
    result = CliRunner().invoke(cli.main, arguments)
    assert result.exit_code == 0, result.output
    assert output.read_text().strip() == "qualification_path=" + str(tmp_path / "validation.json")


def test_apk_browser_image_pins_match_the_dockerfile_and_hashed_dependency_authority():
    root = Path(qualification.__file__).parent / "release_assets/android"
    config = json.loads((root / "apk-browser.json").read_text())
    assert config["image"] in (root / "Dockerfile.apk-emulator").read_text()
    requirements = (root / "apk-browser-requirements.txt").read_text()
    assert "playwright==" + config["version"] in requirements
    assert "--hash=sha256:" in requirements and "../ci_linux/requirements.txt" in requirements
