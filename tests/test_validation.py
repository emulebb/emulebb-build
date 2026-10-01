from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from emule_workspace import validation
from emule_workspace import product_family
from emule_workspace.topology import ManagedRepo


def test_policy_audits_receive_workspace_root_through_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tooling_root = tmp_path / "repos" / "emulebb-tooling"
    audit_path = tooling_root / "ci" / "check-workspace-policy.py"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    calls: list[dict[str, object]] = []

    def fake_run_native(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(validation, "run_native", fake_run_native)
    output_root = tmp_path.parent / f"{tmp_path.name}-output"
    layout = SimpleNamespace(
        emule_workspace_root=tmp_path,
        output_build_root=output_root / "builds",
        tooling_repo_root=tooling_root,
        subprocess_environment=lambda: {
            "EMULEBB_WORKSPACE_ROOT": tmp_path,
            "EMULEBB_WORKSPACE_OUTPUT_ROOT": output_root,
        },
    )

    validation.run_policy_audits(layout)

    assert calls
    for call in calls:
        assert "-EmuleWorkspaceRoot" not in call["command"]
        assert "pwsh" not in call["command"]
        assert call["command"][-2] == str(audit_path)
        assert call["env"] == {
            "EMULEBB_WORKSPACE_ROOT": tmp_path,
            "EMULEBB_WORKSPACE_OUTPUT_ROOT": output_root,
        }


def test_validation_reanchors_clean_canonical_app_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_repo = tmp_path / "repos" / "emulebb"
    canonical_repo.mkdir(parents=True)
    calls: list[tuple[str, ...]] = []

    def fake_git_output(repo: Path, *args: str) -> str:
        assert repo == canonical_repo
        calls.append(args)
        if args == ("rev-parse", "refs/remotes/origin/main"):
            return "expected-head\n"
        if args == ("rev-parse", "HEAD"):
            return "stale-head\n"
        if args == ("checkout", "--detach", "refs/remotes/origin/main"):
            return ""
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(validation, "repo_status_lines", lambda repo: ["## HEAD (no branch)"])
    monkeypatch.setattr(validation, "repo_branch", lambda repo: "HEAD")
    monkeypatch.setattr(validation, "git_output", fake_git_output)
    layout = SimpleNamespace(seed_repo_path=canonical_repo, seed_repo_branch="main")

    validation.ensure_canonical_app_anchor(layout)

    assert calls[-1] == ("checkout", "--detach", "refs/remotes/origin/main")


def test_validation_refuses_dirty_canonical_app_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_repo = tmp_path / "repos" / "emulebb"
    canonical_repo.mkdir(parents=True)
    calls: list[tuple[str, ...]] = []

    def fake_git_output(repo: Path, *args: str) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(validation, "repo_status_lines", lambda repo: ["## HEAD (no branch)", " M srchybrid/emule.rc"])
    monkeypatch.setattr(validation, "git_output", fake_git_output)
    layout = SimpleNamespace(seed_repo_path=canonical_repo, seed_repo_branch="main")

    with pytest.raises(RuntimeError, match="local changes"):
        validation.ensure_canonical_app_anchor(layout)

    assert calls == []


def test_required_workspace_paths_include_topology_managed_repos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspaces" / "workspace"
    for path in (
        tmp_path / "AGENTS.md",
        tmp_path / "workspace.props",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    for path in (
        tmp_path / "repos" / "emulebb",
        tmp_path / "repos" / "emulebb-build-tests",
        tmp_path / "repos" / "emulebb-tooling",
        tmp_path / "repos" / "emulebb-build",
        tmp_path / "analysis" / "compare",
        workspace_root,
    ):
        path.mkdir(parents=True, exist_ok=True)
    (workspace_root / "deps.json").write_text("{}\n", encoding="utf-8")
    (workspace_root / "repo-roles.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        validation,
        "canonical_topology",
        lambda: SimpleNamespace(
            repos=(
                ManagedRepo(
                    name="emulebb-build",
                    url="https://example.invalid/build.git",
                    relative_path="repos\\emulebb-build",
                    branch="main",
                ),
                ManagedRepo(
                    name="emulebb-pages",
                    url="https://example.invalid/pages.git",
                    relative_path="repos\\emulebb-pages",
                    branch="main",
                ),
            )
        ),
    )
    layout = SimpleNamespace(
        emule_workspace_root=tmp_path,
        workspace_root=workspace_root,
        seed_repo_path=tmp_path / "repos" / "emulebb",
        tests_repo_root=tmp_path / "repos" / "emulebb-build-tests",
        tooling_repo_root=tmp_path / "repos" / "emulebb-tooling",
        dependencies=(),
        app_variants=(),
        resolve_workspace_path=lambda relative_path: tmp_path / relative_path,
    )

    with pytest.raises(RuntimeError, match="emulebb-pages"):
        validation.assert_required_workspace_paths(layout)


def test_validation_artifact_audit_accepts_clean_workspace(tmp_path: Path) -> None:
    (tmp_path / "repos" / "amutorrent" / "server").mkdir(parents=True)
    layout = SimpleNamespace(emule_workspace_root=tmp_path)

    validation.assert_no_workspace_generated_artifacts(layout)


def test_validation_artifact_audit_rejects_repo_local_generated_outputs(tmp_path: Path) -> None:
    generated_file = tmp_path / "repos" / "amutorrent" / "server" / "node_modules" / "express" / "package.json"
    generated_file.parent.mkdir(parents=True)
    generated_file.write_text("{}\n", encoding="utf-8")
    layout = SimpleNamespace(emule_workspace_root=tmp_path)

    with pytest.raises(RuntimeError, match="EMULEBB_WORKSPACE_OUTPUT_ROOT"):
        validation.assert_no_workspace_generated_artifacts(layout)


def test_product_family_validation_runs_repo_native_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rust_root = tmp_path / "repos" / "emulebb-rust"
    agents_root = tmp_path / "repos" / "p2p-overlord-agents"
    coordinator_root = tmp_path / "repos" / "p2p-overlord-be" / "overlord-be-coordinator"
    goed2k_root = tmp_path / "repos" / "goed2k-server"
    for path in (rust_root, agents_root, coordinator_root, goed2k_root):
        path.mkdir(parents=True)
    calls: list[tuple[tuple[str, ...], Path]] = []

    def fake_run_native(command, **kwargs):
        calls.append((tuple(str(part) for part in command), kwargs["cwd"]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(product_family, "run_native", fake_run_native)
    monkeypatch.setattr(product_family.shutil, "which", lambda name: name)
    layout = SimpleNamespace(
        emulebb_rust_repo_root=rust_root,
        p2p_overlord_agents_repo_root=agents_root,
        p2p_overlord_be_repo_root=tmp_path / "repos" / "p2p-overlord-be",
        ed2k_server_repo_root=goed2k_root,
    )

    product_family.validate_product_family_repos(layout)

    assert (("cargo.exe", "fmt", "--all", "--check"), rust_root) in calls
    assert (("cargo.exe", "fmt", "--all", "--check"), agents_root) in calls
    assert (("npm.cmd", "run", "quality"), coordinator_root) in calls
    assert (("go.exe", "test", "./..."), goed2k_root) in calls


def test_product_family_quick_validation_uses_lightweight_coordinator_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator_root = tmp_path / "repos" / "p2p-overlord-be" / "overlord-be-coordinator"
    coordinator_root.mkdir(parents=True)
    calls: list[tuple[tuple[str, ...], Path]] = []

    def fake_run_native(command, **kwargs):
        calls.append((tuple(str(part) for part in command), kwargs["cwd"]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(product_family, "run_native", fake_run_native)
    monkeypatch.setattr(product_family.shutil, "which", lambda name: name)
    layout = SimpleNamespace(
        p2p_overlord_agents_repo_root=None,
        p2p_overlord_be_repo_root=tmp_path / "repos" / "p2p-overlord-be",
        ed2k_server_repo_root=tmp_path / "repos" / "goed2k-server",
    )

    product_family.validate_product_family_repos(layout, tier="quick")

    assert calls == [((("npm.cmd", "run", "prisma:validate")), coordinator_root)]


def test_product_family_prepare_fetches_native_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rust_root = tmp_path / "repos" / "emulebb-rust"
    agents_root = tmp_path / "repos" / "p2p-overlord-agents"
    coordinator_root = tmp_path / "repos" / "p2p-overlord-be" / "overlord-be-coordinator"
    goed2k_root = tmp_path / "repos" / "goed2k-server"
    for path in (rust_root, agents_root, coordinator_root, goed2k_root):
        path.mkdir(parents=True)
    (rust_root / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    (agents_root / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    (coordinator_root / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (goed2k_root / "go.mod").write_text("module example.invalid/demo\n", encoding="utf-8")
    calls: list[tuple[tuple[str, ...], Path]] = []

    def fake_run_native(command, **kwargs):
        calls.append((tuple(str(part) for part in command), kwargs["cwd"]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(product_family, "run_native", fake_run_native)
    monkeypatch.setattr(product_family.shutil, "which", lambda name: name)
    layout = SimpleNamespace(
        emulebb_rust_repo_root=rust_root,
        p2p_overlord_agents_repo_root=agents_root,
        p2p_overlord_be_repo_root=tmp_path / "repos" / "p2p-overlord-be",
        ed2k_server_repo_root=goed2k_root,
    )

    product_family.prepare_product_family_repos(layout)

    assert (("cargo.exe", "fetch"), rust_root) in calls
    assert (("cargo.exe", "fetch"), agents_root) in calls
    assert (("npm.cmd", "ci"), coordinator_root) in calls
    assert (("npm.cmd", "run", "prisma:generate"), coordinator_root) in calls
    assert (("go.exe", "mod", "download"), goed2k_root) in calls


def test_ed2k_server_product_family_tiers_are_linux_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server_root = tmp_path / "repos" / "ed2k-server"
    server_root.mkdir(parents=True)
    (server_root / "Cargo.toml").write_text("[package]\nname = 'ed2k-server'\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        product_family,
        "_run_ed2k_server_cargo",
        lambda _layout, arguments, *, label: calls.append(arguments),
    )
    layout = SimpleNamespace(
        ed2k_index_server_repo_root=server_root,
        emulebb_rust_repo_root=None,
        p2p_overlord_agents_repo_root=None,
        p2p_overlord_be_repo_root=None,
        ed2k_server_repo_root=tmp_path / "repos" / "goed2k-server",
    )

    product_family.prepare_product_family_repos(layout)
    product_family.validate_product_family_repos(layout, tier="full")

    assert calls == [
        ("fetch", "--locked"),
        ("fmt", "--all", "--check"),
        ("test", "--locked", "--all-targets"),
        ("clippy", "--locked", "--all-targets", "--all-features"),
        ("build", "--locked", "--release"),
    ]


def test_product_family_rebase_refresh_resets_clean_clone_after_remote_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repos" / "amule"
    repo_root.mkdir(parents=True)
    fetched = False
    commands: list[tuple[str, ...]] = []

    def fake_git(_repo_root: Path, *args: str) -> str:
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "master"
        if args == ("status", "--short"):
            return ""
        if args == ("rev-parse", "HEAD"):
            return "a" * 40
        if args == ("rev-parse", "--verify", "refs/remotes/origin/master"):
            return ("b" if fetched else "a") * 40
        raise AssertionError(f"unexpected git call: {args}")

    def fake_run_native(command, **kwargs):
        nonlocal fetched
        del kwargs
        command_tuple = tuple(str(part) for part in command)
        commands.append(command_tuple)
        if command_tuple[-3:] == ("fetch", "origin", "--prune"):
            fetched = True
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(product_family, "_git", fake_git)
    monkeypatch.setattr(product_family, "run_native", fake_run_native)

    result = product_family._refresh_rebased_repo(
        product_family.ProductFamilyRebaseRepo("amule", repo_root, "master")
    )

    assert result.status == "refreshed"
    assert commands[-1][-3:] == ("reset", "--hard", "refs/remotes/origin/master")


def test_product_family_rebase_refresh_noops_when_clone_already_matches_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repos" / "amutorrent"
    repo_root.mkdir(parents=True)
    commands: list[tuple[str, ...]] = []

    def fake_git(_repo_root: Path, *args: str) -> str:
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        if args == ("status", "--short"):
            return ""
        if args == ("rev-parse", "HEAD"):
            return "c" * 40
        if args == ("rev-parse", "--verify", "refs/remotes/origin/main"):
            return "c" * 40
        raise AssertionError(f"unexpected git call: {args}")

    def fake_run_native(command, **kwargs):
        del kwargs
        commands.append(tuple(str(part) for part in command))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(product_family, "_git", fake_git)
    monkeypatch.setattr(product_family, "run_native", fake_run_native)

    result = product_family._refresh_rebased_repo(
        product_family.ProductFamilyRebaseRepo("amutorrent", repo_root, "main")
    )

    assert result.status == "unchanged"
    assert len(commands) == 1
    assert commands[0][-3:] == ("fetch", "origin", "--prune")


def test_product_family_rebase_refresh_refuses_local_divergence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repos" / "amule"
    repo_root.mkdir(parents=True)
    fetched = False

    def fake_git(_repo_root: Path, *args: str) -> str:
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "master"
        if args == ("status", "--short"):
            return ""
        if args == ("rev-parse", "HEAD"):
            return "d" * 40
        if args == ("rev-parse", "--verify", "refs/remotes/origin/master"):
            return ("b" if fetched else "a") * 40
        raise AssertionError(f"unexpected git call: {args}")

    def fake_run_native(command, **kwargs):
        nonlocal fetched
        del kwargs
        command_tuple = tuple(str(part) for part in command)
        if command_tuple[-3:] == ("fetch", "origin", "--prune"):
            fetched = True
        if command_tuple[-4:] == ("reset", "--hard", "refs/remotes/origin/master"):
            raise AssertionError("diverged clone must not be reset")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(product_family, "_git", fake_git)
    monkeypatch.setattr(product_family, "run_native", fake_run_native)

    with pytest.raises(RuntimeError, match="refusing to reset local work"):
        product_family._refresh_rebased_repo(product_family.ProductFamilyRebaseRepo("amule", repo_root, "master"))
