"""Source-bound universal Android application production and external signing."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from uuid import uuid4

from . import release
from .android_inputs import tool_inputs
from .android_package import identity
from .android_apk_notices import (audit_packaged_notices, notice_inventory, spdx_components,
                                 stage_runtime_notices, stage_source_notices)
from .android_toolchain import prepare_apk_toolchain as prepare_toolchain
from .android_validation import TARGETS, audit_android_elf, compiler_environment, _runtime_dependencies
from .linux_containers import LinuxDocker, _builder_user, run_owned_container

APPLICATION_ID = "org.emulebb.android"
MIN_SDK = 34
TARGET_SDK = 37
SIGNING_ENV = ("EMULEBB_ANDROID_KEYSTORE", "EMULEBB_ANDROID_KEY_ALIAS",
               "EMULEBB_ANDROID_KEYSTORE_PASSWORD", "EMULEBB_ANDROID_KEY_PASSWORD")


def gradle_plugin_version(rust: Path) -> str:
    """Read the one explicit APK plugin pin from its owning Gradle source."""
    text = (rust / "android/build.gradle.kts").read_text(encoding="utf-8")
    versions = re.findall(r'id\("com\.android\.application"\)\s+version\s+"([0-9]+\.[0-9]+\.[0-9]+)"', text)
    if len(versions) != 1:
        raise RuntimeError("APK project requires exactly one explicit stable Android Gradle plugin pin")
    return versions[0]


def probe_gradle(layout, *, wsl_distro: str = "Ubuntu") -> Path:
    """Resolve the exact APK plugin with the pinned Gradle/JDK before native compilation."""
    prepare_toolchain(layout, wsl_distro=wsl_distro)
    plugin = gradle_plugin_version(layout.emulebb_rust_repo_root)
    output = layout.output_root.resolve()
    root = output / "reports/android-apk-gradle" / uuid4().hex
    project = root / "project"
    project.mkdir(parents=True)
    (project / "settings.gradle.kts").write_text(
        'pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } }\n'
        'rootProject.name = "android-apk-toolchain-probe"\n', encoding="utf-8")
    (project / "build.gradle.kts").write_text(
        f'plugins {{ id("com.android.application") version "{plugin}" apply false }}\n', encoding="utf-8")
    with LinuxDocker(wsl_distro) as docker:
        home = output / "cache/android-apk-gradle-probe"
        home.mkdir(parents=True, exist_ok=True)
        relative = lambda path: "/output/" + path.relative_to(output).as_posix()
        run_owned_container(docker, ["--rm", "--platform", "linux/amd64", "--user", _builder_user(docker),
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777",
            "--mount", f"type=bind,source={docker.path(output)},target=/output",
            "--env", "HOME=" + relative(home), "--env", "GRADLE_USER_HOME=" + relative(home / "gradle"),
            "local/emulebb-android-apk-toolchain:reviewed", "gradle", "--no-daemon", "--console=plain",
            "--project-dir", relative(project), "help"], root / "gradle.log", timeout=1200)
    return root / "gradle.log"


def public_names(version: str) -> dict[str, str]:
    """Keep every published APK sidecar unambiguous beside daemon archives."""
    if not release._is_release_version(version):
        raise ValueError("APK version must be a valid distribution version")
    stem = f"emulebb-rust-v{version}-android-universal"
    return {"apk": stem + ".apk", "manifest": stem + ".apk.manifest.json",
            "sbom": stem + ".sbom.spdx.json", "proof": stem + "-apk-lifecycle.json"}


def write_github_outputs(apk: Path, destination: Path, *, version: str, source_sha: str, signer_sha256: str, version_code: int) -> None:
    """Expose only a completed production artifact's verified paths and identities."""
    names = public_names(version)
    root = apk.parent
    manifest = root / names["manifest"]
    data = json.loads(manifest.read_text(encoding="utf-8"))
    release._assert_emulebb_rust_hosted_artifact(data)
    if (apk.name != names["apk"] or data.get("status") != "passed" or data.get("debug") is not False
            or data.get("buildIdentity") != {"sourceRevision": source_sha, "version": version, "channel": "nightly"}
            or type(data.get("versionCode")) is not int or data["versionCode"] != version_code
            or data.get("signerSha256") != signer_sha256 or data.get("apk", {}).get("sha256") != release._sha256(apk)
            or data.get("sbomSha256") != release._sha256(root / names["sbom"])):
        raise RuntimeError("GitHub APK outputs require the exact verified production package")
    outputs = {"apk_path": str(apk), "manifest_path": str(manifest), "manifest_sha256": release._sha256(manifest),
               "sbom_path": str(root / names["sbom"]), "apk_sha256": data["apk"]["sha256"], "signer_sha256": signer_sha256,
               "apk_version_code": str(version_code)}
    if any("\n" in value or "\r" in value for value in outputs.values()):
        raise RuntimeError("GitHub APK output values must be single-line paths or identities")
    with destination.open("a", encoding="utf-8", newline="\n") as stream:
        stream.writelines(name + "=" + value + "\n" for name, value in outputs.items())


def signing_inputs(environment: dict[str, str]) -> tuple[Path, str]:
    """Fail before compilation when release signing is not configured."""
    if any(not environment.get(name) for name in SIGNING_ENV):
        raise RuntimeError("APK release requires all four EMULEBB_ANDROID signing inputs")
    key = Path(environment[SIGNING_ENV[0]])
    if not key.is_absolute() or key.is_symlink() or not key.is_file():
        raise RuntimeError("APK keystore must be an absolute existing regular file")
    alias = environment[SIGNING_ENV[1]]
    if not alias.strip() or any(char in alias for char in "\r\n\0"):
        raise RuntimeError("APK signing alias must be a single nonempty value")
    return key, alias


def audit_badging(text: str, *, version: str, version_code: int) -> None:
    """Validate packaged manifest/ABI metadata using the pinned Android aapt2 output."""
    expected = {"name": APPLICATION_ID, "versionName": version, "versionCode": str(version_code)}
    packages = [line for line in text.splitlines() if line.startswith("package: ")]
    if len(packages) != 1:
        raise RuntimeError("APK must have exactly one application identity")
    fields = dict(re.findall(r"([a-zA-Z]+)='([^']*)'", packages[0]))
    if any(fields.get(key) != value for key, value in expected.items()):
        raise RuntimeError("APK package identity differs from the requested source release")
    for field, expected_value in (("minSdkVersion", MIN_SDK), ("targetSdkVersion", TARGET_SDK)):
        if re.findall(r"^" + field + r":'([0-9]+)'$", text, re.MULTILINE) != [str(expected_value)]:
            raise RuntimeError("APK SDK bounds differ from its declared package contract")
    native = [line for line in text.splitlines() if line.startswith("native-code:")]
    if len(native) != 1 or set(re.findall(r"'([^']+)'", native[0])) != set(TARGETS):
        raise RuntimeError("APK must contain exactly both supported native ABIs")


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _container_environment(values: dict[str, str]) -> list[str]:
    return [argument for name, value in values.items() for argument in ("--env", f"{name}={value}")]


def certificate_sha256(text: str) -> str:
    """Read one signer identity from pinned apksigner's scheme-specific output."""
    identities, labels = set(), set()
    counts = [line for line in text.splitlines() if line.startswith("Number of signers:")]
    if counts and counts != ["Number of signers: 1"]:
        raise RuntimeError("APK signature verification did not identify exactly one signer")
    pattern = re.compile(r"(?P<label>Signer #(?P<number>[1-9][0-9]*) certificate|(?:V[12]|V3(?:\.[012])?) Signer: certificate) "
                         r"SHA-256 digest: (?P<digest>[0-9a-fA-F]{64})")
    for line in text.splitlines():
        if "certificate SHA-256 digest:" not in line:
            continue
        match = pattern.fullmatch(line)
        if match is None or match["number"] not in {None, "1"} or match["label"] in labels:
            raise RuntimeError("APK signature verification did not identify exactly one signer")
        labels.add(match["label"])
        identities.add(match["digest"].lower())
    if len(identities) != 1:
        raise RuntimeError("APK signature verification did not identify exactly one signer")
    return identities.pop()


def _expected_certificate(value: str | None) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("Production APK requires an explicit expected certificate SHA-256")
    return value


def _sign(docker, *, base: list[str], image: str, scratch: Path, output: Path,
          unsigned: Path, signed: Path, report_root: Path,
          signing: dict[str, str] | None = None) -> str:
    """Expose passwords only to the signing subprocess, using apksigner env references."""
    selected = signing if signing is not None else dict(os.environ)
    key, alias = signing_inputs(selected)
    secret_root = scratch / "signing"
    secret_root.mkdir(mode=0o700)
    secret_key = secret_root / "release.keystore"
    try:
        shutil.copyfile(key, secret_key)
        secret_key.chmod(0o600)
        child = dict(os.environ)
        passwords = SIGNING_ENV[2:]
        child.update({name: selected[name] for name in passwords})
        if os.name == "nt":
            child["WSLENV"] = ":".join(filter(None, (child.get("WSLENV", ""), *passwords)))
        # WHY: only this container receives signing secrets. General Gradle and
        # Cargo containers cannot access the read-only keystore mount or passwords.
        mount = ["--mount", f"type=bind,source={docker.path(secret_root)},target=/signing,readonly"]
        forwarding = [argument for name in passwords for argument in ("--env", name)]
        tools = "/opt/android-tools/sdk/build-tools/" + tool_inputs()["assets"]["build-tools"]["version"]
        path = lambda item: "/output/" + item.relative_to(output).as_posix()
        run_owned_container(docker, [*base, *mount, *forwarding, "--network", "none", image,
            tools + "/apksigner", "sign", "--ks", "/signing/release.keystore", "--ks-key-alias", alias,
            "--ks-pass", "env:" + passwords[0], "--key-pass", "env:" + passwords[1],
            "--out", path(signed), path(unsigned)], report_root / "sign.log", timeout=120,
            environment=child)
        text = run_owned_container(docker, [*base, "--network", "none", image,
            tools + "/apksigner", "verify", "--verbose", "--print-certs", path(signed)],
            report_root / "signature.log", timeout=120, capture=True)
        return certificate_sha256(text)
    finally:
        shutil.rmtree(secret_root)


def package_apk(layout, *, release_version: str, channel: str, expected_source_sha: str,
                version_code: int, wsl_distro: str = "Ubuntu", debug: bool = False,
                retained_signing: bool = False, author_gradle_verification: bool = False,
                kotlin_only: bool = False, lint_only: bool = False,
                expected_cert_sha256: str | None = None) -> Path:
    """Build both JNI ABIs, bundled SPA and APK from one immutable source selection.

    Debug builds are explicit local test inputs. Nightly publication always
    requires configured production signing, regardless of any debug keystore.
    """
    identity(release_version, channel, expected_source_sha)
    if not 1 <= version_code <= 2_100_000_000:
        raise ValueError("APK versionCode must be between 1 and 2100000000")
    if (debug and retained_signing) or (author_gradle_verification and not debug):
        raise ValueError("Gradle verification authoring requires debug; retained signing requires release")
    if kotlin_only and lint_only:
        raise ValueError("Select one Gradle-only validation scope")
    selected_signing, retained_certificate = None, None
    if retained_signing:
        from .android_signing import retained_signing_inputs
        selected_signing, retained_certificate = retained_signing_inputs()
    if not debug:
        signing_inputs(selected_signing if selected_signing is not None else dict(os.environ))
        expected_cert_sha256 = _expected_certificate(expected_cert_sha256)
        if retained_certificate is not None and retained_certificate != expected_cert_sha256:
            raise RuntimeError("Retained APK key differs from the expected public certificate")
    rust, output = layout.emulebb_rust_repo_root, layout.output_root.resolve()
    provenance = release._emulebb_rust_package_provenance(layout, rust)
    if not author_gradle_verification and not (rust / "android/gradle/verification-metadata.xml").is_file():
        raise RuntimeError("APK source requires reviewed Gradle dependency verification metadata")
    if provenance["source"]["emulebbRust"]["commit"] != expected_source_sha:
        raise RuntimeError("APK source differs from the requested revision")
    if not release_version.startswith(release._emulebb_rust_workspace_version(rust) + ".nightly."):
        raise RuntimeError("APK version differs from its Cargo source version")
    prepare_toolchain(layout, wsl_distro=wsl_distro)
    run_id = uuid4().hex
    scratch = output / "tmp/android-apk" / run_id
    report_root = output / "reports/android-apk" / run_id
    root = output / ("packages/rust-android-apk-debug" if debug else "packages/rust-android-apk") / release_version
    for directory in (scratch, report_root, root):
        if directory.is_symlink() or not directory.resolve().is_relative_to(output):
            raise RuntimeError("APK output must stay within the inherited output root")
        directory.mkdir(parents=True, exist_ok=True)
    report = {"schema": "emulebb.android.apk-build/1", "status": "failed",
              "source": provenance["source"], "runtimeExecution": "not-run", "checks": []}
    try:
        project = scratch / "android"
        shutil.copytree(rust / "android", project, ignore=shutil.ignore_patterns(".gradle", "build", "local.properties"))
        webui = scratch / "webui"
        subprocess.run([sys.executable, "-B", str(rust / "tools/build_webui.py"),
                        "--stage-dir", str(webui)], check=True,
                       env={name: value for name, value in os.environ.items()
                            if name not in {*SIGNING_ENV, "EMULEBB_ANDROID_KEYSTORE_BASE64"}})
        with LinuxDocker(wsl_distro) as docker:
            user = _builder_user(docker)
            if user.split(":")[0] == "0":
                raise RuntimeError("APK production requires a non-root operator")
            owner = "uid-" + user.replace(":", "-gid-")
            target = output / "builds/rust" / ("target-android-apk-" + owner)
            home = output / "cache/android-apk-home" / owner
            cargo_home = output / "cache/cargo-android-apk" / owner
            for directory in (target, home, cargo_home):
                directory.mkdir(parents=True, exist_ok=True)
            mini, pcp = release._emulebb_rust_native_source_roots(layout)
            mounts = [argument for host, guest, readonly in (
                (output, "/output", False), (rust, "/source", True),
                (mini, "/native/miniupnp", True), (pcp, "/native/pcpnatpmp", True))
                for argument in ("--mount", f"type=bind,source={docker.path(host)},target={guest}" + (",readonly" if readonly else ""))]
            path = lambda item: "/output/" + item.relative_to(output).as_posix()
            env = {"HOME": path(home), "CARGO_HOME": path(cargo_home), "CARGO_TARGET_DIR": path(target),
                   "GRADLE_USER_HOME": path(home / "gradle"), "CARGO_BUILD_JOBS": "4",
                   "RUSTUP_TOOLCHAIN": "1.98.1",
                   "MINIUPNP_ROOT": "/native/miniupnp", "PCPNATPMP_ROOT": "/native/pcpnatpmp",
                   "EMULEBB_SOURCE_REVISION": expected_source_sha, "EMULEBB_RELEASE_VERSION": release_version,
                   "EMULEBB_RELEASE_CHANNEL": channel}
            base = ["--rm", "--platform", "linux/amd64", "--user", user, "--read-only",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777", *mounts, *_container_environment(env),
                    "--workdir", "/source"]
            image = "local/emulebb-android-apk-toolchain:reviewed"
            def execute(label, command, *, extra=(), network="none", capture=False):
                result = run_owned_container(docker, [*base, *extra, "--network", network, image, *command],
                    report_root / (label + ".log"), timeout=3600, capture=capture)
                report["checks"].append({"name": label, "status": "passed"})
                return result
            native = scratch / "native"
            native.mkdir()
            notices = scratch / "notices"
            notices.mkdir()
            runtime_inventory = scratch / "runtime-inventory"
            verification = ["--write-verification-metadata", "sha256"] if author_gradle_verification else ["--dependency-verification", "strict"]
            def gradle(task, label):
                try:
                    execute(label, ["gradle", "--no-daemon", "--console=plain", "--project-dir", path(project),
                        "--project-cache-dir", path(home / "gradle-project-cache"), *verification,
                        "-PandroidBuildRoot=" + path(scratch / "gradle-build"),
                        "-PemulebbNativeDir=" + path(native), "-PemulebbWebuiDir=" + path(webui),
                        "-PemulebbNoticesDir=" + path(notices),
                        "-PemulebbRuntimeInventoryDir=" + path(runtime_inventory),
                        "-PemulebbRuntimeVariant=" + ("debug" if debug else "release"),
                        "-PemulebbVersionName=" + release_version, "-PemulebbVersionCode=" + str(version_code),
                        ":app:" + task], network="bridge")
                finally:
                    metadata = project / "gradle/verification-metadata.xml"
                    if author_gradle_verification and metadata.is_file():
                        shutil.copy2(metadata, report_root / "verification-metadata.xml")
                    inventory_report = runtime_inventory / "report.json"
                    if task == "emulebbRuntimeInventory" and inventory_report.is_file():
                        shutil.copy2(inventory_report, report_root / "runtime-classpath-inputs.json")
                    if task.startswith("lint"):
                        for lint_report in (scratch / "gradle-build/app/reports").glob("lint-results-*"):
                            if lint_report.is_file() and not lint_report.is_symlink():
                                destination = report_root / "lint" / lint_report.name
                                destination.parent.mkdir(exist_ok=True)
                                shutil.copy2(lint_report, destination)
            # WHY: Kotlin/Gradle errors need no JNI bytes. Validate the actual
            # staged app before invalidating or rebuilding native Cargo caches.
            gradle("compileDebugKotlin" if debug else "compileReleaseKotlin", "kotlin-preflight")
            if kotlin_only:
                release._assert_emulebb_rust_package_provenance(layout, rust, provenance)
                report.update(status="passed", scope="kotlin-compilation", applicationQualification="not-run")
                return report_root / "build.json"
            stage_source_notices(rust, mini, pcp, notices)
            flavor = "debug" if debug else "release"
            gradle("emulebbRuntimeInventory", "runtime-classpath")
            runtime = stage_runtime_notices(runtime_inventory, notices, output / "cache/android-apk-notices", variant=flavor)
            report["runtimeClasspath"] = runtime
            _write_json(report_root / "runtime-classpath.json", runtime)
            gradle("lintDebug" if debug else "lintRelease", "gradle-lint")
            lint_xml = report_root / "lint" / ("lint-results-" + flavor + ".xml")
            if not lint_xml.is_file():
                raise RuntimeError("Gradle lint did not retain its actual XML report")
            report["lint"] = {"status": "passed", "task": "lintDebug" if debug else "lintRelease",
                              "report": lint_xml.relative_to(output).as_posix()}
            if lint_only:
                release._assert_emulebb_rust_package_provenance(layout, rust, provenance)
                report.update(status="passed", scope="gradle-lint", applicationQualification="not-run")
                return report_root / "build.json"
            execute("fetch-locked", ["cargo", "fetch", "--locked", "--manifest-path", "/source/Cargo.toml"], network="bridge")
            from .rust_source_cache import ensure_rust_source_cache
            invocation = 0
            def cache_cargo(arguments):
                nonlocal invocation
                invocation += 1
                return run_owned_container(docker, [*base, image, "cargo", *arguments],
                    report_root / f"source-cache-{invocation}.log", timeout=120, capture=True)
            ensure_rust_source_cache(layout, target=target, runner=cache_cargo,
                execution_source="/source", execution_target=env["CARGO_TARGET_DIR"],
                native_roots=(mini, pcp), execution_identity={"image": image, "inputs": tool_inputs()})
            report["native"] = {}
            for abi, (triple, _machine) in TARGETS.items():
                extra = _container_environment(compiler_environment(abi, min_api=MIN_SDK))
                execute(abi + "-jni", ["cargo", "build", "--locked", "--offline", "--release",
                    "--manifest-path", "/source/Cargo.toml", "--target", triple, "-p", "emulebb-android"], extra=extra)
                library = target / triple / "release/libemulebb_android.so"
                audit = audit_android_elf(library, abi, shared_library=True)
                readelf = "/opt/android-tools/sdk/ndk/" + tool_inputs()["assets"]["ndk"]["version"] + "/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-readelf"
                symbols = execute(abi + "-jni-exports", [readelf, "--dyn-syms", "--wide", path(library)], capture=True)
                if "Java_org_emulebb_android_NativeBridge_request__Ljava_lang_String_2" not in symbols:
                    raise RuntimeError("Android JNI library does not export its application bridge")
                dependencies = _runtime_dependencies(docker, base, image, output, report_root, abi, library)
                destination = native / abi
                destination.mkdir()
                shutil.copy2(library, destination / library.name)
                for runtime in dependencies["bundled"]:
                    shutil.copy2(output / runtime["path"], destination / runtime["name"])
                    shutil.copy2(output / runtime["licensePath"], notices / "Android-NDK-NOTICE")
                report["native"][abi] = {"elfAudit": audit, "sha256": release._sha256(library),
                                        "runtimeDependencies": dependencies}
            task = "assembleDebug" if debug else "assembleRelease"
            gradle(task, "gradle")
            flavor = "debug" if debug else "release"
            unsigned = scratch / "gradle-build/app/outputs/apk" / flavor / ("app-debug.apk" if debug else "app-release-unsigned.apk")
            names = public_names(release_version)
            signed = root / names["apk"]
            if signed.is_symlink():
                raise RuntimeError("APK output cannot replace a symlink")
            if debug:
                shutil.copy2(unsigned, signed)
                tools = "/opt/android-tools/sdk/build-tools/" + tool_inputs()["assets"]["build-tools"]["version"]
                certificate = execute("debug-signature", [tools + "/apksigner", "verify", "--print-certs", path(signed)], capture=True)
                signer = certificate_sha256(certificate)
            else:
                signer = _sign(docker, base=base, image=image, scratch=scratch, output=output,
                               unsigned=unsigned, signed=signed, report_root=report_root, signing=selected_signing)
                if signer != expected_cert_sha256:
                    raise RuntimeError("Signed APK differs from the expected public certificate identity")
            tools = "/opt/android-tools/sdk/build-tools/" + tool_inputs()["assets"]["build-tools"]["version"]
            execute("zip-alignment", [tools + "/zipalign", "-c", "-P", "16", "4", path(signed)])
            badging = execute("apk-manifest", [tools + "/aapt2", "dump", "badging", path(signed)], capture=True)
            audit_badging(badging, version=release_version, version_code=version_code)
            license_assets = notice_inventory(notices)
            audit_packaged_notices(signed, license_assets)
            sbom_root = scratch / "sbom-payload"
            sbom_root.mkdir()
            shutil.copy2(signed, sbom_root / signed.name)
            sbom = release._build_spdx_sbom(
                name=f"eMuleBB Rust {release_version} Android application",
                namespace=f"https://github.com/emulebb/emulebb-rust/releases/download/rust-v{release_version}/{names['sbom']}",
                package_name="emulebb-rust-android-universal", package_version=release_version,
                package_license="GPL-2.0-only", package_comment="Experimental Android application; device qualification remains separate.",
                package_root=sbom_root, release_root=root,
                components=[*release._emulebb_rust_sbom_components(layout, rust), *spdx_components(runtime)])
            if any(report["native"][abi]["runtimeDependencies"]["bundled"] for abi in TARGETS):
                component = release._component_spdx_package(name="Android NDK libc++ runtime",
                    declared_license="Apache-2.0 WITH LLVM-exception", version=tool_inputs()["assets"]["ndk"]["version"],
                    comment="Pinned Android NDK archive; exact per-ABI bytes and NOTICE are bound in the APK manifest.")
                sbom["packages"].append(component)
                sbom["relationships"].append({"spdxElementId": "SPDXRef-Package", "relationshipType": "DEPENDS_ON",
                                               "relatedSpdxElement": component["SPDXID"]})
            _write_json(root / names["sbom"], sbom)
            manifest = {**provenance, "schema": "emulebb.android.apk-package/1", "status": "passed",
                "applicationId": APPLICATION_ID, "minSdk": MIN_SDK, "targetSdk": TARGET_SDK,
                "versionCode": version_code, "experimental": True, "debug": debug,
                "source": provenance["source"], "buildIdentity": {"sourceRevision": expected_source_sha,
                "version": release_version, "channel": channel}, "signerSha256": signer,
                "runtimeExecution": "not-run", "abis": list(TARGETS), "toolInputs": tool_inputs(),
                "apk": {"path": signed.name, "sha256": release._sha256(signed), "sizeBytes": signed.stat().st_size},
                "native": report["native"], "runtimeClasspath": runtime, "notices": license_assets}
            manifest.update(sbom=names["sbom"], sbomSha256=release._sha256(root / names["sbom"]))
            release._assert_emulebb_rust_package_provenance(layout, rust, provenance)
            _write_json(root / names["manifest"], manifest)
            report["status"] = "passed"
            report["manifest"] = manifest
            release._write_sha256sums(root / "SHA256SUMS", (signed, root / names["manifest"], root / names["sbom"]))
            return signed
    finally:
        _write_json(report_root / "build.json", report)
        if scratch.is_symlink() or not scratch.resolve().is_relative_to(output / "tmp/android-apk"):
            raise RuntimeError("APK scratch ownership changed during production")
        shutil.rmtree(scratch)
