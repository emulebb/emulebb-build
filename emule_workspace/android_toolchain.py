"""Reviewed Android build inputs shared by local WSL/Docker and hosted CI.

This preparation step downloads data only. SDK installation, license handling,
compilation and release signing belong to subsequent explicit operations.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4

from .python_runtime import download_verified, sha256

from .android_inputs import ROLES, emulator_inputs, emulator_subset, tool_inputs


def fetch_tool_inputs() -> Path:
    """Cache exact archives and record verified bytes without running SDK tools."""
    return _fetch_inputs(tool_inputs(), "android-tool-inputs", "emulebb.android-tool-cache/1")


def fetch_emulator_inputs(*, images: tuple[str, ...] = ()) -> Path:
    """Fetch only requested qualification images; ordinary local builds need none."""
    selected = emulator_subset(emulator_inputs(), images)
    return _fetch_inputs(selected, "android-emulator-inputs", "emulebb.android-emulator-cache/1")


def _fetch_inputs(inputs: dict, cache_name: str, schema: str) -> Path:
    from .config import resolve_required_workspace_roots

    _workspace, output = resolve_required_workspace_roots()
    cache = output / "cache" / cache_name
    if not cache.resolve().is_relative_to(output):
        raise RuntimeError("Android tool cache must remain within the inherited output root")
    records = {}
    for role, item in sorted(inputs["assets"].items()):
        filename = urlsplit(item["url"]).path.rsplit("/", 1)[-1]
        target = cache / item["sha256"] / filename
        if target.is_symlink() or not target.resolve().is_relative_to(cache.resolve()):
            raise RuntimeError("Android input cache cannot use external paths or symlink archives")
        download_verified(item["url"], target, item["sha256"])
        # WHY: the receipt describes actual cache bytes, including cache hits;
        # it is never an SDK installation or release-provenance claim.
        if not target.is_file() or sha256(target) != item["sha256"]:
            raise RuntimeError(f"Android input checksum mismatch: {role}")
        records[role] = {**item, "path": target.relative_to(output).as_posix(), "size": target.stat().st_size}
    receipt = cache / "verified-inputs.json"
    if receipt.is_symlink():
        raise RuntimeError("Android input receipt cannot replace a symlink")
    value = {"schema": schema, "host": inputs["host"], "assets": records}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, receipt)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return receipt


def prepare_toolchain(layout, *, wsl_distro: str = "Ubuntu") -> Path:
    """Build and probe one reviewed native Android toolchain through Linux/WSL Docker."""
    from .config import resolve_required_workspace_roots
    from .linux_containers import LinuxDocker, _builder_user, run_owned_container

    output = layout.output_root.resolve()
    _workspace, inherited_output = resolve_required_workspace_roots()
    if output != inherited_output:
        raise RuntimeError("Android toolchain output must match the inherited operator root")
    cache = fetch_tool_inputs().parent
    root = Path(__file__).resolve().parents[1]
    image = "local/emulebb-android-toolchain:reviewed"
    report = output / "reports/android-toolchain" / uuid4().hex / "toolchain-probe.json"
    report.parent.mkdir(parents=True)
    log = report.with_suffix(".log")
    with LinuxDocker(wsl_distro) as docker:
        arch = docker.run("version", "--format", "{{.Server.Arch}}", capture=True, timeout=30).stdout.strip()
        if arch != "amd64":
            raise RuntimeError("The reviewed Android SDK/NDK archives require native AMD64 Docker")
        user = _builder_user(docker)
        if user.split(":")[0] == "0":
            raise RuntimeError("Run Android validation as a non-root Linux/WSL operator")
        context = docker.path(root)
        docker.run("build", "--platform", "linux/amd64", "--build-context", f"android-inputs={docker.path(cache)}",
                   "--file", context + "/emule_workspace/release_assets/android/Dockerfile",
                   "--tag", image, context, timeout=1200)
        run_owned_container(docker, ["--rm", "--platform", "linux/amd64", "--user", user,
                             "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                             # The pinned CLI unpacks its embedded JVM here;
                             # its shared libraries require executable mappings.
                             "--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777",
                             "--env", "HOME=/tmp/home", "--env", "GRADLE_USER_HOME=/tmp/gradle",
                             image, "python3", "-B", "-m", "emule_workspace.android_probe"], log, timeout=300)
    probe = json.loads(log.read_text(encoding="utf-8"))
    if probe.get("schema") != "emulebb.android-toolchain-probe/1" or probe.get("inputs") != tool_inputs():
        raise RuntimeError("Android toolchain probe returned inconsistent input identity")
    report.write_text(json.dumps(probe, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def prepare_emulator_toolchain(layout, *, images: tuple[str, ...] = (), wsl_distro: str = "Ubuntu") -> Path:
    """Install and execute pinned emulator tools offline; guest boot remains separate."""
    from .config import resolve_required_workspace_roots
    from .linux_containers import LinuxDocker, _builder_user, run_owned_container

    inputs = emulator_subset(emulator_inputs(), images)
    _workspace, output = resolve_required_workspace_roots()
    if layout.output_root.resolve() != output:
        raise RuntimeError("Android emulator output must match the inherited operator root")
    base_report = prepare_toolchain(layout, wsl_distro=wsl_distro)
    cache = fetch_emulator_inputs(images=images).parent
    report = output / "reports/android-emulator" / uuid4().hex / "toolchain-probe.json"
    report.parent.mkdir(parents=True)
    build_log = report.parent / "build.log"
    log = report.with_suffix(".log")
    image = "local/emulebb-android-emulator:" + ("-".join(sorted(images)) or "tools")
    root = Path(__file__).resolve().parents[1]
    with LinuxDocker(wsl_distro) as docker:
        arch = docker.run("version", "--format", "{{.Server.Arch}}", capture=True, timeout=30).stdout.strip()
        if arch != "amd64":
            raise RuntimeError("Reviewed Android emulator tools require native AMD64 Docker")
        user = _builder_user(docker)
        if user.split(":")[0] == "0":
            raise RuntimeError("Run Android emulator probes as a non-root Linux/WSL operator")
        context = docker.path(root)
        command = [*docker.prefix, "docker", "build", "--platform", "linux/amd64",
                   "--build-context", f"android-emulator-inputs={docker.path(cache)}",
                   "--build-arg", "ANDROID_SYSTEM_IMAGES=" + ",".join(sorted(images)),
                   "--file", context + "/emule_workspace/release_assets/android/Dockerfile.emulator",
                   "--tag", image, context]
        print(f"Building Android emulator tools; log: {build_log}", flush=True)
        try:
            with build_log.open("w", encoding="utf-8") as stream:
                subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=1200)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"Android emulator image build failed; inspect {build_log}") from error
        image_id = docker.run("image", "inspect", "--format", "{{.Id}}", image, capture=True, timeout=30).stdout.strip()
        if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
            raise RuntimeError("Android emulator image requires a content identity")
        run_owned_container(docker, ["--rm", "--platform", "linux/amd64", "--user", user,
                             "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                             "--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777",
                             "--env", "HOME=/tmp/home", image_id,
                             "python3", "-B", "-m", "emule_workspace.android_probe", "--emulator"], log, timeout=120)
    probe = json.loads(log.read_text(encoding="utf-8"))
    if (probe.get("schema") != "emulebb.android-emulator-toolchain-probe/1" or probe.get("inputs") != inputs
            or probe.get("runtimeExecution") != "not-run"):
        raise RuntimeError("Android emulator probe returned inconsistent input identity")
    value = {**probe, "image": {"tag": image, "id": image_id}, "toolchainReport": str(base_report)}
    report.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _guest_sources(layout) -> dict:
    """Record only clean contributing revisions, without private remotes or paths."""
    from .git import git_output

    result = {}
    for name, path in (("build", Path(__file__).resolve().parents[1]), ("harness", layout.tests_repo_root)):
        if git_output(path, "status", "--porcelain", "--untracked-files=all").strip():
            raise RuntimeError("Android guest boot requires clean build and harness checkouts")
        commit = git_output(path, "rev-parse", "HEAD").strip()
        if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            raise RuntimeError("Android guest boot requires full contributing source revisions")
        result[name] = {"commit": commit}
    return result


def _kvm_group(docker) -> str:
    """Preflight the selected Linux host before fetching or building guest inputs."""
    available = subprocess.run([*docker.prefix, "test", "-c", "/dev/kvm"], capture_output=True, timeout=30)
    if available.returncode:
        raise RuntimeError("Android guest boot requires /dev/kvm on the selected Linux/WSL host; use GitHub KVM runners")
    group = subprocess.check_output([*docker.prefix, "stat", "--format=%g", "/dev/kvm"], text=True, timeout=30).strip()
    if re.fullmatch(r"0|[1-9][0-9]*", group) is None:
        raise RuntimeError("Android KVM device requires a numeric group identity")
    return group


def probe_emulator_guest(layout, *, image: str, wsl_distro: str = "Ubuntu") -> Path:
    """Run the shared guest boot harness locally or on hosted KVM, without an APK."""
    from .android_install import emulator_layout
    from .config import resolve_required_workspace_roots
    from .linux_containers import LinuxDocker, _builder_user, run_owned_container
    from .release import _emulebb_rust_artifact_origin

    inputs = emulator_subset(emulator_inputs(), (image,))
    workspace, output = resolve_required_workspace_roots()
    if layout.output_root.resolve() != output:
        raise RuntimeError("Android guest output must match the inherited operator root")
    harness = layout.tests_repo_root.resolve()
    if not harness.is_relative_to(workspace) or not (harness / "scripts/probe-android-emulator.py").is_file():
        raise RuntimeError("Android guest boot requires the shared harness below the workspace root")
    with LinuxDocker(wsl_distro) as docker:
        group = _kvm_group(docker)
        if docker.run("version", "--format", "{{.Server.Arch}}", capture=True, timeout=30).stdout.strip() != "amd64":
            raise RuntimeError("Android guest boot requires native AMD64 Docker")
        user = _builder_user(docker)
        if user.split(":")[0] == "0":
            raise RuntimeError("Android guest boot requires a non-root operator")
    source = _guest_sources(layout)
    tool_report = prepare_emulator_toolchain(layout, images=(image,), wsl_distro=wsl_distro)
    tools = json.loads(tool_report.read_text(encoding="utf-8"))
    image_id = tools.get("image", {}).get("id", "")
    if (re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None or tools.get("inputs") != inputs
            or tools.get("runtimeExecution") != "not-run"):
        raise RuntimeError("Android guest boot requires the exact verified emulator tool image")
    role = "system-image-" + image
    image_path = emulator_layout(inputs)[role][1]
    api = int(inputs["assets"][role]["version"].split(".")[0].split("-")[0])
    page_size = 4096 if image == "min" else 16384
    directory = output / "reports/android-emulator-boot" / uuid4().hex
    if not directory.resolve().is_relative_to(output):
        raise RuntimeError("Android boot evidence must remain below the output root")
    directory.mkdir(parents=True, mode=0o700)
    report = directory / "validation.json"
    value = {"schema": "emulebb.android.guest-validation/1", "status": "failed", "scope": "guest-boot",
             "applicationQualification": "not-run", "source": source, "inputs": inputs,
             "imageId": image_id, "selectedGuest": image, **_emulebb_rust_artifact_origin()}
    try:
        if _guest_sources(layout) != source:
            raise RuntimeError("Android guest boot sources changed during tool preparation")
        with LinuxDocker(wsl_distro) as docker:
            # WHY: only this fresh run directory is writable. ADB has no host
            # ports, USB access, Docker socket, credentials or shared server.
            run_owned_container(docker, ["--rm", "--platform", "linux/amd64", "--user", user,
                "--device", "/dev/kvm", "--group-add", group, "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777",
                "--env", "HOME=/tmp/home", "--mount", f"type=bind,source={docker.path(directory)},target=/output",
                "--mount", f"type=bind,source={docker.path(harness)},target=/harness,readonly",
                image_id, "python3", "-B", "/harness/scripts/probe-android-emulator.py",
                "--sdk-root", "/opt/android-emulator", "--image-dir", "/opt/android-emulator/" + image_path,
                "--api", str(api), "--page-size", str(page_size), "--output-root", "/output",
                "--report-dir", "/output/guest"], directory / "container.log", timeout=480)
        guest = json.loads((directory / "guest/report.json").read_text(encoding="utf-8"))
        expected = {"sdk": api, "codename": "REL", "machine": "x86_64", "pageSize": page_size, "emulator": True}
        facts = guest.get("guest")
        if (guest.get("schema") != "emulebb.android.emulator-boot/1" or guest.get("status") != "passed"
                or guest.get("scope") != "guest-boot" or guest.get("applicationQualification") != "not-run"
                or guest.get("kvm") is not True or not isinstance(facts, dict)
                or any(facts.get(key) != item for key, item in expected.items())
                or not isinstance(facts.get("supported64BitAbis"), list)
                or "x86_64" not in facts["supported64BitAbis"]):
            raise RuntimeError("Android boot harness did not prove the requested stable guest")
        if _guest_sources(layout) != source:
            raise RuntimeError("Android guest boot sources changed during qualification")
        # Publish an allowlisted result only; private AVDs and emulator logs can
        # contain local authentication material and must never be uploaded.
        value.update(status="passed", guest=expected, kvm=True)
    except BaseException as error:
        value["failureType"] = type(error).__name__
        raise
    finally:
        report.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report
