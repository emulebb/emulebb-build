"""Synthetic source-bound Android archives; no compiler, SDK, Docker or network."""
from __future__ import annotations
from pathlib import Path
import struct
from types import SimpleNamespace
from emule_workspace import android_package as package, release
from emule_workspace.android_inputs import tool_inputs

SOURCE = dict(zip(("emulebbRust", "emulebbBuild", "emulebbTooling", "emulebbMiniupnp", "emulebbLibpcpnatpmp"),
                  (str(n) * 40 for n in (1, 2, 4, 5, 6))))
WORKFLOW = {"repository": "example/project", "runId": "101", "runAttempt": "1", "workflowSha": "7" * 40}
VERSION = "0.1.0-beta.2.nightly.20261109.g11111111"


def native_elf(path: Path, abi="arm64-v8a", *, shared=False):
    data = bytearray(4096)
    data[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HH", data, 16, 3, 183 if abi == "arm64-v8a" else 62)
    struct.pack_into("<Q", data, 32, 64)
    struct.pack_into("<HH", data, 54, 56, 1 if shared else 2)
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 5, 0, 0, 0, 4096, 4096, 16384)
    if not shared:
        interpreter = b"/system/bin/linker64\0"
        data[512:512+len(interpreter)] = interpreter
        struct.pack_into("<IIQQQQQQ", data, 120, 3, 4, 512, 512, 0, len(interpreter), len(interpreter), 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def synthetic_bundle(root, monkeypatch, *, abi="arm64-v8a", cpp=False):
    root.mkdir(parents=True, exist_ok=True)
    output, rust, mini, pcp = (root / name for name in ("output", "rust", "mini", "pcp"))
    for path in (output, rust, mini / "miniupnpc", pcp):
        path.mkdir(parents=True, exist_ok=True)
    for name in ("LICENSE", "emulebb-rust-settings.example.toml"):
        (rust / name).write_text("Synthetic public fixture")
    (mini / "LICENSE").write_text("BSD License\nCopyright Synthetic Authors\n" + "Redistribution is permitted. " * 5)
    (mini / "miniupnpc/LICENSE").write_text("../LICENSE")
    (pcp / "COPYING").write_text("Synthetic BSD fixture")
    binary = native_elf(output / "compiled/emulebb-rust", abi)
    webui = output / "webui"
    webui.mkdir()
    (webui / "index.html").write_text("<html>Synthetic console</html>")
    provenance = {"schema": "emulebb.rust.package/2", "sourceDirty": False,
                  "source": {name: {"commit": sha} for name, sha in SOURCE.items()},
                  "origin": "github-actions", "workflow": WORKFLOW}
    checks = [{"label": "fetch-locked", "command": ["fetch", "--locked"], "status": "passed"}]
    target = package.TARGETS[abi][0]
    for label, verb, extra in (("clippy", "clippy", []), ("packet-diagnostics", "check", ["--features", "packet-diagnostics"]),
                                ("workspace-test-link", "test", ["--workspace", "--no-run"]),
                                ("release-link", "build", ["--release", "-p", "emulebb-daemon", "--bin", "emulebb-rust"])):
        checks.append({"label": abi+"-"+label, "command": [verb, "--locked", "--offline", "--target", target, *extra],
                       "status": "passed"})
    needed = ["libc.so"] + (["libc++_shared.so"] if cpp else [])
    runtime = {"needed": sorted(needed),
               "readelf": "Dynamic section at offset 0x100 contains entries:\n" +
                          "\n".join("(NEEDED) Shared library: ["+name+"]" for name in needed),
               "bundled": []}
    if cpp:
        library = native_elf(output / "runtime/libc++_shared.so", abi, shared=True)
        notice = output / "runtime/NOTICE.toolchain"
        notice.write_text("Synthetic Apache LLVM notice")
        ndk = tool_inputs()["assets"]["ndk"]
        runtime["bundled"].append({
            "name": library.name, "path": library.relative_to(output).as_posix(),
            "sha256": release._sha256(library), "sizeBytes": library.stat().st_size,
            "elfAudit": package.audit_android_elf(library, abi, shared_library=True),
            "needed": ["libc.so"], "readelf": "Dynamic section\n(NEEDED) Shared library: [libc.so]",
            "licensePath": notice.relative_to(output).as_posix(), "licenseSha256": release._sha256(notice),
            "ndkVersion": ndk["version"], "ndkArchiveSha256": ndk["sha256"],
        })
    report = {"status": "passed", "version": VERSION, "channel": "nightly", "source": provenance,
              "runtimeExecution": "not-run", "checks": checks, "inputs": tool_inputs(),
              "binaries": {abi: {"path": binary.relative_to(output).as_posix(),
                                "sha256": release._sha256(binary), "sizeBytes": binary.stat().st_size}},
              "runtimeDependencies": {abi: runtime}}
    layout = SimpleNamespace(output_root=output, emulebb_rust_repo_root=rust)
    monkeypatch.setattr(release, "_emulebb_rust_native_source_roots", lambda layout: (mini, pcp))
    monkeypatch.setattr(release, "_emulebb_rust_sbom_components", lambda *args: [])
    monkeypatch.setattr(release, "_assert_emulebb_rust_package_provenance", lambda *args: None)
    destination, scratch = output / "packages" / abi, output / "scratch"
    destination.mkdir(parents=True)
    scratch.mkdir()
    archive = package.write_bundle(layout, root=destination, scratch=scratch, binary=binary,
                                   webui=webui, report=report, abi=abi)
    return destination, report, layout, archive
