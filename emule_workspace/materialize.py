"""Python-native workspace materialization and sync commands."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

from .config import resolve_required_workspace_roots
from .layout import build_repo_root
from .process import find_tool, run_captured, run_native
from .topology import (
    AppRepo,
    SETUP_LOG_FILE_NAME,
    SHARED_HOOK_REPO_NAMES,
    WORKSPACE_MANIFEST_NAME,
    WORKSPACE_PROPS_FILE_NAME,
    WorkspaceTopology,
    REPO_ROLE_MANIFEST_NAME,
    build_workspace_manifest,
    build_repo_role_manifest,
    canonical_topology,
    write_json,
)
from .topology import ManagedRepo

ROOT_AGENTS_CONTENT = """Read `EMULEBB_WORKSPACE_ROOT\\repos\\emulebb-tooling\\docs\\WORKSPACE-POLICY.md`
before making workspace decisions.

Use `repos` and `workspaces` as the workspace boundaries. For the repeatable
agent path, start from
`EMULEBB_WORKSPACE_ROOT\\repos\\emulebb-tooling\\docs\\reference\\AGENT-CHECKLIST.md`.

Infer the active project from the user's wording and stay scoped to it. If the
user says `emulebb rust`, focus on `repos\\emulebb-rust`, its Rust-native UI, and
only the support harness/docs needed for that work. If the user says
`emulebb mfc`, focus on `workspaces\\workspace\\app\\emulebb-main` and required
MFC support repos. If the user says `qbittorrentbb`, focus on
`repos\\qbittorrentbb` and required support only. Do not sweep the whole
workspace unless the user asks for workspace-wide work or the focused task
actually needs cross-repo context.

Environment variables are inherited operator state. Do not set or repair
`EMULEBB_WORKSPACE_ROOT`, `EMULEBB_WORKSPACE_OUTPUT_ROOT`, `CARGO_TARGET_DIR`, or
`X_LOCAL_IP` inline. If one is missing or wrong, stop and report the preflight
failure.

A persisted Python launcher may propagate already-valid Windows operator state
across the Windows-to-WSL process boundary. It may translate Windows paths for
the WSL child and derive a platform-specific Cargo target below the translated
output root, but it must not mutate the Windows environment, persist duplicate
WSL configuration, or guess missing source values. Record the translation in
run evidence. WSL lanes that keep control traffic on loopback do not require
`X_LOCAL_IP`.

For Rust live soak/profile work, use the policy quickstart and persisted Python
scripts in `repos\\emulebb-build-tests`; do not use `.ps1`, inline Python, or
ad-hoc launch logic.
"""


def derive_workspace_root_from_build_repo() -> Path:
    """Returns EMULEBB_WORKSPACE_ROOT from the required repos/emulebb-build clone layout."""

    repo_root = build_repo_root().resolve()
    if repo_root.name != "emulebb-build" or repo_root.parent.name != "repos":
        raise RuntimeError(
            "emulebb-build must be cloned as <EMULEBB_WORKSPACE_ROOT>\\repos\\emulebb-build "
            f"for materialization. Current path: {repo_root}"
        )
    return repo_root.parent.parent.resolve()


def resolve_setup_workspace_root(workspace_root: str | None) -> Path:
    """Resolves setup commands to an explicit or layout-derived workspace root."""

    if workspace_root:
        root = Path(workspace_root).expanduser().resolve()
        _assert_build_repo_matches_root(root)
        return root
    return derive_workspace_root_from_build_repo()


def _assert_build_repo_matches_root(root: Path) -> None:
    repo_root = build_repo_root().resolve()
    expected = (root / "repos" / "emulebb-build").resolve()
    if repo_root != expected:
        raise RuntimeError(
            "emulebb-build must be the clone at <EMULEBB_WORKSPACE_ROOT>\\repos\\emulebb-build. "
            f"Expected {expected}, current package is {repo_root}."
        )


def materialize_workspace(
    *,
    workspace_root: str | None = None,
    workspace_name: str | None = None,
    artifacts_seed_root: str | None = None,
    include_analysis: bool = False,
) -> None:
    """Materializes a new workspace around the required repos/emulebb-build clone."""

    root = resolve_setup_workspace_root(workspace_root)
    topology = canonical_topology(include_analysis=include_analysis)
    resolved_workspace_name = workspace_name or topology.default_workspace_name
    assert_materialize_bootstrap_root(root)
    sync_workspace(
        workspace_root=str(root),
        workspace_name=resolved_workspace_name,
        artifacts_seed_root=artifacts_seed_root,
        include_worktrees=True,
        persist_environment=True,
        include_analysis=include_analysis,
    )
    legacy_status_path = root / "workspaces" / resolved_workspace_name / "state" / "EMULE-STATUS.md"
    legacy_status_path.unlink(missing_ok=True)
    log_line(root, "Materialize complete.")


def sync_workspace(
    *,
    workspace_root: str | None = None,
    workspace_name: str | None = None,
    artifacts_seed_root: str | None = None,
    include_worktrees: bool = True,
    persist_environment: bool = False,
    include_analysis: bool = False,
) -> None:
    """Synchronizes setup-owned workspace state."""

    root = resolve_setup_workspace_root(workspace_root)
    topology = canonical_topology(include_analysis=include_analysis)
    resolved_workspace_name = workspace_name or topology.default_workspace_name
    ensure_required_tools()
    ensure_root_layout(root, topology, resolved_workspace_name)
    for repo in topology.all_repos():
        repo_path = ensure_repo_clone(root, repo)
        log_line(root, f"Repo ready: {repo.name} [{repo_path}]")
    overlay_seed_artifacts(root, topology, artifacts_seed_root, resolved_workspace_name)
    write_workspace_props(root)
    write_workspace_manifest(root, topology, resolved_workspace_name)
    write_repo_role_manifest(root, topology, resolved_workspace_name)
    if include_worktrees:
        ensure_app_worktrees(root, topology)
        remove_legacy_app_dependency_links(root, topology)
    if include_analysis:
        write_compare_launchers(root)
    install_workspace_hooks(root, topology)
    if persist_environment:
        set_workspace_root_environment(root)
    log_line(root, "Sync complete.")


def assert_materialize_bootstrap_root(root: Path) -> None:
    """Allows an empty root or one containing only the required build clone."""

    if not root.exists():
        return
    build_repo = build_repo_root().resolve()
    repos_root = build_repo.parent
    unexpected: list[Path] = []
    for path in root.iterdir():
        if path.resolve() == repos_root:
            continue
        unexpected.append(path)
        if len(unexpected) >= 5:
            break
    if not unexpected and repos_root.exists():
        for path in repos_root.iterdir():
            if path.resolve() == build_repo:
                continue
            unexpected.append(path)
            if len(unexpected) >= 5:
                break
    if unexpected:
        details = "\n".join(str(path) for path in unexpected)
        raise RuntimeError(
            f"Materialize expects an empty workspace root containing only repos\\emulebb-build. "
            f"Refusing populated root '{root}'. Use sync for an existing workspace.\n{details}"
        )


def assert_clean_repo(repo_path: Path, repo_name: str, root: Path) -> None:
    """Fails before managed sync would overwrite local repository work."""

    status_output = run_captured(["git", "-C", repo_path, "status", "--short"], label=f"status {repo_name}", cwd=root)
    if status_output.strip():
        raise RuntimeError(
            f"Managed repository '{repo_name}' has local changes and cannot be fast-forwarded safely: {repo_path}"
        )


def branch_ref_exists(repo_path: Path, ref: str, root: Path, repo_name: str) -> bool:
    """Returns whether a Git ref exists in a managed repository."""

    result = run_native(
        ["git", "-C", repo_path, "show-ref", "--verify", "--quiet", ref],
        label=f"check {repo_name} ref {ref}",
        cwd=root,
        allow_failure=True,
    )
    return result.returncode == 0


def print_repo_provenance(root: Path, repo_path: Path, repo_name: str) -> None:
    """Prints the checked-out commit for CI materialization logs."""

    head = run_captured(["git", "-C", repo_path, "rev-parse", "HEAD"], label=f"provenance {repo_name}", cwd=root)
    branch = run_captured(
        ["git", "-C", repo_path, "branch", "--show-current"],
        label=f"provenance branch {repo_name}",
        cwd=root,
    )
    print(f"Managed repository '{repo_name}' at {head.strip()} ({branch.strip() or 'detached'}).")


def ensure_managed_repo_branch(root: Path, repo_path: Path, repo: ManagedRepo) -> None:
    """Checks out and fast-forwards a non-app managed repository branch."""

    local_ref = f"refs/heads/{repo.branch}"
    remote_ref = f"refs/remotes/origin/{repo.branch}"
    local_exists = branch_ref_exists(repo_path, local_ref, root, repo.name)
    remote_exists = branch_ref_exists(repo_path, remote_ref, root, repo.name)
    if not local_exists and not remote_exists and repo.branch_optional:
        print(f"WARNING: Optional branch '{repo.branch}' is unavailable for '{repo.name}'; leaving checkout unchanged.")
        return
    if not local_exists and not remote_exists:
        raise RuntimeError(f"Managed repository '{repo.name}' is missing required branch '{repo.branch}'.")
    assert_clean_repo(repo_path, repo.name, root)
    run_native(["git", "-C", repo_path, "checkout", repo.branch], label=f"checkout {repo.name}", cwd=root)
    if remote_exists:
        run_native(
            ["git", "-C", repo_path, "merge", "--ff-only", remote_ref],
            label=f"fast-forward {repo.name}",
            cwd=root,
        )


def ensure_required_tools() -> None:
    """Checks command-line tools required by materialization."""

    for name in ("git",):
        if find_tool((f"{name}.exe", name)) is None:
            raise RuntimeError(f"Required tool '{name}' is not available on PATH.")


def ensure_root_layout(root: Path, topology: WorkspaceTopology, workspace_name: str) -> None:
    """Creates setup-owned root directories and root AGENTS.md."""

    root.mkdir(parents=True, exist_ok=True)
    for relative_path in topology.root_directories:
        (root / relative_path).mkdir(parents=True, exist_ok=True)
    (root / "AGENTS.md").write_text(ROOT_AGENTS_CONTENT, encoding="ascii", newline="\n")
    workspace_root = root / "workspaces" / workspace_name
    for relative_path in ("app", "artifacts", "logs", "scripts", "state"):
        (workspace_root / relative_path).mkdir(parents=True, exist_ok=True)


def ensure_repo_clone(root: Path, repo: ManagedRepo) -> Path:
    """Clones or fast-forwards one managed repository."""

    repo_path = (root / repo.relative_path).resolve()
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    if not repo_path.exists():
        clone_args = ["git", "clone"]
        if repo.has_submodules:
            clone_args.append("--recurse-submodules")
        if not repo.branch_optional:
            clone_args.extend(["--branch", repo.branch])
        clone_args.extend([repo.url, str(repo_path)])
        run_native(clone_args, label=f"clone {repo.name}", cwd=root)
        ensure_repo_additional_remotes(repo_path, repo)
        print_repo_provenance(root, repo_path, repo.name)
        return repo_path

    run_native(["git", "-C", repo_path, "fetch", "origin", "--prune"], label=f"fetch {repo.name}", cwd=root)
    ensure_repo_additional_remotes(repo_path, repo)
    if not isinstance(repo, AppRepo):
        ensure_managed_repo_branch(root, repo_path, repo)
        print_repo_provenance(root, repo_path, repo.name)
    if repo.has_submodules:
        run_native(
            ["git", "-C", repo_path, "submodule", "update", "--init", "--recursive"],
            label=f"submodules {repo.name}",
            cwd=root,
        )
    return repo_path


def ensure_repo_additional_remotes(repo_path: Path, repo: ManagedRepo) -> None:
    """Adds or updates configured secondary remotes."""

    existing = run_captured(["git", "-C", repo_path, "remote"], label=f"list remotes {repo.name}", cwd=repo_path)
    existing_names = {line.strip() for line in existing.splitlines() if line.strip()}
    for remote in repo.additional_remotes:
        if remote.name in existing_names:
            run_native(
                ["git", "-C", repo_path, "remote", "set-url", remote.name, remote.url],
                label=f"set remote {repo.name}/{remote.name}",
                cwd=repo_path,
            )
        else:
            run_native(
                ["git", "-C", repo_path, "remote", "add", remote.name, remote.url],
                label=f"add remote {repo.name}/{remote.name}",
                cwd=repo_path,
            )


def ensure_app_worktrees(root: Path, topology: WorkspaceTopology) -> None:
    """Ensures all active app worktrees exist and match their managed branches."""

    repo_path = (root / topology.app_repo.relative_path).resolve()
    run_native(["git", "-C", repo_path, "fetch", "origin", "--prune"], label="fetch app repo", cwd=root)
    for worktree in topology.app_repo.worktrees:
        if not worktree.active:
            continue
        branch_exists = run_native(
            ["git", "-C", repo_path, "show-ref", "--verify", "--quiet", f"refs/heads/{worktree.branch}"],
            label=f"check app branch {worktree.branch}",
            cwd=root,
            allow_failure=True,
        )
        if branch_exists.returncode != 0:
            run_native(
                [
                    "git",
                    "-C",
                    repo_path,
                    "fetch",
                    "origin",
                    f"refs/heads/{worktree.branch}:refs/remotes/origin/{worktree.branch}",
                ],
                label=f"fetch app branch {worktree.branch}",
                cwd=root,
            )
            run_native(
                ["git", "-C", repo_path, "branch", "--track", worktree.branch, f"origin/{worktree.branch}"],
                label=f"track app branch {worktree.branch}",
                cwd=root,
            )
    current_branch = run_captured(
        ["git", "-C", repo_path, "branch", "--show-current"],
        label="current app anchor branch",
        cwd=root,
    ).strip()
    if current_branch == topology.app_repo.branch:
        run_native(
            ["git", "-C", repo_path, "checkout", "--detach", f"refs/remotes/origin/{topology.app_repo.branch}"],
            label="detach app anchor",
            cwd=root,
        )
    for worktree in topology.app_repo.worktrees:
        if worktree.active:
            ensure_app_worktree(root, repo_path, worktree.relative_path, worktree.branch)


def ensure_app_worktree(root: Path, repo_path: Path, relative_path: str, branch: str) -> None:
    """Ensures one managed app worktree exists and can fast-forward when clean."""

    target_path = (root / relative_path).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if not target_path.exists():
        run_native(
            ["git", "-C", repo_path, "worktree", "add", str(target_path), branch],
            label=f"add app worktree {branch}",
            cwd=root,
        )
        return
    run_native(["git", "-C", target_path, "fetch", "origin", "--prune"], label=f"fetch worktree {branch}", cwd=root)
    run_native(["git", "-C", target_path, "checkout", branch], label=f"checkout worktree {branch}", cwd=root)
    status = run_captured(["git", "-C", target_path, "status", "--short"], label=f"status worktree {branch}", cwd=root)
    if status.strip():
        print(f"WARNING: Worktree '{target_path}' is dirty; skipping fast-forward for managed branch '{branch}'.")
        return
    remote_ref = f"refs/remotes/origin/{branch}"
    remote_exists = run_native(
        ["git", "-C", target_path, "show-ref", "--verify", "--quiet", remote_ref],
        label=f"check remote {branch}",
        cwd=root,
        allow_failure=True,
    )
    if remote_exists.returncode == 0:
        run_native(["git", "-C", target_path, "merge", "--ff-only", remote_ref], label=f"fast-forward {branch}", cwd=root)


def remove_legacy_app_dependency_links(root: Path, topology: WorkspaceTopology) -> None:
    """Removes old dependency reparse links from managed app worktrees."""

    for worktree in topology.app_repo.worktrees:
        if not worktree.active:
            continue
        worktree_root = root / worktree.relative_path
        for name in ("cryptopp", "id3lib", "mbedtls", "miniupnpc", "ResizableLib", "zlib"):
            candidate = worktree_root / name
            if candidate.exists() and is_reparse_link(candidate):
                if candidate.is_dir():
                    candidate.rmdir()
                else:
                    candidate.unlink()


def overlay_seed_artifacts(root: Path, topology: WorkspaceTopology, seed_root: str | None, workspace_name: str) -> None:
    """Copies optional third-party artifact seeds into managed dependency repos."""

    if not seed_root:
        return
    resolved_seed_root = Path(seed_root).expanduser().resolve()
    if not resolved_seed_root.is_dir():
        raise RuntimeError(f"Artifacts seed root '{resolved_seed_root}' does not exist.")
    _workspace_root, output_root = resolve_required_workspace_roots()
    overlay_state_path = output_root / "artifacts" / "materialize" / workspace_name / "artifact-seed-overlays.json"
    previous = load_seed_overlay_state(overlay_state_path)
    current: dict[str, list[str]] = {}
    for repo in topology.third_party_repos:
        source = resolved_seed_root / repo.name
        if not source.is_dir():
            continue
        destination = root / repo.relative_path
        copied = overlay_seed_directory(source, destination, previous.get(repo.name, ()))
        current[repo.name] = sorted(copied)
    write_seed_overlay_state(overlay_state_path, current)


def load_seed_overlay_state(path: Path) -> dict[str, tuple[str, ...]]:
    """Loads the previous artifact-seed overlay file list."""

    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Artifact seed overlay state is not an object: {path}")
    result: dict[str, tuple[str, ...]] = {}
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, list) and all(isinstance(item, str) for item in value):
            result[key] = tuple(value)
    return result


def write_seed_overlay_state(path: Path, payload: dict[str, list[str]]) -> None:
    """Writes tracked artifact-seed overlay state outside dependency repos."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def overlay_seed_directory(source: Path, destination: Path, previous_files: tuple[str, ...]) -> tuple[str, ...]:
    """Copies one seed directory and removes files from the previous seed overlay."""

    destination.mkdir(parents=True, exist_ok=True)
    current_files = tuple(
        sorted(
            str(path.relative_to(source)).replace("/", "\\")
            for path in source.rglob("*")
            if path.is_file()
        )
    )
    current_set = set(current_files)
    for relative_file in previous_files:
        if relative_file in current_set:
            continue
        stale_path = (destination / relative_file).resolve()
        if destination.resolve() not in stale_path.parents:
            raise RuntimeError(f"Refusing to remove artifact seed outside destination: {stale_path}")
        if stale_path.is_file() or stale_path.is_symlink():
            stale_path.unlink()
            prune_empty_seed_dirs(stale_path.parent, destination)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    return current_files


def prune_empty_seed_dirs(path: Path, stop: Path) -> None:
    """Removes empty directories left by stale seed files."""

    resolved_stop = stop.resolve()
    current = path.resolve()
    while current != resolved_stop and resolved_stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def is_reparse_link(path: Path) -> bool:
    """Returns true for symlinks and Windows junction-style reparse points."""

    if path.is_symlink():
        return True
    if sys.platform != "win32":
        return False
    try:
        attributes = path.lstat().st_file_attributes
    except OSError:
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def write_workspace_props(root: Path) -> None:
    """Writes the generated MSBuild workspace props file."""

    content = """<?xml version="1.0" encoding="utf-8"?>
<Project ToolsVersion="Current" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <PropertyGroup>
    <WorkspaceRoot>$([MSBuild]::EnsureTrailingSlash('$(WorkspaceRoot)'))</WorkspaceRoot>
    <ReposRoot>$(WorkspaceRoot)repos\\</ReposRoot>
    <ThirdPartyRoot>$(ReposRoot)third_party\\</ThirdPartyRoot>
    <NlohmannJsonRoot>$(ThirdPartyRoot)emulebb-nlohmann-json\\single_include\\</NlohmannJsonRoot>
  </PropertyGroup>
  <ItemDefinitionGroup>
    <ClCompile>
      <AdditionalIncludeDirectories>$(NlohmannJsonRoot);$(ThirdPartyRoot)emulebb-cryptopp;$(ThirdPartyRoot)emulebb-resizablelib;$(ThirdPartyRoot)emulebb-zlib;$(ThirdPartyRoot)emulebb-miniupnp;%(AdditionalIncludeDirectories)</AdditionalIncludeDirectories>
    </ClCompile>
  </ItemDefinitionGroup>
</Project>
"""
    (root / WORKSPACE_PROPS_FILE_NAME).write_text(content, encoding="utf-8", newline="\n")


def write_workspace_manifest(root: Path, topology: WorkspaceTopology, workspace_name: str) -> None:
    """Writes the generated JSON workspace manifest."""

    path = root / "workspaces" / workspace_name / WORKSPACE_MANIFEST_NAME
    write_json(path, build_workspace_manifest(topology, workspace_name))


def write_repo_role_manifest(root: Path, topology: WorkspaceTopology, workspace_name: str) -> None:
    """Writes the generated repository role manifest."""

    path = root / "workspaces" / workspace_name / REPO_ROLE_MANIFEST_NAME
    write_json(path, build_repo_role_manifest(topology, workspace_name))


def write_compare_launchers(root: Path) -> None:
    """Writes simple compare launchers that route back through the Python CLI."""

    from .setup_commands import compare_presets

    topology = canonical_topology(include_analysis=True)
    compare_root = root / "analysis" / "compare"
    mods_root = compare_root / "mods-archive"
    compare_root.mkdir(parents=True, exist_ok=True)
    mods_root.mkdir(parents=True, exist_ok=True)
    _write_cmd(compare_root / "open-compare-menu.cmd", "python -m emule_workspace compare\n")
    _write_cmd(
        mods_root / "open-mods-archive-menu.cmd",
        "python -m emule_workspace compare mods-archive\n",
    )
    for preset in compare_presets(root, topology):
        destination_root = mods_root if preset.category == "Mods Archive" else compare_root
        _write_cmd(
            destination_root / f"{preset.key}.cmd",
            f'python -m emule_workspace compare "{preset.key}"\n',
        )


def install_workspace_hooks(root: Path, topology: WorkspaceTopology) -> None:
    """Configures workspace repos and worktrees to use the shared hook path."""

    hooks_path = root / "repos" / "emulebb-tooling" / "hooks"
    if not (hooks_path / "pre-commit").is_file():
        raise RuntimeError(f"Shared pre-commit hook is missing: {hooks_path / 'pre-commit'}")
    targets = [root / repo.relative_path for repo in topology.repos if repo.name in SHARED_HOOK_REPO_NAMES]
    targets.extend(root / worktree.relative_path for worktree in topology.app_repo.worktrees if worktree.active)
    for target in targets:
        if not target.is_dir():
            raise RuntimeError(f"Missing hook install target: {target}")
        configured = run_native(
            ["git", "-C", target, "config", "--local", "--get", "core.hooksPath"],
            label=f"read hooks path {target}",
            cwd=root,
            allow_failure=True,
        )
        current = run_captured(
            ["git", "-C", target, "config", "--local", "--get", "core.hooksPath"],
            label=f"hooks path {target}",
            cwd=root,
        ).strip() if configured.returncode == 0 else ""
        if current:
            current_path = (target / current).resolve() if not Path(current).is_absolute() else Path(current).resolve()
            if current_path != hooks_path.resolve():
                raise RuntimeError(f"Refusing to replace unmanaged core.hooksPath for '{target}'.")
        run_native(["git", "-C", target, "config", "--local", "core.hooksPath", str(hooks_path)], label=f"install hooks {target}", cwd=root)
        run_native(["git", "-C", target, "config", "--local", "core.autocrlf", "false"], label=f"configure autocrlf {target}", cwd=root)


def set_workspace_root_environment(root: Path) -> None:
    """Sets EMULEBB_WORKSPACE_ROOT for this process and the Windows user environment."""

    resolved = str(root.resolve())
    os.environ["EMULEBB_WORKSPACE_ROOT"] = resolved
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "EMULEBB_WORKSPACE_ROOT", 0, winreg.REG_EXPAND_SZ, resolved)
    print(f"EMULEBB_WORKSPACE_ROOT={resolved}")
    print("Restart existing shells to pick up the persisted user environment value.")


def log_line(root: Path, message: str) -> None:
    """Appends one setup log line under the workspace root."""

    log_path = root / SETUP_LOG_FILE_NAME
    with log_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(message + "\n")


def _write_cmd(path: Path, command: str) -> None:
    path.write_text("@ECHO OFF\n" + command.replace("\r\n", "\n"), encoding="ascii", newline="\n")
