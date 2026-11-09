"""Cross-target Rust validation shared by local WSL Docker and GitHub.

Compilation and ELF inspection do not qualify an Android device or an APK.
Runtime qualification belongs to the Android package/emulator harness.
"""
from __future__ import annotations

import json
from pathlib import Path
import struct
from uuid import uuid4

from .android_inputs import tool_inputs
from .android_toolchain import prepare_toolchain
from .config import resolve_required_cargo_target_dir, resolve_required_workspace_roots
from .linux_containers import LinuxDocker, _builder_user, linux_quality_plan, run_owned_container

TARGETS = {"arm64-v8a": ("aarch64-linux-android", 183), "x86_64": ("x86_64-linux-android", 62)}
MIN_API = 28
PAGE_SIZE = 16384


def compiler_environment(abi: str) -> dict[str, str]:
    """Use the NDK's API-specific compiler drivers for Rust and native C/C++."""
    if abi not in TARGETS:
        raise ValueError("Android validation supports arm64-v8a and x86_64")
    target, _machine = TARGETS[abi]
    version = tool_inputs()["assets"]["ndk"]["version"]
    compiler = f"/opt/android-tools/sdk/ndk/{version}/toolchains/llvm/prebuilt/linux-x86_64/bin/"
    key = target.replace("-", "_")
    return {f"CARGO_TARGET_{key.upper()}_LINKER": compiler + target + str(MIN_API) + "-clang",
            f"CC_{key}": compiler + target + str(MIN_API) + "-clang",
            f"CXX_{key}": compiler + target + str(MIN_API) + "-clang++",
            f"AR_{key}": compiler + "llvm-ar", f"RANLIB_{key}": compiler + "llvm-ranlib",
            "RUSTFLAGS": f"-C link-arg=-Wl,-z,max-page-size={PAGE_SIZE}"}


def audit_android_elf(binary: Path, abi: str, *, shared_library: bool = False) -> dict:
    """Reject wrong CPUs, non-PIE binaries, wrong loaders and 4KB-only segments."""
    if abi not in TARGETS:
        raise ValueError("Unknown Android ABI")
    size = binary.stat().st_size
    with binary.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:6] != b"\x7fELF\x02\x01":
            raise RuntimeError("Android native payload must be a 64-bit little-endian ELF")
        kind, machine = struct.unpack_from("<HH", header, 16)
        offset = struct.unpack_from("<Q", header, 32)[0]
        entry_size, count = struct.unpack_from("<HH", header, 54)
        if kind != 3 or machine != TARGETS[abi][1] or entry_size != 56 or not 1 <= count <= 128:
            raise RuntimeError("Android ELF type, CPU or program headers do not match the target")
        if offset < 64 or offset + count * entry_size > size:
            raise RuntimeError("Android ELF program headers are out of bounds")
        stream.seek(offset)
        segments = [struct.unpack("<IIQQQQQQ", stream.read(56)) for _ in range(count)]
        loads = []
        interpreters = []
        for segment in segments:
            segment_type, _flags, file_offset, address, _physical, file_size, memory_size, alignment = segment
            if file_offset + file_size > size or file_size > memory_size:
                raise RuntimeError("Android ELF segment is out of bounds")
            if segment_type == 1:
                if alignment < PAGE_SIZE or alignment & (alignment - 1) or file_offset % PAGE_SIZE != address % PAGE_SIZE:
                    raise RuntimeError("Android ELF load segments must support 16KB pages")
                loads.append(alignment)
            elif segment_type == 3:
                if not 1 <= file_size <= 256:
                    raise RuntimeError("Android ELF interpreter is invalid")
                stream.seek(file_offset)
                interpreters.append(stream.read(file_size))
        expected = [] if shared_library else [b"/system/bin/linker64\0"]
        if not loads or interpreters != expected:
            raise RuntimeError("Android ELF must use the expected Bionic loader and load segments")
    return {"abi": abi, "elfMachine": machine, "pageSize": PAGE_SIZE,
            "loadAlignments": loads, "sharedLibrary": shared_library}


def validation_plan(rust: Path, abi: str) -> list[tuple[str, list[str]]]:
    """Reuse the canonical Clippy/diagnostics definitions for each Android target."""
    target, _machine = TARGETS[abi]
    plan = linux_quality_plan(rust, target)
    for _label, command in plan:
        command.insert(1, "--offline")
    plan.extend([
        ("workspace-test-link", ["test", "--locked", "--offline", "--workspace", "--no-run", "--target", target]),
        ("release-link", ["build", "--locked", "--offline", "--release", "--target", target,
                          "-p", "emulebb-daemon", "--bin", "emulebb-rust"]),
    ])
    return plan


def validate_android(layout, *, abis: tuple[str, ...] = ("arm64-v8a", "x86_64"), wsl_distro: str = "Ubuntu") -> Path:
    """Compile both Android CPUs without publishing any local build output."""
    from . import release

    if not abis or len(set(abis)) != len(abis) or any(abi not in TARGETS for abi in abis):
        raise ValueError("Select unique supported Android ABIs")
    _workspace, output = resolve_required_workspace_roots()
    resolve_required_cargo_target_dir(output)
    if output != layout.output_root.resolve():
        raise RuntimeError("Android validation requires the inherited output root")
    rust = layout.emulebb_rust_repo_root
    provenance = release._emulebb_rust_package_provenance(layout, rust)
    toolchain_report = prepare_toolchain(layout, wsl_distro=wsl_distro)
    report = output / "reports/rust-android" / uuid4().hex / "validation.json"
    report.parent.mkdir(parents=True)
    value = {"schema": "emulebb.rust.android-validation/1", "status": "running", "source": provenance,
             "minApi": MIN_API, "inputs": tool_inputs(), "toolchainReport": str(toolchain_report),
             "runtimeExecution": "not-run", "checks": [], "elf": {}}
    try:
        with LinuxDocker(wsl_distro) as docker:
            user = _builder_user(docker)
            uid, gid = user.split(":")
            if uid == "0":
                raise RuntimeError("Android compilation requires a non-root Linux/WSL operator")
            owner = f"uid-{uid}-gid-{gid}"
            home = output / "cache/rust-android-home" / owner
            cargo_home = output / "cache/cargo-android" / owner
            target_dir = output / "builds/rust" / ("target-android-" + owner)
            for path in (home, cargo_home, target_dir):
                if not path.resolve().is_relative_to(output):
                    raise RuntimeError("Android build paths must remain inside the operator output root")
                path.mkdir(parents=True, exist_ok=True)
            miniupnp, pcpnatpmp = release._emulebb_rust_native_source_roots(layout)
            mounts = ["--mount", f"type=bind,source={docker.path(output)},target=/output",
                      "--mount", f"type=bind,source={docker.path(rust)},target=/source,readonly",
                      "--mount", f"type=bind,source={docker.path(miniupnp)},target=/native/miniupnp,readonly",
                      "--mount", f"type=bind,source={docker.path(pcpnatpmp)},target=/native/pcpnatpmp,readonly"]
            child = {"HOME": "/output/" + home.relative_to(output).as_posix(),
                     "CARGO_HOME": "/output/" + cargo_home.relative_to(output).as_posix(),
                     "CARGO_TARGET_DIR": "/output/" + target_dir.relative_to(output).as_posix(),
                     "MINIUPNP_ROOT": "/native/miniupnp", "PCPNATPMP_ROOT": "/native/pcpnatpmp",
                     "CARGO_BUILD_JOBS": "4", "RUSTUP_TOOLCHAIN": "1.98.1",
                     "EMULEBB_SOURCE_REVISION": provenance["source"]["emulebbRust"]["commit"],
                     "EMULEBB_RELEASE_VERSION": release._emulebb_rust_workspace_version(rust),
                     "EMULEBB_RELEASE_CHANNEL": "development"}
            environment = [arg for name, item in child.items() for arg in ("--env", f"{name}={item}")]
            base = ["--rm", "--platform", "linux/amd64", "--user", user, "--read-only",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777", *mounts, *environment,
                    "--workdir", "/source"]
            image = "local/emulebb-android-toolchain:reviewed"

            def check(label: str, command: list[str], *, abi: str | None = None) -> None:
                log = report.parent / (label + ".log")
                check_value = {"label": label, "command": command, "status": "failed", "log": str(log)}
                value["checks"].append(check_value)
                extra = compiler_environment(abi) if abi else {}
                arguments = [*base, "--network", "none" if abi else "bridge",
                             *[arg for name, item in extra.items() for arg in ("--env", f"{name}={item}")],
                             image, "cargo", *command]
                print(f"Android {label}; log: {log}", flush=True)
                try:
                    run_owned_container(docker, arguments, log, timeout=3600)
                    check_value["status"] = "passed"
                finally:
                    report.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

            check("fetch-locked", ["fetch", "--locked", *[arg for abi in abis for arg in ("--target", TARGETS[abi][0])]])
            for abi in abis:
                for label, command in validation_plan(rust, abi):
                    check(abi + "-" + label, command, abi=abi)
                binary = target_dir / TARGETS[abi][0] / "release/emulebb-rust"
                value["elf"][abi] = audit_android_elf(binary, abi)
        release._assert_emulebb_rust_package_provenance(layout, rust, provenance)
        value["status"] = "passed"
    except BaseException as error:
        value.update(status="failed", error=str(error))
        raise
    finally:
        report.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return report
