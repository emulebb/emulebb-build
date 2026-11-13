"""Register pinned SDK payloads for AGP inside a fresh APK-only tool image."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path, PurePosixPath
import os
import stat
import subprocess
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
import zipfile

from .android_inputs import tool_inputs
from .android_install import installation_layout
from .python_runtime import sha256

SDK_ROLES = ("command-line-tools", "ndk", "build-tools", "platform", "platform-tools")


def package_ids(inputs: dict) -> dict[str, str]:
    """Derive exact SDK package IDs from the existing reviewed tool pins."""
    assets = inputs["assets"]
    return {"command-line-tools": "cmdline-tools;" + assets["command-line-tools"]["version"],
            "ndk": "ndk;" + assets["ndk"]["version"],
            "build-tools": "build-tools;" + assets["build-tools"]["version"],
            "platform": "platforms;android-" + assets["platform"]["version"].split("-r")[0],
            "platform-tools": "platform-tools"}


def verify_sdk_payloads(cache: Path, tools: Path, inputs: dict) -> dict:
    """Reject SDKmanager drift; only its root package.xml is generated metadata."""
    layout, packages = installation_layout(inputs), package_ids(inputs)
    verified = {}
    for role in SDK_ROLES:
        item = inputs["assets"][role]
        archive = cache / item["sha256"] / urlsplit(item["url"]).path.rsplit("/", 1)[-1]
        if not archive.is_file() or archive.is_symlink() or sha256(archive) != item["sha256"]:
            raise RuntimeError("APK SDK verification requires the checksum-pinned archive: " + role)
        archive_root, installed = layout[role]
        root = tools / installed
        metadata = root / "package.xml"
        if metadata.is_symlink() or not metadata.is_file():
            raise RuntimeError("SDKmanager did not register package metadata: " + role)
        local = next((node for node in ET.parse(metadata).iter()
                      if node.tag.split("}")[-1] == "localPackage"), None)
        if local is None or local.attrib.get("path") != packages[role]:
            raise RuntimeError("SDKmanager registered a different package identity: " + role)
        expected = set()
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                relative = PurePosixPath(member.filename).relative_to(archive_root)
                if ".." in relative.parts or "\\" in member.filename:
                    raise RuntimeError("Unsafe reviewed SDK archive entry")
                if member.is_dir() or str(relative) in {".", "package.xml"}:
                    continue
                name = relative.as_posix()
                if name in expected:
                    raise RuntimeError("Duplicate SDK archive payload")
                expected.add(name)
                actual = root / name
                if stat.S_ISLNK(member.external_attr >> 16):
                    if not actual.is_symlink() or os.readlink(actual).encode() != source.read(member):
                        raise RuntimeError("SDK symlink differs from pinned archive: " + role)
                else:
                    if actual.is_symlink() or not actual.is_file():
                        raise RuntimeError("SDK payload is missing: " + role + "/" + name)
                    with source.open(member) as stream, actual.open("rb") as target:
                        if hashlib.file_digest(stream, "sha256").digest() != hashlib.file_digest(target, "sha256").digest():
                            raise RuntimeError("SDK payload differs from pinned archive: " + role + "/" + name)
        actual_files = {path.relative_to(root).as_posix() for path in root.rglob("*")
                        if path.is_file() or path.is_symlink()}
        if actual_files != expected | {"package.xml"}:
            raise RuntimeError("SDKmanager installed unexpected package files: " + role)
        verified[role] = {"package": packages[role], "archiveSha256": item["sha256"], "payloadFiles": len(expected)}
    return verified


def register_sdk(cache: Path, tools: Path) -> dict:
    """Run real SDK license acceptance/registration only while building the image."""
    inputs = tool_inputs()
    manager = tools / "sdk/cmdline-tools" / inputs["assets"]["command-line-tools"]["version"] / "bin/sdkmanager"
    command = [str(manager), "--sdk_root=" + str(tools / "sdk")]
    subprocess.run([*command, "--licenses"], input="y\n" * 100, text=True, check=True, timeout=600)
    subprocess.run([*command, "--install", *package_ids(inputs).values()], check=True, timeout=1200)
    return verify_sdk_payloads(cache, tools, inputs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    args = parser.parse_args()
    register_sdk(args.cache, args.tools)
