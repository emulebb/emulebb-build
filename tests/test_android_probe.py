"""Probes must reject unexpected tool identity and avoid root execution."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from emule_workspace import android_probe as probe


@pytest.fixture
def environment(tmp_path, monkeypatch):
    inputs = probe.tool_inputs()
    monkeypatch.setattr(probe, "tool_inputs", lambda: inputs)
    monkeypatch.setattr(probe.platform, "system", lambda: "Linux")
    monkeypatch.setattr(probe.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(probe.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(probe.os, "getgid", lambda: 1000, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    original = Path.read_text
    def read(path, *args, **kwargs):
        if path.as_posix() == "/opt/android-tools/tool-inputs.json":
            return json.dumps(inputs)
        if path.name == "source.properties":
            return "Pkg.Revision = " + inputs["assets"]["command-line-tools"]["version"]
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    return inputs


def test_root_is_rejected_before_tool_execution(environment, monkeypatch):
    monkeypatch.setattr(probe.os, "getuid", lambda: 0)
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: pytest.fail("root tool execution"))
    with pytest.raises(RuntimeError, match="non-root native"):
        probe.probe()


def test_mismatched_installed_pins_are_rejected(environment, monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: "{}")
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: pytest.fail("unverified tool execution"))
    with pytest.raises(RuntimeError, match="differ from the reviewed pins"):
        probe.probe()


def test_wrong_sdk_package_revision_is_rejected(environment, monkeypatch):
    marker = json.dumps(environment)
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs:
                        marker if path.name == "tool-inputs.json" else "Pkg.Revision = 22.0")
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: pytest.fail("wrong SDK execution"))
    with pytest.raises(RuntimeError, match="package revision differs"):
        probe.probe()


@pytest.mark.parametrize("exit_code,output", [(1, "public loader failure"), (0, "wrong tool version")])
def test_failed_version_probe_retains_diagnostic(environment, monkeypatch, exit_code, output):
    monkeypatch.setattr(probe.subprocess, "run",
                        lambda command, **kwargs: subprocess.CompletedProcess(command, exit_code, output))
    with pytest.raises(RuntimeError) as failure:
        probe.probe()
    assert output in str(failure.value)
