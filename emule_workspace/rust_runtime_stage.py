"""Own registered Rust runtime bundles independently of the operator stage."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from uuid import uuid4

from .config import WorkspaceOptions, resolve_required_workspace_roots
from .git_environment import git_environment
from .locks import WORKSPACE_LOCK_TOKEN_ENV, WorkspaceLock
from .rust_source_cache import _source_identity
from .workstreams import _safe_path, resolve_workstream

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


def _inventory(root: Path, target: str) -> list[dict]:
    """Bind native companions and the installed WebUI to their actual bytes."""
    suffix = ".exe" if "windows" in target else ""
    native = {name + suffix for name in BINARIES}
    native.update(name + ".pdb" for name in BINARIES)
    native.update(name.replace("-", "_") + ".pdb" for name in BINARIES)
    if not (root / "bin/webui/index.html").is_file():
        raise RuntimeError("Runtime bundle requires its staged WebUI index")
    files = []
    for path in sorted(root.rglob("*")):
        _safe_path(root, path)
        if path.is_dir():
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
    return files


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
    """Publish a complete source-bound bundle and restore the previous stage on failure."""
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
    if (root / RECEIPT).is_file():
        try:
            previous = _ready(root, context)
        except (OSError, ValueError, RuntimeError):
            previous = None
        if previous is not None:
            # WHY: a partial-flavor build may keep only proven companions from
            # the same material identity. Old regular/diagnostic bytes never
            # inherit a new source receipt merely because their names match.
            for item in previous["files"]:
                relative = Path(item["path"])
                destination = pending / relative
                if relative.parent == Path("bin") and not destination.exists():
                    shutil.copy2(root / relative, destination)
    data = {**context, "files": _inventory(pending, context["target"])}
    with (pending / RECEIPT).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if _context(layout, context["target"]) != context:
        raise RuntimeError("Runtime source identity changed before ready publication")
    backup = _safe_path(layout.output_root, root.with_name(f".{root.name}.{uuid4().hex}.previous"))
    if root.exists():
        os.replace(root, backup)
    try:
        os.replace(pending, root)
    except BaseException:
        if backup.exists():
            if root.exists():
                os.replace(root, pending)
            os.replace(backup, root)
        raise
    if backup.exists():
        shutil.rmtree(backup)
    return {**data, "receipt": str(root / RECEIPT), "binDir": str(root / "bin")}


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
