"""Android target checks must verify ABI and loader compatibility truthfully."""
from __future__ import annotations

import os
import struct
import json
from types import SimpleNamespace

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


@pytest.mark.parametrize("fail", [False, True])
def test_prefetch_covers_host_dependencies_and_failures_never_report_success(tmp_path, monkeypatch, fail):
    guarded = []
    monkeypatch.setattr("emule_workspace.rust_source_cache.ensure_rust_source_cache",
                        lambda layout, **kwargs: guarded.append(kwargs))
    from emule_workspace import release

    output = tmp_path / "output"
    output.mkdir()
    layout = SimpleNamespace(output_root=output, emulebb_rust_repo_root=tmp_path / "rust")
    provenance = {"source": {"emulebbRust": {"commit": "a" * 40}}}
    monkeypatch.setattr(release, "_emulebb_rust_package_provenance", lambda *args: provenance)
    monkeypatch.setattr(release, "_assert_emulebb_rust_package_provenance", lambda *args: None)
    monkeypatch.setattr(release, "_emulebb_rust_workspace_version", lambda *args: "0.1.0-beta.2")
    monkeypatch.setattr(release, "_emulebb_rust_native_source_roots", lambda *args: (tmp_path / "nat1", tmp_path / "nat2"))
    monkeypatch.setattr(android, "resolve_required_workspace_roots", lambda: (tmp_path, output))
    monkeypatch.setattr(android, "resolve_required_cargo_target_dir", lambda *args: output)
    monkeypatch.setattr(android, "prepare_toolchain", lambda *args, **kwargs: output / "toolchain.json")
    monkeypatch.setattr(android, "_builder_user", lambda *args: "1000:1000")
    monkeypatch.setattr(android, "validation_plan", lambda *args: [])
    def audited_fixture(path, *args):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Synthetic compiled fixture")
        return {"verified": True}
    monkeypatch.setattr(android, "audit_android_elf", audited_fixture)

    class Docker:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def path(self, path): return str(path)

    monkeypatch.setattr(android, "LinuxDocker", Docker)
    commands = []
    def execute(docker, arguments, log, **kwargs):
        commands.append(arguments)
        log.write_text("public diagnostic")
        if fail:
            raise RuntimeError("prefetch failed")
    monkeypatch.setattr(android, "run_owned_container", execute)
    if fail:
        with pytest.raises(RuntimeError, match="prefetch failed"):
            android.validate_android(layout)
    else:
        android.validate_android(layout)
    assert commands[0][-3:] == ["cargo", "fetch", "--locked"]
    assert "--target" not in commands[0]
    assert len(guarded) == (0 if fail else 1)
    if guarded:
        assert guarded[0]["execution_source"] == "/source"
        assert guarded[0]["execution_target"].startswith("/output/builds/rust/target-android-")
    report = json.loads(next((output / "reports/rust-android").rglob("validation.json")).read_text())
    assert report["status"] == ("failed" if fail else "passed")
    assert report["runtimeExecution"] == "not-run"
    assert report["checks"][0]["status"] == ("failed" if fail else "passed")


def test_nightly_build_receives_identity_before_fresh_release_link(tmp_path, monkeypatch):
    monkeypatch.setattr("emule_workspace.rust_source_cache.ensure_rust_source_cache", lambda *args, **kwargs: None)
    from emule_workspace import release
    from android_package_fixtures import VERSION
    output = tmp_path/"output"
    output.mkdir()
    layout = SimpleNamespace(output_root=output, emulebb_rust_repo_root=tmp_path/"rust")
    sha = "1"*40
    provenance = {"source": {"emulebbRust": {"commit": sha}}}
    monkeypatch.setattr(release, "_emulebb_rust_package_provenance", lambda *args: provenance)
    monkeypatch.setattr(release, "_assert_emulebb_rust_package_provenance", lambda *args: None)
    monkeypatch.setattr(release, "_emulebb_rust_native_source_roots", lambda *args: (tmp_path/"mini", tmp_path/"pcp"))
    monkeypatch.setattr(android, "resolve_required_workspace_roots", lambda: (tmp_path, output))
    monkeypatch.setattr(android, "resolve_required_cargo_target_dir", lambda *args: output)
    monkeypatch.setattr(android, "prepare_toolchain", lambda *args, **kwargs: output/"tools.json")
    monkeypatch.setattr(android, "_builder_user", lambda *args: "1000:1000")
    monkeypatch.setattr(android, "validation_plan", lambda *args: [("release-link", ["build", "--release"])])
    monkeypatch.setattr(android, "_runtime_dependencies", lambda *args: {"needed": [], "bundled": []})
    binary = output/"builds/rust/target-android-uid-1000-gid-1000/aarch64-linux-android/release/emulebb-rust"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"old development binary")
    class Docker:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def path(self, path): return str(path)
    monkeypatch.setattr(android, "LinuxDocker", Docker)
    def execute(docker, arguments, log, **kwargs):
        log.write_text("Synthetic compile")
        assert "EMULEBB_RELEASE_VERSION="+VERSION in arguments
        assert "EMULEBB_RELEASE_CHANNEL=nightly" in arguments
        if arguments[-2:] == ["build", "--release"]:
            assert not binary.exists()
            binary.write_bytes(b"new nightly binary")
    monkeypatch.setattr(android, "run_owned_container", execute)
    monkeypatch.setattr(android, "audit_android_elf", lambda *args: {"verified": True})
    path = android.validate_android(layout, abis=("arm64-v8a",), release_version=VERSION,
                                    channel="nightly", expected_source_sha=sha)
    report = json.loads(path.read_text())
    assert report["binaries"]["arm64-v8a"]["sha256"] == release._sha256(binary)
    assert report["version"] == VERSION and report["status"] == "passed"
