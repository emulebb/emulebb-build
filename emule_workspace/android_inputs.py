"""Reviewed, stable Android input identities; standard library only."""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import urlsplit

CONFIG = Path(__file__).parent / "release_assets/android/toolchain.json"
ROLES = frozenset({"command-line-tools", "ndk", "build-tools", "platform", "platform-tools", "gradle", "jdk", "android-cli"})
EMULATOR_CONFIG = CONFIG.with_name("emulator-inputs.json")
EMULATOR_ROLES = frozenset({"emulator", "system-image-min", "system-image-current",
                           "system-image-apk34", "system-image-apk35", "system-image-apk36"})
APK_IMAGES = ("apk34", "apk35", "apk36", "apk37")


def tool_inputs(config: Path = CONFIG) -> dict:
    """Reject incomplete or floating input declarations before any download."""
    return _read_inputs(config, "emulebb.android-tool-inputs/1", ROLES)


def emulator_inputs(config: Path = EMULATOR_CONFIG) -> dict:
    """Keep emulator/image pins separate from the mandatory local compile inputs."""
    return _read_inputs(config, "emulebb.android-emulator-inputs/1", EMULATOR_ROLES)


def emulator_subset(inputs: dict, images: tuple[str, ...]) -> dict:
    """Select emulator/ADB and requested guests without compiler-only inputs."""
    canonical = ["current" if image == "apk37" else image for image in images]
    if len(set(canonical)) != len(canonical) or any(image not in {"min", "current", *APK_IMAGES} for image in images):
        raise ValueError("Select unique supported native or APK Android images")
    assets = {"emulator": inputs["assets"]["emulator"]}
    for image, source in zip(images, canonical):
        assets["system-image-" + image] = inputs["assets"]["system-image-" + source]
    # WHY: guest boot needs ADB, but not the SDK/NDK/JDK/Gradle stack. Reuse
    # its single reviewed pin without depending on unrelated upstream downloads.
    assets["platform-tools"] = tool_inputs()["assets"]["platform-tools"]
    return {**inputs, "assets": assets}


def _read_inputs(config: Path, schema: str, roles: frozenset[str]) -> dict:
    value = json.loads(config.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or set(value) != {"schema", "host", "assets"}
            or value["schema"] != schema
            or value["host"] != "linux-x86_64"
            or not isinstance(value["assets"], dict) or set(value["assets"]) != roles):
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
        elif role.startswith("system-image-"):
            prefix += "sys-img/" + ("android/" if role == "system-image-min" else "google_apis/")
        filename = url.path.rsplit("/", 1)[-1]
        if (url.scheme != "https" or url.netloc != expected_host or not url.path.startswith(prefix)
                or url.query or url.fragment
                or (filename != "android-cli" if role == "android-cli" else not filename.endswith("." + expected_kind))
                or re.fullmatch(r"[A-Za-z0-9_.+-]+", filename) is None):
            raise RuntimeError(f"Android input must use a reviewed official archive URL: {role}")
        if role == "emulator" and re.fullmatch(r"emulator-linux_x64-[0-9]+\.zip", filename) is None:
            raise RuntimeError("Android emulator must use an immutable build archive")
        if role.startswith("system-image-"):
            version = re.fullmatch(r"(?P<api>[0-9]+(?:\.[0-9]+)?)-r(?P<revision>[0-9]+)", item["version"])
            if version is None:
                raise RuntimeError("Android system images require an API and exact revision")
            variant = "ps16k-" if role == "system-image-current" else ""
            expected = f"x86_64-{variant}{version['api']}_r{int(version['revision']):02}.zip"
            if filename != expected:
                raise RuntimeError("Android system image URL must match its API, page-size variant and revision")
    return value
