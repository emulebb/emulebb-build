"""Local equivalents of CI checks, shared by Git hooks and manual validation."""
from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from uuid import uuid4
from time import perf_counter


def git(repo: Path, *args: str) -> str:
    """Run a read-only Git query, propagating errors."""
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).rstrip("\r\n")


def outgoing_ranges(repo: Path, remote: str, updates: str | None) -> list[tuple[str, str]]:
    """Resolve every non-deleted outgoing ref to the committed range it changes."""
    head = git(repo, "rev-parse", "HEAD")
    ranges = []
    if updates is not None:
        for line in updates.splitlines():
            local_ref, local_sha, remote_ref, remote_sha = line.split()
            if set(local_sha) == {"0"}:
                continue
            if local_ref.startswith("refs/tags/"):
                local_sha = git(repo, "rev-parse", f"{local_sha}^{{commit}}")
            # WHY: build commands inspect the checkout; validating another ref
            # here would falsely certify code that was never tested.
            if local_sha != head:
                raise RuntimeError(f"Check out {local_ref} and push it separately; it differs from HEAD")
            ranges.append((None if set(remote_sha) == {"0"} else remote_sha, local_sha))
    else:
        ranges.append((None, head))
    resolved = []
    for base, tip in ranges:
        if base is None:
            default_ref = git(repo, "symbolic-ref", f"refs/remotes/{remote}/HEAD")
            base = git(repo, "merge-base", tip, default_ref)
        resolved.append((base, tip))
    return resolved


def changed_paths(repo: Path, stage: str, remote: str, updates: str | None) -> set[str]:
    """Select staged paths or every outgoing ref's complete change range."""
    if stage == "commit":
        raw = git(repo, "diff", "--cached", "--name-only", "-z", "--")
        return set(filter(None, raw.split("\0")))
    paths: set[str] = set()
    for base, tip in outgoing_ranges(repo, remote, updates):
        paths.update(filter(None, git(repo, "diff", "--name-only", "-z", base, tip, "--").split("\0")))
    return paths


def check_whitespace(repo: Path, stage: str, remote: str, updates: str | None) -> None:
    """Check the index at commit time and actual outgoing changes at push time."""
    if stage == "commit":
        git(repo, "diff", "--cached", "--check", "--")
    else:
        # WHY: HEAD versus a clean worktree is empty, even when outgoing commits
        # introduce whitespace errors. Use the same ranges as change selection.
        for base, tip in outgoing_ranges(repo, remote, updates):
            git(repo, "diff", "--check", base, tip, "--")


def check_plan(repo: Path, paths: set[str], stage: str, root: Path, output: Path) -> list[tuple[Path, list[str]]]:
    """Choose checks by repository and changed surface, without running them."""
    tooling = root / "repos" / "emulebb-tooling"
    py = sys.executable
    commands: list[tuple[Path, list[str]]] = []
    policy = repo / "manifests" / "privacy-guard" / "policy.v1.json"
    if not policy.is_file():
        policy = tooling / "manifests" / "privacy-guard" / "policy.v1.json"
    commands.append((repo, [py, str(tooling / "ci" / "guard-tracked-files.py"),
                            "--repo-root", str(repo), "--policy-path", str(policy),
                            "--paths-file", str(output / "tmp" / "local-check" / f"{repo.name}-{stage}-paths.json")]))
    commands.append((repo, [py, str(tooling / "ci" / "check-basic-hygiene.py"),
                            "--repo-root", str(repo), "--repo-kind",
                            {"emulebb-build": "emulebb-build", "emulebb-tooling": "tooling"}.get(repo.name, "generic")]))
    workflow_changed = any(p.startswith(".github/workflows/") for p in paths)
    if workflow_changed and repo.name != "emulebb-rust":
        commands.append((repo, ["go", "run", "github.com/rhysd/actionlint/cmd/actionlint@914e7df21a07ef503a81201c76d2b11c789d3fca"]))
    implementation = any(not (p.endswith(".md") or p.startswith("docs/")) for p in paths)
    if repo.name == "emulebb-rust":
        gate = "pre-push" if stage == "push" and implementation else "branch-ci" if workflow_changed else "branch-check"
        commands.append((repo, [py, "tools/rust_quality_gate.py", gate]))
    if stage == "push" and repo.name == "emulebb-build" and implementation:
        tests = {"tests/test_local_checks.py", "tests/test_ci_workflows.py", "tests/test_locks.py"}
        for path in paths:
            if path.startswith("tests/") and path.endswith(".py") and (repo / path).is_file():
                tests.add(path)
            elif path.startswith("emule_workspace/") and path.endswith(".py"):
                if path == "emule_workspace/topology.py":
                    tests.update({"tests/test_layout.py", "tests/test_materialize.py", "tests/test_validation.py", "tests/test_setup_commands.py"})
                    continue
                test = f"tests/test_{Path(path).stem}.py"
                if (repo / test).is_file():
                    tests.add(test)
                else:
                    tests = {"tests"}
                    break
            elif path in {"pyproject.toml", "deps.json"}:
                tests = {"tests"}
                break
        commands.append((repo, [py, "-m", "pytest", *sorted(tests), "-o", f"cache_dir={output / 'cache' / 'pytest' / repo.name}", "--basetemp", str(output / "tmp" / "local-check-pytest")]))
    if repo.name == "emulebb-tooling":
        if stage == "push":
            if implementation:
                commands.append((repo, [py, "-m", "unittest", "discover", "-s", "ci/tests"]))
            if any(p.startswith("docs/") or p in {
                "mkdocs.yml", "requirements-docs.txt", ".github/workflows/docs-site.yml",
                "helpers/public-presence.py", "manifests/public-presence.v1.json",
                "scripts/docs-item-taxonomy-check.py", "scripts/docs-structure-check.py",
                "scripts/github-roadmap-check.py", "scripts/docs-publish-check.py",
            } for p in paths):
                commands.append((repo, [py, "scripts/docs-publish-check.py"]))
    return commands


def run_checks(*, repo: Path, stage: str, remote: str, updates: str | None, root: Path, output: Path) -> None:
    """Execute applicable checks; a missing tool or failed check blocks Git."""
    repo = repo.resolve()
    if not any(repo.is_relative_to(root / boundary) for boundary in ("repos", "workspaces")):
        raise RuntimeError("Local checks require a repository inside repos or workspaces")
    if repo != Path(git(repo, "rev-parse", "--show-toplevel")).resolve():
        raise RuntimeError("--repo must identify the repository root")
    paths = changed_paths(repo, stage, remote, updates)
    if not paths:
        print("No outgoing file changes; no local checks needed.")
        return
    dirty = git(repo, "status", "--porcelain") if stage == "push" else git(repo, "diff", "--name-only")
    if dirty:
        raise RuntimeError("Local validation requires a clean worktree for push and no unstaged tracked edits for commit")
    check_whitespace(repo, stage, remote, updates)
    target = Path(os.environ.get("CARGO_TARGET_DIR", "")).resolve()
    if repo.name == "emulebb-rust" and target != output / "builds" / "rust" / "target":
        raise RuntimeError("CARGO_TARGET_DIR must equal EMULEBB_WORKSPACE_OUTPUT_ROOT/builds/rust/target")
    child_env = os.environ.copy()
    scratch = output / "tmp" / "local-check"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / f"{repo.name}-{stage}-paths.json").write_text(json.dumps(sorted(paths)), encoding="utf-8")
    child_env.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch),
                     PYTHONPYCACHEPREFIX=str(output / "cache" / "pycache"),
                     GOPATH=str(output / "cache" / "go"), GOCACHE=str(output / "cache" / "go-build"))
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    reports = output / "reports" / "local-check" / repo.name / run_id
    reports.mkdir(parents=True)
    results = []
    for index, (cwd, command) in enumerate(check_plan(repo, paths, stage, root, output)):
        print(f"Local {stage}: {' '.join(command)}", flush=True)
        log_path = reports / f"{index:02d}.log"
        started = perf_counter()
        result = None
        error = None
        try:
            with log_path.open("w", encoding="utf-8") as log:
                result = subprocess.run(command, cwd=cwd, env=child_env, stdout=log, stderr=subprocess.STDOUT)
        except OSError as exc:
            error = str(exc)
            raise RuntimeError(f"Cannot start required local check {command[0]}: {exc}") from exc
        finally:
            results.append({"command": command, "returncode": result.returncode if result else None,
                            "log": str(log_path), "elapsed_seconds": round(perf_counter() - started, 3),
                            "error": error})
            (reports / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        print(f"Local check finished in {results[-1]['elapsed_seconds']:.3f}s", flush=True)
        if result.returncode:
            print("\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]))
            raise RuntimeError(f"Local check failed ({result.returncode}); evidence: {log_path}")
    if stage == "push" and git(repo, "status", "--porcelain"):
        raise RuntimeError("Validation changed the worktree; review changes before pushing")
    print(f"Local checks passed; evidence: {reports}")
    print("Other OS builds, hosted CodeQL, permissions and publication still require GitHub.")
