"""The prepared package environment must contain the pipeline's actual tools."""
import json
import os
import re
import stat
import tomllib

import pytest

from emule_workspace import ci_linux_tools as tools


def test_exported_python_dependencies_are_hash_locked_to_workspace_authority():
    lock = tomllib.loads((tools.ASSETS.parents[1] / "uv.lock").read_text(encoding="utf-8"))
    versions = {item["name"]: item["version"] for item in lock["package"] if "version" in item}
    requirements = tools.REQUIREMENTS.read_text(encoding="utf-8")
    packages = {}
    for line in requirements.splitlines():
        if re.match(r"^[A-Za-z]", line):
            name, version = line.split("==", 1)
            version = version.split()[0]
            assert versions[name] == version
            packages[name] = version
    assert {"click", "pydantic", "pyyaml", "jsonschema"} <= packages.keys()
    assert requirements.count("--hash=sha256:") >= len(packages)
    assert "file:" not in requirements and "C:\\" not in requirements


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_image_installer_uses_verified_archives_and_locked_dependencies(tmp_path, monkeypatch, machine):
    monkeypatch.setattr(tools.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tools.platform, "machine", lambda: machine)
    monkeypatch.setattr(tools.os, "geteuid", lambda: 0, raising=False)
    downloaded, commands = [], []
    def download(url, destination, expected):
        downloaded.append((url, expected))
        destination.write_bytes(b"synthetic verified archive")
        # Match download_verified's private temporary-file publication on POSIX.
        destination.chmod(0o600)
        if os.name == "posix":
            assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    def run(command, **kwargs):
        commands.append((command, kwargs))
    result = tools.install(tmp_path, downloader=download, runner=run)
    profile = tools.tool_inputs()
    assert result["status"] == "passed" and result["architecture"] == machine
    assert downloaded[0] == (profile["npm"]["url"], profile["npm"]["sha256"])
    assert downloaded[1][1] == profile["appimagetool"]["sha256"][machine]
    runtime = json.loads(tools.RUNTIME.read_text(encoding="utf-8"))
    assert downloaded[2][1] == runtime["sha256"][machine]
    assert "--require-hashes" in commands[0][0]
    assert "--offline" in commands[1][0] and "--ignore-scripts" in commands[1][0]
    assert all(command[0] == "timeout" and "--kill-after=3s" in command for command, _ in commands)
    assert all(options["env"]["PYYAML_FORCE_LIBYAML"] == "0" for _, options in commands)
    assert not (tmp_path / "npm.tgz").exists()
    assert (tmp_path / "inputs.json").is_file()
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "appimagetool").stat().st_mode) == 0o755
        assert stat.S_IMODE((tmp_path / f"runtime-{machine}").stat().st_mode) == 0o644


def test_complete_recipe_copies_tool_stages_and_runs_verification():
    recipe = (tools.ASSETS / "ci_linux/Dockerfile").read_text(encoding="utf-8")
    assert "FROM ${NODE_IMAGE} AS node-toolchain" in recipe
    assert "--from=node-toolchain /usr/local/bin/node" in recipe
    assert "--from=python-toolchain /etc/ssl/certs" in recipe
    assert "ldconfig" in recipe and "emule_workspace.ci_linux_tools" in recipe
    assert "COPY emule_workspace /opt/emulebb/emule_workspace" in recipe
    assert "COPY --from=node-toolchain /usr/local/LICENSE /opt/emulebb/licenses/Node-LICENSE" in recipe
