"""Parent-managed Git worktrees without changing canonical workspace roots."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from dataclasses import replace
from contextlib import ExitStack, contextmanager
from functools import wraps
from typing import Any
from uuid import uuid4
from .git_environment import git_environment
from .locks import lifecycle_file_lock


CORE_REPOSITORY_FIELDS = {
    "emulebb-rust": "emulebb_rust_repo_root",
    "emulebb-build": "build_repo_root",
    "emulebb-tooling": "tooling_repo_root",
    "emulebb-build-tests": "tests_repo_root",
}
OPTIONAL_REPOSITORY_FIELDS = {
    "emulebb-pages": "emulebb_pages_repo_root",
    "emulebb-org-profile": "emulebb_org_profile_repo_root",
}
REPOSITORY_FIELDS = {**CORE_REPOSITORY_FIELDS, **OPTIONAL_REPOSITORY_FIELDS}


def _git(repo: Path, *args: str, check: bool = True, input_text: str | None = None) -> str:
    # WHY: Git's stdin transaction protocol requires literal LF. Windows text
    # pipes translate LF to CRLF, so only this optional input uses UTF-8 bytes.
    pipe = {"text": True} if input_text is None else {"input": input_text.encode("utf-8")}
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            env=git_environment(), **pipe)
    stdout = result.stdout if isinstance(result.stdout, str) else result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr if isinstance(result.stderr, str) else result.stderr.decode("utf-8", errors="replace")
    if check and result.returncode:
        raise RuntimeError(stderr.strip() or f"Git {' '.join(args)} failed")
    return stdout.strip() if result.returncode == 0 else ""


def _id(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value):
        raise ValueError("Workstream IDs use lowercase letters, digits and hyphens (maximum 64)")
    return value


def _safe_path(root: Path, path: Path) -> Path:
    """Reject redirected ancestors before lifecycle operations touch source trees."""
    root = root.absolute()
    path = path.absolute()
    if not path.is_relative_to(root) or path == root:
        raise RuntimeError("Workstream path escapes its managed boundary")
    current = path
    while current != root.parent:
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise RuntimeError(f"Redirected workstream path is forbidden: {current}")
        current = current.parent
    if path.resolve() != path:
        raise RuntimeError("Workstream path must resolve inside its managed boundary")
    return path


def _registry(root: Path, output: Path) -> Path:
    if output.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("Workstream registration belongs outside the source workspace")
    return _safe_path(output.resolve(), output.resolve() / "tmp" / "workstreams")


def _write(root: Path, output: Path, registration: dict[str, Any], *, reserve: bool = False) -> None:
    directory = _registry(root, output)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{_id(registration['id'])}.json"
    if reserve:
        # WHY: never replace another creator's receipt, even an interrupted one.
        # A crash during this first write leaves an invalid, diagnosable receipt;
        # subsequent lifecycle updates use the atomic replacement below.
        try:
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(registration, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise RuntimeError("Workstream ID already registered") from exc
        return
    temporary = directory / f".{target.stem}-{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(registration, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _read(root: Path, output: Path, workstream_id: str) -> dict[str, Any]:
    path = _safe_path(_registry(root, output), _registry(root, output) / f"{_id(workstream_id)}.json")
    registration = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(registration, dict) or registration.get("schema") != 1 or registration.get("id") != workstream_id:
        raise RuntimeError("Invalid workstream registration identity or schema")
    repositories = registration.get("repositories")
    if not isinstance(repositories, dict) or any(not isinstance(entry, dict) for entry in repositories.values()):
        raise RuntimeError("Invalid workstream repository registration")
    if Path(registration["workspace_root"]).resolve() != root.resolve():
        raise RuntimeError("Registration belongs to another workspace")
    return registration


def _common_dir(repo: Path) -> str:
    return str(Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve())


@contextmanager
def workspace_lifecycle_guard(root: Path, output: Path, repositories=()):
    """Coordinate every setup/lifecycle writer before shared Git administration.

    The workspace registry guard comes first, then sorted existing Git common
    directories. Setup holds the same guard while creating missing anchors.
    Optional anchors are locked when present, without admitting them as inputs.
    """
    directory = _registry(root, output)
    directory.mkdir(parents=True, exist_ok=True)
    with ExitStack() as guards:
        guards.enter_context(lifecycle_file_lock(_safe_path(directory, directory / ".lifecycle.lock")))
        from .topology import canonical_topology
        declared = canonical_topology(profiles=("public-presence",))
        optional = [root / repo.relative_path.replace("\\", "/") for repo in declared.repos
                    if repo.name in OPTIONAL_REPOSITORY_FIELDS]
        common_dirs = {_common_dir(repo) for repo in repositories}
        for repo in optional:
            # A disabled optional profile must not make core work depend on a
            # missing/non-Git folder. A selected entry is validated separately.
            if repo.is_dir() and (repo / ".git").exists():
                _safe_path(root, repo)
                common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
                if common:
                    common_dirs.add(str(Path(common).resolve()))
        for common in sorted(common_dirs):
            common_root = Path(common)
            guards.enter_context(lifecycle_file_lock(_safe_path(common_root, common_root / "emulebb-workstreams.lock")))
        yield


@contextmanager
def _lifecycle_guard(layout):
    """Hold the common setup/lifecycle guard for admitted canonical anchors."""
    with workspace_lifecycle_guard(layout.emule_workspace_root, layout.output_root,
                                   _sources(layout).values()):
        yield


def _lifecycle_operation(function):
    @wraps(function)
    def guarded(layout, *args, **kwargs):
        with _lifecycle_guard(layout):
            return function(layout, *args, **kwargs)
    return guarded


def _check_entry(root: Path, workstream_id: str, name: str, entry: dict[str, Any]) -> None:
    if name not in REPOSITORY_FIELDS:
        raise RuntimeError(f"Unsupported registered repository: {name}")
    expected = _safe_path(root.resolve(), root.resolve() / "workspaces" / "agents" / _id(workstream_id) / name)
    if Path(entry["path"]).absolute() != expected or entry.get("status") != "ready":
        raise RuntimeError(f"Workstream {workstream_id}/{name} is incomplete or has an invalid path")
    if not expected.is_dir() or Path(_git(expected, "rev-parse", "--show-toplevel")).resolve() != expected:
        raise RuntimeError(f"Missing workstream checkout: {expected}")
    canonical = Path(entry["canonical_path"]).resolve()
    if not canonical.is_relative_to(root.resolve() / "repos") or canonical.name != name:
        raise RuntimeError("Invalid canonical repository identity")
    if _common_dir(expected) != entry["common_dir"] or _common_dir(canonical) != entry["common_dir"]:
        raise RuntimeError("Workstream Git common directory changed")
    if _git(expected, "remote", "get-url", "origin") != entry["origin"]:
        raise RuntimeError("Workstream origin changed")
    if _git(expected, "branch", "--show-current") != entry["branch"]:
        raise RuntimeError("Workstream branch changed")


def resolve_workstream(root: Path, output: Path, repo: Path | None = None,
                       workstream_id: str | None = None) -> dict[str, Any] | None:
    """Resolve and verify selected source worktrees; canonical calls remain unchanged."""
    root, output = root.resolve(), output.resolve()
    if workstream_id is None and repo is not None:
        selected = repo.resolve()
        boundary = root / "workspaces" / "agents"
        if selected.is_relative_to(boundary):
            parts = selected.relative_to(boundary).parts
            if len(parts) < 2:
                raise RuntimeError("Expected a registered workstream repository root")
            workstream_id = parts[0]
            repo = boundary / parts[0] / parts[1]
    if workstream_id is None:
        return None
    registration = _read(root, output, workstream_id)
    if registration.get("state") != "ready" or not registration.get("repositories"):
        if registration.get("state") == "retiring-repository":
            raise RuntimeError("Repository retirement is incomplete; rerun workstream remove-repo "
                               "with the recorded repository and parent owner from outside this workstream")
        raise RuntimeError("Workstream registration is incomplete; parent recovery is required")
    for name, entry in registration["repositories"].items():
        _check_entry(root, workstream_id, name, entry)
    if repo is not None and repo.resolve() not in {Path(e["path"]).resolve() for e in registration["repositories"].values()}:
        raise RuntimeError("Selected repository does not belong to this workstream")
    return registration


def apply_workstream_layout(layout, registration: dict[str, Any]):
    """Select registered source roots without altering output or environment contracts."""
    return replace(layout, **{REPOSITORY_FIELDS[name]: Path(entry["path"])
                              for name, entry in registration["repositories"].items()})


def _sources(layout) -> dict[str, Path]:
    sources = {}
    for name, field in REPOSITORY_FIELDS.items():
        value = getattr(layout, field, None)
        if value is not None:
            path = Path(value).resolve()
            if name in OPTIONAL_REPOSITORY_FIELDS and not path.is_dir():
                continue
            if not path.is_relative_to(layout.emule_workspace_root.resolve() / "repos"):
                # WHY: lifecycle code may itself run from a managed builder worktree.
                # Its shared Git directory identifies the canonical anchor without
                # deriving a second repository topology from directory names.
                common = Path(_common_dir(path))
                candidate = common.parent
                if (common.name != ".git" or candidate.name != name
                        or not candidate.is_relative_to(layout.emule_workspace_root.resolve() / "repos")
                        or _common_dir(candidate) != str(common)):
                    raise RuntimeError("Selected repository has no canonical workspace anchor")
                path = candidate
            if name in OPTIONAL_REPOSITORY_FIELDS:
                _safe_path(layout.emule_workspace_root, path)
                if path.name != name:
                    raise RuntimeError("Optional topology has an invalid canonical repository identity")
            sources[name] = path
    return sources


def _prepare_entry(layout, registration: dict[str, Any], name: str, *, base: str | None = None) -> dict[str, Any]:
    sources = _sources(layout)
    if name not in sources:
        raise RuntimeError(f"Unsupported or unavailable repository: {name}")
    canonical = sources[name]
    if name in OPTIONAL_REPOSITORY_FIELDS:
        hooks = _git(canonical, "config", "--get", "core.hooksPath", check=False)
        expected_hooks = sources["emulebb-tooling"] / "hooks"
        selected_hooks = (canonical / hooks).resolve() if hooks else None
        if selected_hooks != expected_hooks or not all((expected_hooks / hook).is_file()
                                                       for hook in ("pre-commit", "pre-push")):
            raise RuntimeError("Optional repository requires completed shared-hook setup")
    destination = _safe_path(layout.emule_workspace_root.resolve(), layout.emule_workspace_root.resolve()
                             / "workspaces" / "agents" / registration["id"] / name)
    if destination.exists():
        raise RuntimeError(f"Checkout already exists; refusing adoption: {destination}")
    if _git(canonical, "show-ref", "--verify", f"refs/heads/{registration['branch']}", check=False):
        raise RuntimeError(f"Branch already exists: {registration['branch']}")
    advertised = _git(canonical, "ls-remote", "--exit-code", "origin", "refs/heads/main").splitlines()
    if len(advertised) != 1:
        raise RuntimeError("Origin must advertise one exact main revision")
    revision, ref = advertised[0].split()
    if ref != "refs/heads/main" or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        raise RuntimeError("Origin advertised an invalid main revision")
    # WHY: ordinary fetch updates shared origin/main and FETCH_HEAD while an
    # active validator may still read them. Fetch the advertised immutable tip
    # without ref mappings or FETCH_HEAD; never fall back to a moving ref.
    _git(canonical, "fetch", "--no-tags", "--no-prune", "--no-prune-tags",
         "--no-write-fetch-head", "--refmap=", "origin", revision)
    revision = _git(canonical, "rev-parse", "--verify", f"{base or revision}^{{commit}}")
    return {"path": str(destination), "canonical_path": str(canonical), "branch": registration["branch"],
            "base_revision": revision, "common_dir": _common_dir(canonical),
            "origin": _git(canonical, "remote", "get-url", "origin"), "status": "planned"}


def _materialize(layout, registration: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    root, output = layout.emule_workspace_root.resolve(), layout.output_root.resolve()
    registration["state"] = "creating"
    _write(root, output, registration)
    for name in names:
        entry = registration["repositories"][name]
        path = Path(entry["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(Path(entry["canonical_path"]), "worktree", "add", "-b", entry["branch"], str(path), entry["base_revision"])
        entry["status"] = "ready"
        _write(root, output, registration)
    registration["state"] = "ready"
    _write(root, output, registration)
    return registration


@_lifecycle_operation
def create_workstream(layout, workstream_id: str, branch: str, owner: str, repositories: tuple[str, ...],
                      *, issues: tuple[str, ...] = (), pull_requests: tuple[str, ...] = ()) -> dict[str, Any]:
    """Create unrelated checkouts from the exact advertised main tip under lifecycle guards."""
    workstream_id = _id(workstream_id)
    root, output = layout.emule_workspace_root.resolve(), layout.output_root.resolve()
    if not owner.strip() or not repositories or len(set(repositories)) != len(repositories):
        raise ValueError("A workstream needs an owner and unique repositories")
    if not re.fullmatch(r"(?:feature|fix|chore)/.+", branch):
        raise ValueError("Branch must use feature/, fix/ or chore/")
    canonical = next(iter(_sources(layout).values()))
    _git(canonical, "check-ref-format", "--branch", branch)
    if (_registry(root, output) / f"{workstream_id}.json").exists():
        raise RuntimeError("Workstream ID already registered")
    registration = {"schema": 1, "id": workstream_id, "workspace_root": str(root), "branch": branch,
                    "owner": owner, "issues": list(issues), "pull_requests": list(pull_requests),
                    "state": "creating", "repositories": {}}
    for name in repositories:
        registration["repositories"][name] = _prepare_entry(layout, registration, name)
    _write(root, output, registration, reserve=True)
    return _materialize(layout, registration, repositories)


@_lifecycle_operation
def add_repository(layout, workstream_id: str, name: str) -> dict[str, Any]:
    """Add one new checkout, preserving all existing workstream branches."""
    registration = resolve_workstream(layout.emule_workspace_root, layout.output_root, workstream_id=workstream_id)
    if name in registration["repositories"]:
        raise RuntimeError("Repository already registered in workstream")
    registration["repositories"][name] = _prepare_entry(layout, registration, name)
    return _materialize(layout, registration, (name,))


@_lifecycle_operation
def register_bootstrap(layout, workstream_id: str, branch: str, owner: str,
                       base_revisions: dict[str, str], *, allow_dirty: bool = False,
                       extend: bool = False) -> dict[str, Any]:
    """Register explicitly parent-created bootstrap worktrees, never adopt arbitrary paths.

    This internal bootstrap is used only while introducing the lifecycle itself.
    Normal operator commands must use create_workstream and never adopt checkouts.
    """
    root, output = layout.emule_workspace_root.resolve(), layout.output_root.resolve()
    workstream_id = _id(workstream_id)
    if not owner.strip() or not base_revisions:
        raise ValueError("Bootstrap requires an assigned parent and exact base revisions")
    exists = (_registry(root, output) / f"{workstream_id}.json").exists()
    if exists and not extend:
        raise RuntimeError("Workstream ID already registered")
    registration = {"schema": 1, "id": workstream_id, "workspace_root": str(root), "branch": branch,
                    "owner": owner, "issues": [], "pull_requests": [], "state": "ready", "repositories": {}}
    if exists:
        registration = resolve_workstream(root, output, workstream_id=workstream_id)
        if registration["owner"] != owner or registration["branch"] != branch:
            raise RuntimeError("Bootstrap extension has a different owner or branch")
    sources = _sources(layout)
    for name, base in base_revisions.items():
        if name not in sources or not re.fullmatch(r"[0-9a-fA-F]{40}", base):
            raise ValueError("Bootstrap requires supported repositories and full commit hashes")
        if name in registration["repositories"]:
            if registration["repositories"][name]["base_revision"] != base:
                raise RuntimeError("Bootstrap extension cannot replace an existing base revision")
            continue
        path = _safe_path(root, root / "workspaces" / "agents" / workstream_id / name)
        canonical = sources[name]
        _git(path, "merge-base", "--is-ancestor", base, "HEAD")
        entry = {"path": str(path), "canonical_path": str(canonical), "branch": branch,
                 "base_revision": base, "common_dir": _common_dir(canonical),
                 "origin": _git(canonical, "remote", "get-url", "origin"), "status": "ready"}
        _check_entry(root, workstream_id, name, entry)
        if not allow_dirty and _git(path, "status", "--porcelain"):
            raise RuntimeError("Bootstrap checkout is dirty")
        registration["repositories"][name] = entry
    _write(root, output, registration)
    return registration


def workstream_status(layout, workstream_id: str | None = None) -> list[dict[str, Any]]:
    """Report registrations and verification errors without changing Git or disk state."""
    root, output = layout.emule_workspace_root, layout.output_root
    ids = [workstream_id] if workstream_id else [p.stem for p in sorted(_registry(root, output).glob("*.json"))]
    result = []
    for selected in ids:
        try:
            registration = resolve_workstream(root, output, workstream_id=selected)
            result.append({**registration, "verification": "valid"})
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            result.append({"id": selected, "verification": "invalid", "error": str(exc)})
    return result


def _open_pull_requests(entry: dict[str, Any]) -> list[dict[str, Any]]:
    result = subprocess.run(["gh", "pr", "list", "--repo", entry["origin"], "--head", entry["branch"],
                             "--state", "open", "--json", "number,url"], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Cannot establish PR state; cleanup refused: " + result.stderr.strip())
    return json.loads(result.stdout)


def _removal_preflight(entry: dict[str, Any], *, abandoned: bool = False,
                       checkout_present: bool = True) -> str:
    """Verify cleanup eligibility without discarding a branch or checkout."""
    path, canonical = Path(entry["path"]), Path(entry["canonical_path"])
    if checkout_present and _git(path, "status", "--porcelain", "--ignored"):
        raise RuntimeError("Dirty worktree; cleanup refused")
    _git(canonical, "fetch", "origin", "main")
    requests = _open_pull_requests(entry)
    if not isinstance(requests, list):
        raise RuntimeError("Cannot establish PR state; cleanup refused")
    if requests:
        raise RuntimeError("Open pull request; cleanup refused")
    ref = "refs/heads/" + entry["branch"]
    head = _git(canonical, "rev-parse", "--verify", ref + "^{commit}")
    merged = bool(_git(canonical, "branch", "-r", "--contains", head, "origin/main"))
    if not merged:
        # WHY: a rebase merge rewrites commit identities. Git's patch comparison
        # recognizes equivalent published outcomes without deleting history.
        merged = not any(line.startswith("+") for line in
                         _git(canonical, "cherry", "origin/main", head).splitlines())
    if not merged:
        upstream_ref = entry["branch"] + "@{upstream}"
        upstream = _git(canonical, "rev-parse", "--verify", upstream_ref, check=False)
        if (not abandoned
                and (not upstream or _git(canonical, "rev-list", "--count", f"{upstream}..{head}") != "0")):
            raise RuntimeError("Unpublished work; cleanup refused")
        if not abandoned:
            raise RuntimeError("Unmerged branch; explicit parent abandonment is required")
    return head


def _archive_abandoned_head(workstream_id: str, name: str, entry: dict[str, Any], head: str) -> None:
    """Retain exact abandoned history before removing any checkout or runtime."""
    canonical = Path(entry["canonical_path"])
    archive = f"refs/recovery/abandoned/{_id(workstream_id)}/{name}"
    branch = "refs/heads/" + entry["branch"]
    _git(canonical, "check-ref-format", archive)
    existing = _git(canonical, "rev-parse", "--verify", archive, check=False)
    if (_git(canonical, "symbolic-ref", "--quiet", archive, check=False)
            or existing and existing != head):
        raise RuntimeError("Abandoned recovery ref already has a different identity; cleanup refused")
    # WHY: branch deletion remains a separate parent decision. Git retains all
    # reachable commits via this local ref, without publishing withdrawn code.
    # Verify the source head and recovery-ref lease in one atomic Git operation.
    transaction = (f"start\noption no-deref\nverify {branch} {head}\n"
                   f"option no-deref\nupdate {archive} {head} {existing or '0' * len(head)}\n"
                   "prepare\ncommit\n")
    _git(canonical, "update-ref", "--stdin", input_text=transaction)
    if _git(canonical, "rev-parse", "--verify", archive) != head:
        raise RuntimeError("Abandoned recovery ref changed; cleanup refused")
    print(f"Archived abandoned head: {name} {archive} {head}")


def _check_removal_snapshot(layout, registration: dict[str, Any], heads: dict[str, str]) -> None:
    """Refuse changed ownership or sources after network and reference operations."""
    if _read(layout.emule_workspace_root, layout.output_root, registration["id"]) != registration:
        raise RuntimeError("Workstream registration changed during removal preflight")
    for name, entry in registration["repositories"].items():
        _check_entry(layout.emule_workspace_root, registration["id"], name, entry)
        path = Path(entry["path"])
        if (_git(path, "status", "--porcelain", "--ignored")
                or _git(path, "rev-parse", "HEAD") != heads[name]):
            raise RuntimeError("Checkout changed during removal preflight")


def _retirement_identity(layout, registration: dict[str, Any], name: str,
                         entry: dict[str, Any]) -> bool:
    """Verify either the exact live checkout or its cleanly removed Git identity."""
    root = layout.emule_workspace_root.resolve()
    expected = _safe_path(root, root / "workspaces" / "agents" / _id(registration["id"]) / name)
    canonical = _sources(layout).get(name)
    if canonical is None and name in OPTIONAL_REPOSITORY_FIELDS:
        # Retire a previously admitted optional checkout even if a subsequent
        # setup omitted that profile. The declaration still fixes its anchor;
        # this does not admit it as a new consumed source.
        from .topology import canonical_topology
        declared = canonical_topology(profiles=("public-presence",))
        canonical = next((_safe_path(root, root / repo.relative_path.replace("\\", "/"))
                          for repo in declared.repos if repo.name == name), None)
    if (canonical is None or Path(entry["path"]).absolute() != expected
            or Path(entry["canonical_path"]).absolute() != canonical
            or entry.get("status") != "ready" or entry["branch"] != registration["branch"]
            or _common_dir(canonical) != entry["common_dir"]
            or _git(canonical, "remote", "get-url", "origin") != entry["origin"]):
        raise RuntimeError("Repository retirement identity changed")
    if expected.exists():
        _check_entry(root, registration["id"], name, entry)
        if Path.cwd().resolve().is_relative_to(expected):
            raise RuntimeError("Run remove-repo from outside the checkout being retired")
        return True
    # WHY: a missing folder alone is not proof of a successful Git removal.
    # Never prune dangling administration or adopt a replacement on retry.
    records = _git(canonical, "worktree", "list", "--porcelain", "-z").split("\0")
    if any(field.startswith("worktree ") and Path(field[9:]).absolute() == expected for field in records):
        raise RuntimeError("Checkout is missing but Git worktree administration remains; retirement refused")
    return False


@_lifecycle_operation
def remove_repository(layout, workstream_id: str, name: str, owner: str) -> dict[str, Any]:
    """Retire sources and their owned runtime together after the normal preflight."""
    from .rust_runtime_stage import runtime_stage_retirement

    with runtime_stage_retirement(layout, workstream_id, enabled=name == "emulebb-rust") as retire:
        return _remove_repository(layout, workstream_id, name, owner, retire_runtime=retire)


def _remove_repository(layout, workstream_id: str, name: str, owner: str, *, retire_runtime) -> dict[str, Any]:
    """Retire one integrated companion, resuming only its exact atomic journal.

    The CLI holds the heavy workspace lock before lifecycle/common-Git guards.
    Interrupted retirement blocks all source selection until this command is
    repeated from outside the workstream. Branches and unrelated entries remain.
    """
    root, output = layout.emule_workspace_root.resolve(), layout.output_root.resolve()
    registration = _read(root, output, workstream_id)
    if owner != registration.get("owner"):
        raise RuntimeError("Removal must identify the assigned parent owner")
    if name not in REPOSITORY_FIELDS or name not in registration["repositories"]:
        raise RuntimeError("Repository is not registered in this workstream")
    if len(registration["repositories"]) <= 1:
        raise RuntimeError("Cannot retire the last repository; use workstream remove")
    entry = registration["repositories"][name]
    state = registration.get("state")
    journal = registration.get("retirement")
    if state == "ready":
        if journal is not None:
            raise RuntimeError("Ambiguous repository retirement registration")
        resolve_workstream(root, output, workstream_id=workstream_id)
    elif state == "retiring-repository":
        if (not isinstance(journal, dict) or journal.get("repository") != name
                or journal.get("entry") != entry
                or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(journal.get("head", "")))):
            raise RuntimeError("Incomplete or conflicting repository retirement journal")
    else:
        raise RuntimeError("Workstream registration is incomplete; retirement refused")
    for remaining, value in registration["repositories"].items():
        if remaining != name:
            _check_entry(root, workstream_id, remaining, value)
    present = _retirement_identity(layout, registration, name, entry)
    if state == "ready" and not present:
        raise RuntimeError("Missing checkout without a retirement journal; refusing adoption")
    head = _removal_preflight(entry, checkout_present=present)
    if journal is not None and head != journal["head"]:
        raise RuntimeError("Retained branch changed during repository retirement")
    retire_runtime()
    if journal is None:
        journal = {"repository": name, "entry": dict(entry), "head": head}
        registration["state"], registration["retirement"] = "retiring-repository", journal
        _write(root, output, registration)
    if present:
        # Recheck after network probes and the durable transition; Git's normal
        # removal is an additional dirty/untracked guard, never a force removal.
        _check_entry(root, workstream_id, name, entry)
        if (_git(Path(entry["path"]), "status", "--porcelain", "--ignored")
                or _git(Path(entry["path"]), "rev-parse", "HEAD") != head):
            raise RuntimeError("Checkout changed during repository retirement")
        _git(Path(entry["canonical_path"]), "worktree", "remove", entry["path"])
    if _retirement_identity(layout, registration, name, entry):
        raise RuntimeError("Checkout still exists after Git removal; retirement incomplete")
    if _git(Path(entry["canonical_path"]), "rev-parse", "refs/heads/" + entry["branch"]) != head:
        raise RuntimeError("Retained branch changed during repository retirement")
    for remaining, value in registration["repositories"].items():
        if remaining != name:
            _check_entry(root, workstream_id, remaining, value)
    del registration["repositories"][name]
    del registration["retirement"]
    registration["state"] = "ready"
    _write(root, output, registration)
    return registration


@_lifecycle_operation
def remove_workstream(layout, workstream_id: str, owner: str, *, abandoned: bool = False) -> None:
    """Retire an admitted runtime namespace under the same owner as its source cleanup."""
    from .rust_runtime_stage import runtime_stage_retirement

    with runtime_stage_retirement(layout, workstream_id) as retire:
        _remove_workstream(layout, workstream_id, owner, abandoned=abandoned, retire_runtime=retire)


def _remove_workstream(layout, workstream_id: str, owner: str, *, abandoned: bool, retire_runtime) -> None:
    """Parent cleanup archives explicit abandonment; ordinary removal remains strict."""
    registration = resolve_workstream(layout.emule_workspace_root, layout.output_root, workstream_id=workstream_id)
    if owner != registration["owner"]:
        raise RuntimeError("Removal must identify the assigned parent owner")
    heads = {name: _removal_preflight(entry, abandoned=abandoned)
             for name, entry in registration["repositories"].items()}
    _check_removal_snapshot(layout, registration, heads)
    if abandoned:
        for name, entry in registration["repositories"].items():
            _archive_abandoned_head(workstream_id, name, entry, heads[name])
        _check_removal_snapshot(layout, registration, heads)
    retire_runtime()
    registration["state"] = "removing"
    _write(layout.emule_workspace_root, layout.output_root, registration)
    for name, entry in registration["repositories"].items():
        if (_git(Path(entry["path"]), "status", "--porcelain", "--ignored")
                or _git(Path(entry["path"]), "rev-parse", "HEAD") != heads[name]):
            raise RuntimeError("Checkout changed during removal")
        _git(Path(entry["canonical_path"]), "worktree", "remove", entry["path"])
    # WHY: GitHub rebase merges and explicit abandonment can leave valuable local
    # history unmerged by identity. Keep branches; this command never force-deletes.
    (_registry(layout.emule_workspace_root, layout.output_root) / f"{_id(workstream_id)}.json").unlink()
