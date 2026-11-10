"""Public checks must bind the selected authority and stage outside sources."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from emule_workspace import public_presence_checks as checks


@pytest.fixture
def public_layout(tmp_path, monkeypatch):
    root, output = tmp_path / "source", tmp_path / "output"
    output.mkdir()
    fields = {"emulebb_pages_repo_root": "emulebb-pages",
              "emulebb_org_profile_repo_root": "emulebb-org-profile",
              "tooling_repo_root": "emulebb-tooling", "build_repo_root": "emulebb-build"}
    paths = {field: root / "repos" / name for field, name in fields.items()}
    for path in paths.values():
        path.mkdir(parents=True)
    pages = paths["emulebb_pages_repo_root"]
    (pages / "sitemap.xml").write_text('<urlset><url><lastmod>2026-01-02</lastmod></url></urlset>', encoding="utf-8")
    profile = paths["emulebb_org_profile_repo_root"] / "profile/README.md"
    profile.parent.mkdir()
    profile.write_text("# Synthetic organization profile\n", encoding="utf-8")
    layout = SimpleNamespace(emule_workspace_root=root, output_root=output, **paths,
                             subprocess_environment=lambda: {})
    monkeypatch.setattr(checks, "_git", lambda path, *args:
                        str(path) if args == ("rev-parse", "--show-toplevel") else
                        "a" * 40 if args == ("rev-parse", "HEAD") else "")
    monkeypatch.setattr(checks, "_capture", lambda *args, **kwargs: json.dumps({
        "schema": "emulebb.pages.renderer-capabilities/1", "check": True,
        "outputStaging": {"protocol": 1, "boundary": "workspace-output", "sourceWrites": False},
    }))
    return layout


def stage_installer(path: Path, *, valid=True):
    path.mkdir(parents=True)
    payload = b"# Synthetic stable installer fixture\n"
    (path / "install.ps1").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest() if valid else "0" * 64
    (path / "install.ps1.sha256").write_text(f"{digest}  install.ps1\n", encoding="utf-8")


def test_pages_checks_bind_selected_contract_date_and_output_only_stage(public_layout, monkeypatch):
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        if "--output-root" in command:
            stage_installer(Path(command[command.index("--output-root") + 1]))
    monkeypatch.setattr(checks, "_run", run)
    report = checks.validate_public_presence(public_layout, repository="emulebb-pages")
    result = json.loads(report.read_text())
    assert result["status"] == "passed"
    assert set(result["sources"]) == {"emulebb-pages", "emulebb-tooling", "emulebb-build"}
    staging = Path(result["staging"])
    assert staging.is_relative_to(public_layout.output_root / "artifacts/public-presence")
    render = [command for command in commands if "render_pages.py" in " ".join(command)]
    assert len(render) == 2
    assert all(command[command.index("--lastmod") + 1] == "2026-01-02" for command in render)
    assert all(command[command.index("--contract") + 1] == str(public_layout.tooling_repo_root / "manifests/public-presence.v1.json")
               for command in render)
    assert not (public_layout.emulebb_pages_repo_root / ".local").exists()
    assert not any("cargo" in command or "docker" in command for command in commands)


def test_old_renderer_external_stage_rejection_has_no_source_fallback(public_layout, monkeypatch):
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        if "--output-root" in command:
            raise RuntimeError("old renderer rejects external OUTPUT staging")
    monkeypatch.setattr(checks, "_run", run)
    with pytest.raises(RuntimeError, match="old renderer"):
        checks.validate_public_presence(public_layout, repository="emulebb-pages")
    report = next(public_layout.output_root.glob("reports/public-presence/emulebb-pages/*/results.json"))
    assert json.loads(report.read_text())["status"] == "failed"
    stages = [command[command.index("--output-root") + 1] for command in commands if "--output-root" in command]
    assert len(stages) == 1 and Path(stages[0]).is_relative_to(public_layout.output_root)
    assert not (public_layout.emulebb_pages_repo_root / ".local").exists()


@pytest.mark.parametrize("response", ["not JSON", '{}', '{"schema":"old"}',
    '{"schema":"emulebb.pages.renderer-capabilities/1","check":true,"outputStaging":{"protocol":1,"boundary":"source-local","sourceWrites":false}}'])
def test_capability_mismatch_prevents_any_render_or_fallback(public_layout, monkeypatch, response):
    monkeypatch.setattr(checks, "_capture", lambda *args, **kwargs: response)
    monkeypatch.setattr(checks, "_run", lambda *args, **kwargs: pytest.fail("Unsupported renderer must not run"))
    with pytest.raises((ValueError, RuntimeError)):
        checks.validate_public_presence(public_layout, repository="emulebb-pages")
    assert not (public_layout.emulebb_pages_repo_root / ".local").exists()


def test_installer_mismatch_is_a_blocking_retained_failure(public_layout, monkeypatch):
    def run(command, **kwargs):
        if "--output-root" in command:
            stage_installer(Path(command[command.index("--output-root") + 1]), valid=False)
    monkeypatch.setattr(checks, "_run", run)
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        checks.validate_public_presence(public_layout, repository="emulebb-pages")
    report = next(public_layout.output_root.glob("reports/public-presence/emulebb-pages/*/results.json"))
    assert json.loads(report.read_text())["status"] == "failed"


def test_org_checks_do_not_read_pages_or_product_sources(public_layout, monkeypatch):
    public_layout.emulebb_pages_repo_root = None
    commands = []
    monkeypatch.setattr(checks, "_run", lambda command, **kwargs: commands.append(command))
    report = checks.validate_public_presence(public_layout, repository="emulebb-org-profile")
    result = json.loads(report.read_text())
    assert result["status"] == "passed"
    assert set(result["sources"]) == {"emulebb-org-profile", "emulebb-tooling", "emulebb-build"}
    assert any("check-org-profile" in command for command in commands)
    assert (Path(result["staging"]) / "README.md").read_text() == "# Synthetic organization profile\n"


def test_consumed_authority_head_change_cannot_receive_a_pass(public_layout, monkeypatch):
    revision = ["a" * 40]
    monkeypatch.setattr(checks, "_git", lambda path, *args:
                        str(path) if args == ("rev-parse", "--show-toplevel") else
                        revision[0] if args == ("rev-parse", "HEAD") else "")
    monkeypatch.setattr(checks, "_run", lambda *args, **kwargs: revision.__setitem__(0, "b" * 40))
    with pytest.raises(RuntimeError, match="source identity changed"):
        checks.validate_public_presence(public_layout, repository="emulebb-org-profile")


def test_missing_topology_and_in_source_output_are_rejected(public_layout):
    public_layout.emulebb_pages_repo_root = None
    with pytest.raises(RuntimeError, match="not admitted"):
        checks.validate_public_presence(public_layout, repository="emulebb-pages")
    public_layout.output_root = public_layout.emule_workspace_root / "output"
    with pytest.raises(RuntimeError, match="outside the source"):
        checks.validate_public_presence(public_layout, repository="emulebb-org-profile")


def test_invalid_committed_date_does_not_use_today_or_regenerate_sources(public_layout, monkeypatch):
    (public_layout.emulebb_pages_repo_root / "sitemap.xml").write_text(
        '<urlset><lastmod>2026-01-02</lastmod><lastmod>2026-01-03</lastmod></urlset>', encoding="utf-8")
    monkeypatch.setattr(checks, "_run", lambda *args, **kwargs: pytest.fail("No command should run"))
    with pytest.raises(RuntimeError, match="one consistent ISO"):
        checks.validate_public_presence(public_layout, repository="emulebb-pages")


def test_runner_does_not_reintroduce_inherited_git_bindings(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", "foreign.git")
    copied = {"EMULEBB_WORKSPACE_ROOT": "synthetic-root", "TEMP": str(tmp_path)}
    def run(command, **kwargs):
        assert kwargs["env"] == copied and "GIT_DIR" not in kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="capability")
    monkeypatch.setattr(checks.subprocess, "run", run)
    assert checks._capture(["python", "-B", "renderer"], cwd=tmp_path, environment=copied) == "capability"


def test_nested_git_directory_cannot_masquerade_as_selected_source(public_layout, monkeypatch):
    original = checks._git
    monkeypatch.setattr(checks, "_git", lambda path, *args:
                        str(path.parent) if args == ("rev-parse", "--show-toplevel") else original(path, *args))
    with pytest.raises(RuntimeError, match="exact repository root"):
        checks.validate_public_presence(public_layout, repository="emulebb-org-profile")
