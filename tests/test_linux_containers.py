"""Verify architecture preflight rejects advertised but unusable ARM64 execution."""
import json
import subprocess
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

from emule_workspace.linux_containers import LinuxDocker, BINFMT_IMAGE
from emule_workspace.linux_containers import validate_elf_audit
from emule_workspace.linux_containers import build_configuration, RUST_IMAGES, MUSL_CROSS_IMAGE
from emule_workspace.linux_containers import run_owned_container
from emule_workspace.linux_containers import _package_gnu_deb
from emule_workspace.linux_containers import linux_test_plan, linux_quality_plan
from emule_workspace import linux_containers


def _linked_harness_fixture(tmp_path):
    """Build a real linked checkout whose host Git pointer cannot be mounted."""
    canonical = tmp_path / "workspace/repos/emulebb-build-tests"
    canonical.mkdir(parents=True)
    def git(repo, *args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git(canonical, "init", "--initial-branch", "main")
    git(canonical, "config", "user.name", "Synthetic Fixture")
    git(canonical, "config", "user.email", "fixture@example.invalid")
    git(canonical, "config", "commit.gpgsign", "false")
    git(canonical, "config", "core.autocrlf", "false")
    (canonical / "sample.txt").write_text("sample harness\n", encoding="utf-8")
    git(canonical, "add", "sample.txt")
    git(canonical, "commit", "-m", "Synthetic harness")
    selected = tmp_path / "workspace/workspaces/agents/sample/emulebb-build-tests"
    selected.parent.mkdir(parents=True)
    git(canonical, "worktree", "add", "-b", "chore/sample", str(selected))
    reports = tmp_path / "output/reports/sample"
    reports.mkdir(parents=True)
    layout = SimpleNamespace(tests_repo_root=selected, output_root=tmp_path / "output",
                             emule_workspace_root=tmp_path / "workspace")
    return layout, reports, git


def test_runtime_harness_worktree_gets_independent_git_metadata_and_exact_revision(tmp_path):
    layout, reports, git = _linked_harness_fixture(tmp_path)
    original_pointer = (layout.tests_repo_root / ".git").read_text()
    expected = git(layout.tests_repo_root, "rev-parse", "HEAD")
    snapshot = linux_containers._runtime_harness_source(layout, reports)
    assert snapshot.is_relative_to(layout.output_root / "tmp/linux-harness-snapshots")
    assert (snapshot / ".git").is_dir()
    assert not (snapshot / ".git/objects/info/alternates").exists()
    assert git(snapshot, "rev-parse", "HEAD") == expected
    assert git(snapshot, "status", "--porcelain") == ""
    assert (snapshot / "sample.txt").read_text() == "sample harness\n"
    assert (layout.tests_repo_root / ".git").read_text() == original_pointer
    receipt = json.loads((reports / "harness-snapshot.json").read_text())
    assert receipt["revision"] == expected
    assert receipt["sourcePath"] == str(layout.tests_repo_root.resolve())


def test_runtime_harness_snapshot_refuses_dirty_selected_source(tmp_path):
    layout, reports, _git = _linked_harness_fixture(tmp_path)
    (layout.tests_repo_root / "sample.txt").write_text("uncommitted changes\n")
    with pytest.raises(RuntimeError, match="clean selected source"):
        linux_containers._runtime_harness_source(layout, reports)
    assert not (layout.output_root / "tmp/linux-harness-snapshots").exists()


def test_runtime_harness_canonical_clone_keeps_existing_mount_path(tmp_path):
    source = tmp_path / "canonical"
    (source / ".git").mkdir(parents=True)
    layout = SimpleNamespace(tests_repo_root=source)
    assert linux_containers._runtime_harness_source(layout, tmp_path / "reports") == source.resolve()


@pytest.fixture
def hosted_context(monkeypatch):
    values = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "example/project",
              "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_WORKFLOW_SHA": "c" * 40}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GITHUB_TOKEN", "private-host-token")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "private-oidc-token")
    return values


@pytest.mark.parametrize("machine,architecture", [("x86_64", "amd64"), ("aarch64", "arm64")])
def test_hosted_linux_context_is_native_and_forwards_only_public_fields(monkeypatch, hosted_context, machine, architecture):
    monkeypatch.setattr(linux_containers.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linux_containers.platform, "machine", lambda: machine)
    arguments = linux_containers._hosted_linux_environment(architecture, architecture)
    assert arguments[::2] == ["--env"] * len(hosted_context)
    assert set(arguments[1::2]) == {f"{name}={value}" for name, value in hosted_context.items()}
    assert not any("private-" in argument for argument in arguments)


@pytest.mark.parametrize("system,machine,target,engine", [
    ("Windows", "x86_64", "amd64", "amd64"), ("Linux", "unknown", "amd64", "amd64"),
    ("Linux", "x86_64", "arm64", "amd64"), ("Linux", "aarch64", "arm64", "amd64"),
])
def test_hosted_linux_rejects_emulation_cross_builds_and_remote_cpu_mismatch(monkeypatch, hosted_context, system, machine, target, engine):
    monkeypatch.setattr(linux_containers.platform, "system", lambda: system)
    monkeypatch.setattr(linux_containers.platform, "machine", lambda: machine)
    with pytest.raises(RuntimeError, match="matching native runner, Docker, and target CPUs"):
        linux_containers._hosted_linux_environment(target, engine)


def test_local_linux_does_not_claim_hosted_origin_or_disallow_cross_builds(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert linux_containers._hosted_linux_environment("arm64", "amd64") == []


@pytest.mark.parametrize("prefix", [[], ["wsl.exe", "-d", "Ubuntu", "--"]])
@pytest.mark.parametrize("uid,gid", [("1001", "1002"), ("0", "0")])
def test_builder_uses_the_selected_linux_operators_identity(monkeypatch, prefix, uid, gid):
    calls = []
    def identity(command, **kwargs):
        calls.append(command)
        return (uid if command[-1] == "-u" else gid) + "\n"
    monkeypatch.setattr(linux_containers.subprocess, "check_output", identity)
    assert linux_containers._builder_user(SimpleNamespace(prefix=prefix)) == f"{uid}:{gid}"
    assert calls == [[*prefix, "id", "-u"], [*prefix, "id", "-g"]]


@pytest.mark.parametrize("value", ["", "-1", "1000:0", "1000\n0", "root", "01"])
def test_builder_rejects_invalid_operator_identity_before_docker(monkeypatch, value):
    monkeypatch.setattr(linux_containers.subprocess, "check_output", lambda *args, **kwargs: value)
    with pytest.raises(RuntimeError, match="numeric operator UID and GID"):
        linux_containers._builder_user(SimpleNamespace(prefix=[]))


@pytest.mark.parametrize("mirrors", [False, True])
@pytest.mark.parametrize("redirect", [None, "leaf-before", "parent-before", "leaf-after", "junction-before"])
def test_linux_build_prepares_user_owned_paths_and_never_runs_cargo_or_packaging_as_root(tmp_path, monkeypatch, mirrors, redirect):
    guarded = []
    monkeypatch.setattr("emule_workspace.rust_source_cache.ensure_rust_source_cache",
                        lambda layout, **kwargs: guarded.append(kwargs))
    from emule_workspace import release

    workspace, output = tmp_path / "workspace", tmp_path / "out"
    rust, tooling = workspace / "repos/rust", workspace / "repos/tooling"
    rust.mkdir(parents=True)
    docs = tooling / "docs/products/emulebb-rust"
    docs.mkdir(parents=True)
    (docs / "RELEASE-SCOPE.md").write_text("release scope")
    (rust / "Cargo.toml").write_text('[workspace.package]\nversion="0.1.0-beta.2"\n')
    (rust / "rust-toolchain.toml").write_text('[toolchain]\nchannel="1.98.1"\n')
    for name in ("Cargo.lock", "LICENSE", "emulebb-rust-settings.example.toml"):
        (rust / name).write_text("fixture")
    mini, pcp = workspace / "mini", workspace / "pcp"
    (mini / "miniupnpc").mkdir(parents=True)
    pcp.mkdir()
    (mini / "miniupnpc/LICENSE").write_text("mini license")
    (pcp / "COPYING").write_text("pcp license")
    webui = output / "webui"
    webui.mkdir(parents=True)
    (webui / "index.html").write_text("SPA")
    layout = SimpleNamespace(emule_workspace_root=workspace, output_root=output,
                             emulebb_rust_repo_root=rust, tooling_repo_root=tooling)
    target = "x86_64-unknown-linux-musl"
    target_directory = output / "builds/rust/target-linux-musl-amd64-uid-1001-gid-1002"
    target_mapping = "/output/" + target_directory.relative_to(output).as_posix()
    calls, cargo_calls = [], []
    class Docker:
        prefix = []
        def path(self, value):
            assert value.is_dir()
            return value.as_posix()
        def run(self, *args, **kwargs):
            calls.append(args)
            if args[0] == "version":
                return SimpleNamespace(stdout="amd64")
            if args[0] == "build":
                assert target_directory.is_dir()
                assert (target_directory / "tmp/profile-ownership-tests").is_dir()
                assert (output / "cache/cargo-linux-registry/1001-1002").is_dir()
                assert (output / "cache/rust-linux-home/1001-1002").is_dir()
                assert "BUILD_UID=1001" in args and "BUILD_GID=1002" in args
            if "tar" in args:
                archive = output / args[args.index("-cf") + 1].removeprefix("/output/")
                assert not archive.exists()
                archive.write_bytes(b"archive")
            return SimpleNamespace(stdout="Machine: Advanced Micro Devices X86-64\nno dynamic section")
    def cargo(docker, arguments, *args, **kwargs):
        cargo_calls.append(arguments)
        assert f"CARGO_TARGET_DIR={target_mapping}" in arguments
        if arguments[arguments.index("cargo") + 1] == "build":
            binary = target_directory / target / "release/emulebb-rust"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"ELF fixture")
    def sbom(**kwargs):
        (kwargs["package_root"] / "SBOM.spdx.json").write_text('{"spdxVersion":"SPDX-2.3"}')
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(linux_containers, "_builder_user", lambda docker: "1001:1002")
    monkeypatch.setattr(linux_containers, "run_owned_container", cargo)
    monkeypatch.setattr(release, "_emulebb_rust_package_provenance", lambda *args: {
        "source": {"emulebbRust": {"commit": "a" * 40}}})
    monkeypatch.setattr(release, "_assert_emulebb_rust_package_provenance", lambda *args: None)
    monkeypatch.setattr(release, "_emulebb_rust_native_source_roots", lambda *args: (mini, pcp))
    monkeypatch.setattr(release, "_write_emulebb_rust_linux_sbom", sbom)
    old_archive = output / "packages/rust-linux/0.1.0-beta.2/musl-amd64/emulebb-rust-v0.1.0-beta.2-linux-amd64-musl.tar.zst"
    old_archive.parent.mkdir(parents=True)
    old_archive.write_bytes(b"previous root-owned archive")
    scratch_path = target_directory / "tmp/profile-ownership-tests"
    original_link, original_mkdir = type(scratch_path).is_symlink, type(scratch_path).mkdir
    original_junction = getattr(type(scratch_path), "is_junction", lambda path: False)
    scratch_created = False
    def is_link(path):
        redirected = ((redirect == "leaf-before" and path == scratch_path)
                      or (redirect == "parent-before" and path == scratch_path.parent)
                      or (redirect == "leaf-after" and scratch_created and path == scratch_path))
        return redirected or original_link(path)
    def mkdir(path, *args, **kwargs):
        nonlocal scratch_created
        result = original_mkdir(path, *args, **kwargs)
        if path == scratch_path:
            scratch_created = True
        return result
    monkeypatch.setattr(type(scratch_path), "is_symlink", is_link)
    monkeypatch.setattr(type(scratch_path), "is_junction",
                        lambda path: (redirect == "junction-before" and path == scratch_path)
                        or original_junction(path), raising=False)
    monkeypatch.setattr(type(scratch_path), "mkdir", mkdir)
    if redirect:
        with pytest.raises(RuntimeError, match="Redirected workstream path"):
            linux_containers._build_linux_artifacts(layout, Docker(), libc="musl", architecture="amd64",
                                                    webui_dist=webui, release_version=None, test_scope="portable",
                                                    use_ci_image_mirrors=mirrors)
        assert not cargo_calls
        assert not any(arguments[0] == "build" for arguments in calls)
        return
    archive = linux_containers._build_linux_artifacts(layout, Docker(), libc="musl", architecture="amd64",
                                                      webui_dist=webui, release_version=None, test_scope="portable",
                                                      use_ci_image_mirrors=mirrors)
    assert guarded[0]["target"] == target_directory
    assert guarded[0]["execution_source"] == "/source"
    assert guarded[0]["execution_target"] == target_mapping
    assert guarded[0]["native_roots"] == (mini, pcp)
    assert cargo_calls
    for arguments in [*cargo_calls, *(args for args in calls if args[0] == "run")]:
        assert arguments[arguments.index("--user") + 1] == "1001:1002"
        image_index = next(index for index, value in enumerate(arguments) if value.startswith("local/emulebb-rust-builder:"))
        assert arguments.index("--user") < image_index
        assert arguments[arguments.index("--cap-drop") + 1] == "ALL"
    test_calls = [arguments for arguments in cargo_calls if arguments[arguments.index("cargo") + 1] == "test"]
    assert test_calls
    scratch = (f"{target_mapping}/tmp/profile-ownership-tests:rw,exec,nosuid,nodev,"
               "size=8g,uid=1001,gid=1002,mode=0700")
    for arguments in cargo_calls:
        if arguments in test_calls:
            assert arguments.count("--tmpfs") == 1
            assert arguments[arguments.index("--tmpfs") + 1] == scratch
            assert arguments.index("--tmpfs") < arguments.index("cargo")
        else:
            assert "--tmpfs" not in arguments
    assert (output / "cache/cargo-linux-registry/1001-1002").is_dir()
    assert all("type=bind,source=" + output.as_posix() + ",target=/output" in arguments for arguments in cargo_calls)
    manifest = json.loads(archive.with_name(archive.name + ".manifest.json").read_text())
    assert manifest["buildUser"] == "1001:1002"
    assert manifest["pathMapping"]["cargoTarget"] == target_mapping
    expected = linux_containers.select_image("rust-musl", linux_containers.RUST_IMAGES["musl"], mirrors)
    assert manifest["builderBase"] == expected
    assert "BASE_IMAGE=" + expected in next(args for args in calls if args[0] == "build")


@pytest.mark.parametrize("producer_run", ["123", "999"])
@pytest.mark.parametrize("host_uid", ["0", "1000"])
@pytest.mark.parametrize("mirrors", [False, True])
def test_hosted_runtime_launch_receives_only_this_runs_public_identity(monkeypatch, tmp_path, hosted_context, producer_run, host_uid, mirrors):
    output = tmp_path / "out"
    output.mkdir()
    archive = output / "emulebb-rust-v0.1.0-beta.2-linux-amd64-musl.tar.zst"
    archive.write_bytes(b"synthetic package")
    workflow = {"repository": "example/project", "runId": producer_run, "runAttempt": "1", "workflowSha": "c" * 40}
    manifest = {"origin": "github-actions", "workflow": workflow, "sourceDirty": False,
                "source": {"emulebbRust": {"commit": "a" * 40}}, "sha256": "b" * 64}
    archive.with_name(archive.name + ".manifest.json").write_text(json.dumps(manifest))
    layout = SimpleNamespace(output_root=output, emulebb_rust_repo_root=tmp_path / "rust", tests_repo_root=tmp_path / "tests")
    monkeypatch.setattr(linux_containers, "git_output", lambda repo, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "")
    monkeypatch.setattr(linux_containers.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linux_containers.platform, "machine", lambda: "x86_64")
    calls = []
    class Docker:
        prefix = []
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def path(self, value):
            return value.as_posix()
        def run(self, *args, **kwargs):
            calls.append(args)
            return SimpleNamespace(stdout="amd64" if args[0] == "version" else "")
    launches = []
    monkeypatch.setattr(linux_containers, "LinuxDocker", Docker)
    monkeypatch.setattr(linux_containers.subprocess, "check_output", lambda *args, **kwargs: host_uid)
    monkeypatch.setattr(linux_containers, "run_owned_container", lambda docker, arguments, *args, **kwargs: launches.append(arguments))
    if producer_run != "123":
        with pytest.raises(RuntimeError, match="this GitHub run and workflow"):
            linux_containers.smoke_linux_archive(layout, archive=archive, libc="musl", architecture="amd64", use_ci_image_mirrors=mirrors)
        assert not calls and not launches
        return
    linux_containers.smoke_linux_archive(layout, archive=archive, libc="musl", architecture="amd64", use_ci_image_mirrors=mirrors)
    assert calls[0][0] == "version"
    expected = linux_containers.select_image("alpine-runtime", linux_containers.RUNTIME_IMAGES["musl"], mirrors)
    assert "BASE_IMAGE=" + expected in next(args for args in calls if args[0] == "build")
    command = launches[0]
    image_index = command.index("local/emulebb-rust-runtime:musl-amd64")
    assert command[command.index("--user") + 1] == ("65534:65534" if host_uid == "0" else "1000:1000")
    for setting in ("GIT_CONFIG_COUNT=1", "GIT_CONFIG_KEY_0=safe.directory",
                    "GIT_CONFIG_VALUE_0=/workspace/repos/emulebb-build-tests"):
        assert command.index(setting) < image_index
    assert all(command.index(f"{name}={value}") < image_index for name, value in hosted_context.items())
    assert not any("private-" in argument for argument in command)


def test_ci_linux_entrypoint_rejects_local_invocation(monkeypatch):
    from click.testing import CliRunner
    from emule_workspace.cli import main
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    result = CliRunner().invoke(main, ["package-emulebb-rust-linux-ci", "--libc", "musl",
                                      "--architecture", "amd64", "--release-version", "0.1.0-beta.2"])
    assert result.exit_code != 0
    assert "requires GitHub Actions" in result.output


@pytest.fixture
def ci_linux_producer(tmp_path, monkeypatch, hosted_context):
    from click.testing import CliRunner
    from emule_workspace.cli import main

    output = tmp_path / "output"
    output.mkdir()
    for name in ("EMULEBB_RUST_REPO", "EMULEBB_TOOLING_REPO", "MINIUPNP_ROOT",
                 "PCPNATPMP_ROOT", "EMULEBB_BUILD_TESTS_REPO"):
        source = tmp_path / name.lower()
        source.mkdir()
        monkeypatch.setenv(name, str(source))
    monkeypatch.setenv("EMULEBB_WORKSPACE_OUTPUT_ROOT", str(output))
    archive = output / "synthetic-musl.tar.zst"
    archive.write_bytes(b"Synthetic package")
    receipt = output / "reports" / "package-smoke.json"
    receipt.parent.mkdir()
    receipt.write_bytes(b'{"schema":"emulebb.rust.package-smoke/3","status":"passed"}\n')
    builds, smokes = [], []

    def build(layout, **kwargs):
        builds.append((layout, kwargs))
        return archive

    def smoke(layout, **kwargs):
        smokes.append((layout, kwargs))
        return receipt

    monkeypatch.setattr(linux_containers, "build_linux_artifacts", build)
    monkeypatch.setattr(linux_containers, "smoke_linux_archive", smoke)

    def invoke(*extra):
        return CliRunner().invoke(main, [
            "package-emulebb-rust-linux-ci", "--libc", "musl", "--architecture", "amd64",
            "--release-version", "0.1.0-beta.2", *map(str, extra),
        ])

    return SimpleNamespace(output=output, archive=archive, receipt=receipt,
                           builds=builds, smokes=smokes, invoke=invoke)


def test_ci_linux_retains_exact_receipt_from_shared_native_smoke(ci_linux_producer):
    producer = ci_linux_producer
    target = producer.output / "evidence" / "emulebb-rust-package-smoke-linux-x64-musl.json"
    result = producer.invoke("--smoke", "--test-scope", "full", "--smoke-report-path", target)
    assert result.exit_code == 0, result.output
    assert target.read_bytes() == producer.receipt.read_bytes()
    assert len(producer.builds) == len(producer.smokes) == 1
    assert producer.builds[0][1]["test_scope"] == "full"
    assert producer.builds[0][1]["smoke"] is False
    assert producer.smokes[0][1] == {"archive": producer.archive, "libc": "musl", "architecture": "amd64"}


def test_ci_linux_report_option_requires_runtime_smoke(ci_linux_producer):
    producer = ci_linux_producer
    result = producer.invoke("--smoke-report-path", producer.output / "receipt.json")
    assert result.exit_code != 0 and "requires --smoke" in result.output
    assert not producer.builds and not producer.smokes


@pytest.mark.parametrize("kind", ["outside-output", "existing-file", "symlink"])
def test_ci_linux_report_target_is_checked_before_build(ci_linux_producer, tmp_path, kind):
    producer = ci_linux_producer
    target = producer.output / "retained.json"
    original = b"Synthetic existing receipt"
    if kind == "outside-output":
        target = tmp_path / "outside.json"
    elif kind == "existing-file":
        target.write_bytes(original)
    else:
        source = producer.output / "existing.json"
        source.write_bytes(original)
        try:
            target.symlink_to(source)
        except (NotImplementedError, OSError):
            pytest.skip("This host does not permit creation of a test symlink")
    result = producer.invoke("--smoke", "--smoke-report-path", target)
    assert result.exit_code != 0 and "new regular path below" in result.output
    assert not producer.builds and not producer.smokes
    if target.exists():
        assert target.read_bytes() == original


def test_ci_linux_failed_smoke_cannot_create_a_pass_receipt(ci_linux_producer, monkeypatch):
    producer = ci_linux_producer
    target = producer.output / "retained.json"

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic lifecycle failure")

    monkeypatch.setattr(linux_containers, "smoke_linux_archive", fail)
    result = producer.invoke("--smoke", "--smoke-report-path", target)
    assert result.exit_code != 0 and "Synthetic lifecycle failure" in result.output
    assert not target.exists()


def test_ci_linux_retention_never_replaces_a_racing_producer(ci_linux_producer, monkeypatch):
    producer = ci_linux_producer
    target = producer.output / "retained.json"
    original = b"Synthetic racing receipt"

    def race(*args, **kwargs):
        target.write_bytes(original)
        return producer.receipt

    monkeypatch.setattr(linux_containers, "smoke_linux_archive", race)
    result = producer.invoke("--smoke", "--smoke-report-path", target)
    assert result.exit_code != 0 and target.read_bytes() == original


def test_ci_linux_smoke_without_retention_keeps_the_shared_owner(ci_linux_producer):
    producer = ci_linux_producer
    result = producer.invoke("--smoke")
    assert result.exit_code == 0, result.output
    assert len(producer.builds) == len(producer.smokes) == 1


def test_full_native_suite_uses_candidate_definitions_and_cross_compiles_both(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "rust_quality_gate.py").write_text('''
def test_workspace_step():
    return "workspace", ["cargo", "test", "--workspace", "--locked"]
def test_vpn_leak_step():
    return "egress", ["cargo", "test", "--features", "egress-audit", "--test", "vpn_leak_egress", "--locked", "--", "--test-threads=1"]
''')
    native = linux_test_plan("full", cross=False, rust_root=tmp_path)
    assert native == [("workspace-tests", ["--workspace"]), ("vpn-egress-tests", [
        "--features", "egress-audit", "--test", "vpn_leak_egress", "--", "--test-threads=1"])]
    cross = linux_test_plan("full", cross=True, rust_root=tmp_path)
    assert cross[0] == ("workspace-tests-build", ["--workspace", "--no-run"])
    assert cross[1][1][-3:] == ["--no-run", "--", "--test-threads=1"]
    assert [label for label, _ in cross[2:]] == ["native-library-tests", "tracker-tests", "durability-tests"]
    assert cross[3][1] == ["--lib", "-p", "emulebb-kad-net", "tracker::tests"]
    assert linux_test_plan("portable", cross=False) == cross[2:]
    portable = cross[2][1]
    assert "emulebb-kad-proto" in portable and "emulebb-kad-routing" in portable and "emulebb-index" in portable
    assert "emulebb-core" not in portable and "emulebb-kad-dht" not in portable
    with pytest.raises(ValueError):
        linux_test_plan("skip", cross=False)
    with pytest.raises(ValueError, match="candidate Rust"):
        linux_test_plan("full", cross=False)


def test_changed_canonical_egress_definition_is_not_silently_replaced(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "rust_quality_gate.py").write_text('''
def test_workspace_step():
    return "workspace", ["cargo", "test", "--workspace", "--locked"]
def test_vpn_leak_step():
    return "unsafe drift", ["cargo", "test", "--test", "different-test"]
''')
    with pytest.raises(RuntimeError, match="locked Cargo test"):
        linux_test_plan("full", cross=False, rust_root=tmp_path)


def test_linux_lint_and_diagnostics_preserve_candidate_flags_and_cargo_target_boundary(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    definition = tools / "rust_quality_gate.py"
    definition.write_text('''
def clippy_step():
    return "lint", ["cargo", "clippy", "--workspace", "--all-targets", "--features", "candidate-feature", "--locked", "--", "-D", "warnings"]
def diagnostics_step():
    return "diagnostics", ["cargo", "check", "--bin", "candidate-diagnostics", "--features", "packet-diagnostics", "--locked"]
''')
    target = "aarch64-unknown-linux-musl"
    plan = linux_quality_plan(tmp_path, target)
    assert plan == [
        ("clippy", ["clippy", "--workspace", "--all-targets", "--features", "candidate-feature", "--locked", "--target", target, "--", "-D", "warnings"]),
        ("packet-diagnostics", ["check", "--bin", "candidate-diagnostics", "--features", "packet-diagnostics", "--locked", "--target", target]),
    ]
    definition.write_text(definition.read_text().replace(', "--locked"', ""))
    with pytest.raises(RuntimeError, match="locked Cargo clippy"):
        linux_quality_plan(tmp_path, target)


@pytest.mark.parametrize("outcome", ["success", "failed", "timeout", "remaining"])
def test_owned_container_removes_only_its_invocation_and_retains_log(tmp_path, monkeypatch, outcome):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if "--name" in command:
            kwargs["stdout"].write("retained container output\n")
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(command, 1)
            if outcome == "failed":
                raise subprocess.CalledProcessError(3, command)
        return SimpleNamespace(stdout="remaining-owned-container" if outcome == "remaining" and "ps" in command else "")
    monkeypatch.setattr("emule_workspace.linux_containers.subprocess.run", run)
    docker = LinuxDocker()
    path = tmp_path / "build.log"
    ownership = {}
    if outcome == "success":
        run_owned_container(docker, ["--rm", "image", "cargo", "test"], path, timeout=1, ownership=ownership)
    else:
        with pytest.raises(RuntimeError, match="survived" if outcome == "remaining" else "inspect"):
            run_owned_container(docker, ["--rm", "image", "cargo", "test"], path, timeout=1, ownership=ownership)
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[-2][-3:] == ["rm", "--force", name]
    assert f"name={name}" in calls[-1]
    assert ownership == {"name": name, "absent": outcome != "remaining"}
    assert path.read_text() == "retained container output\n"


@pytest.mark.parametrize("counts", [(0, 0, 0), (1, 0, 0), (2, 0, 0), (1, 1, 0), (1, 0, 1)])
def test_profile_scratch_proof_runs_exact_fixed_library_cases_and_rejects_wrong_counts(tmp_path, monkeypatch, counts):
    scratch, logs = tmp_path / "scratch", tmp_path / "logs"
    scratch.mkdir()
    logs.mkdir()
    cargo = ["--tmpfs", "fixed-native-scratch", "--user", "1001:1002", "image", "cargo"]
    calls = []
    def run(docker, arguments, log, **kwargs):
        calls.append(arguments)
        kwargs["ownership"].update(name=f"owned-{len(calls)}", absent=True)
        assert arguments[:len(cargo) - 1] == cargo[:-1]
        if "python3" in arguments:
            assert kwargs["capture"] is True
            return '{"filesystem":"tmpfs","uid":1001,"gid":1002,"directoryMode":"0700","fileMode":"0600"}'
        assert "--lib" in arguments and "--bin" not in arguments
        assert arguments[-2:] == ["--", "--exact"]
        case = arguments[arguments.index("--lib") + 1]
        passed, failed, ignored = counts
        log.write_text(f"test {case} ... ok\ntest result: ok. {passed} passed; {failed} failed; {ignored} ignored;\n")
        return ""
    monkeypatch.setattr(linux_containers, "run_owned_container", run)
    receipt = {"tests": [], "containers": []}
    arguments = (object(), cargo, "x86_64-unknown-linux-gnu", "/output/target", scratch,
                 "1001:1002", logs, (logs / "proof.json", receipt))
    if counts == (1, 0, 0):
        assert linux_containers._prove_profile_test_scratch(*arguments) == logs / "proof.json"
        assert [test["name"] for test in receipt["tests"]] == [
            "tests::retargeted_profile_alias_keeps_io_in_the_leased_directory",
            "tests::load_rejects_group_or_world_readable_bootstrap_settings"]
        assert len(calls) == 3
    else:
        with pytest.raises(RuntimeError, match="exactly one passing case"):
            linux_containers._prove_profile_test_scratch(*arguments)
        assert len(calls) == 2
    assert receipt["hostScratchUnchanged"] is True


@pytest.mark.parametrize("fault", ["mode", "host-leak"])
def test_profile_scratch_proof_does_not_hide_mode_failure_or_host_leak(tmp_path, monkeypatch, fault):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    def run(docker, arguments, log, **kwargs):
        kwargs["ownership"].update(name="owned", absent=True)
        if "python3" in arguments:
            if fault == "host-leak":
                (scratch / "unexpected").write_text("synthetic leak")
            raise RuntimeError("synthetic mode probe failure")
    monkeypatch.setattr(linux_containers, "run_owned_container", run)
    receipt = {"tests": [], "containers": []}
    with pytest.raises(RuntimeError, match="leaked" if fault == "host-leak" else "mode probe failure"):
        linux_containers._prove_profile_test_scratch(object(), ["image", "cargo"], "target", "/output/target",
                                                    scratch, "1001:1002", tmp_path, (tmp_path / "proof.json", receipt))
    assert receipt["hostScratchUnchanged"] is (fault != "host-leak")


@pytest.mark.parametrize("outcome", ["passed", "changed", "empty", "cleanup"])
def test_fixed_profile_proof_fences_sources_and_retains_honest_receipt(tmp_path, monkeypatch, outcome):
    workspace, output = tmp_path / "workspace", tmp_path / "output"
    output.mkdir()
    layout = SimpleNamespace(emule_workspace_root=workspace, output_root=output, build_repo_root=workspace / "builder",
                             emulebb_rust_repo_root=workspace / "rust", tooling_repo_root=workspace / "tooling",
                             tests_repo_root=workspace / "harness")
    snapshots = []
    def snapshot(sources, **kwargs):
        assert kwargs == {"clean": True}
        snapshots.append(sources)
        return {"revision": "changed" if outcome == "changed" and len(snapshots) > 1 else "fixed"}
    class Docker:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def prove(*args, **kwargs):
        assert kwargs["libc"] == "gnu" and kwargs["architecture"] == "amd64"
        report, value = kwargs["profile_scratch_proof"]
        if outcome != "empty":
            value.update(tests=[{}, {}], containers=[{"absent": outcome != "cleanup"}] * 3,
                         hostScratchUnchanged=True)
        return report
    monkeypatch.setattr("emule_workspace.local_checks._source_snapshot", snapshot)
    monkeypatch.setattr("emule_workspace.release._emulebb_rust_native_source_roots",
                        lambda layout: (workspace / "mini", workspace / "pcp"))
    monkeypatch.setattr(linux_containers, "LinuxDocker", Docker)
    monkeypatch.setattr(linux_containers, "_build_linux_artifacts", prove)
    if outcome == "passed":
        report = linux_containers.verify_linux_profile_scratch(layout)
    else:
        with pytest.raises(RuntimeError):
            linux_containers.verify_linux_profile_scratch(layout)
        report = next((output / "reports/linux-profile-scratch").glob("*/results.json"))
    value = json.loads(report.read_text())
    assert value["qualification"] == "focused-profile-scratch-only"
    assert value["status"] == ("passed" if outcome == "passed" else "failed")


def test_fixed_profile_proof_cli_uses_the_existing_lock_without_arbitrary_filters(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from emule_workspace import cli

    options = SimpleNamespace(workspace_root=tmp_path, output_root=tmp_path / "output", workspace_name="workspace")
    selected = object()
    events = []
    monkeypatch.setattr(cli, "resolve_workspace_options", lambda **kwargs: options)
    monkeypatch.setattr(cli, "load_layout", lambda *args, **kwargs: selected)
    monkeypatch.setattr(linux_containers, "verify_linux_profile_scratch", lambda layout: events.append(("proof", layout)))
    def locked(name, function):
        def invoke(**kwargs):
            events.append(("lock", name))
            return function(**kwargs)
        return invoke
    monkeypatch.setattr(cli, "_locked", locked)
    result = CliRunner().invoke(cli.main, ["verify-linux-profile-scratch"])
    assert result.exit_code == 0, result.output
    assert events == [("lock", "verify Linux profile scratch"), ("proof", selected)]
    events.clear()
    result = CliRunner().invoke(cli.main, ["verify-linux-profile-scratch", "--case", "arbitrary"])
    assert result.exit_code != 0 and not events


def test_arm64_build_is_native_on_ci_and_cross_compiled_locally():
    assert build_configuration("musl", "arm64", "arm64") == (RUST_IMAGES["musl"], "arm64", False)
    assert build_configuration("musl", "arm64", "amd64") == (MUSL_CROSS_IMAGE, "amd64", True)
    assert build_configuration("gnu", "arm64", "amd64") == (RUST_IMAGES["gnu"], "amd64", True)
    with pytest.raises(ValueError):
        build_configuration("musl", "armv7", "amd64")


def test_static_musl_audit_rejects_interpreter_shared_dependencies_and_wrong_cpu():
    validate_elf_audit("Machine: AArch64\nThere is no dynamic section", libc="musl", architecture="arm64")
    for audit in ("Machine: AArch64\nINTERP", "Machine: AArch64\nNEEDED libgcc_s.so", "Machine: X86-64"):
        with pytest.raises(RuntimeError):
            validate_elf_audit(audit, libc="musl", architecture="arm64")


def test_gnu_deb_uses_verified_linux_payload_and_private_native_modes(tmp_path, monkeypatch):
    from emule_workspace import release

    output = tmp_path / "out"
    root = output / "packages/gnu-arm64"
    stage = root / "staging/emulebb-rust"
    (stage / "webui").mkdir(parents=True)
    (stage / "licenses").mkdir()
    (stage / "emulebb-rust").write_bytes(b"verified Linux ELF payload")
    (stage / "webui/index.html").write_text("bundled SPA")
    (stage / "licenses/native-LICENSE").write_text("native notice")
    rust = tmp_path / "rust"
    rust.mkdir()
    for name in ("LICENSE", "emulebb-rust-settings.example.toml"):
        (rust / name).write_text("fixture")
    icons = rust / "resources/icons"
    icons.mkdir(parents=True)
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        (icons / f"donkey-{size}.png").write_bytes(f"synthetic icon {size}".encode("ascii"))
    tooling = tmp_path / "tooling"
    docs = tooling / "docs/products/emulebb-rust"
    docs.mkdir(parents=True)
    (docs / "RELEASE-SCOPE.md").write_text("scope")
    layout = SimpleNamespace(output_root=output, emulebb_rust_repo_root=rust, tooling_repo_root=tooling)
    version = "0.1.0-beta.2"
    deb = root / f"emulebb-rust-v{version}-linux-arm64.deb"
    deb.write_bytes(b"previous root-owned DEB")
    calls = []
    def package(docker, arguments, log, **kwargs):
        calls.append(arguments)
        assert not deb.exists()
        deb.write_bytes(b"DEB fixture")
    def sbom(**kwargs):
        (kwargs["package_root"] / "SBOM.spdx.json").write_text('{"spdxVersion":"SPDX-2.3"}')
    monkeypatch.setattr("emule_workspace.linux_containers.run_owned_container", package)
    monkeypatch.setattr(release, "_write_emulebb_rust_linux_sbom", sbom)
    files = _package_gnu_deb(layout, object(), root=root, stage=stage, version=version,
                             architecture="arm64", build_arch="amd64", builder="cross-builder", user="1000:1000",
                             mounts=[], manifest={"sourceDirty": False, "target": "aarch64-unknown-linux-gnu"},
                             log_root=root)
    assert (root / "deb/usr/lib/emulebb-rust/emulebb-rust").read_bytes() == (stage / "emulebb-rust").read_bytes()
    assert set(path.name for path in (root / "deb").iterdir()) == {"usr", "DEBIAN"}
    control = (root / "deb/DEBIAN/control").read_text()
    assert "Architecture: arm64" in control and "Depends: libc6 (>= 2.36)" in control
    assert "--tmpfs" in calls[0]
    assert calls[0][calls[0].index("--user") + 1] == "1000:1000"
    assert "uid=1000,gid=1000,mode=0700" in calls[0][calls[0].index("--tmpfs") + 1]
    assert "--root-owner-group" in calls[0][-1] and "chmod 0644" in calls[0][-1]
    script = calls[0][-1]
    assert script.index("desktop-file-validate ") < script.index("dpkg-deb --build")
    assert script.index("appstreamcli validate-tree --no-net ") < script.index("dpkg-deb --build")
    assert len(list((root / "deb/usr/share/metainfo").glob("*.xml"))) == 1
    manifest = json.loads(files[1].read_text())
    assert manifest["sha256"] == release._sha256(deb)
    assert manifest["target"] == "aarch64-unknown-linux-gnu"
    assert manifest["sbomSha256"] == release._sha256(files[2])


@pytest.mark.parametrize("platforms,machine", [("linux/amd64", "aarch64"), ("linux/amd64, linux/arm64", "x86_64")])
def test_preflight_requires_available_and_executable_arm64(tmp_path, monkeypatch, platforms, machine):
    docker = LinuxDocker()
    def run(*args, **kwargs):
        if args[0] == "version":
            return SimpleNamespace(stdout=json.dumps({"Server": {"Arch": "amd64", "Version": "test"}}))
        return SimpleNamespace(stdout=f"Platforms: {platforms}" if args[0] == "buildx" else machine)
    monkeypatch.setattr(docker, "run", run)
    with pytest.raises(RuntimeError):
        docker.preflight(tmp_path)
    assert not (tmp_path / "reports/linux-containers/preflight.json").exists()


def test_setup_registers_only_pinned_arm64_and_retains_proof(tmp_path, monkeypatch):
    docker = LinuxDocker()
    commands = []
    def run(*args, **kwargs):
        commands.append(args)
        if args[0] == "version":
            return SimpleNamespace(stdout=json.dumps({"Server": {"Arch": "amd64", "Version": "test"}}))
        return SimpleNamespace(stdout="Platforms: linux/amd64, linux/arm64" if args[0] == "buildx" else "aarch64")
    monkeypatch.setattr(docker, "run", run)
    docker.preflight(tmp_path, setup_arm64=True)
    assert ("run", "--privileged", "--rm", BINFMT_IMAGE, "--install", "arm64") in commands
    assert json.loads((tmp_path / "reports/linux-containers/preflight.json").read_text())["arm64_execution"] == "aarch64"


def test_untraversable_windows_mount_uses_same_directory_and_cleans_alias(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr("emule_workspace.linux_containers.subprocess.check_output", lambda *a, **k: "/unavailable\n")
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=1 if "test" in command else 0)
    monkeypatch.setattr("emule_workspace.linux_containers.subprocess.run", run)
    with LinuxDocker() as docker:
        docker.prefix = ["wsl.exe", "-d", "Ubuntu", "--"]
        mapped = docker.path(tmp_path)
        assert mapped.startswith("/mnt/emulebb-build-")
        assert docker.path(tmp_path / "cache") == mapped + "/cache"
    assert any("mount" in cmd and tmp_path.resolve().as_posix() in cmd for cmd in commands)
    assert commands[-2][-2:] == ["umount", mapped]
    assert commands[-1][-2:] == ["rmdir", mapped]


@pytest.fixture
def owned_wsl_lifetime(tmp_path, monkeypatch):
    """Use real host children while modelling only the Linux identity boundary."""
    harness = tmp_path / "harness"
    harness.mkdir()
    helper = harness / "helper.py"
    helper.write_text("# Synthetic selected lifecycle input.\n")
    state = {"boot": "00000000-0000-4000-8000-000000000001", "birth": "1234",
             "ignore_eof": False, "pid_ready": True}
    children, stopped = [], []

    def start(executable, arguments, log_path, pid_path, **kwargs):
        assert executable.name == "timeout"
        assert arguments == ["--signal=TERM", "--kill-after=5", "21600", "/usr/bin/cat"]
        assert kwargs["stdin"] == subprocess.PIPE
        program = "import time; time.sleep(60)" if state["ignore_eof"] else "import sys; sys.stdin.buffer.read()"
        proxy = subprocess.Popen([sys.executable, "-c", program], stdin=kwargs["stdin"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append(proxy)
        if state["pid_ready"]:
            pid_path.write_text("73\n", encoding="ascii")
        return SimpleNamespace(proxy=proxy, pid_path=pid_path, poll=proxy.poll)

    def stop(process):
        assert process.pid_path.read_text(encoding="ascii").strip() == "73"
        stopped.append(process.proxy.pid)
        process.proxy.terminate()
        process.proxy.wait(timeout=5)

    api = SimpleNamespace(start_wsl_managed_executable=start, stop_process=stop)
    monkeypatch.setattr(linux_containers, "_load_wsl_process_api", lambda path: (api, (helper,)))
    session = linux_containers.WslValidationSession(harness, tmp_path / "evidence")
    # This fixture models identities and real host proxies without launching WSL.
    session.docker.prefix = []

    def read(path):
        if path.endswith("boot_id"):
            return state["boot"]
        if not children or children[-1].poll() is not None:
            raise subprocess.CalledProcessError(1, ["cat", path])
        return "73 (sleep) " + " ".join(["S", *["0"] * 18, state["birth"]])

    monkeypatch.setattr(session, "_read_linux", read)
    monkeypatch.setattr(session, "_pid_exists", lambda pid: bool(children) and children[-1].poll() is None)
    monkeypatch.setattr(session.docker, "run", lambda *args, **kwargs:
                        SimpleNamespace(stdout=json.dumps({"Server": {"Arch": "amd64", "Version": "synthetic"}})))
    yield session, state, stopped, children, helper
    for child in children:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_wsl_lifetime_reaps_only_its_owned_proxy_on_every_plan_exit(owned_wsl_lifetime, outcome):
    session, _state, stopped, children, _helper = owned_wsl_lifetime
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if outcome == "success":
            with session:
                session.check("host-stage")
        else:
            error = KeyboardInterrupt if outcome == "cancel" else RuntimeError
            with pytest.raises(error):
                with session:
                    raise error("Synthetic check interruption")
        assert stopped == []
        assert children[0].poll() is not None
        assert foreign.poll() is None
        receipt = json.loads((session.evidence / "wsl-session.json").read_text())
        assert receipt["status"] == "stopped"
        assert receipt["planOutcome"] == {"success": "completed", "failure": "failed", "cancel": "interrupted"}[outcome]
        assert receipt["helperSources"] == {"helper.py": linux_containers.hashlib.sha256(_helper.read_bytes()).hexdigest()}
    finally:
        foreign.terminate()
        foreign.wait(timeout=5)


@pytest.mark.parametrize("change", ["boot", "birth"])
def test_wsl_restart_or_pid_reuse_never_signals_the_new_linux_identity(owned_wsl_lifetime, monkeypatch, change):
    session, state, stopped, children, _helper = owned_wsl_lifetime
    state["ignore_eof"] = True
    monkeypatch.setattr(linux_containers, "_WSL_STOP_TIMEOUT", 0.05)
    with pytest.raises(RuntimeError, match="restarted|identity changed"):
        with session:
            state[change] = "00000000-0000-4000-8000-000000000002" if change == "boot" else "9999"
            session.check("before-linux")
    assert stopped == []
    assert children[0].poll() is not None
    assert session.receipt["linuxCleanup"] == ("previous-kernel-ended-no-linux-signals" if change == "boot"
                                               else "owned-identity-ended-no-linux-signals")


def test_wsl_deadline_blocks_the_next_stage_and_tears_down(owned_wsl_lifetime):
    session, _state, stopped, children, _helper = owned_wsl_lifetime
    with pytest.raises(RuntimeError, match="deadline"):
        with session:
            session.deadline = time.monotonic() - 1
            session.check("before-linux")
    assert stopped == [] and children[0].poll() is not None


def test_wsl_early_docker_failure_reaps_lifetime_before_host_work(owned_wsl_lifetime, monkeypatch):
    session, _state, stopped, children, _helper = owned_wsl_lifetime
    def unavailable(*args, **kwargs):
        raise RuntimeError("Synthetic Docker unavailable")
    monkeypatch.setattr(session.docker, "run", unavailable)
    with pytest.raises(RuntimeError, match="Docker unavailable"):
        with session:
            pytest.fail("Host work must not start")
    assert stopped == [] and children[0].poll() is not None


def test_wsl_selected_helper_bytes_cannot_change_during_the_plan(owned_wsl_lifetime):
    session, _state, stopped, children, helper = owned_wsl_lifetime
    with pytest.raises(RuntimeError, match="helper sources changed"):
        with session:
            helper.write_text("# Changed selected helper.\n")
            session.check("before-linux")
    assert stopped == []


@pytest.mark.parametrize("name,relative", [
    ("emule_test_harness", "emule_test_harness/__init__.py"),
    ("emule_live_profile_common_rust_ed2k", "scripts/emule-live-profile-common.py"),
    ("cached_script_alias", "scripts/emule-live-profile-common.py"),
])
def test_wsl_loader_refuses_foreign_cached_packages_and_script_helpers(tmp_path, monkeypatch, name, relative):
    selected = tmp_path / "selected"
    foreign = ModuleType(name)
    foreign.__file__ = str(tmp_path / "foreign/emulebb-build-tests" / relative)
    monkeypatch.setitem(sys.modules, name, foreign)
    def must_not_import(*args, **kwargs):
        pytest.fail("A foreign cache must fail before importing selected code")
    monkeypatch.setattr(linux_containers.importlib, "import_module", must_not_import)
    with pytest.raises(RuntimeError, match="foreign"):
        linux_containers._load_wsl_process_api(selected)
    assert sys.modules[name] is foreign


def test_wsl_loader_refuses_same_path_cached_function_objects(tmp_path, monkeypatch):
    selected = tmp_path / "selected"
    cached = ModuleType("emule_test_harness")
    cached.__file__ = str(selected / "emule_test_harness/__init__.py")
    monkeypatch.setitem(sys.modules, "emule_test_harness", cached)
    with pytest.raises(RuntimeError, match="fresh process"):
        linux_containers._load_wsl_process_api(selected)
    assert sys.modules["emule_test_harness"] is cached


@pytest.mark.parametrize("foreign_dependency,owned_stdin", [(False, True), (True, True), (False, False)])
def test_wsl_loader_binds_all_new_material_modules_and_restores_search_path(tmp_path, monkeypatch,
                                                                         foreign_dependency, owned_stdin):
    selected = tmp_path / "selected"
    api_path = selected / "emule_test_harness/rust_ed2k.py"
    api_path.parent.mkdir(parents=True)
    parameters = "*, stdin=None" if owned_stdin else ""
    source = f"def start_wsl_managed_executable({parameters}): pass\ndef stop_process(): pass\n"
    api_path.write_text(source)
    (api_path.parent / "wsl_managed_process.sh").write_text("#!/bin/sh\nexec \"$@\"\n")
    api = ModuleType("emule_test_harness.rust_ed2k")
    api.__file__ = str(api_path)
    exec(compile(source, str(api_path), "exec"), api.__dict__)
    script = ModuleType("emule_live_profile_common_rust_ed2k")
    script_path = (tmp_path / "foreign/emulebb-build-tests/scripts/emule-live-profile-common.py"
                   if foreign_dependency else selected / "scripts/emule-live-profile-common.py")
    script_path.parent.mkdir(parents=True)
    script_path.write_text("# Synthetic script dependency.\n")
    script.__file__ = str(script_path)
    original_path = list(sys.path)
    def selected_import(name):
        assert sys.path[0] == str(selected)
        sys.path.insert(0, str(selected / "scripts"))
        monkeypatch.setitem(sys.modules, api.__name__, api)
        monkeypatch.setitem(sys.modules, script.__name__, script)
        return api
    monkeypatch.setattr(linux_containers.importlib, "import_module", selected_import)
    if foreign_dependency:
        with pytest.raises(RuntimeError, match="foreign"):
            linux_containers._load_wsl_process_api(selected)
    elif not owned_stdin:
        with pytest.raises(RuntimeError, match="lacks owned-stdin"):
            linux_containers._load_wsl_process_api(selected)
    else:
        loaded, material = linux_containers._load_wsl_process_api(selected)
        assert loaded is api
        assert set(material) == {api_path, script_path, api_path.parent / "wsl_managed_process.sh"}
    assert sys.path == original_path


def test_failed_linux_teardown_still_reaps_the_owned_windows_proxy(owned_wsl_lifetime, monkeypatch):
    session, state, _stopped, children, _helper = owned_wsl_lifetime
    state["ignore_eof"] = True
    monkeypatch.setattr(linux_containers, "_WSL_STOP_TIMEOUT", 0.05)
    def failed_stop(process):
        raise RuntimeError("Synthetic Linux cleanup failure")
    with pytest.raises(RuntimeError, match="lifetime cleanup failed"):
        with session:
            session.api.stop_process = failed_stop
    assert children[0].poll() is not None
    assert session.receipt["status"] == "cleanup-failed"


def test_changed_pid_file_cannot_redirect_teardown_to_an_unrelated_pid(owned_wsl_lifetime, monkeypatch):
    session, state, stopped, children, _helper = owned_wsl_lifetime
    state["ignore_eof"] = True
    monkeypatch.setattr(linux_containers, "_WSL_STOP_TIMEOUT", 0.05)
    with pytest.raises(RuntimeError, match="identity changed"):
        with session:
            session.process.pid_path.write_text("999\n", encoding="ascii")
            session.check("before-linux")
    assert stopped == [children[0].pid]
    assert children[0].poll() is not None
    assert (session.evidence / "wsl-owned-stop.pid").read_text(encoding="ascii") == "73\n"


@pytest.mark.parametrize("requires_alias", [False, True])
def test_wsl_pid_path_preserves_regular_output_and_maps_owned_mount_alias(owned_wsl_lifetime, monkeypatch, requires_alias):
    session, _state, _stopped, _children, _helper = owned_wsl_lifetime
    session.docker.prefix = ["wsl.exe", "-d", "Ubuntu", "--"]
    mapped = "/mnt/owned-output" if requires_alias else "/mnt/c/output"
    def map_directory(path):
        assert path == session.evidence
        if requires_alias:
            session.docker.aliases.append((path.resolve(), mapped))
        return mapped
    monkeypatch.setattr(session.docker, "path", map_directory)
    result = session._shared_pid_path()
    expected = (f"//wsl.localhost/Ubuntu{mapped}/wsl-session.pid" if requires_alias
                else (session.evidence / "wsl-session.pid").as_posix())
    assert result.as_posix() == expected
    assert session.receipt["pathMapping"]["evidence"] == mapped


def test_wsl_owned_alias_closes_after_child_and_proxy_teardown(owned_wsl_lifetime):
    session, _state, _stopped, children, _helper = owned_wsl_lifetime
    events = []
    with session:
        def close_alias():
            assert children[0].poll() is not None
            events.append("close-alias")
        session.docker.cleanup.callback(close_alias)
    assert events == ["close-alias"]


def test_wsl_alias_is_closed_when_launch_fails_before_a_process_is_returned(owned_wsl_lifetime, monkeypatch):
    session, _state, _stopped, children, _helper = owned_wsl_lifetime
    session.docker.prefix = ["wsl.exe", "-d", "Ubuntu", "--"]
    events = []
    def map_directory(path):
        session.docker.aliases.append((path.resolve(), "/mnt/owned-output"))
        session.docker.cleanup.callback(lambda: events.append("close-alias"))
        return "/mnt/owned-output"
    monkeypatch.setattr(session.docker, "path", map_directory)
    api, _material = linux_containers._load_wsl_process_api(session.harness)
    def failed_start(*args, **kwargs):
        raise RuntimeError("Synthetic launch failure")
    api.start_wsl_managed_executable = failed_start
    with pytest.raises(RuntimeError, match="Synthetic launch failure"):
        with session:
            pytest.fail("The check plan must not start")
    assert not children and events == ["close-alias"]
    assert session.receipt["planOutcome"] == "startup-failed"
    assert session.receipt["linuxCleanup"] == "not-started"


@pytest.mark.parametrize("phase", ["pid-readiness", "birth-capture"])
def test_startup_cancellation_closes_owned_input_before_identity_is_ready(owned_wsl_lifetime, monkeypatch, phase):
    session, state, stopped, children, _helper = owned_wsl_lifetime
    state["pid_ready"] = phase != "pid-readiness"
    def cancel(*args):
        raise KeyboardInterrupt("Synthetic startup cancellation")
    monkeypatch.setattr(session, "_wait_for_pid" if phase == "pid-readiness" else "_pid_birth", cancel)
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with pytest.raises(KeyboardInterrupt, match="startup cancellation"):
            with session:
                pytest.fail("The plan must not run after startup cancellation")
        assert children[0].stdin.closed and children[0].poll() == 0
        assert foreign.poll() is None and stopped == []
        assert session.receipt["status"] == "stopped"
        assert session.receipt["linuxCleanup"] == "stdin-eof-stopped"
        assert session.receipt["planOutcome"] == "interrupted"
    finally:
        foreign.terminate()
        foreign.wait(timeout=5)


def test_unverifiable_startup_teardown_is_failed_even_after_proxy_reaping(owned_wsl_lifetime, monkeypatch):
    session, state, stopped, children, _helper = owned_wsl_lifetime
    state.update(ignore_eof=True, pid_ready=False)
    monkeypatch.setattr(linux_containers, "_WSL_STOP_TIMEOUT", 0.05)
    def cancel():
        raise KeyboardInterrupt("Synthetic startup cancellation")
    monkeypatch.setattr(session, "_wait_for_pid", cancel)
    with pytest.raises(RuntimeError, match="lifetime cleanup failed"):
        with session:
            pytest.fail("The plan must not run")
    assert children[0].stdin.closed and children[0].poll() is not None
    assert stopped == [] and session.receipt["status"] == "cleanup-failed"
    assert "identity is incomplete" in session.receipt["cleanupError"]
