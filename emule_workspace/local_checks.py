"""Local equivalents of CI checks, shared by Git hooks and manual validation."""
from __future__ import annotations

import os
import json
import re
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from uuid import uuid4
from time import perf_counter
from .git_environment import git_environment


def git(repo: Path, *args: str) -> str:
    """Run a read-only Git query, propagating errors."""
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True,
                                   env=git_environment()).rstrip("\r\n")


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


def linux_push_scope(paths: set[str], repo: Path | None = None) -> str:
    """Expand platform, dependency, and packaging changes to all available CPUs."""
    platform_owners = (
        ".github/workflows/", ".cargo/", "packaging/", "policy/",
        "crates/emulebb-miniupnpc", "crates/emulebb-pcpnatpmp",
        "crates/emulebb-kad-net/src/socket_opts", "crates/emulebb-ed2k/src/long_path",
        "crates/emulebb-ed2k/src/ed2k_transfer/fs_durability",
        "tools/ci-toolchain.json", "tools/rust_quality_gate.py", "tools/release_",
    )
    if any(path.startswith(platform_owners) or Path(path).name in {
        "Cargo.toml", "Cargo.lock", "build.rs", "rust-toolchain.toml",
    } for path in paths):
        return "full"
    if repo is not None:
        for path in paths:
            if not path.endswith(".rs"):
                continue
            source = repo / path
            # WHY: platform code can live outside known owners. Missing files
            # include deletions; neither case may escape cross-target checks.
            if not source.is_file() or re.search(
                r"\btarget_(?:os|arch|env|family|endian|pointer_width|vendor|feature)\s*="
                r"|\bcfg(?:_attr)?\s*!?\s*\([^;\]]*\b(?:windows|unix)\b"
                r"|\b(?:std|core)::os::(?:windows|unix)\b|\bwindows(?:_sys)?::",
                source.read_text(encoding="utf-8"),
            ):
                return "full"
    return "musl"


def check_plan(repo: Path, paths: set[str], stage: str, root: Path, output: Path,
               sources: dict[str, Path] | None = None, workstream: str | None = None,
               paths_file: Path | None = None) -> list[tuple[Path, list[str]]]:
    """Choose checks by repository and changed surface, without running them."""
    sources = sources or {}
    tooling = sources.get("emulebb-tooling", root / "repos" / "emulebb-tooling")
    builder = sources.get("emulebb-build", root / "repos" / "emulebb-build")
    py = sys.executable
    commands: list[tuple[Path, list[str]]] = []
    policy = repo / "manifests" / "privacy-guard" / "policy.v1.json"
    if not policy.is_file():
        policy = tooling / "manifests" / "privacy-guard" / "policy.v1.json"
    commands.append((repo, [py, str(tooling / "ci" / "guard-tracked-files.py"),
                            "--repo-root", str(repo), "--policy-path", str(policy),
                            "--paths-file", str(paths_file or output / "tmp" / "local-check" / f"{repo.name}-{stage}-paths.json")]))
    commands.append((repo, [py, str(tooling / "ci" / "check-basic-hygiene.py"),
                            "--repo-root", str(repo), "--repo-kind",
                            {"emulebb-build": "emulebb-build", "emulebb-tooling": "tooling"}.get(repo.name, "generic")]))
    workflow_changed = any(p.startswith(".github/workflows/") for p in paths)
    if workflow_changed and repo.name != "emulebb-rust":
        commands.append((repo, ["go", "run", "github.com/rhysd/actionlint/cmd/actionlint@914e7df21a07ef503a81201c76d2b11c789d3fca"]))
    implementation = any(not (p.endswith(".md") or p.startswith("docs/")) for p in paths)
    if repo.name == "emulebb-rust":
        gate = "pre-push" if stage == "push" and implementation else "branch-ci" if workflow_changed else "branch-check"
        gate_command = [py, "tools/rust_quality_gate.py", gate]
        if workstream and stage == "push" and implementation:
            gate_command.extend(["--rest-harness-script", str(sources.get("emulebb-build-tests", root / "repos/emulebb-build-tests") / "scripts/rust-rest-openapi-ci.py"),
                                 "--rest-openapi-contract", str(tooling / "docs/products/emulebb-rust/api/REST-API-OPENAPI.yaml")])
        commands.append((repo, gate_command))
        if stage == "push" and implementation:
            commands.append((builder, [py, "-B", "-m", "emule_workspace",
                             "validate-emulebb-rust-linux", "--scope", linux_push_scope(paths, repo)]))
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
    if stage == "push" and repo.name == "emulebb-build-tests" and implementation:
        tests: set[str] = set()
        for path in paths:
            if path.startswith("tests/python/") and path.endswith(".py") and (repo / path).is_file():
                tests.add(path)
            elif path.endswith(".py") and path.startswith(("scripts/", "emule_test_harness/")):
                test = f"tests/python/test_{Path(path).stem.replace('-', '_')}.py"
                if (repo / test).is_file():
                    tests.add(test)
                else:
                    # Shared owners without a single test file can affect
                    # several suites. Pytest's normal live/native exclusions apply.
                    tests = {"tests/python"}
                    break
            elif path in {"pyproject.toml", "requirements.txt"} or path.startswith(".github/workflows/"):
                tests = {"tests/python"}
                break
        if tests:
            commands.append((repo, [py, "-m", "pytest", *sorted(tests), "-o",
                                    f"cache_dir={output / 'cache' / 'pytest' / repo.name}",
                                    "--basetemp", str(output / "tmp" / "local-check-harness-pytest")]))
    linux_owners = {
        "emulebb-build": ("emule_workspace/linux_containers.py", "emule_workspace/linux_validation.py",
                          "emule_workspace/release_assets/rust_linux/"),
        "emulebb-build-tests": ("scripts/smoke-rust-release-package.py",),
    }
    image_owners = {
        "emulebb-build": ("emule_workspace/rust_images.py", "emule_workspace/image_security.py",
                          "emule_workspace/release_assets/rust_images/"),
        "emulebb-build-tests": ("scripts/smoke-rust-container.py",),
    }
    full_linux = any(path.startswith(linux_owners.get(repo.name, ())) for path in paths)
    image_only = any(path.startswith(image_owners.get(repo.name, ())) for path in paths)
    if stage == "push" and (full_linux or image_only):
        commands.append((builder, [py, "-B", "-m", "emule_workspace",
                         "validate-emulebb-rust-linux", "--scope", "full" if full_linux else "image"]))
    android_tools_changed = repo.name == "emulebb-build" and any(path.startswith((
            "emule_workspace/android_inputs.py", "emule_workspace/android_install.py",
            "emule_workspace/android_probe.py", "emule_workspace/android_toolchain.py",
            "emule_workspace/android_validation.py", "emule_workspace/release_assets/android/")) for path in paths)
    if stage == "push" and (android_tools_changed or repo.name == "emulebb-rust" and implementation):
        commands.append((builder, [py, "-B", "-m", "emule_workspace", "validate-rust-android"]))
    emulator_tools_changed = repo.name == "emulebb-build" and any(path in {
        "emule_workspace/android_inputs.py", "emule_workspace/android_install.py", "emule_workspace/android_probe.py",
        "emule_workspace/android_toolchain.py", "emule_workspace/release_assets/android/Dockerfile",
        "emule_workspace/release_assets/android/Dockerfile.emulator", "emule_workspace/release_assets/android/toolchain.json",
        "emule_workspace/release_assets/android/emulator-inputs.json",
    } for path in paths)
    if stage == "push" and emulator_tools_changed:
        commands.append((builder, [py, "-B", "-m", "emule_workspace", "prepare-rust-android-emulator"]))
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
    if stage == "push" and repo.name in {"emulebb-pages", "emulebb-org-profile"}:
        commands.append((builder, [py, "-B", "-m", "emule_workspace", "test", "public-presence",
                                   "--repo", repo.name]))
    if workstream:
        for _cwd, command in commands:
            if "emule_workspace" in command:
                command.extend(["--workstream", workstream])
    return commands


def _resolve_registration(root: Path, output: Path, repo: Path, workstream: str | None) -> dict | None:
    """Require registration for managed agent checkouts without changing roots."""
    if workstream or repo.is_relative_to(root / "workspaces" / "agents"):
        from .workstreams import resolve_workstream
        return resolve_workstream(root, output, repo=repo, workstream_id=workstream)
    return None


def _source_snapshot(sources: dict[str, Path], *, clean: bool) -> dict:
    """Capture actual revisions; require stable clean inputs for push evidence."""
    snapshot = {}
    for name, source in sources.items():
        revision = git(source, "rev-parse", "HEAD")
        dirty = git(source, "status", "--porcelain")
        if clean and dirty:
            raise RuntimeError(f"Source {name} requires a clean worktree for push validation")
        snapshot[name] = {"path": str(source), "revision": revision, "dirty": dirty}
    return snapshot


def _workstream_sources(root: Path, output: Path, registration: dict | None,
                        repo_name: str | None = None) -> dict[str, Path]:
    """Resolve supporting sources from generated topology before applying selection."""
    from .layout import load_layout
    from .workstreams import CORE_REPOSITORY_FIELDS, OPTIONAL_REPOSITORY_FIELDS, REPOSITORY_FIELDS, apply_workstream_layout
    layout = load_layout(root, output_root=output)
    if registration:
        layout = apply_workstream_layout(layout, registration)
    # Optional public repositories consume their own content and authority,
    # never unrelated client sources or the other optional repository.
    names = ("emulebb-build", "emulebb-tooling", repo_name) if repo_name in OPTIONAL_REPOSITORY_FIELDS else CORE_REPOSITORY_FIELDS
    sources = {}
    for name in names:
        source = getattr(layout, REPOSITORY_FIELDS[name])
        if source is None:
            raise RuntimeError(f"Required validation repository is missing from topology: {name}")
        sources[name] = Path(source).resolve()
    return sources


def run_checks(*, repo: Path, stage: str, remote: str, updates: str | None, root: Path, output: Path,
               workstream: str | None = None) -> None:
    """Execute applicable checks; a missing tool or failed check blocks Git."""
    # WHY: a UTF-8 subprocess log can contain characters the Windows console
    # cannot encode. Preserve that log and report the original failing check.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="backslashreplace")
    repo = repo.resolve()
    if not any(repo.is_relative_to(root / boundary) for boundary in ("repos", "workspaces")):
        raise RuntimeError("Local checks require a repository inside repos or workspaces")
    if repo != Path(git(repo, "rev-parse", "--show-toplevel")).resolve():
        raise RuntimeError("--repo must identify the repository root")
    registration = _resolve_registration(root, output, repo, workstream)
    source_paths = {repo.name: repo}
    if registration or repo.name in {"emulebb-pages", "emulebb-org-profile"}:
        source_paths = _workstream_sources(root, output, registration, repo.name)
        if source_paths.get(repo.name) != repo:
            raise RuntimeError("--repo is not the selected workstream checkout")
    checked_head = git(repo, "rev-parse", "HEAD") if stage == "push" else None
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
    child_env = git_environment()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    scratch = output / "tmp" / "local-check" / run_id
    scratch.mkdir(parents=True)
    paths_file = scratch / f"{repo.name}-{stage}-paths.json"
    paths_file.write_text(json.dumps(sorted(paths)), encoding="utf-8")
    child_env.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch),
                     PYTHONPYCACHEPREFIX=str(output / "cache" / "pycache"),
                     GOPATH=str(output / "cache" / "go"), GOCACHE=str(output / "cache" / "go-build"))
    reports = output / "reports" / "local-check" / repo.name / run_id
    reports.mkdir(parents=True)
    snapshot = _source_snapshot(source_paths, clean=stage == "push")
    (reports / "sources.json").write_text(json.dumps({"workstream": registration["id"] if registration else None,
                                                      "sources": snapshot}, indent=2) + "\n", encoding="utf-8")
    results = []
    plan = check_plan(repo, paths, stage, root, output, source_paths,
                      registration["id"] if registration else None, paths_file)
    if repo.name == "emulebb-rust" and stage == "push" and any(
            not (path.endswith(".md") or path.startswith("docs/")) for path in paths):
        # WHY: the delegated Rust gate invokes Cargo directly. Fence its selected
        # checkout before the first Cargo freshness decision, under this owner.
        from .layout import load_layout
        from .workstreams import apply_workstream_layout
        from .rust_source_cache import ensure_rust_source_cache
        cargo_layout = load_layout(root, output_root=output)
        if registration:
            cargo_layout = apply_workstream_layout(cargo_layout, registration)
        ensure_rust_source_cache(cargo_layout)
    for index, (cwd, command) in enumerate(plan):
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
    if stage == "push":
        # WHY: a clean commit made during validation changes the checked source
        # without leaving a dirty tree. Its gates must run before it can push.
        if git(repo, "rev-parse", "HEAD") != checked_head:
            raise RuntimeError("HEAD changed during validation; validate the intended commit before pushing")
        if git(repo, "status", "--porcelain"):
            raise RuntimeError("Validation changed the worktree; review changes before pushing")
        if _source_snapshot(source_paths, clean=True) != snapshot:
            raise RuntimeError("Participating sources changed during validation; validate the intended revisions")
    print(f"Local checks passed; evidence: {reports}")
    print("Other OS builds, hosted CodeQL, permissions and publication still require GitHub.")
