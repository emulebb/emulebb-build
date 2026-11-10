"""Source-bound Cargo reuse, including a real older-checkout regression."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from emule_workspace import rust_source_cache as cache
from emule_workspace.config import WorkspaceOptions
from emule_workspace.git_environment import git_environment
from emule_workspace.locks import WORKSPACE_LOCK_TOKEN_ENV, WorkspaceLock


def git(root, *arguments):
    return subprocess.run(["git", "-C", str(root), *arguments], env=git_environment(),
                          capture_output=True, text=True, check=True).stdout


def repository(path, files, *, before_commit=None):
    path.mkdir(parents=True)
    for name, text in files.items():
        destination = path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
    if before_commit is not None:
        before_commit(path)
    git(path, "init", "-q")
    git(path, "add", "--", *files)
    git(path, "-c", "user.name=Synthetic Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "Synthetic fixture")
    return path


@pytest.fixture
def guarded(tmp_path_factory, monkeypatch):
    # Keep real Git/Cargo fixtures below OUTPUT without Windows path-length noise.
    tmp_path = tmp_path_factory.mktemp("rust-cache")
    workspace, output = tmp_path / "workspace", tmp_path / "output"
    workspace.mkdir()
    target = output / "builds/rust/target"
    target.mkdir(parents=True)
    layout = SimpleNamespace(emule_workspace_root=workspace, output_root=output,
                             output_tmp_root=output / "tmp", output_rust_target_root=target,
                             workspace_name="workspace", emulebb_rust_repo_root=None)
    monkeypatch.setattr(cache, "resolve_required_workspace_roots", lambda: (workspace, output))
    monkeypatch.setattr(cache, "resolve_required_cargo_target_dir", lambda output: target)
    lock = WorkspaceLock(layout, "synthetic source-cache regression", WorkspaceOptions(workspace_root=workspace))
    assert lock.acquire()
    monkeypatch.setenv(WORKSPACE_LOCK_TOKEN_ENV, lock.delegation_token)
    try:
        yield layout
    finally:
        lock.release()


def source(layout, name="first", value="one"):
    return repository(layout.emule_workspace_root / name, {
        "Cargo.toml": '[workspace]\nmembers=["sample"]\n',
        "sample/Cargo.toml": '[package]\nname="source-cache-sample"\nversion="0.1.0"\nedition="2024"\n',
        "sample/src/lib.rs": f'pub fn value() -> &\'static str {{ "{value}" }}\n',
    })


def fake_runner(layout, calls, *, fail_clean=False, target=None):
    def run(arguments):
        calls.append(arguments)
        if arguments[0] == "metadata":
            return json.dumps({"workspace_root": str(layout.emulebb_rust_repo_root),
                               "target_directory": str(target or layout.output_rust_target_root),
                               "workspace_members": ["sample"], "packages": [{"id": "sample",
                               "name": "source-cache-sample", "manifest_path":
                               str(layout.emulebb_rust_repo_root / "sample/Cargo.toml")}]})
        if fail_clean:
            raise RuntimeError("synthetic Cargo clean failure")
        return ""
    return run


def test_unqualified_cache_cleans_workspace_host_and_target_then_reuses_binding(guarded):
    layout = guarded
    layout.emulebb_rust_repo_root = source(layout)
    explicit = layout.output_rust_target_root / "aarch64-unknown-linux-gnu/debug"
    explicit.mkdir(parents=True)
    calls = []
    runner = fake_runner(layout, calls)
    cache.ensure_rust_source_cache(layout, runner=runner, native_roots=(layout.emulebb_rust_repo_root,))
    assert calls == [["metadata", "--offline", "--locked", "--no-deps", "--format-version", "1"],
                     ["clean", "--package", "source-cache-sample", "--profile", "dev"],
                     ["clean", "--package", "source-cache-sample", "--profile", "release"],
                     ["clean", "--package", "source-cache-sample", "--target", "aarch64-unknown-linux-gnu", "--profile", "dev"],
                     ["clean", "--package", "source-cache-sample", "--target", "aarch64-unknown-linux-gnu", "--profile", "release"]]
    calls.clear()
    cache.ensure_rust_source_cache(layout, runner=runner, native_roots=(layout.emulebb_rust_repo_root,))
    assert calls == [["metadata", "--offline", "--locked", "--no-deps", "--format-version", "1"]]


def test_dirty_older_source_bytes_invalidate_without_touching_dependencies(guarded):
    layout = guarded
    layout.emulebb_rust_repo_root = source(layout)
    dependency = layout.output_rust_target_root / "debug/deps/dependency-marker"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("keep", encoding="utf-8")
    calls = []
    runner = fake_runner(layout, calls)
    before = cache.ensure_rust_source_cache(layout, runner=runner, native_roots=(layout.emulebb_rust_repo_root,))
    changed = layout.emulebb_rust_repo_root / "sample/src/lib.rs"
    changed.write_text("pub fn changed() {}\n", encoding="utf-8")
    os.utime(changed, (1, 1))
    calls.clear()
    after = cache.ensure_rust_source_cache(layout, runner=runner, native_roots=(layout.emulebb_rust_repo_root,))
    assert before["sources"] != after["sources"]
    assert calls[1] == ["clean", "--package", "source-cache-sample", "--profile", "dev"]
    assert dependency.read_text() == "keep"


def test_wrong_metadata_target_and_failed_clean_never_publish_binding(guarded):
    layout = guarded
    layout.emulebb_rust_repo_root = source(layout)
    for runner in (fake_runner(layout, [], target=layout.output_root / "wrong"),
                   fake_runner(layout, [], fail_clean=True)):
        with pytest.raises(RuntimeError):
            cache.ensure_rust_source_cache(layout, runner=runner, native_roots=(layout.emulebb_rust_repo_root,))
        assert not (layout.output_rust_target_root / cache.RECEIPT).exists()


def test_missing_owner_fails_before_metadata_or_receipt(guarded, monkeypatch):
    layout = guarded
    layout.emulebb_rust_repo_root = source(layout)
    monkeypatch.delenv(WORKSPACE_LOCK_TOKEN_ENV)
    calls = []
    with pytest.raises(RuntimeError, match="active workspace lock"):
        cache.ensure_rust_source_cache(layout, runner=fake_runner(layout, calls))
    assert calls == []
    assert not (layout.output_rust_target_root / cache.RECEIPT).exists()


def test_native_material_change_invalidates_even_with_older_timestamp(guarded):
    layout = guarded
    layout.emulebb_rust_repo_root = source(layout)
    native = repository(layout.emule_workspace_root / "native", {"source.c": "int value = 1;\n"})
    calls = []
    runner = fake_runner(layout, calls)
    cache.ensure_rust_source_cache(layout, runner=runner, native_roots=(native,))
    (native / "source.c").write_text("int value = 2;\n", encoding="utf-8")
    os.utime(native / "source.c", (1, 1))
    calls.clear()
    cache.ensure_rust_source_cache(layout, runner=runner, native_roots=(native,))
    assert calls[1] == ["clean", "--package", "source-cache-sample", "--profile", "dev"]


def test_source_change_during_clean_cannot_publish_new_binding(guarded):
    layout = guarded
    layout.emulebb_rust_repo_root = source(layout)
    calls = []
    invoke = fake_runner(layout, calls)
    def runner(arguments):
        result = invoke(arguments)
        if arguments[0] == "clean":
            (layout.emulebb_rust_repo_root / "sample/src/lib.rs").write_text("changed\n", encoding="utf-8")
        return result
    with pytest.raises(RuntimeError, match="changed during"):
        cache.ensure_rust_source_cache(layout, runner=runner, native_roots=(layout.emulebb_rust_repo_root,))
    assert not (layout.output_rust_target_root / cache.RECEIPT).exists()


def test_malformed_binding_fails_closed_without_clean(guarded):
    layout = guarded
    layout.emulebb_rust_repo_root = source(layout)
    path = layout.output_rust_target_root / cache.RECEIPT
    path.write_text("invalid json\n", encoding="utf-8")
    calls = []
    with pytest.raises(RuntimeError, match="invalid"):
        cache.ensure_rust_source_cache(layout, runner=fake_runner(layout, calls),
                                       native_roots=(layout.emulebb_rust_repo_root,))
    assert calls == []
    assert path.read_text() == "invalid json\n"


def test_default_native_roots_and_inherited_override_are_material_inputs(guarded, monkeypatch):
    layout = guarded
    layout.emulebb_rust_repo_root = source(layout)
    mini = repository(layout.emule_workspace_root / "repos/third_party/emulebb-miniupnp", {"source.c": "one\n"})
    pcp = repository(layout.emule_workspace_root / "repos/third_party/emulebb-libpcpnatpmp", {"source.c": "two\n"})
    monkeypatch.delenv("MINIUPNP_ROOT", raising=False)
    monkeypatch.delenv("PCPNATPMP_ROOT", raising=False)
    calls = []
    runner = fake_runner(layout, calls)
    before = cache.ensure_rust_source_cache(layout, runner=runner)
    assert [item["root"] for item in before["sources"]] == [str(layout.emulebb_rust_repo_root), str(mini), str(pcp)]
    (mini / "source.c").write_text("older changed native source\n", encoding="utf-8")
    os.utime(mini / "source.c", (1, 1))
    calls.clear()
    cache.ensure_rust_source_cache(layout, runner=runner)
    assert calls[1][0] == "clean"
    override = repository(layout.emule_workspace_root.parent / "override", {"source.c": "override\n"})
    monkeypatch.setenv("MINIUPNP_ROOT", str(override))
    after = cache.ensure_rust_source_cache(layout, runner=runner)
    assert [item["root"] for item in after["sources"]][1:] == [str(override), str(pcp)]


def test_explicit_prefetch_precedes_offline_metadata_without_retry(guarded):
    layout = guarded
    layout.emulebb_rust_repo_root = source(layout)
    calls = []
    cache.ensure_rust_source_cache(layout, runner=fake_runner(layout, calls),
                                   native_roots=(layout.emulebb_rust_repo_root,), prefetch=True)
    assert calls[:2] == [["fetch", "--locked"],
                         ["metadata", "--offline", "--locked", "--no-deps", "--format-version", "1"]]


def test_matching_receipt_still_rejects_actual_wrong_target(guarded):
    layout = guarded
    layout.emulebb_rust_repo_root = source(layout)
    cache.ensure_rust_source_cache(layout, runner=fake_runner(layout, []),
                                   native_roots=(layout.emulebb_rust_repo_root,))
    path = layout.output_rust_target_root / cache.RECEIPT
    before = path.read_bytes()
    calls = []
    with pytest.raises(RuntimeError, match="selected source and target"):
        cache.ensure_rust_source_cache(layout, runner=fake_runner(layout, calls, target=layout.output_root / "wrong"),
                                       native_roots=(layout.emulebb_rust_repo_root,))
    assert len(calls) == 1 and calls[0][0] == "metadata"
    assert path.read_bytes() == before


def test_source_mutation_during_warm_metadata_is_not_accepted(guarded):
    layout = guarded
    layout.emulebb_rust_repo_root = source(layout)
    invoke = fake_runner(layout, [])
    cache.ensure_rust_source_cache(layout, runner=invoke, native_roots=(layout.emulebb_rust_repo_root,))
    def changing(arguments):
        result = invoke(arguments)
        if arguments[0] == "metadata":
            (layout.emulebb_rust_repo_root / "sample/src/lib.rs").write_text("changed during metadata\n", encoding="utf-8")
        return result
    with pytest.raises(RuntimeError, match="changed during cache validation"):
        cache.ensure_rust_source_cache(layout, runner=changing, native_roots=(layout.emulebb_rust_repo_root,))


@pytest.mark.parametrize("invalid", ["tokenless", "missing-pid", "foreign-owner", "bool-pid",
                                    "wrong-root", "wrong-workspace", "empty-stored-token", "missing-record"])
def test_malformed_lock_owner_cannot_authorize_cargo_clean(guarded, monkeypatch, invalid):
    layout = guarded
    layout.emulebb_rust_repo_root = source(layout)
    path = layout.output_tmp_root / "active-command-lock.json"
    original = path.read_bytes()
    owner = json.loads(original)
    if invalid == "tokenless":
        owner = {"workspace_root": str(layout.emule_workspace_root)}
        monkeypatch.delenv(WORKSPACE_LOCK_TOKEN_ENV)
    elif invalid == "missing-pid":
        owner.pop("pid")
    elif invalid == "foreign-owner":
        owner["machine_name"] = "synthetic-foreign-owner"
    elif invalid == "bool-pid":
        owner["pid"] = True
    elif invalid == "wrong-root":
        owner["workspace_root"] = str(layout.emule_workspace_root / "wrong")
    elif invalid == "wrong-workspace":
        owner["workspace_name"] = "another-workspace"
    elif invalid == "empty-stored-token":
        owner["delegation_token"] = ""
    if invalid == "missing-record":
        path.unlink()
    else:
        path.write_text(json.dumps(owner), encoding="utf-8")
    calls = []
    try:
        with pytest.raises(RuntimeError, match="active workspace lock"):
            cache.ensure_rust_source_cache(layout, runner=fake_runner(layout, calls))
        assert calls == []
        assert not (layout.output_rust_target_root / cache.RECEIPT).exists()
    finally:
        path.write_bytes(original)


def test_real_cargo_rebuilds_older_other_checkout_and_keeps_nonworkspace_dependency(guarded):
    """Execute distinct A/B tests, not a command-plan assertion or a fake compiler."""
    layout = guarded
    dependency = repository(layout.emule_workspace_root / "dependency", {
        "Cargo.toml": '[package]\nname="synthetic-dependency"\nversion="0.1.0"\nedition="2024"\n',
        "src/lib.rs": "pub fn value() -> u8 { 7 }\n",
    })
    environment = git_environment()
    environment["CARGO_TARGET_DIR"] = str(layout.output_rust_target_root)
    cold_home = layout.output_root / "cache/synthetic-cargo-home"
    cold_home.mkdir(parents=True)
    environment["CARGO_HOME"] = str(cold_home)
    def canonical_lock(path):
        # --locked may still normalize a hand-authored lock's presentation.
        # Generate stable Cargo-owned bytes before Git/identity/mtime setup.
        cache._host_runner(path, environment)(["generate-lockfile", "--offline"])
    roots = []
    for name in ("first", "second"):
        roots.append(repository(layout.emule_workspace_root / name, {
            "Cargo.toml": '[workspace]\nmembers=["sample"]\nresolver="3"\n[profile.cache-proof]\ninherits="release"\nopt-level=0\n',
            "rust-toolchain.toml": '[toolchain]\nchannel="1.98.1"\nprofile="minimal"\n',
            "Cargo.lock": 'version=4\n[[package]]\nname="source-cache-sample"\nversion="0.1.0"\ndependencies=["synthetic-dependency"]\n[[package]]\nname="synthetic-dependency"\nversion="0.1.0"\n',
            "sample/Cargo.toml": '[package]\nname="source-cache-sample"\nversion="0.1.0"\nedition="2024"\n[dependencies]\nsynthetic-dependency={path="../../dependency"}\n',
            "sample/src/lib.rs": f'#[test] fn from_{name}_checkout() {{ assert_eq!(synthetic_dependency::value(),7); }}\n',
        }, before_commit=canonical_lock))
    for item in roots[1].rglob("*"):
        if item.is_file() and ".git" not in item.parts:
            os.utime(item, (1, 1))
    original_environment = os.environ.copy()
    layout.emulebb_rust_repo_root = roots[0]
    first = cache._host_runner(roots[0], environment)
    host = re.search(r"^host: (\S+)$", first(["-vV"]), re.MULTILINE).group(1)
    modes = ([], ["--release"], ["--target", host], ["--target", host, "--release"],
             ["--profile", "cache-proof"], ["--target", host, "--profile", "cache-proof"])
    cache.ensure_rust_source_cache(layout, runner=first, native_roots=(dependency,), prefetch=True)
    for mode in modes:
        assert "from_first_checkout ... ok" in first(["test", "--locked", "--offline", *mode])
    dependency_artifacts = list(layout.output_rust_target_root.rglob("*synthetic_dependency*.rlib"))
    assert len(dependency_artifacts) >= len(modes)
    retained = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in dependency_artifacts}
    layout.emulebb_rust_repo_root = roots[1]
    second = cache._host_runner(roots[1], environment)
    for mode in modes:
        stale = second(["test", "--locked", "--offline", *mode])
        assert "from_first_checkout ... ok" in stale
        assert "from_second_checkout" not in stale
    cache.ensure_rust_source_cache(layout, runner=second, native_roots=(dependency,))
    for mode in modes:
        actual = second(["test", "--locked", "--offline", *mode])
        assert "from_second_checkout ... ok" in actual
        assert "from_first_checkout" not in actual
    assert retained == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in dependency_artifacts}
    assert os.environ == original_environment
