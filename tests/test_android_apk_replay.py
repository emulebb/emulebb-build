"""Diagnostic old-byte replay keeps original provenance separate from execution."""
import io
import json
from dataclasses import replace
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from click.testing import CliRunner

from emule_workspace import android_apk_qualification as replay, cli, release
from emule_workspace.android_apk import public_names
from test_android_apk_candidate import evidence as apk_evidence
from emule_workspace import android_apk_candidate as candidate_apk


@pytest.mark.parametrize("fault", [None, "digest", "source", "run", "path", "duplicate"])
def test_artifact_envelope_digest_and_flat_inventory_are_verified_before_use(tmp_path, monkeypatch, fault):
    memory = io.BytesIO()
    with pytest.warns(UserWarning, match="Duplicate name") if fault == "duplicate" else nullcontext():
        with zipfile.ZipFile(memory, "w") as archive:
            for name in ("one", "two", "../three" if fault == "path" else "two" if fault == "duplicate" else "three"):
                archive.writestr(name, b"synthetic public bytes")
    content = memory.getvalue()
    import hashlib
    artifact = {"id": 12, "name": "payload", "expired": False, "size_in_bytes": len(content),
        "digest": "sha256:" + hashlib.sha256(content).hexdigest(), "workflow_run": {"head_sha": "1" * 40, "id": 123}}
    if fault == "digest": artifact["digest"] = "sha256:" + "0" * 64
    if fault == "source": artifact["workflow_run"]["head_sha"] = "2" * 40
    if fault == "run": artifact["workflow_run"]["id"] = 124
    def download(command, **kwargs):
        assert command == ["gh", "api", "repos/emulebb/emulebb-rust/actions/artifacts/12/zip"]
        kwargs["stdout"].write(content)
    monkeypatch.setattr(replay.subprocess, "run", download)
    directory = tmp_path / "payload"
    parameters = dict(repository="emulebb/emulebb-rust", source_sha="1" * 40, run_id="123")
    if fault is not None:
        with pytest.raises(RuntimeError): replay._download_diagnostic_artifact(artifact, directory, **parameters)
        assert not (directory / "one").exists()
    else:
        assert replay._download_diagnostic_artifact(artifact, directory, **parameters)["id"] == 12
        assert {path.name for path in directory.iterdir()} == {"one", "two", "three"}


@pytest.mark.parametrize("fault", [None, "original-run", "producer-job", "manifest", "support-pin"])
def test_replay_admits_original_authority_then_calls_existing_guest_with_distinct_executor(apk_evidence, tmp_path, monkeypatch, fault):
    root, original, metadata, _proof, _save = apk_evidence
    repository = "emulebb/emulebb-rust"
    original = replace(original, workflow={**original.workflow, "repository": repository,
        "workflowSha": original.source["emulebbRust"]})
    metadata["workflow"] = dict(original.workflow)
    names = public_names(original.version)
    (root / names["manifest"]).write_text(json.dumps(metadata))
    checksum = release._sha256(root / names["manifest"])
    source_sha = original.source["emulebbRust"]
    current_workflow = {**original.workflow, "runId": "987", "workflowSha": "f" * 40}
    current_materials = {**original.apk_materials, "emulebbBuild": "a" * 40}
    layout = SimpleNamespace(output_root=tmp_path / "output", emulebb_rust_repo_root=tmp_path / "rust", tests_repo_root=tmp_path / "harness")
    pins = {**original.source, "apkBuild": original.apk_sources.build_sha,
        "emulebbBuildTests": original.apk_sources.harness_sha}
    if fault == "support-pin": pins["emulebbTooling"] = "0" * 40
    monkeypatch.setattr(replay, "_original_pins", lambda _: pins)
    monkeypatch.setattr(replay, "_checked_checkout", lambda path, *_args: path)
    monkeypatch.setattr(release, "_emulebb_rust_artifact_origin", lambda: {"origin": "github-actions", "workflow": current_workflow})
    monkeypatch.setattr(release, "_emulebb_rust_package_provenance", lambda *args: {"source": {name: {"commit": sha} for name, sha in current_materials.items()}})
    monkeypatch.setattr(release, "_emulebb_rust_workspace_version", lambda _: original.version.split(".nightly.")[0])
    artifacts = [{"name": name} for name in ("emulebb-rust-android-apk-payload", "emulebb-rust-android-developer-payload-x86_64")]
    def github(endpoint):
        if "/jobs?" in endpoint:
            return {"total_count": 2, "jobs": [{"name": name, "status": "completed", "conclusion": "failure" if fault == "producer-job" else "success"}
                for name in ("Build and optionally publish nightly / Signed universal Android application",
                             "Build and optionally publish nightly / Experimental Android developer bundle (x86_64)")]}
        if "/artifacts?" in endpoint: return {"total_count": 2, "artifacts": artifacts}
        return {"head_sha": "0" * 40 if fault == "original-run" else source_sha,
            "repository": {"full_name": repository}, "head_repository": {"full_name": repository},
            "path": ".github/workflows/nightly.yml", "status": "completed", "run_attempt": int(original.workflow["runAttempt"])}
    monkeypatch.setattr(replay, "_github_json", github)
    def download(artifact, destination, **kwargs):
        destination.mkdir()
        if destination.name == "apk":
            for name in (names["apk"], names["manifest"], names["sbom"]):
                (destination / name).write_bytes((root / name).read_bytes())
        return artifact
    monkeypatch.setattr(replay, "_download_diagnostic_artifact", download)
    def peer(_layout, **kwargs):
        assert kwargs["producer_context"].workflow == original.workflow
        assert kwargs["peer_build_ref"] == pins["emulebbBuild"]
        path = tmp_path / "flattened-peer"
        path.mkdir()
        (path / "manifest.json").write_text("{}")
        return path
    monkeypatch.setattr(replay, "prepare_peer", peer)
    executor = SimpleNamespace(workflow=current_workflow)
    monkeypatch.setattr(replay, "qualification_context", lambda *args, **kwargs: executor)
    from emule_workspace import android_toolchain
    def probe(_layout, **kwargs):
        payload = kwargs["apk_payload"]
        assert payload["context"] is executor and payload["producerContext"].workflow == original.workflow
        assert payload["diagnosticReplay"]["publicationEligible"] is False
        assert payload["diagnosticReplay"]["producerWorkflow"] != payload["diagnosticReplay"]["executorWorkflow"]
        assert payload["metadata"]["apk"]["sha256"] == metadata["apk"]["sha256"]
        return tmp_path / "diagnostic.json"
    monkeypatch.setattr(android_toolchain, "probe_emulator_guest", probe)
    arguments = dict(producer_run_id=original.workflow["runId"], producer_source_sha=source_sha,
        manifest_sha256="0" * 64 if fault == "manifest" else checksum, image="apk36",
        signer_sha256=original.apk_signer_sha256, build_ref="a" * 40, harness_ref="b" * 40)
    if fault is not None:
        with pytest.raises(RuntimeError): replay.diagnose(layout, **arguments)
    else:
        assert replay.diagnose(layout, **arguments) == tmp_path / "diagnostic.json"


def test_production_candidate_rejects_diagnostic_scope_even_with_all_runtime_cases_passed(apk_evidence):
    _root, context, metadata, proof, _save = apk_evidence
    value = proof["proofs"][0]
    value.update(scope="apk-diagnostic-replay", applicationQualification="not-run", publicationEligible=False)
    with pytest.raises(RuntimeError, match="APK guest receipt"):
        candidate_apk.validate_guest(value, context=context, identity=metadata["buildIdentity"], apk_sha256=metadata["apk"]["sha256"],
            manifest_sha256=value["apkManifestSha256"], api=value["guest"]["sdk"], contract=candidate_apk.apk_contract(context))


def test_diagnostic_cli_has_only_diagnostic_output_and_reuses_workspace_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_apk_evidence_layout", lambda: (object(), object()))
    events = []
    def locked(name, operation):
        def invoke(**kwargs):
            events.append(name)
            return operation(**kwargs)
        return invoke
    monkeypatch.setattr(cli, "_locked", locked)
    monkeypatch.setattr(replay, "diagnose", lambda *args, **kwargs: tmp_path / "validation.json")
    output = tmp_path / "outputs"
    result = CliRunner().invoke(cli.main, ["diagnose-rust-android-apk-ci", "--producer-run-id", "123",
        "--producer-source-sha", "1" * 40, "--apk-manifest-sha256", "2" * 64, "--image", "apk36",
        "--expected-cert-sha256", "3" * 64, "--diagnostic-build-ref", "4" * 40,
        "--diagnostic-harness-ref", "5" * 40, "--github-output", str(output)])
    assert result.exit_code == 0, result.output
    assert events == ["diagnose-rust-android-apk-ci"]
    assert output.read_text() == "diagnostic_path=" + str(tmp_path / "validation.json") + "\n"
