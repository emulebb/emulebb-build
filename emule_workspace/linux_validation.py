"""Batch available Linux CI locally while naming required native GitHub proof."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tomllib
from uuid import uuid4

from .git import git_output
from .image_security import validate_image
from .linux_containers import BINFMT_IMAGE, LinuxDocker, build_linux_artifacts
from .release import build_emulebb_rust_image_local
from .rust_images import collect_linux_packages, IMAGE_VARIANTS


def validation_plan(scope: str, host: str) -> list[dict]:
    """Separate available builds, emulated libraries, and native runtime proof."""
    if scope not in {"musl", "full", "image"} or host not in {"amd64", "arm64"}:
        raise ValueError("Expected musl/full/image scope and an AMD64/ARM64 Docker host")
    if scope == "image":
        return []
    targets = [("musl", host)] if scope == "musl" else [(libc, arch) for libc in ("gnu", "musl") for arch in ("amd64", "arm64")]
    plan = []
    for libc, arch in targets:
        available = host == arch or host == "amd64"
        plan.append({"libc": libc, "architecture": arch, "buildAvailable": available,
                     "testScope": "full",
                     "runtime": "native" if arch == host else "requires-native-github",
                     "runner": "ubuntu-24.04" if arch == "amd64" else "ubuntu-24.04-arm"})
    return plan


def _validate_image_targets(layout, *, version: str, architectures: tuple[str, ...], variant: str) -> dict:
    """Verify current package bytes, then build and check a fresh image."""
    image = f"local/emulebb-rust:{version}-{variant}"
    try:
        assets = collect_linux_packages(layout, version=version, architectures=architectures, variant=variant)
    except (OSError, ValueError, RuntimeError) as error:
        raise RuntimeError(f"{variant} image validation requires verified packages for the current clean revision; "
                           "run validate-emulebb-rust-linux --scope full to rebuild them") from error
    archive = build_emulebb_rust_image_local(rust_root=layout.emulebb_rust_repo_root,
                                            output_root=layout.output_root.resolve(), assets_dir=assets,
                                            version=version, image=image, wsl_distro="Ubuntu", architectures=architectures,
                                            variant=variant)
    evidence = validate_image(layout, archive=archive, image=image, architectures=architectures)
    return {"status": "passed", "architectures": architectures, "securityEvidence": str(evidence)}


def validate_linux(layout, *, scope: str) -> Path:
    """Run fresh local gates once per target; do not count delegation as a pass."""
    rust, output = layout.emulebb_rust_repo_root, layout.output_root.resolve()
    for repo in (rust, layout.tests_repo_root):
        if git_output(repo, "status", "--porcelain").strip():
            raise RuntimeError("Local Linux CI requires clean Rust and harness sources")
    with LinuxDocker() as docker:
        host = docker.run("version", "--format", "{{.Server.Arch}}", capture=True, timeout=30).stdout.strip()
        plan = validation_plan(scope, host)
        if scope in {"full", "image"} and host == "amd64":
            docker.preflight(output)
    version = tomllib.loads((rust / "Cargo.toml").read_text(encoding="utf-8"))["workspace"]["package"]["version"]
    root = output / "reports/local-linux-ci" / uuid4().hex
    root.mkdir(parents=True)
    path = root / "results.json"
    report = {"schema": "emulebb.rust.local-linux-ci/1", "status": "running", "scope": scope,
              "coverage": "image-only" if scope == "image" else "available-local-checks", "hostArchitecture": host,
              "sourceSha": git_output(rust, "rev-parse", "HEAD").strip(), "binfmtImage": BINFMT_IMAGE,
              "builderSha": git_output(layout.build_repo_root, "rev-parse", "HEAD").strip(),
              "harnessSha": git_output(layout.tests_repo_root, "rev-parse", "HEAD").strip(),
              "targets": [], "githubRequired": []}
    failures = []
    print(f"Local Linux CI ({scope}); evidence: {root}", flush=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    try:
        if scope == "image":
            arches = tuple(target["architecture"] for target in validation_plan("full", host)
                           if target["libc"] == "gnu" and target["buildAvailable"])
            report["images"] = {variant: _validate_image_targets(layout, version=version, architectures=arches,
                                                                variant=variant) for variant in IMAGE_VARIANTS}
            report["status"] = "passed"
            return path
        webui = root / "webui"
        with (root / "webui-build.log").open("w", encoding="utf-8") as log:
            subprocess.run([sys.executable, "-B", str(rust / "tools/build_webui.py"), "--stage-dir", str(webui)],
                           stdout=log, stderr=subprocess.STDOUT, check=True, timeout=600)
        for target in plan:
            entry = {**target, "build": "delegated" if not target["buildAvailable"] else "running"}
            report["targets"].append(entry)
            path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            if target["runtime"] != "native":
                reason = "Pinned QEMU user-mode lacks SO_BINDTOIFINDEX; network runtime requires a native runner"
                if not target["buildAvailable"]:
                    reason = "This host has no supported cross compiler for the target CPU"
                entry["delegationReason"] = reason
                report["githubRequired"].append({"libc": target["libc"], "architecture": target["architecture"],
                                                 "runner": target["runner"], "reason": reason})
            if not target["buildAvailable"]:
                continue
            try:
                archive = build_linux_artifacts(layout, libc=target["libc"], architecture=target["architecture"],
                                                webui_dist=webui, release_version=version,
                                                smoke=target["runtime"] == "native", test_scope=target["testScope"])
                entry.update(build="passed", archive=archive.name,
                             testsExecuted="workspace" if target["runtime"] == "native" else "portable-libraries-and-durability",
                             runtimeResult="passed" if target["runtime"] == "native" else "delegated")
                print(f"PASSED Linux {target['libc']}/{target['architecture']}; network runtime: {entry['runtimeResult']}", flush=True)
            except Exception as error:
                entry.update(build="failed", error=str(error))
                failures.append(f"{target['libc']}/{target['architecture']}: {error}")
                print(f"FAILED Linux {failures[-1]}", flush=True)
            finally:
                path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        available = [entry for entry in report["targets"] if entry["buildAvailable"]]
        if scope == "full" and available and all(entry["build"] == "passed" for entry in available):
            arches = tuple(entry["architecture"] for entry in available if entry["libc"] == "gnu")
            report["images"] = {variant: _validate_image_targets(layout, version=version, architectures=arches,
                                                                variant=variant) for variant in IMAGE_VARIANTS}
        elif scope == "full":
            # WHY: a successful subset must not look like proof of the required
            # image CPU/libc set when another package gate failed.
            report["images"] = {variant: {"status": "not-run", "reason": "Required available package gates did not all pass"}
                                for variant in IMAGE_VARIANTS}
        if failures:
            raise RuntimeError("Local Linux gates failed: " + ", ".join(failures))
        report["status"] = "passed"
    except BaseException as error:
        report.update(status="failed", error=str(error))
        raise RuntimeError(f"Local Linux CI failed: {error}; inspect {root}") from error
    finally:
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path
