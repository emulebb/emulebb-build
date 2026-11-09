"""Reviewed, stable Android input identities; standard library only."""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import urlsplit

CONFIG = Path(__file__).parent / "release_assets/android/toolchain.json"
ROLES = frozenset({"command-line-tools", "ndk", "build-tools", "platform", "platform-tools", "gradle", "jdk", "android-cli"})


def tool_inputs(config: Path = CONFIG) -> dict:
    """Reject incomplete or floating input declarations before any download."""
    value = json.loads(config.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or set(value) != {"schema", "host", "assets"}
            or value["schema"] != "emulebb.android-tool-inputs/1"
            or value["host"] != "linux-x86_64"
            or not isinstance(value["assets"], dict) or set(value["assets"]) != ROLES):
        raise RuntimeError("Expected the complete reviewed Linux x86_64 Android tool inputs")
    for role, item in value["assets"].items():
        if (not isinstance(item, dict) or set(item) != {"version", "url", "sha256", "kind"}
                or not all(isinstance(part, str) for part in item.values())
                or re.fullmatch(r"[0-9]+(?:\.[0-9]+)*(?:[+-](?:r)?[0-9]+)?", item["version"]) is None
                or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None):
            raise RuntimeError(f"Invalid reviewed Android input: {role}")
        expected_kind = "binary" if role == "android-cli" else "tar.gz" if role == "jdk" else "zip"
        if item["kind"] != expected_kind:
            raise RuntimeError(f"Invalid reviewed Android input kind: {role}")
        url = urlsplit(item["url"])
        expected_host, prefix = ("services.gradle.org", "/distributions/") if role == "gradle" else (
            ("github.com", "/adoptium/temurin21-binaries/releases/download/") if role == "jdk" else
            ("dl.google.com", "/android/repository/"))
        if role == "android-cli":
            expected_host, prefix = "dl.google.com", "/android/cli/" + item["version"] + "/linux_x86_64/"
        filename = url.path.rsplit("/", 1)[-1]
        if (url.scheme != "https" or url.netloc != expected_host or not url.path.startswith(prefix)
                or url.query or url.fragment
                or (filename != "android-cli" if role == "android-cli" else not filename.endswith("." + expected_kind))
                or re.fullmatch(r"[A-Za-z0-9_.+-]+", filename) is None):
            raise RuntimeError(f"Android input must use a reviewed official archive URL: {role}")
    return value
