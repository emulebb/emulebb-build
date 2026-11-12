"""Hosted guest validation shares the local owner and exposes only public proof."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from emule_workspace import android_toolchain as subject
from emule_workspace import config, linux_containers


def test_kvm_absence_fails_before_downloading_or_building(monkeypatch):
    monkeypatch.setattr(subject.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    inspect = Mock(side_effect=AssertionError("no further commands"))
    monkeypatch.setattr(subject.subprocess, "check_output", inspect)
    with pytest.raises(RuntimeError, match="requires /dev/kvm"):
        subject._kvm_group(SimpleNamespace(prefix=[]))
    inspect.assert_not_called()


@pytest.mark.parametrize("group", ["", "kvm", "-1", "100\n200"])
def test_kvm_group_must_be_numeric(monkeypatch, group):
    monkeypatch.setattr(subject.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(subject.subprocess, "check_output", lambda *a, **k: group)
    with pytest.raises(RuntimeError, match="numeric group"):
        subject._kvm_group(SimpleNamespace(prefix=[]))


@pytest.mark.parametrize("outcome", ["success", "failure", "mismatch", "source-change", "private-extra",
                                     "tls-success", "tls-missing", "tls-names", "tls-ignored", "tls-source",
                                     "tls-bool", "tls-pre-source", "tls-post-source", "tls-manifest",
                                     "tls-startup-missing", "tls-startup-source", "tls-startup-auth", "tls-startup-shutdown", "tls-old-schema"])
@pytest.mark.parametrize("image,api,page", [("min", 28, 4096), ("current", 37, 16384)])
def test_shared_boot_owner_enforces_identity_isolation_and_source_freeze(tmp_path, monkeypatch, outcome, image, api, page):
    from emule_workspace import release

    workspace, output = tmp_path / "workspace", tmp_path / "output"
    harness = workspace / "repos/emulebb-build-tests"
    (harness / "scripts").mkdir(parents=True)
    (harness / "scripts/probe-android-emulator.py").touch()
    output.mkdir()
    layout = SimpleNamespace(output_root=output, tests_repo_root=harness)
    tls = outcome.startswith("tls-")
    if tls:
        from emule_workspace import android_validation
        monkeypatch.setattr(subject.shutil, "disk_usage", lambda path: SimpleNamespace(free=30 * 1024**3))
        layout.emulebb_rust_repo_root = workspace / "repos/emulebb-rust"
        payload_root = output / "tls-payload"
        payload_root.mkdir()
        payload = {"schema": "emulebb.android.tls-payload/2", "status": "passed", "source": {"emulebbRust": {"commit": "5" * 40}},
                   "files": {"core-tests": {"sha256": "6" * 64}, "daemon-tests": {"sha256": "7" * 64}, "emulebb-rust": {"sha256": "8" * 64}},
                   "buildIdentity": {"version": "0.1.0-beta.2", "channel": "development", "sourceRevision": "5" * 40, "platform": "x64", "build": "release"}}
        manifest = payload_root / "manifest.json"
        manifest.write_text(json.dumps(payload))
        compiled = output / "compile.json"
        compiled.write_text(json.dumps({"status": "passed", "source": {},
            "tlsPayload": {"path": "tls-payload/manifest.json", "sha256": release._sha256(manifest)}}))
        compile_calls = Mock(return_value=compiled)
        monkeypatch.setattr(android_validation, "validate_android", compile_calls)
        source_check = Mock()
        if outcome == "tls-pre-source": source_check.side_effect = RuntimeError("source moved before guest")
        if outcome == "tls-post-source": source_check.side_effect = [None, RuntimeError("source moved after guest")]
        monkeypatch.setattr(release, "_assert_emulebb_rust_package_provenance", source_check)
    monkeypatch.setattr(config, "resolve_required_workspace_roots", lambda: (workspace, output))
    source = {"build": {"commit": "1" * 40}, "harness": {"commit": "2" * 40}}
    provenance = Mock(return_value=source)
    if outcome == "source-change":
        provenance.side_effect = [source, source, {**source, "harness": {"commit": "3" * 40}}]
    monkeypatch.setattr(subject, "_guest_sources", provenance)
    monkeypatch.setattr(subject, "_kvm_group", lambda docker: "108")
    monkeypatch.setattr(release, "_emulebb_rust_artifact_origin", lambda: {"origin": "local-development", "workflow": None})
    docker = Mock()
    docker.__enter__ = Mock(return_value=docker)
    docker.__exit__ = Mock(return_value=False)
    docker.run.return_value = SimpleNamespace(stdout="amd64")
    docker.prefix = []
    docker.path.side_effect = lambda p: str(p)
    monkeypatch.setattr(linux_containers, "LinuxDocker", lambda distro: docker)
    monkeypatch.setattr(linux_containers, "_builder_user", lambda docker: "1000:1000")
    tool_report = output / "tool-report.json"
    image_id = "sha256:" + "4" * 64
    tool_report.write_text(json.dumps({"image": {"id": image_id}, "runtimeExecution": "not-run",
        "inputs": subject.emulator_subset(subject.emulator_inputs(), (image,))}))
    prepare = Mock(return_value=tool_report)
    monkeypatch.setattr(subject, "prepare_emulator_toolchain", prepare)
    captured = []

    def run(docker, arguments, log, *, timeout):
        captured.extend(arguments)
        assert timeout == (900 if tls else 480)
        if outcome == "failure":
            raise RuntimeError("private command output")
        guest = log.parent / "guest"
        guest.mkdir()
        value = {"schema": "emulebb.android.emulator-boot/1", "status": "passed", "scope": "guest-boot",
                 "applicationQualification": "not-run", "kvm": True,
                 "guest": {"sdk": api, "pageSize": (16384 if page == 4096 else 4096) if outcome == "mismatch" else page,
                     "codename": "REL", "machine": "x86_64", "emulator": True,
                     "supported64BitAbis": ["x86_64"]}}
        if outcome == "private-extra":
            value["guest"]["localAuthentication"] = "synthetic-private-token"
        if tls and outcome != "tls-missing":
            value["tls"] = {"schema": "emulebb.android.tls-execution/2", "status": "passed",
                "source": payload["source"] if outcome != "tls-source" else {}, "files": payload["files"],
                "freshProfileRestReadiness": "passed", "apkQualification": "not-run",
                "startup": {"schema": "emulebb.android.first-launch/1", "status": "passed",
                    "app": {**payload["buildIdentity"], "name": "eMuleBB", "apiVersion": "v1", "lifecycleState": "running"},
                    "profilePlacement": "executable-directory", "callerCwdDifferent": True,
                    "bootstrapSnakeCase": True, "bootstrapOwnerOnly": True,
                    "unauthenticatedStatus": 401, "authenticatedStatus": 200,
                    "shutdown": {"signal": "SIGTERM", "identityVerified": True, "exitCode": 0, "graceful": True, "forwardRemoved": True},
                    "webuiReadiness": "not-run", "apkQualification": "not-run", "physicalDeviceQualification": "not-run"},
                "cases": [{"name": name, "passed": 1, "failed": 0, "ignored": 0} for name in sorted(subject.TLS_CASE_NAMES)]}
            if outcome == "tls-names": value["tls"]["cases"][0]["name"] = "other-test"
            if outcome == "tls-ignored": value["tls"]["cases"][0]["ignored"] = 1
            if outcome == "tls-bool": value["tls"]["cases"][0]["failed"] = False
            if outcome == "tls-manifest": manifest.write_text(manifest.read_text() + "\n")
            if outcome == "tls-old-schema": value["tls"]["schema"] = "emulebb.android.tls-execution/1"
            if outcome == "tls-startup-missing": del value["tls"]["startup"]
            if outcome == "tls-startup-source": value["tls"]["startup"]["app"]["sourceRevision"] = "9" * 40
            if outcome == "tls-startup-auth": value["tls"]["startup"]["authenticatedStatus"] = 401
            if outcome == "tls-startup-shutdown": value["tls"]["startup"]["shutdown"]["exitCode"] = 1
        (guest / "report.json").write_text(json.dumps(value))

    monkeypatch.setattr(linux_containers, "run_owned_container", run)
    before = os.environ.copy()
    extra = {"tls_startup": True} if tls else {}
    if outcome in {"success", "private-extra", "tls-success"}:
        subject.probe_emulator_guest(layout, image=image, **extra)
    else:
        with pytest.raises(RuntimeError):
            subject.probe_emulator_guest(layout, image=image, **extra)
    assert os.environ == before
    if outcome == "tls-pre-source":
        assert not captured
        result = json.loads(next((output / "reports/android-emulator-boot").rglob("validation.json")).read_text())
        assert result["status"] == "failed" and result["tls"]["status"] == "failed"
        return
    assert captured[captured.index("--device") + 1] == "/dev/kvm"
    assert captured[captured.index("--group-add") + 1] == "108"
    assert captured[captured.index("--user") + 1] == "1000:1000"
    assert captured[captured.index("--network") + 1] == "none"
    assert "--read-only" in captured and "ALL" in captured and "no-new-privileges" in captured
    assert image_id in captured and "--privileged" not in captured
    assert str(harness) + ",target=/harness,readonly" in " ".join(captured)
    reports = list(output.glob("reports/android-emulator-boot/*/validation.json"))
    assert len(reports) == 1
    public = reports[0].read_text()
    value = json.loads(public)
    assert value["status"] == ("passed" if outcome in {"success", "private-extra", "tls-success"} else "failed")
    assert value["applicationQualification"] == "not-run" and value["source"] == source
    assert "synthetic-private-token" not in public and "localAuthentication" not in public and str(tmp_path) not in public
    if tls:
        compile_calls.assert_called_once_with(layout, abis=("x86_64",), wsl_distro="Ubuntu", tls_payload=True)
        assert "--tls-source-sha" in captured and "--tls-manifest-sha256" in captured
        assert str(payload_root) + ",target=/tls-payload,readonly" in " ".join(captured)
        assert value["tls"]["status"] == ("passed" if outcome == "tls-success" else "failed")
    prepare.assert_called_once_with(layout, images=(image,), wsl_distro="Ubuntu")


def test_guest_sources_reject_dirty_or_changed_revisions(tmp_path, monkeypatch):
    from emule_workspace import git

    monkeypatch.setattr(git, "git_output", lambda path, *a: " M script.py" if a[0] == "status" else "1" * 40)
    with pytest.raises(RuntimeError, match="clean build and harness"):
        subject._guest_sources(SimpleNamespace(tests_repo_root=tmp_path))


def test_failure_evidence_publishes_only_bounded_os_facts(tmp_path):
    directory = tmp_path / "guest"
    directory.mkdir()
    (directory / "report.json").write_text(json.dumps({
        "schema": "emulebb.android.emulator-boot/1", "phase": "guest-validation", "property": "pageSize",
        "error": "synthetic-private-token", "emulatorExitCode": 1,
        "observedGuest": {"sdk": "28", "codename": "REL", "pageSize": "4096", "qemu": "1",
            "machine": "/private/operator/path", "abis64": "x" * 129, "authentication": "synthetic-private-token"},
    }))
    assert subject._guest_failure_details(tmp_path) == {
        "phase": "guest-validation", "property": "pageSize", "emulatorExitCode": 1,
        "observedGuest": {"sdk": "28", "codename": "REL", "pageSize": "4096", "qemu": "1"}}


@pytest.mark.parametrize("payload", ["not JSON", "[]", '{"schema":"other"}', " " * 65537],
                         ids=["malformed", "array", "schema", "oversize"])
def test_missing_or_invalid_failure_receipt_cannot_claim_a_boot_phase(tmp_path, payload):
    assert subject._guest_failure_details(tmp_path) == {"phase": "unknown"}
    (tmp_path / "guest").mkdir()
    (tmp_path / "guest/report.json").write_text(payload)
    assert subject._guest_failure_details(tmp_path) == {"phase": "unknown"}


@pytest.mark.parametrize("private", [False, True])
def test_tls_failure_diagnostics_allow_only_fixed_owner_codes_and_stages(tmp_path, private):
    directory = tmp_path / "guest"
    directory.mkdir()
    case = sorted(subject.TLS_CASE_NAMES)[0]
    progress = {"stage": "cleanup-forward", "failureStage": "daemon-admission", "activeCase": case,
                "completedCases": sorted(subject.TLS_CASE_NAMES), "daemonExitCode": 101,
                "errorType": "RuntimeError",
                "failureMessage": "Android daemon executable path differs; refusing to signal it"}
    if private:
        progress = {"stage": "synthetic-private-token", "failureStage": "synthetic-private-token",
                    "activeCase": ["synthetic-private-token"], "completedCases": ["synthetic-private-token"],
                    "daemonExitCode": True, "failureMessage": "synthetic-private-token", "errorType": "synthetic-private-token"}
    (directory / "report.json").write_text(json.dumps({"schema": "emulebb.android.emulator-boot/1",
        "phase": "tls-execution", "error": "synthetic-private-cleanup-error", "tlsProgress": progress}))
    value = subject._guest_failure_details(tmp_path)
    if private:
        assert value == {"phase": "tls-execution", "tlsProgress": {}}
    else:
        assert value["reason"] == "daemon-executable-path"
        assert value["tlsProgress"] == {key: progress[key] for key in ("stage", "failureStage", "activeCase", "completedCases", "daemonExitCode", "errorType")}
    assert "synthetic-private" not in json.dumps(value) and "failureMessage" not in json.dumps(value)


@pytest.mark.parametrize("invalid", [False, True])
def test_native_startup_failure_context_is_enum_and_bool_only(tmp_path, invalid):
    directory = tmp_path / "guest"
    directory.mkdir()
    startup = {"context": "interface-enumeration", "systemCondition": "operation-not-permitted",
               "bootstrapCreated": True, "runtimeEntered": True, "leaseExists": True, "metadataExists": False,
               "rawLog": "synthetic-private-key-and-profile"}
    if invalid:
        startup = {"context": ["synthetic-private"], "systemCondition": "synthetic-private",
                   "bootstrapCreated": 1, "metadataExists": "synthetic-private", "rawLog": "synthetic-private"}
    (directory / "report.json").write_text(json.dumps({"schema": "emulebb.android.emulator-boot/1",
        "phase": "tls-execution", "tlsProgress": {"startupFailure": startup}}))
    value = subject._guest_failure_details(tmp_path)
    expected = {} if invalid else {key: item for key, item in startup.items() if key != "rawLog"}
    assert value["tlsProgress"]["startupFailure"] == expected
    assert "synthetic-private" not in json.dumps(value) and "rawLog" not in json.dumps(value)
