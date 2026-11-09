"""Bootstrap and select the workspace's private, pinned Python environment.

This module deliberately uses only the standard library: an existing Python
can install the private runtime before any workspace dependencies are present.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

sys.dont_write_bytecode = True
BUILD_ROOT = Path(__file__).resolve().parents[1]
UV_CONFIG = BUILD_ROOT / "emule_workspace/release_assets/python/uv.json"
BOOTSTRAP_COMMAND = "python -B -m emule_workspace bootstrap-python"


def python_version() -> str:
    """Read the single workspace Python pin, never a floating download alias."""
    value = (BUILD_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    if re.fullmatch(r"3\.\d+\.\d+", value) is None:
        raise RuntimeError("The workspace requires an exact stable CPython version")
    return value


def host_target() -> str:
    """Select a reviewed native tool asset for the six supported CI hosts."""
    machine = platform.machine().lower()
    architecture = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(machine)
    system = {"Windows": "pc-windows-msvc", "Darwin": "apple-darwin", "Linux": "unknown-linux-gnu"}.get(platform.system())
    if architecture is None or system is None:
        raise RuntimeError("No reviewed workspace Python bootstrap for this host")
    if platform.system() == "Linux" and platform.libc_ver()[0] == "musl":
        raise RuntimeError("The workspace host bootstrap requires GNU Linux; Alpine remains a product image target")
    return f"{architecture}-{system}"


def output_root() -> Path:
    """Validate inherited operator paths without deriving or repairing them."""
    root_value = os.environ.get("EMULEBB_WORKSPACE_ROOT", "")
    output_value = os.environ.get("EMULEBB_WORKSPACE_OUTPUT_ROOT", "")
    if not root_value or not output_value:
        raise RuntimeError("EMULEBB_WORKSPACE_ROOT and EMULEBB_WORKSPACE_OUTPUT_ROOT must be inherited")
    root, output = Path(root_value), Path(output_value)
    if not root.is_absolute() or not output.is_absolute() or not root.is_dir() or not output.is_dir():
        raise RuntimeError("Workspace and output roots must be existing absolute directories")
    root, output = root.resolve(), output.resolve()
    if output.is_relative_to(root) or not _is_workspace_builder(root):
        raise RuntimeError("Bootstrap requires the canonical builder or its managed worktree and an output root outside the workspace")
    return output


def _is_workspace_builder(root: Path) -> bool:
    """Accept linked builder checkouts without trusting a directory name alone."""
    canonical = (root / "repos/emulebb-build").resolve()
    selected = BUILD_ROOT.resolve()
    if selected == canonical:
        return True
    agents = (root / "workspaces/agents").resolve()
    if (not selected.is_relative_to(agents) or selected.name != "emulebb-build"
            or len(selected.relative_to(agents).parts) != 2):
        return False
    # WHY: bootstrap precedes third-party imports and workstream registration.
    # Git's shared repository identity proves this is a linked canonical builder.
    def common_directory(checkout: Path) -> Path:
        from .git_environment import git_environment
        value = subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "--git-common-dir"],
            text=True, stderr=subprocess.DEVNULL, env=git_environment(),
        ).strip()
        return (checkout / value).resolve()
    try:
        return common_directory(selected) == common_directory(canonical)
    except (OSError, subprocess.CalledProcessError):
        return False


def environment_root(output: Path) -> Path:
    """Keep separate environments for native host architectures and Python pins."""
    return output / "tools/python/workspace" / host_target() / python_version()


def environment_python(environment: Path) -> Path:
    """Return the venv launcher without resolving away its Unix symlink."""
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def sha256(path: Path) -> str:
    """Hash tool inputs using the standard streaming implementation."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def environment_inputs() -> dict[str, str]:
    """Bind installed dependencies to the reviewed project, lock and tool pins."""
    return {name: sha256(BUILD_ROOT / name) for name in (
        ".python-version", "pyproject.toml", "uv.lock", "emule_workspace/release_assets/python/uv.json")}


def selected_python() -> Path:
    """Require a completed private environment; never fall back to system Python."""
    output = output_root()
    environment = environment_root(output)
    executable = environment_python(environment)
    marker = environment / "emulebb-environment.json"
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8"))
        valid = (recorded.get("schema") == "emulebb.workspace-python/1"
                 and recorded.get("inputs") == environment_inputs() and executable.is_file())
    except (OSError, ValueError):
        valid = False
    if not valid:
        raise RuntimeError(f"The private workspace Python environment needs setup: run {BOOTSTRAP_COMMAND}")
    return executable


def download_verified(url: str, destination: Path, expected: str) -> None:
    """Publish an official tool archive only after validating its pinned digest."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if sha256(destination) != expected:
            raise RuntimeError(f"Cached workspace tool checksum mismatch: {destination}")
        return
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "emulebb-workspace-bootstrap"})
                with urllib.request.urlopen(request, timeout=60) as source, temporary_path.open("wb") as target:
                    shutil.copyfileobj(source, target)
                break
            except (urllib.error.URLError, TimeoutError, ConnectionResetError) as error:
                if isinstance(error, urllib.error.HTTPError) and error.code not in {429, 500, 502, 503, 504}:
                    raise
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        if sha256(temporary_path) != expected:
            raise RuntimeError("Downloaded workspace tool checksum mismatch")
        temporary_path.replace(destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def _uv_binary(archive: Path, executable: str) -> bytes:
    """Read one bounded regular executable without extracting archive paths."""
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as source:
            members = [item for item in source.infolist() if not item.is_dir() and PurePosixPath(item.filename).name == executable]
            if len(members) != 1 or members[0].file_size > 128 * 1024 * 1024:
                raise RuntimeError("Reviewed uv archive must contain one bounded executable")
            return source.read(members[0])
    with tarfile.open(archive, "r:gz") as source:
        members = [item for item in source.getmembers() if item.isfile() and PurePosixPath(item.name).name == executable]
        if len(members) != 1 or members[0].size > 128 * 1024 * 1024:
            raise RuntimeError("Reviewed uv archive must contain one bounded executable")
        stream = source.extractfile(members[0])
        if stream is None:
            raise RuntimeError("Cannot read reviewed uv executable")
        return stream.read()


def ensure_uv(output: Path) -> Path:
    """Install a checksum-pinned manager without touching system PATH or Python."""
    config = json.loads(UV_CONFIG.read_text(encoding="utf-8"))
    target = host_target()
    version, expected = config["version"], config["sha256"][target]
    if re.fullmatch(r"\d+\.\d+\.\d+", version) is None or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise RuntimeError("The reviewed uv version or digest is invalid")
    executable = "uv.exe" if os.name == "nt" else "uv"
    filename = f"uv-{target}." + ("zip" if os.name == "nt" else "tar.gz")
    archive = output / "cache/workspace-python" / version / filename
    download_verified(f"https://github.com/astral-sh/uv/releases/download/{version}/{filename}", archive, expected)
    binary = _uv_binary(archive, executable)
    installed = output / "tools/uv" / version / target / executable
    installed.parent.mkdir(parents=True, exist_ok=True)
    if not installed.is_file() or hashlib.sha256(installed.read_bytes()).digest() != hashlib.sha256(binary).digest():
        installed.write_bytes(binary)
    installed.chmod(0o755)
    actual = subprocess.check_output([str(installed), "--version"], text=True, timeout=30).split()
    if actual[:2] != ["uv", version]:
        raise RuntimeError("The installed workspace manager has an unexpected version")
    return installed


def bootstrap(*, update_lock: bool = False, reinstall: bool = False) -> Path:
    """Install managed CPython and synchronize only reviewed locked dependencies."""
    output = output_root()
    if reinstall and Path(sys.base_prefix).resolve().is_relative_to((output / "tools/python/managed").resolve()):
        raise RuntimeError("Run private interpreter reinstallation from the external bootstrap Python")
    uv = ensure_uv(output)
    environment = environment_root(output)
    child_env = os.environ.copy()
    # These configure this manager invocation only; inherited workspace roots,
    # Cargo targets, operator Python and global PATH remain operator state.
    child_env.update(UV_PYTHON_INSTALL_DIR=str(output / "tools/python/managed"),
                     UV_PYTHON_BIN_DIR=str(output / "tools/python/bin"),
                     UV_CACHE_DIR=str(output / "cache/uv"),
                     UV_PROJECT_ENVIRONMENT=str(environment),
                     PYTHONDONTWRITEBYTECODE="1")
    scratch = output / "tmp/workspace-python"
    scratch.mkdir(parents=True, exist_ok=True)
    child_env.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch))
    def run(*arguments: str) -> None:
        subprocess.run([str(uv), *arguments], cwd=BUILD_ROOT, env=child_env, check=True)
    if reinstall:
        run("python", "uninstall", python_version())
    run("python", "install", "--no-bin", "--no-registry", python_version())
    if update_lock:
        run("lock", "--python", python_version())
    run("sync", "--locked", "--extra", "dev", "--no-install-project", "--managed-python", "--python", python_version())
    executable = environment_python(environment)
    probe = json.loads(subprocess.check_output([str(executable), "-I", "-B", "-c",
        "import json,platform,sys; print(json.dumps({'version':platform.python_version(),'implementation':platform.python_implementation(),'prefix':sys.prefix}))"],
        text=True, timeout=30))
    if (probe["version"] != python_version() or probe["implementation"] != "CPython"
            or Path(probe["prefix"]).resolve() != environment.resolve()):
        raise RuntimeError("The managed workspace interpreter does not match the selected environment")
    marker = {"schema": "emulebb.workspace-python/1", "python": probe["version"], "host": host_target(), "inputs": environment_inputs()}
    (environment / "emulebb-environment.json").write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    print(f"Workspace Python {probe['version']}: {executable}", flush=True)
    return executable


def run_script(arguments: list[str]) -> int:
    """Run a hook script with the private interpreter, preserving Git's stdin."""
    return subprocess.call([str(selected_python()), "-B", *arguments])


def main() -> None:
    """Bootstrap before importing dependencies, otherwise select owned execution."""
    if sys.argv[1:2] == ["bootstrap-python"]:
        parser = argparse.ArgumentParser(prog="python -m emule_workspace bootstrap-python")
        parser.add_argument("--update-lock", action="store_true", help="Explicitly regenerate the reviewed dependency lock")
        parser.add_argument("--reinstall", action="store_true", help="Reinstall only the private managed interpreter")
        arguments = parser.parse_args(sys.argv[2:])
        bootstrap(update_lock=arguments.update_lock, reinstall=arguments.reinstall)
        return
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # Hosted jobs select an exact native interpreter through their workflow.
        if platform.python_version() != python_version():
            raise RuntimeError(f"GitHub must select workspace Python {python_version()}")
    else:
        executable = selected_python()
        if Path(sys.prefix).resolve() != executable.parent.parent.resolve():
            raise SystemExit(subprocess.call([str(executable), "-B", "-m", "emule_workspace", *sys.argv[1:]]))
        if platform.python_version() != python_version() or platform.python_implementation() != "CPython":
            raise RuntimeError("The private workspace interpreter needs reinstallation")
    from .cli import main as cli_main
    cli_main()
