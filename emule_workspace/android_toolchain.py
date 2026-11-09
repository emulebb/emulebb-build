"""Reviewed Android build inputs shared by local WSL/Docker and hosted CI.

This preparation step downloads data only. SDK installation, license handling,
compilation and release signing belong to subsequent explicit operations.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from .config import resolve_required_workspace_roots
from .python_runtime import download_verified, sha256

CONFIG = Path(__file__).parent / "release_assets/android/toolchain.json"
ROLES = frozenset({"command-line-tools", "ndk", "build-tools", "platform", "platform-tools", "gradle", "jdk"})


def tool_inputs(config: Path = CONFIG) -> dict:
    """Reject incomplete or floating input declarations before any download."""
    value = json.loads(config.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or set(value) != {"schema", "host", "archives"}
            or value["schema"] != "emulebb.android-tool-inputs/1"
            or value["host"] != "linux-x86_64"
            or not isinstance(value["archives"], dict) or set(value["archives"]) != ROLES):
        raise RuntimeError("Expected the complete reviewed Linux x86_64 Android tool inputs")
    for role, item in value["archives"].items():
        if (not isinstance(item, dict) or set(item) != {"version", "url", "sha256"}
                or not all(isinstance(part, str) for part in item.values())
                or re.fullmatch(r"[0-9]+(?:\.[0-9]+)*(?:[+-](?:r)?[0-9]+)?", item["version"]) is None
                or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None):
            raise RuntimeError(f"Invalid reviewed Android input: {role}")
        url = urlsplit(item["url"])
        expected_host, prefix = ("services.gradle.org", "/distributions/") if role == "gradle" else (
            ("github.com", "/adoptium/temurin21-binaries/releases/download/") if role == "jdk" else
            ("dl.google.com", "/android/repository/"))
        filename = url.path.rsplit("/", 1)[-1]
        if (url.scheme != "https" or url.netloc != expected_host or not url.path.startswith(prefix)
                or url.query or url.fragment or not filename.endswith((".zip", ".tar.gz"))
                or re.fullmatch(r"[A-Za-z0-9_.+-]+", filename) is None):
            raise RuntimeError(f"Android input must use a reviewed official archive URL: {role}")
    return value


def fetch_tool_inputs() -> Path:
    """Cache exact archives and record verified bytes without running SDK tools."""
    _workspace, output = resolve_required_workspace_roots()
    inputs = tool_inputs()
    cache = output / "cache/android-tool-inputs"
    if not cache.resolve().is_relative_to(output):
        raise RuntimeError("Android tool cache must remain within the inherited output root")
    records = {}
    for role, item in sorted(inputs["archives"].items()):
        filename = urlsplit(item["url"]).path.rsplit("/", 1)[-1]
        target = cache / item["sha256"] / filename
        if target.is_symlink() or not target.resolve().is_relative_to(cache.resolve()):
            raise RuntimeError("Android input cache cannot use external paths or symlink archives")
        download_verified(item["url"], target, item["sha256"])
        # WHY: the receipt describes actual cache bytes, including cache hits;
        # it is never an SDK installation or release-provenance claim.
        if not target.is_file() or sha256(target) != item["sha256"]:
            raise RuntimeError(f"Android input checksum mismatch: {role}")
        records[role] = {**item, "path": target.relative_to(output).as_posix(), "size": target.stat().st_size}
    receipt = cache / "verified-inputs.json"
    if receipt.is_symlink():
        raise RuntimeError("Android input receipt cannot replace a symlink")
    value = {"schema": "emulebb.android-tool-cache/1", "host": inputs["host"], "archives": records}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, receipt)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return receipt
