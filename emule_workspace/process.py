"""Subprocess and tool-resolution helpers for workspace commands."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .config import CARGO_TARGET_DIR_ENV, WORKSPACE_OUTPUT_ROOT_ENV


@dataclass(frozen=True)
class PythonInvocation:
    """Resolved Python executable and launcher prefix."""

    executable: Path
    prefix: tuple[str, ...] = ()

    def command(self, args: Sequence[str | os.PathLike[str]]) -> list[str]:
        """Returns a complete command line using this Python launcher."""

        return [str(self.executable), *self.prefix, *[str(arg) for arg in args]]


def find_tool(names: Sequence[str]) -> Path | None:
    """Returns the first executable found on PATH for one of the supplied names."""

    for name in names:
        resolved = shutil.which(name)
        if resolved:
            return Path(resolved).resolve()
    return None


def get_python_invocation() -> PythonInvocation:
    """Keep subprocesses in the selected environment, including Unix venv links."""

    return PythonInvocation(executable=Path(sys.executable))


def _validate_cargo_target_dir(
    command: Sequence[str | os.PathLike[str]],
    env: dict[str, str],
) -> None:
    """Fails fast when a Cargo command lacks the caller-provided target dir."""

    if not command:
        return
    program = os.path.basename(str(command[0])).lower()
    if program not in ("cargo", "cargo.exe"):
        return
    output_root_value = env.get(WORKSPACE_OUTPUT_ROOT_ENV, "").strip()
    if not output_root_value:
        raise RuntimeError(f"{WORKSPACE_OUTPUT_ROOT_ENV} is required for cargo commands.")
    target_value = env.get(CARGO_TARGET_DIR_ENV, "").strip()
    if not target_value:
        raise RuntimeError(f"{CARGO_TARGET_DIR_ENV} is required for cargo commands.")
    output_root = Path(output_root_value).expanduser().resolve()
    target_dir = Path(target_value).expanduser().resolve()
    if not output_root.is_dir():
        raise RuntimeError(f"{WORKSPACE_OUTPUT_ROOT_ENV} must point to an existing directory: {output_root}")
    if not target_dir.is_dir():
        raise RuntimeError(f"{CARGO_TARGET_DIR_ENV} must point to an existing directory: {target_dir}")
    expected = (output_root / "builds" / "rust" / "target").resolve()
    if _normcase_path(target_dir) != _normcase_path(expected):
        raise RuntimeError(f"{CARGO_TARGET_DIR_ENV} must be {expected}, got {target_dir}.")


def _normcase_path(path: Path) -> str:
    return str(path.resolve()).casefold().rstrip("\\/")


def run_native(
    command: Sequence[str | os.PathLike[str]],
    *,
    label: str,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Runs a native command and raises with a concise label on failure."""

    merged_env = os.environ.copy()
    if env:
        merged_env.update({key: str(value) for key, value in env.items()})
    _validate_cargo_target_dir(command, merged_env)

    completed = subprocess.run(
        [str(part) for part in command],
        cwd=str(cwd),
        env=merged_env,
        text=True,
        check=False,
    )
    if completed.returncode != 0 and not allow_failure:
        raise RuntimeError(f"{label} failed with exit code {completed.returncode}.")
    return completed


def run_captured(
    command: Sequence[str | os.PathLike[str]],
    *,
    label: str,
    cwd: Path,
) -> str:
    """Runs a command and returns stdout, raising with stderr on failure."""

    merged_env = os.environ.copy()
    _validate_cargo_target_dir(command, merged_env)

    completed = subprocess.run(
        [str(part) for part in command],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=merged_env,
        check=False,
    )
    if completed.returncode != 0:
        tail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"{label} failed with exit code {completed.returncode}.\n{tail}")
    return completed.stdout


def current_python_module_command(module_name: str, args: Sequence[str]) -> list[str]:
    """Returns a command that invokes a module with the current Python runtime."""

    return [sys.executable, "-m", module_name, *args]
