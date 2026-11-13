"""APK production must isolate signing secrets and preserve daemon build defaults."""
import os
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from emule_workspace import android_apk as apk
from emule_workspace.android_validation import compiler_environment


def test_readonly_apk_container_selects_installed_rust_toolchain_explicitly():
    # The source toolchain file otherwise asks rustup to synchronize into the
    # immutable image's /usr/local/rustup before Cargo can execute at all.
    assert '"RUSTUP_TOOLCHAIN": "1.98.1"' in inspect.getsource(apk.package_apk)


@pytest.mark.parametrize("version", ["9.5.1", "9.2.0"])
def test_tools_probe_uses_the_source_owned_gradle_plugin_pin(tmp_path, version):
    project = tmp_path / "android"
    project.mkdir()
    (project / "build.gradle.kts").write_text(f'plugins {{ id("com.android.application") version "{version}" apply false }}')
    assert apk.gradle_plugin_version(tmp_path) == version


@pytest.mark.parametrize("version", ["9.+", "latest.release", "9.5.1-alpha01"])
def test_tools_probe_rejects_floating_or_prerelease_plugin_pins(tmp_path, version):
    project = tmp_path / "android"
    project.mkdir()
    (project / "build.gradle.kts").write_text(f'plugins {{ id("com.android.application") version "{version}" apply false }}')
    with pytest.raises(RuntimeError, match="pin"):
        apk.gradle_plugin_version(tmp_path)


def test_apk_api_floor_does_not_change_cli_compilation():
    for abi in ("arm64-v8a", "x86_64"):
        assert any("android28-clang" in value for value in compiler_environment(abi).values())
        assert any("android34-clang" in value for value in compiler_environment(abi, min_api=34).values())
    with pytest.raises(ValueError):
        compiler_environment("arm64-v8a", min_api=1)


def test_public_apk_names_cannot_escape_output():
    assert apk.public_names("0.1.0-beta.2.nightly.20261113.gabcdef0")["apk"].endswith("android-universal.apk")
    for value in ("../payload", "version/other", ""):
        with pytest.raises(ValueError):
            apk.public_names(value)


@pytest.mark.parametrize("change", [None, "name", "versionName", "versionCode", "minSdkVersion", "native-code"])
def test_packaged_manifest_is_verified_against_release(change):
    text = ("package: name='org.emulebb.android' versionName='0.1.0' versionCode='1'\n"
            "minSdkVersion:'34'\ntargetSdkVersion:'37'\nnative-code: 'arm64-v8a' 'x86_64'\n")
    if change is None:
        apk.audit_badging(text, version="0.1.0", version_code=1)
    else:
        altered = text.replace(change, "unrecognized-field", 1)
        with pytest.raises(RuntimeError):
            apk.audit_badging(altered, version="0.1.0", version_code=1)


def test_pinned_aapt2_badging_uses_explicit_min_sdk_version_field():
    version = "0.1.0-beta.2.nightly.20261113.g09bbc2d1"
    text = ("package: name='org.emulebb.android' versionCode='1' "
        f"versionName='{version}' platformBuildVersionName='17' platformBuildVersionCode='37' "
        "compileSdkVersion='37' compileSdkVersionCodename='17'\n"
        "minSdkVersion:'34'\ntargetSdkVersion:'37'\n"
        "uses-permission: name='android.permission.FOREGROUND_SERVICE_SPECIAL_USE'\n"
        "application-label:'eMuleBB'\napplication: label='eMuleBB' icon=''\n"
        "launchable-activity: name='org.emulebb.android.MainActivity'  label='' icon=''\n"
        "native-code: 'arm64-v8a' 'x86_64'\n")
    apk.audit_badging(text, version=version, version_code=1)
    with pytest.raises(RuntimeError, match="SDK bounds"):
        apk.audit_badging(text.replace("minSdkVersion", "sdkVersion"), version=version, version_code=1)
    with pytest.raises(RuntimeError, match="SDK bounds"):
        apk.audit_badging(text + "minSdkVersion:'34'\n", version=version, version_code=1)


def test_missing_release_signing_fails_before_build(monkeypatch):
    for name in apk.SIGNING_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(apk, "prepare_toolchain", lambda *_args, **_kwargs: pytest.fail("must not build"))
    with pytest.raises(RuntimeError, match="four"):
        apk.package_apk(SimpleNamespace(), release_version="0.1.0-beta.2.nightly.20261113.g" + "a" * 7,
                        channel="nightly", expected_source_sha="a" * 40, version_code=1)


@pytest.mark.parametrize("expected", [None, "", "A" * 64, "a" * 63])
def test_production_requires_independently_supplied_signer_authority(tmp_path, monkeypatch, expected):
    _signing(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="expected certificate"):
        apk.package_apk(SimpleNamespace(), release_version="0.1.0-beta.2.nightly.20261113.gaaaaaaa",
            channel="nightly", expected_source_sha="a" * 40, version_code=1, expected_cert_sha256=expected)


@pytest.mark.parametrize("text", ["V2 Signer: certificate SHA-256 digest: " + "A" * 64,
    "Signer #1 certificate SHA-256 digest: " + "A" * 64,
    "V2 Signer: certificate SHA-256 digest: " + "a" * 64 + "\nV3 Signer: certificate SHA-256 digest: " + "a" * 64])
def test_pinned_apksigner_formats_identify_one_certificate_across_schemes(text):
    assert apk.certificate_sha256(text) == "a" * 64


@pytest.mark.parametrize("scheme", ["V3.0", "V3.1", "V3.2"])
def test_pinned_verbose_production_signer_output_uses_certificate_and_single_signer_count(scheme):
    text = ("Verifies\n"
        "Verified using v1 scheme (JAR signing): false\n"
        "Verified using v2 scheme (APK Signature Scheme v2): false\n"
        "Verified using v3 scheme (APK Signature Scheme v3): true\n"
        "Verified using v3.1 scheme (APK Signature Scheme v3.1): false\n"
        "Verified using v3.2 scheme (APK Signature Scheme v3.2): false\n"
        "Verified using v4 scheme (APK Signature Scheme v4): false\n"
        "Verified for SourceStamp: false\n"
        "Number of signers: 1\n"
        f"{scheme} Signer: certificate DN: CN=eMuleBB\n"
        f"{scheme} Signer: certificate SHA-256 digest: " + "a" * 64 + "\n"
        f"{scheme} Signer: key algorithm: RSA\n"
        f"{scheme} Signer: key size (bits): 4096\n"
        f"{scheme} Signer: public key SHA-256 digest: " + "b" * 64 + "\n")
    assert apk.certificate_sha256(text) == "a" * 64


@pytest.mark.parametrize("count", ["0", "2", "01", "1\nNumber of signers: 1"])
def test_verbose_signer_count_must_identify_exactly_one_signer(count):
    text = "Number of signers: " + count + "\nV3.0 Signer: certificate SHA-256 digest: " + "a" * 64
    with pytest.raises(RuntimeError, match="exactly one signer"):
        apk.certificate_sha256(text)


@pytest.mark.parametrize("text", ["a" * 64,
    "prefix V2 Signer: certificate SHA-256 digest: " + "a" * 64,
    "Signer #2 certificate SHA-256 digest: " + "a" * 64,
    "V2 Signer: certificate SHA-256 digest: " + "a" * 64 + "suffix",
    "V2 Signer: certificate SHA-256 digest: " + "a" * 64 + "\nV3 Signer: certificate SHA-256 digest: " + "b" * 64,
    ("V2 Signer: certificate SHA-256 digest: " + "a" * 64 + "\n") * 2])
def test_ambiguous_multiple_or_unanchored_signer_evidence_is_rejected(text):
    with pytest.raises(RuntimeError, match="exactly one signer"):
        apk.certificate_sha256(text)


@pytest.mark.parametrize("debug,retained,author", [(False, False, True), (True, True, False)])
def test_authoring_and_retained_signing_modes_cannot_be_mixed(debug, retained, author):
    with pytest.raises(ValueError):
        apk.package_apk(SimpleNamespace(), release_version="0.1.0-beta.2.nightly.20261113.gaaaaaaa",
            channel="nightly", expected_source_sha="a" * 40, version_code=1, debug=debug,
            retained_signing=retained, author_gradle_verification=author)


def test_retained_passwords_are_scoped_to_signer_child(tmp_path, monkeypatch):
    scratch = _signing(tmp_path, monkeypatch)
    selected = {name: os.environ[name] for name in apk.SIGNING_ENV}
    selected[apk.SIGNING_ENV[2]] = "synthetic-retained-password"
    selected[apk.SIGNING_ENV[3]] = "synthetic-retained-password"
    def run(_docker, arguments, _log, **kwargs):
        if "sign" in arguments:
            assert kwargs["environment"][apk.SIGNING_ENV[2]] == "synthetic-retained-password"
            assert "synthetic-retained-password" not in " ".join(arguments)
        return "Signer #1 certificate SHA-256 digest: " + "a" * 64
    monkeypatch.setattr(apk, "run_owned_container", run)
    apk._sign(SimpleNamespace(path=str), base=[], image="reviewed", scratch=scratch, output=tmp_path,
        unsigned=tmp_path / "unsigned.apk", signed=tmp_path / "signed.apk", report_root=tmp_path, signing=selected)
    assert os.environ[apk.SIGNING_ENV[2]] == "synthetic-store-secret"


def _signing(tmp_path, monkeypatch):
    key = tmp_path / "operator.keystore"
    key.write_bytes(b"synthetic-keystore")
    for name, value in zip(apk.SIGNING_ENV, (str(key), "release", "synthetic-store-secret", "synthetic-key-secret")):
        monkeypatch.setenv(name, value)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return scratch


@pytest.mark.parametrize("fail", [False, True])
def test_signing_uses_env_references_and_always_removes_copied_key(tmp_path, monkeypatch, fail):
    scratch = _signing(tmp_path, monkeypatch)
    calls = []
    def run(_docker, arguments, _log, **kwargs):
        calls.append((arguments, kwargs))
        if "sign" in arguments:
            assert (scratch / "signing/release.keystore").read_bytes() == b"synthetic-keystore"
            if fail:
                raise RuntimeError("synthetic signing failure")
        return "Signer #1 certificate SHA-256 digest: " + "a" * 64
    monkeypatch.setattr(apk, "run_owned_container", run)
    kwargs = dict(base=[], image="reviewed", scratch=scratch, output=tmp_path,
                  unsigned=tmp_path / "unsigned.apk", signed=tmp_path / "signed.apk", report_root=tmp_path)
    docker = SimpleNamespace(path=lambda value: str(value))
    if fail:
        with pytest.raises(RuntimeError, match="synthetic"):
            apk._sign(docker, **kwargs)
    else:
        assert apk._sign(docker, **kwargs) == "a" * 64
    assert not (scratch / "signing").exists()
    for arguments, options in calls:
        assert "synthetic-store-secret" not in " ".join(arguments)
        assert "synthetic-key-secret" not in " ".join(arguments)
        if "sign" in arguments:
            assert "env:EMULEBB_ANDROID_KEYSTORE_PASSWORD" in arguments
            assert options["environment"]["EMULEBB_ANDROID_KEY_PASSWORD"] == "synthetic-key-secret"
        else:
            assert "environment" not in options


def test_bad_keystore_and_alias_fail_before_signing(tmp_path):
    environment = dict(zip(apk.SIGNING_ENV, ("relative", "release", "synthetic", "synthetic")))
    with pytest.raises(RuntimeError, match="absolute"):
        apk.signing_inputs(environment)
    key = tmp_path / "key"
    key.write_bytes(b"synthetic")
    environment[apk.SIGNING_ENV[0]] = str(key)
    environment[apk.SIGNING_ENV[1]] = "release\nother"
    with pytest.raises(RuntimeError, match="alias"):
        apk.signing_inputs(environment)


@pytest.mark.parametrize("scope,fail_task", [("kotlin", None), ("kotlin", "compileDebugKotlin"),
                                           ("lint", None), ("lint", "lintDebug")])
def test_gradle_preflight_never_compiles_native_or_claims_an_apk(tmp_path, monkeypatch, scope, fail_task):
    rust, output = tmp_path / "rust", tmp_path / "output"
    (rust / "android").mkdir(parents=True)
    (rust / "android/build.gradle.kts").write_text("synthetic staged Gradle source")
    license_text = "Synthetic License\nCopyright Synthetic Authors\n" + "redistribution permitted " * 4
    (rust / "LICENSE").write_text(license_text)
    mini, pcp = tmp_path / "mini", tmp_path / "pcp"
    (mini / "miniupnpc").mkdir(parents=True)
    pcp.mkdir()
    (mini / "LICENSE").write_text(license_text)
    (mini / "miniupnpc/LICENSE").write_text("../LICENSE")
    (pcp / "COPYING").write_text(license_text)
    monkeypatch.setattr(apk, "stage_runtime_notices", lambda *args, **kwargs: {"components": []})
    source = "a" * 40
    version = "0.1.0-beta.2.nightly.20261113.gaaaaaaa"
    monkeypatch.setattr(apk.release, "_emulebb_rust_package_provenance", lambda *_args:
                        {"source": {"emulebbRust": {"commit": source}}})
    monkeypatch.setattr(apk.release, "_assert_emulebb_rust_package_provenance", lambda *_args: None)
    monkeypatch.setattr(apk.release, "_emulebb_rust_workspace_version", lambda *_args: "0.1.0-beta.2")
    monkeypatch.setattr(apk.release, "_emulebb_rust_native_source_roots", lambda *_args: (mini, pcp))
    monkeypatch.setattr(apk, "prepare_toolchain", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(apk, "_builder_user", lambda _docker: "1000:1000")
    monkeypatch.setenv("EMULEBB_ANDROID_KEYSTORE_PASSWORD", "synthetic-build-secret")
    def webui(arguments, **kwargs):
        assert not set(apk.SIGNING_ENV).intersection(kwargs["env"])
        directory = Path(arguments[arguments.index("--stage-dir") + 1])
        directory.mkdir()
        (directory / "index.html").write_text("synthetic webui")
    monkeypatch.setattr(apk.subprocess, "run", webui)
    class Docker:
        def __init__(self, _distro): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def path(self, value): return str(value)
    monkeypatch.setattr(apk, "LinuxDocker", Docker)
    calls = []
    def execute(_docker, arguments, _log, **_kwargs):
        calls.append(arguments)
        assert "cargo" not in arguments
        task = arguments[-1].removeprefix(":app:")
        assert task in {"compileDebugKotlin", "lintDebug", "emulebbRuntimeInventory"}
        if task == "lintDebug":
            build_root = Path(next(argument.split("=", 1)[1] for argument in arguments
                                   if argument.startswith("-PandroidBuildRoot=")))
            reports = output / build_root.relative_to("/output") / "app/reports"
            reports.mkdir(parents=True)
            (reports / "lint-results-debug.xml").write_text('<issues format="6"/>')
        if task == fail_task:
            raise RuntimeError("synthetic Gradle error")
        return ""
    monkeypatch.setattr(apk, "run_owned_container", execute)
    layout = SimpleNamespace(output_root=output, emulebb_rust_repo_root=rust)
    parameters = dict(release_version=version, channel="nightly", expected_source_sha=source,
                      version_code=1, debug=True, author_gradle_verification=True,
                      kotlin_only=scope == "kotlin", lint_only=scope == "lint")
    if fail_task:
        with pytest.raises(RuntimeError, match="Gradle"):
            apk.package_apk(layout, **parameters)
    else:
        report_path = apk.package_apk(layout, **parameters)
        report = json.loads(report_path.read_text())
        assert report["scope"] == ("kotlin-compilation" if scope == "kotlin" else "gradle-lint")
        assert report["runtimeExecution"] == "not-run"
        assert "manifest" not in report
    if scope == "lint":
        retained = list((output / "reports").rglob("lint-results-debug.xml"))
        assert len(retained) == 1 and retained[0].read_text() == '<issues format="6"/>'
    assert len(calls) == (1 if scope == "kotlin" else 3)
    assert not list(output.rglob("*.apk"))
