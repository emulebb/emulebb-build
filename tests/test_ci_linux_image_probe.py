"""Compiler images must execute their real metadata validator prerequisites."""

import subprocess
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
