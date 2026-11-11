"""Bind shared Cargo workspace artifacts to their actual selected source bytes.

Cargo's relative dep-info and timestamp freshness can reuse another checkout's
newer executable. Keep registry dependencies warm, but invalidate workspace
packages through Cargo before a different source tree may use a shared target.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import socket
import subprocess
import tomllib
from uuid import uuid4

from .git_environment import git_environment
from .locks import WorkspaceLock, WORKSPACE_LOCK_TOKEN_ENV
from .config import WorkspaceOptions
from .config import resolve_required_cargo_target_dir, resolve_required_workspace_roots
from .workstreams import _safe_path

SCHEMA = "emulebb.rust-source-cache/1"
RECEIPT = ".emulebb-source-identity.json"
MAX_FILES = 20000
MAX_BYTES = 256 * 1024 * 1024


def _source_identity(roots: tuple[Path, ...]) -> list[dict]:
    """Hash Rust then native inputs; only native tracked file links may be followed."""
    result = []
    for root_number, root in enumerate(roots):
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            env=git_environment(), capture_output=True, check=True, timeout=60,
        ).stdout
        names = sorted(set(os.fsdecode(name) for name in listing.split(b"\0") if name))
        if not names or len(names) > MAX_FILES:
            raise RuntimeError("Cargo source file inventory is empty or exceeds its bound")
        digest, size = hashlib.sha256(), 0
        tracked_links = None
        for name in names:
            path = root / name
            digest.update(name.encode("utf-8", errors="surrogatepass") + b"\0")
            if root_number and path.is_symlink():
                if tracked_links is None:
                    indexed = subprocess.run(
                        ["git", "-C", str(root), "ls-files", "--stage", "-z"],
                        env=git_environment(), capture_output=True, check=True, timeout=60,
                    ).stdout
                    tracked_links = set()
                    for entry in indexed.split(b"\0"):
                        header, separator, filename = entry.partition(b"\t")
                        if separator and header.startswith(b"120000 ") and header.endswith(b" 0"):
                            tracked_links.add(os.fsdecode(filename))
                if name not in tracked_links:
                    raise RuntimeError("Native source symlink must be tracked with Git symlink mode")
                # WHY: upstream native trees contain LICENSE/source links. Bind
                # their spelling and contained bytes without admitting redirected
                # source roots, directory ancestors or external material inputs.
                _safe_path(root.parent, root)
                if path.parent != root:
                    _safe_path(root, path.parent)
                link = os.readlink(path)
                encoded_link = os.fsencode(link)
                if os.path.isabs(link) or len(encoded_link) > 4096:
                    raise RuntimeError("Native source symlink must have a bounded relative target")
                target = Path(os.path.normpath(path.parent / link))
                target = _safe_path(root, target)
                if not target.is_file():
                    raise RuntimeError("Native source symlink target must be a contained regular file")
                digest.update(b"symlink\0" + encoded_link + b"\0")
                size += len(encoded_link)
                path = target
            else:
                path = _safe_path(root, path)
            if not path.exists():
                digest.update(b"deleted\0")
                continue
            if not path.is_file():
                raise RuntimeError("Cargo source input must be a regular file")
            size += path.stat().st_size
            if size > MAX_BYTES:
                raise RuntimeError("Cargo material source inputs exceed their byte bound")
            with path.open("rb") as stream:
                digest.update(hashlib.file_digest(stream, "sha256").digest())
        result.append({"root": str(root), "sha256": digest.hexdigest(), "files": len(names), "bytes": size})
    return result


def _host_runner(source: Path, environment: dict[str, str]):
    def run(arguments: list[str]) -> str:
        completed = subprocess.run(
            ["cargo", *arguments], cwd=source, env=environment,
            text=True, capture_output=True, timeout=600 if arguments[0] == "fetch" else 120, check=True,
        )
        if len(completed.stdout) > 8 * 1024 * 1024:
            raise RuntimeError("Cargo source-cache response exceeds its bound")
        return completed.stdout
    return run


def _normalized(value: str) -> str:
    windows = PureWindowsPath(value)
    if windows.is_absolute():
        return windows.as_posix().casefold().rstrip("/")
    return value.replace("\\", "/").rstrip("/")


def ensure_rust_source_cache(layout, *, target: Path | None = None, runner=None,
                             execution_source: str | None = None,
                             execution_target: str | None = None,
                             native_roots: tuple[Path, ...] = (),
                             execution_identity: dict | None = None,
                             prefetch: bool = False) -> dict:
    """Fence workspace artifacts under the existing lock; never clear registry caches.

    Container/WSL owners supply a Cargo executor and its literal mapped paths.
    The host still inventories the selected sources and owns the atomic receipt.
    """
    workspace, output = resolve_required_workspace_roots()
    if output != layout.output_root.resolve():
        raise RuntimeError("Cargo cache OUTPUT must match the inherited layout")
    if layout.workspace_name != "ci" and workspace != layout.emule_workspace_root.resolve():
        raise RuntimeError("Cargo source workspace must match the inherited layout")
    inherited_target = resolve_required_cargo_target_dir(output)
    source = layout.emulebb_rust_repo_root.absolute()
    if source == workspace and layout.workspace_name == "ci":
        # Hosted product checkouts intentionally occupy GITHUB_WORKSPACE itself.
        if source.is_symlink() or getattr(source, "is_junction", lambda: False)():
            raise RuntimeError("Cargo source root must not be redirected")
    else:
        source = _safe_path(workspace, source)
    if output.is_relative_to(workspace):
        raise RuntimeError("Cargo cache OUTPUT must be outside the source workspace")
    target = _safe_path(output, (target or layout.output_rust_target_root).absolute())
    if (target.parent != output / "builds/rust" or not re.fullmatch(
            r"target(?:-wsl|-linux-(?:gnu|musl)-(?:amd64|arm64)-uid-[0-9]+-gid-[0-9]+|-android-uid-[0-9]+-gid-[0-9]+)?",
            target.name)):
        raise RuntimeError("Cargo cache target must be an owned Rust target role")
    if runner is None and target != inherited_target:
        raise RuntimeError("Host Cargo cache must use the inherited target directory")
    lock = WorkspaceLock(layout, "Cargo source-cache fence", WorkspaceOptions(
        workspace_root=workspace, output_root=output, workspace_name=layout.workspace_name))
    if _safe_path(output, lock.metadata_path.absolute()) != output / "tmp/active-command-lock.json":
        raise RuntimeError("Cargo source-cache lock must belong to the inherited OUTPUT")
    token = os.environ.get(WORKSPACE_LOCK_TOKEN_ENV, "")
    try:
        if lock.metadata_path.stat().st_size > 65536:
            raise ValueError("owner record exceeds bound")
        owner = json.loads(lock.metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError("Cargo source-cache fence requires the active workspace lock") from error
    if (not token or not isinstance(owner, dict)
            or not isinstance(owner.get("delegation_token"), str) or not owner["delegation_token"]
            or type(owner.get("pid")) is not int or owner["pid"] <= 0
            or not isinstance(owner.get("machine_name"), str)
            or owner["machine_name"].lower() != socket.gethostname().lower()
            or owner.get("workspace_name") != layout.workspace_name
            or not isinstance(owner.get("workspace_root"), str)
            or Path(owner["workspace_root"]) != layout.emule_workspace_root
            or not lock._can_reenter(token)):
        raise RuntimeError("Cargo source-cache fence requires the active workspace lock")
    if not native_roots:
        from .release import _emulebb_rust_native_source_roots
        for name in ("MINIUPNP_ROOT", "PCPNATPMP_ROOT"):
            if name in os.environ and not Path(os.environ[name]).is_absolute():
                raise RuntimeError("Native Cargo input roots must be inherited absolute paths")
        native_roots = _emulebb_rust_native_source_roots(layout)
    if any(not path.is_absolute() for path in native_roots):
        raise RuntimeError("Native Cargo input roots must be inherited absolute paths")
    # Native overrides are read-only effective inputs, including operator-owned
    # absolute checkouts outside this workspace. Cleaning still touches only the
    # validated workspace target and its selected workspace packages.
    roots = (source, *(path.resolve() for path in native_roots))
    identity = _source_identity(roots)
    effective = execution_identity or {}
    path = _safe_path(output, target / RECEIPT)
    previous = None
    if path.exists():
        if path.stat().st_size > 1024 * 1024:
            raise RuntimeError("Cargo source-cache receipt exceeds its bound")
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            raise RuntimeError("Cargo source-cache receipt is invalid; explicit recovery required") from error
        if (not isinstance(previous, dict) or previous.get("schema") != SCHEMA
                or previous.get("target") != str(target) or not isinstance(previous.get("packages"), list)):
            raise RuntimeError("Cargo source-cache receipt has an ambiguous identity")
    execute = runner or _host_runner(source, git_environment())
    # Network policy belongs to the existing compile owner. Fetch is explicit,
    # never an implicit retry of a failed offline metadata/preflight operation.
    if prefetch:
        execute(["fetch", "--locked"])
    metadata = json.loads(execute(["metadata", "--offline", "--locked", "--no-deps", "--format-version", "1"]))
    expected_source = execution_source or str(source)
    expected_target = execution_target or str(target)
    if (_normalized(metadata.get("workspace_root", "")) != _normalized(expected_source)
            or _normalized(metadata.get("target_directory", "")) != _normalized(expected_target)):
        raise RuntimeError("Cargo metadata does not match selected source and target")
    members = metadata.get("workspace_members")
    packages = metadata.get("packages")
    if not isinstance(members, list) or not members or not isinstance(packages, list):
        raise RuntimeError("Cargo metadata has no bounded workspace package inventory")
    selected = [package for package in packages if package.get("id") in members]
    if len(selected) != len(members) or len(selected) > 256:
        raise RuntimeError("Cargo workspace package identity is incomplete")
    if any(not _normalized(package.get("manifest_path", "")).startswith(
            _normalized(expected_source) + "/") for package in selected):
        raise RuntimeError("Cargo workspace package escapes its selected source root")
    names = sorted(package["name"] for package in selected)
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in names):
        raise RuntimeError("Cargo workspace package name is invalid")
    if (previous and previous.get("sources") == identity and previous.get("execution") == effective
            and previous["packages"] == names):
        if _source_identity(roots) != identity:
            raise RuntimeError("Cargo material source inputs changed during cache validation")
        return previous
    # Cargo must never clean redirected artifact directories. Only a source
    # change pays this bounded directory inventory; warm reuse reads no artifacts.
    for count, (directory, children, _files) in enumerate(os.walk(target, followlinks=False)):
        if count >= 10000:
            raise RuntimeError("Cargo artifact directory inventory exceeds its bound")
        for name in children:
            _safe_path(output, Path(directory) / name)
    if previous and set(previous["packages"]).difference(names):
        raise RuntimeError("Cargo workspace removed packages require explicit scoped cache recovery")
    # WHY: a missing receipt cannot qualify already-populated shared output.
    # Cargo owns removal of only these workspace packages; dependencies survive.
    with (source / "Cargo.toml").open("rb") as stream:
        profiles = tomllib.load(stream).get("profile", {})
    profile_directories = ("debug", "release", *profiles)
    targets = []
    if target.exists():
        for child in sorted(target.iterdir()):
            if (child.is_dir() and re.fullmatch(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+){2,}", child.name)
                    and any((child / profile).is_dir() for profile in profile_directories)):
                _safe_path(output, child)
                targets.append(child.name)
    clean = ["clean", "--locked", "--offline",
             *[part for name in names for part in ("--package", name)]]
    for target_name in (None, *targets):
        arguments = clean + (["--target", target_name] if target_name else [])
        # Packaging uses release and explicit triples. Never rely on Cargo's
        # unqualified-clean default to invalidate a different profile's tests.
        for profile in ("dev", "release", *(name for name in profiles
                                             if name not in {"dev", "release", "test", "bench"})):
            execute([*arguments, "--profile", profile])
    if _source_identity(roots) != identity:
        raise RuntimeError("Cargo material source inputs changed during cache invalidation")
    receipt = {"schema": SCHEMA, "target": str(target), "sources": identity, "packages": names,
               "execution": effective}
    target.mkdir(parents=True, exist_ok=True)
    temporary = _safe_path(output, path.with_name(path.name + "." + uuid4().hex + ".tmp"))
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(receipt, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    print("Cargo source identity changed: invalidated workspace packages; dependency caches retained.", flush=True)
    return receipt
