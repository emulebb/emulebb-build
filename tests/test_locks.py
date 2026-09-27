from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from emule_workspace.config import WorkspaceOptions
from emule_workspace.locks import WORKSPACE_LOCK_TOKEN_ENV, WorkspaceLock


def make_lock(tmp_path: Path, command: str = "validate") -> WorkspaceLock:
    """Builds a lock with the filesystem fields used by lock metadata."""

    root = (tmp_path / "WorkspaceRoot").resolve()
    layout = SimpleNamespace(
        emule_workspace_root=root,
        workspace_root=root / "workspaces" / "workspace",
        output_tmp_root=(tmp_path / "output" / "tmp").resolve(),
    )
    options = WorkspaceOptions(workspace_root=root)
    return WorkspaceLock(layout=layout, command=command, options=options)


def test_mutex_name_uses_workspace_prefix(tmp_path: Path) -> None:
    layout = SimpleNamespace(
        emule_workspace_root=(tmp_path / "WorkspaceRoot").resolve(),
        workspace_root=tmp_path / "WorkspaceRoot" / "workspaces" / "workspace",
    )
    options = WorkspaceOptions(workspace_root=layout.emule_workspace_root)

    lock = WorkspaceLock(layout=layout, command="validate", options=options)
    digest = lock._mutex_name().removeprefix("Global\\eMuleBuild-")

    assert lock._mutex_name().startswith("Global\\eMuleBuild-")
    assert digest == digest.upper()
    assert len(digest) == 64


def test_acquire_records_delegation_token(tmp_path: Path, monkeypatch) -> None:
    lock = make_lock(tmp_path)
    monkeypatch.delenv(WORKSPACE_LOCK_TOKEN_ENV, raising=False)
    monkeypatch.setattr(lock, "_acquire_named_mutex", lambda: True)
    monkeypatch.setattr(lock, "_release_named_mutex", lambda: None)

    assert lock.acquire() is True
    metadata = json.loads(lock.metadata_path.read_text(encoding="utf-8"))

    assert lock.delegation_token
    assert metadata["delegation_token"] == lock.delegation_token
    lock.release()
    assert not lock.metadata_path.exists()


def test_delegated_child_reenters_without_releasing_parent_metadata(tmp_path: Path, monkeypatch) -> None:
    parent = make_lock(tmp_path, "test release-campaign")
    monkeypatch.delenv(WORKSPACE_LOCK_TOKEN_ENV, raising=False)
    monkeypatch.setattr(parent, "_acquire_named_mutex", lambda: True)
    monkeypatch.setattr(parent, "_release_named_mutex", lambda: None)
    assert parent.acquire() is True
    assert parent.delegation_token

    child = make_lock(tmp_path, "test rust-unit")
    monkeypatch.setenv(WORKSPACE_LOCK_TOKEN_ENV, parent.delegation_token)
    monkeypatch.setattr(child, "_acquire_named_mutex", lambda: (_ for _ in ()).throw(AssertionError("mutex used")))

    assert child.acquire() is True
    assert child.delegation_token == parent.delegation_token
    child.release()
    assert parent.metadata_path.is_file()

    parent.release()
    assert not parent.metadata_path.exists()


def test_wrong_delegation_token_does_not_bypass_active_owner(tmp_path: Path, monkeypatch) -> None:
    parent = make_lock(tmp_path, "test release-campaign")
    monkeypatch.delenv(WORKSPACE_LOCK_TOKEN_ENV, raising=False)
    monkeypatch.setattr(parent, "_acquire_named_mutex", lambda: True)
    monkeypatch.setattr(parent, "_release_named_mutex", lambda: None)
    assert parent.acquire() is True

    child = make_lock(tmp_path, "test rust-unit")
    monkeypatch.setenv(WORKSPACE_LOCK_TOKEN_ENV, "not-the-parent-token")
    monkeypatch.setattr(child, "_acquire_named_mutex", lambda: False)

    assert child.acquire() is False
    assert child.delegation_token is None
    parent.release()
