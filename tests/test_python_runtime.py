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
        runtime._download("https://example.invalid/fixture", archive, "0" * 64)
    assert archive.read_bytes() == b"damaged cache"


def test_downloaded_checksum_mismatch_cannot_enter_the_cache(tmp_path, monkeypatch):
    archive = tmp_path / "uv.zip"
    monkeypatch.setattr(runtime.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"wrong bytes"))
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        runtime._download("https://example.invalid/fixture", archive, "0" * 64)
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


def test_hosted_python_pins_match_the_workspace_runtime():
    import yaml

    builder = Path(__file__).resolve().parents[1]
    version = (builder / ".python-version").read_text().strip()
    repos = builder.parent
    for name in ("emulebb-build", "emulebb-rust", "emulebb-build-tests", "emulebb-tooling"):
        for path in (repos / name / ".github/workflows").glob("*.yml"):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job in document.get("jobs", {}).values():
                for step in job.get("steps", []):
                    if not step.get("uses", "").startswith("actions/setup-python@"):
                        continue
                    selected = step["with"]["python-version"]
                    if selected == "${{ inputs.python_version }}":
                        assert document[True]["workflow_call"]["inputs"]["python_version"]["default"] == version
                    else:
                        assert str(selected) == version, str(path)
    rust_pin = repos / "emulebb-rust/tools/ci-toolchain.json"
    if rust_pin.is_file():
        assert json.loads(rust_pin.read_text())["python"] == version
