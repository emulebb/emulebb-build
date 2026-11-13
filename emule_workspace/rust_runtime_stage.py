"""Own registered Rust runtime bundles independently of the operator stage."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from uuid import uuid4

from .config import WorkspaceOptions, resolve_required_workspace_roots
from .git_environment import git_environment
from .locks import WORKSPACE_LOCK_TOKEN_ENV, WorkspaceLock
from .rust_source_cache import _source_identity
from .workstreams import _id, _safe_path, resolve_workstream

SCHEMA = "emulebb.rust-runtime-stage/1"
RECEIPT = "stage.json"
BINARIES = ("emulebb-rust", "emulebb-rust-diagnostics", "emulebb-nat-diagnostic")


def _require_lock(layout) -> None:
    """Require the existing delegated heavy owner without acquiring another lock."""
    lock = WorkspaceLock(layout, "Rust runtime stage", WorkspaceOptions(
        workspace_root=layout.emule_workspace_root, output_root=layout.output_root,
        workspace_name=layout.workspace_name,
    ))
    token = os.environ.get(WORKSPACE_LOCK_TOKEN_ENV, "")
    if not token or not lock._can_reenter(token):
        raise RuntimeError("Registered runtime staging requires the active workspace lock")


def _target(target: str | None) -> str:
    from .build import RUST_CLIENT_TARGETS

    if target is None:
        output = subprocess.check_output(["rustc", "-vV"], text=True, timeout=30)
        match = re.search(r"^host: (\S+)$", output, re.MULTILINE)
        if match is None:
            raise RuntimeError("Rust compiler did not report its host target")
        target = match.group(1)
    if target not in {value for targets in RUST_CLIENT_TARGETS.values() for value in targets.values()}:
        raise RuntimeError("Runtime stage requires a supported native Rust target")
    return target


def stage_root(layout, target: str | None = None) -> Path | None:
    """Resolve a registered stage while preserving canonical and hosted defaults."""
    source = layout.emulebb_rust_repo_root
    if source is None:
        raise RuntimeError("Rust runtime staging requires its selected source checkout")
    registration = resolve_workstream(layout.emule_workspace_root, layout.output_root, repo=source)
    if registration is None:
        return None
    target = _target(target)
    return _safe_path(layout.output_root, layout.output_root / "tools" / "workstreams"
                      / registration["id"] / "emulebb-rust" / target)


def _context(layout, target: str | None) -> dict:
    workspace, output = resolve_required_workspace_roots()
    if workspace != layout.emule_workspace_root.resolve() or output != layout.output_root.resolve():
        raise RuntimeError("Runtime stage roots must match the inherited workspace and OUTPUT")
    _require_lock(layout)
    target = _target(target)
    root = stage_root(layout, target)
    if root is None:
        raise RuntimeError("An owned runtime stage requires a registered Rust workstream")
    from .release import _emulebb_rust_native_source_roots

    source = layout.emulebb_rust_repo_root
    native = _emulebb_rust_native_source_roots(layout)
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], env=git_environment(), text=True, timeout=30,
    ).strip()
    return {"schema": SCHEMA, "workstream": root.parent.parent.name, "target": target,
            "stageRoot": str(root), "source": {"path": str(source), "revision": revision,
            "materials": _source_identity((source, *native))}}


def prepare_stage(layout, *, target: str | None = None, flavor: str = "fresh") -> dict:
    """Capture source identity once and allocate an unpublished bundle directory."""
    if flavor not in {"fresh", "regular", "diagnostics"}:
        raise RuntimeError("Unknown runtime producer flavor")
    context = _context(layout, target)
    root = Path(context["stageRoot"])
    root.parent.mkdir(parents=True, exist_ok=True)
    pending = _safe_path(layout.output_root, root.with_name(f".{root.name}.{uuid4().hex}.pending"))
    (pending / "bin").mkdir(parents=True)
    return {**context, "flavor": flavor, "pendingRoot": str(pending)}


def _pending(layout, description: dict) -> Path:
    """Admit only the selected registration's private pending directory."""
    root = stage_root(layout, description.get("target"))
    if root is None or description.get("stageRoot") != str(root):
        raise RuntimeError("Runtime stage description belongs to another source selection")
    pending = _safe_path(layout.output_root, Path(description.get("pendingRoot", "")))
    if pending.parent != root.parent or re.fullmatch(
            rf"\.{re.escape(root.name)}\.[0-9a-f]{{32}}\.pending", pending.name) is None:
        raise RuntimeError("Runtime pending directory is outside its registered owner")
    return pending


def pending_bin_dir(layout, description: dict) -> Path:
    """Return the admitted unpublished destination for native and WebUI producers."""
    _require_lock(layout)
    return _pending(layout, description) / "bin"


def _inventory(root: Path, target: str) -> list[dict]:
    """Bind native companions and the installed WebUI to their actual bytes."""
    suffix = ".exe" if "windows" in target else ""
    native = {name + suffix for name in BINARIES}
    native.update(name + ".pdb" for name in BINARIES)
    native.update(name.replace("-", "_") + ".pdb" for name in BINARIES)
    if not (root / "bin/webui/index.html").is_file():
        raise RuntimeError("Runtime bundle requires its staged WebUI index")
    files = []
    directories = set()
    for path in sorted(root.rglob("*")):
        _safe_path(root, path)
        if path.is_symlink() or path.is_junction():
            raise RuntimeError("Runtime bundle cannot own linked paths")
        if path.is_dir():
            directories.add(path.relative_to(root).as_posix())
            continue
        relative = path.relative_to(root).as_posix()
        if relative == RECEIPT:
            continue
        if not path.is_file() or not (relative.startswith("bin/webui/")
                                      or relative.startswith("bin/") and relative[4:] in native):
            raise RuntimeError("Runtime bundle contains an unsupported companion path")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files.append({"path": relative, "sha256": digest, "size": path.stat().st_size})
    ancestors = {parent.as_posix() for item in files for parent in Path(item["path"]).parents
                 if parent != Path(".")}
    if directories != ancestors:
        raise RuntimeError("Runtime bundle contains an unowned directory")
    return files


def _owned_stage(root: Path, *, workstream: str, target: str, identity_root: Path | None = None) -> dict:
    """Prove deletion ownership from the stored bytes, not the current source revision."""
    _safe_path(root.parent, root)
    if root.is_symlink() or root.is_junction() or not root.is_dir():
        raise RuntimeError("Runtime deletion requires a regular owned bundle directory")
    receipt = _safe_path(root, root / RECEIPT)
    if receipt.is_symlink() or not receipt.is_file():
        raise RuntimeError("Runtime deletion requires its intact ownership receipt; preserving existing data")
    data = json.loads(receipt.read_text(encoding="utf-8"))
    expected = {"schema": SCHEMA, "workstream": workstream, "target": target,
                "stageRoot": str(identity_root or root)}
    if (not isinstance(data, dict) or any(data.get(key) != value for key, value in expected.items())
            or data.get("producerFlavor") not in {"fresh", "regular", "diagnostics"}
            or data.get("files") != _inventory(root, target)):
        raise RuntimeError("Runtime ownership is unproven; preserving existing data")
    return data


def _remove_owned_stage(root: Path, data: dict, *, identity_root: Path | None = None) -> None:
    """Remove only receipt-owned files; nonrecursive rmdir preserves raced unknown data."""
    if _owned_stage(root, workstream=data["workstream"], target=data["target"],
                    identity_root=identity_root) != data:
        raise RuntimeError("Runtime ownership changed before cleanup; preserving existing data")
    directories = {root / parent for item in data["files"] for parent in Path(item["path"]).parents
                   if parent != Path(".")}
    for item in data["files"]:
        path = _safe_path(root, root / item["path"])
        if path.is_symlink() or path.is_junction() or not path.is_file():
            raise RuntimeError("Runtime member changed before cleanup; preserving existing data")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != item["sha256"] or path.stat().st_size != item["size"]:
            raise RuntimeError("Runtime member changed before cleanup; preserving existing data")
        _safe_path(root, path).unlink()
    # WHY: a running naked EXE can create a profile after the preflight. Never
    # recursively delete a previously generated directory: rmdir fails closed.
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _safe_path(root, directory).rmdir()
    _safe_path(root.parent, root)
    if set(root.iterdir()) != {root / RECEIPT}:
        raise RuntimeError("Runtime cleanup found new unowned data; preserving it")
    if json.loads(_safe_path(root, root / RECEIPT).read_text(encoding="utf-8")) != data:
        raise RuntimeError("Runtime receipt changed before cleanup; preserving it")
    _safe_path(root, root / RECEIPT).unlink()
    _safe_path(root.parent, root).rmdir()


def remove_empty_canonical_runtime(root: Path) -> None:
    """Clean only an empty canonical stage, which has no generated ownership receipt."""
    _safe_path(root.parent, root)
    if root.is_symlink() or root.is_junction():
        raise RuntimeError("Canonical runtime clean refuses redirected directories")
    if not root.exists():
        return
    if root.is_symlink() or root.is_junction() or not root.is_dir():
        raise RuntimeError("Canonical runtime clean requires an empty regular directory")
    try:
        root.rmdir()
    except OSError as error:
        raise RuntimeError("Canonical runtime is not proven disposable; use non-clean staging and an explicit "
                           "external --profile, preserving existing contents") from error


def _ready(root: Path, context: dict) -> dict:
    _safe_path(root.parent, root)
    receipt = _safe_path(root, root / RECEIPT)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(data.get(key) != value for key, value in context.items()):
        raise RuntimeError("Runtime receipt does not match the selected source identity")
    if data.get("files") != _inventory(root, context["target"]):
        raise RuntimeError("Runtime bundle bytes do not match its ready receipt")
    return data


def publish_stage(layout, description: dict) -> dict:
    """Publish a complete bundle; restore the prior stage on precommit promotion failure."""
    context = _context(layout, description.get("target"))
    if any(description.get(key) != value for key, value in context.items()):
        raise RuntimeError("Runtime source identity changed during the build")
    pending = _pending(layout, description)
    root = Path(context["stageRoot"])
    suffix = ".exe" if "windows" in context["target"] else ""
    flavor = description.get("flavor")
    required = {"fresh": BINARIES, "regular": (BINARIES[0],), "diagnostics": (BINARIES[1],)}.get(flavor)
    if required is None or any(not (pending / "bin" / (name + suffix)).is_file() for name in required):
        raise RuntimeError("Runtime producer did not provide its required native binaries")
    previous_owned = None
    if root.exists():
        previous_owned = _owned_stage(root, workstream=context["workstream"], target=context["target"])
        try:
            previous = _ready(root, context)
        except (OSError, ValueError, RuntimeError):
            previous = None
        if previous is not None:
            # WHY: a partial-flavor build may keep only proven companions from
            # the same material identity. Old regular/diagnostic bytes never
            # inherit a new source receipt merely because their names match.
            previous_files = {item["path"] for item in previous["files"]}
            for name in BINARIES:
                executable = name + suffix
                if (pending / "bin" / executable).exists() or "bin/" + executable not in previous_files:
                    continue
                companions = {executable, name + ".pdb", name.replace("-", "_") + ".pdb"}
                for companion in companions:
                    relative = "bin/" + companion
                    if relative in previous_files:
                        shutil.copy2(root / relative, pending / relative)
    data = {**context, "producerFlavor": flavor, "files": _inventory(pending, context["target"])}
    with (pending / RECEIPT).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if _context(layout, context["target"]) != context:
        raise RuntimeError("Runtime source identity changed before ready publication")
    backup = _safe_path(layout.output_root, root.with_name(f".{root.name}.{uuid4().hex}.previous"))
    try:
        if root.exists():
            # Ownership is separate from source freshness and is rechecked at
            # the destructive boundary, after pending construction completes.
            if _owned_stage(root, workstream=context["workstream"], target=context["target"]) != previous_owned:
                raise RuntimeError("Runtime ownership changed before promotion; preserving existing data")
            os.replace(root, backup)
        os.replace(pending, root)
    except BaseException:
        if backup.exists():
            if root.exists():
                os.replace(root, pending)
            os.replace(backup, root)
        raise
    result = {**data, "receipt": str(root / RECEIPT), "binDir": str(root / "bin")}
    if backup.exists():
        try:
            _remove_owned_stage(backup, previous_owned, identity_root=root)
        except (OSError, ValueError, RuntimeError) as error:
            # WHY: ready publication is committed. Backup cleanup can partly
            # succeed, so attempting rollback here could destroy valid bytes.
            result["cleanupWarnings"] = [{"operation": "remove-previous-runtime-stage",
                                          "path": str(backup), "error": str(error)}]
            print(f"Warning: runtime stage is ready; previous owned backup cleanup failed: {backup}: {error}",
                  file=sys.stderr)
    return result


def verify_stage(layout, *, target: str | None = None, diagnostics: bool = False) -> dict:
    """Fail before launch when source, target or any installed companion differs."""
    context = _context(layout, target)
    root = Path(context["stageRoot"])
    data = _ready(root, context)
    name = BINARIES[1] if diagnostics else BINARIES[0]
    name += ".exe" if "windows" in context["target"] else ""
    if not any(item["path"] == "bin/" + name for item in data["files"]):
        raise RuntimeError("Runtime stage is missing the selected executable flavor")
    return {**data, "receipt": str(root / RECEIPT), "binDir": str(root / "bin"),
            "executable": str(root / "bin" / name)}


def discard_stage(layout, description: dict) -> None:
    """Remove only this invocation's unpublished directory under the existing lock."""
    _require_lock(layout)
    pending = _pending(layout, description)
    if pending.exists():
        shutil.rmtree(pending)


@contextmanager
def runtime_stage_retirement(layout, workstream_id: str, *, enabled: bool = True):
    """Hold the normal heavy lock while the parent retires this lane's generated bundle."""
    if not enabled:
        yield lambda: None
        return
    workspace, output = resolve_required_workspace_roots()
    if workspace != layout.emule_workspace_root.resolve() or output != layout.output_root.resolve():
        raise RuntimeError("Runtime retirement roots must match inherited workspace and OUTPUT")
    root = _safe_path(output, output / "tools" / "workstreams" / _id(workstream_id) / "emulebb-rust")
    if not root.exists():
        yield lambda: None
        return
    lock = WorkspaceLock(layout, "workstream runtime retirement", WorkspaceOptions(
        workspace_root=workspace, output_root=output, workspace_name=layout.workspace_name,
    ))
    if not lock.acquire():
        raise RuntimeError("Runtime stage is busy under the workspace lock; retry retirement after validation")

    def retire():
        # WHY: remove only this admitted generated namespace after source/PR
        # preflight, while the same heavy owner excludes live validation.
        _safe_path(output, root)
        if root.exists():
            owned = []
            for child in root.iterdir():
                target = _target(child.name)
                owned.append((child, _owned_stage(child, workstream=workstream_id, target=target)))
            # Preflight the entire namespace before removing any target.
            for child, data in owned:
                _remove_owned_stage(child, data)
            root.rmdir()

    try:
        yield retire
    finally:
        lock.release()
