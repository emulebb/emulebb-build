"""Android target checks must verify ABI and loader compatibility truthfully."""
from __future__ import annotations

import os
import struct

import pytest

from emule_workspace import android_validation as android


def elf(tmp_path, *, machine=183, alignment=16384, interpreter=b"/system/bin/linker64\0", shared=False):
    content = bytearray(4096)
    content[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HH", content, 16, 3, machine)
    struct.pack_into("<Q", content, 32, 64)
    count = 1 if shared else 2
    struct.pack_into("<HH", content, 54, 56, count)
    struct.pack_into("<IIQQQQQQ", content, 64, 1, 5, 0, 0, 0, 4096, 4096, alignment)
    if not shared:
        content[512:512 + len(interpreter)] = interpreter
        struct.pack_into("<IIQQQQQQ", content, 120, 3, 4, 512, 512, 0, len(interpreter), len(interpreter), 1)
    path = tmp_path / "native-elf"
    path.write_bytes(content)
    return path


@pytest.mark.parametrize("abi,machine", [("arm64-v8a", 183), ("x86_64", 62)])
@pytest.mark.parametrize("shared", [False, True])
def test_android_executable_and_jni_elf_layouts(tmp_path, abi, machine, shared):
    path = elf(tmp_path, machine=machine, shared=shared)
    result = android.audit_android_elf(path, abi, shared_library=shared)
    assert result["elfMachine"] == machine and result["pageSize"] == 16384
    assert result["sharedLibrary"] is shared


@pytest.mark.parametrize("changes,reason", [
    ({"machine": 62}, "CPU"),
    ({"alignment": 4096}, "16KB"),
    ({"alignment": 24576}, "16KB"),
    ({"interpreter": b"/lib64/ld-linux-aarch64.so.1\0"}, "Bionic"),
])
def test_wrong_architecture_page_size_and_glibc_loader_are_rejected(tmp_path, changes, reason):
    with pytest.raises(RuntimeError, match=reason):
        android.audit_android_elf(elf(tmp_path, **changes), "arm64-v8a")


def test_jni_library_must_not_have_a_program_interpreter(tmp_path):
    with pytest.raises(RuntimeError, match="Bionic"):
        android.audit_android_elf(elf(tmp_path), "arm64-v8a", shared_library=True)


@pytest.mark.parametrize("mutate", [
    lambda data: data.__setitem__(4, 1),
    lambda data: struct.pack_into("<Q", data, 32, 1 << 30),
    lambda data: struct.pack_into("<HH", data, 54, 32, 2),
    lambda data: struct.pack_into("<HH", data, 54, 56, 0),
    lambda data: struct.pack_into("<Q", data, 64 + 32, 8192),
])
def test_truncated_and_malformed_elf_headers_fail_closed(tmp_path, mutate):
    path = elf(tmp_path)
    content = bytearray(path.read_bytes())
    mutate(content)
    path.write_bytes(content)
    with pytest.raises(RuntimeError):
        android.audit_android_elf(path, "arm64-v8a")


@pytest.mark.parametrize("abi", ["arm64-v8a", "x86_64"])
def test_rust_and_native_compilers_share_api_floor_without_parent_environment_changes(abi):
    before = os.environ.copy()
    values = android.compiler_environment(abi)
    compiler = next(value for key, value in values.items() if key.startswith("CARGO_TARGET_"))
    assert compiler.endswith(android.TARGETS[abi][0] + "28-clang")
    assert next(value for key, value in values.items() if key.startswith("CC_")) == compiler
    assert next(value for key, value in values.items() if key.startswith("CXX_")) == compiler + "++"
    assert "16384" in values["RUSTFLAGS"]
    assert not {"EMULEBB_WORKSPACE_ROOT", "EMULEBB_WORKSPACE_OUTPUT_ROOT", "CARGO_TARGET_DIR", "X_LOCAL_IP"} & values.keys()
    assert os.environ == before


def test_unsupported_android_abi_is_rejected():
    with pytest.raises(ValueError):
        android.compiler_environment("armeabi-v7a")


def test_checks_remain_locked_offline_and_include_canonical_target_lints(tmp_path, monkeypatch):
    monkeypatch.setattr(android, "linux_quality_plan", lambda root, target: [
        ("clippy", ["clippy", "--workspace", "--all-targets", "--locked", "--target", target, "--", "-D", "warnings"]),
        ("packet-diagnostics", ["check", "--locked", "--features", "packet-diagnostics", "--target", target]),
    ])
    plan = android.validation_plan(tmp_path, "arm64-v8a")
    assert all("--locked" in command and "--offline" in command for _, command in plan)
    assert all("aarch64-linux-android" in command for _, command in plan)
    assert any(command[0] == "test" and "--no-run" in command for _, command in plan)
    assert any(command[0] == "build" and "--release" in command for _, command in plan)
