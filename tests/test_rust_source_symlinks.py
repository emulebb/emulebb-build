"""Hash real upstream-style native file links without admitting redirected roots."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from emule_workspace import rust_source_cache as cache
from emule_workspace.git_environment import git_environment


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], env=git_environment(),
                   capture_output=True, check=True)


def symlink(path: Path, target: str, *, directory: bool = False) -> None:
    try:
        # Windows reparse targets require native separators; Path preserves the
        # literal upstream relative form on POSIX and gives a usable native link.
        path.symlink_to(Path(target), target_is_directory=directory)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            pytest.skip("Native Windows symlink creation requires an operator privilege")
        raise


@pytest.fixture
def sources(tmp_path):
    rust, native = tmp_path / "rust", tmp_path / "native"
    for root, files in ((rust, {"Cargo.toml": "[workspace]\n", "source.rs": "// Sample source\n"}),
                        (native, {"LICENSE": "Sample license\n", "COPYING": "Sample license\n",
                                  "miniupnpd/getifaddr.c": "int sample(void) { return 1; }\n",
                                  "miniupnpd/getifaddr.h": "int sample(void);\n"})):
        root.mkdir()
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        git(root, "init", "-q")
        git(root, "add", ".")
    (native / "minissdpd").mkdir()
    # These are the literal relative shapes in the pinned upstream checkout.
    symlink(native / "minissdpd/LICENSE", "../LICENSE")
    symlink(native / "minissdpd/getifaddr.c", "../miniupnpd/getifaddr.c")
    symlink(native / "minissdpd/getifaddr.h", "../miniupnpd/getifaddr.h")
    git(native, "add", ".")
    return rust, native


def test_tracked_native_license_c_and_h_links_bind_target_bytes_and_link_spelling(sources):
    rust, native = sources
    original = cache._source_identity(sources)
    assert cache._source_identity(sources) == original
    for name in ("LICENSE", "miniupnpd/getifaddr.c", "miniupnpd/getifaddr.h"):
        path = native / name
        content = path.read_bytes()
        path.write_bytes(content + b"// Changed material\n")
        assert cache._source_identity(sources) != original
        path.write_bytes(content)
    # Equal bytes at a different admitted target still change the link identity.
    link = native / "minissdpd/LICENSE"
    link.unlink()
    symlink(link, "../COPYING")
    assert cache._source_identity(sources) != original
    assert (rust / "source.rs").read_text(encoding="utf-8") == "// Sample source\n"


@pytest.mark.parametrize("invalid", ["escaping", "absolute", "dangling", "directory", "self-cycle", "cycle"])
def test_native_links_fail_closed_for_noncontained_or_nonregular_targets(sources, invalid):
    _rust, native = sources
    link = native / "minissdpd/LICENSE"
    link.unlink()
    outside = native.parent / "outside.txt"
    outside.write_text("Outside material\n", encoding="utf-8")
    targets = {"escaping": "../../outside.txt", "absolute": str(native / "LICENSE"),
               "dangling": "../missing", "directory": "../miniupnpd",
               "self-cycle": "LICENSE", "cycle": "loop"}
    if invalid == "cycle":
        symlink(link.parent / "loop", "LICENSE")
    symlink(link, targets[invalid])
    with pytest.raises(RuntimeError):
        cache._source_identity(sources)


@pytest.mark.parametrize("redirect", ["link-parent", "target-parent", "root"])
def test_native_links_do_not_admit_redirected_root_or_directory_ancestors(sources, redirect):
    rust, native = sources
    directory = {"link-parent": native / "minissdpd",
                 "target-parent": native / "miniupnpd", "root": native}[redirect]
    # Keep relocated files outside the admitted tree so newly untracked links
    # cannot mask the specific ancestor-redirection rejection under test.
    moved = native.parent / f"{native.name}-{directory.name}-physical"
    directory.rename(moved)
    symlink(directory, str(moved), directory=True)
    # Git may inventory the directory link itself as untracked before its
    # tracked children; either admission guard must refuse the redirected tree.
    with pytest.raises(RuntimeError):
        cache._source_identity((rust, native))


@pytest.mark.parametrize("replacement", ["untracked", "regular-file"])
def test_native_links_require_git_index_symlink_mode(sources, replacement):
    _rust, native = sources
    link = native / ("untracked-link" if replacement == "untracked" else "LICENSE")
    if replacement == "regular-file":
        link.unlink()
    symlink(link, "COPYING")
    with pytest.raises(RuntimeError, match="tracked with Git symlink mode"):
        cache._source_identity(sources)


def test_rust_source_links_remain_forbidden_even_when_tracked(sources):
    rust, _native = sources
    link = rust / "source.rs"
    link.unlink()
    symlink(link, "Cargo.toml")
    git(rust, "add", "source.rs")
    with pytest.raises(RuntimeError, match="Redirected workstream path"):
        cache._source_identity(sources)


@pytest.mark.parametrize("redirect", ["outside", "same-parent", "missing"])
def test_link_dotdot_cannot_hide_ignored_redirect_or_missing_directory(sources, redirect):
    _rust, native = sources
    excluded = native / "ignored-redirect"
    # The inventory must not reject the directory link first: exercise literal
    # target resolution even when Git legitimately excludes that separate path.
    with (native / ".git/info/exclude").open("a", encoding="utf-8") as stream:
        stream.write("\n/ignored-redirect\n")
    if redirect == "outside":
        external = native.parent / "external"
        (external / "nested").mkdir(parents=True)
        (external / "LICENSE").write_text("Different external material\n", encoding="utf-8")
        symlink(excluded, str(external / "nested"), directory=True)
    elif redirect == "same-parent":
        symlink(excluded, "miniupnpd", directory=True)
    link = native / "minissdpd/LICENSE"
    link.unlink()
    symlink(link, "../ignored-redirect/../LICENSE")
    with pytest.raises(RuntimeError, match="resolve|redirected ancestor"):
        cache._source_identity(sources)
