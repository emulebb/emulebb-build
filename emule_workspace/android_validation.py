"""Cross-target Rust validation shared by local WSL Docker and GitHub.

Compilation and ELF inspection do not qualify an Android device or an APK.
Runtime qualification belongs to the Android package/emulator harness.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import struct
from uuid import uuid4

from .android_inputs import tool_inputs
from .android_toolchain import prepare_toolchain
from .config import resolve_required_cargo_target_dir, resolve_required_workspace_roots
from .linux_containers import LinuxDocker, _builder_user, linux_quality_plan, run_owned_container

TARGETS = {"arm64-v8a": ("aarch64-linux-android", 183), "x86_64": ("x86_64-linux-android", 62)}
MIN_API = 28
PAGE_SIZE = 16384
SYSTEM_LIBRARIES = frozenset({"libc.so", "libm.so", "libdl.so", "liblog.so", "libandroid.so", "libz.so"})


def needed_libraries(readelf: str) -> list[str]:
    """Read llvm-readelf's dynamic dependency names; never implement another ELF parser."""
    if not isinstance(readelf, str) or "Dynamic section" not in readelf:
        raise RuntimeError("Android dependency proof requires llvm-readelf dynamic output")
    names = re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", readelf)
    if len(names) != len(set(names)) or any("/" in name or "\\" in name for name in names):
        raise RuntimeError("Android dynamic dependencies contain invalid library names")
    return sorted(names)


def _runtime_dependencies(docker, base: list[str], image: str, output: Path,
                          report_root: Path, abi: str, binary: Path) -> dict:
    """Audit NDK dependency closure and retain its pinned C++ runtime when required."""
    from .release import _sha256
    inputs = tool_inputs()["assets"]["ndk"]
    ndk = "/opt/android-tools/sdk/ndk/" + inputs["version"]
    readelf = ndk + "/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-readelf"

    def dynamic(path: str, name: str) -> str:
        log = report_root / (abi + "-" + name + "-needed.txt")
        run_owned_container(docker, [*base, "--network", "none", image, readelf, "--dynamic", "--wide", path],
                            log, timeout=120)
        return log.read_text(encoding="utf-8")

    text = dynamic("/output/" + binary.relative_to(output).as_posix(), "daemon")
    needed = needed_libraries(text)
    if set(needed) - SYSTEM_LIBRARIES - {"libc++_shared.so"}:
        raise RuntimeError("Android executable has unresolved non-system dependencies")
    result = {"needed": needed, "readelf": text, "bundled": []}
    if "libc++_shared.so" in needed:
        root = report_root / "runtime-libs" / abi
        root.mkdir(parents=True)
        source = ndk + "/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib/" + TARGETS[abi][0] + "/libc++_shared.so"
        library, notice = root / "libc++_shared.so", root / "NOTICE.toolchain"
        for src, target in ((source, library), (ndk + "/NOTICE.toolchain", notice)):
            log = report_root / (abi + "-" + target.name + "-copy.log")
            run_owned_container(docker, [*base, "--network", "none", image, "cp", src,
                                         "/output/" + target.relative_to(output).as_posix()], log, timeout=120)
        library_text = dynamic(source, "libcxx")
        dependencies = needed_libraries(library_text)
        if set(dependencies) - SYSTEM_LIBRARIES:
            raise RuntimeError("Pinned Android C++ runtime has unresolved dependencies")
        result["bundled"].append({
            "name": library.name, "path": library.relative_to(output).as_posix(),
            "sha256": _sha256(library), "sizeBytes": library.stat().st_size,
            "elfAudit": audit_android_elf(library, abi, shared_library=True),
            "needed": dependencies, "readelf": library_text,
            "licensePath": notice.relative_to(output).as_posix(), "licenseSha256": _sha256(notice),
            "ndkVersion": inputs["version"], "ndkArchiveSha256": inputs["sha256"],
        })
    return result


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
    with binary.open("rb") as stream:
        return audit_android_elf_stream(stream, binary.stat().st_size, abi, shared_library=shared_library)


def audit_android_elf_stream(stream, size: int, abi: str, *, shared_library: bool = False) -> dict:
    """Apply the same bounded ELF audit to regular files and archived members."""
    if abi not in TARGETS:
        raise ValueError("Unknown Android ABI")
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


def _tls_test_executables(messages: str, output: Path, target_dir: Path) -> dict[str, Path]:
    """Select the two exact library test artifacts, never a guessed target glob."""
    selected = {}
    for line in messages.splitlines():
        item = json.loads(line)
        if item.get("reason") != "compiler-artifact" or not item.get("profile", {}).get("test"):
            continue
        target = item.get("target", {})
        for role in ("core", "daemon"):
            if (target.get("name") != "emulebb_" + role or target.get("kind") != ["lib"]
                    or target.get("src_path") != f"/source/crates/emulebb-{role}/src/lib.rs"):
                continue
            executable = item.get("executable")
            if not isinstance(executable, str) or not executable.startswith("/output/") or role in selected:
                raise RuntimeError("Android TLS proof requires exactly one executable per library")
            path = output / executable.removeprefix("/output/")
            if (path.is_symlink() or not path.is_file()
                    or not path.resolve().is_relative_to((target_dir / TARGETS["x86_64"][0] / "debug/deps").resolve())):
                raise RuntimeError("Android TLS test artifact escaped its owned x86_64 target")
            selected[role] = path
    if set(selected) != {"core", "daemon"}:
        raise RuntimeError("Android TLS proof is missing a required library test executable")
    return selected


def _export_tls_payload(docker, base: list[str], image: str, output: Path, directory: Path,
                        provenance: dict, executables: dict[str, Path], *, version: str, channel: str) -> dict:
    """Freeze audited Android library test bytes for the fixed guest TLS proof."""
    from . import release

    root = directory / "tls-payload"
    root.mkdir(mode=0o700)
    files, dependencies = {}, {}
    names = {"core": "core-tests", "daemon": "daemon-tests", "runtime": "emulebb-rust"}
    if set(executables) != set(names):
        raise RuntimeError("Android first-launch proof requires both test libraries and the production daemon")
    for role, executable in executables.items():
        audit = audit_android_elf(executable, "x86_64")
        dependency_root = directory / ("tls-" + role)
        dependency_root.mkdir()
        closure = _runtime_dependencies(docker, base, image, output, dependency_root, "x86_64", executable)
        dependencies[role] = closure
        sources = [(names[role], executable, audit)]
        for library in closure["bundled"]:
            sources.append((library["name"], output / library["path"], library["elfAudit"]))
        for name, source, elf in sources:
            if name not in {*names.values(), "libc++_shared.so"}:
                raise RuntimeError("Unexpected Android TLS runtime dependency")
            if source.is_symlink() or not source.resolve().is_relative_to(output.resolve()):
                raise RuntimeError("Android TLS runtime dependency escaped its owned output")
            size = source.stat().st_size
            if not 0 < size <= 1024**3:
                raise RuntimeError("Android TLS test artifact exceeds its bounded payload size")
            identity = {"sha256": release._sha256(source), "sizeBytes": size, "elf": elf}
            if name in files:
                if files[name] != identity:
                    raise RuntimeError("Android TLS libraries require conflicting runtime dependency bytes")
                continue
            target = root / name
            shutil.copyfile(source, target)
            if release._sha256(target) != identity["sha256"]:
                raise RuntimeError("Android TLS payload changed during staging")
            files[name] = identity
    value = {"schema": "emulebb.android.tls-payload/2", "status": "passed", "abi": "x86_64",
             "sourceDirty": False, "source": provenance["source"], "files": files,
             "buildIdentity": {"version": version, "channel": channel, "platform": "x64", "build": "release",
                               "sourceRevision": provenance["source"]["emulebbRust"]["commit"]},
             "runtimeDependencies": dependencies, "runtimeExecution": "not-run",
             "origin": provenance["origin"], "workflow": provenance["workflow"]}
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"path": manifest.relative_to(output).as_posix(), "sha256": release._sha256(manifest)}


def validate_android(layout, *, abis: tuple[str, ...] = ("arm64-v8a", "x86_64"), wsl_distro: str = "Ubuntu",
                     release_version: str | None = None, channel: str = "development",
                     expected_source_sha: str | None = None, tls_payload: bool = False) -> Path:
    """Compile both Android CPUs without publishing any local build output."""
    from . import release

    if not abis or len(set(abis)) != len(abis) or any(abi not in TARGETS for abi in abis):
        raise ValueError("Select unique supported Android ABIs")
    if tls_payload and abis != ("x86_64",):
        raise ValueError("Android TLS guest payload requires only the native x86_64 guest ABI")
    _workspace, output = resolve_required_workspace_roots()
    resolve_required_cargo_target_dir(output)
    if output != layout.output_root.resolve():
        raise RuntimeError("Android validation requires the inherited output root")
    rust = layout.emulebb_rust_repo_root
    provenance = release._emulebb_rust_package_provenance(layout, rust)
    version = release_version or release._emulebb_rust_workspace_version(rust)
    if channel not in {"development", "nightly"} or not release._is_release_version(version):
        raise ValueError("Android compilation requires a valid explicit distribution identity")
    if channel == "nightly" and release_version is None:
        raise ValueError("Android nightly compilation requires its release version")
    if expected_source_sha is not None and provenance["source"]["emulebbRust"]["commit"] != expected_source_sha:
        raise RuntimeError("Android compilation source differs from the expected source revision")
    toolchain_report = prepare_toolchain(layout, wsl_distro=wsl_distro)
    report = output / "reports/rust-android" / uuid4().hex / "validation.json"
    report.parent.mkdir(parents=True)
    value = {"schema": "emulebb.rust.android-validation/1", "status": "running", "source": provenance,
             "minApi": MIN_API, "inputs": tool_inputs(), "toolchainReport": str(toolchain_report),
             "runtimeExecution": "not-run", "version": version, "channel": channel,
             "checks": [], "elf": {}, "binaries": {}, "runtimeDependencies": {}}
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
                     "EMULEBB_RELEASE_VERSION": version,
                     "EMULEBB_RELEASE_CHANNEL": channel}
            environment = [arg for name, item in child.items() for arg in ("--env", f"{name}={item}")]
            base = ["--rm", "--platform", "linux/amd64", "--user", user, "--read-only",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777", *mounts, *environment,
                    "--workdir", "/source"]
            image = "local/emulebb-android-toolchain:reviewed"

            from .rust_source_cache import ensure_rust_source_cache
            cache_invocation = 0
            def cache_cargo(arguments):
                nonlocal cache_invocation
                cache_invocation += 1
                return run_owned_container(docker, [*base, image, "cargo", *arguments],
                                           report.parent / f"source-cache-{cache_invocation}.log",
                                           timeout=120, capture=True)

            def check(label: str, command: list[str], *, abi: str | None = None, capture: bool = False) -> str:
                log = report.parent / (label + ".log")
                check_value = {"label": label, "command": command, "status": "failed", "log": str(log)}
                value["checks"].append(check_value)
                extra = compiler_environment(abi) if abi else {}
                arguments = [*base, "--network", "none" if abi else "bridge",
                             *[arg for name, item in extra.items() for arg in ("--env", f"{name}={item}")],
                             image, "cargo", *command]
                print(f"Android {label}; log: {log}", flush=True)
                try:
                    result = run_owned_container(docker, arguments, log, timeout=3600, **({"capture": True} if capture else {}))
                    check_value["status"] = "passed"
                    return result
                finally:
                    report.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

            # Cargo also compiles host build dependencies and procedural macros.
            # Fetch the complete lockfile before disabling network access.
            check("fetch-locked", ["fetch", "--locked"])
            ensure_rust_source_cache(layout, target=target_dir, runner=cache_cargo,
                                     execution_source="/source", execution_target=child["CARGO_TARGET_DIR"],
                                     native_roots=(miniupnp, pcpnatpmp),
                                     execution_identity={"image": image, "inputs": tool_inputs()})
            for abi in abis:
                binary = target_dir / TARGETS[abi][0] / "release/emulebb-rust"
                if release_version is not None:
                    if binary.is_symlink() or not binary.resolve().is_relative_to(target_dir.resolve()):
                        raise RuntimeError("Android final executable must remain inside its owned target")
                    # Do not export an old development executable if a producer
                    # fails to recreate the final link under the nightly identity.
                    binary.unlink(missing_ok=True)
                artifacts = None
                for label, command in validation_plan(rust, abi):
                    capture = tls_payload and label == "workspace-test-link"
                    if capture:
                        command = [*command, "--message-format=json"]
                    result = check(abi + "-" + label, command, abi=abi, capture=capture)
                    if capture:
                        artifacts = _tls_test_executables(result, output, target_dir)
                value["elf"][abi] = audit_android_elf(binary, abi)
                value["binaries"][abi] = {"path": binary.relative_to(output).as_posix(),
                                         "sha256": release._sha256(binary),
                                         "sizeBytes": binary.stat().st_size}
                if release_version is not None:
                    value["runtimeDependencies"][abi] = _runtime_dependencies(
                        docker, base, image, output, report.parent, abi, binary,
                    )
                if tls_payload:
                    if artifacts is None:
                        raise RuntimeError("Android TLS export requires the successful canonical workspace test-link")
                    value["tlsPayload"] = _export_tls_payload(
                        docker, base, image, output, report.parent, provenance, {**artifacts, "runtime": binary},
                        version=version, channel=channel)
        release._assert_emulebb_rust_package_provenance(layout, rust, provenance)
        value["status"] = "passed"
    except BaseException as error:
        value.update(status="failed", error=str(error))
        raise
    finally:
        report.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return report
