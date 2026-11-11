"""Bounded Ubuntu package provisioning without rewriting host repository files."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile
import time


PROFILES = {
    "rust-native-release": ("dpkg-dev", "appstream", "desktop-file-utils"),
    "rust-prepared-builder": (
        "build-essential", "ca-certificates", "cmake", "curl", "dpkg-dev", "git", "libatomic1",
        "libssl-dev", "perl", "pkg-config", "zstd",
        "libbz2-1.0", "libffi8", "liblzma5", "libncursesw6", "libreadline8t64",
        "libsqlite3-0", "libuuid1", "zlib1g", "appstream", "desktop-file-utils",
    ),
}
KEYRING = Path("/usr/share/keyrings/ubuntu-archive-keyring.gpg")


class ProvisioningError(RuntimeError):
    """Retain bounded public evidence when a required provisioning step fails."""

    def __init__(self, message: str, receipt: dict):
        super().__init__(message)
        self.receipt = receipt


def ubuntu_sources(codename: str, architecture: str, keyring: Path = KEYRING) -> str:
    """Select official HTTPS archives and keep Ubuntu signature verification."""
    if not re.fullmatch(r"[a-z][a-z0-9-]*", codename):
        raise ValueError("Ubuntu requires a valid VERSION_CODENAME")
    if architecture not in {"amd64", "arm64"}:
        raise ValueError("Ubuntu CI provisioning supports only amd64 and arm64")
    archive = ("https://archive.ubuntu.com/ubuntu" if architecture == "amd64"
               else "https://ports.ubuntu.com/ubuntu-ports")
    security = ("https://security.ubuntu.com/ubuntu" if architecture == "amd64" else archive)
    return "".join(
        f"deb [arch={architecture} signed-by={keyring}] {uri} {suite} main universe\n"
        for uri, suite in ((archive, codename), (archive, codename + "-updates"),
                           (security, codename + "-security"))
    )


def provision_dependencies(*, profile: str, deadline_seconds: int = 300,
                           network_timeout_seconds: int = 20, attempts: int = 3,
                           runner=subprocess.run, clock=time.monotonic,
                           os_release=None, keyring: Path = KEYRING,
                           scratch_root: Path | None = None, privileged: bool | None = None) -> dict:
    """Run signed Ubuntu-only APT operations within a single wall-clock budget.

    Callers own CI/image-build authorization and output selection. All original
    sources, including unrelated vendor repositories, remain untouched. GNU
    timeout runs with APT's privileges so its deadline also reaches root children.
    """
    if profile not in PROFILES:
        raise ValueError("Unknown Ubuntu provisioning profile")
    if not 10 <= deadline_seconds <= 900 or not 1 <= network_timeout_seconds <= 60 or not 1 <= attempts <= 5:
        raise ValueError("Provisioning requires bounded deadline, request timeout and attempts")
    release = platform.freedesktop_os_release() if os_release is None else os_release
    if release.get("ID") != "ubuntu" or not keyring.is_file():
        raise RuntimeError("Provisioning requires Ubuntu and its installed archive signing keyring")
    started = clock()
    deadline = started + deadline_seconds
    receipt = {"schema": "emulebb.ubuntu-ci-dependencies/1", "status": "running", "profile": profile,
               "packages": list(PROFILES[profile]), "codename": release.get("VERSION_CODENAME", ""),
               "deadlineSeconds": deadline_seconds, "networkTimeoutSeconds": network_timeout_seconds,
               "maximumAttempts": attempts, "operations": [], "hostSourcesModified": False}
    root = (os.geteuid() == 0) if privileged is None else privileged

    def command(arguments: list[str], *, elevated: bool = True):
        remaining = deadline - clock()
        if remaining <= 4:
            raise ProvisioningError("Ubuntu provisioning exhausted its overall deadline", receipt)
        # WHY: Python killing sudo alone can leave privileged APT children alive.
        # Coreutils timeout owns the privileged process group and bounded cleanup.
        argv = ([] if root or not elevated else ["sudo", "--non-interactive", "--"])
        argv += ["timeout", "--signal=TERM", "--kill-after=3s", f"{remaining - 3:.3f}s",
                 "env", "DEBIAN_FRONTEND=noninteractive", "LC_ALL=C", *arguments]
        try:
            return runner(argv, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise ProvisioningError("Ubuntu provisioning reached its overall deadline", receipt) from error

    try:
        architecture = command(["dpkg", "--print-architecture"], elevated=False)
        if architecture.returncode:
            raise ProvisioningError("Could not determine Ubuntu package architecture", receipt)
        architecture = architecture.stdout.strip()
        receipt["architecture"] = architecture
        sources = ubuntu_sources(receipt["codename"], architecture, keyring)
        receipt["sources"] = sources.splitlines()
        with tempfile.TemporaryDirectory(prefix="emulebb-ubuntu-apt-", dir=scratch_root) as directory:
            source_file = Path(directory) / "ubuntu.list"
            source_file.write_text(sources, encoding="utf-8")
            # APT's sandbox may read this public list; it contains no credentials.
            Path(directory).chmod(0o755)
            options = ["-o", f"Dir::Etc::sourcelist={source_file}", "-o", "Dir::Etc::sourceparts=-",
                       "-o", "APT::Get::List-Cleanup=false", "-o", "APT::Update::Error-Mode=any",
                       "-o", "Acquire::Retries=0", "-o", f"Acquire::https::Timeout={network_timeout_seconds}",
                       "-o", f"Acquire::http::Timeout={network_timeout_seconds}",
                       "-o", "Acquire::https::AllowRedirect=false", "-o", "Acquire::http::AllowRedirect=false",
                       "-o", "Acquire::https::Verify-Peer=true", "-o", "Acquire::https::Verify-Host=true",
                       "-o", "Acquire::AllowInsecureRepositories=false", "-o", "APT::Get::AllowUnauthenticated=false"]
            for operation, tail in (("update", ["update"]),
                                    ("install", ["install", "--no-install-recommends", "-y", *PROFILES[profile]])):
                for attempt in range(1, attempts + 1):
                    result = command(["apt-get", *options, *tail])
                    receipt["operations"].append({"operation": operation, "attempt": attempt,
                                                  "returncode": result.returncode})
                    if result.returncode == 0:
                        break
                    if result.returncode in {124, 137} or attempt == attempts:
                        receipt["diagnostic"] = result.stdout[-4000:]
                        raise ProvisioningError(f"Required Ubuntu {operation} failed within its bounded attempts", receipt)
            versions = command(["dpkg-query", "--show", "--showformat=${Package}\t${Version}\t${Architecture}\\n",
                                *PROFILES[profile]], elevated=False)
            if versions.returncode:
                raise ProvisioningError("Cannot record installed Ubuntu dependency versions", receipt)
            receipt["installedPackages"] = versions.stdout.splitlines()
        receipt["status"] = "passed"
        return receipt
    except ProvisioningError:
        receipt["status"] = "failed"
        raise
    finally:
        receipt["elapsedSeconds"] = round(clock() - started, 3)


def write_receipt(receipt: dict, path: Path | None) -> str:
    """Write only public provisioning evidence to the caller's selected output."""
    value = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if path is not None:
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("Ubuntu provisioning report requires an absolute regular output path")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    return value


def image_build_main(argv: list[str] | None = None) -> None:
    """Provision the build image and retain bounded public failure evidence."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-build", action="store_true", required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("Image construction requires the build container's root user")
    try:
        receipt = provision_dependencies(profile="rust-prepared-builder", deadline_seconds=600)
    except ProvisioningError as error:
        # WHY: failed Docker layers disappear; emit their bounded public evidence
        # before preserving the original provisioning failure and nonzero exit.
        print(write_receipt(error.receipt, arguments.report), flush=True)
        raise
    print(write_receipt(receipt, arguments.report), flush=True)


if __name__ == "__main__":
    # Private image-construction entrypoint; hosted callers use the shared CLI.
    image_build_main()
