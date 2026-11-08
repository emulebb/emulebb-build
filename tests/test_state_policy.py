from __future__ import annotations

from pathlib import Path


ALLOWED_WORKSPACE_STATE_PATHS = {
    # The audit rejects VHDs under the retired state root; it never writes there.
    (
        "emule_workspace/windows_vm_lab.py",
        'workspace_state_root = (layout.workspace_root / "state").resolve()',
    ),
    (
        "emule_workspace/cleanup.py",
        'root = layout.workspace_root / "state" / LEGACY_LIVE_E2E_ARTIFACTS_DIR_NAME',
    ),
    (
        "emule_workspace/cleanup.py",
        'root = layout.emule_workspace_root / "state"',
    ),
    (
        "emule_workspace/cleanup.py",
        'nested_state = layout.build_repo_root / "workspaces" / getattr(layout, "workspace_name", "workspace") / "state"',
    ),
    (
        "emule_workspace/materialize.py",
        'legacy_status_path = root / "workspaces" / resolved_workspace_name / "state" / "EMULE-STATUS.md"',
    ),
}


def test_production_workspace_state_paths_are_explicitly_legacy() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    hits: set[tuple[str, str]] = set()

    for path in (repo_root / "emule_workspace").rglob("*.py"):
        relative = path.relative_to(repo_root).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if ' / "state"' in stripped:
                hits.add((relative, stripped))

    assert hits == ALLOWED_WORKSPACE_STATE_PATHS
