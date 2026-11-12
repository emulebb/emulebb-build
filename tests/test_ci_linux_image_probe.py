"""Compiler images must execute their real metadata validator prerequisites."""

import subprocess
import json
from pathlib import Path
from types import SimpleNamespace
import shutil
import xml.etree.ElementTree as ET

import pytest

from emule_workspace import ci_linux_image_probe


@pytest.mark.parametrize("failure", [None, "desktop-file-validate", "appstreamcli"])
def test_metadata_probe_requires_both_tools_to_succeed(tmp_path, monkeypatch, failure):
    calls = []
    def run(command, **options):
        calls.append(command)
        assert options == {"check": True, "capture_output": True, "timeout": 20}
        if command[0] == failure:
            raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(ci_linux_image_probe.subprocess, "run", run)
    if failure:
        with pytest.raises(subprocess.CalledProcessError):
            ci_linux_image_probe.metadata_tools_probe(tmp_path)
    else:
        assert ci_linux_image_probe.metadata_tools_probe(tmp_path) == {
            "desktopFileValidation": "passed", "appstreamValidation": "passed"}
    assert calls[0][0] == "desktop-file-validate"
    if failure != "desktop-file-validate":
        assert calls[1][:3] == ["appstreamcli", "validate", "--no-net"]
    assert ET.parse(tmp_path / "org.example.synthetic-probe.metainfo.xml").getroot().attrib["type"] == "generic"


@pytest.mark.parametrize("failure", [None, "missing-file", "no-image", "runtime", "extraction", "payload"])
def test_appimage_probe_assembles_then_verifies_extracted_payload(tmp_path, monkeypatch, failure):
    native = tmp_path / "native-true"
    native.write_bytes(b"synthetic native executable bytes")
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"synthetic pinned runtime")
    tool = tmp_path / "appimagetool"
    tool.write_bytes(b"synthetic verified tool")
    calls = []
    monkeypatch.setattr(ci_linux_image_probe.shutil, "which", lambda name: str(native) if name == "true" else None)
    def run(command, **options):
        calls.append((command, options))
        if command[0] == "timeout":
            assert command[:5] == ("timeout", "--signal=TERM", "--kill-after=3s", "45s", str(tool))
            assert command[5:7] == ("--runtime-file", str(runtime))
            assert options == {"check": False, "text": True, "capture_output": True}
            if failure == "missing-file":
                return SimpleNamespace(returncode=1, stdout="", stderr="file command is missing but required")
            if failure != "no-image":
                Path(command[-1]).write_bytes(runtime.read_bytes() + b"squashfs")
            if failure == "runtime":
                runtime.write_bytes(b"changed pinned runtime input")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        assert command[1:] == ["--appimage-extract"]
        assert options == {"cwd": tmp_path / "extracted", "check": True, "capture_output": True, "timeout": 30}
        if failure == "extraction":
            raise subprocess.CalledProcessError(1, command)
        shutil.copytree(tmp_path / "Synthetic.AppDir", tmp_path / "extracted/squashfs-root")
        if failure == "payload":
            (tmp_path / "extracted/squashfs-root/synthetic-payload.txt").write_bytes(b"wrong payload")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(ci_linux_image_probe.subprocess, "run", run)
    if failure:
        expected = subprocess.CalledProcessError if failure == "extraction" else RuntimeError
        with pytest.raises(expected):
            ci_linux_image_probe.appimage_tools_probe(tmp_path, tool, runtime)
    else:
        proof = ci_linux_image_probe.appimage_tools_probe(tmp_path, tool, runtime)
        assert proof["nonrootAppImagePackaging"] == "passed" and proof["appimageExtraction"] == "no-fuse"
        assert proof["appimagePayloadFiles"] == ["AppRun", "synthetic-payload.txt", "synthetic-probe.desktop", "synthetic-probe.svg"]
    assert len(calls) == (1 if failure in {"missing-file", "no-image", "runtime"} else 2)


def test_appimage_native_and_prepared_profiles_install_the_external_file_command():
    from emule_workspace.ubuntu_ci import PROFILES
    for name in ("rust-native-release", "rust-prepared-builder"):
        assert "file" in PROFILES[name]


def docker_conformance(architecture: str, report: Path) -> None:
    """Explicit hosted native recipe proof; ordinary pytest never builds Docker images."""
    from emule_workspace import ci_linux_images as images
    from emule_workspace import prepared_native_ci as prepared
    from emule_workspace.linux_containers import LinuxDocker, run_owned_container
    native = {"x86_64": "amd64", "aarch64": "arm64"}.get(prepared.platform.machine().lower())
    if prepared.platform.system() != "Linux" or native != architecture:
        raise RuntimeError("Prepared AppImage conformance requires the actual native Linux CPU")
    if not report.is_absolute() or report.is_symlink() or report.resolve().is_relative_to(images.ROOT):
        raise RuntimeError("Prepared AppImage conformance evidence must stay outside the source checkout")
    report.parent.mkdir(parents=True, exist_ok=True)
    sources = prepared.snapshot({"build": images.ROOT})
    source_sha = sources["build"]
    image = "emulebb-prepared-conformance:" + source_sha + "-" + architecture
    value = {"schema": "emulebb.prepared-image-conformance/1", "status": "running",
             "sourceSha": source_sha, "architecture": architecture, "localImage": image,
             "materials": images.material_hashes(), "bases": images.prepared_bases()}
    try:
        with LinuxDocker() as docker:
            server = json.loads(docker.run("version", "--format", "{{json .Server}}", capture=True, timeout=30).stdout)
            value["docker"] = {name: server.get(name) for name in ("Version", "Arch", "Os")}
            if value["docker"] != {"Version": "29.6.1", "Arch": architecture, "Os": "linux"}:
                raise RuntimeError("Prepared AppImage conformance requires pinned native Linux Docker 29.6.1")
            arguments = images.native_build_arguments(architecture, report.with_suffix(".buildx.json"), source_sha)
            # Local loading is a recipe conformance proof, never a producer or
            # attestation. Only the trusted-main publisher selects registry output.
            docker.run(*arguments[1:], "--provenance=false", "--load", "--tag", image, str(images.ROOT), timeout=1800)
            raw = run_owned_container(docker, [
                "--rm", "--platform", "linux/" + architecture, "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "10000:10000",
                "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=1g,mode=1777",
                "--env", "HOME=/tmp", "--env", "NPM_CONFIG_CACHE=/tmp/npm", image,
                "python3", "-B", "-m", "emule_workspace.ci_linux_image_probe"],
                report.with_suffix(".log"), timeout=120, capture=True)
            proof = json.loads(raw)
            value["probe"] = proof
            tools = proof.get("packagingTools", {})
            if (proof.get("status") != "passed" or proof.get("architecture") != architecture
                    or proof.get("uid") != 10000 or tools.get("nonrootAppImagePackaging") != "passed"
                    or tools.get("appimageExtraction") != "no-fuse"):
                raise RuntimeError("Prepared native AppImage assembly/extraction proof did not pass")
        if prepared.snapshot({"build": images.ROOT}) != sources:
            raise RuntimeError("Prepared AppImage conformance source changed during execution")
        value["status"] = "passed"
    except Exception as error:
        value["status"] = "failed"
        value["failureType"] = type(error).__name__
        raise
    finally:
        report.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("failure", [None, "missing-assembly-proof", "wrong-uid", "source-change"])
def test_hosted_conformance_loads_local_recipe_and_requires_actual_nonroot_probe(tmp_path, monkeypatch, failure):
    from emule_workspace import ci_linux_images as images
    from emule_workspace import linux_containers, prepared_native_ci as prepared
    monkeypatch.setattr(prepared.platform, "system", lambda: "Linux")
    monkeypatch.setattr(prepared.platform, "machine", lambda: "x86_64")
    snapshots = iter([{"build": "a" * 40}, {"build": ("b" if failure == "source-change" else "a") * 40}])
    monkeypatch.setattr(prepared, "snapshot", lambda roots: next(snapshots))
    calls = []
    class Docker:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def run(self, *arguments, **options):
            calls.append((arguments, options))
            return SimpleNamespace(stdout=json.dumps({"Version": "29.6.1", "Arch": "amd64", "Os": "linux"}))
    def probe(docker, arguments, log, **options):
        calls.append((arguments, options))
        return json.dumps({"status": "passed", "architecture": "amd64",
                           "uid": 0 if failure == "wrong-uid" else 10000,
                           "packagingTools": {} if failure == "missing-assembly-proof" else {
                               "nonrootAppImagePackaging": "passed", "appimageExtraction": "no-fuse"}})
    monkeypatch.setattr(linux_containers, "LinuxDocker", Docker)
    monkeypatch.setattr(linux_containers, "run_owned_container", probe)
    report = tmp_path / "conformance.json"
    if failure:
        with pytest.raises(RuntimeError):
            docker_conformance("amd64", report)
    else:
        docker_conformance("amd64", report)
    value = json.loads(report.read_text())
    assert value["status"] == ("failed" if failure else "passed")
    build, build_options = calls[1]
    assert build[:2] == ("buildx", "build")
    assert "--load" in build and "--provenance=false" in build and "--push" not in build
    assert not any("push-by-digest" in item for item in build)
    assert build[build.index("--tag") + 1] == "emulebb-prepared-conformance:" + "a" * 40 + "-amd64"
    assert build_options == {"timeout": 1800}
    native, options = calls[2]
    for option, expected in (("--network", "none"), ("--user", "10000:10000"), ("--cap-drop", "ALL")):
        assert native[native.index(option) + 1] == expected
    assert "--read-only" in native and "--tmpfs" in native
    assert native[-3:] == ["-B", "-m", "emule_workspace.ci_linux_image_probe"]
    assert options == {"timeout": 120, "capture": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Explicit native prepared-image conformance")
    parser.add_argument("--docker-conformance", action="store_true", required=True)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()
    docker_conformance(arguments.architecture, arguments.report)
