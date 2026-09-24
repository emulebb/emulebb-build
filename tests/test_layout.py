from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from emule_workspace.layout import (
    AppVariant,
    TestTargets as LayoutTestTargets,
    WorkspaceLayout,
    _resolve_workspace_manifest_path,
    file_token,
    get_test_build_tag,
)
from emule_workspace.setup_commands import compare_root
from emule_workspace.topology import (
    WORKSPACE_MANIFEST_SCHEMA_VERSION,
    build_repo_role_manifest,
    build_workspace_manifest,
    canonical_topology,
    validate_workspace_manifest_contract,
)


def test_get_test_build_tag_matches_existing_harness_shape(tmp_path: Path) -> None:
    workspace_root = tmp_path / "owner" / "workspaces" / "workspace"
    app_root = workspace_root / "app" / "emulebb-main"

    assert get_test_build_tag(workspace_root, app_root) == "owner-workspace-emulebb-main"


def test_file_token_matches_legacy_filename_sanitization() -> None:
    assert file_token('repos\\emulebb-build-tests: bad/name') == "repos-emulebb-build-tests-bad-name"


def test_workspace_manifest_windows_paths_are_portable(tmp_path: Path) -> None:
    assert _resolve_workspace_manifest_path(tmp_path / "workspaces" / "workspace", "..\\..\\repos\\emulebb-rust") == (
        tmp_path / "repos" / "emulebb-rust"
    ).resolve()


def test_workspace_manifest_uses_json_contract_shape() -> None:
    manifest = build_workspace_manifest(canonical_topology(), "workspace")

    assert manifest["schema_version"] == WORKSPACE_MANIFEST_SCHEMA_VERSION
    assert manifest["workspace"]["repos"]["build"] == "..\\..\\repos\\emulebb-build"
    assert manifest["workspace"]["repos"]["ed2k_server"] == "..\\..\\repos\\goed2k-server"
    assert "amule" not in manifest["workspace"]["repos"]
    assert "amutorrent" not in manifest["workspace"]["repos"]
    assert "emuleai" not in manifest["workspace"]["repos"]
    assert manifest["workspace"]["repos"]["pages"] == "..\\..\\repos\\emulebb-pages"
    assert manifest["workspace"]["repos"]["org_profile"] == "..\\..\\repos\\emulebb-org-profile"
    assert manifest["workspace"]["repos"]["emulebb_rust"] == "..\\..\\repos\\emulebb-rust"
    assert "p2p_overlord_agents" not in manifest["workspace"]["repos"]
    assert "p2p_overlord_be" not in manifest["workspace"]["repos"]
    assert "p2p_overlord_tooling" not in manifest["workspace"]["repos"]
    assert manifest["workspace"]["app_repo"]["variants"][0] == {
        "name": "main",
        "path": "app\\emulebb-main",
        "branch": "main",
    }


def test_repo_role_manifest_describes_workspace_repositories() -> None:
    manifest = build_repo_role_manifest(canonical_topology(), "workspace")

    assert manifest["schema_version"] == 1
    assert manifest["workspace"]["name"] == "workspace"
    assert manifest["app_repo"]["name"] == "emulebb"
    assert manifest["app_repo"]["role"] == "app-seed"
    assert manifest["app_repo"]["worktrees"][0] == {
        "name": "main",
        "role": "app-worktree",
        "group": "app",
        "relative_path": "workspaces\\workspace\\app\\emulebb-main",
        "branch": "main",
        "active": True,
        "source_repo": "emulebb",
    }

    repos = {repo["name"]: repo for repo in manifest["repos"]}
    assert repos["emulebb-build"]["role"] == "workspace-orchestration"
    assert repos["emulebb-tooling"]["group"] == "workspace"
    assert repos["emulebb-rust"]["role"] == "headless-rust-client"
    assert repos["emulebb-rust"]["group"] == "product-family"
    assert repos["qbittorrentbb"]["role"] == "bittorrent-client"
    assert repos["qbittorrentbb"]["group"] == "product-family"
    assert repos["goed2k-server"]["role"] == "local-ed2k-test-server"
    assert "amule" not in repos
    assert "amutorrent" not in repos
    assert "p2p-overlord-be" not in repos

    analysis_repos = {repo["name"]: repo for repo in manifest["analysis_repos"]}
    assert analysis_repos == {}

    third_party_repos = {repo["name"]: repo for repo in manifest["third_party_repos"]}
    assert third_party_repos["emulebb-libtorrent"]["role"] == "third-party-dependency"
    assert third_party_repos["emulebb-libtorrent"]["has_submodules"] is True
    assert third_party_repos["emulebb-zlib"]["role"] == "third-party-dependency"
    assert "update_policy" in third_party_repos["emulebb-zlib"]


def test_canonical_topology_materializes_web_repositories_under_repos() -> None:
    repos = {repo.name: repo for repo in canonical_topology().repos}

    assert repos["emulebb-pages"].url == "https://github.com/emulebb/emulebb.github.io.git"
    assert repos["emulebb-pages"].relative_path == "repos\\emulebb-pages"
    assert repos["emulebb-org-profile"].url == "https://github.com/emulebb/.github.git"
    assert repos["emulebb-org-profile"].relative_path == "repos\\emulebb-org-profile"


def test_canonical_topology_materializes_ed2k_server_fork_under_repos() -> None:
    repos = {repo.name: repo for repo in canonical_topology().repos}

    ed2k_server = repos["goed2k-server"]
    assert ed2k_server.url == "https://github.com/emulebb/goed2k-server.git"
    assert ed2k_server.relative_path == "repos\\goed2k-server"
    assert ed2k_server.branch == "master"
    assert tuple((remote.name, remote.url) for remote in ed2k_server.additional_remotes) == (
        ("upstream", "https://github.com/chenjia404/goed2k-server.git"),
    )


def test_canonical_topology_keeps_analysis_references_optional() -> None:
    repos = {repo.name: repo for repo in canonical_topology().repos}
    analysis_repos = {repo.name: repo for repo in canonical_topology(include_analysis=True).analysis_repos}

    assert "emuleai" not in repos
    emuleai = analysis_repos["emuleai"]
    assert emuleai.url == "https://github.com/emulebb/emulebb-ai.git"
    assert emuleai.relative_path == "analysis\\emuleai"
    assert emuleai.branch == "master"
    assert emuleai.compare_subdir == "srchybrid"
    assert tuple((remote.name, remote.url) for remote in emuleai.additional_remotes) == (
        ("upstream", "https://github.com/eMuleAI/eMuleAI.git"),
    )


def test_canonical_topology_materializes_emulebb_rust_under_repos() -> None:
    repos = {repo.name: repo for repo in canonical_topology().repos}

    rust = repos["emulebb-rust"]
    assert rust.url == "https://github.com/emulebb/emulebb-rust.git"
    assert rust.relative_path == "repos\\emulebb-rust"
    assert rust.branch == "main"


def test_canonical_topology_does_not_materialize_retired_product_family_repos() -> None:
    repos = {repo.name: repo for repo in canonical_topology().repos}

    assert "amule" not in repos
    assert "amutorrent" not in repos
    assert "p2p-overlord-agents" not in repos
    assert "p2p-overlord-be" not in repos
    assert "p2p-overlord-tooling" not in repos
    assert "p2p-overlord-ed2k-server" not in repos


def test_compare_root_accepts_repo_targets_with_and_without_compare_subdirs(tmp_path: Path) -> None:
    topology = canonical_topology(include_analysis=True)

    assert compare_root(tmp_path, topology, "emuleai") == tmp_path / "analysis" / "emuleai" / "srchybrid"
    assert compare_root(tmp_path, topology, "mods-archive") == tmp_path / "analysis" / "mods-archive"


def test_workspace_manifest_schema_rejects_unsupported_versions() -> None:
    manifest = build_workspace_manifest(canonical_topology(), "workspace")
    manifest["schema_version"] = WORKSPACE_MANIFEST_SCHEMA_VERSION + 1

    with pytest.raises(ValidationError, match="unsupported workspace manifest schema_version"):
        validate_workspace_manifest_contract(manifest)


def test_get_app_variant_error_lists_keys_paths_and_branches(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces" / "workspace"
    layout = WorkspaceLayout(
        emule_workspace_root=tmp_path,
        workspace_name="workspace",
        workspace_root=workspace_root,
        build_repo_root=tmp_path / "repos" / "emulebb-build",
        tests_repo_root=tmp_path / "repos" / "emulebb-build-tests",
        tooling_repo_root=tmp_path / "repos" / "emulebb-tooling",
        ed2k_server_repo_root=tmp_path / "repos" / "goed2k-server",
        amule_repo_root=tmp_path / "repos" / "amule",
        emulebb_rust_repo_root=tmp_path / "repos" / "emulebb-rust",
        seed_repo_path=tmp_path / "repos" / "emulebb",
        seed_repo_branch="main",
        dependencies=(),
        app_variants=(
            AppVariant(name="main", path=workspace_root / "app" / "emulebb-main", branch="main"),
            AppVariant(
                name="community",
                path=workspace_root / "app" / "emulebb-community-baseline",
                branch="baseline/community-0.72a",
            ),
        ),
        test_targets=LayoutTestTargets(test_build_variant="main", test_run_variant="main", baseline_variant="community"),
        toolset_override_variable="",
    )

    with pytest.raises(RuntimeError) as exc_info:
        layout.get_app_variant("community-baseline")

    message = str(exc_info.value)
    assert "Use a configured variant key, not the worktree folder name." in message
    assert "main -> app\\emulebb-main (main)" in message
    assert "community -> app\\emulebb-community-baseline (baseline/community-0.72a)" in message


def test_build_log_directory_uses_output_root_when_configured(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces" / "workspace"
    output_root = tmp_path / "emulebb-output"
    layout = WorkspaceLayout(
        emule_workspace_root=tmp_path,
        workspace_name="workspace",
        workspace_root=workspace_root,
        build_repo_root=tmp_path / "repos" / "emulebb-build",
        tests_repo_root=tmp_path / "repos" / "emulebb-build-tests",
        tooling_repo_root=tmp_path / "repos" / "emulebb-tooling",
        ed2k_server_repo_root=tmp_path / "repos" / "goed2k-server",
        amule_repo_root=tmp_path / "repos" / "amule",
        emulebb_rust_repo_root=tmp_path / "repos" / "emulebb-rust",
        seed_repo_path=tmp_path / "repos" / "emulebb",
        seed_repo_branch="main",
        dependencies=(),
        app_variants=(),
        test_targets=LayoutTestTargets(test_build_variant="main", test_run_variant="main", baseline_variant="community"),
        toolset_override_variable="",
        output_root=output_root,
    )

    assert layout.build_log_directory("20260609T120000Z-build-app") == (
        output_root / "logs" / "builds" / "20260609T120000Z-build-app"
    )


def test_generated_output_roots_require_output_root(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces" / "workspace"
    layout = WorkspaceLayout(
        emule_workspace_root=tmp_path,
        workspace_name="workspace",
        workspace_root=workspace_root,
        build_repo_root=tmp_path / "repos" / "emulebb-build",
        tests_repo_root=tmp_path / "repos" / "emulebb-build-tests",
        tooling_repo_root=tmp_path / "repos" / "emulebb-tooling",
        ed2k_server_repo_root=tmp_path / "repos" / "goed2k-server",
        amule_repo_root=tmp_path / "repos" / "amule",
        emulebb_rust_repo_root=tmp_path / "repos" / "emulebb-rust",
        seed_repo_path=tmp_path / "repos" / "emulebb",
        seed_repo_branch="main",
        dependencies=(),
        app_variants=(),
        test_targets=LayoutTestTargets(test_build_variant="main", test_run_variant="main", baseline_variant="community"),
        toolset_override_variable="",
    )

    with pytest.raises(RuntimeError, match="output_root must be configured"):
        _ = layout.output_build_root


def test_layout_exposes_generated_output_subroots_and_child_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspaces" / "workspace"
    output_root = tmp_path.parent / f"{tmp_path.name}-output"
    layout = WorkspaceLayout(
        emule_workspace_root=tmp_path,
        workspace_name="workspace",
        workspace_root=workspace_root,
        build_repo_root=tmp_path / "repos" / "emulebb-build",
        tests_repo_root=tmp_path / "repos" / "emulebb-build-tests",
        tooling_repo_root=tmp_path / "repos" / "emulebb-tooling",
        ed2k_server_repo_root=tmp_path / "repos" / "goed2k-server",
        amule_repo_root=tmp_path / "repos" / "amule",
        emulebb_rust_repo_root=tmp_path / "repos" / "emulebb-rust",
        seed_repo_path=tmp_path / "repos" / "emulebb",
        seed_repo_branch="main",
        dependencies=(),
        app_variants=(),
        test_targets=LayoutTestTargets(test_build_variant="main", test_run_variant="main", baseline_variant="community"),
        toolset_override_variable="",
        output_root=output_root,
    )
    (output_root / "builds" / "rust" / "target").mkdir(parents=True)
    monkeypatch.setenv("EMULEBB_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("EMULEBB_WORKSPACE_OUTPUT_ROOT", str(output_root))
    monkeypatch.setenv("CARGO_TARGET_DIR", str(output_root / "builds" / "rust" / "target"))

    assert layout.output_third_party_build_root == output_root / "builds" / "third_party"
    assert layout.output_rust_target_root == output_root / "builds" / "rust" / "target"
    assert layout.subprocess_environment() == {
        "EMULEBB_WORKSPACE_ROOT": tmp_path,
        "EMULEBB_WORKSPACE_OUTPUT_ROOT": output_root,
        "CARGO_TARGET_DIR": output_root / "builds" / "rust" / "target",
    }
