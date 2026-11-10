"""Keep workspace tooling isolated from operator Python and Git input streams."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tarfile
import zipfile

import pytest

from emule_workspace import python_runtime as runtime
from emule_workspace import process


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    builder = root / "repos/emulebb-build"
    builder.mkdir(parents=True)
    output = tmp_path / "output"
    output.mkdir()
    for name, text in ((".python-version", "3.15.2\n"), ("pyproject.toml", "[project]\nname='fixture'\n"),
                       ("uv.lock", "version=1\n"), ("emule_workspace/release_assets/python/uv.json", "{}\n")):
        path = builder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    monkeypatch.setattr(runtime, "BUILD_ROOT", builder)
    monkeypatch.setenv("EMULEBB_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("EMULEBB_WORKSPACE_OUTPUT_ROOT", str(output))
    monkeypatch.setattr(runtime, "host_target", lambda: "x86_64-pc-windows-msvc")
    return root, builder, output


@pytest.mark.parametrize("system,machine,target", [
    ("Windows", "AMD64", "x86_64-pc-windows-msvc"), ("Windows", "ARM64", "aarch64-pc-windows-msvc"),
    ("Linux", "x86_64", "x86_64-unknown-linux-gnu"), ("Linux", "aarch64", "aarch64-unknown-linux-gnu"),
    ("Darwin", "x86_64", "x86_64-apple-darwin"), ("Darwin", "arm64", "aarch64-apple-darwin"),
])
def test_native_host_assets_are_selected_explicitly(monkeypatch, system, machine, target):
    monkeypatch.setattr(runtime.platform, "system", lambda: system)
    monkeypatch.setattr(runtime.platform, "machine", lambda: machine)
    monkeypatch.setattr(runtime.platform, "libc_ver", lambda: ("glibc", "2.36"))
    assert runtime.host_target() == target


def test_linked_builder_preserves_operator_environment(installation, monkeypatch):
    root, builder, output = installation
    def git(*args):
        return runtime.subprocess.check_output(["git", "-C", str(builder), *args], text=True)
    git("init")
    git("add", "pyproject.toml")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "fixture")
    checkout = root / "workspaces/agents/first/emulebb-build"
    git("worktree", "add", "--detach", str(checkout))
    monkeypatch.setattr(runtime, "BUILD_ROOT", checkout)
    inherited = dict(runtime.os.environ)
    assert runtime.output_root() == output
    assert dict(runtime.os.environ) == inherited


def test_unrelated_builder_under_agents_is_rejected(installation, monkeypatch):
    root, _, _ = installation
    checkout = root / "workspaces/agents/impostor/emulebb-build"
    checkout.mkdir(parents=True)
    runtime.subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    monkeypatch.setattr(runtime, "BUILD_ROOT", checkout)
    with pytest.raises(RuntimeError, match="canonical builder or its managed worktree"):
        runtime.output_root()


@pytest.mark.parametrize("fault", ["missing", "relative", "inside-workspace", "wrong-builder"])
def test_invalid_operator_roots_are_reported_without_repair(installation, monkeypatch, fault):
    root, builder, output = installation
    if fault == "missing":
        monkeypatch.delenv("EMULEBB_WORKSPACE_OUTPUT_ROOT")
    elif fault == "relative":
        monkeypatch.setenv("EMULEBB_WORKSPACE_OUTPUT_ROOT", "relative-output")
    elif fault == "inside-workspace":
        monkeypatch.setenv("EMULEBB_WORKSPACE_OUTPUT_ROOT", str(builder))
    else:
        monkeypatch.setattr(runtime, "BUILD_ROOT", root / "other-builder")
    inherited = dict(runtime.os.environ)
    with pytest.raises(RuntimeError):
        runtime.output_root()
    assert dict(runtime.os.environ) == inherited


def test_private_environment_never_falls_back_to_global_python(installation):
    with pytest.raises(RuntimeError, match="bootstrap-python"):
        runtime.selected_python()


def test_only_an_environment_with_the_current_dependency_lock_is_selected(installation):
    _, builder, output = installation
    environment = runtime.environment_root(output)
    executable = runtime.environment_python(environment)
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture executable")
    marker = environment / "emulebb-environment.json"
    marker.write_text(json.dumps({"schema": "emulebb.workspace-python/1", "inputs": runtime.environment_inputs()}))
    assert runtime.selected_python() == executable
    (builder / "uv.lock").write_text("changed dependency lock")
    with pytest.raises(RuntimeError, match="bootstrap-python"):
        runtime.selected_python()


def test_corrupt_archive_is_never_executed_or_replaced(tmp_path, monkeypatch):
    archive = tmp_path / "uv.zip"
    archive.write_bytes(b"damaged cache")
    monkeypatch.setattr(runtime.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("Do not overwrite unverified cache"))
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        runtime.download_verified("https://example.invalid/fixture", archive, "0" * 64)
    assert archive.read_bytes() == b"damaged cache"


def test_downloaded_checksum_mismatch_cannot_enter_the_cache(tmp_path, monkeypatch):
    archive = tmp_path / "uv.zip"
    monkeypatch.setattr(runtime.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"wrong bytes"))
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        runtime.download_verified("https://example.invalid/fixture", archive, "0" * 64)
    assert not archive.exists() and not list(tmp_path.iterdir())


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_executable_read_does_not_extract_archive_paths(tmp_path, kind):
    archive = tmp_path / ("fixture.zip" if kind == "zip" else "fixture.tar.gz")
    if kind == "zip":
        with zipfile.ZipFile(archive, "w") as target:
            target.writestr("../outside.txt", "must not extract")
            target.writestr("reviewed/uv", b"expected executable")
    else:
        with tarfile.open(archive, "w:gz") as target:
            for name, data in (("../outside.txt", b"must not extract"), ("reviewed/uv", b"expected executable")):
                member = tarfile.TarInfo(name)
                member.size = len(data)
                target.addfile(member, io.BytesIO(data))
    assert runtime._uv_binary(archive, "uv") == b"expected executable"
    assert not (tmp_path.parent / "outside.txt").exists()


def test_bootstrap_does_not_change_system_paths_or_install_global_launchers(installation, monkeypatch):
    _, _, output = installation
    commands = []
    monkeypatch.setattr(runtime, "ensure_uv", lambda output: Path("reviewed-uv"))
    environment = runtime.environment_root(output)
    environment.mkdir(parents=True)
    monkeypatch.setattr(runtime.subprocess, "run", lambda command, **kwargs: commands.append((command, kwargs)))
    monkeypatch.setattr(runtime.subprocess, "check_output", lambda *args, **kwargs: json.dumps(
        {"version": "3.15.2", "implementation": "CPython", "prefix": str(environment)}))
    inherited = dict(runtime.os.environ)
    runtime.bootstrap()
    assert dict(runtime.os.environ) == inherited
    install = next(command for command, _ in commands if command[1:3] == ["python", "install"])
    assert "--no-bin" in install and "--no-registry" in install
    assert all(kwargs["env"]["PATH"] == inherited["PATH"] for _, kwargs in commands)
    assert all(kwargs["env"]["EMULEBB_WORKSPACE_OUTPUT_ROOT"] == str(output) for _, kwargs in commands)
    assert "--locked" in commands[-1][0] and "--no-install-project" in commands[-1][0]


def test_hook_execution_preserves_gits_stdin_and_argument_boundaries(monkeypatch, tmp_path):
    executable = tmp_path / "private environment/Scripts/python.exe"
    calls = []
    monkeypatch.setattr(runtime, "selected_python", lambda: executable)
    monkeypatch.setattr(runtime.subprocess, "call", lambda *args, **kwargs: calls.append((args, kwargs)) or 7)
    assert runtime.run_script(["script with spaces.py", "push", "origin"]) == 7
    assert calls == [(([str(executable), "-B", "script with spaces.py", "push", "origin"],), {})]


def test_child_python_uses_the_active_interpreter_without_resolving_venv_links(monkeypatch):
    selected = "/owned/venv/bin/python"
    monkeypatch.setattr(process.sys, "executable", selected)
    monkeypatch.setattr(process, "find_tool", lambda *args: pytest.fail("Do not use global PATH"))
    assert process.get_python_invocation().command(["script.py"]) == [str(Path(selected)), "script.py"]


def test_hosted_interpreter_mismatch_fails_before_loading_the_cli(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(runtime, "python_version", lambda: "3.15.2")
    monkeypatch.setattr(runtime.platform, "python_version", lambda: "3.13.12")
    monkeypatch.setattr(runtime.sys, "argv", ["emule_workspace", "--help"])
    with pytest.raises(RuntimeError, match="GitHub must select workspace Python 3.15.2"):
        runtime.main()


def test_running_managed_interpreter_cannot_uninstall_itself(installation, monkeypatch):
    _, _, output = installation
    monkeypatch.setattr(runtime.sys, "base_prefix", str(output / "tools/python/managed/current"))
    monkeypatch.setattr(runtime, "ensure_uv", lambda output: pytest.fail("Do not start uninstall"))
    with pytest.raises(RuntimeError, match="external bootstrap Python"):
        runtime.bootstrap(reinstall=True)


def _assert_hosted_python_pins(builder):
    import yaml

    version = (builder / ".python-version").read_text().strip()
    repos = builder.parent
    # WHY: isolated or hosted checkout names need not be emulebb-build. Always
    # inspect the selected Builder itself, then any available support checkouts.
    roots = {builder, *(repos / name for name in
              ("emulebb-rust", "emulebb-build-tests", "emulebb-tooling"))}
    for root in sorted(roots):
        for path in (root / ".github/workflows").glob("*.yml"):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job in document.get("jobs", {}).values():
                for step in job.get("steps", []):
                    if not step.get("uses", "").startswith("actions/setup-python@"):
                        continue
                    inputs = step.get("with", {})
                    selectors = {"python-version", "python-version-file"}.intersection(inputs)
                    assert len(selectors) == 1, f"ambiguous or missing Python selector: {path}"
                    if "python-version-file" in selectors:
                        assert inputs["python-version-file"] == ".python-version", str(path)
                        selected = (root / ".python-version").read_text().strip()
                        assert selected == version, str(path)
                        continue
                    else:
                        selected = inputs["python-version"]
                    if selected == "${{ inputs.python_version }}":
                        assert document[True]["workflow_call"]["inputs"]["python_version"]["default"] == version
                    else:
                        assert str(selected) == version, str(path)
    rust_pin = repos / "emulebb-rust/tools/ci-toolchain.json"
    if rust_pin.is_file():
        assert json.loads(rust_pin.read_text())["python"] == version


def test_hosted_python_pins_match_the_workspace_runtime():
    _assert_hosted_python_pins(Path(__file__).resolve().parents[1])


def _python_pin_workflow(builder, inputs, default="3.15.2"):
    import yaml

    path = builder / ".github/workflows/pin.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        True: {"workflow_call": {"inputs": {"python_version": {"default": default}}}},
        "jobs": {"check": {"steps": [{"uses": "actions/setup-python@synthetic", "with": inputs}]}},
    }), encoding="utf-8")


@pytest.mark.parametrize("inputs", [
    {"python-version": "3.15.2"},
    {"python-version-file": ".python-version"},
    {"python-version": "${{ inputs.python_version }}"},
])
def test_hosted_pin_scanner_accepts_only_bound_supported_forms(installation, inputs):
    _, builder, _ = installation
    _python_pin_workflow(builder, inputs)
    _assert_hosted_python_pins(builder)


@pytest.mark.parametrize("inputs", [
    {},
    {"version": "3.15.2"},
    {"python-version": "3.15.2", "python-version-file": ".python-version"},
    {"python-version-file": "../.python-version"},
    {"python-version-file": "${{ env.PYTHON_PIN }}"},
    {"python-version": "3.13.12"},
    {"python-version": "${{ env.PYTHON_VERSION }}"},
])
def test_hosted_pin_scanner_rejects_unbound_or_ambiguous_forms(installation, inputs):
    _, builder, _ = installation
    _python_pin_workflow(builder, inputs)
    with pytest.raises(AssertionError):
        _assert_hosted_python_pins(builder)


@pytest.mark.parametrize("pin", ["3.13.12", "${{ inputs.python_version }}"])
def test_hosted_pin_scanner_checks_the_selected_file_against_builder_authority(installation, pin):
    root, builder, _ = installation
    support = root / "repos/emulebb-rust"
    support.mkdir()
    (support / ".python-version").write_text(pin + "\n", encoding="utf-8")
    _python_pin_workflow(support, {"python-version-file": ".python-version"})
    with pytest.raises(AssertionError):
        _assert_hosted_python_pins(builder)
    (support / ".python-version").unlink()
    with pytest.raises(FileNotFoundError):
        _assert_hosted_python_pins(builder)


def test_hosted_pin_scanner_rejects_a_different_reusable_input_default(installation):
    _, builder, _ = installation
    _python_pin_workflow(builder, {"python-version": "${{ inputs.python_version }}"}, default="3.13.12")
    with pytest.raises(AssertionError):
        _assert_hosted_python_pins(builder)


def test_hosted_pin_scanner_always_inspects_a_differently_named_builder(installation):
    _, builder, _ = installation
    selected = builder.with_name("selected-builder")
    builder.rename(selected)
    _python_pin_workflow(selected, {"python-version": "3.13.12"})
    with pytest.raises(AssertionError):
        _assert_hosted_python_pins(selected)
