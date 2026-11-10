"""Provisioning failures must be bounded and must never weaken APT trust."""
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from emule_workspace import cli, ubuntu_ci


class AptHost:
    def __init__(self, root, architecture="amd64", failures=()):
        self.root, self.architecture = root, architecture
        self.failures = list(failures)
        self.commands = []
        self.sources = []
        self.keyring = root / "ubuntu-archive-keyring.gpg"
        self.keyring.write_bytes(b"synthetic signing keyring")

    def run(self, arguments, **kwargs):
        self.commands.append((arguments, kwargs))
        if "apt-get" in arguments:
            source = next(item.split("=", 1)[1] for item in arguments if item.startswith("Dir::Etc::sourcelist="))
            self.sources.append(Path(source).read_text(encoding="utf-8"))
            return SimpleNamespace(returncode=self.failures.pop(0) if self.failures else 0, stdout="public apt diagnostic")
        if "dpkg-query" in arguments:
            return SimpleNamespace(returncode=0, stdout="dpkg-dev\t1.0\tall\nlibfontconfig1-dev\t2.0\tamd64\n")
        return SimpleNamespace(returncode=0, stdout=self.architecture + "\n")

    def provision(self, **kwargs):
        return ubuntu_ci.provision_dependencies(profile="rust-native-release", runner=self.run,
                                                os_release={"ID": "ubuntu", "VERSION_CODENAME": "noble"},
                                                keyring=self.keyring, scratch_root=self.root,
                                                privileged=False, **kwargs)


@pytest.mark.parametrize("architecture,archive,security", [
    ("amd64", "https://archive.ubuntu.com/ubuntu", "https://security.ubuntu.com/ubuntu"),
    ("arm64", "https://ports.ubuntu.com/ubuntu-ports", "https://ports.ubuntu.com/ubuntu-ports"),
])
def test_signed_https_architecture_sources_without_rewriting_other_repositories(tmp_path, architecture, archive, security):
    vendor = tmp_path / "unrelated-vendor.sources"
    vendor.write_bytes(b"Types: deb\nURIs: https://vendor.example.invalid/repo\nSigned-By: /vendor.gpg\n")
    before = vendor.read_bytes()
    host = AptHost(tmp_path, architecture)
    result = host.provision()
    assert result["status"] == "passed" and result["architecture"] == architecture
    assert vendor.read_bytes() == before
    sources = host.sources[0].splitlines()
    assert len(sources) == 3 and all(f"arch={architecture} signed-by={host.keyring}" in line for line in sources)
    assert f"] {archive} noble main universe" in sources[0]
    assert f"] {security} noble-security main universe" in sources[2]
    apt = [args for args, _ in host.commands if "apt-get" in args]
    for args in apt:
        assert args[:3] == ["sudo", "--non-interactive", "--"]
        assert "timeout" in args and "--kill-after=3s" in args
        assert "APT::Update::Error-Mode=any" in args
        assert "Acquire::Retries=0" in args and "Acquire::https::Timeout=20" in args
        assert "Acquire::https::Verify-Peer=true" in args
        assert "Acquire::AllowInsecureRepositories=false" in args
        assert "APT::Get::AllowUnauthenticated=false" in args
        assert "Dir::Etc::sourceparts=-" in args and "APT::Get::List-Cleanup=false" in args
        assert "--allow-unauthenticated" not in args
    assert "--no-install-recommends" in apt[-1]
    assert apt[-1][-2:] == ["dpkg-dev", "libfontconfig1-dev"]
    assert not list(tmp_path.glob("emulebb-ubuntu-apt-*"))


def test_incomplete_update_exhausts_bounded_attempts_and_never_installs(tmp_path):
    host = AptHost(tmp_path, failures=[100, 100, 100])
    with pytest.raises(ubuntu_ci.ProvisioningError) as failure:
        host.provision()
    assert failure.value.receipt["status"] == "failed"
    assert [item["returncode"] for item in failure.value.receipt["operations"]] == [100] * 3
    assert all(args[-1] == "update" for args, _ in host.commands if "apt-get" in args)


def test_transient_update_retries_then_records_installed_versions(tmp_path):
    host = AptHost(tmp_path, failures=[100, 0, 0])
    result = host.provision()
    assert [(item["operation"], item["attempt"]) for item in result["operations"]] == [
        ("update", 1), ("update", 2), ("install", 1)]
    assert result["installedPackages"] == ["dpkg-dev\t1.0\tall", "libfontconfig1-dev\t2.0\tamd64"]


def test_overall_budget_does_not_restart_for_retry_or_install(tmp_path):
    host = AptHost(tmp_path)
    readings = iter([0, 0, 296, 299])
    with pytest.raises(ubuntu_ci.ProvisioningError, match="overall deadline"):
        host.provision(clock=lambda: next(readings))
    assert not any("apt-get" in args for args, _ in host.commands)


def test_timeout_of_root_process_is_not_retried(tmp_path):
    host = AptHost(tmp_path, failures=[124])
    with pytest.raises(ubuntu_ci.ProvisioningError):
        host.provision()
    assert len([args for args, _ in host.commands if "apt-get" in args]) == 1


def test_outer_process_timeout_fails_with_receipt(tmp_path):
    host = AptHost(tmp_path)
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    host.run = timed_out
    with pytest.raises(ubuntu_ci.ProvisioningError, match="overall deadline") as failure:
        host.provision()
    assert failure.value.receipt["status"] == "failed"


@pytest.mark.parametrize("architecture", ["armhf", "", "amd64\nmalformed"])
def test_unsupported_architecture_cannot_trigger_apt(tmp_path, architecture):
    host = AptHost(tmp_path, architecture)
    with pytest.raises(ValueError):
        host.provision()
    assert not any("apt-get" in args for args, _ in host.commands)


def test_ci_command_works_before_workspace_roots_or_heavy_lock(tmp_path, monkeypatch):
    import platform
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    def forbidden(*args, **kwargs):
        raise AssertionError("Early provisioning must not materialize or acquire the heavy lock")
    monkeypatch.setattr(cli, "resolve_required_workspace_roots", forbidden)
    monkeypatch.setattr(cli.WorkspaceLock, "acquire", forbidden)
    calls = []
    monkeypatch.setattr(ubuntu_ci, "provision_dependencies", lambda **kwargs: calls.append(kwargs) or {"status": "passed"})
    result = CliRunner().invoke(cli.main, ["provision-ubuntu-dependencies-ci", "--profile", "rust-native-release",
                                         "--deadline-seconds", "300", "--network-timeout-seconds", "20", "--attempts", "3"])
    assert result.exit_code == 0, result.output
    assert calls == [{"profile": "rust-native-release", "deadline_seconds": 300, "network_timeout_seconds": 20,
                      "attempts": 3, "scratch_root": tmp_path}]


def test_ci_command_rejects_local_provisioning(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    result = CliRunner().invoke(cli.main, ["provision-ubuntu-dependencies-ci", "--profile", "rust-native-release"])
    assert result.exit_code != 0 and "requires GitHub Actions" in result.output
