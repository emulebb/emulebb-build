"""Product-family bootstrap and validation helpers."""

from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .layout import WorkspaceLayout
from .build import wsl_path
from .process import find_tool, run_captured, run_native

ProductFamilyValidationTier = Literal["quick", "quality", "full"]

_VERSION_PATTERN = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


@dataclass(frozen=True)
class ToolchainPolicy:
    """Version policy for one product-family runtime or build tool."""

    name: str
    command_names: tuple[str, ...]
    version_args: tuple[str, ...]
    minimum_version: tuple[int, ...] | None = None
    allowed_majors: tuple[int, ...] | None = None
    preferred_major: int | None = None
    note: str = ""

    @property
    def policy_text(self) -> str:
        parts: list[str] = []
        if self.minimum_version is not None:
            parts.append(f">= {_format_version(self.minimum_version)}")
        if self.allowed_majors is not None:
            parts.append("major in {" + ", ".join(str(value) for value in self.allowed_majors) + "}")
        if self.preferred_major is not None:
            parts.append(f"preferred major {self.preferred_major}")
        return "; ".join(parts) if parts else "present on PATH"


@dataclass(frozen=True)
class ToolchainCheck:
    """One resolved product-family runtime/tool version check."""

    name: str
    command: str
    raw_version: str
    parsed_version: tuple[int, ...] | None
    policy: str
    status: Literal["ok", "warning", "missing"]
    note: str


@dataclass(frozen=True)
class ProductFamilyRebaseRepo:
    """One fork whose remote branch is maintained by upstream-rebase automation."""

    name: str
    path: Path
    branch: str
    remote: str = "origin"


@dataclass(frozen=True)
class ProductFamilyRebaseRefresh:
    """Result of refreshing one local fork clone from its rebased remote branch."""

    name: str
    branch: str
    path: Path
    old_remote_head: str
    new_remote_head: str
    old_local_head: str
    status: Literal["unchanged", "refreshed"]
    message: str


TOOLCHAIN_POLICIES: tuple[ToolchainPolicy, ...] = (
    ToolchainPolicy(
        name="python",
        command_names=(sys.executable,),
        version_args=(),
        minimum_version=(3, 11),
        note="Workspace orchestration runtime.",
    ),
    ToolchainPolicy(
        name="node",
        command_names=("node.exe", "node"),
        version_args=("--version",),
        allowed_majors=(20, 22, 24),
        preferred_major=24,
        note="p2p-overlord coordinator and Svelte/Prisma runtime.",
    ),
    ToolchainPolicy(
        name="npm",
        command_names=("npm.cmd", "npm.exe", "npm"),
        version_args=("--version",),
        minimum_version=(10, 0),
        note="Node package manager for coordinator dependency installs.",
    ),
    ToolchainPolicy(
        name="rustc",
        command_names=("rustc.exe", "rustc"),
        version_args=("--version",),
        minimum_version=(1, 85),
        note="Rust 2024 edition support for p2p-overlord agents.",
    ),
    ToolchainPolicy(
        name="cargo",
        command_names=("cargo.exe", "cargo"),
        version_args=("--version",),
        minimum_version=(1, 85),
        note="Rust workspace build, test, and dependency fetch tool.",
    ),
    ToolchainPolicy(
        name="go",
        command_names=("go.exe", "go"),
        version_args=("version",),
        minimum_version=(1, 25),
        note="goed2k-server module declares Go 1.25.x.",
    ),
)


def refresh_product_family_rebases(layout: WorkspaceLayout) -> list[ProductFamilyRebaseRefresh]:
    """Refreshes local product-family fork clones after GitHub upstream rebases.

    The refresh is intentionally narrower than a general `git pull`: the local
    branch must be clean and exactly at the pre-fetch remote-tracking ref before
    the command will reset it to the freshly fetched remote branch.
    """

    if layout.amule_repo_root is None:
        raise RuntimeError(
            "refresh-product-family-rebases requires manual repos/amule and repos/amutorrent checkouts; "
            "they are no longer materialized by workspace setup."
        )
    repos = (
        ProductFamilyRebaseRepo("amule", layout.amule_repo_root, "master"),
        ProductFamilyRebaseRepo("amutorrent", layout.resolve_workspace_path("repos/amutorrent"), "main"),
    )
    results: list[ProductFamilyRebaseRefresh] = []
    failures: list[str] = []
    for repo in repos:
        try:
            results.append(_refresh_rebased_repo(repo))
        except Exception as exc:
            failures.append(f"{repo.name}: {exc}")
    for result in results:
        print(f"{result.name:<10} {result.status:<9} {result.branch:<6} {result.message}")
    if failures:
        for failure in failures:
            print(f"SKIPPED {failure}")
        raise RuntimeError("Product-family rebase refresh skipped one or more repositories; review messages above.")
    return results


def prepare_product_family_repos(layout: WorkspaceLayout) -> None:
    """Fetches or installs repo-native dependencies for optional product-family repos."""

    cargo: str | None = None
    for label, root in _rust_product_family_roots(layout):
        if root is None or not (root / "Cargo.toml").is_file():
            continue
        if cargo is None:
            cargo = _required_product_family_command(("cargo.exe", "cargo"), "Rust cargo")
        run_native(
            [cargo, "fetch"],
            label=f"{label} cargo fetch",
            cwd=root,
        )

    ed2k_index_root = getattr(layout, "ed2k_index_server_repo_root", None)
    if ed2k_index_root is not None and (ed2k_index_root / "Cargo.toml").is_file():
        _run_ed2k_server_cargo(layout, ("fetch", "--locked"), label="ed2k-server cargo fetch")

    coordinator_root = _coordinator_root(layout)
    if coordinator_root is not None and (coordinator_root / "package-lock.json").is_file():
        npm = _required_product_family_command(("npm.cmd", "npm.exe", "npm"), "Node npm")
        run_native([npm, "ci"], label="p2p-overlord-be coordinator npm ci", cwd=coordinator_root)
        run_native(
            [npm, "run", "prisma:generate"],
            label="p2p-overlord-be coordinator prisma generate",
            cwd=coordinator_root,
        )

    goed2k_root = layout.ed2k_server_repo_root
    if goed2k_root.is_dir() and (goed2k_root / "go.mod").is_file():
        run_native(
            [_required_product_family_command(("go.exe", "go"), "Go toolchain"), "mod", "download"],
            label="goed2k-server go mod download",
            cwd=goed2k_root,
        )


def validate_product_family_repos(layout: WorkspaceLayout, *, tier: ProductFamilyValidationTier = "quality") -> None:
    """Runs repo-native checks for non-app product-family repositories."""

    if tier not in ("quick", "quality", "full"):
        raise ValueError(f"Unsupported product-family validation tier: {tier}")

    cargo: str | None = None
    for label, root in _rust_product_family_roots(layout):
        if root is None or not root.is_dir():
            continue
        if cargo is None:
            cargo = _required_product_family_command(("cargo.exe", "cargo"), "Rust cargo")
        run_native(
            [cargo, "fmt", "--all", "--check"],
            label=f"{label} cargo fmt",
            cwd=root,
        )
        if tier == "full":
            run_native(
                [
                    cargo,
                    "clippy",
                    "--workspace",
                    "--all-targets",
                    "--all-features",
                    "--",
                    "-D",
                    "warnings",
                    "-W",
                    "clippy::all",
                    "-W",
                    "clippy::too_many_arguments",
                    "-W",
                    "clippy::type_complexity",
                    "-W",
                    "clippy::cognitive_complexity",
                ],
                label=f"{label} cargo clippy",
                cwd=root,
            )
            run_native(
                [cargo, "test", "--workspace", "--all-targets", "--all-features"],
                label=f"{label} cargo test",
                cwd=root,
            )

    ed2k_index_root = getattr(layout, "ed2k_index_server_repo_root", None)
    if ed2k_index_root is not None and ed2k_index_root.is_dir():
        _run_ed2k_server_cargo(
            layout,
            ("fmt", "--all", "--check"),
            label="ed2k-server cargo fmt",
        )
        if tier in ("quality", "full"):
            _run_ed2k_server_cargo(
                layout,
                ("test", "--locked", "--all-targets"),
                label="ed2k-server cargo test",
            )
        if tier == "full":
            _run_ed2k_server_cargo(
                layout,
                ("clippy", "--locked", "--all-targets", "--all-features"),
                label="ed2k-server cargo clippy",
            )
            _run_ed2k_server_cargo(
                layout,
                ("build", "--locked", "--release"),
                label="ed2k-server cargo release build",
            )

    coordinator_root = _coordinator_root(layout)
    if coordinator_root is not None and coordinator_root.is_dir():
        npm = _required_product_family_command(("npm.cmd", "npm.exe", "npm"), "Node npm")
        script = "prisma:validate" if tier == "quick" else "quality"
        run_native(
            [npm, "run", script],
            label=f"p2p-overlord-be coordinator {script}",
            cwd=coordinator_root,
        )

    goed2k_root = layout.ed2k_server_repo_root
    if goed2k_root.is_dir():
        run_native(
            [_required_product_family_command(("go.exe", "go"), "Go toolchain"), "test", "./..."],
            label="goed2k-server go test",
            cwd=goed2k_root,
        )


def audit_product_family_toolchain(*, strict: bool = False) -> dict[str, Any]:
    """Returns the product-family runtime/tool version policy and current status."""

    checks = [_toolchain_check(policy) for policy in TOOLCHAIN_POLICIES]
    payload = {
        "schema_version": 1,
        "checks": [_toolchain_check_payload(check) for check in checks],
        "totals": {
            "ok": sum(1 for check in checks if check.status == "ok"),
            "warning": sum(1 for check in checks if check.status == "warning"),
            "missing": sum(1 for check in checks if check.status == "missing"),
        },
    }
    if strict:
        bad = [check for check in checks if check.status != "ok"]
        if bad:
            names = ", ".join(check.name for check in bad)
            raise RuntimeError(f"Product-family toolchain policy failed: {names}.")
    return payload


def print_product_family_toolchain(payload: dict[str, Any]) -> None:
    """Prints a concise product-family runtime/tool version report."""

    totals = payload["totals"]
    print(
        "Product-family toolchain: "
        f"{totals['ok']} ok, {totals['warning']} warning, {totals['missing']} missing"
    )
    for check in payload["checks"]:
        print(f"  {check['status']:<7} {check['name']:<7} {check['raw_version'] or check['command']}")
        print(f"          policy: {check['policy']}")
        if check["note"]:
            print(f"          note: {check['note']}")


def _coordinator_root(layout: WorkspaceLayout) -> Path | None:
    be_root = layout.p2p_overlord_be_repo_root
    return be_root / "overlord-be-coordinator" if be_root is not None else None


def _rust_product_family_roots(layout: WorkspaceLayout) -> tuple[tuple[str, Path | None], ...]:
    return (
        ("emulebb-rust", getattr(layout, "emulebb_rust_repo_root", None)),
        ("p2p-overlord-agents", getattr(layout, "p2p_overlord_agents_repo_root", None)),
    )


def _run_ed2k_server_cargo(
    layout: WorkspaceLayout,
    arguments: tuple[str, ...],
    *,
    label: str,
) -> None:
    """Run one Linux-first ed2k-server Cargo command through the owned boundary."""

    repo_root = getattr(layout, "ed2k_index_server_repo_root", None)
    if repo_root is None:
        raise RuntimeError(f"{label} requires repos/ed2k-server in the workspace manifest.")
    env = {name: str(value) for name, value in layout.subprocess_environment().items()}
    if os.name != "nt":
        cargo = _required_product_family_command(("cargo",), "Rust cargo")
        run_native([cargo, *arguments], label=label, cwd=repo_root, env=env)
        return

    target_root = layout.output_rust_target_root.parent / "target-wsl"
    wsl_workspace = wsl_path(layout.emule_workspace_root)
    wsl_output = wsl_path(layout.output_root)
    wsl_target = wsl_path(target_root)
    wsl_repo = wsl_path(repo_root)
    command = [
        "wsl.exe",
        "--",
        "bash",
        "-lc",
        " && ".join(
            (
                f"export EMULEBB_WORKSPACE_ROOT={shlex.quote(wsl_workspace)}",
                f"export EMULEBB_WORKSPACE_OUTPUT_ROOT={shlex.quote(wsl_output)}",
                f"export CARGO_TARGET_DIR={shlex.quote(wsl_target)}",
                f"cd {shlex.quote(wsl_repo)}",
                f"exec cargo {shlex.join(arguments)}",
            )
        ),
    ]
    run_native(command, label=label, cwd=repo_root, env=env)


def _refresh_rebased_repo(repo: ProductFamilyRebaseRepo) -> ProductFamilyRebaseRefresh:
    if not repo.path.is_dir():
        raise RuntimeError(f"repository is missing: {repo.path}")
    _require_clean_expected_branch(repo)
    local_head = _git(repo.path, "rev-parse", "HEAD")
    old_remote_head = _git(repo.path, "rev-parse", "--verify", f"refs/remotes/{repo.remote}/{repo.branch}")
    run_native(["git", "-C", repo.path, "fetch", repo.remote, "--prune"], label=f"fetch {repo.name}", cwd=repo.path)
    new_remote_head = _git(repo.path, "rev-parse", "--verify", f"refs/remotes/{repo.remote}/{repo.branch}")
    if local_head == new_remote_head:
        return ProductFamilyRebaseRefresh(
            repo.name,
            repo.branch,
            repo.path,
            old_remote_head,
            new_remote_head,
            local_head,
            "unchanged",
            f"already at {new_remote_head[:12]}",
        )
    if local_head != old_remote_head:
        raise RuntimeError(
            f"local HEAD {local_head[:12]} is not the pre-fetch {repo.remote}/{repo.branch} "
            f"{old_remote_head[:12]}; refusing to reset local work"
        )
    run_native(
        ["git", "-C", repo.path, "reset", "--hard", f"refs/remotes/{repo.remote}/{repo.branch}"],
        label=f"reset {repo.name}",
        cwd=repo.path,
    )
    return ProductFamilyRebaseRefresh(
        repo.name,
        repo.branch,
        repo.path,
        old_remote_head,
        new_remote_head,
        local_head,
        "refreshed",
        f"{old_remote_head[:12]} -> {new_remote_head[:12]}",
    )


def _require_clean_expected_branch(repo: ProductFamilyRebaseRepo) -> None:
    branch = _git(repo.path, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != repo.branch:
        raise RuntimeError(f"checkout is on branch '{branch}', expected '{repo.branch}'")
    status = _git(repo.path, "status", "--short")
    if status:
        raise RuntimeError("worktree has local changes")


def _git(repo_root: Path, *args: str) -> str:
    return run_captured(["git", "-C", repo_root, *args], label=f"git {' '.join(args)}", cwd=repo_root).strip()


def _required_product_family_command(names: tuple[str, ...], label: str) -> str:
    for name in names:
        if shutil.which(name):
            return name
    if find_tool(names) is None:
        raise RuntimeError(f"{label} was not found on PATH for product-family operations.")
    return names[0]


def _toolchain_check(policy: ToolchainPolicy) -> ToolchainCheck:
    command = _resolve_toolchain_command(policy.command_names)
    if command is None:
        return ToolchainCheck(
            name=policy.name,
            command=policy.command_names[0],
            raw_version="",
            parsed_version=None,
            policy=policy.policy_text,
            status="missing",
            note=f"{policy.note} Tool was not found on PATH.",
        )

    if policy.name == "python":
        raw_version = platform.python_version()
    else:
        raw_version = run_captured(
            [command, *policy.version_args],
            label=f"{policy.name} version",
            cwd=Path.cwd(),
        ).strip()
    parsed_version = _parse_version(raw_version)
    status, status_note = _evaluate_toolchain_policy(policy, parsed_version)
    note = policy.note
    if status_note:
        note = f"{note} {status_note}" if note else status_note
    return ToolchainCheck(
        name=policy.name,
        command=command,
        raw_version=raw_version,
        parsed_version=parsed_version,
        policy=policy.policy_text,
        status=status,
        note=note,
    )


def _resolve_toolchain_command(names: tuple[str, ...]) -> str | None:
    if len(names) == 1 and Path(names[0]).exists():
        return names[0]
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            return name
    resolved_path = find_tool(names)
    return str(resolved_path) if resolved_path is not None else None


def _evaluate_toolchain_policy(
    policy: ToolchainPolicy,
    parsed_version: tuple[int, ...] | None,
) -> tuple[Literal["ok", "warning"], str]:
    if parsed_version is None:
        return "warning", "Version output could not be parsed."
    if policy.minimum_version is not None and _compare_versions(parsed_version, policy.minimum_version) < 0:
        return "warning", f"Detected {_format_version(parsed_version)} is below the policy minimum."
    if policy.allowed_majors is not None and parsed_version[0] not in policy.allowed_majors:
        return "warning", f"Detected major {parsed_version[0]} is outside the supported set."
    if policy.preferred_major is not None and parsed_version[0] != policy.preferred_major:
        return "warning", f"Release packaging should use major {policy.preferred_major}."
    return "ok", ""


def _toolchain_check_payload(check: ToolchainCheck) -> dict[str, Any]:
    return {
        "name": check.name,
        "command": check.command,
        "raw_version": check.raw_version,
        "parsed_version": list(check.parsed_version) if check.parsed_version is not None else None,
        "policy": check.policy,
        "status": check.status,
        "note": check.note,
    }


def _parse_version(raw_version: str) -> tuple[int, ...] | None:
    match = _VERSION_PATTERN.search(raw_version)
    if match is None:
        return None
    parts = [int(part) for part in match.groups(default="0")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _compare_versions(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    width = max(len(left), len(right))
    padded_left = left + (0,) * (width - len(left))
    padded_right = right + (0,) * (width - len(right))
    return (padded_left > padded_right) - (padded_left < padded_right)


def _format_version(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)
