from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from emule_workspace.config import resolve_required_workspace_roots


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_android_guest_proof_uploads_only_public_construction_and_sanitized_results():
    import yaml

    workflow = yaml.safe_load((_repo_root() / ".github/workflows/android-guest.yml").read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["boot"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert set(job["strategy"]["matrix"]["image"]) == {"min", "current"}
    steps = job["steps"]
    assert any("probe-rust-android-guest-ci" in step.get("run", "") for step in steps)
    uploads = [step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@")]
    assert len(uploads) == 1
    root = "${{ runner.temp }}/emulebb-android-out/reports/"
    assert set(uploads[0]["with"]["path"].splitlines()) == {
        root + "android-emulator/*/build.log", root + "android-emulator/*/toolchain-probe.json",
        root + "android-emulator-boot/*/validation.json"}
    for step in steps:
        if step.get("uses", "").startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] is False
        assert "secrets." not in str(step)


def test_android_tls_runtime_is_additional_pinned_source_bound_guest_proof():
    import re
    import yaml

    workflow = yaml.safe_load((_repo_root() / ".github/workflows/android-guest.yml").read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]["boot"]["strategy"]["matrix"]["image"]) == {"min", "current"}
    job = workflow["jobs"]["tls-runtime"]
    assert job["runs-on"] == "ubuntu-24.04" and job["timeout-minutes"] == 60
    checkouts = {step["with"]["repository"]: step["with"] for step in job["steps"]
                 if step.get("uses", "").startswith("actions/checkout@") and "repository" in step["with"]}
    assert {name: value["ref"] for name, value in checkouts.items()} == {
        "emulebb/emulebb-rust": "${{ steps.rust-source.outputs.rust_sha }}",
        "emulebb/emulebb-tooling": "3a1361364dcf01bdec07f7df65107ee9a80e0375",
        "emulebb/emulebb-build-tests": "db468245313504959146b6222544acf34c0cb53b",
        "emulebb/emulebb-miniupnp": "4e7a10948004ca2085ac8103e14fc77edb898b4c",
        "emulebb/emulebb-libpcpnatpmp": "a6e787f837e507336a8774522c33b54264ef4a4b",
    }
    for step in job["steps"]:
        if "uses" in step:
            assert re.search(r"@[0-9a-f]{40}$", step["uses"])
        if step.get("uses", "").startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] is False
        assert "secrets." not in str(step)
    command = next(step for step in job["steps"] if "--tls-startup" in step.get("run", ""))
    assert command["working-directory"] == "repos/emulebb-build"
    assert "probe-rust-android-guest-ci --image current --tls-startup" in command["run"]
    assert "adb" not in command["run"] and "docker" not in command["run"]
    steps = job["steps"]
    select = next(step for step in steps if step.get("id") == "rust-source")
    verify = next(step for step in steps if step.get("name") == "Verify exact clean Rust TLS source before execution")
    checkout = next(step for step in steps if step.get("with", {}).get("repository") == "emulebb/emulebb-rust")
    assert steps.index(select) < steps.index(checkout) < steps.index(verify) < steps.index(command)
    for step in steps:
        if step.get("uses", "").startswith(("actions/setup-python@", "docker/setup-docker-action@", "docker/setup-buildx-action@")):
            assert steps.index(step) < steps.index(checkout)
    assert verify["working-directory"] == "repos/emulebb-rust"
    assert verify["env"] == {"EXPECTED_RUST_SHA": "${{ steps.rust-source.outputs.rust_sha }}"}
    assert not any("run" in step for step in steps[steps.index(checkout) + 1:steps.index(verify)])
    for trusted in (select, verify):
        assert trusted["shell"] == "bash" and "${{" not in trusted["run"]
        assert "if" not in trusted and not trusted.get("continue-on-error")
    events = workflow.get("on", workflow.get(True))  # PyYAML's YAML 1.1 bool spelling.
    inputs = events["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"rust_ref"}
    assert inputs["rust_ref"]["type"] == "string" and inputs["rust_ref"]["required"] is True
    baseline = "5aba7940cc53eeae20dd3fb19bbb1c275717b7f2"
    assert "default" not in inputs["rust_ref"]
    assert f"rust_sha='{baseline}'" in select["run"]
    assert select["env"] == {
        "SOURCE_EVENT": "${{ github.event_name }}", "REQUESTED_RUST_SHA": "${{ inputs.rust_ref }}"}
    upload = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["if"] == "always()"
    assert upload["with"]["retention-days"] == 14
    paths = upload["with"]["path"].splitlines()
    assert all(path.startswith("${{ runner.temp }}/emulebb-android-tls-out/reports/") for path in paths)
    assert any(path.endswith("/tls-payload/manifest.json") for path in paths)
    assert any(path.endswith("android-emulator-boot/*/validation.json") for path in paths)
    assert not any(name in upload["with"]["path"] for name in ("guest/", "emulator.log", "home/", "*.so", "*-tests"))


def _trusted_tls_steps():
    import yaml

    workflow = yaml.safe_load((_repo_root() / ".github/workflows/android-guest.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["tls-runtime"]["steps"]
    return (next(step for step in steps if step.get("id") == "rust-source"),
            next(step for step in steps if step.get("name") == "Verify exact clean Rust TLS source before execution"))


def _native_bash() -> str:
    if os.name == "nt":
        # WHY: PATH's bash.exe can be the WSL launcher, which must not launch
        # another OS or translate these Windows-only source/evidence paths.
        git_exec = Path(subprocess.check_output(["git", "--exec-path"], text=True).strip())
        bash = git_exec.parents[2] / "bin" / "bash.exe"
        assert bash.is_file(), f"Git for Windows Bash is required: {bash}"
        return str(bash)
    bash = shutil.which("bash")
    assert bash, "Native Bash is required for trusted workflow script tests"
    return bash


def _run_tls_script(script: str, cwd: Path, *, mock_remote: str | None = None, remote_exit: int = 0, **values: str):
    env = {**os.environ, **values}
    if mock_remote is not None:
        commands = cwd / "commands"
        commands.mkdir(exist_ok=True)
        git = commands / "git"
        git.write_text("#!/usr/bin/env bash\nset -euo pipefail\n"
                       "printf '%s\\n' \"$*\" > \"$PWD/git-called\"\n"
                       "[[ $# == 4 && $1 == ls-remote && $2 == --exit-code && "
                       "$3 == https://github.com/emulebb/emulebb-rust.git && $4 == refs/heads/main ]] || exit 99\n"
                       "printf '%s' \"$MOCK_GIT_OUTPUT\"\nexit \"$MOCK_GIT_EXIT\"\n", encoding="utf-8")
        git.chmod(0o755)
        env.update(MOCK_GIT_OUTPUT=mock_remote, MOCK_GIT_EXIT=str(remote_exit))
        script = 'export PATH="$PWD/commands:$PATH"\n' + script
    return subprocess.run([_native_bash(), "--noprofile", "--norc", "-c", script],
                          cwd=cwd, env=env, capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize("requested", [
    "", "main", "refs/heads/main", "a" * 39, "a" * 41, "A" * 40,
    " " + "a" * 40, "a" * 40 + " ", "a" * 40 + "\n", "a" * 40 + "\nextra=bad",
    "$(touch injected)", "`touch injected`", "a;touch injected", "../main",
])
def test_android_tls_manual_selector_rejects_nonexact_sha_before_checkout(tmp_path, requested):
    select, _ = _trusted_tls_steps()
    output = tmp_path / "step-output"
    result = _run_tls_script(select["run"], tmp_path, SOURCE_EVENT="workflow_dispatch",
                             REQUESTED_RUST_SHA=requested, GITHUB_OUTPUT=output.as_posix())
    assert result.returncode != 0
    assert not output.exists() and not (tmp_path / "injected").exists()


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_android_tls_automatic_selector_ignores_manual_data(tmp_path, event):
    select, _ = _trusted_tls_steps()
    output = tmp_path / "step-output"
    result = _run_tls_script(select["run"], tmp_path, SOURCE_EVENT=event,
                             REQUESTED_RUST_SHA="$(touch injected)", GITHUB_OUTPUT=output.as_posix(), mock_remote="untrusted")
    assert result.returncode == 0, result.stderr
    assert output.read_text() == "rust_sha=5aba7940cc53eeae20dd3fb19bbb1c275717b7f2\n"
    assert not (tmp_path / "injected").exists()
    assert not (tmp_path / "git-called").exists()


def test_android_tls_manual_selector_emits_independently_resolved_official_main(tmp_path):
    select, _ = _trusted_tls_steps()
    output = tmp_path / "step-output"
    requested = "0123456789abcdef" * 2 + "01234567"
    result = _run_tls_script(select["run"], tmp_path, SOURCE_EVENT="workflow_dispatch",
                             REQUESTED_RUST_SHA=requested, GITHUB_OUTPUT=output.as_posix(),
                             mock_remote=f"{requested}\trefs/heads/main\n")
    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"rust_sha={requested}\n"
    assert (tmp_path / "git-called").read_text().strip() == (
        "ls-remote --exit-code https://github.com/emulebb/emulebb-rust.git refs/heads/main")


@pytest.mark.parametrize("remote,exit_code", [
    ("", 0), ("", 128), ("a" * 40 + "\trefs/heads/another", 0),
    ("A" * 40 + "\trefs/heads/main", 0), ("a" * 39 + "\trefs/heads/main", 0),
    ("a" * 40 + " refs/heads/main", 0), ("a" * 40 + "\trefs/heads/main extra", 0),
    ("a" * 40 + "\trefs/heads/main\n" + "b" * 40 + "\trefs/heads/main", 0),
    ("b" * 40 + "\trefs/heads/main", 0), ("$(touch injected)\trefs/heads/main", 0),
])
def test_android_tls_manual_selector_rejects_unavailable_ambiguous_or_different_main(tmp_path, remote, exit_code):
    select, _ = _trusted_tls_steps()
    output = tmp_path / "step-output"
    result = _run_tls_script(select["run"], tmp_path, SOURCE_EVENT="workflow_dispatch",
                             REQUESTED_RUST_SHA="a" * 40, GITHUB_OUTPUT=output.as_posix(),
                             mock_remote=remote, remote_exit=exit_code)
    assert result.returncode != 0
    assert not output.exists() and not (tmp_path / "injected").exists()


def test_android_tls_actual_checkout_must_be_exact_and_clean(tmp_path):
    _, verify = _trusted_tls_steps()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "--quiet")
    git("config", "user.name", "Synthetic Test")
    git("config", "user.email", "synthetic@example.invalid")
    payload = tmp_path / "source.txt"
    payload.write_text("clean\n", encoding="utf-8")
    git("add", "source.txt")
    git("commit", "--quiet", "-m", "Synthetic exact-source fixture")
    sha = git("rev-parse", "HEAD")
    assert _run_tls_script(verify["run"], tmp_path, EXPECTED_RUST_SHA=sha).returncode == 0
    for wrong in ("0" * 40, "main", "$(touch injected)", ""):
        assert _run_tls_script(verify["run"], tmp_path, EXPECTED_RUST_SHA=wrong).returncode != 0
    assert not (tmp_path / "injected").exists()
    payload.write_text("dirty\n", encoding="utf-8")
    assert _run_tls_script(verify["run"], tmp_path, EXPECTED_RUST_SHA=sha).returncode != 0
    git("checkout", "--", "source.txt")
    (tmp_path / "unexpected.txt").write_text("untracked\n", encoding="utf-8")
    assert _run_tls_script(verify["run"], tmp_path, EXPECTED_RUST_SHA=sha).returncode != 0
    (tmp_path / "unexpected.txt").unlink()
    # WHY: failed git-status output must not be mistaken for an empty clean
    # tree when HEAD is still valid. Exercise the real Git failure boundary.
    (tmp_path / ".git" / "index").write_bytes(b"invalid synthetic index")
    assert git("rev-parse", "HEAD") == sha
    assert subprocess.run(["git", "-C", str(tmp_path), "status", "--porcelain=v1"],
                          capture_output=True, timeout=20).returncode != 0
    assert _run_tls_script(verify["run"], tmp_path, EXPECTED_RUST_SHA=sha).returncode != 0


def test_branch_check_leaves_concurrency_to_reusable_workflow() -> None:
    text = (_repo_root() / ".github/workflows/branch-check.yml").read_text(encoding="utf-8")
    # WHY: identical caller/callee groups cancel the caller before any jobs run.
    assert "concurrency:" not in text
    assert "repo_kind: emulebb-build" in text
    assert "emulebb/emulebb-tooling/.github/workflows/branch-check.yml@" in text


def _workspace_root() -> Path:
    if "EMULEBB_WORKSPACE_ROOT" in os.environ:
        return resolve_required_workspace_roots()[0]
    # WHY: standalone workflow lint checks out only this repository. Preserve
    # its source-only discovery/optional external-repository coverage, while
    # managed local runs use the inherited root rather than worktree depth.
    return _repo_root().parents[1]


def _app_workflow(name: str) -> Path:
    return _workspace_root() / "workspaces" / "workspace" / "app" / "emulebb-main" / ".github" / "workflows" / name


def _read_app_workflow(name: str) -> str:
    workflow = _app_workflow(name)
    if not workflow.is_file():
        pytest.skip(f"app workflow is not available in this checkout: {workflow}")
    return workflow.read_text(encoding="utf-8")


def test_reusable_workspace_command_owns_materialized_ci_setup() -> None:
    workflow = _repo_root() / ".github" / "workflows" / "reusable-workspace-command.yml"
    text = workflow.read_text(encoding="utf-8")

    assert "workflow_call:" in text
    assert "EMULEBB_WORKSPACE_ROOT: ${{ github.workspace }}" in text
    assert "Join-Path $env:EMULEBB_WORKSPACE_ROOT '..\\emulebb-output'" in text
    assert "EMULEBB_WORKSPACE_OUTPUT_ROOT=$outputRoot" in text
    assert 'Join-Path $outputRoot "builds\\rust\\target"' in text
    assert "CARGO_TARGET_DIR=$cargoTargetDir" in text
    assert "Resolve-ArtifactPathInput" in text
    assert "python -m emule_workspace materialize" in text
    assert "Checkout aMuTorrent source" not in text
    assert "amutorrent_repository" not in text
    assert "repos/amutorrent" not in text
    assert "python -m emule_workspace audit-artifacts" in text
    assert "materialize --workspace-root" not in text
    assert "setup_wsl:" in text
    assert "if: ${{ inputs.setup_wsl }}" in text
    assert "Vampire/setup-wsl@4c48163d9f981914dd8caa0fc335fe19cca5bff4 # v7.2.0" in text
    assert "distribution: Debian-13" in text
    assert "wsl-version: 1" in text
    assert "additional-packages: build-essential cargo rustc" in text
    assert "input_artifact_name:" in text
    assert "input_artifact_output_path:" in text
    assert "if: ${{ inputs.input_artifact_name != '' }}" in text
    assert "uses: actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333" in text
    assert "id: workspace-output" in text
    assert '"root=$outputRoot" | Add-Content -LiteralPath $env:GITHUB_OUTPUT' in text
    assert "steps.workspace-output.outputs.root, inputs.input_artifact_output_path" in text
    assert (
        "python -m pip install \"click>=8.1\" \"pydantic>=2.0\" \"jinja2>=3\" "
        "\"jsonschema>=4\" \"PyYAML>=6\" \"pywin32\" \"pywinauto\""
    ) in text
    assert "Reset-RepoRef" in text
    assert "[string]$Ref" in text
    assert "$PSNativeCommandUseErrorActionPreference = $true" in text
    assert "repos\\amutorrent" not in text
    assert "workspaces\\workspace\\app\\emulebb-main" in text


def test_reusable_workspace_command_keeps_hyperv_local_only() -> None:
    workflow = _repo_root() / ".github" / "workflows" / "reusable-workspace-command.yml"
    text = workflow.read_text(encoding="utf-8").lower()

    assert "hyper-v" not in text
    assert "hyperv" not in text
    assert "windows-vm" not in text


def test_build_baseline_uses_supported_reusable_inputs() -> None:
    workflow = _repo_root() / ".github" / "workflows" / "baseline.yml"
    text = workflow.read_text(encoding="utf-8")

    assert "uses: emulebb/emulebb-tooling/.github/workflows/reusable-baseline.yml@856e0499d12e281697618cc81a0b09805e96f59e" in text
    assert "repo_kind: emulebb-build" in text
    assert "extra_step_script:" not in text
    assert "extra_commands:" in text
    assert "python -m pip install -e .[dev]" in text
    assert "python -m pytest tests/test_ci_workflows.py" in text
    assert {"tests/test_layout.py", "tests/test_materialize.py", "tests/test_public_presence_checks.py"} <= set(text.split())


def test_linux_lifecycle_proof_cannot_skip_os_process_tests_or_drift_dependency_pins():
    import re
    import shlex
    import tomllib
    import yaml

    workflow = yaml.safe_load((_repo_root() / ".github/workflows/baseline.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["lifecycle-linux"]
    assert job["runs-on"].startswith("ubuntu-")
    assert job["defaults"]["run"]["shell"] == "bash"
    assert not job.get("continue-on-error") and "if" not in job
    steps = job["steps"]
    for step in steps:
        assert not step.get("continue-on-error") and "if" not in step
        if "uses" in step:
            assert re.fullmatch(r"actions/(checkout|setup-python)@[0-9a-f]{40}", step["uses"])
        if step.get("uses", "").startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] is False
            assert "repository" not in step["with"]
        if step.get("uses", "").startswith("actions/setup-python@"):
            assert step["with"]["python-version-file"] == ".python-version"
    commands = [shlex.split(step["run"]) for step in steps if "run" in step]
    test = next(command for command in commands if "pytest" in command)
    assert {"tests/test_workstreams.py", "tests/test_locks.py", "tests/test_cli.py", "tests/test_public_presence_checks.py"} <= set(test)
    assert not {"-k", "-m", "--deselect", "--ignore", "||"} & set(test[4:])
    assert test[test.index("--basetemp") + 1].startswith("${{ runner.temp }}/")
    assert "no:cacheprovider" in test and "-B" in test
    locked = {package["name"]: package["version"]
              for package in tomllib.loads((_repo_root() / "uv.lock").read_text(encoding="utf-8"))["package"]}
    install = next(command for command in commands if "install" in command)
    for requirement in install[install.index("install") + 1:]:
        name, version = requirement.split("==")
        assert locked[name.lower()] == version


def test_controlled_smoke_uses_reusable_core_offline_and_lan_suite() -> None:
    text = _read_app_workflow("controlled-smoke.yml")

    assert "workflow_dispatch:" in text
    assert 'cron: "47 5 * * *"' in text
    assert "uses: emulebb/emulebb-build/.github/workflows/reusable-workspace-command.yml@main" in text
    assert "runs_on: ${{ matrix.runner.label }}" in text
    assert "label: windows-2022" in text
    assert "runs_on: windows-11-arm" in text
    assert "label: windows-2025-vs2026" not in text
    assert "Hosted runner: windows-2022 with v143 ATL/MFC" in text
    assert "Hosted runner: windows-11-arm" in text
    assert 'node_version: "24"' in text
    assert "app_ref: ${{ github.sha }}" in text
    assert "timeout_minutes: 90" in text
    assert "python -m emule_workspace package-release" in text
    assert "python -m emule_workspace build app" in text
    assert "python -m emule_workspace package-release `\n          --workspace-root" not in text
    assert "python -m emule_workspace build app `\n          --workspace-root" not in text
    assert "--variant main" in text
    assert "tracing-harness" not in text
    assert "tmp\\ci\\live-wire-inputs.ci.json" in text
    assert "local_package_install" in text
    assert "Get-NetAdapter" in text
    assert "--materialize-test-install" in text
    assert "--materialize-test-install-skip-build" in text
    assert "--live-wire-inputs-file $ciLiveWireInputs" in text
    assert "--suite command-line-smoke" in text
    assert "--test-network offline" in text
    assert "--suite deterministic-two-client-transfer" in text
    assert "--test-network lan" in text
    assert "--platform ARM64" in text
    assert "Expand-Archive -LiteralPath $packageZip" in text
    assert r"scripts\command-line-smoke.py" in text
    assert "Release ARM64 eMuleBB package" in text
    assert "ARM64 offline command-line smoke" in text
    assert "reports/command-line-smoke/**" in text
    assert "artifacts/command-line-smoke/**" in text
    assert "reports/live-e2e-suite/**" in text
    assert "artifacts/live-e2e-suite/**" in text
    assert "logs/builds/**" in text


def test_controlled_smoke_gates_expensive_lanes_behind_wsl_preflight() -> None:
    text = _read_app_workflow("controlled-smoke.yml")

    assert "scope:" in text
    assert "default: preflight" in text
    assert "ed2k_wsl_preflight:" in text
    assert "runs-on: windows-2022" in text
    assert "timeout-minutes: 10" in text
    assert "repository: emulebb/emulebb-build-tests" in text
    assert "ref: main" in text
    assert "EMULEBB_WORKSPACE_OUTPUT_ROOT: ${{ github.workspace }}/../emulebb-output" in text
    assert "Vampire/setup-wsl@d1da7f2c0322a5ee4f24975344f67fc0f5baf364" in text
    assert "distribution: Debian-13" in text
    assert "wsl-version: 1" in text
    assert r"scripts\ed2k-server-readiness-smoke.py" in text
    assert "reports/ed2k-server-readiness-smoke/**" in text
    gate = (
        "if: ${{ needs.ed2k_wsl_preflight.result == 'success' && "
        "(github.event_name == 'schedule' || inputs.scope == 'full') }}"
    )
    assert text.count(gate) == 2
    assert "if: ${{ github.event_name == 'schedule' || inputs.scope == 'full' }}" in text
    assert "PREFLIGHT_RESULT: ${{ needs.ed2k_wsl_preflight.result }}" in text


def test_controlled_smoke_keeps_first_lane_narrow() -> None:
    text = _read_app_workflow("controlled-smoke.yml").lower()

    assert "--fixture-size-bytes" not in text
    assert "package-helper-integration" not in text
    assert "amutorrent-local-ed2k-ui-live" not in text
    assert "--test-network vpn" not in text
    assert "--test-network all" not in text
    assert "hyper-v" not in text
    assert "hyperv" not in text
    assert "windows-vm" not in text


def test_vs2026_v145_probe_exercises_native_build_without_smoke_or_package() -> None:
    text = _read_app_workflow("vs2026-v145-probe.yml")

    assert "workflow_dispatch:" in text
    assert "runs_on: windows-2025-vs2026" in text
    assert '$env:EMULEBB_VS_PLATFORM_TOOLSET = "v145"' in text
    assert '$env:EMULEBB_CMAKE_GENERATOR = "Visual Studio 18 2026"' in text
    assert "python -m emule_workspace build libs" in text
    assert "python -m emule_workspace build app" in text
    assert "--config Release" in text
    assert "--platform x64" in text
    assert "--clean" in text
    assert "--variant main" in text
    assert "reports/v145-probe/**" in text
    assert "logs/builds/**" in text
    assert "package-release" not in text
    assert "package-amutorrent" not in text
    assert "test live-e2e" not in text
    assert "hyper-v" not in text.lower()
    assert "hyperv" not in text.lower()


def test_emulebb_publish_release_workflow_builds_and_publishes_github_generated_assets() -> None:
    text = _read_app_workflow("publish-release.yml")

    assert "workflow_dispatch:" in text
    assert "release_version:" in text
    assert "target_sha:" in text
    assert "build_ref:" in text
    assert "emulebb-v${RELEASE_VERSION}" in text
    assert "^[0-9]+\\.[0-9]+\\.[0-9]+(-(rc|beta)\\.[0-9]+)?$" in text
    assert "^[0-9a-fA-F]{40}$" in text
    assert "gh release view" in text
    assert 'tag_type="$(git cat-file -t ' in text
    assert "git tag -a \"${TAG_NAME}\" \"${TARGET_SHA}\"" in text
    assert "git push origin \"refs/tags/${TAG_NAME}\"" in text
    assert "--verify-tag" in text
    assert "--prerelease --latest=false" in text
    assert "--latest" in text
    assert "uses: emulebb/emulebb-build/.github/workflows/reusable-workspace-command.yml@main" in text
    assert "app_ref: ${{ needs.prepare.outputs.target_sha }}" in text
    assert "build_ref: ${{ inputs.build_ref }}" in text
    assert "runs_on: windows-2022" in text
    assert "python -m emule_workspace package-release" in text
    assert "--release-version $packageVersion" in text
    assert "emulebb-$packageVersion-$assetArch.zip" in text
    assert "emulebb-$packageVersion-$assetArch.manifest.json" in text
    assert "emulebb-$packageVersion-$assetArch.sbom.spdx.json" in text
    assert "emulebb-$packageVersion-diagnostics-$assetArch.zip" in text
    assert "emulebb-$packageVersion-diagnostics-$assetArch.manifest.json" in text
    assert "emulebb-$packageVersion-diagnostics-$assetArch.sbom.spdx.json" in text
    assert "Bootstrap-eMuleBBSuite.ps1" in text
    assert "Bootstrap-eMuleBBSuite.ps1.sha256" in text
    assert "actions/attest@v4" in text
    assert "gh attestation verify PATH_TO_ASSET -R ${GITHUB_REPOSITORY}" in text
    assert "package-amutorrent" not in text
    assert "emulebb-nightly-" not in text


def test_emulebb_rc_package_proof_is_manual_and_non_publishing() -> None:
    text = _read_app_workflow("rc-package-proof.yml")

    assert "workflow_dispatch:" in text
    assert "release_version:" in text
    assert "target_sha:" in text
    assert "build_ref:" in text
    assert "schedule:" not in text
    assert "uses: emulebb/emulebb-build/.github/workflows/reusable-workspace-command.yml@main" in text
    assert "app_ref: ${{ needs.prepare.outputs.target_sha }}" in text
    assert "build_ref: ${{ inputs.build_ref }}" in text
    assert "runs_on: windows-2022" in text
    assert "python -m emule_workspace package-release" in text
    assert "--release-version $packageVersion" in text
    assert "emulebb-$packageVersion-$assetArch.zip" in text
    assert "emulebb-$packageVersion-$assetArch.manifest.json" in text
    assert "emulebb-$packageVersion-$assetArch.sbom.spdx.json" in text
    assert "emulebb-$packageVersion-diagnostics-$assetArch.zip" in text
    assert "emulebb-$packageVersion-diagnostics-$assetArch.manifest.json" in text
    assert "emulebb-$packageVersion-diagnostics-$assetArch.sbom.spdx.json" in text
    assert "Bootstrap-eMuleBBSuite.ps1" in text
    assert "Bootstrap-eMuleBBSuite.ps1.sha256" in text
    assert "emulebb-diagnostics.exe" in text
    assert "No release, tag, attestation, nightly publish, soak, or live campaign" in text
    assert "gh release create" not in text
    assert "git tag -a" not in text
    assert "actions/attest" not in text
    assert "package-amutorrent" not in text
    assert "test live-e2e" not in text


def test_emulebb_nightly_workflow_publishes_standard_and_diagnostics_assets() -> None:
    text = _read_app_workflow("nightly.yml")

    assert "emulebb-nightly-${release_date}-${short_sha}" in text
    assert "python -m emule_workspace package-release" in text
    assert "--release-version $packageVersion" in text
    assert "emulebb-$packageVersion-${{ matrix.asset_arch }}.zip" in text
    assert "emulebb-$packageVersion-${{ matrix.asset_arch }}.manifest.json" in text
    assert "emulebb-$packageVersion-${{ matrix.asset_arch }}.sbom.spdx.json" in text
    assert "emulebb-$packageVersion-diagnostics-${{ matrix.asset_arch }}.zip" in text
    assert "emulebb-$packageVersion-diagnostics-${{ matrix.asset_arch }}.manifest.json" in text
    assert "emulebb-$packageVersion-diagnostics-${{ matrix.asset_arch }}.sbom.spdx.json" in text
    assert "Nightly standard and diagnostics ZIP" in text
    assert "Bootstrap-eMuleBBSuite.ps1" in text
    assert "Bootstrap-eMuleBBSuite.ps1.sha256" in text
    assert "actions/attest@v4" in text
