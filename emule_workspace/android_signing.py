"""Operator-owned, retained Android release keys and GitHub secret provisioning."""
from __future__ import annotations

import base64
import csv
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
SYSTEM_SID = "S-1-5-18"


def _operator_sid() -> str:
    rows = list(csv.reader(subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True).splitlines()))
    if len(rows) != 1 or len(rows[0]) != 2 or re.fullmatch(r"S-1-[0-9-]+", rows[0][1]) is None:
        raise RuntimeError("Cannot determine the current operator SID")
    return rows[0][1]


def _verify_acl(path: Path, sid: str, *, protected: bool = True) -> None:
    # WHY: chmod on Windows changes file attributes, not who can read a key.
    # Query the native ACL through PowerShell's structured .NET API; no parsing
    # of localized icacls prose or assumption about inherited directory access.
    script = (
        "$ErrorActionPreference='Stop';"
        "$path=$env:ANDROID_SIGNING_ACL_PATH;"
        "$item=if([System.IO.Directory]::Exists($path)){[System.IO.DirectoryInfo]::new($path)}"
        "else{[System.IO.FileInfo]::new($path)};"
        "$acl=$item.GetAccessControl();"
        "$rules=@($acl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]) | ForEach-Object { @{"
        "sid=$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value;"
        "kind=[string]$_.AccessControlType;rights=[int]$_.FileSystemRights} });"
        "@{protected=$acl.AreAccessRulesProtected;"
        "owner=$acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value;rules=$rules} | ConvertTo-Json -Depth 4 -Compress"
    )
    child = dict(os.environ, ANDROID_SIGNING_ACL_PATH=str(path))
    result = subprocess.check_output(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                     text=True, env=child)
    acl = json.loads(result)
    rules = acl.get("rules", [])
    if (acl.get("owner") != sid or (protected and not acl.get("protected")) or len(rules) != 2
            or {item.get("sid") for item in rules} != {sid, SYSTEM_SID}
            or any(item.get("kind") != "Allow" or item.get("rights") != 2_032_127 for item in rules)):
        raise RuntimeError("Android signing directory ACL is not restricted to operator and SYSTEM")


def private_directory(path: Path, *, create: bool) -> None:
    """Create or verify the retained signing root before handling private bytes."""
    if path.is_symlink():
        raise RuntimeError("Android signing directory cannot be a symlink")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
    if not path.is_dir():
        raise RuntimeError("Android signing directory is missing")
    if os.name == "nt":
        sid = _operator_sid()
        if create:
            # WHY: Python's Windows mode-0700 mkdir can create an explicit
            # Administrators ACE. Removing inheritance retains that broad grant;
            # construct a fresh native DACL with exactly the two intended SIDs.
            script = (
                "$ErrorActionPreference='Stop';"
                "$sid=[System.Security.Principal.SecurityIdentifier]::new($env:ANDROID_SIGNING_OPERATOR_SID);"
                "$system=[System.Security.Principal.SecurityIdentifier]::new('S-1-5-18');"
                "$acl=[System.Security.AccessControl.DirectorySecurity]::new();"
                "$acl.SetAccessRuleProtection($true,$false);$acl.SetOwner($sid);"
                "$flags=[System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor "
                "[System.Security.AccessControl.InheritanceFlags]::ObjectInherit;"
                "foreach($identity in @($sid,$system)){"
                "$rule=[System.Security.AccessControl.FileSystemAccessRule]::new($identity,"
                "[System.Security.AccessControl.FileSystemRights]::FullControl,$flags,"
                "[System.Security.AccessControl.PropagationFlags]::None,"
                "[System.Security.AccessControl.AccessControlType]::Allow);$acl.AddAccessRule($rule)};"
                "[System.IO.DirectoryInfo]::new($env:ANDROID_SIGNING_ACL_PATH).SetAccessControl($acl)"
            )
            child = dict(os.environ, ANDROID_SIGNING_ACL_PATH=str(path), ANDROID_SIGNING_OPERATOR_SID=sid)
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                           env=child, check=True, capture_output=True)
        _verify_acl(path, sid)
    else:
        stat = path.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise RuntimeError("Android signing directory must be private to the current operator")


def _verify_material(root: Path) -> None:
    for name in ("release.p12", "credentials.json", "identity.json"):
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Android signing material must be regular private files")
        if os.name == "nt":
            _verify_acl(path, _operator_sid(), protected=False)
        elif path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise RuntimeError("Android signing material has unsafe permissions")


def _exclusive_json(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def retained_signing_inputs(output: Path) -> tuple[dict[str, str], str]:
    """Read explicitly selected retained credentials only after native privacy checks."""
    root = output.resolve() / "tools/signing/android"
    private_directory(root, create=False)
    _verify_material(root)
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
    """Create once, verify on every retry, and send secrets through gh stdin only.

    The retained key and credentials are the operator's long-lived backup.
    Failed GitHub uploads never delete or regenerate them; a retry sends the
    same complete secret set from the verified local identity.
    """
    output = layout.output_root.resolve()
    root = output / "tools/signing/android"
    if not root.resolve().is_relative_to(output):
        raise RuntimeError("Android signing material must remain inside the inherited output root")
    listed = json.loads(subprocess.check_output(
        ["gh", "secret", "list", "--repo", REPOSITORY, "--json", "name"], text=True))
    existing = {item["name"] for item in listed}
    create = not root.exists()
    if create and existing.intersection(GITHUB_SECRETS):
        raise RuntimeError("GitHub signing secrets already exist without the retained local identity")
    if not create:
        private_directory(root, create=False)
        if any(not (root / name).is_file() or (root / name).is_symlink()
               for name in ("release.p12", "credentials.json", "identity.json")):
            raise RuntimeError("Partial Android signing state requires operator inspection; no key was overwritten")
        _verify_material(root)
    prepare_toolchain(layout, wsl_distro=wsl_distro)
    if create:
        private_directory(root, create=True)
        password = secrets.token_urlsafe(48)
        _exclusive_json(root / "credentials.json", {"password": password, "alias": "release"})
    else:
        credentials = json.loads((root / "credentials.json").read_text(encoding="utf-8"))
        password = credentials["password"]
        if credentials.get("alias") != "release" or not isinstance(password, str) or not password:
            raise RuntimeError("Invalid retained Android signing credentials")
    report = output / "reports/android-signing"
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
            if os.name != "nt":
                (root / "release.p12").chmod(0o600)
        fingerprint = _certificate_fingerprint(docker, base, password=password, log=report / "certificate.log")
    if create:
        _exclusive_json(root / "identity.json", {"schema": "emulebb.android.signing-identity/1",
            "repository": REPOSITORY, "alias": "release", "signerSha256": fingerprint})
    else:
        identity = json.loads((root / "identity.json").read_text(encoding="utf-8"))
        if identity.get("repository") != REPOSITORY or identity.get("signerSha256") != fingerprint:
            raise RuntimeError("Retained Android signing key differs from its recorded public identity")
    private_directory(root, create=False)
    _verify_material(root)
    values = (base64.b64encode((root / "release.p12").read_bytes()).decode("ascii"), "release", password, password)
    for name, value in zip(GITHUB_SECRETS, values):
        subprocess.run(["gh", "secret", "set", name, "--repo", REPOSITORY],
                       input=value, text=True, check=True, capture_output=True)
    return {"signerSha256": fingerprint, "retainedDirectory": str(root), "repository": REPOSITORY}
