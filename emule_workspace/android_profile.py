"""Prove unchanged persisted schema material before an explicitly owned update."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
from uuid import uuid4

from .config import resolve_required_workspace_roots
from .git import git_output

SCHEMA_PATHS = ("crates/emulebb-metadata/src/schema.sql", "crates/emulebb-metadata/src/schema.rs")


def _blob(repo: Path, revision: str, path: str) -> bytes:
    try:
        return subprocess.check_output(["git", "--no-replace-objects", "-C", str(repo),
            "cat-file", "blob", revision + ":" + path], stderr=subprocess.PIPE, timeout=30)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("Cannot read immutable profile schema material: " + path) from error


def prove_profile_compatibility(layout, *, from_source_sha: str, to_source_sha: str) -> Path:
    """Compare exact Git blobs; this never reads, resets or migrates a user profile."""
    if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None
           for value in (from_source_sha, to_source_sha)):
        raise ValueError("Profile schema proof requires two explicit full source revisions")
    _workspace, output = resolve_required_workspace_roots()
    if layout.output_root.resolve() != output:
        raise RuntimeError("Profile schema proof must use the inherited output root")
    rust = layout.emulebb_rust_repo_root.resolve()
    if (Path(git_output(rust, "rev-parse", "--show-toplevel").strip()).resolve() != rust
            or git_output(rust, "status", "--porcelain", "--untracked-files=all").strip()
            or git_output(rust, "rev-parse", "HEAD").strip() != to_source_sha):
        raise RuntimeError("Profile schema proof requires the clean selected new Rust checkout")
    for revision in (from_source_sha, to_source_sha):
        if git_output(rust, "rev-parse", "--verify", revision + "^{commit}").strip() != revision:
            raise RuntimeError("Profile schema proof requires exact commit identities")
    materials = {}
    for path in SCHEMA_PATHS:
        old, new = (_blob(rust, revision, path) for revision in (from_source_sha, to_source_sha))
        if not old or len(old) > 4 * 1024 * 1024 or old != new:
            raise RuntimeError("Owned update requires unchanged persisted schema material: " + path)
        materials[path] = hashlib.sha256(new).hexdigest()
    encoded = json.dumps(materials, sort_keys=True, separators=(",", ":")).encode("utf-8")
    value = {"schema": "emulebb.android.profile-compatibility/1", "status": "passed",
             "fromSourceRevision": from_source_sha, "toSourceRevision": to_source_sha,
             "materials": materials, "metadataSchemaSha256": hashlib.sha256(encoded).hexdigest()}
    directory = output / "reports/android-profile-compatibility" / uuid4().hex
    if directory.is_symlink() or not directory.resolve().is_relative_to(output):
        raise RuntimeError("Profile schema proof must remain below the inherited output root")
    directory.mkdir(parents=True, exist_ok=False)
    receipt = directory / "profile-compatibility.json"
    receipt.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt
