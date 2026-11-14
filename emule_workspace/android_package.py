"""Source-bound experimental Android daemon archives, never APK qualification."""
from __future__ import annotations

import hashlib
from copy import deepcopy
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
from uuid import uuid4

from . import release
from .android_inputs import tool_inputs
from .android_validation import MIN_API, PAGE_SIZE, TARGETS, audit_android_elf, validate_android

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_UNPACKED_BYTES = 768 * 1024 * 1024
MAX_FILES = 20_000
LIMITATIONS = (
    "Experimental developer daemon, not an APK or supported Android application.",
    "Runtime execution, completed-file delivery, routing and VPN behavior are unqualified.",
)


def identity(version: str, channel: str, source_sha: str) -> None:
    """Reject development relabelling and versions unrelated to the actual source."""
    match = re.fullmatch(r"(.+)\.nightly\.[0-9]{8}\.g([0-9a-f]{7,40})", version)
    if (channel != "nightly" or not release._is_release_version(version)
            or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
            or match is None or not source_sha.startswith(match[2])):
        raise ValueError("Android developer bundles require a source-bound nightly identity")


def names(version: str, abi: str) -> dict[str, str]:
    """Return the four flat public files; per-ABI checksums stay out of merged artifacts."""
    if abi not in TARGETS:
        raise ValueError("Unsupported Android ABI")
    stem = f"emulebb-rust-v{version}-android-{abi}"
    return {"archive": stem + ".tar.zst", "manifest": stem + ".tar.zst.manifest.json",
            "sbom": stem + ".sbom.spdx.json", "proof": stem + "-compile-elf.json"}


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def package_android(layout, *, abi: str, release_version: str, channel: str,
                    expected_source_sha: str, wsl_distro: str = "Ubuntu") -> Path:
    """Compile once under the distribution identity, then export only those verified bytes."""
    identity(release_version, channel, expected_source_sha)
    if abi not in TARGETS:
        raise ValueError("Unsupported Android ABI")
    rust = layout.emulebb_rust_repo_root
    if not release_version.startswith(release._emulebb_rust_workspace_version(rust) + ".nightly."):
        raise RuntimeError("Android nightly version differs from the source Cargo version")
    report_path = validate_android(
        layout, abis=(abi,), wsl_distro=wsl_distro, release_version=release_version,
        channel=channel, expected_source_sha=expected_source_sha,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    provenance = report["source"]
    if (report.get("status") != "passed" or report.get("version") != release_version
            or report.get("channel") != channel
            or provenance["source"]["emulebbRust"]["commit"] != expected_source_sha):
        raise RuntimeError("Android bundle requires its successful exact-identity compilation")
    output = layout.output_root.resolve()
    record = report["binaries"][abi]
    binary = output / record["path"]
    if (binary.is_symlink() or not binary.resolve().is_relative_to(output)
            or not binary.is_file() or release._sha256(binary) != record["sha256"]
            or binary.stat().st_size != record["sizeBytes"]):
        raise RuntimeError("Android executable changed after compilation")
    root = output / "packages/rust-android" / release_version / abi
    if root.is_symlink() or not root.resolve().is_relative_to(output):
        raise RuntimeError("Android package output must remain inside the inherited output root")
    root.mkdir(parents=True, exist_ok=True)
    scratch = output / "tmp/android-package" / uuid4().hex
    scratch.mkdir(parents=True)
    try:
        webui = scratch / "webui"
        subprocess.run([sys.executable, "-B", str(rust / "tools/build_webui.py"),
                        "--stage-dir", str(webui)], check=True)
        return write_bundle(layout, root=root, scratch=scratch, binary=binary,
                            webui=webui, report=report, abi=abi)
    finally:
        # Only this invocation's freshly allocated, external-output scratch.
        if scratch.is_symlink() or not scratch.resolve().is_relative_to(output / "tmp/android-package"):
            raise RuntimeError("Android package scratch identity changed")
        shutil.rmtree(scratch)


def write_bundle(layout, *, root: Path, scratch: Path, binary: Path, webui: Path,
                 report: dict, abi: str) -> Path:
    """Stage a verified compilation without rebuilding or upgrading its evidence claim."""
    version, channel = report["version"], report["channel"]
    provenance = report["source"]
    identity(version, channel, provenance["source"]["emulebbRust"]["commit"])
    if report.get("status") != "passed" or report.get("runtimeExecution") != "not-run":
        raise RuntimeError("Android export requires passed compile-only evidence")
    if not webui.is_dir() or not (webui / "index.html").is_file():
        raise RuntimeError("Android developer bundle requires the built WebUI")
    public = names(version, abi)
    for name in (*public.values(), "SHA256SUMS"):
        target = root / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise RuntimeError("Android package destinations must be regular owned files")
    stage = scratch / "staging/emulebb-rust"
    stage.mkdir(parents=True)
    shutil.copy2(binary, stage / "emulebb-rust")
    (stage / "emulebb-rust").chmod(0o755)
    shutil.copytree(webui, stage / "webui", symlinks=True)
    rust = layout.emulebb_rust_repo_root
    for name in ("LICENSE",):
        shutil.copy2(rust / name, stage / name)
    release._stage_emulebb_rust_bootstrap(rust, stage)
    mini, pcp = release._emulebb_rust_native_source_roots(layout)
    (stage / "licenses").mkdir()
    from .android_apk_notices import stage_source_license
    stage_source_license(mini / "LICENSE", stage / "licenses/MiniUPnPc-LICENSE")
    shutil.copy2(pcp / "COPYING", stage / "licenses/libpcpnatpmp-COPYING")
    runtime = deepcopy(report["runtimeDependencies"][abi])
    for item in runtime["bundled"]:
        for key, checksum, destination in (
            ("path", "sha256", stage / "lib" / item["name"]),
            ("licensePath", "licenseSha256", stage / "licenses/Android-NDK-NOTICE"),
        ):
            source = layout.output_root / item[key]
            if (source.is_symlink() or not source.resolve().is_relative_to(layout.output_root.resolve())
                    or release._sha256(source) != item[checksum]):
                raise RuntimeError("Pinned Android runtime changed after dependency audit")
            destination.parent.mkdir(exist_ok=True)
            shutil.copy2(source, destination)
            item[key] = destination.relative_to(stage.parent).as_posix()
    (stage / "run-developer.sh").write_text(
        '#!/system/bin/sh\n'
        'bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1\n'
        'export LD_LIBRARY_PATH="$bundle_dir/lib"\n'
        'exec "$bundle_dir/emulebb-rust" "$@"\n', encoding="utf-8", newline="\n",
    )
    (stage / "README.txt").write_text(
        f"eMuleBB Rust {version}, Android {abi}, API {MIN_API}+, 16KB-compatible ELF.\n"
        + "\n".join(LIMITATIONS) + "\n"
        "For controlled developer/ADB use only; this is not an installable application.\n"
        "Use ./run-developer.sh so any bundled pinned runtime is found before Android system libraries.\n"
        "Run ./run-developer.sh from this writable unpacked directory (or add --profile .).\n"
        "The default profile is the executable directory; --profile paths resolve from the shell working directory.\n"
        "Open http://127.0.0.1:20017/ and enter change_me_123change_me_123change_me_123.\n"
        "The adjacent webui directory supplies the browser console. Never publish profile secrets.\n",
        encoding="utf-8",
    )
    for path in stage.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise RuntimeError("Android package staging cannot contain links or special files")
    audit = audit_android_elf(stage / "emulebb-rust", abi)
    record = report["binaries"][abi]
    if release._sha256(stage / "emulebb-rust") != record["sha256"]:
        raise RuntimeError("Android executable changed before staging")
    components = release._emulebb_rust_sbom_components(layout, rust)
    if runtime["bundled"]:
        components.append(release._component_spdx_package(
            name="Android NDK libc++ runtime", declared_license="Apache-2.0 WITH LLVM-exception",
            version=tool_inputs()["assets"]["ndk"]["version"],
            comment="Pinned Android NDK archive; exact SHA-256 and NOTICE are bound in the package manifest.",
        ))
    sbom = release._build_spdx_sbom(
        name=f"eMuleBB Rust {version} Android {abi} developer bundle",
        namespace=f"https://github.com/emulebb/emulebb-rust/releases/download/rust-v{version}/{public['archive']}.sbom",
        package_name=f"emulebb-rust-android-{abi}", package_version=version,
        package_license="GPL-2.0-only", package_comment=" ".join(LIMITATIONS),
        package_root=stage, release_root=root,
        components=components,
    )
    _write_json(stage / "SBOM.spdx.json", sbom)
    shutil.copy2(stage / "SBOM.spdx.json", root / public["sbom"])
    archive = root / public["archive"]
    release._write_emulebb_rust_portable_archive(stage, archive)
    common = {**provenance, "version": version, "channel": channel, "abi": abi,
              "target": TARGETS[abi][0], "minApi": MIN_API, "pageSize": PAGE_SIZE,
              "experimental": True, "distribution": "developer-daemon",
              "runtimeExecution": "not-run", "testCompilation": "workspace",
              "testExecution": "not-run", "buildExecution": "cross",
              "limitations": list(LIMITATIONS), "elfAudit": audit,
              "binary": {"path": "emulebb-rust/emulebb-rust", "sha256": record["sha256"],
                         "sizeBytes": record["sizeBytes"]},
              "toolInputs": report["inputs"]}
    common["runtimeDependencies"] = runtime
    proof = {**common, "schema": "emulebb.android.compile-elf/1", "status": "passed",
             "checks": [{key: item[key] for key in ("label", "command", "status")}
                        for item in report["checks"]]}
    _write_json(root / public["proof"], proof)
    manifest = {**common, "package": "emulebb-rust", "platform": f"android-{abi}",
                "asset": archive.name, "sha256": release._sha256(archive),
                "sbom": public["sbom"], "sbomSha256": release._sha256(root / public["sbom"]),
                "compileProof": public["proof"], "compileProofSha256": release._sha256(root / public["proof"]),
                "files": release._staged_package_file_hashes(stage)}
    _write_json(root / public["manifest"], manifest)
    release._assert_emulebb_rust_package_provenance(layout, rust, provenance)
    release._write_sha256sums(root / "SHA256SUMS", tuple(root / name for name in public.values()))
    return archive


def inspect_archive(archive: Path, *, abi: str, files: dict[str, str], sbom_sha256: str) -> dict:
    """Audit bounded regular tar payloads and the actual ELF without extracting paths."""
    if archive.is_symlink() or not archive.is_file() or not 0 < archive.stat().st_size <= MAX_ARCHIVE_BYTES:
        raise RuntimeError("Android archive is not a bounded regular file")
    expected = {**files, "emulebb-rust/SBOM.spdx.json": sbom_sha256}
    actual = {}
    total = 0
    audit = None
    binary_size = None
    libraries = {}
    try:
        with tarfile.open(archive, "r:zst") as bundle:
            for index, member in enumerate(bundle):
                path = PurePosixPath(member.name)
                if (index >= MAX_FILES or not path.parts or path.parts[0] != "emulebb-rust"
                        or path.is_absolute() or ".." in path.parts or "\\" in member.name
                        or member.name != path.as_posix() or not (member.isdir() or member.isfile())):
                    raise RuntimeError("Android archive contains an unsafe member")
                if member.isdir():
                    continue
                if member.name in actual or member.size < 0:
                    raise RuntimeError("Android archive contains duplicate files")
                if member.mode & 0o7000 or (path.name in {"emulebb-rust", "run-developer.sh"}
                                          and not member.mode & 0o111):
                    raise RuntimeError("Android archive has unsafe or non-executable file modes")
                total += member.size
                if total > MAX_UNPACKED_BYTES or member.name not in expected:
                    raise RuntimeError("Android archive exceeds its exact bounded inventory")
                with bundle.extractfile(member) as source:
                    actual[member.name] = hashlib.file_digest(source, "sha256").hexdigest()
                    if member.name == "emulebb-rust/emulebb-rust":
                        source.seek(0)
                        audit = audit_android_elf_stream(source, member.size, abi)
                        binary_size = member.size
                    elif member.name.startswith("emulebb-rust/lib/"):
                        source.seek(0)
                        from .android_validation import audit_android_elf_stream as inspect_elf
                        libraries[path.name] = inspect_elf(source, member.size, abi, shared_library=True)
        if actual != expected or audit is None:
            raise RuntimeError("Android archive differs from its exact file checksums")
    except (tarfile.TarError, EOFError, OSError) as error:
        raise RuntimeError("Cannot inspect Android archive") from error
    return {"binary": audit, "binarySize": binary_size, "libraries": libraries}


def audit_android_elf_stream(stream, size: int, abi: str) -> dict:
    """Adapt tar members to the single canonical ELF auditor without filesystem extraction."""
    from .android_validation import audit_android_elf_stream as audit
    return audit(stream, size, abi)
