"""Verify architecture preflight rejects advertised but unusable ARM64 execution."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from emule_workspace.linux_containers import LinuxDocker, BINFMT_IMAGE
from emule_workspace.linux_containers import validate_elf_audit
from emule_workspace.linux_containers import build_configuration, RUST_IMAGES, MUSL_CROSS_IMAGE
from emule_workspace.linux_containers import run_owned_container
from emule_workspace.linux_containers import _package_gnu_deb
from emule_workspace.linux_containers import linux_test_plan, linux_quality_plan
from emule_workspace import linux_containers


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


def test_linux_build_prepares_user_owned_paths_and_never_runs_cargo_or_packaging_as_root(tmp_path, monkeypatch):
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
    archive = linux_containers._build_linux_artifacts(layout, Docker(), libc="musl", architecture="amd64",
                                                      webui_dist=webui, release_version=None, test_scope="portable")
    assert cargo_calls
    for arguments in [*cargo_calls, *(args for args in calls if args[0] == "run")]:
        assert arguments[arguments.index("--user") + 1] == "1001:1002"
        image_index = next(index for index, value in enumerate(arguments) if value.startswith("local/emulebb-rust-builder:"))
        assert arguments.index("--user") < image_index
        assert arguments[arguments.index("--cap-drop") + 1] == "ALL"
    manifest = json.loads(archive.with_name(archive.name + ".manifest.json").read_text())
    assert manifest["buildUser"] == "1001:1002"
    assert manifest["pathMapping"]["cargoTarget"] == target_mapping


@pytest.mark.parametrize("producer_run", ["123", "999"])
def test_hosted_runtime_launch_receives_only_this_runs_public_identity(monkeypatch, tmp_path, hosted_context, producer_run):
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
    monkeypatch.setattr(linux_containers.subprocess, "check_output", lambda *args, **kwargs: "1000")
    monkeypatch.setattr(linux_containers, "run_owned_container", lambda docker, arguments, *args, **kwargs: launches.append(arguments))
    if producer_run != "123":
        with pytest.raises(RuntimeError, match="this GitHub run and workflow"):
            linux_containers.smoke_linux_archive(layout, archive=archive, libc="musl", architecture="amd64")
        assert not calls and not launches
        return
    linux_containers.smoke_linux_archive(layout, archive=archive, libc="musl", architecture="amd64")
    assert calls[0][0] == "version"
    command = launches[0]
    image_index = command.index("local/emulebb-rust-runtime:musl-amd64")
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


def test_timed_out_wsl_job_removes_only_its_owned_container(tmp_path, monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if "--name" in command:
            kwargs["stdout"].write("retained build failure\n")
            raise subprocess.TimeoutExpired(command, 1)
    monkeypatch.setattr("emule_workspace.linux_containers.subprocess.run", run)
    docker = LinuxDocker()
    path = tmp_path / "build.log"
    with pytest.raises(RuntimeError, match="inspect"):
        run_owned_container(docker, ["--rm", "image", "cargo", "test"], path, timeout=1)
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[-1][-3:] == ["rm", "--force", name]
    assert path.read_text() == "retained build failure\n"


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
