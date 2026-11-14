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
@pytest.mark.parametrize("image,api,page", [("min", 28, 4096), ("current", 37, 16384),
    ("apk34", 34, 4096), ("apk35", 35, 4096), ("apk36", 36, 4096), ("apk37", 37, 16384)])
def test_shared_boot_owner_enforces_identity_isolation_and_source_freeze(tmp_path, monkeypatch, capsys, outcome, image, api, page):
    from emule_workspace import release

    workspace, output = tmp_path / "workspace", tmp_path / "output"
    harness = workspace / "repos/emulebb-build-tests"
    (harness / "scripts").mkdir(parents=True)
    (harness / "scripts/probe-android-emulator.py").touch()
    output.mkdir()
    layout = SimpleNamespace(output_root=output, tests_repo_root=harness)
    tls = outcome.startswith("tls-")
    apk_role = image.startswith("apk")
    if tls and apk_role:
        with pytest.raises(ValueError, match="one native or APK"):
            subject.probe_emulator_guest(layout, image=image, tls_startup=True)
        return
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
    unused_prepare = Mock(side_effect=AssertionError("wrong toolchain role"))
    monkeypatch.setattr(subject, "prepare_emulator_toolchain", unused_prepare if apk_role else prepare)
    monkeypatch.setattr(subject, "prepare_apk_emulator_toolchain", prepare if apk_role else unused_prepare)
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
    assert value["scope"] == "guest-boot" and "apk" not in value
    assert "--apk-source-sha" not in captured and "--apk-manifest-sha256" not in captured
    assert "synthetic-private-token" not in public and "localAuthentication" not in public and str(tmp_path) not in public
    public_output = capsys.readouterr().out
    if outcome == "failure":
        assert json.loads(public_output) == {"failureDetails": {"phase": "unknown"}}
        assert "private command output" not in public_output
    if tls:
        compile_calls.assert_called_once_with(layout, abis=("x86_64",), wsl_distro="Ubuntu", tls_payload=True)
        assert "--tls-source-sha" in captured and "--tls-manifest-sha256" in captured
        assert str(payload_root) + ",target=/tls-payload,readonly" in " ".join(captured)
        assert value["tls"]["status"] == ("passed" if outcome == "tls-success" else "failed")
    prepare.assert_called_once_with(layout, **({"image": image} if apk_role else {"images": (image,)}), wsl_distro="Ubuntu")
    unused_prepare.assert_not_called()


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


@pytest.mark.parametrize("message,reason", [
    ("Owned Android emulator did not complete boot within its bound", "guest-boot-timeout"),
    ("Owned Android emulator exited before boot; inspect the private emulator log", "emulator-exited-before-boot"),
    ("Owned Android emulator cleanup failed", "owned-emulator-cleanup-failed")])
def test_boot_failure_diagnostics_use_only_fixed_reason_codes(tmp_path, message, reason):
    (tmp_path / "guest").mkdir()
    (tmp_path / "guest/report.json").write_text(json.dumps({
        "schema": "emulebb.android.emulator-boot/1", "phase": "guest-boot", "error": message}))
    assert subject._guest_failure_details(tmp_path) == {"phase": "guest-boot", "reason": reason}


@pytest.mark.parametrize("fault", [None, "private-case", "duplicate", "private-enums", "oversize", "malformed", "schema"])
def test_apk_failure_progress_projects_only_source_owned_completed_cases(tmp_path, fault):
    (tmp_path / "guest").mkdir()
    (tmp_path / "guest/report.json").write_text(json.dumps({"schema": "emulebb.android.emulator-boot/1",
        "phase": "apk-lifecycle", "error": "synthetic-private-key-and-ui"}))
    receipt = {"schema": "emulebb.android.apk-lifecycle/1", "status": "failed", "runtimeExecution": "executed",
        "cases": [{"name": "authenticated-rest", "status": "passed"}], "private": "synthetic-private-key-and-ui"}
    if fault == "private-case": receipt["cases"][0]["name"] = "synthetic-private-key-and-ui"
    if fault == "duplicate": receipt["cases"] *= 2
    if fault == "private-enums": receipt.update(status=["synthetic-private"], runtimeExecution="synthetic-private")
    if fault == "schema": receipt["schema"] = "unknown"
    text = " " * 65537 if fault == "oversize" else "not json" if fault == "malformed" else json.dumps(receipt)
    (tmp_path / "guest/apk-lifecycle.json").write_text(text)
    result = subject._guest_failure_details(tmp_path, apk_case_names=frozenset({"authenticated-rest", "install"}))
    if fault in {"oversize", "malformed", "schema"}:
        assert result == {"phase": "apk-lifecycle"}
    else:
        expected = {} if fault == "private-enums" else {"status": "failed", "runtimeExecution": "executed"}
        if fault not in {"private-case", "duplicate"}: expected["completedCases"] = ["authenticated-rest"]
        assert result == {"phase": "apk-lifecycle", "apkProgress": expected}
    assert "synthetic-private" not in json.dumps(result)


@pytest.mark.parametrize("fault", [None, "stage", "code", "phase", "control-bool", "control-name"])
def test_apk_failure_substage_and_ui_facts_are_enum_and_bool_only(tmp_path, fault):
    (tmp_path / "guest").mkdir()
    (tmp_path / "guest/report.json").write_text(json.dumps({"schema": "emulebb.android.emulator-boot/1",
        "phase": "apk-lifecycle", "property": "pageSize", "error": "synthetic-private-key-and-ui"}))
    failure = {"stage": "launch-main", "code": "required-control-missing", "servicePhase": "Stopped",
        "controls": {"api_key": {"present": False, "enabled": False, "text": "synthetic-private-key-and-ui"}},
        "message": "synthetic-private-key-and-ui"}
    if fault == "stage": failure["stage"] = ["synthetic-private"]
    if fault == "code": failure["code"] = "synthetic-private"
    if fault == "phase": failure["servicePhase"] = "synthetic-private"
    if fault == "control-bool": failure["controls"]["api_key"]["enabled"] = 1
    if fault == "control-name": failure["controls"] = {"synthetic-private": {"present": True, "enabled": True}}
    (tmp_path / "guest/apk-lifecycle.json").write_text(json.dumps({"schema": "emulebb.android.apk-lifecycle/1",
        "status": "failed", "runtimeExecution": "not-run", "cases": [], "failure": failure}))
    result = subject._guest_failure_details(tmp_path, apk_case_names=frozenset({"install"}),
        apk_failure_stages=frozenset({"launch-main"}), apk_failure_codes=frozenset({"required-control-missing"}))
    expected = {"phase": "apk-lifecycle", "apkProgress": {"status": "failed", "runtimeExecution": "not-run", "completedCases": []}}
    if fault not in {"stage", "code"}:
        safe = {"stage": "launch-main", "code": "required-control-missing", "controls": {}}
        if fault != "phase": safe["servicePhase"] = "Stopped"
        if fault not in {"control-bool", "control-name"}: safe["controls"] = {"api_key": {"present": False, "enabled": False}}
        expected["apkProgress"]["failure"] = safe
    assert result == expected
    assert "synthetic-private" not in json.dumps(result) and "property" not in result


@pytest.mark.parametrize("invalid", [False, True])
def test_navigation_facts_are_closed_bounded_and_not_raw_ui(tmp_path, invalid):
    (tmp_path / "guest").mkdir()
    (tmp_path / "guest/report.json").write_text(json.dumps({"schema": "emulebb.android.emulator-boot/1", "phase": "apk-lifecycle"}))
    navigation = {"control": "start_service", "displayBounds": [0, 0, 1080, 1920],
        "scrollBounds": [0, 100, 1080, 1800], "lastGesture": [500, 1500, 500, 300],
        "forwardAttempts": 6, "backwardAttempts": 0, "forwardProgress": 5, "backwardProgress": 0,
        "imeVisible": True, "rawUi": "synthetic-private-key-and-ui"}
    boot = {"userUnlocked": True, "packageStopped": False, "bootReceiverResolved": True,
        "servicePresent": True, "serviceForeground": False, "serviceStartRequested": True,
        "allFilesAllowed": True, "notificationsGranted": False, "localNetworkGranted": True,
        "dozeWhitelisted": False, "backgroundRestricted": False, "latestDaemonExitReason": 10,
        "restAttempts": 30, "restConnectionFailures": 29, "restHttpResponses": 1, "lastHttpStatus": 503,
        "rawLog": "synthetic-private-key-and-ui"}
    if invalid:
        navigation.update(displayBounds=[True, 0, 1080, 1920], scrollBounds=[0, 0, 1],
            lastGesture=[0, -1, 1, 32769], forwardAttempts=7, backwardAttempts=-1,
            forwardProgress=True, backwardProgress="synthetic-private", imeVisible=1)
        boot.update({name: "synthetic-private" for name, value in boot.items() if type(value) is bool})
        boot.update(latestDaemonExitReason=17, restAttempts=0, restConnectionFailures=True,
            restHttpResponses=1025, lastHttpStatus=600)
    (tmp_path / "guest/apk-lifecycle.json").write_text(json.dumps({"schema": "emulebb.android.apk-lifecycle/1",
        "failure": {"stage": "task-manager-stop", "code": "listener-still-running", "navigation": navigation, "boot": boot}}))
    result = subject._guest_failure_details(tmp_path, apk_case_names=frozenset({"install"}),
        apk_failure_stages=frozenset({"task-manager-stop"}), apk_failure_codes=frozenset({"listener-still-running"}),
        apk_navigation_controls=frozenset({"start_service"}))
    facts = result["apkProgress"]["failure"]
    assert facts["navigation"] == ({"control": "start_service"} if invalid else {
        key: value for key, value in navigation.items() if key != "rawUi"})
    assert "boot" not in facts
    assert "synthetic-private" not in json.dumps(result)
    navigation["control"] = "synthetic-private"
    (tmp_path / "guest/apk-lifecycle.json").write_text(json.dumps({"schema": "emulebb.android.apk-lifecycle/1",
        "failure": {"stage": "task-manager-stop", "code": "listener-still-running", "navigation": navigation}}))
    assert "navigation" not in subject._guest_failure_details(tmp_path, apk_case_names=frozenset({"install"}),
        apk_failure_stages=frozenset({"task-manager-stop"}), apk_failure_codes=frozenset({"listener-still-running"}),
        apk_navigation_controls=frozenset({"start_service"}))["apkProgress"]["failure"]


@pytest.mark.parametrize('invalid', [False, True])
def test_lifecycle_failure_details_are_independently_filtered(tmp_path, invalid):
    (tmp_path / 'guest').mkdir()
    (tmp_path / 'guest/report.json').write_text(json.dumps({'schema': 'emulebb.android.emulator-boot/1', 'phase': 'apk-lifecycle'}))
    failure = {'stage': 'local-transfer', 'code': 'other',
        'networkWait': {'state': 'waiting', 'phase': 'Starting', 'text': 'private'},
        'transfer': {'phase': 'await-completed-delivery', 'code': 'await-completed-delivery-failed',
            'adb': {'operation': 'push', 'reason': 'no-space', 'exitCategory': 'nonzero', 'raw': 'private'},
            'rest': {'httpStatus': 409, 'apiCode': 'DOWNLOAD_CONFLICT', 'coreContext': 'unrecognized', 'raw': 'private'},
            'ownedFixture': {'code': 'inventory-invalid', 'filesRemoved': 1, 'raw': 'private'}}}
    if invalid:
        failure['networkWait'] = {'state': 'private', 'phase': 'private'}
        failure['transfer']['phase'] = 'private'
    (tmp_path / 'guest/apk-lifecycle.json').write_text(json.dumps({'schema': 'emulebb.android.apk-lifecycle/1', 'failure': failure}))
    result = subject._guest_failure_details(tmp_path, apk_case_names=frozenset({'install'}),
        apk_failure_stages=frozenset({'local-transfer'}), apk_failure_codes=frozenset({'other'}))
    safe = result['apkProgress']['failure']
    if invalid:
        assert 'networkWait' not in safe and 'transfer' not in safe
    else:
        assert safe['networkWait'] == {'state': 'waiting', 'phase': 'Starting'}
        assert safe['transfer']['rest']['httpStatus'] == 409 and safe['transfer']['adb']['exitCategory'] == 'nonzero'
        assert safe['transfer']['ownedFixture'] == {'code': 'inventory-invalid', 'filesRemoved': 1}
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize("phase", ["await-completed-delivery", "enqueue-owned-transfer"])
def test_completion_failure_observations_are_independently_projected(tmp_path, phase):
    (tmp_path / "guest").mkdir()
    (tmp_path / "guest/report.json").write_text(json.dumps({"schema": "emulebb.android.emulator-boot/1", "phase": "apk-lifecycle"}))
    sample = {"sharedLookup": "missing", "transferPresence": "present", "state": "completing", "progress": 1.0,
        "completedBytes": 262144, "sizeBytes": 262144, "partsObtained": 1, "partsTotal": 1,
        "deliveryPathPresent": False, "deliveryErrorCategory": "atomic-no-replace-unsupported"}
    untrusted = {**sample, "path": "private", "deliveryError": "private", "hash": "private", "apiKey": "private"}
    completion = {"reason": "deadline-exhausted", "samples": [untrusted, untrusted], "raw": "private"}
    failure = {"stage": "local-transfer", "code": "other", "transfer": {
        "phase": phase, "code": phase + "-failed", "completion": completion}}
    (tmp_path / "guest/apk-lifecycle.json").write_text(json.dumps({"schema": "emulebb.android.apk-lifecycle/1", "failure": failure}))
    result = subject._guest_failure_details(tmp_path, apk_case_names=frozenset(),
        apk_failure_stages=frozenset({"local-transfer"}), apk_failure_codes=frozenset({"other"}))
    transfer = result["apkProgress"]["failure"]["transfer"]
    if phase == "await-completed-delivery":
        assert transfer["completion"] == {"reason": "deadline-exhausted", "samples": [sample, sample]}
    else:
        assert "completion" not in transfer
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("change", [
    {"reason": "private"}, {"reason": []}, {"samples": []}, {"samples": [None]},
    {"samples": [{"sharedLookup": [], "transferPresence": "present"}]},
    {"samples": [{"sharedLookup": "missing", "transferPresence": {}}]},
    {"samples": [{"sharedLookup": "missing", "transferPresence": "present"}] * 3},
])
def test_completion_failure_rejects_unknown_or_unbounded_samples(change):
    value = {"reason": "lookup-failed", "samples": [{"sharedLookup": "missing", "transferPresence": "absent"}]}
    value.update(change)
    assert subject._safe_transfer_completion(value) == {}


@pytest.mark.parametrize("progress", [True, -1, 1.01, float("nan"), float("inf"), "private", {}, []])
def test_completion_failure_excludes_invalid_numbers_and_arbitrary_categories(progress):
    sample = {"sharedLookup": "metadata-mismatch", "transferPresence": "present", "state": [], "progress": progress,
        "completedBytes": True, "sizeBytes": 2**64, "partsObtained": -1, "partsTotal": 2**32,
        "deliveryPathPresent": "private", "deliveryErrorCategory": {"raw": "private"}}
    assert subject._safe_transfer_completion({"reason": "lookup-failed", "samples": [sample]}) == {
        "reason": "lookup-failed", "samples": [{"sharedLookup": "metadata-mismatch", "transferPresence": "present"}]}


def test_completion_failure_preserves_zeroes_limits_and_omits_absent_transfer_fields():
    sample = {"sharedLookup": "missing", "transferPresence": "present", "state": "downloading", "progress": 0,
        "completedBytes": 0, "sizeBytes": 2**64 - 1, "partsObtained": 0, "partsTotal": 2**32 - 1,
        "deliveryPathPresent": False, "deliveryErrorCategory": "none"}
    absent = {**sample, "transferPresence": "absent"}
    assert subject._safe_transfer_completion({"reason": "deadline-exhausted", "samples": [sample, absent]}) == {
        "reason": "deadline-exhausted", "samples": [sample, {"sharedLookup": "missing", "transferPresence": "absent"}]}


@pytest.mark.parametrize("invalid", [False, True])
def test_new_failure_diagnostics_are_closed_and_never_publish_raw_values(tmp_path, invalid):
    (tmp_path / "guest").mkdir()
    (tmp_path / "guest/report.json").write_text(json.dumps({"schema": "emulebb.android.emulator-boot/1", "phase": "apk-lifecycle"}))
    launch = {"result": "ok", "resumed": "other", "window": "absent", "process": "present", "keyguard": "showing", "raw": "private"}
    modes = {"uidMode": "deny", "packageMode": "allow", "defaultMode": "unavailable", "raw": "private"}
    boot = {"allFilesBeforeReboot": modes, "allFilesAfterReboot": modes,
            "serviceNotificationState": "waiting-for-network", "nativeRouteProbe": "unavailable", "raw": "private"}
    if invalid:
        launch = {key: "private" for key in launch}
        boot.update(allFilesBeforeReboot={"uidMode": "private"}, allFilesAfterReboot=[],
                    serviceNotificationState="private", nativeRouteProbe="private")
    (tmp_path / "guest/apk-lifecycle.json").write_text(json.dumps({"schema": "emulebb.android.apk-lifecycle/1",
        "failure": {"stage": "wait-stopped", "code": "app-not-visible", "initialLaunch": launch, "boot": boot}}))
    result = subject._guest_failure_details(tmp_path, apk_case_names=frozenset({'install'}),
        apk_failure_stages=frozenset({'wait-stopped'}), apk_failure_codes=frozenset({'app-not-visible'}))
    failure = result['apkProgress']['failure']
    if invalid:
        assert 'initialLaunch' not in failure and 'boot' not in failure
    else:
        assert failure['initialLaunch'] == {key: value for key, value in launch.items() if key != 'raw'}
        assert 'boot' not in failure
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize("invalid", [False, True])
def test_post_failure_ui_cannot_rewrite_stop_failure_or_expose_raw_error(tmp_path, invalid):
    (tmp_path / "guest").mkdir()
    (tmp_path / "guest/report.json").write_text(json.dumps({"schema": "emulebb.android.emulator-boot/1", "phase": "apk-lifecycle"}))
    post_ui = {"servicePhase": "Running", "resumeOnBoot": False, "errorCategory": "required-files-permission",
        "error": "synthetic-private-error-and-key", "text": "synthetic-private-error-and-key"}
    if invalid:
        post_ui.update(servicePhase=["synthetic-private"], resumeOnBoot=1, errorCategory="synthetic-private")
    (tmp_path / "guest/apk-lifecycle.json").write_text(json.dumps({"schema": "emulebb.android.apk-lifecycle/1",
        "status": "failed", "runtimeExecution": "executed", "failure": {
            "stage": "task-manager-stop", "code": "listener-still-running", "boot": {"servicePresent": False},
            "postFailureUi": post_ui}}))
    value = subject._guest_failure_details(tmp_path, apk_case_names=frozenset({"install"}),
        apk_failure_stages=frozenset({"task-manager-stop"}), apk_failure_codes=frozenset({"listener-still-running"}),
        apk_post_failure_error_categories=frozenset({"required-files-permission", "unknown"}))
    expected_failure = {"stage": "task-manager-stop", "code": "listener-still-running"}
    if not invalid:
        expected_failure["postFailureUi"] = {"servicePhase": "Running", "errorCategory": "required-files-permission"}
    assert value == {"phase": "apk-lifecycle", "apkProgress": {"status": "failed", "runtimeExecution": "executed", "failure": expected_failure}}
    assert "synthetic-private" not in json.dumps(value)


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
               "errorSource": "retained-jsonl", "fatalRecordStatus": "matched",
               "rawLog": "synthetic-private-key-and-profile"}
    if invalid:
        startup = {"context": ["synthetic-private"], "systemCondition": "synthetic-private",
                   "bootstrapCreated": 1, "metadataExists": "synthetic-private", "rawLog": "synthetic-private",
                   "errorSource": "synthetic-private", "fatalRecordStatus": {"private": "synthetic-private"}}
    (directory / "report.json").write_text(json.dumps({"schema": "emulebb.android.emulator-boot/1",
        "phase": "tls-execution", "tlsProgress": {"startupFailure": startup}}))
    value = subject._guest_failure_details(tmp_path)
    expected = {} if invalid else {key: item for key, item in startup.items() if key != "rawLog"}
    assert value["tlsProgress"]["startupFailure"] == expected
    assert "synthetic-private" not in json.dumps(value) and "rawLog" not in json.dumps(value)
