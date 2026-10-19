from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from emule_workspace import artifact_audit, cli


def test_artifact_audit_flags_repo_local_build_outputs(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    leak = write_file(layout.emule_workspace_root / "repos" / "third_party" / "emulebb-cryptopp" / "x64" / "Output" / "Release" / "cryptlib.lib")
    cache = write_file(layout.build_repo_root / "__pycache__" / "module.pyc")
    source = write_file(layout.emule_workspace_root / "repos" / "third_party" / "emulebb-zlib" / "README.md")

    findings = artifact_audit.audit_workspace_artifacts(layout)

    assert leak in {finding.path for finding in findings}
    assert cache not in {finding.path for finding in findings}
    assert source not in {finding.path for finding in findings}


def test_audit_artifacts_command_fails_on_findings(tmp_path: Path, monkeypatch) -> None:
    layout = make_layout(tmp_path)
    write_file(layout.emule_workspace_root / "workspaces" / "workspace" / "app" / "emulebb-main" / "srchybrid" / "x64" / "Release" / "emulebb.exe")
    monkeypatch.setenv("EMULEBB_WORKSPACE_ROOT", str(layout.emule_workspace_root))
    layout.output_root.mkdir(parents=True)
    monkeypatch.setenv("EMULEBB_WORKSPACE_OUTPUT_ROOT", str(layout.output_root))
    monkeypatch.setattr(cli, "load_layout", lambda *_args, **_kwargs: layout)

    result = CliRunner().invoke(cli.main, ["audit-artifacts"])

    assert result.exit_code != 0
    assert "Workspace artifact audit failed." in result.output


def write_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def make_layout(tmp_path: Path):
    workspace_root = tmp_path / "workspaces" / "workspace"
    output_root = tmp_path.parent / f"{tmp_path.name}-output"
    return SimpleNamespace(
        emule_workspace_root=tmp_path,
        output_root=output_root,
        output_tmp_root=output_root / "tmp",
        workspace_root=workspace_root,
        build_repo_root=tmp_path / "repos" / "emulebb-build",
        tests_repo_root=tmp_path / "repos" / "emulebb-build-tests",
        tooling_repo_root=tmp_path / "repos" / "emulebb-tooling",
        output_third_party_build_root=output_root / "builds" / "third_party",
        dependencies=(),
        app_variants=(),
    )
