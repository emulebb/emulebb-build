"""Boot an owned host emulator and smoke one retained debug APK."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager, suppress
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time


AVD = "emulebb-smoke-x64"
SERIAL = "emulator-5580"
CONSOLE_PORT = 5580


def run_smoke(harness: Path, *, environment=None, **inputs) -> None:
    """Use the shared APK admission and installation proof."""
    command = [sys.executable, "-B", str(harness / "scripts/smoke-android-debug-apk.py")]
    for name, value in inputs.items():
        command.extend(("--" + name.replace("_", "-"), str(value)))
    subprocess.run(command, env=environment, check=True)


@contextmanager
def _owned_process(command, environment, log):
    windows = os.name == "nt"
    with log.open("xb") as stream:
        process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if windows else 0)
        try:
            yield process
        finally:
            if process.poll() is None:
                if windows:
                    # The Windows launcher owns a separate QEMU child.
                    subprocess.run(["taskkill", "/pid", str(process.pid), "/t", "/f"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def _private_port() -> int:
    with ExitStack() as stack:
        for port in (CONSOLE_PORT, CONSOLE_PORT + 1):
            listener = stack.enter_context(socket.socket())
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", port))
        listener = stack.enter_context(socket.socket())
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def prepare_hosted_emulator(output: Path) -> tuple[Path, Path, Path]:
    """Install the existing reviewed Linux emulator and API 37 guest pins."""
    from .android_inputs import emulator_inputs, emulator_subset
    from .android_install import emulator_layout, install_emulator_inputs
    from .android_toolchain import fetch_emulator_inputs

    images = ("apk37",)
    inputs = emulator_subset(emulator_inputs(), images)
    cache = fetch_emulator_inputs(images=images).parent
    sdk = output / "tools/android-debug-emulator"
    install_emulator_inputs(cache, sdk, images=images)
    user_home = output / "tmp/android-debug-emulator"
    avd_home = user_home / "avd"
    avd = avd_home / f"{AVD}.avd"
    avd.mkdir(parents=True)
    (avd_home / f"{AVD}.ini").write_text(f"path={avd}\ntarget=android-37\n", encoding="utf-8")
    image = sdk / emulator_layout(inputs)["system-image-apk37"][1]
    settings = {
        "AvdId": AVD, "avd.ini.encoding": "UTF-8", "abi.type": "x86_64",
        "hw.cpu.arch": "x86_64", "hw.lcd.width": "1080", "hw.lcd.height": "1920",
        "hw.lcd.density": "420", "hw.gpu.enabled": "yes",
        "image.sysdir.1": f"{image}/", "disk.dataPartition.size": "8G",
    }
    (avd / "config.ini").write_text("".join(f"{key}={value}\n" for key, value in settings.items()),
                                   encoding="utf-8")
    return sdk, user_home, avd_home


def boot_and_smoke(harness: Path, *, output: Path, sdk: Path, user_home: Path,
                   avd_home: Path, manifest: Path, report: Path,
                   source_sha: str | None = None, manifest_sha256: str | None = None,
                   certificate_sha256: str | None = None) -> Path:
    """Verify a retained x86_64 APK on one disposable, loopback-owned ADB session."""
    output = output.resolve()
    if (manifest.is_symlink() or not manifest.resolve().is_relative_to(output)
            or not report.resolve().is_relative_to(output) or report.exists()
            or not user_home.resolve().is_relative_to(output)
            or not avd_home.resolve().is_relative_to(output)):
        raise ValueError("Select a retained manifest, isolated Android state and new report below the output root")
    if not manifest.is_file() or manifest.stat().st_size > 65536:
        raise ValueError("Select a bounded debug APK manifest")
    raw = manifest.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    metadata = json.loads(raw)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("buildIdentity"), dict):
        raise ValueError("Select a debug manifest with its build identity")
    source = metadata["buildIdentity"].get("sourceRevision")
    certificate = metadata.get("signerSha256")
    if (metadata.get("debug") is not True or metadata.get("abis") != ["x86_64"]
            or not isinstance(source, str) or re.fullmatch(r"[0-9a-f]{40}", source) is None
            or not isinstance(certificate, str) or re.fullmatch(r"[0-9a-f]{64}", certificate) is None
            or (source_sha is not None and source != source_sha)
            or (manifest_sha256 is not None and digest != manifest_sha256)
            or (certificate_sha256 is not None and certificate != certificate_sha256)):
        raise ValueError("Select the exact retained single-ABI x86_64 debug package")

    suffix = ".exe" if os.name == "nt" else ""
    adb = sdk / ("platform-tools/adb" + suffix)
    emulator = sdk / ("emulator/emulator" + suffix)
    if not adb.is_file() or not emulator.is_file() or not (avd_home / f"{AVD}.ini").is_file():
        raise RuntimeError(f"Install Android Emulator and platform-tools in {sdk}, then create {AVD} in {avd_home}")
    environment = dict(os.environ)
    environment.pop("ADB_SERVER_SOCKET", None)
    port = _private_port()
    environment.update(ANDROID_HOME=str(sdk), ANDROID_SDK_ROOT=str(sdk),
                       ANDROID_USER_HOME=str(user_home), ANDROID_EMULATOR_HOME=str(user_home),
                       ANDROID_AVD_HOME=str(avd_home), ANDROID_ADB_SERVER_PORT=str(port),
                       ADB_USB="0", ADB_MDNS="0", ADB_MDNS_AUTO_CONNECT="")
    accel = subprocess.run([emulator, "-accel-check"], env=environment, check=True,
                           capture_output=True, text=True, timeout=15)
    acceleration = accel.stdout.splitlines()
    if len(acceleration) < 3 or acceleration[:2] != ["accel:", "0"]:
        raise RuntimeError("Android emulator hardware acceleration is unavailable")
    installed = sdk / "emulator-inputs.json"
    if installed.is_file():
        # WHY: the checksum-verified Linux installation already identifies
        # its build. The desktop version probe can load Qt on a headless host.
        version = json.loads(installed.read_text(encoding="utf-8"))["assets"]["emulator"]["version"]
    else:
        emulator_info = subprocess.run([emulator, "-version"], env=environment, check=True,
                                       capture_output=True, text=True, timeout=15)
        match = re.search(r"Android emulator version ([0-9.]+)", emulator_info.stdout + emulator_info.stderr)
        if match is None:
            raise RuntimeError("Android emulator version could not be identified")
        version = match[1]
    report.parent.mkdir(parents=True, exist_ok=True)
    directory = report.parent

    def command(*args):
        return subprocess.run([adb, "-P", str(port), "-s", SERIAL, *args], env=environment,
                              capture_output=True, text=True, timeout=10)

    adb_command = [adb, "-P", str(port), "server", "nodaemon"]
    emulator_command = [emulator, "-avd", AVD, "-port", str(CONSOLE_PORT), "-no-window",
                        "-no-audio", "-no-snapshot", "-no-boot-anim", "-read-only",
                        "-accel", "on", "-gpu", "software", "-cores", "2", "-memory", "3072"]
    with _owned_process(adb_command, environment, directory / "adb.log") as server:
        for _ in range(30):
            if server.poll() is not None:
                raise RuntimeError(f"Owned ADB server exited during startup. Logs: {directory}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError(f"Owned ADB server did not open its loopback socket. Logs: {directory}")
        with _owned_process(emulator_command, environment, directory / "emulator.log") as guest:
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                if server.poll() is not None or guest.poll() is not None:
                    raise RuntimeError(f"Owned ADB or Android emulator exited during boot. Logs: {directory}")
                # WHY: the ADB transport can stall briefly during guest startup.
                # The outer deadline still bounds boot and later probes fail normally.
                with suppress(subprocess.TimeoutExpired):
                    if command("shell", "getprop", "sys.boot_completed").stdout.strip() == "1":
                        break
                time.sleep(1)
            else:
                raise RuntimeError(f"Android emulator did not finish booting. Logs: {directory}")
            if command("emu", "avd", "name").stdout.splitlines()[:1] != [AVD]:
                raise RuntimeError("ADB selected a different virtual device")
            try:
                api = command("shell", "getprop", "ro.build.version.sdk").stdout.strip()
                page_size = command("shell", "getconf", "PAGE_SIZE").stdout.strip()
                image = command("shell", "getprop", "ro.build.fingerprint").stdout.strip()
                if (api, page_size) != ("37", "16384") or re.fullmatch(r"[A-Za-z0-9_./:+-]{1,180}", image) is None:
                    raise RuntimeError("Select the API 37 emulator image with 16 KiB pages")
                run_smoke(harness, environment=environment, adb=adb, serial=SERIAL,
                          server_port=port, manifest=manifest, manifest_sha256=digest,
                          source_sha=source, certificate_sha256=certificate,
                          abi="x86_64", report=report)
                with (directory / "emulator.json").open("x", encoding="utf-8") as stream:
                    json.dump({"api": int(api), "pageSizeBytes": int(page_size),
                               "systemImageFingerprint": image, "emulatorVersion": version,
                               "accelerator": acceleration[2]}, stream, indent=2)
                    stream.write("\n")
            finally:
                with suppress(subprocess.TimeoutExpired):
                    command("emu", "kill")
                    guest.wait(timeout=15)
    return report
