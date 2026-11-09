"""Reviewed Android archive declarations must be complete and immutable."""
from __future__ import annotations

import copy
import json

import pytest

from emule_workspace import android_inputs as inputs


def test_reviewed_inputs_are_complete_and_use_stable_platform_tools():
    value = inputs.tool_inputs()
    assert value["assets"]["platform-tools"]["version"] == "37.0.1"
    assert value["assets"]["platform"]["version"] == "37.2-r2"


def test_emulator_inputs_separate_minimum_aosp_and_current_16kb_images():
    value = inputs.emulator_inputs()
    assert set(value["assets"]) == inputs.EMULATOR_ROLES
    assert value["assets"]["system-image-min"]["version"] == "28-r4"
    assert "/sys-img/android/" in value["assets"]["system-image-min"]["url"]
    assert "x86_64-ps16k-37.2_r07.zip" in value["assets"]["system-image-current"]["url"]
    assert not inputs.EMULATOR_ROLES & inputs.ROLES


@pytest.mark.parametrize("role,changes", [
    ("emulator", {"url": "https://dl.google.com/android/repository/emulator-linux_x64-latest.zip"}),
    ("emulator", {"sha256": "f" * 63}),
    ("system-image-min", {"version": "29-r4"}),
    ("system-image-min", {"url": "https://dl.google.com/android/repository/sys-img/google_apis/x86_64-28_r04.zip"}),
    ("system-image-current", {"url": "https://dl.google.com/android/repository/sys-img/google_apis/x86_64-37.2_r07.zip"}),
    ("system-image-current", {"version": "37.2-r8"}),
])
def test_emulator_input_versions_and_archive_variants_cannot_drift(tmp_path, role, changes):
    value = copy.deepcopy(inputs.emulator_inputs())
    value["assets"][role].update(changes)
    config = tmp_path / "emulator.json"
    config.write_text(json.dumps(value))
    with pytest.raises(RuntimeError):
        inputs.emulator_inputs(config)


@pytest.mark.parametrize("mutation", [
    lambda value: value["assets"].pop("ndk"),
    lambda value: value["assets"]["ndk"].update(version="latest"),
    lambda value: value["assets"]["ndk"].update(sha256="f" * 63),
    lambda value: value["assets"]["android-cli"].update(url="https://dl.google.com/android/cli/latest/linux_x86_64/android-cli"),
    lambda value: value["assets"]["android-cli"].update(kind="zip"),
    lambda value: value["assets"]["ndk"].update(url="http://dl.google.com/android/repository/ndk.zip"),
    lambda value: value["assets"]["ndk"].update(url="https://dl.google.com.evil.test/android/repository/ndk.zip"),
    lambda value: value["assets"]["ndk"].update(url="https://dl.google.com/android/repository/ndk.zip?revision=latest"),
    lambda value: value["assets"]["ndk"].update(url="https://dl.google.com/android/repository/ndk.zip#latest"),
    lambda value: value["assets"]["jdk"].update(url="https://github.com/other/project/jdk.tar.gz"),
    lambda value: value.update(host="linux-aarch64"),
])
def test_invalid_inputs_fail_before_fetch(tmp_path, mutation):
    value = copy.deepcopy(inputs.tool_inputs())
    mutation(value)
    config = tmp_path / "inputs.json"
    config.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(RuntimeError):
        inputs.tool_inputs(config)
