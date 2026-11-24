"""Read-only replay of the published Rust nightly assembly path."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path


REPOSITORY = "emulebb/emulebb-rust"
ARTIFACT_PREFIX = "emulebb-rust-"
ARCHIVE_LIMIT = 1024**3
NATIVE = ("windows-x64", "windows-arm64", "linux-x64", "linux-arm64", "macos-x64", "macos-arm64")
MUSL = ("amd64", "arm64")
VARIANTS = ("ubuntu", "alpine")
ABIS = ("arm64-v8a", "x86_64")
REPOSITORIES = {
    "rust": ("emulebb-rust", None),
    "producer": ("emulebb-build", "EMULEBB_BUILD_REF"),
    "validator": ("emulebb-build", "EMULEBB_RELEASE_VALIDATOR_REF"),
    "image-tests": ("emulebb-build-tests", "EMULEBB_BUILD_TESTS_REF"),
    "apk-tests": ("emulebb-build-tests", "EMULEBB_APK_BUILD_TESTS_REF"),
    "tooling": ("emulebb-tooling", "EMULEBB_TOOLING_REF"),
    "miniupnp": ("third_party/emulebb-miniupnp", "MINIUPNP_REF"),
    "pcpnatpmp": ("third_party/emulebb-libpcpnatpmp", "PCPNATPMP_REF"),
}


def _github(resource: str) -> dict:
    response = subprocess.run(
        ["gh", "api", f"repos/{REPOSITORY}/actions/{resource}"],
        check=True, capture_output=True, encoding="utf-8", timeout=60,
    )
    return json.loads(response.stdout)


def _timestamp(value: str) -> datetime:
    time = datetime.fromisoformat(value)
    if time.utcoffset() is None:
        raise ValueError("GitHub timestamp has no UTC offset")
    return time


def _run(run_id: int, attempt: int) -> dict:
    run = _github(f"runs/{run_id}/attempts/{attempt}")
    if (run.get("id") != run_id or run.get("run_attempt") != attempt
            or run.get("repository", {}).get("full_name") != REPOSITORY
            or run.get("head_repository", {}).get("full_name") != REPOSITORY
            or run.get("path") != ".github/workflows/nightly.yml"
            or run.get("status") != "completed"
            or not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", ""))):
        raise RuntimeError("Select a completed attempt of an official same-repository Nightly")
    return run


def _destinations() -> dict[str, str]:
    public = "release"
    destinations = {
        "release-documents": public,
        "android-apk-payload": public,
        **{f"native-payload-{slug}": public for slug in NATIVE},
        **{f"musl-payload-{architecture}": public for architecture in MUSL},
        **{f"android-developer-payload-{abi}": public for abi in ABIS},
        **{f"package-smoke-{slug}": public for slug in (*NATIVE, "linux-x64-musl", "linux-arm64-musl")},
        **{f"android-apk-proof-apk{api}": f"apk-evidence/{ARTIFACT_PREFIX}android-apk-proof-apk{api}"
           for api in range(31, 38)},
        **{f"image-archive-{variant}": "archives" for variant in VARIANTS},
        **{f"image-native-proof-{variant}-{architecture}": f"image-evidence/{variant}"
           for variant in VARIANTS for architecture in MUSL},
    }
    return {ARTIFACT_PREFIX + name: destination for name, destination in destinations.items()}


def _select_artifacts(run: dict, listing: dict, names: set[str]) -> dict[str, dict]:
    artifacts = listing["artifacts"]
    if len(artifacts) != listing["total_count"]:
        raise RuntimeError("Nightly artifact inventory is incomplete")
    cutoff = _timestamp(run["updated_at"])
    selected = {}
    for artifact in artifacts:
        name = artifact["name"]
        if name not in names:
            continue
        if _timestamp(artifact["created_at"]) > cutoff:
            continue
        workflow = artifact.get("workflow_run", {})
        if (name in selected or workflow.get("id") != run["id"] or workflow.get("head_sha") != run["head_sha"]
                or artifact.get("expired") is not False or type(artifact.get("id")) is not int
                or artifact["id"] <= 0 or type(artifact.get("size_in_bytes")) is not int
                or not 0 < artifact["size_in_bytes"] <= ARCHIVE_LIMIT
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.get("digest", ""))):
            raise RuntimeError(f"{name} lacks a unique retained GitHub artifact with the expected run, source and digest")
        selected[name] = artifact
    missing = names - selected.keys()
    if missing:
        raise RuntimeError("Missing retained inputs for this attempt: " + ", ".join(sorted(missing)))
    return selected


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _archive(artifact: dict, cache: Path) -> Path:
    digest = artifact["digest"].removeprefix("sha256:")
    path = cache / f"{artifact['id']}-{digest}.zip"

    def verify(candidate: Path) -> None:
        if candidate.stat().st_size != artifact["size_in_bytes"] or _sha256(candidate) != digest:
            raise RuntimeError(f"{artifact['name']} archive differs from its GitHub SHA-256 or size")

    if path.exists():
        verify(path)
        return path
    with tempfile.TemporaryDirectory(dir=cache, prefix="download-") as scratch:
        partial = Path(scratch) / "artifact.zip"
        with partial.open("xb") as stream:
            subprocess.run(
                ["gh", "api", f"repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip"],
                stdout=stream, stderr=subprocess.PIPE, check=True, timeout=300,
            )
        verify(partial)
        partial.replace(path)
    return path


def _extract(path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    existing = {item.name.casefold() for item in destination.iterdir()}
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = {entry.filename.casefold() for entry in entries}
        if (not entries or len(entries) > 100 or len(names) != len(entries)
                or sum(entry.file_size for entry in entries) > 2 * ARCHIVE_LIMIT):
            raise RuntimeError(f"Invalid or oversized flat release artifact {path.name}")
        for entry in entries:
            name = entry.filename
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", name) or name.endswith(".")
                    or re.match(r"(?:aux|con|nul|prn|com[0-9]|lpt[0-9])(?:\.|$)", name, re.IGNORECASE)
                    or entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16)
                    or entry.flag_bits & 1 or entry.file_size > ARCHIVE_LIMIT
                    or name.casefold() in existing):
                raise RuntimeError(f"{path.name} contains an unsafe or duplicate flat artifact entry")
        for entry in entries:
            with archive.open(entry) as source, (destination / entry.filename).open("xb") as target:
                shutil.copyfileobj(source, target)


def _checkout(workspace: Path, repository: str, sha: str, destination: Path) -> Path:
    source = workspace / "repos" / repository
    ref = "refs/remotes/origin/HEAD" if repository.startswith("third_party/") else "refs/remotes/origin/main"
    ancestry = subprocess.run(["git", "-C", str(source), "merge-base", "--is-ancestor",
                               sha, ref], timeout=30, capture_output=True)
    if ancestry.returncode:
        raise RuntimeError(f"{repository} does not have {sha} on its fetched {ref.removeprefix('refs/remotes/')}")
    subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(source), str(destination)],
                   check=True, timeout=90)
    subprocess.run(["git", "-C", str(destination), "-c", "core.autocrlf=false", "checkout", "--detach", "--quiet", sha],
                   check=True, timeout=90)
    return destination


def _pins(source: Path) -> dict[str, str]:
    import yaml

    release = yaml.safe_load((source / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    keys = {key for _, key in REPOSITORIES.values() if key} | {
        "EMULEBB_APK_BUILD_REF", "EMULEBB_ANDROID_SIGNER_SHA256",
    }
    pins = {}
    for key in sorted(keys):
        size = 64 if key == "EMULEBB_ANDROID_SIGNER_SHA256" else 40
        value = release["env"].get(key)
        if not isinstance(value, str) or not re.fullmatch(f"[0-9a-f]{{{size}}}", value):
            raise RuntimeError(f"Exact nightly workflow lacks an immutable {key}")
        pins[key] = value
    return pins


def _invoke(checkout: Path, environment: dict[str, str], phase: str, *arguments: str | Path) -> None:
    result = subprocess.run(
        [sys.executable, "-B", *map(str, arguments)], cwd=checkout, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", timeout=300,
    )
    if result.returncode:
        raise RuntimeError(f"{phase} failed:\n" + "\n".join(result.stdout.splitlines()[-12:]))


def _hosted_environment(run: dict, pins: dict[str, str], sources: dict[str, Path],
                        version: str, version_code: int, scratch: Path) -> dict[str, str]:
    # Replay allows only the assembly and validation commands below. Keep their
    # work ephemeral and disconnect their GitHub CLI credentials.
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith((
        "GITHUB_", "GH_", "ACTIONS_", "RUNNER_", "PYTHONPATH", "PYTHONHOME",
    ))}
    return {**environment, **pins, "GH_CONFIG_DIR": str(scratch / "empty-gh-config"),
        "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_RUN_ID": str(run["id"]),
        "GITHUB_RUN_ATTEMPT": str(run["run_attempt"]), "GITHUB_WORKFLOW_SHA": run["head_sha"],
        "EMULEBB_SOURCE_REVISION": run["head_sha"], "RELEASE_VERSION": version, "RELEASE_CHANNEL": "nightly",
        "RELEASE_PLATFORM_PROFILE": "full", "EMULEBB_ANDROID_VERSION_CODE": str(version_code),
        "EMULEBB_RUST_REPO": str(sources["rust"]), "EMULEBB_TOOLING_REPO": str(sources["tooling"]),
        "EMULEBB_TESTS_REPO": str(sources["apk-tests"]), "MINIUPNP_ROOT": str(sources["miniupnp"]),
        "PCPNATPMP_ROOT": str(sources["pcpnatpmp"])}


def _linux_image_output(directory: Path, stem: str) -> None:
    # Older assemblers used the host newline for two generated files. Match
    # the Linux publisher's bytes before running the production readers.
    digest = directory / f"{stem}-multiarch.oci.digest"
    summary = directory / f"{stem}-image-security.json"
    if not digest.exists() or not summary.exists():
        raise RuntimeError(f"Missing generated image summary or digest for {stem}")
    digest.write_text(digest.read_text(encoding="ascii"), encoding="ascii", newline="\n")
    data = json.loads(summary.read_text(encoding="utf-8"))
    for record in data["artifacts"]:
        if record["name"] == digest.name:
            record.update(sha256=_sha256(digest), sizeBytes=digest.stat().st_size)
    summary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    sums = directory / f"{stem}-image-security-SHA256SUMS"
    names = sorted(line.split("  ", 1)[1] for line in sums.read_text(encoding="ascii").splitlines())
    sums.write_text("".join(f"{_sha256(directory / name)}  {name}\n" for name in names),
                    encoding="ascii", newline="\n")


def _replay(run: dict, workspace: Path, scratch: Path, source_root: Path) -> dict:
    release = scratch / "release"
    archives = scratch / "archives"
    source_sha = run["head_sha"]
    rust = _checkout(workspace, REPOSITORIES["rust"][0], source_sha, source_root / "rust")
    pins = _pins(rust)
    sources = {"rust": rust}
    for name, (repository, pin) in REPOSITORIES.items():
        if pin:
            sources[name] = _checkout(workspace, repository, pins[pin], source_root / name)

    notes = list(release.glob("RELEASE-*-NOTES.md"))
    if len(notes) != 1:
        raise RuntimeError("Replay requires one preflighted release notes document")
    version = notes[0].name.removeprefix("RELEASE-").removesuffix("-NOTES.md")
    if not re.fullmatch(rf"[0-9]+\.[0-9]+\.[0-9]+[0-9a-z.-]*\.nightly\.[0-9]{{8}}\.g{source_sha[:8]}", version):
        raise RuntimeError("Retained release version does not bind the exact nightly commit")
    stem = f"emulebb-rust-v{version}"
    apk = release / f"{stem}-android-universal.apk.manifest.json"
    version_code = json.loads(apk.read_text(encoding="utf-8"))["versionCode"]
    if type(version_code) is not int or not 1 <= version_code <= 2_100_000_000:
        raise RuntimeError("Retained APK receipt has an invalid version code")
    environment = _hosted_environment(run, pins, sources, version, version_code, scratch)

    for variant in VARIANTS:
        _invoke(sources["producer"], environment, f"Assemble {variant} OCI evidence",
                "-m", "emule_workspace", "assemble-emulebb-rust-image-evidence-ci",
                "--archive", archives / f"{stem}-{variant}-multiarch.oci.tar",
                "--evidence-dir", scratch / "image-evidence" / variant, "--release-dir", release,
                "--release-version", version, "--source-sha", source_sha, "--harness-repo", sources["image-tests"],
                "--harness-sha", pins["EMULEBB_BUILD_TESTS_REF"], "--control-rust-repo", rust)
        if os.name == "nt":
            _linux_image_output(release, f"{stem}-{variant}")

    validator = sources["validator"]
    _invoke(validator, environment, "Assemble Android APK evidence",
            "-m", "emule_workspace", "assemble-rust-android-apk-evidence-ci",
            "--apk-payload", release, "--apk-manifest-name", apk.name, "--apk-manifest-sha256", _sha256(apk),
            "--apk-source-sha", source_sha, "--expected-cert-sha256", pins["EMULEBB_ANDROID_SIGNER_SHA256"],
            "--apk-version-code", str(version_code), "--release-version", version,
            "--apk-build-ref", pins["EMULEBB_APK_BUILD_REF"], "--apk-harness-ref", pins["EMULEBB_APK_BUILD_TESTS_REF"],
            "--evidence-dir", scratch / "apk-evidence", "--release-dir", release)
    if os.name == "nt":
        proof = release / f"{stem}-android-universal-apk-lifecycle.json"
        proof.write_text(proof.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")

    _invoke(validator, environment, "Assemble native assets and checksums",
            "-m", "emule_workspace", "assemble-emulebb-rust-release-ci",
            "--release-version", version, "--assets-dir", release, "--platform-profile", "full")
    manifest = release / f"{stem}-candidate-manifest.json"
    command = rust / "tools/release_publish.py"
    common = ("--release-dir", release, "--build-repo", validator, "--producer-build-repo", sources["producer"],
              "--apk-harness-repo", sources["apk-tests"])
    full = (*common, "--harness-repo", sources["image-tests"], "--archives-dir", archives)
    _invoke(rust, environment, "Write candidate manifest", command, "candidate-manifest", *full, "--output", manifest)
    digest = _sha256(manifest)
    _invoke(rust, environment, "Verify image publisher inputs", command, "verify-candidate", *full)
    _invoke(rust, environment, "Verify native publisher inputs", command, "verify-public-candidate", *common,
            "--verified-candidate-sha256", digest)
    return {"version": version, "pins": pins, "candidateSha256": digest}


def replay(*, workspace: Path, output: Path, run_id: int, attempt: int,
           expected_candidate_sha256: str | None = None) -> Path:
    """Validate retained inputs and persist a diagnostic report with no release bytes."""
    if expected_candidate_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", expected_candidate_sha256):
        raise ValueError("The independently supplied candidate SHA-256 must be 64 lowercase hex characters")
    run = _run(run_id, attempt)
    destinations = _destinations()
    artifacts = _select_artifacts(run, _github(f"runs/{run_id}/artifacts?per_page=100"), set(destinations))
    root = output / "reports/nightly-replay" / str(run_id)
    cache = root / "archives"
    cache.mkdir(parents=True, exist_ok=True)
    report = {"repository": REPOSITORY, "runId": run_id, "runAttempt": attempt,
              "sourceSha": run["head_sha"], "publishable": False,
              "artifacts": [{key: item[key] for key in ("id", "name", "digest")} for item in artifacts.values()]}
    report_dir = Path(tempfile.mkdtemp(prefix=f"attempt-{attempt}-", dir=root))
    report_path = report_dir / "replay.json"
    try:
        with (tempfile.TemporaryDirectory(prefix="scratch-", dir=report_dir) as scratch_dir,
              tempfile.TemporaryDirectory(prefix="nightly-sources-", dir=workspace / "workspaces") as source_dir):
            scratch = Path(scratch_dir)
            for name, artifact in artifacts.items():
                _extract(_archive(artifact, cache), scratch / destinations[name])
            report.update(_replay(run, workspace, scratch, Path(source_dir)))
            if expected_candidate_sha256 is not None and report["candidateSha256"] != expected_candidate_sha256:
                raise RuntimeError("Replay candidate differs from the independently supplied manifest digest")
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report_path
