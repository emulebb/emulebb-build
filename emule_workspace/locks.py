"""Single-owner workspace lock used by orchestration commands."""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from .config import WorkspaceOptions
from .layout import WorkspaceLayout

WORKSPACE_LOCK_TOKEN_ENV = "EMULEBB_WORKSPACE_LOCK_TOKEN"


@dataclass
class WorkspaceLock:
    """Legacy-compatible single-owner workspace command lock."""

    layout: WorkspaceLayout
    command: str
    options: WorkspaceOptions
    acquired: bool = False
    _mutex_handle: int | None = None
    _delegation_token: str | None = None
    _reentrant: bool = False

    @property
    def metadata_path(self) -> Path:
        """Returns the active lock metadata path."""

        return self.layout.output_tmp_root / "active-command-lock.json"

    def acquire(self) -> bool:
        """Attempts to acquire the workspace lock without waiting."""

        inherited_token = os.environ.get(WORKSPACE_LOCK_TOKEN_ENV, "")
        if inherited_token and self._can_reenter(inherited_token):
            self.acquired = True
            self._delegation_token = inherited_token
            self._reentrant = True
            return True

        if not self._acquire_named_mutex():
            return False

        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        delegation_token = secrets.token_urlsafe(32)
        metadata = {
            "command": self.command,
            "pid": os.getpid(),
            "machine_name": socket.gethostname(),
            "started_utc": datetime.now(timezone.utc).isoformat(),
            "workspace_root": str(self.layout.emule_workspace_root),
            "workspace_name": self.options.workspace_name,
            "config": self.options.configuration,
            "platform": self.options.platform,
            "delegation_token": delegation_token,
        }
        try:
            handle = os.open(self.metadata_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not self._metadata_is_stale():
                self._release_named_mutex()
                return False
            self.metadata_path.unlink(missing_ok=True)
            handle = os.open(self.metadata_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except Exception:
            self._release_named_mutex()
            raise
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(metadata, stream, indent=2)
            stream.write("\n")
        self.acquired = True
        self._delegation_token = delegation_token
        return True

    def release(self) -> None:
        """Releases this lock if it is currently held by this process."""

        if not self.acquired:
            return
        if self._reentrant:
            self.acquired = False
            self._delegation_token = None
            self._reentrant = False
            return
        try:
            metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            metadata = {}
        if int(metadata.get("pid", -1)) == os.getpid():
            self.metadata_path.unlink(missing_ok=True)
        self.acquired = False
        self._delegation_token = None
        self._release_named_mutex()

    @property
    def delegation_token(self) -> str | None:
        """Returns the token inherited by orchestrated child commands."""

        return self._delegation_token

    def active_owner_text(self) -> str:
        """Returns a human-readable description of the current lock owner."""

        try:
            metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return "another eMule workspace command"
        return (
            f"'{metadata.get('command')}' "
            f"(PID {metadata.get('pid')} on {metadata.get('machine_name')}, "
            f"started {metadata.get('started_utc')})"
        )

    def _mutex_name(self) -> str:
        """Returns the Windows named mutex for this workspace root."""

        normalized_root = str(self.layout.emule_workspace_root).rstrip("\\").lower()
        digest = sha256(normalized_root.encode("utf-8")).hexdigest().upper()
        return f"Global\\eMuleBuild-{digest}"

    def _acquire_named_mutex(self) -> bool:
        """Acquires the workspace Windows named mutex when running on Windows."""

        if sys.platform != "win32":
            return True

        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        create_mutex.restype = ctypes.c_void_p
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        wait_for_single_object.restype = ctypes.c_uint32

        handle = create_mutex(None, False, self._mutex_name())
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())

        wait_result = wait_for_single_object(handle, 0)
        if wait_result not in (0x00000000, 0x00000080):
            kernel32.CloseHandle(handle)
            return False
        self._mutex_handle = int(handle)
        return True

    def _release_named_mutex(self) -> None:
        """Releases the Windows named mutex when this process owns it."""

        if self._mutex_handle is None or sys.platform != "win32":
            self._mutex_handle = None
            return

        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = ctypes.c_void_p(self._mutex_handle)
        kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)
        self._mutex_handle = None

    def _metadata_is_stale(self) -> bool:
        """Returns whether lock metadata belongs to a missing local process."""

        try:
            metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return True

        pid = int(metadata.get("pid", -1))
        machine_name = str(metadata.get("machine_name") or "")
        if pid <= 0 or machine_name.lower() != socket.gethostname().lower():
            return False
        if sys.platform == "win32":
            return not _windows_process_exists(pid)
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        return False

    def _can_reenter(self, inherited_token: str) -> bool:
        """Returns whether this command is a delegated child of the active owner."""

        try:
            metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return False
        return (
            secrets.compare_digest(str(metadata.get("delegation_token") or ""), inherited_token)
            and str(metadata.get("workspace_root") or "").lower()
            == str(self.layout.emule_workspace_root).lower()
            and not self._metadata_is_stale()
        )


def _windows_process_exists(pid: int) -> bool:
    """Checks whether a Windows process id is still present."""

    import subprocess

    completed = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return str(pid) in completed.stdout
