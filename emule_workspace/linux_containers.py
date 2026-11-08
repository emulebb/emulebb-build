"""Shared native-Linux and Windows-to-WSL Docker execution for Rust packaging."""
from __future__ import annotations

import json
import os
import runpy
from pathlib import Path
import subprocess
import shlex
import shutil
import sys
import tomllib
from .git import git_output
from contextlib import ExitStack
from uuid import uuid4


BINFMT_IMAGE = "docker.io/tonistiigi/binfmt@sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0"
ALPINE_IMAGE = "docker.io/library/alpine:3.24@sha256:3f4d6b32e26703c6082839234f43769d5046b3ac7c910033911d1e71d8cd630a"
RUST_IMAGES = {
    "gnu": "docker.io/library/rust:1.98.1-bookworm@sha256:93ce27a88655056a51dbdd8f5f2d7ddc071c7b0070fb288a37b5a285fc83971e",
    "musl": "docker.io/library/rust:1.98.1-alpine3.24@sha256:7cc1c22d77d9432f7fe012a70e6d3e555af54c2a6832700ed7d553f1769ae89f",
}
MUSL_CROSS_IMAGE = "ghcr.io/rust-cross/rust-musl-cross:aarch64-musl@sha256:02e790506c5f15a533bc05b3af8719b16a8a2314480f6f0fdc0166432035d9b4"
RUNTIME_IMAGES = {"musl": ALPINE_IMAGE, "gnu": "docker.io/library/ubuntu:26.04@sha256:6a06111c3b0a8600feadb270aec771dc239bf407b25625f93f44ab00e11f4d3c"}


def build_configuration(libc: str, architecture: str, host: str) -> tuple[str, str, bool]:
    """@brief Select native CI builds or x64-hosted ARM64 cross compilation."""
    if libc not in RUST_IMAGES or architecture not in {"amd64", "arm64"} or host not in {"amd64", "arm64"}:
        raise ValueError("Expected GNU/musl and supported Linux CPU architectures")
    cross = architecture != host
    if cross and host != "amd64":
        raise RuntimeError("Cross compilation currently requires an AMD64 host")
    return MUSL_CROSS_IMAGE if cross and libc == "musl" else RUST_IMAGES[libc], host, cross


def linux_test_plan(scope: str, *, cross: bool, rust_root: Path | None = None) -> list[tuple[str, list[str]]]:
    """Select executable coverage separately from cross-target compilation."""
    if scope not in {"portable", "full"}:
        raise ValueError("Linux test scope must be portable or full")
    plan = []
    if scope == "full":
        if rust_root is None:
            raise ValueError("Full Linux tests require the candidate Rust quality definitions")
        definitions = runpy.run_path(str(rust_root / "tools/rust_quality_gate.py"))
        for label, factory in (("workspace-tests", "test_workspace_step"), ("vpn-egress-tests", "test_vpn_leak_step")):
            _description, command = definitions[factory]()
            if command[:2] != ["cargo", "test"] or "--locked" not in command:
                raise RuntimeError(f"Canonical {factory} must remain a locked Cargo test command")
            arguments = [value for value in command[2:] if value != "--locked"]
            if cross:
                arguments.insert(arguments.index("--") if "--" in arguments else len(arguments), "--no-run")
            plan.append((label + ("-build" if cross else ""), arguments))
        if not cross:
            return plan
    plan.extend([
        ("native-library-tests", ["--lib", *[argument for package in (
            "emulebb-metadata", "emulebb-miniupnpc", "emulebb-pcpnatpmp", "emulebb-kad-proto",
            "emulebb-kad-routing", "emulebb-index", "emulebb-settings", "emulebb-diagnostics", "emulebb-tools",
        ) for argument in ("-p", package)]]),
        ("tracker-tests", ["--lib", "-p", "emulebb-kad-net", "tracker::tests"]),
        ("durability-tests", ["--lib", "-p", "emulebb-ed2k", "fs_durability::tests"]),
    ])
    return plan


def linux_quality_plan(rust_root: Path, target: str) -> list[tuple[str, list[str]]]:
    """Compile target-specific lint and diagnostics from the candidate CI gate."""
    definitions = runpy.run_path(str(rust_root / "tools/rust_quality_gate.py"))
    plan = []
    for label, factory, subcommand in (("clippy", "clippy_step", "clippy"),
                                        ("packet-diagnostics", "diagnostics_step", "check")):
        _description, command = definitions[factory]()
        if command[:2] != ["cargo", subcommand] or "--locked" not in command:
            raise RuntimeError(f"Canonical {factory} must remain a locked Cargo {subcommand} command")
        arguments = list(command[1:])
        # WHY: target selection belongs to Cargo, before the rustc/Clippy flags
        # separator. Passing it after -- would change the compiler invocation.
        index = arguments.index("--") if "--" in arguments else len(arguments)
        arguments[index:index] = ["--target", target]
        plan.append((label, arguments))
    return plan


def run_owned_container(docker, arguments: list[str], log: Path, *, timeout: int, append: bool = False) -> None:
    """@brief Retain failure output and stop only this invocation's container on interruption."""
    name = f"emulebb-linux-{uuid4().hex}"
    command = [*docker.prefix, "docker", "run", "--name", name, *arguments]
    try:
        with log.open("a" if append else "w", encoding="utf-8") as stream:
            subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Linux container check failed; inspect {log}") from exc
    finally:
        # WHY: terminating wsl.exe does not reliably terminate its Docker job.
        subprocess.run([*docker.prefix, "docker", "rm", "--force", name], capture_output=True, timeout=60)


class LinuxDocker:
    """@brief Execute Docker on Linux or inside the explicitly selected WSL distro."""

    def __init__(self, distro: str = "Ubuntu") -> None:
        if not distro.strip():
            raise ValueError("WSL distribution must be non-empty")
        self.distro = distro
        self.prefix = ["wsl.exe", "-d", distro, "--"] if os.name == "nt" else []
        self.cleanup = ExitStack()
        self.aliases: list[tuple[Path, str]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.cleanup.__exit__(*args)

    def path(self, path: Path) -> str:
        """@brief Translate an existing operator path without changing its environment."""
        if not self.prefix:
            return str(path.resolve())
        resolved = path.resolve()
        for host, mount in self.aliases:
            if resolved.is_relative_to(host):
                return mount + "/" + resolved.relative_to(host).as_posix()
        translated = subprocess.check_output(
            [*self.prefix, "wslpath", "-a", "-u", path.resolve().as_posix()], text=True,
        ).strip()
        if subprocess.run([*self.prefix, "test", "-d", translated], capture_output=True).returncode == 0:
            return translated
        # WHY: DrvFS cannot traverse every Windows volume mount point. Mount the
        # exact existing directory, as the profile-import harness does; keep the
        # operator root unchanged and remove only this invocation's mount alias.
        mount = f"/mnt/emulebb-build-{uuid4().hex}"
        root = ["wsl.exe", "-d", self.distro, "-u", "root", "--"]
        subprocess.run([*root, "mkdir", mount], check=True)
        self.cleanup.callback(subprocess.run, [*root, "rmdir", mount], check=True)
        subprocess.run([*root, "mount", "-t", "drvfs", "-o", "metadata,uid=1000,gid=1000,umask=022",
                        resolved.as_posix(), mount], check=True)
        self.cleanup.callback(subprocess.run, [*root, "umount", mount], check=True)
        self.aliases.append((resolved, mount))
        print("Mounted an existing operator directory through a temporary WSL alias")
        return mount

    def run(self, *arguments: str, capture: bool = False, timeout: int | None = None):
        """@brief Run a Docker command, propagating missing tools and failed checks."""
        return subprocess.run(
            [*self.prefix, "docker", *arguments], check=True, text=True,
            capture_output=capture, timeout=timeout,
        )

    def preflight(self, output: Path, *, setup_arm64: bool = False) -> dict:
        """@brief Verify both Linux architectures; optionally register pinned ARM64 QEMU."""
        versions = json.loads(self.run("version", "--format", "{{json .}}", capture=True, timeout=30).stdout)
        if setup_arm64 and versions["Server"]["Arch"] != "arm64":
            self.run("run", "--privileged", "--rm", BINFMT_IMAGE, "--install", "arm64", timeout=180)
        builder = self.run("buildx", "inspect", capture=True, timeout=30).stdout
        platforms = []
        for line in builder.splitlines():
            if line.strip().startswith("Platforms:"):
                platforms.extend(value.strip().rstrip("*") for value in line.split(":", 1)[1].split(","))
        missing = {"linux/amd64", "linux/arm64"} - set(platforms)
        if missing:
            raise RuntimeError(f"Docker builder lacks {sorted(missing)}; run linux-containers --setup-arm64")
        # WHY: advertised targets alone do not prove an ARM64 executable runs.
        machine = self.run("run", "--rm", "--network", "none", "--platform", "linux/arm64",
                           ALPINE_IMAGE, "uname", "-m", capture=True, timeout=180).stdout.strip()
        if machine != "aarch64":
            raise RuntimeError(f"ARM64 execution probe returned {machine!r}")
        report = {"docker": versions["Server"]["Version"], "host_arch": versions["Server"]["Arch"],
                  "platforms": platforms, "arm64_execution": machine, "binfmt_image": BINFMT_IMAGE,
                  "alpine_image": ALPINE_IMAGE, "rust_images": RUST_IMAGES}
        path = output / "reports" / "linux-containers" / "preflight.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Linux container preflight passed: {path}")
        return report


def build_linux_artifacts(layout, *, libc: str, architecture: str, webui_dist: Path | None = None,
                          release_version: str | None = None, smoke: bool = False, test_scope: str = "portable") -> Path:
    """@brief Build and inventory one GNU/musl package using pinned Docker inputs.

    Container paths translate valid operator roots; only generated output is
    writable. ARM64 cross-compiles on x64 and runs selected tests under QEMU;
    native CI can execute the full workspace suite with the same recipe.
    """
    with LinuxDocker() as docker:
        archive = _build_linux_artifacts(layout, docker, libc=libc, architecture=architecture,
                                         webui_dist=webui_dist, release_version=release_version, test_scope=test_scope)
    if smoke:
        smoke_linux_archive(layout, archive=archive, libc=libc, architecture=architecture)
    return archive


def smoke_linux_archive(layout, *, archive: Path, libc: str, architecture: str) -> Path:
    """@brief Run the shared two-peer lifecycle harness as a non-root container user."""
    if libc not in RUNTIME_IMAGES or architecture not in {"amd64", "arm64"}:
        raise ValueError("Expected GNU/musl and AMD64/ARM64")
    output = layout.output_root.resolve()
    archive = archive.resolve()
    if not archive.is_relative_to(output) or not archive.is_file():
        raise RuntimeError("Runtime tests require an archive below the operator output root")
    manifest = json.loads(archive.with_name(archive.name + ".manifest.json").read_text(encoding="utf-8"))
    prefix = "emulebb-rust-v"
    suffix = f"-linux-{architecture}-{libc}.tar.zst"
    if not archive.name.startswith(prefix) or not archive.name.endswith(suffix):
        raise RuntimeError("Runtime archive name does not match the selected architecture/libc")
    version = archive.name[len(prefix):-len(suffix)]
    source_sha = git_output(layout.emulebb_rust_repo_root, "rev-parse", "HEAD").strip()
    if (git_output(layout.emulebb_rust_repo_root, "status", "--porcelain").strip()
            or git_output(layout.tests_repo_root, "status", "--porcelain").strip()):
        raise RuntimeError("Runtime validation requires clean Rust and harness sources")
    if manifest.get("sourceDirty") is not False or manifest.get("source", {}).get("emulebbRust", {}).get("commit") != source_sha:
        raise RuntimeError("Runtime archive must match the clean current Rust revision")
    context = Path(__file__).parent / "release_assets/rust_linux"
    runtime = f"local/emulebb-rust-runtime:{libc}-{architecture}"
    reports = output / "reports/rust-linux" / f"{libc}-{architecture}" / uuid4().hex
    reports.mkdir(parents=True)
    (reports / "tmp").mkdir()
    with LinuxDocker() as docker:
        docker.run("build", "--platform", f"linux/{architecture}", "--build-arg", f"BASE_IMAGE={RUNTIME_IMAGES[libc]}",
                   "--file", docker.path(context) + "/Dockerfile.runtime", "--tag", runtime, docker.path(context))
        mounts = ["--mount", f"type=bind,source={docker.path(output)},target=/output",
                  "--mount", f"type=bind,source={docker.path(layout.emulebb_rust_repo_root)},target=/workspace/repos/emulebb-rust,readonly",
                  "--mount", f"type=bind,source={docker.path(layout.tests_repo_root)},target=/workspace/repos/emulebb-build-tests,readonly"]
        relative = reports.relative_to(output).as_posix()
        docker.run("run", "--rm", "--platform", f"linux/{architecture}", *mounts, runtime,
                   "chmod", "0777", f"/output/{relative}", f"/output/{relative}/tmp")
        uid = subprocess.check_output([*docker.prefix, "id", "-u"], text=True).strip()
        gid = subprocess.check_output([*docker.prefix, "id", "-g"], text=True).strip()
        user = f"{uid}:{gid}" if uid != "0" else "65534:65534"
        runtime_uid, runtime_gid = user.split(":")
        # WHY: DrvFS rejects RENAME_NOREPLACE, while native Linux filesystems
        # support the publication invariant. Keep disposable profiles on Linux
        # tmpfs inside the output namespace and retain receipts outside it.
        scratch_mount = f"/output/{relative}/tmp:rw,exec,nosuid,nodev,size=8g,uid={runtime_uid},gid={runtime_gid},mode=0700"
        command = ["--rm", "--platform", f"linux/{architecture}", "--user", user,
                   "--tmpfs", scratch_mount,
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--network", "bridge", *mounts,
                   "--env", "EMULEBB_WORKSPACE_ROOT=/workspace", "--env", "EMULEBB_WORKSPACE_OUTPUT_ROOT=/output",
                   "--env", f"TMPDIR=/output/{relative}/tmp", runtime,
                   "python3", "-B", "/workspace/repos/emulebb-build-tests/scripts/smoke-rust-release-package.py",
                   "--release-dir", "/output/" + archive.parent.relative_to(output).as_posix(),
                   "--release-version", version, "--target-os", "linux", "--platform", "x64" if architecture == "amd64" else "ARM64",
                   "--linux-libc", libc, "--expected-source-sha", source_sha,
                   "--rust-repo", "/workspace/repos/emulebb-rust", "--report-path", f"/output/{relative}/package-smoke.json"]
        run_owned_container(docker, command, reports / "runtime.log", timeout=600)
        host_arch = docker.run("version", "--format", "{{.Server.Arch}}", capture=True).stdout.strip()
        (reports / "runtime-inputs.json").write_text(json.dumps({
            "runtimeBase": RUNTIME_IMAGES[libc], "filesystem": "tmpfs", "user": user,
            "execution": "native" if host_arch == architecture else "qemu",
            "sourceSha": source_sha, "archiveSha256": manifest["sha256"],
            "harnessSha": git_output(layout.tests_repo_root, "rev-parse", "HEAD").strip(),
        }, indent=2) + "\n", encoding="utf-8")
    print(f"Non-root Linux {architecture}/{libc} lifecycle passed: {reports / 'package-smoke.json'}")
    return reports / "package-smoke.json"


def _build_linux_artifacts(layout, docker: LinuxDocker, *, libc: str, architecture: str,
                           webui_dist: Path | None, release_version: str | None, test_scope: str) -> Path:
    from . import release
    if libc not in RUST_IMAGES or architecture not in {"amd64", "arm64"}:
        raise ValueError("Expected gnu/musl and amd64/arm64")
    rust = layout.emulebb_rust_repo_root
    output = layout.output_root.resolve()
    if output.is_relative_to(layout.emule_workspace_root.resolve()):
        raise RuntimeError("Linux output must be outside the source workspace")
    with (rust / "Cargo.toml").open("rb") as stream:
        version = tomllib.load(stream)["workspace"]["package"]["version"]
    if release_version is not None:
        release._assert_emulebb_rust_version_matches(rust, release_version)
        version = release_version
    if not release._is_release_version(version):
        raise RuntimeError("Linux packaging requires a valid release version")
    provenance = release._emulebb_rust_package_provenance(layout, rust)
    with (rust / "rust-toolchain.toml").open("rb") as stream:
        if tomllib.load(stream)["toolchain"]["channel"] != "1.98.1":
            raise RuntimeError("Update the pinned Linux builder images with the Rust toolchain")
    target = f"{'x86_64' if architecture == 'amd64' else 'aarch64'}-unknown-linux-{libc}"
    engine_arch = docker.run("version", "--format", "{{.Server.Arch}}", capture=True).stdout.strip()
    base, build_arch, cross = build_configuration(libc, architecture, engine_arch)
    test_plan = linux_test_plan(test_scope, cross=cross, rust_root=rust)
    quality_plan = linux_quality_plan(rust, target) if test_scope == "full" else []
    builder = f"local/emulebb-rust-builder:{libc}-{architecture}"
    context = Path(__file__).parent / "release_assets" / "rust_linux"
    docker.run("build", "--platform", f"linux/{build_arch}", "--build-arg", f"BASE_IMAGE={base}",
               "--build-arg", f"CROSS_TARGET={target if cross else ''}",
               "--tag", builder, docker.path(context))
    root = output / "packages" / "rust-linux" / version / f"{libc}-{architecture}"
    root.mkdir(parents=True, exist_ok=True)
    log_root = output / "reports" / "rust-linux" / f"{libc}-{architecture}" / uuid4().hex
    log_root.mkdir(parents=True, exist_ok=True)
    cache = output / "cache" / "cargo-linux-registry"
    cache.mkdir(parents=True, exist_ok=True)
    miniupnp, pcpnatpmp = release._emulebb_rust_native_source_roots(layout)
    mounts = [
        "--mount", f"type=bind,source={docker.path(rust)},target=/source,readonly",
        "--mount", f"type=bind,source={docker.path(miniupnp)},target=/native/miniupnp,readonly",
        "--mount", f"type=bind,source={docker.path(pcpnatpmp)},target=/native/pcpnatpmp,readonly",
        "--mount", f"type=bind,source={docker.path(output)},target=/output",
        "--mount", f"type=bind,source={docker.path(cache)},target=/usr/local/cargo/registry",
    ]
    target_root = f"/output/builds/rust/target-linux-{libc}-{architecture}"
    environment = ["--env", f"CARGO_TARGET_DIR={target_root}", "--env", "MINIUPNP_ROOT=/native/miniupnp",
                   "--env", "PCPNATPMP_ROOT=/native/pcpnatpmp", "--env", "CARGO_BUILD_JOBS=4"]
    source_revision = provenance["source"]["emulebbRust"]["commit"]
    hosted = os.environ.get("GITHUB_ACTIONS") == "true"
    channel = os.environ.get("RELEASE_CHANNEL", "candidate") if hosted else "development"
    environment.extend(["--env", f"EMULEBB_RELEASE_VERSION={version}",
                        "--env", f"EMULEBB_SOURCE_REVISION={source_revision}",
                        "--env", f"EMULEBB_RELEASE_CHANNEL={channel}"])
    if cross:
        compiler = "aarch64-linux-gnu" if libc == "gnu" else "aarch64-unknown-linux-musl"
        environment.extend(["--env", f"CARGO_TARGET_{target.upper().replace('-', '_')}_LINKER={compiler}-gcc",
                            "--env", f"CC_{target.replace('-', '_')}={compiler}-gcc",
                            "--env", f"AR_{target.replace('-', '_')}={compiler}-ar"])
        if libc == "gnu":
            environment.extend(["--env", "QEMU_LD_PREFIX=/usr/aarch64-linux-gnu"])
    cargo = ["--rm", "--platform", f"linux/{build_arch}", *mounts, *environment,
             "--workdir", "/source", builder, "cargo"]
    for label, arguments in quality_plan:
        print(f"Checking Linux {architecture}/{libc}: {label}; log: {log_root / (label + '.log')}", flush=True)
        run_owned_container(docker, [*cargo, *arguments], log_root / f"{label}.log", timeout=3600)
    command = ["run", *cargo, "build", "--locked", "--release",
               "--target", target, "-p", "emulebb-daemon", "--bin", "emulebb-rust"]
    log = log_root / "build.log"
    print(f"Building Linux {architecture}/{libc}; log: {log}", flush=True)
    run_owned_container(docker, command[1:], log, timeout=7200)
    tests = ["run", *cargo, "test", "--locked", "--target", target]
    for label, arguments in test_plan:
        print(f"Checking Linux {architecture}/{libc}: {label}; log: {log_root / (label + '.log')}", flush=True)
        run_owned_container(docker, [*tests[1:], *arguments], log_root / f"{label}.log", timeout=3600)
    binary = output / "builds" / "rust" / f"target-linux-{libc}-{architecture}" / target / "release" / "emulebb-rust"
    audit = docker.run("run", "--rm", "--network", "none", "--platform", f"linux/{build_arch}", *mounts,
                       builder, "readelf", "-h", "-l", "-d", f"{target_root}/{target}/release/emulebb-rust", capture=True).stdout
    (log_root / "elf.txt").write_text(audit, encoding="utf-8")
    validate_elf_audit(audit, libc=libc, architecture=architecture)
    tools = docker.run("run", "--rm", "--network", "none", "--platform", f"linux/{build_arch}",
                       builder, "sh", "-ec", "rustc --version; cc --version; "
                       "if command -v apk >/dev/null; then apk info -v; else dpkg-query -W; fi", capture=True).stdout
    (log_root / "toolchain.txt").write_text(tools, encoding="utf-8")
    stage = root / "staging" / "emulebb-rust"
    if not stage.resolve().is_relative_to(output):
        raise RuntimeError("Linux staging must remain below the operator output root")
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, stage / "emulebb-rust")
    if webui_dist is None:
        webui_dist = root / "webui-built"
        subprocess.run([sys.executable, "-B", str(rust / "tools/build_webui.py"), "--stage-dir", str(webui_dist)], check=True)
    if not webui_dist.resolve().is_relative_to(output) or not (webui_dist / "index.html").is_file():
        raise RuntimeError("--webui-dist must contain index.html below the output root")
    if (stage / "webui").exists():
        shutil.rmtree(stage / "webui")
    shutil.copytree(webui_dist, stage / "webui")
    for name in ("LICENSE", "emulebb-rust-settings.example.toml"):
        shutil.copy2(rust / name, stage / name)
    notices = stage / "licenses"
    notices.mkdir()
    shutil.copy2(miniupnp / "miniupnpc" / "LICENSE", notices / "MiniUPnPc-LICENSE")
    shutil.copy2(pcpnatpmp / "COPYING", notices / "libpcpnatpmp-COPYING")
    shutil.copy2(layout.tooling_repo_root / "docs/products/emulebb-rust/RELEASE-SCOPE.md", stage / "RELEASE-SCOPE.md")
    (stage / "README.txt").write_text(
        f"eMuleBB Rust {version}, Linux {architecture} {libc}.\n"
        "Run ./emulebb-rust with a fresh writable profile. The adjacent webui directory is required.\n"
        "Linux kernel and CPU requirements still apply; TLS requires system CA certificates.\n", encoding="utf-8")
    stem = f"emulebb-rust-v{version}-linux-{architecture}-{libc}"
    archive = root / f"{stem}.tar.zst"
    release._write_emulebb_rust_linux_sbom(layout=layout, rust_root=rust, package_root=stage, release_root=root,
                                         asset_name=archive.name, version=version, arch=f"{architecture}-{libc}")
    relative = root.relative_to(output).as_posix()
    docker.run("run", "--rm", "--network", "none", "--platform", f"linux/{build_arch}", *mounts,
               builder, "chmod", "755", f"/output/{relative}/staging/emulebb-rust/emulebb-rust")
    docker.run("run", "--rm", "--network", "none", "--platform", f"linux/{build_arch}", *mounts,
               builder, "tar", "--zstd", "--sort=name", "--owner=0", "--group=0", "--numeric-owner",
               "-cf", f"/output/{relative}/{archive.name}",
               "-C", f"/output/{relative}/staging", "emulebb-rust")
    sbom = root / f"{stem}.sbom.spdx.json"
    shutil.copy2(stage / "SBOM.spdx.json", sbom)
    release._assert_emulebb_rust_package_provenance(layout, rust, provenance)
    manifest = {**provenance, "package": "emulebb-rust", "version": version,
                "baseVersion": release._emulebb_rust_workspace_version(rust),
                "platform": f"linux-{architecture}-{libc}",
                "asset": archive.name, "sha256": release._sha256(archive), "sbom": sbom.name,
                "sbomSha256": release._sha256(sbom), "target": target, "libc": libc,
                "builderBase": base, "elfAudit": audit,
                "channel": channel,
                "buildExecution": "cross" if cross else "native", "libraryTestExecution": "qemu" if cross else "native",
                "testCompilation": "workspace" if test_scope == "full" else "selected-libraries",
                "testExecution": "workspace" if test_scope == "full" and not cross else "selected-libraries",
                "testProfile": "test", "testPlan": test_plan, "qualityPlan": quality_plan,
                "resolvedToolchain": tools,
                "builderRecipeSha256": release._sha256(context / "Dockerfile"),
                "cargoLockSha256": release._sha256(rust / "Cargo.lock"),
                "files": release._staged_package_file_hashes(stage),
                "pathMapping": {"source": "/source", "output": "/output", "cargoTarget": target_root}}
    manifest_path = root / f"{archive.name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    deb_files = ()
    if libc == "gnu":
        deb_files = _package_gnu_deb(layout, docker, root=root, stage=stage, version=version,
                                     architecture=architecture, build_arch=build_arch,
                                     builder=builder, mounts=mounts, manifest=manifest, log_root=log_root)
    release._assert_emulebb_rust_package_provenance(layout, rust, provenance)
    release._write_sha256sums(root / "SHA256SUMS", (archive, manifest_path, sbom, *deb_files))
    print(f"Linux {architecture}/{libc} package: {archive}")
    return archive


def _package_gnu_deb(layout, docker, *, root: Path, stage: Path, version: str, architecture: str,
                     build_arch: str, builder: str, mounts: list[str], manifest: dict, log_root: Path) -> tuple[Path, ...]:
    """Package the exact GNU payload without touching the canonical Windows stage."""
    from . import release
    deb_root = root / "deb"
    if not deb_root.resolve().is_relative_to(layout.output_root.resolve()):
        raise RuntimeError("GNU DEB staging must remain below the operator output root")
    if deb_root.exists():
        shutil.rmtree(deb_root)
    release._stage_emulebb_rust_linux_tree(
        destination=deb_root / "usr", executable=stage / "emulebb-rust", webui=stage / "webui",
        rust_root=layout.emulebb_rust_repo_root, tooling_root=layout.tooling_repo_root,
        version=version, arch=architecture)
    release._write_emulebb_rust_deb_control(deb_root, version=version, arch=architecture,
                                           dependencies="libc6 (>= 2.36), libgcc-s1, ca-certificates")
    docs = deb_root / "usr/share/doc/emulebb-rust"
    shutil.copytree(stage / "licenses", docs / "licenses")
    deb = root / f"emulebb-rust-v{version}-linux-{architecture}.deb"
    release._write_emulebb_rust_linux_sbom(layout=layout, rust_root=layout.emulebb_rust_repo_root,
                                         package_root=deb_root, release_root=root,
                                         asset_name=deb.name, version=version, arch=architecture)
    sbom = deb.with_name(deb.name + ".sbom.spdx.json")
    shutil.copy2(deb_root / "SBOM.spdx.json", sbom)
    (deb_root / "SBOM.spdx.json").replace(docs / "SBOM.spdx.json")
    scratch = root / "native-deb"
    scratch.mkdir(exist_ok=True)
    relative = root.relative_to(layout.output_root.resolve()).as_posix()
    native = f"/output/{relative}/native-deb"
    source = f"/output/{relative}/deb"
    destination = f"/output/{relative}/{deb.name}"
    q = shlex.quote
    script = "\n".join([
        "set -eu", f"cp -a {q(source)}/. {q(native)}/",
        f"find {q(native)} -type d -exec chmod 0755 {{}} +",
        f"find {q(native)} -type f -exec chmod 0644 {{}} +",
        "chmod 0755 " + " ".join(q(native + path) for path in
            ("/usr/bin/emulebb-rust", "/usr/bin/emulebb-rust-launch",
             "/usr/lib/emulebb-rust/emulebb-rust", "/usr/lib/emulebb-rust/launch")),
        f"dpkg-deb --build --root-owner-group {q(native)} {q(destination)}",
    ])
    # DrvFS modes must not enter an installable Linux artifact.
    run_owned_container(docker, ["--rm", "--platform", f"linux/{build_arch}", *mounts,
                                 "--tmpfs", f"{native}:rw,exec,nosuid,nodev,size=256m,mode=0700",
                                 builder, "sh", "-ec", script], log_root / "deb.log", timeout=300)
    deb_manifest = {**manifest, "asset": deb.name, "sha256": release._sha256(deb),
                    "sbom": sbom.name, "sbomSha256": release._sha256(sbom),
                    "executable": "/usr/lib/emulebb-rust/emulebb-rust",
                    "webuiRoot": "/usr/lib/emulebb-rust/webui",
                    "files": release._staged_package_file_hashes(deb_root)}
    manifest_path = deb.with_name(deb.name + ".manifest.json")
    manifest_path.write_text(json.dumps(deb_manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Linux {architecture} GNU DEB: {deb}")
    return deb, manifest_path, sbom


def validate_elf_audit(audit: str, *, libc: str, architecture: str) -> None:
    """@brief Reject wrong-architecture payloads and non-static musl artifacts."""
    machine = "Advanced Micro Devices X86-64" if architecture == "amd64" else "AArch64"
    if machine not in audit:
        raise RuntimeError(f"ELF architecture does not match {architecture}")
    if libc == "musl" and ("INTERP" in audit or "NEEDED" in audit):
        raise RuntimeError("musl portability requires a static binary with no interpreter or shared dependencies")
