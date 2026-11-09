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
