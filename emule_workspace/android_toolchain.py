"""Reviewed Android build inputs shared by local WSL/Docker and hosted CI.

This preparation step downloads data only. SDK installation, license handling,
compilation and release signing belong to subsequent explicit operations.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4

from .python_runtime import download_verified, sha256

from .android_inputs import ROLES, tool_inputs


def fetch_tool_inputs() -> Path:
    """Cache exact archives and record verified bytes without running SDK tools."""
    from .config import resolve_required_workspace_roots

    _workspace, output = resolve_required_workspace_roots()
    inputs = tool_inputs()
    cache = output / "cache/android-tool-inputs"
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
    value = {"schema": "emulebb.android-tool-cache/1", "host": inputs["host"], "assets": records}
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
