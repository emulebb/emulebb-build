"""Compiler images must execute their real metadata validator prerequisites."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
import shutil
import xml.etree.ElementTree as ET

import pytest

from emule_workspace import ci_linux_image_probe


@pytest.mark.parametrize("failure", [None, "desktop-file-validate", "appstreamcli"])
def test_metadata_probe_requires_both_tools_to_succeed(tmp_path, monkeypatch, failure):
    calls = []
    def run(command, **options):
        calls.append(command)
        assert options == {"check": True, "capture_output": True, "timeout": 20}
        if command[0] == failure:
            raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(ci_linux_image_probe.subprocess, "run", run)
    if failure:
        with pytest.raises(subprocess.CalledProcessError):
            ci_linux_image_probe.metadata_tools_probe(tmp_path)
    else:
        assert ci_linux_image_probe.metadata_tools_probe(tmp_path) == {
            "desktopFileValidation": "passed", "appstreamValidation": "passed"}
    assert calls[0][0] == "desktop-file-validate"
    if failure != "desktop-file-validate":
        assert calls[1][:3] == ["appstreamcli", "validate", "--no-net"]
    assert ET.parse(tmp_path / "org.example.synthetic-probe.metainfo.xml").getroot().attrib["type"] == "generic"


@pytest.mark.parametrize("failure", [None, "missing-file", "no-image", "runtime", "extraction", "payload"])
def test_appimage_probe_assembles_then_verifies_extracted_payload(tmp_path, monkeypatch, failure):
    native = tmp_path / "native-true"
    native.write_bytes(b"synthetic native executable bytes")
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"synthetic pinned runtime")
    tool = tmp_path / "appimagetool"
    tool.write_bytes(b"synthetic verified tool")
    calls = []
    monkeypatch.setattr(ci_linux_image_probe.shutil, "which", lambda name: str(native) if name == "true" else None)
    def run(command, **options):
        calls.append((command, options))
        if command[0] == "timeout":
            assert command[:5] == ("timeout", "--signal=TERM", "--kill-after=3s", "45s", str(tool))
            assert command[5:7] == ("--runtime-file", str(runtime))
            assert options == {"check": False, "text": True, "capture_output": True}
            if failure == "missing-file":
                return SimpleNamespace(returncode=1, stdout="", stderr="file command is missing but required")
            if failure != "no-image":
                Path(command[-1]).write_bytes(runtime.read_bytes() + b"squashfs")
            if failure == "runtime":
                runtime.write_bytes(b"changed pinned runtime input")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        assert command[1:] == ["--appimage-extract"]
        assert options == {"cwd": tmp_path / "extracted", "check": True, "capture_output": True, "timeout": 30}
        if failure == "extraction":
            raise subprocess.CalledProcessError(1, command)
        shutil.copytree(tmp_path / "Synthetic.AppDir", tmp_path / "extracted/squashfs-root")
        if failure == "payload":
            (tmp_path / "extracted/squashfs-root/synthetic-payload.txt").write_bytes(b"wrong payload")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(ci_linux_image_probe.subprocess, "run", run)
    if failure:
        expected = subprocess.CalledProcessError if failure == "extraction" else RuntimeError
        with pytest.raises(expected):
            ci_linux_image_probe.appimage_tools_probe(tmp_path, tool, runtime)
    else:
        proof = ci_linux_image_probe.appimage_tools_probe(tmp_path, tool, runtime)
        assert proof["nonrootAppImagePackaging"] == "passed" and proof["appimageExtraction"] == "no-fuse"
        assert proof["appimagePayloadFiles"] == ["AppRun", "synthetic-payload.txt", "synthetic-probe.desktop", "synthetic-probe.svg"]
    assert len(calls) == (1 if failure in {"missing-file", "no-image", "runtime"} else 2)


def test_appimage_native_and_prepared_profiles_install_the_external_file_command():
    from emule_workspace.ubuntu_ci import PROFILES
    for name in ("rust-native-release", "rust-prepared-builder"):
        assert "file" in PROFILES[name]
