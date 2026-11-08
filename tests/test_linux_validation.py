"""Keep unavailable native runtime proof distinct from available local gates."""
import json
from types import SimpleNamespace

import pytest

from emule_workspace import linux_validation


@pytest.mark.parametrize("host", ["amd64", "arm64"])
def test_musl_push_gate_executes_full_native_workspace(host):
    assert linux_validation.validation_plan("musl", host) == [
        {"libc": "musl", "architecture": host, "buildAvailable": True,
         "testScope": "full", "runtime": "native",
         "runner": "ubuntu-24.04" if host == "amd64" else "ubuntu-24.04-arm"}]


def test_x64_builds_all_targets_but_does_not_claim_emulated_network_proof():
    plan = linux_validation.validation_plan("full", "amd64")
    assert len(plan) == 4
    assert all(item["buildAvailable"] and item["testScope"] == "full" for item in plan)
    assert [item["runtime"] for item in plan] == ["native", "requires-native-github"] * 2


def test_arm_host_explicitly_delegates_unavailable_x64_compilation():
    plan = linux_validation.validation_plan("full", "arm64")
    assert [item["buildAvailable"] for item in plan] == [False, True] * 2


@pytest.fixture
def batch(tmp_path, monkeypatch):
    rust = tmp_path / "rust"
    rust.mkdir()
    (rust / "Cargo.toml").write_text('[workspace.package]\nversion = "0.1.0-beta.2"\n')
    layout = SimpleNamespace(emulebb_rust_repo_root=rust, output_root=tmp_path / "output",
                             build_repo_root=tmp_path / "build", tests_repo_root=tmp_path / "harness")
    calls, failures = [], set()
    host = SimpleNamespace(architecture="amd64")
    class Docker:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def run(self, *args, **kwargs):
            return SimpleNamespace(stdout=host.architecture)
        def preflight(self, output):
            calls.append(("preflight", output))
    def build(layout, **kwargs):
        calls.append(("build", kwargs))
        if (kwargs["libc"], kwargs["architecture"]) in failures:
            raise RuntimeError("target failed")
        return layout.output_root / f'{kwargs["libc"]}-{kwargs["architecture"]}.tar.zst'
    def webui(command, **kwargs):
        calls.append(("webui", command))
        if "webui" in failures:
            raise RuntimeError("UI failed")
    def collect(layout, **kwargs):
        calls.append(("collect", kwargs))
        if "inputs" in failures:
            raise RuntimeError("missing or stale package")
        return layout.output_root / "inputs"
    def image(**kwargs):
        calls.append(("image", kwargs))
        return layout.output_root / "image.oci.tar"
    def validate(layout, **kwargs):
        calls.append(("security", kwargs))
        if "security" in failures:
            raise RuntimeError("CVE gate failed")
        return layout.output_root / "security.json"
    monkeypatch.setattr(linux_validation, "LinuxDocker", Docker)
    monkeypatch.setattr(linux_validation, "git_output", lambda repo, *args: "" if args[0] == "status" else "a" * 40)
    monkeypatch.setattr(linux_validation.subprocess, "run", webui)
    monkeypatch.setattr(linux_validation, "build_linux_artifacts", build)
    monkeypatch.setattr(linux_validation, "collect_linux_packages", collect)
    monkeypatch.setattr(linux_validation, "build_emulebb_rust_image_local", image)
    monkeypatch.setattr(linux_validation, "validate_image", validate)
    return SimpleNamespace(layout=layout, calls=calls, failures=failures, host=host)


def test_full_batch_reuses_only_its_fresh_ui_and_runs_available_checks(batch):
    report = json.loads(linux_validation.validate_linux(batch.layout, scope="full").read_text())
    assert report["status"] == "passed"
    assert report["coverage"] == "available-local-checks"
    builds = [data for kind, data in batch.calls if kind == "build"]
    assert len(builds) == 4
    assert len([data for kind, data in batch.calls if kind == "webui"]) == 1
    assert len({item["webui_dist"] for item in builds}) == 1
    assert all(item["test_scope"] == "full" for item in builds)
    assert [item["smoke"] for item in builds] == [True, False] * 2
    assert [item["testsExecuted"] for item in report["targets"]] == ["workspace", "portable-libraries-and-durability"] * 2
    assert [item["runtimeResult"] for item in report["targets"]] == ["passed", "delegated"] * 2
    assert len(report["githubRequired"]) == 2
    assert all(item["architecture"] == "arm64" for item in report["githubRequired"])
    assert all(item["status"] == "passed" for item in report["images"].values())


@pytest.mark.parametrize("failed_cpus", [("amd64",), ("arm64",), ("amd64", "arm64")])
def test_failed_targets_do_not_reuse_old_image_inputs_or_hide_other_failures(batch, failed_cpus, capsys):
    batch.failures.update(("gnu", cpu) for cpu in failed_cpus)
    with pytest.raises(RuntimeError, match="Local Linux CI failed: .*target failed"):
        linux_validation.validate_linux(batch.layout, scope="full")
    report = json.loads(next(batch.layout.output_root.glob("reports/local-linux-ci/*/results.json")).read_text())
    assert report["status"] == "failed"
    assert [item["build"] for item in report["targets"]] == [
        "failed" if cpu in failed_cpus else "passed" for cpu in ("amd64", "arm64")
    ] + ["passed", "passed"]
    assert all(item["status"] == "not-run" for item in report["images"].values())
    assert not any(kind in {"collect", "image", "security"} for kind, _ in batch.calls)
    output = capsys.readouterr().out
    assert all(f"FAILED Linux gnu/{cpu}: target failed" in output for cpu in failed_cpus)
    assert "PASSED Linux musl/amd64; network runtime: passed" in output


@pytest.mark.parametrize("failure", ["webui", "security"])
def test_ui_and_security_failures_remain_blocking_with_retained_report(batch, failure):
    batch.failures.add(failure)
    with pytest.raises(RuntimeError, match="Local Linux CI failed"):
        linux_validation.validate_linux(batch.layout, scope="full")
    report = json.loads(next(batch.layout.output_root.glob("reports/local-linux-ci/*/results.json")).read_text())
    assert report["status"] == "failed"
    assert not (failure == "webui" and any(kind == "build" for kind, _ in batch.calls))


def test_ordinary_musl_gate_includes_runtime_and_omits_image_work(batch):
    report = json.loads(linux_validation.validate_linux(batch.layout, scope="musl").read_text())
    assert report["status"] == "passed"
    assert report["githubRequired"] == []
    build = next(data for kind, data in batch.calls if kind == "build")
    assert build["libc"] == "musl" and build["smoke"] and build["test_scope"] == "full"
    assert not any(kind in {"collect", "image", "security", "preflight"} for kind, _ in batch.calls)


@pytest.mark.parametrize("host,arches", [("amd64", ("amd64", "arm64")), ("arm64", ("arm64",))])
def test_image_gate_rebuilds_and_scans_available_cpus_without_repeating_rust_tests(batch, host, arches):
    batch.host.architecture = host
    report = json.loads(linux_validation.validate_linux(batch.layout, scope="image").read_text())
    assert report["status"] == "passed" and report["coverage"] == "image-only"
    assert report["targets"] == [] and report["githubRequired"] == []
    assert all(item["architectures"] == list(arches) for item in report["images"].values())
    assert not any(kind in {"webui", "build"} for kind, _ in batch.calls)
    assert [kind for kind, _ in batch.calls if kind != "preflight"] == ["collect", "image", "security"] * 2
    assert all(data["architectures"] == arches for kind, data in batch.calls if kind != "preflight")


@pytest.mark.parametrize("failure", ["inputs", "security"])
def test_image_gate_retains_failure_and_never_accepts_a_prior_pass(batch, failure):
    batch.failures.add(failure)
    with pytest.raises(RuntimeError, match="--scope full" if failure == "inputs" else "CVE gate failed"):
        linux_validation.validate_linux(batch.layout, scope="image")
    report = json.loads(next(batch.layout.output_root.glob("reports/local-linux-ci/*/results.json")).read_text())
    assert report["status"] == "failed" and "images" not in report
    if failure == "inputs":
        assert not any(kind in {"image", "security"} for kind, _ in batch.calls)
