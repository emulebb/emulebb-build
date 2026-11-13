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


def test_apk_probe_executes_api34_compilers_and_packaging_tools_offline(environment, monkeypatch):
    commands = []
    def execute(command, **kwargs):
        commands.append(command)
        name = Path(command[0]).name
        outputs = {"java": environment["assets"]["jdk"]["version"].split("+")[0],
                   "gradle": "Gradle " + environment["assets"]["gradle"]["version"],
                   "android": environment["assets"]["android-cli"]["version"],
                   "adb": environment["assets"]["platform-tools"]["version"],
                   "aapt2": "Android Asset Packaging Tool (aapt) 2", "apksigner": "0.9", "zipalign": ""}
        return subprocess.CompletedProcess(command, 0, outputs.get(name, "clang version"))
    monkeypatch.setattr(probe.subprocess, "run", execute)
    result = probe.probe(apk=True)
    assert result["schema"] == "emulebb.android-apk-toolchain-probe/1"
    assert {Path(command[0]).name for command in commands} >= {
        "aarch64-linux-android34-clang", "x86_64-linux-android34-clang", "aapt2", "apksigner", "zipalign"}
    alignment = [command for command in commands if Path(command[0]).name == "zipalign"]
    assert alignment[0][1:5] == ["-f", "-P", "16", "4"]
    assert alignment[1][1:5] == ["-c", "-P", "16", "4"]


@pytest.fixture
def emulator_environment(environment, monkeypatch):
    inputs = probe.emulator_subset(probe.emulator_inputs(), ("min", "current"))
    monkeypatch.setattr(probe, "emulator_inputs", lambda: inputs)
    original_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: False if path.as_posix() == "/dev/kvm" else original_exists(path))
    def read(path, *args, **kwargs):
        if path.name == "toolchain.json":
            return json.dumps(environment)
        if path.name == "emulator-inputs.json":
            return json.dumps(inputs)
        if path.parent.name == "emulator":
            return "Pkg.Revision=37.2.13\nPkg.BuildId=16740729"
        if path.parent.name == "platform-tools":
            return "Pkg.Revision=37.0.1"
        if "android-28" in path.as_posix():
            return "Pkg.Revision=4\nAndroidVersion.ApiLevel=28\nSystemImage.Abi=x86_64\nSystemImage.TagId=default"
        return ("Pkg.Revision=7\nAndroidVersion.ApiLevel=37.2\nSystemImage.Abi=x86_64\n"
                "SystemImage.TagId=google_apis,page_size_16kb\nAndroidVersion.PreviewSdkInt=0")
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(probe.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, "Version 37.0.1-123" if command[-1] == "version" else
                            "Android emulator version 37.2.13.0 (build_id 16740729)"))
    return inputs


def test_emulator_tool_execution_does_not_claim_guest_runtime(emulator_environment, monkeypatch):
    execute = probe.subprocess.run
    commands = []
    def headless(command, **kwargs):
        commands.append(command)
        if "emulator/emulator" in command[0]:
            assert "-no-window" in command
        return execute(command, **kwargs)
    monkeypatch.setattr(probe.subprocess, "run", headless)
    result = probe.probe_emulator()
    assert result["inputs"] == emulator_environment
    assert result["runtimeExecution"] == "not-run"
    assert result["uid"] == 1000 and result["kvm"] == {"deviceVisible": False, "usable": False}
    assert len(commands) == 2 and "-version" in commands[0]
    assert commands[1][-1] == "version"


def test_required_kvm_cannot_silently_fall_back_to_software(emulator_environment):
    with pytest.raises(RuntimeError, match="usable KVM"):
        probe.probe_emulator(require_kvm=True)


def test_emulator_adb_package_mismatch_fails_before_execution(emulator_environment, monkeypatch):
    original = Path.read_text
    def read(path, *args, **kwargs):
        return "Pkg.Revision=0" if path.parent.name == "platform-tools" else original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: pytest.fail("unverified ADB execution"))
    with pytest.raises(RuntimeError, match="ADB package revision"):
        probe.probe_emulator()


@pytest.mark.parametrize("field,value", [("SystemImage.Abi", "arm64-v8a"), ("AndroidVersion.PreviewSdkInt", "1"),
                                       ("SystemImage.TagId", "google_apis"), ("AndroidVersion.ApiLevel", "37.1")])
def test_wrong_guest_variant_fails_before_emulator_execution(emulator_environment, monkeypatch, field, value):
    original = Path.read_text
    def read(path, *args, **kwargs):
        text = original(path, *args, **kwargs)
        if "android-37.2" in path.as_posix():
            text = "\n".join(field + "=" + value if line.startswith(field + "=") else line for line in text.splitlines())
        return text
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: pytest.fail("mismatched guest execution"))
    with pytest.raises(RuntimeError, match="differs from its pin"):
        probe.probe_emulator()
