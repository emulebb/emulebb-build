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


@pytest.mark.parametrize("change", [None, "name", "versionName", "versionCode", "sdkVersion", "native-code"])
def test_packaged_manifest_is_verified_against_release(change):
    text = ("package: name='org.emulebb.android' versionName='0.1.0' versionCode='1'\n"
            "sdkVersion:'34'\ntargetSdkVersion:'37'\nnative-code: 'arm64-v8a' 'x86_64'\n")
    if change is None:
        apk.audit_badging(text, version="0.1.0", version_code=1)
    else:
        altered = text.replace(change, "unrecognized-field", 1)
        with pytest.raises(RuntimeError):
            apk.audit_badging(altered, version="0.1.0", version_code=1)


def test_missing_release_signing_fails_before_build(monkeypatch):
    for name in apk.SIGNING_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(apk, "prepare_toolchain", lambda *_args, **_kwargs: pytest.fail("must not build"))
    with pytest.raises(RuntimeError, match="four"):
        apk.package_apk(SimpleNamespace(), release_version="0.1.0-beta.2.nightly.20261113.g" + "a" * 7,
                        channel="nightly", expected_source_sha="a" * 40, version_code=1)


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


@pytest.mark.parametrize("fail_compile", [False, True])
def test_kotlin_preflight_never_compiles_native_or_claims_an_apk(tmp_path, monkeypatch, fail_compile):
    rust, output = tmp_path / "rust", tmp_path / "output"
    (rust / "android").mkdir(parents=True)
    (rust / "android/build.gradle.kts").write_text("synthetic staged Gradle source")
    (rust / "LICENSE").write_text("synthetic license")
    mini, pcp = tmp_path / "mini", tmp_path / "pcp"
    (mini / "miniupnpc").mkdir(parents=True)
    pcp.mkdir()
    (mini / "miniupnpc/LICENSE").write_text("synthetic license")
    (pcp / "COPYING").write_text("synthetic license")
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
        assert ":app:compileDebugKotlin" in arguments
        if fail_compile:
            raise RuntimeError("synthetic Kotlin error")
        return ""
    monkeypatch.setattr(apk, "run_owned_container", execute)
    layout = SimpleNamespace(output_root=output, emulebb_rust_repo_root=rust)
    parameters = dict(release_version=version, channel="nightly", expected_source_sha=source,
                      version_code=1, debug=True, author_gradle_verification=True, kotlin_only=True)
    if fail_compile:
        with pytest.raises(RuntimeError, match="Kotlin"):
            apk.package_apk(layout, **parameters)
    else:
        report_path = apk.package_apk(layout, **parameters)
        report = json.loads(report_path.read_text())
        assert report["scope"] == "kotlin-compilation" and report["runtimeExecution"] == "not-run"
        assert "manifest" not in report
    assert len(calls) == 1
    assert not list(output.rglob("*.apk"))
