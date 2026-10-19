"""Setup command helpers built on the Python workspace topology."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import resolve_required_workspace_roots
from .git import repo_branch, repo_head
from .materialize import resolve_setup_workspace_root
from .process import find_tool, run_captured
from .topology import HISTORICAL_ANALYSIS_PROFILE, ManagedRepo, UpdatePolicy, WorkspaceTopology, canonical_topology


@dataclass(frozen=True)
class CompareTarget:
    """One directory that can participate in a WinMerge comparison."""

    name: str
    path: Path


@dataclass(frozen=True)
class ComparePreset:
    """One named comparison preset."""

    key: str
    label: str
    category: str
    left_name: str
    right_name: str


def write_materialization_status(*, workspace_root: str | None = None) -> None:
    """Prints setup-managed repository status."""

    root = resolve_setup_workspace_root(workspace_root)
    topology = canonical_topology()
    for repo in topology.all_repos():
        repo_path = root / repo.relative_path
        if not repo_path.is_dir():
            print(f"[missing] {repo.name} -> {repo_path}")
            continue
        status = run_captured(["git", "-C", repo_path, "status", "--short"], label=f"status {repo.name}", cwd=root)
        print(f"[{repo.name}] {repo_branch(repo_path)} @ {repo_head(repo_path)} dirty={bool(status.strip())}")


def write_dependency_update_report(*, workspace_root: str | None = None, workspace_name: str | None = None) -> None:
    """Writes advisory dependency update artifacts for setup-managed third-party repos."""

    root = resolve_setup_workspace_root(workspace_root)
    _workspace_root, output_root_value = resolve_required_workspace_roots()
    topology = canonical_topology()
    resolved_workspace_name = workspace_name or topology.default_workspace_name
    workspace_path = root / "workspaces" / resolved_workspace_name
    if not workspace_path.is_dir():
        raise RuntimeError(f"Workspace root is missing: {workspace_path}. Run materialize or sync first.")
    entries = [_dependency_update_entry(repo) for repo in topology.third_party_repos]
    output_root = output_root_value / "reports" / "dep-updates"
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report_path = output_root / f"{stamp}-dep-updates.json"
    summary_path = output_root / "latest-summary.json"
    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
    }
    for path in (report_path, summary_path):
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    print("Dependency update report")
    for status, count in sorted(counts.items()):
        print(f"{status}: {count}")
    print(f"summary: {summary_path}")
    if counts.get("error", 0):
        raise RuntimeError(f"Dependency update report completed with errors. See {summary_path}.")


def run_compare(*, preset_key: str | None = None, workspace_root: str | None = None) -> None:
    """Shows compare presets or launches WinMerge for a selected preset."""

    root = resolve_setup_workspace_root(workspace_root)
    topology = canonical_topology(profiles=(HISTORICAL_ANALYSIS_PROFILE,))
    presets = compare_presets(root, topology)
    if not preset_key:
        print("WinMerge compare presets")
        for preset in presets:
            print(f"{preset.key}: [{preset.category}] {preset.label}")
        return
    if preset_key == "mods-archive":
        for preset in presets:
            if preset.category == "Mods Archive":
                print(f"{preset.key}: {preset.label}")
        return
    selected = next((preset for preset in presets if preset.key == preset_key), None)
    if selected is None:
        raise RuntimeError(f"Unknown compare preset: {preset_key}")
    invoke_compare_preset(root, topology, selected)


def compare_presets(root: Path, topology: WorkspaceTopology) -> tuple[ComparePreset, ...]:
    """Returns all configured comparison presets."""

    analysis_specs = {
        "community-0.60": ("Community 0.60", "Community 0.60 vs local"),
        "community-0.72": ("Community 0.72", "Community 0.72 vs local"),
        "mods-archive": ("Mods archive", "Mods Archive"),
        "stale-v0.72a-experimental-clean": ("Stale experimental clean", "Stale experimental reference"),
        "emuleai": ("eMuleAI", "eMuleAI vs local"),
    }
    presets: list[ComparePreset] = []
    for right in (target.name for target in local_variant_compare_targets(root, topology)):
        for repo in topology.analysis_repos:
            if repo.name not in analysis_specs:
                continue
            label, category = analysis_specs[repo.name]
            key_name = repo.name.replace("community-0.60", "community-060").replace(
                "community-0.72", "community-072"
            ).replace("stale-v0.72a-experimental-clean", "stale-experimental-clean")
            presets.append(ComparePreset(f"{key_name}-vs-{right}", f"{label} vs {right}", category, repo.name, right))
    return tuple(presets)


def local_variant_compare_targets(root: Path, topology: WorkspaceTopology) -> tuple[CompareTarget, ...]:
    """Returns srchybrid roots for active local app variants."""

    return tuple(
        CompareTarget(f"local-072-{worktree.name}", root / worktree.relative_path / "srchybrid")
        for worktree in topology.app_repo.worktrees
        if worktree.active
    )


def invoke_compare_preset(root: Path, topology: WorkspaceTopology, preset: ComparePreset) -> None:
    """Starts WinMerge for one comparison preset."""

    winmerge = winmerge_path()
    left = compare_root(root, topology, preset.left_name)
    right = compare_root(root, topology, preset.right_name)
    for name, path in ((preset.left_name, left), (preset.right_name, right)):
        if not path.exists():
            raise RuntimeError(f"Compare target path missing for {name}: {path}")
    subprocess.Popen([str(winmerge), str(left), str(right)])


def compare_root(root: Path, topology: WorkspaceTopology, name: str) -> Path:
    """Resolves one comparison target name."""

    local_targets = {target.name: target for target in local_variant_compare_targets(root, topology)}
    if name in local_targets:
        return local_targets[name].path
    compare_repo = next((repo for repo in (*topology.repos, *topology.analysis_repos) if repo.name == name), None)
    if compare_repo is None:
        raise RuntimeError(f"Unknown compare target: {name}")
    if compare_repo.compare_subdir:
        path = root / compare_repo.relative_path / str(compare_repo.compare_subdir)
    else:
        path = root / compare_repo.relative_path
    return path


def winmerge_path() -> Path:
    """Resolves WinMergeU.exe from standard install locations or PATH."""

    for candidate in (
        Path("C:/Program Files/WinMerge/WinMergeU.exe"),
        Path("C:/Program Files (x86)/WinMerge/WinMergeU.exe"),
    ):
        if candidate.is_file():
            return candidate
    resolved = find_tool(("WinMergeU.exe", "WinMergeU"))
    if resolved is None:
        raise RuntimeError("WinMergeU.exe was not found. Install WinMerge or add it to PATH.")
    return resolved


def _dependency_update_entry(repo: ManagedRepo) -> dict[str, object]:
    policy = repo.update_policy
    if policy is None:
        return {"name": repo.name, "status": "skipped", "reason": "no update policy"}
    if policy.tracking_mode == "none":
        return {"name": repo.name, "status": "skipped", "baseline_ref": policy.baseline_ref, "notes": policy.notes}
    try:
        entry = _evaluate_policy(repo.name, policy)
        if policy.child_components:
            entry["children"] = [_evaluate_policy(child.name, child) for child in policy.child_components]
        return entry
    except Exception as exc:
        return {"name": repo.name, "status": "error", "error": str(exc)}


def _evaluate_policy(name: str, policy: UpdatePolicy) -> dict[str, object]:
    if not policy.upstream_url:
        return {"name": name, "status": "skipped", "reason": "missing upstream URL"}
    if policy.tracking_mode == "tag":
        latest = _latest_matching_tag(policy.upstream_url, policy.version_pattern)
        return {
            "name": name,
            "status": "current" if latest == policy.baseline_ref else "update-available",
            "tracking_mode": policy.tracking_mode,
            "baseline_ref": policy.baseline_ref,
            "latest_ref": latest,
        }
    if policy.tracking_mode == "branch-head":
        ref = policy.upstream_ref or "master"
        head = _remote_branch_head(policy.upstream_url, ref)
        return {
            "name": name,
            "status": "current" if head == policy.baseline_ref else "update-available",
            "tracking_mode": policy.tracking_mode,
            "baseline_ref": policy.baseline_ref,
            "upstream_ref": ref,
            "latest_ref": head,
        }
    return {"name": name, "status": "skipped", "tracking_mode": policy.tracking_mode}


def _latest_matching_tag(url: str, version_pattern: str | None) -> str | None:
    output = run_captured(["git", "ls-remote", "--tags", url], label=f"list tags {url}", cwd=Path.cwd())
    pattern = re.compile(version_pattern or r".+")
    candidates: list[tuple[tuple[int, ...], str]] = []
    for line in output.splitlines():
        if not line.strip() or line.endswith("^{}"):
            continue
        ref = line.split()[-1].removeprefix("refs/tags/")
        match = pattern.match(ref)
        if match:
            version = tuple(int(part) for part in match.groups() if part.isdigit())
            candidates.append((version, ref))
    if not candidates:
        return None
    return sorted(candidates)[-1][1]


def _remote_branch_head(url: str, ref: str) -> str | None:
    output = run_captured(["git", "ls-remote", url, f"refs/heads/{ref}"], label=f"read branch {url}/{ref}", cwd=Path.cwd())
    first = next((line for line in output.splitlines() if line.strip()), "")
    return first.split()[0] if first else None
