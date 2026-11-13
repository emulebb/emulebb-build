"""Retained operator-profile Android keys and GitHub secret provisioning."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import secrets
import subprocess

from .android_apk import SIGNING_ENV
from .android_toolchain import prepare_toolchain
from .linux_containers import LinuxDocker, _builder_user, run_owned_container

REPOSITORY = "emulebb/emulebb-rust"
GITHUB_SECRETS = ("EMULEBB_ANDROID_KEYSTORE_BASE64", *SIGNING_ENV[1:])


def signing_root() -> Path:
    """Use normal operator-profile storage outside workspace cleanup."""
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if not local or not Path(local).is_absolute():
            raise RuntimeError("Android key storage requires the operator LOCALAPPDATA directory")
        return Path(local) / "eMuleBB/signing/android"
    return Path.home() / ".local/share/emulebb/signing/android"


def _exclusive_json(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def retained_signing_inputs() -> tuple[dict[str, str], str]:
    """Read explicitly selected retained credentials without changing operator environment."""
    root = signing_root()
    if any(not (root / name).is_file() or (root / name).is_symlink()
           for name in ("release.p12", "credentials.json", "identity.json")):
        raise RuntimeError("Partial Android signing state requires operator inspection; no key was overwritten")
    credentials = json.loads((root / "credentials.json").read_text(encoding="utf-8"))
    identity = json.loads((root / "identity.json").read_text(encoding="utf-8"))
    fingerprint = identity.get("signerSha256", "")
    password = credentials.get("password")
    if (identity.get("schema") != "emulebb.android.signing-identity/1"
            or identity.get("repository") != REPOSITORY or identity.get("alias") != "release"
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            or credentials.get("alias") != "release" or not isinstance(password, str) or not password):
        raise RuntimeError("Retained Android signing identity is incomplete or inconsistent")
    return dict(zip(SIGNING_ENV, (str(root / "release.p12"), "release", password, password))), fingerprint


def _signing_child(password: str) -> dict[str, str]:
    child = dict(os.environ)
    child[SIGNING_ENV[2]] = password
    if os.name == "nt":
        child["WSLENV"] = ":".join(filter(None, (child.get("WSLENV", ""), SIGNING_ENV[2])))
    return child


def _certificate_fingerprint(docker, base: list[str], *, password: str, log: Path) -> str:
    text = run_owned_container(docker, [*base, "keytool", "-list", "-v", "-alias", "release",
        "-keystore", "/signing/release.p12", "-storepass:env", SIGNING_ENV[2]],
        log, timeout=120, capture=True, environment=_signing_child(password))
    matches = re.findall(r"SHA256:\s*([0-9A-Fa-f:]{95})", text)
    if len(matches) != 1:
        raise RuntimeError("Cannot verify the retained Android signing certificate")
    return matches[0].replace(":", "").lower()


def provision_signing(layout, *, wsl_distro: str = "Ubuntu") -> dict:
    """Create once and upload through gh stdin; retries always reuse the same key."""
    root = signing_root()
    existing = {item["name"] for item in json.loads(subprocess.check_output(
        ["gh", "secret", "list", "--repo", REPOSITORY, "--json", "name"], text=True))}
    create = not root.exists()
    if create and existing.intersection(GITHUB_SECRETS):
        raise RuntimeError("GitHub signing secrets already exist without the retained local identity")
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        password = secrets.token_urlsafe(48)
        _exclusive_json(root / "credentials.json", {"password": password, "alias": "release"})
        expected = None
    else:
        selected, expected = retained_signing_inputs()
        password = selected[SIGNING_ENV[2]]
    prepare_toolchain(layout, wsl_distro=wsl_distro)
    report = layout.output_root.resolve() / "reports/android-signing"
    report.mkdir(parents=True, exist_ok=True)
    with LinuxDocker(wsl_distro) as docker:
        user = _builder_user(docker)
        if user.split(":")[0] == "0":
            raise RuntimeError("Android key provisioning requires a non-root operator")
        base = ["--rm", "--platform", "linux/amd64", "--user", user, "--read-only",
                "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=64m,mode=1777", "--env", SIGNING_ENV[2],
                "--mount", f"type=bind,source={docker.path(root)},target=/signing" + ("" if create else ",readonly"),
                "local/emulebb-android-toolchain:reviewed"]
        if create:
            run_owned_container(docker, [*base, "keytool", "-genkeypair", "-noprompt", "-alias", "release",
                "-keyalg", "RSA", "-keysize", "4096", "-sigalg", "SHA256withRSA", "-validity", "36500",
                "-dname", "CN=eMuleBB", "-storetype", "PKCS12", "-keystore", "/signing/release.p12",
                "-storepass:env", SIGNING_ENV[2], "-keypass:env", SIGNING_ENV[2]],
                report / "keytool.log", timeout=120, environment=_signing_child(password))
        fingerprint = _certificate_fingerprint(docker, base, password=password, log=report / "certificate.log")
    if create:
        _exclusive_json(root / "identity.json", {"schema": "emulebb.android.signing-identity/1",
            "repository": REPOSITORY, "alias": "release", "signerSha256": fingerprint})
    elif fingerprint != expected:
        raise RuntimeError("Retained Android signing key differs from its recorded public identity")
    values = (base64.b64encode((root / "release.p12").read_bytes()).decode("ascii"), "release", password, password)
    for name, value in zip(GITHUB_SECRETS, values):
        subprocess.run(["gh", "secret", "set", name, "--repo", REPOSITORY],
                       input=value, text=True, check=True, capture_output=True)
    return {"signerSha256": fingerprint, "retainedDirectory": str(root), "repository": REPOSITORY}
