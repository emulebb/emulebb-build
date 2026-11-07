"""Verify architecture preflight rejects advertised but unusable ARM64 execution."""
import json
from types import SimpleNamespace

import pytest

from emule_workspace.linux_containers import LinuxDocker, BINFMT_IMAGE


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
