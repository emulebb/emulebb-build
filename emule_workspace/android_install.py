"""Install checksum-verified Android tools into a fresh Linux image layer.

WHY: keep SDK extraction shared by local Docker and GitHub, preserve NDK Unix
symlinks/modes, and never synthesize SDK license acceptance files.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import subprocess
import tarfile
from urllib.parse import urlsplit
import zipfile

from .android_inputs import emulator_inputs, emulator_subset, tool_inputs
from .python_runtime import sha256


def installation_layout(inputs: dict) -> dict[str, tuple[str, str]]:
    """Map official archive roots to the SDK's versioned package locations."""
    archives = inputs["assets"]
    return {
        "command-line-tools": ("cmdline-tools", "sdk/cmdline-tools/" + archives["command-line-tools"]["version"]),
        "build-tools": ("android-" + archives["build-tools"]["version"].rsplit(".", 1)[0],
                        "sdk/build-tools/" + archives["build-tools"]["version"]),
        "ndk": ("android-ndk-r" + archives["ndk"]["version"].split(".")[0], "sdk/ndk/" + archives["ndk"]["version"]),
        "platform": ("android-" + archives["platform"]["version"].split("-r")[0],
                     "sdk/platforms/android-" + archives["platform"]["version"].split("-r")[0]),
        "platform-tools": ("platform-tools", "sdk/platform-tools"),
        "gradle": ("gradle-" + archives["gradle"]["version"], "gradle"),
        "jdk": ("jdk-" + archives["jdk"]["version"], "jdk"),
        "android-cli": ("", "bin/android"),
    }


def archive_names(archive: Path) -> list[str]:
    """Inspect data before asking native extractors to preserve executable modes."""
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as source:
            return source.namelist()
    with tarfile.open(archive, "r:gz") as source:
        return [item.name for item in source.getmembers()]


def install_inputs(cache: Path, destination: Path) -> Path:
    """Validate every input before creating a new, non-overwriting installation."""
    inputs = tool_inputs()
    return _install_inputs(inputs, installation_layout(inputs), cache, destination, "tool-inputs.json")


def emulator_layout(inputs: dict) -> dict[str, tuple[str, str]]:
    """Keep runtime SDK packages in a fresh emulator-only SDK root."""
    layout = {"emulator": ("emulator", "emulator")}
    for role, item in inputs["assets"].items():
        if role == "emulator":
            continue
        api = item["version"].split("-r")[0]
        tag = "default" if role == "system-image-min" else "google_apis_ps16k"
        layout[role] = ("x86_64", f"system-images/android-{api}/{tag}/x86_64")
    return layout


def install_emulator_inputs(cache: Path, destination: Path, *, images: tuple[str, ...] = ()) -> Path:
    """Verify and install selected guests without creating license acceptance state."""
    inputs = emulator_subset(emulator_inputs(), images)
    return _install_inputs(inputs, emulator_layout(inputs), cache, destination, "emulator-inputs.json", runtime_readable=True)


def _runtime_package_mode(mode: int, *, directory: bool) -> int:
    """Grant read/search and existing execute access without write or set-ID grants."""
    return (mode & 0o700) | 0o444 | (0o111 if directory or mode & 0o111 else 0)


def _install_inputs(inputs: dict, layout: dict, cache: Path, destination: Path, receipt_name: str,
                    *, runtime_readable: bool = False) -> Path:
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("Android tools require native Linux x86_64 (use WSL Docker on Windows)")
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("Android tool installation requires a fresh destination")
    checked = {}
    for role, item in inputs["assets"].items():
        archive = cache / item["sha256"] / urlsplit(item["url"]).path.rsplit("/", 1)[-1]
        if (archive.is_symlink() or not archive.resolve().is_relative_to(cache.resolve())
                or not archive.is_file() or sha256(archive) != item["sha256"]):
            raise RuntimeError(f"Unverified Android input: {role}")
        names = archive_names(archive) if item["kind"] != "binary" else []
        root, _target = layout[role]
        if item["kind"] != "binary" and (not names or any(PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
                            or "\\" in name or not PurePosixPath(name).parts
                            or PurePosixPath(name).parts[0] != root for name in names)):
            raise RuntimeError(f"Unexpected Android archive layout: {role}")
        checked[role] = archive
    destination.mkdir(parents=True, exist_ok=False)
    for role, archive in checked.items():
        if inputs["assets"][role]["kind"] == "binary":
            target = destination / layout[role][1]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive, target)
            target.chmod(0o755)
            continue
        scratch = destination / ".extract"
        scratch.mkdir()
        try:
            if archive.name.endswith(".zip"):
                subprocess.run(["unzip", "-q", str(archive), "-d", str(scratch)], check=True, timeout=300)
            else:
                subprocess.run(["tar", "-xzf", str(archive), "-C", str(scratch)], check=True, timeout=300)
            root, target = layout[role]
            installed = destination / target
            installed.parent.mkdir(parents=True, exist_ok=True)
            # WHY: move within this new image directory, never modify host tools.
            shutil.move(str(scratch / root), str(installed))
        finally:
            shutil.rmtree(scratch)
    if runtime_readable:
        # WHY: old AOSP packages ship owner-only modes. Only this fresh image
        # installation is normalized; non-root probes must read packages and
        # execute existing binaries without gaining write or set-ID permissions.
        for path in (destination, *destination.rglob("*")):
            if not path.resolve().is_relative_to(destination.resolve()):
                raise RuntimeError("Android runtime package links must remain inside the fresh installation")
            if path.is_symlink():
                continue
            mode = path.stat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise RuntimeError("Android runtime packages require regular files and directories")
            path.chmod(_runtime_package_mode(mode, directory=stat.S_ISDIR(mode)))
    receipt = destination / receipt_name
    receipt.write_text(json.dumps(inputs, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--emulator", action="store_true")
    parser.add_argument("--images", default="", help="Comma-separated min/current guest images.")
    arguments = parser.parse_args()
    if arguments.images and not arguments.emulator:
        parser.error("--images requires --emulator")
    if arguments.emulator:
        images = tuple(arguments.images.split(",")) if arguments.images else ()
        print(install_emulator_inputs(arguments.cache, arguments.destination, images=images))
    else:
        print(install_inputs(arguments.cache, arguments.destination))


if __name__ == "__main__":
    main()
