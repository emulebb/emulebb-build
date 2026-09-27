"""Workspace validation commands and policy-audit routing."""

from __future__ import annotations

from pathlib import Path

from .artifact_audit import audit_workspace_artifacts, print_artifact_audit
from .git import git_output, repo_branch, repo_status_lines, test_app_branch_allowed
from .layout import WorkspaceLayout
from .materialize import ROOT_AGENTS_CONTENT
from .process import find_tool, get_python_invocation, run_native
from .product_family import ProductFamilyValidationTier, validate_product_family_repos
from .setup_commands import compare_presets, compare_root
from .toolchain import get_visual_studio_info
from .topology import (
    SHARED_HOOK_REPO_NAMES,
    REPO_ROLE_MANIFEST_NAME,
    WORKSPACE_MANIFEST_NAME,
    WORKSPACE_PROPS_FILE_NAME,
    build_repo_role_manifest,
    build_workspace_manifest,
    canonical_topology,
    load_json,
    validate_workspace_manifest_contract,
)


def env_check(layout: WorkspaceLayout) -> None:
    """Validates basic command-line tool discovery."""

    vs = get_visual_studio_info()
    if vs is None or not vs.msbuild.is_file():
        raise RuntimeError("Visual Studio 2022 with MSBuild is required.")
    if find_tool(("git.exe", "git")) is None:
        raise RuntimeError("git not found on PATH.")
    print(f"Visual Studio: {vs.root}")
    print(f"MSBuild: {vs.msbuild}")
    print(f"Python workspace CLI: {layout.build_repo_root}")
    print(f"Toolset override variable: {layout.toolset_override_variable}")


def assert_required_workspace_paths(layout: WorkspaceLayout) -> None:
    """Checks that the canonical workspace paths needed by orchestration exist."""

    required_paths = [
        layout.emule_workspace_root,
        layout.workspace_root,
        layout.workspace_root / WORKSPACE_MANIFEST_NAME,
        layout.workspace_root / REPO_ROLE_MANIFEST_NAME,
        layout.emule_workspace_root / WORKSPACE_PROPS_FILE_NAME,
        layout.emule_workspace_root / "AGENTS.md",
        layout.seed_repo_path,
        *(layout.emule_workspace_root / repo.relative_path for repo in canonical_topology().repos),
        layout.tests_repo_root,
        layout.tooling_repo_root,
        *(layout.resolve_workspace_path(dependency.path) for dependency in layout.dependencies),
        *(variant.path for variant in layout.app_variants),
    ]
    missing = sorted({path for path in required_paths if not path.exists()})
    if missing:
        details = "\n".join(str(path) for path in missing)
        raise RuntimeError(f"Missing required workspace paths:\n{details}")


def assert_generated_workspace_manifest(layout: WorkspaceLayout) -> None:
    """Checks that the generated workspace manifest matches Python topology."""

    topology = canonical_topology()
    manifest_path = layout.workspace_root / WORKSPACE_MANIFEST_NAME
    actual = load_json(manifest_path)
    validate_workspace_manifest_contract(actual)
    expected = build_workspace_manifest(topology, layout.workspace_name)
    if actual != expected:
        raise RuntimeError(f"Workspace manifest drifted from Python topology: {manifest_path}. Run sync to regenerate it.")


def assert_generated_repo_role_manifest(layout: WorkspaceLayout) -> None:
    """Checks that the generated repository role manifest matches Python topology."""

    manifest_path = layout.workspace_root / REPO_ROLE_MANIFEST_NAME
    actual = load_json(manifest_path)
    topology = canonical_topology(include_analysis=bool(actual.get("analysis_repos")))
    expected = build_repo_role_manifest(topology, layout.workspace_name)
    if actual != expected:
        raise RuntimeError(f"Repository role manifest drifted from Python topology: {manifest_path}. Run sync to regenerate it.")


def assert_root_agents_file(layout: WorkspaceLayout) -> None:
    """Checks that root AGENTS.md matches the workspace bootstrap contract."""

    agents_path = layout.emule_workspace_root / "AGENTS.md"
    actual = agents_path.read_text(encoding="ascii").strip()
    expected = ROOT_AGENTS_CONTENT.strip()
    if actual != expected:
        raise RuntimeError(f"Workspace root AGENTS.md drifted from Python-owned content: {agents_path}. Run sync.")


def assert_app_layout(layout: WorkspaceLayout) -> None:
    """Checks that configured app worktrees exist and match their expected branches."""

    missing = [variant.path for variant in layout.app_variants if not variant.path.exists()]
    if missing:
        raise RuntimeError("Missing app worktrees:\n" + "\n".join(str(path) for path in missing))
    for variant in layout.app_variants:
        current_branch = repo_branch(variant.path)
        if not test_app_branch_allowed(variant.branch, current_branch):
            raise RuntimeError(
                f"App checkout '{variant.path}' is on branch '{current_branch}', "
                f"expected '{variant.branch}'."
            )


def assert_compare_targets(layout: WorkspaceLayout) -> None:
    """Checks that generated comparison presets resolve to existing paths."""

    topology = canonical_topology()
    missing: list[str] = []
    checked: set[Path] = set()
    for preset in compare_presets(layout.emule_workspace_root, topology):
        for name in (preset.left_name, preset.right_name):
            path = compare_root(layout.emule_workspace_root, topology, name)
            if path in checked:
                continue
            checked.add(path)
            if not path.exists():
                missing.append(f"{name}: {path}")
    if missing:
        raise RuntimeError("Missing compare targets:\n" + "\n".join(sorted(missing)))


def assert_workspace_hooks_installed(layout: WorkspaceLayout) -> None:
    """Checks that editable workspace repos use the shared hook path."""

    topology = canonical_topology()
    expected_hooks_path = (layout.emule_workspace_root / "repos" / "emulebb-tooling" / "hooks").resolve()
    if not (expected_hooks_path / "pre-commit").is_file():
        raise RuntimeError(f"Shared pre-commit hook is missing: {expected_hooks_path / 'pre-commit'}")
    targets = [layout.emule_workspace_root / repo.relative_path for repo in topology.repos if repo.name in SHARED_HOOK_REPO_NAMES]
    targets.extend(layout.emule_workspace_root / worktree.relative_path for worktree in topology.app_repo.worktrees if worktree.active)
    for target in targets:
        configured = run_native(
            ["git", "-C", target, "config", "--local", "--get", "core.hooksPath"],
            label=f"read hooks path {target}",
            cwd=layout.emule_workspace_root,
            allow_failure=True,
        )
        if configured.returncode != 0:
            raise RuntimeError(f"Hook path drift detected for '{target}'. Expected core.hooksPath '{expected_hooks_path}'.")
        hooks_path = _git_config_value(target, "core.hooksPath")
        resolved_hooks_path = (target / hooks_path).resolve() if not Path(hooks_path).is_absolute() else Path(hooks_path).resolve()
        if resolved_hooks_path != expected_hooks_path:
            raise RuntimeError(f"Hook path drift detected for '{target}'. Expected '{expected_hooks_path}'.")
        autocrlf = _git_config_value(target, "core.autocrlf")
        if autocrlf != "false":
            raise RuntimeError(f"Line-ending config drift detected for '{target}'. Expected core.autocrlf false.")


def assert_required_test_helpers(layout: WorkspaceLayout) -> None:
    """Checks that maintained shared test helper entrypoints are present."""

    helpers = (
        "scripts/build-emule-tests.py",
        "scripts/run-native-coverage.py",
        "scripts/run-live-diff.py",
        "scripts/run-protocol-surface-diff.py",
        "scripts/validate-protocol-goldens.py",
        "scripts/stock-protocol-oracle-proof.py",
        "scripts/run-community-core-coverage.py",
        "scripts/run-live-e2e-suite.py",
        "scripts/fake-kad-trust-soak.py",
        "scripts/amutorrent-interactive-session.py",
        "scripts/amutorrent-resilience-live.py",
        "scripts/amutorrent-emulebb-ui-live.py",
    )
    for relative_path in helpers:
        path = layout.tests_repo_root / Path(relative_path)
        if not path.is_file():
            raise RuntimeError(f"Missing required test helper: {path}")


def assert_no_workspace_generated_artifacts(layout: WorkspaceLayout) -> None:
    """Fails when generated outputs are present under repos or workspaces."""

    findings = audit_workspace_artifacts(layout)
    print_artifact_audit(layout, findings)
    if findings:
        raise RuntimeError("Generated artifacts found under repos or workspaces. Move them under EMULEBB_WORKSPACE_OUTPUT_ROOT.")


def ensure_canonical_app_anchor(layout: WorkspaceLayout) -> None:
    """Ensures the canonical app anchor is clean and detached at origin/main."""

    if not layout.seed_repo_path.is_dir():
        raise RuntimeError(f"Canonical app repo is missing: {layout.seed_repo_path}")
    status_lines = repo_status_lines(layout.seed_repo_path)
    if len(status_lines) > 1:
        raise RuntimeError(
            "Canonical app repo has local changes and cannot be re-anchored automatically: "
            f"{layout.seed_repo_path}"
        )
    expected_revision = f"refs/remotes/origin/{layout.seed_repo_branch}"
    expected_head = git_output(layout.seed_repo_path, "rev-parse", expected_revision).strip()
    current_branch = repo_branch(layout.seed_repo_path)
    current_head = git_output(layout.seed_repo_path, "rev-parse", "HEAD").strip()
    if current_branch == "HEAD" and current_head == expected_head:
        return
    print(f"Reanchoring canonical app repo to detached origin/{layout.seed_repo_branch} at {expected_head}")
    git_output(layout.seed_repo_path, "checkout", "--detach", expected_revision)


def run_policy_audits(layout: WorkspaceLayout) -> None:
    """Runs the centralized workspace policy audits owned by emulebb-tooling."""

    audit_names = (
        ("build policy audit", "build-policy"),
        ("branch policy audit", "branch-policy"),
        ("dependency pin audit", "dependency-pins"),
        ("documentation path audit", "doc-paths"),
        ("editorconfig policy audit", "editorconfig-policy"),
        ("EMULEBB_* env override audit", "emulebb-env-override"),
        ("English-only language audit", "language-policy"),
        ("localization policy audit", "localization-policy"),
        ("output-root build redirection audit", "output-root"),
        ("PowerShell boundary audit", "powershell-boundary"),
        ("project entrypoint audit", "project-entrypoints"),
        ("warning policy audit", "warning-policy"),
    )
    audit_path = layout.tooling_repo_root / "ci" / "check-workspace-policy.py"
    if not audit_path.is_file():
        raise RuntimeError(f"Missing required policy audit: {audit_path}")
    python = get_python_invocation()
    for label, audit_name in audit_names:
        run_native(
            python.command([audit_path, audit_name]),
            label=label,
            cwd=layout.emule_workspace_root,
            env=layout.subprocess_environment(),
        )


def validate_workspace(
    layout: WorkspaceLayout,
    *,
    include_product_family: bool = False,
    product_family_tier: ProductFamilyValidationTier = "quality",
) -> None:
    """Runs the first Python-native workspace validation pass."""

    env_check(layout)
    assert_required_workspace_paths(layout)
    assert_generated_workspace_manifest(layout)
    assert_generated_repo_role_manifest(layout)
    assert_root_agents_file(layout)
    assert_app_layout(layout)
    assert_compare_targets(layout)
    assert_workspace_hooks_installed(layout)
    ensure_canonical_app_anchor(layout)
    run_policy_audits(layout)
    assert_required_test_helpers(layout)
    assert_no_workspace_generated_artifacts(layout)
    if include_product_family:
        validate_product_family_repos(layout, tier=product_family_tier)
    print("Workspace validation passed.")


def _git_config_value(repo_root: Path, key: str) -> str:
    from .process import run_captured

    return run_captured(["git", "-C", repo_root, "config", "--local", "--get", key], label=f"read {key}", cwd=repo_root).strip()
