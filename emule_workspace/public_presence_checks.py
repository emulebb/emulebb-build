"""Validate optional public sources against explicitly selected Tooling authority."""
from __future__ import annotations

import hashlib
from datetime import date
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from time import perf_counter
from uuid import uuid4
import xml.etree.ElementTree as ET

from .git_environment import git_environment
from .workstreams import OPTIONAL_REPOSITORY_FIELDS, _git, _safe_path


def _run(command: list[str], *, cwd: Path, environment: dict[str, str], capture: bool = False) -> str:
    """Use the exact clean child environment, including removal of hook bindings."""
    result = subprocess.run(command, cwd=cwd, env=environment, text=True, check=False,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    if result.returncode:
        raise RuntimeError(f"Public check failed with exit code {result.returncode}: {command[2]}")
    return result.stdout if capture else ""


def _capture(command: list[str], *, cwd: Path, environment: dict[str, str]) -> str:
    return _run(command, cwd=cwd, environment=environment, capture=True)


def public_presence_sources(layout, repository: str) -> dict[str, Path]:
    """Return only the content, authority and orchestration sources consumed."""
    if repository not in OPTIONAL_REPOSITORY_FIELDS:
        raise ValueError("Expected an optional public-presence repository")
    selected = getattr(layout, OPTIONAL_REPOSITORY_FIELDS[repository], None)
    if selected is None:
        raise RuntimeError("Public-presence repository is not admitted by workspace topology")
    sources = {repository: Path(selected), "emulebb-tooling": layout.tooling_repo_root,
               "emulebb-build": layout.build_repo_root}
    for name, path in sources.items():
        absolute = path.absolute()
        if not any(absolute.is_relative_to(layout.emule_workspace_root / boundary)
                   for boundary in ("repos", "workspaces")):
            raise RuntimeError(f"Public source escapes workspace boundaries: {name}")
        _safe_path(layout.emule_workspace_root, absolute)
        if not absolute.is_dir():
            raise RuntimeError(f"Missing public validation source: {name}")
    return sources


def _snapshot(sources: dict[str, Path]) -> dict:
    result = {}
    for name, path in sources.items():
        if Path(_git(path, "rev-parse", "--show-toplevel")).resolve() != path.resolve():
            raise RuntimeError(f"Public validation requires an exact repository root: {name}")
        if _git(path, "status", "--porcelain"):
            raise RuntimeError(f"Public validation requires clean source: {name}")
        result[name] = {"path": str(path), "revision": _git(path, "rev-parse", "HEAD")}
    return result


def _committed_date(repo: Path) -> str:
    sitemap = repo / "sitemap.xml"
    if sitemap.is_symlink() or not sitemap.is_file() or sitemap.stat().st_size > 1024 * 1024:
        raise RuntimeError("A bounded regular committed sitemap is required")
    root = ET.fromstring(sitemap.read_bytes())
    dates = {node.text for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "lastmod"}
    if len(dates) != 1 or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", next(iter(dates)) or "") is None:
        raise RuntimeError("Committed sitemap must contain one consistent ISO lastmod date")
    value = next(iter(dates))
    date.fromisoformat(value)
    return value


def _renderer_capabilities(repo: Path, environment: dict[str, str]) -> dict:
    text = _capture([sys.executable, "-B", str(repo / "tools/render_pages.py"), "--capabilities"],
                    cwd=repo, environment=environment)
    if len(text.encode("utf-8")) > 4096:
        raise RuntimeError("Pages renderer capability response exceeds its bound")
    value = json.loads(text)
    staging = value.get("outputStaging") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("schema") != "emulebb.pages.renderer-capabilities/1"
            or value.get("check") is not True or not isinstance(staging, dict)
            or type(staging.get("protocol")) is not int or staging.get("protocol") != 1
            or staging.get("boundary") != "workspace-output" or staging.get("sourceWrites") is not False):
        raise RuntimeError("Pages renderer lacks the required safe OUTPUT staging contract")
    return value


def public_presence_plan(sources: dict[str, Path], repository: str, staging: Path,
                         output_root: Path) -> list[list[str]]:
    """Build explicit persisted commands; never fall back to sibling sources."""
    repo, tooling = sources[repository], sources["emulebb-tooling"]
    python = [sys.executable, "-B"]
    contract = tooling / "manifests/public-presence.v1.json"
    authority = [*python, str(tooling / "helpers/public-presence.py"), "--contract", str(contract)]
    commands = [[*authority, "validate"]]
    if repository == "emulebb-pages":
        renderer = [*python, str(repo / "tools/render_pages.py"), "--root", str(repo),
                    "--contract", str(contract), "--lastmod", _committed_date(repo)]
        commands.extend([
            [*renderer, "--check"],
            # Old renderers reject this external path. Propagate that failure;
            # never substitute a source .local directory to make the gate pass.
            [*renderer, "--workspace-output-root", str(output_root), "--output-root", str(staging)],
            [*python, str(tooling / "helpers/pages-site-tools.py"), "--pages-root", str(staging), "validate"],
            [*python, str(tooling / "helpers/check-tooling-doclinks.py"),
             "--docs-root", str(tooling / "docs"), str(staging)],
        ])
    else:
        profile = _safe_path(repo, repo / "profile/README.md")
        if not profile.is_file() or profile.stat().st_size > 1024 * 1024:
            raise RuntimeError("A bounded regular organization profile is required")
        commands.extend([
            [*authority, "check-org-profile", "--input", str(repo / "profile/README.md")],
            [*python, str(tooling / "helpers/check-tooling-doclinks.py"),
             "--docs-root", str(tooling / "docs"), str(repo / "profile/README.md")],
        ])
    return commands


def _installer_checksum(staging: Path) -> None:
    checksum, installer = staging / "install.ps1.sha256", staging / "install.ps1"
    for path in (checksum, installer):
        _safe_path(staging, path)
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("Staged stable installer/checksum must be regular files")
    if checksum.stat().st_size > 4096:
        raise RuntimeError("Stable installer checksum file exceeds its bound")
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?install\.ps1\s*", checksum.read_text(encoding="utf-8"))
    if match is None:
        raise RuntimeError("Stable installer checksum must name only install.ps1")
    with installer.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != match[1].lower():
        raise RuntimeError("Stable installer SHA-256 mismatch")


def validate_public_presence(layout, *, repository: str) -> Path:
    """Run source-bound public checks with unique external staging and evidence."""
    sources = public_presence_sources(layout, repository)
    before = _snapshot(sources)
    output = layout.output_root.resolve()
    if output.is_relative_to(layout.emule_workspace_root.resolve()):
        raise RuntimeError("Public output must be outside the source workspace")
    run_id = uuid4().hex
    report_root = _safe_path(output, output / "reports/public-presence" / repository / run_id)
    staging = _safe_path(output, output / "artifacts/public-presence" / run_id / "site")
    scratch = _safe_path(output, output / "tmp/public-presence" / run_id)
    if report_root.exists() or staging.exists() or scratch.exists():
        raise RuntimeError("Public validation run path already exists")
    report_root.mkdir(parents=True)
    scratch.mkdir(parents=True)
    report_path = report_root / "results.json"
    report = {"schema": 1, "status": "running", "repository": repository, "sources": before,
              "staging": str(staging), "steps": []}
    environment = git_environment()
    environment.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch),
                       PYTHONPYCACHEPREFIX=str(output / "cache/pycache"))
    print(f"Public presence evidence: {report_path}", flush=True)
    try:
        layout.subprocess_environment()
        if repository == "emulebb-pages":
            report["rendererCapabilities"] = _renderer_capabilities(sources[repository], environment)
        staging.parent.mkdir(parents=True)
        for command in public_presence_plan(sources, repository, staging, layout.output_root):
            started = perf_counter()
            step = {"command": command, "status": "running"}
            report["steps"].append(step)
            try:
                _run(command, cwd=sources[repository], environment=environment)
                step["status"] = "passed"
            except Exception:
                step["status"] = "failed"
                raise
            finally:
                step["elapsed_seconds"] = perf_counter() - started
        if repository == "emulebb-pages":
            _safe_path(output, staging)
            _installer_checksum(staging)
        else:
            staging.mkdir(parents=True)
            shutil.copy2(sources[repository] / "profile/README.md", staging / "README.md")
        if _snapshot(sources) != before:
            raise RuntimeError("Public source identity changed during validation")
        report["status"] = "passed"
        return report_path
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
