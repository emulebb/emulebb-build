"""Shared native-Linux and Windows-to-WSL Docker execution for Rust packaging."""
from __future__ import annotations

import json
import hashlib
import importlib
import inspect
import os
import platform
import re
import runpy
from pathlib import Path
import subprocess
import shlex
import shutil
import sys
import tomllib
import time
from .git import git_output
from contextlib import ExitStack
from uuid import UUID, uuid4
from .ci_image_sources import UPSTREAM_ALPINE, UPSTREAM_RUST, UPSTREAM_RUNTIME, select_image


BINFMT_IMAGE = "docker.io/tonistiigi/binfmt@sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0"
ALPINE_IMAGE = UPSTREAM_ALPINE
RUST_IMAGES = dict(UPSTREAM_RUST)
MUSL_CROSS_IMAGE = "ghcr.io/rust-cross/rust-musl-cross:aarch64-musl@sha256:02e790506c5f15a533bc05b3af8719b16a8a2314480f6f0fdc0166432035d9b4"
RUNTIME_IMAGES = dict(UPSTREAM_RUNTIME)
_WSL_STOP_TIMEOUT = 10


def _hosted_linux_environment(architecture: str, engine_arch: str) -> list[str]:
    """Require native hosted execution and forward only public run identity."""
    from .release import _emulebb_rust_artifact_origin

    origin = _emulebb_rust_artifact_origin()
    if origin["origin"] != "github-actions":
        return []
    native = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine().lower())
    if platform.system() != "Linux" or native is None or not architecture == engine_arch == native:
        raise RuntimeError("Hosted Linux builds and runtime checks require matching native runner, Docker, and target CPUs")
    context = origin["workflow"]
    values = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": context["repository"],
              "GITHUB_RUN_ID": context["runId"], "GITHUB_RUN_ATTEMPT": context["runAttempt"],
              "GITHUB_WORKFLOW_SHA": context["workflowSha"]}
    # WHY: copying the host environment would expose credentials to containers.
    # Runtime receipts need only the validated public identity of this run.
    return [argument for name, value in values.items() for argument in ("--env", f"{name}={value}")]


def build_configuration(libc: str, architecture: str, host: str, *, use_ci_image_mirrors: bool = False) -> tuple[str, str, bool]:
    """@brief Select native CI builds or x64-hosted ARM64 cross compilation."""
    if libc not in RUST_IMAGES or architecture not in {"amd64", "arm64"} or host not in {"amd64", "arm64"}:
        raise ValueError("Expected GNU/musl and supported Linux CPU architectures")
    cross = architecture != host
    if cross and host != "amd64":
        raise RuntimeError("Cross compilation currently requires an AMD64 host")
    base = MUSL_CROSS_IMAGE if cross and libc == "musl" else select_image("rust-" + libc, RUST_IMAGES[libc], use_ci_image_mirrors)
    return base, host, cross


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


def _builder_user(docker) -> str:
    """Use the actual Linux operator identity without inheriting host credentials."""
    values = [subprocess.check_output([*docker.prefix, "id", option], text=True, timeout=30).strip()
              for option in ("-u", "-g")]
    if any(re.fullmatch(r"0|[1-9][0-9]*", value) is None for value in values):
        raise RuntimeError("Linux builder requires numeric operator UID and GID")
    return ":".join(values)


def run_owned_container(docker, arguments: list[str], log: Path, *, timeout: int, append: bool = False,
                        capture: bool = False, ownership: dict | None = None) -> str:
    """@brief Retain failure output and stop only this invocation's container on interruption."""
    name = f"emulebb-linux-{uuid4().hex}"
    command = [*docker.prefix, "docker", "run", "--name", name, *arguments]
    try:
        with log.open("a" if append else "w", encoding="utf-8") as stream:
            result = subprocess.run(command, stdout=subprocess.PIPE if capture else stream,
                                    stderr=stream if capture else subprocess.STDOUT,
                                    text=True, check=True, timeout=timeout)
            if capture:
                stream.write(result.stdout)
            return result.stdout if capture else ""
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Linux container check failed; inspect {log}") from exc
    finally:
        # WHY: terminating wsl.exe does not reliably terminate its Docker job.
        subprocess.run([*docker.prefix, "docker", "rm", "--force", name], capture_output=True, timeout=60)
        if ownership is not None:
            remaining = subprocess.run([*docker.prefix, "docker", "ps", "--all", "--filter", f"name={name}",
                                        "--format", "{{.Names}}"], check=True, capture_output=True, text=True, timeout=30)
            ownership.update(name=name, absent=not remaining.stdout.strip())
            if not ownership["absent"]:
                raise RuntimeError("Owned proof container survived cleanup")


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


def _harness_module_path(name: str, module, harness: Path) -> Path | None:
    """Identify selected package modules and the script helper used by its WSL API."""
    filename = getattr(module, "__file__", None)
    path = Path(filename).resolve() if filename else None
    relevant = (name == "emule_test_harness" or name.startswith("emule_test_harness.")
                or name == "emule_live_profile_common_rust_ed2k"
                or (path is not None and (path.is_relative_to(harness)
                    or "emule_test_harness" in path.parts
                    or "emulebb-build-tests" in path.parts
                    or path.name == "emule-live-profile-common.py")))
    if not relevant:
        return None
    if path is None or not path.is_relative_to(harness) or path.suffix != ".py":
        raise RuntimeError(f"WSL lifecycle helper has a foreign or missing source: {name}")
    for package_path in getattr(module, "__path__", ()):
        if not Path(package_path).resolve().is_relative_to(harness):
            raise RuntimeError(f"WSL lifecycle package has a foreign search path: {name}")
    return path


def _load_wsl_process_api(harness: Path):
    """Load fresh selected Harness code without repairing a foreign module cache."""
    harness = harness.resolve()
    for name, module in tuple(sys.modules.items()):
        if _harness_module_path(name, module, harness) is not None:
            # WHY: a cached module can retain older function objects even when
            # its current filename and Git revision match the selected source.
            raise RuntimeError("WSL lifecycle requires a fresh process without cached Harness modules")
    original_path = list(sys.path)
    try:
        sys.path.insert(0, str(harness))
        module = importlib.import_module("emule_test_harness.rust_ed2k")
    finally:
        # Harness script imports may themselves insert the selected repo root.
        sys.path[:] = original_path
    material = {path for name, loaded in tuple(sys.modules.items())
                if (path := _harness_module_path(name, loaded, harness)) is not None}
    material.add(harness / "emule_test_harness/wsl_managed_process.sh")
    if not all(path.is_file() and path.resolve().is_relative_to(harness) for path in material):
        raise RuntimeError("WSL lifecycle helper sources must belong to the selected Harness")
    for name in ("start_wsl_managed_executable", "stop_process"):
        function = getattr(module, name, None)
        code = getattr(function, "__code__", None)
        if code is None or not Path(code.co_filename).resolve().is_relative_to(harness):
            raise RuntimeError(f"Selected Harness is missing its WSL lifecycle API: {name}")
    stdin_parameter = inspect.signature(module.start_wsl_managed_executable).parameters.get("stdin")
    if stdin_parameter is None or stdin_parameter.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
        raise RuntimeError("Selected Harness lacks owned-stdin WSL lifecycle support; qualify the matching Harness first")
    return module, tuple(sorted(material))


class WslValidationSession:
    """Keep one bounded, PID-backed WSL lifetime across a Windows check plan.

    The selected Harness owns launch and process-group teardown. Boot identity
    and Linux start time fence its PID file so a restarted kernel or reused PID
    cannot authorize signalling an unrelated Linux process.
    """

    def __init__(self, harness: Path, evidence: Path, *, distro: str = "Ubuntu",
                 lifetime_seconds: int = 21600) -> None:
        if not distro.strip() or any(character in distro for character in "\\/\0"):
            raise ValueError("WSL distribution must be a non-empty distribution name")
        if not 1 <= lifetime_seconds <= 86400:
            raise ValueError("WSL validation lifetime must be between one second and one day")
        self.harness, self.evidence = harness.resolve(), evidence
        self.docker, self.seconds = LinuxDocker(distro), lifetime_seconds
        self.process = self.api = None
        self.sources: dict[Path, str] = {}
        self.boot_id = self.birth = None
        self.pid = None
        self.deadline = None
        self.receipt = {"schema": "emulebb.wsl-validation-session/1", "status": "starting",
                        "distribution": distro, "lifetimeSeconds": lifetime_seconds}

    def _read_linux(self, path: str) -> str:
        return subprocess.check_output([*self.docker.prefix, "cat", path], text=True,
                                       stderr=subprocess.STDOUT, timeout=15).strip()

    def _boot(self) -> str:
        return str(UUID(self._read_linux("/proc/sys/kernel/random/boot_id")))

    def _pid_birth(self, pid: int) -> str:
        # Linux proc_pid_stat(5): comm is parenthesized and may contain spaces.
        fields = self._read_linux(f"/proc/{pid}/stat").rpartition(")")[2].split()
        if len(fields) < 20 or not fields[19].isdigit():
            raise RuntimeError("WSL owned process has an invalid Linux start-time identity")
        return fields[19]

    def _pid_exists(self, pid: int) -> bool:
        result = subprocess.run([*self.docker.prefix, "test", "-d", f"/proc/{pid}"],
                                capture_output=True, timeout=15)
        if result.returncode not in (0, 1):
            raise RuntimeError("Cannot establish whether the owned WSL process still exists")
        return result.returncode == 0

    def _write_receipt(self) -> None:
        self.evidence.mkdir(parents=True, exist_ok=True)
        (self.evidence / "wsl-session.json").write_text(json.dumps(self.receipt, indent=2) + "\n", encoding="utf-8")

    def _shared_pid_path(self) -> Path:
        """Give the existing Harness translator access through an owned DrvFS alias."""
        pid_path = self.evidence / "wsl-session.pid"
        if not self.docker.prefix:
            return pid_path
        mapped = self.docker.path(self.evidence)
        self.receipt["pathMapping"] = {"evidence": mapped}
        if any(self.evidence.resolve().is_relative_to(host) for host, _mount in self.docker.aliases):
            # WHY: wslpath alone cannot traverse every Windows volume mount.
            # A UNC view preserves both Windows PID reads and the Linux alias.
            return Path(f"//wsl.localhost/{self.docker.distro}{mapped}/wsl-session.pid")
        return pid_path

    def __enter__(self):
        self.evidence.mkdir(parents=True, exist_ok=True)
        try:
            self.api, material = _load_wsl_process_api(self.harness)
            self.sources = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in material}
            self.receipt["helperSources"] = {path.relative_to(self.harness).as_posix(): digest
                                             for path, digest in self.sources.items()}
            self.boot_id = self._boot()
            self.deadline = time.monotonic() + self.seconds
            executable = Path(f"//wsl.localhost/{self.docker.distro}/usr/bin/timeout")
            self.process = self.api.start_wsl_managed_executable(
                executable, ["--signal=TERM", "--kill-after=5", str(self.seconds), "/usr/bin/cat"],
                self.evidence / "wsl-session.log", self._shared_pid_path(),
                distribution=self.docker.distro, stdin=subprocess.PIPE)
            if self.process.proxy.stdin is None:
                raise RuntimeError("Selected Harness did not provide the required owned WSL input pipe")
            self._wait_for_pid()
            self.pid = self._read_pid()
            self.birth = self._pid_birth(self.pid)
            self.receipt.update(status="running", bootId=self.boot_id, linuxPid=self.pid,
                                linuxStartTime=self.birth, proxyPid=self.process.proxy.pid)
            self.check("session-start")
            version = json.loads(self.docker.run("version", "--format", "{{json .}}",
                                                capture=True, timeout=30).stdout)
            self.receipt["docker"] = {key: version["Server"][key] for key in ("Arch", "Version")}
            self._write_receipt()
            print(f"Owned WSL validation lifetime: {self.seconds}s; evidence: {self.evidence}", flush=True)
            return self
        except BaseException as error:
            self.receipt["planOutcome"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "startup-failed"
            self.receipt["startupError"] = str(error)
            self._stop()
            raise

    def _wait_for_pid(self) -> None:
        ready_deadline = min(self.deadline, time.monotonic() + 30)
        while not self.process.pid_path.is_file():
            if self.process.poll() is not None or time.monotonic() >= ready_deadline:
                raise RuntimeError("WSL lifetime process failed before its PID was ready")
            time.sleep(0.05)

    def _read_pid(self) -> int:
        with self.process.pid_path.open(encoding="ascii") as source:
            text = source.read(25)
        if len(text) > 24 or not text.strip().isdigit() or int(text) <= 1:
            raise RuntimeError("WSL lifetime PID file must identify an owned non-init process")
        return int(text)

    def check(self, stage: str) -> None:
        """Fail closed before/after a stage if lifetime, kernel or sources changed."""
        if self.process.poll() is not None or time.monotonic() >= self.deadline:
            raise RuntimeError("WSL validation lifetime ended or reached its deadline")
        if self._boot() != self.boot_id:
            raise RuntimeError("WSL kernel restarted during the validation plan")
        if self._read_pid() != self.pid or self._pid_birth(self.pid) != self.birth:
            raise RuntimeError("WSL lifetime process identity changed")
        if any(hashlib.sha256(path.read_bytes()).hexdigest() != digest for path, digest in self.sources.items()):
            raise RuntimeError("Selected WSL lifecycle helper sources changed during validation")
        self.receipt["lastVerifiedStage"] = stage
        self._write_receipt()

    def _stop(self) -> None:
        if self.process is None:
            try:
                self.docker.cleanup.close()
                self.receipt.update(status="stopped", linuxCleanup="not-started")
            except BaseException as error:
                self.receipt.update(status="cleanup-failed", cleanupError=str(error))
                raise
            finally:
                self._write_receipt()
            return
        failure = None
        try:
            proxy = self.process.proxy
            if proxy.stdin is None:
                raise RuntimeError("Owned WSL input pipe is missing; Linux teardown cannot be verified")
            # WHY: EOF is an owned control channel even before the PID file and
            # start-time identity are ready. Killing wsl.exe alone can orphan Linux.
            try:
                proxy.stdin.close()
            except BrokenPipeError:
                pass
            try:
                completed = proxy.wait(timeout=_WSL_STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                completed = None
            if completed == 0:
                self.receipt["linuxCleanup"] = "stdin-eof-stopped"
            elif self._boot() != self.boot_id:
                self.receipt["linuxCleanup"] = "previous-kernel-ended-no-linux-signals"
            elif self.pid is None or self.birth is None:
                raise RuntimeError("Owned WSL startup identity is incomplete; Linux teardown cannot be verified")
            elif not self._pid_exists(self.pid) or self._pid_birth(self.pid) != self.birth:
                self.receipt["linuxCleanup"] = "owned-identity-ended-no-linux-signals"
            else:
                self._stop_verified_pid()
        except BaseException as error:
            failure = error
        finally:
            proxy = self.process.proxy
            try:
                if proxy.poll() is None:
                    proxy.terminate()
                    try:
                        proxy.wait(timeout=_WSL_STOP_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        proxy.kill()
                        proxy.wait(timeout=_WSL_STOP_TIMEOUT)
            except BaseException as error:
                failure = failure or error
            try:
                # The PID file remains reachable until the owned Linux process
                # and Windows proxy have finished. Never unmount it sooner.
                self.docker.cleanup.close()
            except BaseException as error:
                failure = failure or error
            self.receipt["status"] = "cleanup-failed" if failure else "stopped"
            if failure:
                self.receipt["cleanupError"] = str(failure)
            self._write_receipt()
        if failure:
            raise RuntimeError("WSL lifetime cleanup failed; inspect its session receipt") from failure

    def _stop_verified_pid(self) -> None:
        """Use the existing group teardown only after same-boot birth verification."""
        stop_pid = self.evidence / "wsl-owned-stop.pid"
        with stop_pid.open("x", encoding="ascii") as target:
            target.write(f"{self.pid}\n")
        original_pid = self.process.pid_path
        try:
            self.process.pid_path = stop_pid
            self.api.stop_process(self.process)
        finally:
            self.process.pid_path = original_pid
        if self._boot() == self.boot_id and self._pid_exists(self.pid):
            if self._pid_birth(self.pid) == self.birth:
                raise RuntimeError("Owned WSL lifetime process survived teardown")
        self.receipt["linuxCleanup"] = "verified-owned-process-stopped"

    def __exit__(self, exc_type, exc, traceback):
        self.receipt["planOutcome"] = ("completed" if exc_type is None else
                                       "interrupted" if issubclass(exc_type, KeyboardInterrupt) else "failed")
        self._stop()
        return False


def verify_linux_profile_scratch(layout) -> Path:
    """@brief Prove two fixed GNU/AMD64 profile cases without claiming full validation."""
    from .local_checks import _source_snapshot
    from .release import _emulebb_rust_native_source_roots
    from .workstreams import _safe_path

    mini, pcp = _emulebb_rust_native_source_roots(layout)
    sources = {"emulebb-build": layout.build_repo_root, "emulebb-rust": layout.emulebb_rust_repo_root,
               "emulebb-tooling": layout.tooling_repo_root, "emulebb-build-tests": layout.tests_repo_root,
               "emulebb-miniupnp": mini, "emulebb-libpcpnatpmp": pcp}
    before = _source_snapshot(sources, clean=True)
    output = layout.output_root.resolve()
    if output.is_relative_to(layout.emule_workspace_root.resolve()):
        raise RuntimeError("Profile scratch proof requires output outside source")
    report = _safe_path(output, output / "reports/linux-profile-scratch" / uuid4().hex / "results.json")
    report.parent.mkdir(parents=True)
    value = {"schema": "emulebb.linux-profile-scratch-proof/1", "qualification": "focused-profile-scratch-only",
             "sources": before, "status": "failed", "tests": [], "containers": []}
    try:
        with LinuxDocker() as docker:
            _build_linux_artifacts(layout, docker, libc="gnu", architecture="amd64", webui_dist=None,
                                   release_version=None, test_scope="portable", profile_scratch_proof=(report, value))
        if (len(value["tests"]) != 2 or len(value["containers"]) != 3
                or any(owner.get("absent") is not True for owner in value["containers"])
                or value.get("hostScratchUnchanged") is not True):
            raise RuntimeError("Profile scratch proof is incomplete")
        after = _source_snapshot(sources, clean=True)
        if after != before:
            raise RuntimeError("Selected sources changed during profile scratch proof")
        value.update(status="passed", sourcesAfter=after)
        return report
    except BaseException as error:
        value["error"] = str(error)
        raise
    finally:
        report.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        print(f"Focused profile scratch proof {value['status']}: {report}", flush=True)


def _prove_profile_test_scratch(docker, test_cargo: list[str], target: str, target_root: str,
                                scratch: Path, user: str, logs: Path, proof: tuple[Path, dict]) -> Path:
    """Exercise the production test mount and exactly two fixed daemon library cases."""
    report, value = proof
    def entries():
        return sorted((entry.name, entry.lstat().st_mode, entry.lstat().st_size, entry.lstat().st_mtime_ns)
                      for entry in scratch.iterdir())
    before = entries()
    marker = "proof-" + uuid4().hex
    container_scratch = target_root + "/tmp/profile-ownership-tests"
    probe = (
        "import json,os,stat,subprocess,sys\nfrom pathlib import Path\n"
        "root=Path(sys.argv[1]); uid,gid=map(int,sys.argv[3].split(':'))\n"
        "kind=subprocess.check_output(['stat','-f','-c','%T',str(root)],text=True).strip()\n"
        "info=root.stat(); assert kind=='tmpfs'; assert (os.getuid(),os.getgid())==(uid,gid)\n"
        "assert (info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode))==(uid,gid,0o700)\n"
        "fd=os.open(root/sys.argv[2],os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)\n"
        "try:\n os.write(fd,b'synthetic proof'); file=os.fstat(fd); assert stat.S_IMODE(file.st_mode)==0o600; "
        "assert (file.st_uid,file.st_gid)==(uid,gid)\n"
        "finally:\n os.close(fd)\n"
        "print(json.dumps(dict(filesystem=kind,uid=info.st_uid,gid=info.st_gid,"
        "directoryMode=format(stat.S_IMODE(info.st_mode),'04o'),fileMode=format(stat.S_IMODE(file.st_mode),'04o'),"
        "fileUid=file.st_uid,fileGid=file.st_gid)))\n"
    )
    value.update(user=user, scratch=container_scratch, cargoTarget=target_root, logs=str(logs), hostScratchEntries=len(before))
    ownership = {}
    value["containers"].append(ownership)
    try:
        output = run_owned_container(docker, [*test_cargo[:-1], "python3", "-B", "-c", probe,
                                              container_scratch, marker, user], logs / "profile-scratch-mode.log",
                                     timeout=120, capture=True, ownership=ownership)
        value["filesystem"] = json.loads(output)
        for case in ("tests::retargeted_profile_alias_keeps_io_in_the_leased_directory",
                     "tests::load_rejects_group_or_world_readable_bootstrap_settings"):
            log = logs / (case.removeprefix("tests::") + ".log")
            ownership = {}
            value["containers"].append(ownership)
            run_owned_container(docker, [*test_cargo, "test", "--locked", "--target", target,
                                         "-p", "emulebb-daemon", "--lib", case, "--", "--exact"],
                                log, timeout=600, ownership=ownership)
            text = log.read_text(encoding="utf-8")
            if (f"test {case} ... ok" not in text
                    or re.findall(r"(?m)^test result: ok\. (\d+) passed; (\d+) failed; (\d+) ignored;", text)
                    != [("1", "0", "0")]):
                raise RuntimeError(f"Profile proof did not execute exactly one passing case: {case}; inspect {log}")
            value["tests"].append({"name": case, "passed": 1, "log": str(log)})
    finally:
        value["hostScratchUnchanged"] = entries() == before and not (scratch / marker).exists()
        if not value["hostScratchUnchanged"]:
            raise RuntimeError("Profile test scratch leaked into the host output directory")
    return report


def build_linux_artifacts(layout, *, libc: str, architecture: str, webui_dist: Path | None = None,
                          release_version: str | None = None, smoke: bool = False, test_scope: str = "portable",
                          use_ci_image_mirrors: bool = False) -> Path:
    """@brief Build and inventory one GNU/musl package using pinned Docker inputs.

    Container paths translate valid operator roots; only generated output is
    writable. ARM64 cross-compiles on x64 and runs selected tests under QEMU;
    native CI can execute the full workspace suite with the same recipe.
    """
    with LinuxDocker() as docker:
        archive = _build_linux_artifacts(layout, docker, libc=libc, architecture=architecture,
                                         webui_dist=webui_dist, release_version=release_version, test_scope=test_scope,
                                         use_ci_image_mirrors=use_ci_image_mirrors)
    if smoke:
        smoke_linux_archive(layout, archive=archive, libc=libc, architecture=architecture,
                            use_ci_image_mirrors=use_ci_image_mirrors)
    return archive


def _runtime_harness_source(layout, reports: Path) -> Path:
    """Provide self-contained Git metadata when the runtime inspects a worktree."""
    source = layout.tests_repo_root.resolve()
    if not (source / ".git").is_file():
        return source
    output = layout.output_root.resolve()
    if output.is_relative_to(layout.emule_workspace_root.resolve()):
        raise RuntimeError("Runtime harness snapshots require the external output root")
    revision = git_output(source, "rev-parse", "HEAD").strip()
    if git_output(source, "status", "--porcelain").strip():
        raise RuntimeError("Runtime harness snapshot requires clean selected source")
    branch = git_output(source, "branch", "--show-current").strip()
    if not branch:
        raise RuntimeError("Runtime harness worktree snapshot requires an assigned branch")
    snapshot = output / "tmp" / "linux-harness-snapshots" / uuid4().hex / "source"
    if not snapshot.resolve().is_relative_to(output) or snapshot.exists():
        raise RuntimeError("Invalid runtime harness snapshot destination")
    snapshot.parent.mkdir(parents=True)
    # WHY: a linked worktree's .git file contains a host-only absolute pointer.
    # An independent clone carries the exact commit and inspectable Git metadata
    # into the read-only container mount without exposing the canonical repo.
    subprocess.run(["git", "clone", "--no-local", "--no-checkout", "--single-branch",
                    "--branch", branch, "--config", "core.autocrlf=false", str(source), str(snapshot)],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(snapshot), "checkout", "--detach", revision],
                   check=True, capture_output=True, text=True)
    if (not (snapshot / ".git").is_dir()
            or (snapshot / ".git/objects/info/alternates").exists()
            or git_output(snapshot, "rev-parse", "HEAD").strip() != revision
            or git_output(snapshot, "status", "--porcelain").strip()
            or git_output(source, "rev-parse", "HEAD").strip() != revision
            or git_output(source, "status", "--porcelain").strip()):
        raise RuntimeError("Runtime harness source changed or snapshot is not independent")
    (reports / "harness-snapshot.json").write_text(json.dumps({
        "schema": "emulebb.runtime-harness-snapshot/1", "sourcePath": str(source),
        "snapshotPath": str(snapshot), "revision": revision,
    }, indent=2) + "\n", encoding="utf-8")
    return snapshot


def smoke_linux_archive(layout, *, archive: Path, libc: str, architecture: str, use_ci_image_mirrors: bool = False) -> Path:
    """@brief Run the shared two-peer lifecycle harness as a non-root container user."""
    from .release import _emulebb_rust_artifact_origin, _assert_emulebb_rust_hosted_artifact

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
    if _emulebb_rust_artifact_origin()["origin"] == "github-actions":
        _assert_emulebb_rust_hosted_artifact(manifest)
    context = Path(__file__).parent / "release_assets/rust_linux"
    runtime = f"local/emulebb-rust-runtime:{libc}-{architecture}"
    reports = output / "reports/rust-linux" / f"{libc}-{architecture}" / uuid4().hex
    reports.mkdir(parents=True)
    (reports / "tmp").mkdir()
    harness_source = _runtime_harness_source(layout, reports)
    with LinuxDocker() as docker:
        host_arch = docker.run("version", "--format", "{{.Server.Arch}}", capture=True, timeout=30).stdout.strip()
        hosted_environment = _hosted_linux_environment(architecture, host_arch)
        runtime_base = select_image("alpine-runtime" if libc == "musl" else "ubuntu-runtime", RUNTIME_IMAGES[libc], use_ci_image_mirrors)
        docker.run("build", "--platform", f"linux/{architecture}", "--build-arg", f"BASE_IMAGE={runtime_base}",
                   "--file", docker.path(context) + "/Dockerfile.runtime", "--tag", runtime, docker.path(context))
        mounts = ["--mount", f"type=bind,source={docker.path(output)},target=/output",
                  "--mount", f"type=bind,source={docker.path(layout.emulebb_rust_repo_root)},target=/workspace/repos/emulebb-rust,readonly",
                  "--mount", f"type=bind,source={docker.path(harness_source)},target=/workspace/repos/emulebb-build-tests,readonly"]
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
                   # WHY: Git must inspect this exact read-only source mount even
                   # when a root host runs the smoke as the unprivileged UID.
                   "--env", "GIT_CONFIG_COUNT=1", "--env", "GIT_CONFIG_KEY_0=safe.directory",
                   "--env", "GIT_CONFIG_VALUE_0=/workspace/repos/emulebb-build-tests",
                   "--env", f"TMPDIR=/output/{relative}/tmp", *hosted_environment, runtime,
                   "python3", "-B", "/workspace/repos/emulebb-build-tests/scripts/smoke-rust-release-package.py",
                   "--release-dir", "/output/" + archive.parent.relative_to(output).as_posix(),
                   "--release-version", version, "--target-os", "linux", "--platform", "x64" if architecture == "amd64" else "ARM64",
                   "--linux-libc", libc, "--expected-source-sha", source_sha,
                   "--rust-repo", "/workspace/repos/emulebb-rust", "--report-path", f"/output/{relative}/package-smoke.json"]
        run_owned_container(docker, command, reports / "runtime.log", timeout=600)
        if harness_source != layout.tests_repo_root.resolve() and (
                git_output(layout.tests_repo_root, "rev-parse", "HEAD").strip()
                != git_output(harness_source, "rev-parse", "HEAD").strip()
                or git_output(layout.tests_repo_root, "status", "--porcelain").strip()):
            raise RuntimeError("Selected runtime harness changed while validating its snapshot")
        (reports / "runtime-inputs.json").write_text(json.dumps({
            "runtimeBase": runtime_base, "filesystem": "tmpfs", "user": user,
            "execution": "native" if host_arch == architecture else "qemu",
            "sourceSha": source_sha, "archiveSha256": manifest["sha256"],
            "harnessSha": git_output(layout.tests_repo_root, "rev-parse", "HEAD").strip(),
        }, indent=2) + "\n", encoding="utf-8")
    print(f"Non-root Linux {architecture}/{libc} lifecycle passed: {reports / 'package-smoke.json'}")
    return reports / "package-smoke.json"


def _build_linux_artifacts(layout, docker: LinuxDocker, *, libc: str, architecture: str,
                           webui_dist: Path | None, release_version: str | None, test_scope: str,
                           use_ci_image_mirrors: bool = False,
                           profile_scratch_proof: tuple[Path, dict] | None = None) -> Path:
    from . import release
    from .workstreams import _safe_path
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
    if profile_scratch_proof is not None and (libc, architecture, engine_arch) != ("gnu", "amd64", "amd64"):
        raise RuntimeError("Profile scratch proof requires native GNU AMD64")
    hosted_environment = _hosted_linux_environment(architecture, engine_arch)
    base, build_arch, cross = build_configuration(libc, architecture, engine_arch, use_ci_image_mirrors=use_ci_image_mirrors)
    test_plan = linux_test_plan(test_scope, cross=cross, rust_root=rust)
    quality_plan = linux_quality_plan(rust, target) if test_scope == "full" else []
    user = _builder_user(docker)
    uid, gid = user.split(":")
    if profile_scratch_proof is not None and uid == "0":
        raise RuntimeError("Profile scratch proof requires the selected non-root builder user")
    owner = f"{uid}-{gid}"
    target_directory = output / "builds/rust" / f"target-linux-{libc}-{architecture}-uid-{uid}-gid-{gid}"
    profile_test_scratch = _safe_path(output, target_directory / "tmp/profile-ownership-tests")
    cache = output / "cache/cargo-linux-registry" / owner
    home = output / "cache/rust-linux-home" / owner
    # WHY: Docker must not create parent directories as root. Separate stable
    # user namespaces also avoid changing permissions on older root-owned caches.
    for directory in (target_directory, profile_test_scratch, cache, home):
        if not directory.resolve().is_relative_to(output):
            raise RuntimeError("Linux builder directories must remain below the operator output root")
        directory.mkdir(parents=True, exist_ok=True)
    _safe_path(output, profile_test_scratch)
    builder = f"local/emulebb-rust-builder:{libc}-{architecture}-{owner}"
    context = Path(__file__).parent / "release_assets" / "rust_linux"
    docker.run("build", "--platform", f"linux/{build_arch}", "--build-arg", f"BASE_IMAGE={base}",
               "--build-arg", f"CROSS_TARGET={target if cross else ''}",
               "--build-arg", f"BUILD_UID={uid}", "--build-arg", f"BUILD_GID={gid}",
               "--tag", builder, docker.path(context))
    root = output / "packages" / "rust-linux" / version / f"{libc}-{architecture}"
    root.mkdir(parents=True, exist_ok=True)
    log_root = output / "reports" / "rust-linux" / f"{libc}-{architecture}" / uuid4().hex
    log_root.mkdir(parents=True, exist_ok=True)
    miniupnp, pcpnatpmp = release._emulebb_rust_native_source_roots(layout)
    mounts = [
        "--mount", f"type=bind,source={docker.path(rust)},target=/source,readonly",
        "--mount", f"type=bind,source={docker.path(miniupnp)},target=/native/miniupnp,readonly",
        "--mount", f"type=bind,source={docker.path(pcpnatpmp)},target=/native/pcpnatpmp,readonly",
        "--mount", f"type=bind,source={docker.path(output)},target=/output",
        "--mount", f"type=bind,source={docker.path(cache)},target=/usr/local/cargo/registry",
    ]
    target_root = "/output/" + target_directory.relative_to(output).as_posix()
    environment = [*hosted_environment, "--env", f"CARGO_TARGET_DIR={target_root}", "--env", "MINIUPNP_ROOT=/native/miniupnp",
                   "--env", "PCPNATPMP_ROOT=/native/pcpnatpmp", "--env", "CARGO_BUILD_JOBS=4",
                   "--env", "HOME=/output/" + home.relative_to(output).as_posix()]
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
    user_options = ["--user", user, "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
    cargo = ["--rm", "--platform", f"linux/{build_arch}", *user_options, *mounts, *environment,
             "--workdir", "/source", builder, "cargo"]
    # WHY: DrvFS cannot preserve the 0600 bootstrap mode checked by Unix profile
    # tests. Only disposable fixtures use native tmpfs; Cargo caches stay on disk.
    profile_test_mount = (f"{target_root}/tmp/profile-ownership-tests:rw,exec,nosuid,nodev,"
                          f"size=8g,uid={uid},gid={gid},mode=0700")
    test_cargo = ["--tmpfs", profile_test_mount, *cargo]
    from .rust_source_cache import ensure_rust_source_cache
    cache_invocation = 0
    def cache_cargo(arguments):
        nonlocal cache_invocation
        cache_invocation += 1
        return run_owned_container(docker, [*cargo, *arguments],
                                   log_root / f"source-cache-{cache_invocation}.log",
                                   timeout=600 if arguments[0] == "fetch" else 120, capture=True)
    ensure_rust_source_cache(layout, target=target_directory, runner=cache_cargo,
                             execution_source="/source", execution_target=target_root,
                             native_roots=(miniupnp, pcpnatpmp),
                             execution_identity={"base": base, "target": target, "buildArchitecture": build_arch},
                             prefetch=True)
    if profile_scratch_proof is not None:
        profile_scratch_proof[1].update(builderImage=builder, hostArchitecture=engine_arch,
                                       imageId=docker.run("image", "inspect", builder, "--format", "{{.Id}}",
                                                          capture=True, timeout=30).stdout.strip())
        return _prove_profile_test_scratch(docker, test_cargo, target, target_root,
                                          profile_test_scratch, user, log_root, profile_scratch_proof)
    for label, arguments in quality_plan:
        print(f"Checking Linux {architecture}/{libc}: {label}; log: {log_root / (label + '.log')}", flush=True)
        run_owned_container(docker, [*cargo, *arguments], log_root / f"{label}.log", timeout=3600)
    command = ["run", *cargo, "build", "--locked", "--release",
               "--target", target, "-p", "emulebb-daemon", "--bin", "emulebb-rust"]
    log = log_root / "build.log"
    print(f"Building Linux {architecture}/{libc}; log: {log}", flush=True)
    run_owned_container(docker, command[1:], log, timeout=7200)
    tests = ["run", *test_cargo, "test", "--locked", "--target", target]
    for label, arguments in test_plan:
        print(f"Checking Linux {architecture}/{libc}: {label}; log: {log_root / (label + '.log')}", flush=True)
        run_owned_container(docker, [*tests[1:], *arguments], log_root / f"{label}.log", timeout=3600)
    binary = target_directory / target / "release" / "emulebb-rust"
    audit = docker.run("run", "--rm", "--network", "none", "--platform", f"linux/{build_arch}", *mounts,
                       *user_options, *environment, builder, "readelf", "-h", "-l", "-d", f"{target_root}/{target}/release/emulebb-rust", capture=True).stdout
    (log_root / "elf.txt").write_text(audit, encoding="utf-8")
    validate_elf_audit(audit, libc=libc, architecture=architecture)
    tools = docker.run("run", "--rm", "--network", "none", "--platform", f"linux/{build_arch}",
                       *user_options, *mounts, *environment, builder, "sh", "-ec", "rustc --version; cc --version; "
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
    for name in ("LICENSE",):
        shutil.copy2(rust / name, stage / name)
    release._stage_emulebb_rust_bootstrap(rust, stage)
    notices = stage / "licenses"
    notices.mkdir()
    shutil.copy2(miniupnp / "miniupnpc" / "LICENSE", notices / "MiniUPnPc-LICENSE")
    shutil.copy2(pcpnatpmp / "COPYING", notices / "libpcpnatpmp-COPYING")
    shutil.copy2(layout.tooling_repo_root / "docs/products/emulebb-rust/RELEASE-SCOPE.md", stage / "RELEASE-SCOPE.md")
    (stage / "README.txt").write_text(
        f"eMuleBB Rust {version}, Linux {architecture} {libc}.\n"
        "Run ./emulebb-rust for a fresh platform profile, or ./emulebb-rust --profile . here.\n"
        "Open http://127.0.0.1:20017/ and enter change_me_123change_me_123change_me_123.\n"
        "Replace the shared starter key before exposing the API outside this computer.\n"
        "The adjacent webui directory is required.\n"
        "Linux kernel and CPU requirements still apply; TLS requires system CA certificates.\n", encoding="utf-8")
    stem = f"emulebb-rust-v{version}-linux-{architecture}-{libc}"
    archive = root / f"{stem}.tar.zst"
    release._write_emulebb_rust_linux_sbom(layout=layout, rust_root=rust, package_root=stage, release_root=root,
                                         asset_name=archive.name, version=version, arch=f"{architecture}-{libc}")
    relative = root.relative_to(output).as_posix()
    docker.run("run", "--rm", "--network", "none", "--platform", f"linux/{build_arch}", *mounts,
               *user_options, builder, "chmod", "755", f"/output/{relative}/staging/emulebb-rust/emulebb-rust")
    # Replace through the host owner: an earlier root-run Docker build may have
    # left an archive that the operator UID cannot truncate through DrvFS.
    if archive.is_symlink() or not archive.resolve().is_relative_to(output):
        raise RuntimeError("Linux archive must be a regular output path")
    archive.unlink(missing_ok=True)
    release._write_emulebb_rust_portable_archive(stage, archive)
    sbom = root / f"{stem}.sbom.spdx.json"
    shutil.copy2(stage / "SBOM.spdx.json", sbom)
    release._assert_emulebb_rust_package_provenance(layout, rust, provenance)
    manifest = {**provenance, "package": "emulebb-rust", "version": version,
                "baseVersion": release._emulebb_rust_workspace_version(rust),
                "platform": f"linux-{architecture}-{libc}",
                "asset": archive.name, "sha256": release._sha256(archive), "sbom": sbom.name,
                "sbomSha256": release._sha256(sbom), "target": target, "libc": libc,
                "builderBase": base, "buildUser": user, "elfAudit": audit,
                "channel": channel,
                "buildExecution": "cross" if cross else "native", "libraryTestExecution": "qemu" if cross else "native",
                "testCompilation": "workspace" if test_scope == "full" else "selected-libraries",
                "testExecution": "workspace" if test_scope == "full" and not cross else "selected-libraries",
                "testProfile": "test", "testPlan": test_plan, "qualityPlan": quality_plan,
                "resolvedToolchain": tools, "builderBase": base,
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
                                     builder=builder, user=user, mounts=mounts, manifest=manifest, log_root=log_root)
    release._assert_emulebb_rust_package_provenance(layout, rust, provenance)
    release._write_sha256sums(root / "SHA256SUMS", (archive, manifest_path, sbom, *deb_files))
    print(f"Linux {architecture}/{libc} package: {archive}")
    return archive


def _package_gnu_deb(layout, docker, *, root: Path, stage: Path, version: str, architecture: str,
                     build_arch: str, builder: str, user: str, mounts: list[str], manifest: dict, log_root: Path) -> tuple[Path, ...]:
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
        "desktop-file-validate " + q(native + "/usr/share/applications/io.github.emulebb.emulebb-rust.desktop"),
        "appstreamcli validate-tree --no-net " + q(native),
        f"dpkg-deb --build --root-owner-group {q(native)} {q(destination)}",
    ])
    # DrvFS modes must not enter an installable Linux artifact.
    if deb.is_symlink() or not deb.resolve().is_relative_to(layout.output_root.resolve()):
        raise RuntimeError("GNU DEB must be a regular output path")
    deb.unlink(missing_ok=True)
    uid, gid = user.split(":")
    run_owned_container(docker, ["--rm", "--platform", f"linux/{build_arch}", "--user", user,
                                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges", *mounts,
                                 "--tmpfs", f"{native}:rw,exec,nosuid,nodev,size=256m,uid={uid},gid={gid},mode=0700",
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
