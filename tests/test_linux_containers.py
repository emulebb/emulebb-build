"""Verify architecture preflight rejects advertised but unusable ARM64 execution."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from emule_workspace.linux_containers import LinuxDocker, BINFMT_IMAGE
from emule_workspace.linux_containers import validate_elf_audit
from emule_workspace.linux_containers import build_configuration, RUST_IMAGES, MUSL_CROSS_IMAGE
from emule_workspace.linux_containers import run_owned_container


def test_timed_out_wsl_job_removes_only_its_owned_container(tmp_path, monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if "--name" in command:
            kwargs["stdout"].write("retained build failure\n")
            raise subprocess.TimeoutExpired(command, 1)
    monkeypatch.setattr("emule_workspace.linux_containers.subprocess.run", run)
    docker = LinuxDocker()
    path = tmp_path / "build.log"
    with pytest.raises(RuntimeError, match="inspect"):
        run_owned_container(docker, ["--rm", "image", "cargo", "test"], path, timeout=1)
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[-1][-3:] == ["rm", "--force", name]
    assert path.read_text() == "retained build failure\n"


def test_arm64_build_is_native_on_ci_and_cross_compiled_locally():
    assert build_configuration("musl", "arm64", "arm64") == (RUST_IMAGES["musl"], "arm64", False)
    assert build_configuration("musl", "arm64", "amd64") == (MUSL_CROSS_IMAGE, "amd64", True)
    assert build_configuration("gnu", "arm64", "amd64") == (RUST_IMAGES["gnu"], "amd64", True)
    with pytest.raises(ValueError):
        build_configuration("musl", "armv7", "amd64")


def test_static_musl_audit_rejects_interpreter_shared_dependencies_and_wrong_cpu():
    validate_elf_audit("Machine: AArch64\nThere is no dynamic section", libc="musl", architecture="arm64")
    for audit in ("Machine: AArch64\nINTERP", "Machine: AArch64\nNEEDED libgcc_s.so", "Machine: X86-64"):
        with pytest.raises(RuntimeError):
            validate_elf_audit(audit, libc="musl", architecture="arm64")


@pytest.mark.parametrize("platforms,machine", [("linux/amd64", "aarch64"), ("linux/amd64, linux/arm64", "x86_64")])
def test_preflight_requires_available_and_executable_arm64(tmp_path, monkeypatch, platforms, machine):
    docker = LinuxDocker()
    def run(*args, **kwargs):
        if args[0] == "version":
            return SimpleNamespace(stdout=json.dumps({"Server": {"Arch": "amd64", "Version": "test"}}))
        return SimpleNamespace(stdout=f"Platforms: {platforms}" if args[0] == "buildx" else machine)
    monkeypatch.setattr(docker, "run", run)
    with pytest.raises(RuntimeError):
        docker.preflight(tmp_path)
    assert not (tmp_path / "reports/linux-containers/preflight.json").exists()


def test_setup_registers_only_pinned_arm64_and_retains_proof(tmp_path, monkeypatch):
    docker = LinuxDocker()
    commands = []
    def run(*args, **kwargs):
        commands.append(args)
        if args[0] == "version":
            return SimpleNamespace(stdout=json.dumps({"Server": {"Arch": "amd64", "Version": "test"}}))
        return SimpleNamespace(stdout="Platforms: linux/amd64, linux/arm64" if args[0] == "buildx" else "aarch64")
    monkeypatch.setattr(docker, "run", run)
    docker.preflight(tmp_path, setup_arm64=True)
    assert ("run", "--privileged", "--rm", BINFMT_IMAGE, "--install", "arm64") in commands
    assert json.loads((tmp_path / "reports/linux-containers/preflight.json").read_text())["arm64_execution"] == "aarch64"


def test_untraversable_windows_mount_uses_same_directory_and_cleans_alias(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr("emule_workspace.linux_containers.subprocess.check_output", lambda *a, **k: "/unavailable\n")
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=1 if "test" in command else 0)
    monkeypatch.setattr("emule_workspace.linux_containers.subprocess.run", run)
    with LinuxDocker() as docker:
        docker.prefix = ["wsl.exe", "-d", "Ubuntu", "--"]
        mapped = docker.path(tmp_path)
        assert mapped.startswith("/mnt/emulebb-build-")
        assert docker.path(tmp_path / "cache") == mapped + "/cache"
    assert any("mount" in cmd and tmp_path.resolve().as_posix() in cmd for cmd in commands)
    assert commands[-2][-2:] == ["umount", mapped]
    assert commands[-1][-2:] == ["rmdir", mapped]
