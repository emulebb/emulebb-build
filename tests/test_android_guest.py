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


@pytest.mark.parametrize("outcome", ["success", "failure", "mismatch", "source-change", "private-extra"])
@pytest.mark.parametrize("image,api,page", [("min", 28, 4096), ("current", 37, 16384)])
def test_shared_boot_owner_enforces_identity_isolation_and_source_freeze(tmp_path, monkeypatch, outcome, image, api, page):
    from emule_workspace import release

    workspace, output = tmp_path / "workspace", tmp_path / "output"
    harness = workspace / "repos/emulebb-build-tests"
    (harness / "scripts").mkdir(parents=True)
    (harness / "scripts/probe-android-emulator.py").touch()
    output.mkdir()
    layout = SimpleNamespace(output_root=output, tests_repo_root=harness)
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
        assert timeout == 480
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
        (guest / "report.json").write_text(json.dumps(value))

    monkeypatch.setattr(linux_containers, "run_owned_container", run)
    before = os.environ.copy()
    if outcome in {"success", "private-extra"}:
        subject.probe_emulator_guest(layout, image=image)
    else:
        with pytest.raises(RuntimeError):
            subject.probe_emulator_guest(layout, image=image)
    assert os.environ == before
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
    assert value["status"] == ("passed" if outcome in {"success", "private-extra"} else "failed")
    assert value["applicationQualification"] == "not-run" and value["source"] == source
    assert "private" not in public and str(tmp_path) not in public
    prepare.assert_called_once_with(layout, images=(image,), wsl_distro="Ubuntu")


def test_guest_sources_reject_dirty_or_changed_revisions(tmp_path, monkeypatch):
    from emule_workspace import git

    monkeypatch.setattr(git, "git_output", lambda path, *a: " M script.py" if a[0] == "status" else "1" * 40)
    with pytest.raises(RuntimeError, match="clean build and harness"):
        subject._guest_sources(SimpleNamespace(tests_repo_root=tmp_path))
