"""Reviewed Android build inputs shared by local WSL/Docker and hosted CI.

This preparation step downloads data only. SDK installation, license handling,
compilation and release signing belong to subsequent explicit operations.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
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
    """Fetch pinned emulator/ADB and only explicitly requested qualification images."""
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
        try:
            download_verified(item["url"], target, item["sha256"])
        except (OSError, RuntimeError) as error:
            # WHY: hosted CDN failures otherwise lose the selected asset behind
            # a generic HTTP status. Keep the reviewed role and host diagnosable.
            raise RuntimeError(f"Android input {role} from {urlsplit(item['url']).hostname} failed: {error}") from error
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
    value = {**probe, "image": {"tag": image, "id": image_id}}
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


def _guest_failure_details(directory: Path) -> dict:
    """Allowlist guest phases and OS facts; never publish private log text."""
    path = directory / "guest/report.json"
    try:
        if (path.is_symlink() or not path.resolve().is_relative_to(directory.resolve())
                or not path.is_file() or path.stat().st_size > 65536):
            return {"phase": "unknown"}
        guest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"phase": "unknown"}
    if not isinstance(guest, dict) or guest.get("schema") != "emulebb.android.emulator-boot/1":
        return {"phase": "unknown"}
    phases = {"adb-start", "emulator-start", "guest-boot", "guest-properties", "guest-validation", "tls-execution", "complete"}
    properties = {"sdk", "codename", "abis64", "qemu", "machine", "pageSize"}
    phase = guest.get("phase")
    value = {"phase": phase if isinstance(phase, str) and phase in phases else "unknown"}
    # Only fixed owner messages become public reason codes. Raw tracebacks,
    # bootstrap contents, keys and daemon output remain private to the guest.
    reasons = {
        "Owned Android ADB command failed": "owned-adb-command-failed",
        "Android TLS proof did not execute exactly one successful required test": "tls-case-incomplete",
        "Android guest TLS payload hash differs from its source-bound bytes": "guest-payload-hash-mismatch",
        "Android production daemon exited before fresh-profile REST readiness": "daemon-exited-before-rest",
        "Android REST forward did not allocate a private loopback port": "rest-forward-invalid",
        "Android fresh-profile REST did not become ready within its bound": "rest-startup-timeout",
        "Android unauthenticated REST must reject access with HTTP 401": "rest-unauthenticated-status",
        "Android authenticated REST must accept the generated API key": "rest-authentication-rejected",
        "Android authenticated REST does not identify the requested running production daemon": "app-identity-mismatch",
        "Android daemon PID is invalid": "daemon-pid-invalid",
        "Android daemon executable path differs; refusing to signal it": "daemon-executable-path",
        "Android daemon working directory differs; refusing to signal it": "daemon-working-directory",
        "Android daemon executable hash differs; refusing to signal it": "daemon-executable-hash",
        "Android daemon invocation differs; refusing to signal it": "daemon-invocation",
        "Android daemon has no verifiable kernel process identity": "daemon-kernel-identity",
        "Android daemon PID was reused before graceful shutdown": "daemon-pid-reused",
        "Android generated bootstrap is not owner-only": "bootstrap-permissions",
        "Android generated bootstrap must use the current loopback snake_case contract": "bootstrap-contract",
        "Android generated bootstrap must provide a valid private API key": "bootstrap-key-contract",
        "Android generated bootstrap is not valid TOML": "bootstrap-toml",
        "Android production daemon did not exit gracefully after SIGTERM": "daemon-ungraceful-exit",
    }
    progress = guest.get("tlsProgress")
    message = progress.get("failureMessage") if isinstance(progress, dict) else None
    if not isinstance(message, str):
        message = guest.get("error")
    reason = reasons.get(message) if isinstance(message, str) else None
    if reason is not None:
        value["reason"] = reason
    if isinstance(progress, dict):
        stages = {"payload-upload", "tls-test", "daemon-prepare", "daemon-launch", "rest-forward",
                  "rest-wait-unauthenticated", "daemon-admission", "bootstrap-check", "rest-authenticated",
                  "app-identity", "shutdown-identity", "shutdown-term", "shutdown-wait", "cleanup-forward"}
        public_progress = {key: progress[key] for key in ("stage", "failureStage")
                           if isinstance(progress.get(key), str) and progress[key] in stages}
        if isinstance(progress.get("errorType"), str) and progress["errorType"] in {"RuntimeError", "ValueError", "TypeError", "KeyError", "NameError",
                                          "AttributeError", "AssertionError", "OSError", "PermissionError",
                                          "FileNotFoundError", "TimeoutExpired", "TimeoutError", "JSONDecodeError"}:
            public_progress["errorType"] = progress["errorType"]
        if isinstance(progress.get("activeCase"), str) and progress["activeCase"] in TLS_CASE_NAMES:
            public_progress["activeCase"] = progress["activeCase"]
        completed = progress.get("completedCases")
        if (isinstance(completed, list) and len(completed) <= 6
                and all(isinstance(item, str) and item in TLS_CASE_NAMES for item in completed)
                and len(set(completed)) == len(completed)):
            public_progress["completedCases"] = completed
        if type(progress.get("daemonExitCode")) is int and -255 <= progress["daemonExitCode"] <= 255:
            public_progress["daemonExitCode"] = progress["daemonExitCode"]
        startup = progress.get("startupFailure")
        if isinstance(startup, dict):
            safe_startup = {}
            contexts = {"unknown", "dynamic-linker", "shell-launch", "platform-verifier-uninitialized",
                        "interface-enumeration", "profile-lock", "profile-in-use", "profile-lock-open",
                        "profile-resolution", "profile-directory", "executable-location", "metadata-open",
                        "bootstrap-parse", "bootstrap-permissions", "bootstrap-key-validation", "bootstrap-template",
                        "bootstrap-read", "bootstrap-file-error", "logging-initialization", "settings-load", "secure-ident-state",
                        "nat-settings", "incoming-directory", "bootstrap-http-client", "daemon-run-error",
                        "p2p-default-route-missing", "p2p-default-route-ambiguous", "p2p-vpn-binding-required", "rest-listener-bind",
                        "kad-listener-required", "ed2k-listener-required", "secure-ident-generation", "secure-ident-decode",
                        "secure-ident-encode", "secure-ident-size", "ip-filter-read", "transfer-root-create", "core-settings-missing",
                        "settings-snapshot-missing", "transfer-root-scan-task", "transfer-root-inventory", "transfer-root-recheck-task", "web-root-invalid",
                        "sqlite-fts-unavailable", "sqlite-schema-table", "sqlite-schema-column", "sqlite-busy", "sqlite-corrupt",
                        "sqlite-open", "sqlite-read-only", "sqlite-constraint"}
            conditions = {"unknown", "permission-denied", "operation-not-permitted", "operation-not-supported",
                          "not-implemented", "read-only-filesystem", "address-in-use", "missing-file",
                          "out-of-memory", "no-space", "file-limit", "invalid-argument", "temporarily-unavailable",
                          "missing-device", "network-unreachable", "address-unavailable"}
            for key, allowed in (("context", contexts), ("systemCondition", conditions)):
                if isinstance(startup.get(key), str) and startup[key] in allowed:
                    safe_startup[key] = startup[key]
            for key, allowed in (("errorSource", {"retained-jsonl", "private-console"}),
                                 ("fatalRecordStatus", {"matched", "missing", "unbounded", "changed", "malformed", "ambiguous", "unavailable"})):
                if isinstance(startup.get(key), str) and startup[key] in allowed:
                    safe_startup[key] = startup[key]
            for key in ("privateOutputAvailable", "bootstrapCreated", "runtimeEntered", "daemonRunErrorReported", "bootstrapExists", "leaseExists", "metadataExists"):
                if type(startup.get(key)) is bool:
                    safe_startup[key] = startup[key]
            public_progress["startupFailure"] = safe_startup
        value["tlsProgress"] = public_progress
    if isinstance(guest.get("property"), str) and guest["property"] in properties:
        value["property"] = guest["property"]
    observed = guest.get("observedGuest")
    if isinstance(observed, dict):
        value["observedGuest"] = {key: item for key, item in observed.items() if key in properties
            and isinstance(item, str) and len(item) <= 128 and re.fullmatch(r"[A-Za-z0-9_.+,\-]*", item)}
    code = guest.get("emulatorExitCode")
    if type(code) is int and -255 <= code <= 255:
        value["emulatorExitCode"] = code
    return value


TLS_CASE_NAMES = frozenset({
    "http_client::tests::packaged_android_roots_are_nonempty_and_build_without_platform_initialization",
    "http_client::tests::missing_or_malformed_roots_fail_without_platform_fallback",
    "http_client::tests::controlled_ca_accepts_valid_tls_but_rejects_wrong_hostname_and_expiry",
    "http_client::tests::packaged_android_roots_do_not_trust_private_fixture_ca",
    "ed2k_net_drivers::http_client_tests::url_import_builder_rejects_private_fixture_ca",
    "discovery_bootstrap::tests::discovery_https_uses_verified_client_and_rejects_private_ca",
})


def probe_emulator_guest(layout, *, image: str, wsl_distro: str = "Ubuntu", tls_startup: bool = False) -> Path:
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
    payload_manifest = None
    payload_value = None
    payload_identity = None
    capacity = None
    if tls_startup:
        from .android_validation import validate_android
        from .release import _sha256

        # Both pinned tool images, the native target and one fresh AVD coexist.
        # Refuse low capacity rather than deleting unrelated runner/host data.
        capacity = {"freeBytes": shutil.disk_usage(output).free, "requiredFreeBytes": 20 * 1024**3}
        if capacity["freeBytes"] < capacity["requiredFreeBytes"]:
            raise RuntimeError("Android TLS runtime requires at least 20 GiB free before compiling and booting")
        # Reuse the complete supported Android validator. Its default compile
        # graph is retained; this mode only exports the two fixed test artifacts.
        compilation = validate_android(layout, abis=("x86_64",), wsl_distro=wsl_distro, tls_payload=True)
        compiled = json.loads(compilation.read_text(encoding="utf-8"))
        exported = compiled.get("tlsPayload", {})
        relative = exported.get("path")
        if not isinstance(relative, str):
            raise RuntimeError("Android TLS runtime requires the successful owned payload export")
        payload_manifest = output / relative
        if (compiled.get("status") != "passed" or payload_manifest.is_symlink()
                or not payload_manifest.is_file() or not payload_manifest.resolve().is_relative_to(output)
                or _sha256(payload_manifest) != exported.get("sha256")):
            raise RuntimeError("Android TLS runtime payload export identity differs")
        payload_value = json.loads(payload_manifest.read_text(encoding="utf-8"))
        if (payload_value.get("schema") != "emulebb.android.tls-payload/2"
                or payload_value.get("status") != "passed"
                or not {"core-tests", "daemon-tests", "emulebb-rust"} <= set(payload_value.get("files", {}))):
            raise RuntimeError("Android first-launch requires the current production-daemon TLS payload schema")
        payload_identity = exported["sha256"]
        if _guest_sources(layout) != source:
            raise RuntimeError("Android TLS guest sources changed during compilation")
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
    if tls_startup:
        value["tls"] = {"status": "failed", "payloadManifestSha256": payload_identity, "capacityBefore": capacity}
    try:
        if _guest_sources(layout) != source:
            raise RuntimeError("Android guest boot sources changed during tool preparation")
        if tls_startup:
            from .release import _assert_emulebb_rust_package_provenance

            _assert_emulebb_rust_package_provenance(layout, layout.emulebb_rust_repo_root, compiled["source"])
        with LinuxDocker(wsl_distro) as docker:
            # WHY: only this fresh run directory is writable. ADB has no host
            # ports, USB access, Docker socket, credentials or shared server.
            run_owned_container(docker, ["--rm", "--platform", "linux/amd64", "--user", user,
                "--device", "/dev/kvm", "--group-add", group, "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777",
                "--env", "HOME=/tmp/home", "--mount", f"type=bind,source={docker.path(directory)},target=/output",
                "--mount", f"type=bind,source={docker.path(harness)},target=/harness,readonly",
                *(["--mount", f"type=bind,source={docker.path(payload_manifest.parent)},target=/tls-payload,readonly"]
                  if tls_startup else []),
                image_id, "python3", "-B", "/harness/scripts/probe-android-emulator.py",
                "--sdk-root", "/opt/android-emulator", "--image-dir", "/opt/android-emulator/" + image_path,
                "--api", str(api), "--page-size", str(page_size), "--output-root", "/output",
                "--report-dir", "/output/guest",
                *(["--tls-source-sha", payload_value["source"]["emulebbRust"]["commit"],
                   "--tls-manifest-sha256", payload_identity] if tls_startup else [])],
                directory / "container.log", timeout=900 if tls_startup else 480)
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
        if tls_startup:
            from .release import _assert_emulebb_rust_package_provenance, _sha256

            _assert_emulebb_rust_package_provenance(layout, layout.emulebb_rust_repo_root, compiled["source"])
            execution = guest.get("tls", {})
            cases = execution.get("cases")
            if (execution.get("schema") != "emulebb.android.tls-execution/2"
                    or execution.get("status") != "passed"
                    or execution.get("source") != payload_value["source"]
                    or execution.get("files") != payload_value["files"]
                    or _sha256(payload_manifest) != payload_identity
                    or not isinstance(cases, list) or len(cases) != 6
                    or {item.get("name") for item in cases if isinstance(item, dict)} != TLS_CASE_NAMES
                    or any(not isinstance(item, dict) or type(item.get("passed")) is not int or item.get("passed") != 1
                           or type(item.get("failed")) is not int or item.get("failed") != 0
                           or type(item.get("ignored")) is not int or item.get("ignored") != 0 for item in cases)
                    or execution.get("freshProfileRestReadiness") != "passed"
                    or execution.get("apkQualification") != "not-run"):
                raise RuntimeError("Android TLS guest did not execute every source-bound required case")
            startup = execution.get("startup", {})
            if (not isinstance(startup, dict) or startup.get("schema") != "emulebb.android.first-launch/1"
                    or startup.get("status") != "passed"
                    or startup.get("app") != {**payload_value["buildIdentity"], "name": "eMuleBB", "apiVersion": "v1", "lifecycleState": "running"}
                    or startup.get("profilePlacement") != "executable-directory"
                    or startup.get("callerCwdDifferent") is not True
                    or startup.get("bootstrapSnakeCase") is not True or startup.get("bootstrapOwnerOnly") is not True
                    or type(startup.get("unauthenticatedStatus")) is not int or startup.get("unauthenticatedStatus") != 401
                    or type(startup.get("authenticatedStatus")) is not int or startup.get("authenticatedStatus") != 200
                    or startup.get("shutdown") != {"signal": "SIGTERM", "identityVerified": True, "exitCode": 0, "graceful": True, "forwardRemoved": True}
                    or type(startup["shutdown"].get("exitCode")) is not int
                    or any(startup["shutdown"].get(key) is not True for key in ("identityVerified", "graceful", "forwardRemoved"))
                    or any(startup.get(key) != "not-run" for key in ("webuiReadiness", "apkQualification", "physicalDeviceQualification"))):
                raise RuntimeError("Android guest did not prove fresh-profile authenticated production REST and graceful shutdown")
            value["tls"] = {**execution, "payloadManifestSha256": payload_identity, "capacityBefore": capacity}
        # Publish an allowlisted result only; private AVDs and emulator logs can
        # contain local authentication material and must never be uploaded.
        value.update(status="passed", guest=expected, kvm=True)
    except BaseException as error:
        value.update(failureType=type(error).__name__, failureDetails=_guest_failure_details(directory))
        raise
    finally:
        report.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report
