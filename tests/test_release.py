from __future__ import annotations

import configparser
import hashlib
import json
import struct
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from emule_workspace import release
from emule_workspace.config import EmulebbRustPackageOptions, WorkspaceOptions
from emule_workspace.layout import AppVariant, TestTargets, WorkspaceLayout


@pytest.mark.parametrize(
    "release_version",
    [
        "0.7.3",
        "0.7.3-rc.2",
        "0.7.3-beta.2",
        "0.7.3-nightly.20260524.ae562c1",
        "0.7.3-nightly.20260524.0123456789abcdef",
    ],
)
def test_release_version_accepts_public_and_nightly_formats(release_version: str) -> None:
    assert release._is_release_version(release_version)


@pytest.mark.parametrize(
    "release_version",
    [
        "0.7",
        "0.7.3-nightly",
        "0.7.3-nightly.2026052.ae562c1",
        "0.7.3-nightly.20260524.zzzzzzz",
        "0.7.3-alpha.1",
    ],
)
def test_release_version_rejects_unknown_formats(release_version: str) -> None:
    assert not release._is_release_version(release_version)


def _pe_payload(machine: int) -> bytes:
    payload = bytearray(128)
    payload[0:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x40)
    payload[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<H", payload, 0x44, machine)
    return bytes(payload)


def _parse_rgb(value: str) -> tuple[int, int, int]:
    channels = tuple(int(channel.strip()) for channel in value.split(","))
    assert len(channels) == 3
    assert all(0 <= channel <= 255 for channel in channels)
    return channels


def _linear_channel(channel: int) -> float:
    normalized = channel / 255
    if normalized <= 0.03928:
        return normalized / 12.92
    return ((normalized + 0.055) / 1.055) ** 2.4


def _relative_luminance(color: tuple[int, int, int]) -> float:
    red, green, blue = (_linear_channel(channel) for channel in color)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    first_luminance = _relative_luminance(first)
    second_luminance = _relative_luminance(second)
    lighter = max(first_luminance, second_luminance)
    darker = min(first_luminance, second_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def _write_release_zip(
    path: Path,
    *,
    executable_name: str = "emulebb.exe",
    language_payloads: dict[str, bytes] | None = None,
    extra_entries: dict[str, bytes] | None = None,
    include_skin_assets: bool = True,
) -> None:
    entries = {
        f"eMuleBB/{executable_name}": _pe_payload(0x8664),
        "eMuleBB/README.md": b"readme\n",
        "eMuleBB/RELEASE-NOTES.md": b"notes\n",
        "eMuleBB/LICENSE-NOTICE.txt": b"notice\n",
        "eMuleBB/GPL-2.0-or-later.txt": b"gpl\n",
        "eMuleBB/THIRD-PARTY-NOTICES.txt": b"third party\n",
        "eMuleBB/SBOM.spdx.json": b'{"spdxVersion":"SPDX-2.3"}\n',
        "eMuleBB/docs/REST-API-CONTRACT.md": b"contract\n",
        "eMuleBB/docs/REST-API-OPENAPI.yaml": b"openapi\n",
        "eMuleBB/docs/REST-API-PARITY-INVENTORY.md": b"parity\n",
    }
    for relative_path in release.EMULEBB_RUNTIME_SCRIPT_PATHS:
        entries[f"eMuleBB/{relative_path}"] = b"#Requires -Version 5.1\n"
    for relative_path in release.EMULEBB_CONFIG_ASSET_PATHS:
        entries[f"eMuleBB/{relative_path}"] = b'{"schema":"test"}\n'
    if include_skin_assets:
        for relative_path in release.EMULEBB_SKIN_ASSET_PATHS:
            entries[f"eMuleBB/{relative_path}"] = b"skin-or-toolbar-asset\n"
    for name, payload in (language_payloads or {"de_DE.dll": _pe_payload(0x8664)}).items():
        entries[f"eMuleBB/lang/{name}"] = payload
    entries.update(extra_entries or {})
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)


def _write_amutorrent_zip(path: Path, *, extra_entries: dict[str, bytes] | None = None) -> None:
    entries = {
        "aMuTorrent/README.md": b"readme\n",
        "aMuTorrent/LICENSE-aMuTorrent.txt": b"license\n",
        "aMuTorrent/SBOM.spdx.json": b'{"spdxVersion":"SPDX-2.3"}\n',
        "aMuTorrent/server/server.js": b"server\n",
        "aMuTorrent/server/package.json": b"{}\n",
        "aMuTorrent/server/package-lock.json": b"{}\n",
        "aMuTorrent/server/node_modules/express/package.json": b"{}\n",
        "aMuTorrent/server/node_modules/better-sqlite3/package.json": b"{}\n",
        "aMuTorrent/static/index.html": b"<html></html>\n",
        "aMuTorrent/static/output.css": b"body{}\n",
        "aMuTorrent/static/dist/app.bundle.js": b"console.log('ok');\n",
    }
    entries.update(extra_entries or {})
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)


def _make_rust_package_layout(tmp_path: Path) -> WorkspaceLayout:
    workspace_root = tmp_path / "workspace-root"
    output_root = tmp_path / "workspace-output"
    return WorkspaceLayout(
        emule_workspace_root=workspace_root,
        workspace_name="workspace",
        workspace_root=workspace_root / "workspaces" / "workspace",
        build_repo_root=workspace_root / "repos" / "emulebb-build",
        tests_repo_root=workspace_root / "repos" / "emulebb-build-tests",
        tooling_repo_root=workspace_root / "repos" / "emulebb-tooling",
        ed2k_server_repo_root=workspace_root / "repos" / "goed2k-server",
        amule_repo_root=workspace_root / "repos" / "amule",
        seed_repo_path=workspace_root / "repos" / "emulebb",
        seed_repo_branch="main",
        dependencies=(),
        app_variants=(),
        test_targets=TestTargets(test_build_variant="main", test_run_variant="main", baseline_variant="community"),
        toolset_override_variable="EMULEBB_VS_PLATFORM_TOOLSET",
        emulebb_rust_repo_root=workspace_root / "repos" / "emulebb-rust",
        output_root=output_root,
    )


def _stage_rust_package_inputs(layout: WorkspaceLayout) -> None:
    rust_root = layout.emulebb_rust_repo_root
    assert rust_root is not None
    for repo in (rust_root, layout.build_repo_root, layout.tooling_repo_root):
        repo.mkdir(parents=True, exist_ok=True)
    (rust_root / "Cargo.toml").write_text(
        "[workspace]\n[workspace.package]\nversion = \"0.1.0-beta.1\"\n",
        encoding="utf-8",
        newline="\n",
    )
    (rust_root / "emulebb-rust-settings.example.toml").write_text("[rest]\n", encoding="utf-8", newline="\n")
    (rust_root / "LICENSE").write_text("GPL-2.0-only\n", encoding="utf-8", newline="\n")
    scope = layout.tooling_repo_root / "docs" / "products" / "emulebb-rust" / "RELEASE-SCOPE.md"
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text("# emulebb-rust Release Scope\n", encoding="utf-8", newline="\n")
    staged_bin = layout.output_tools_root / "emulebb-rust" / "bin"
    (staged_bin / "webui").mkdir(parents=True, exist_ok=True)
    (staged_bin / "emulebb-rust.exe").write_bytes(_pe_payload(0x8664))
    (staged_bin / "emulebb-rust-ui.exe").write_bytes(_pe_payload(0x8664))
    (staged_bin / "emulebb-rust-diagnostics.exe").write_bytes(_pe_payload(0x8664))
    (staged_bin / "webui" / "index.html").write_text("<div>webui</div>", encoding="utf-8", newline="\n")
    (staged_bin / "webui" / "assets.js").write_text("console.log('webui');\n", encoding="utf-8", newline="\n")


def test_package_release_dirty_guard_reports_all_provenance_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_root = tmp_path / "workspaces" / "workspace" / "app" / "emulebb-main"
    build_root = tmp_path / "repos" / "emulebb-build"
    tests_root = tmp_path / "repos" / "emulebb-build-tests"
    tooling_root = tmp_path / "repos" / "emulebb-tooling"
    for path in (app_root, build_root, tests_root, tooling_root):
        path.mkdir(parents=True)

    dirty = {
        app_root: ["## main...origin/main", " M srchybrid/Preferences.cpp"],
        build_root: ["## main...origin/main"],
        tests_root: ["## main...origin/main", "?? tests/python/test_release_update_urls.py"],
        tooling_root: ["## main...origin/main", " M docs/active/RELEASE-0.7.3.md"],
    }
    monkeypatch.setattr(release, "repo_status_lines", lambda repo: dirty[repo])
    layout = SimpleNamespace(
        build_repo_root=build_root,
        tests_repo_root=tests_root,
        tooling_repo_root=tooling_root,
    )

    with pytest.raises(RuntimeError, match="clean provenance inputs") as excinfo:
        release._assert_clean_release_inputs(layout, app_root)

    message = str(excinfo.value)
    assert "app source" in message
    assert "build orchestration" not in message
    assert "build tests" in message
    assert "tooling docs" in message
    assert "Preferences.cpp" in message
    assert "test_release_update_urls.py" in message
    assert "RELEASE-0.7.3.md" in message


def test_package_release_requires_main_app_source_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_root = tmp_path / "workspaces" / "workspace" / "app" / "emulebb-main"
    app_root.mkdir(parents=True)
    app_variant = AppVariant(name="main", path=app_root, branch="main")
    monkeypatch.setattr(release, "repo_branch", lambda repo: "feature/release-drift")

    with pytest.raises(RuntimeError, match="requires app variant 'main'.*branch 'main'"):
        release._assert_release_source_branch(app_variant)


def test_package_build_disables_startup_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_root = tmp_path / "app"
    captured: dict[str, object] = {}
    session = SimpleNamespace(
        layout=SimpleNamespace(toolset_override_variable="EMULEBB_TEST_TOOLSET"),
        options=SimpleNamespace(configuration="Release", platform="x64"),
    )

    monkeypatch.setattr(release, "ensure_app_dependency_artifacts", lambda _layout, _options, *, clean: None)
    monkeypatch.setattr(release, "app_property_overrides", lambda _layout, _platform: ("/p:DependencyRoot=test",))
    monkeypatch.setattr(release, "env_override", lambda _name: None)
    package_app_output_root = tmp_path / "state" / "package-build" / "emulebb-v0.7.3-rc.2" / "x64" / "app"
    package_app_intermediate_root = tmp_path / "state" / "package-build" / "emulebb-v0.7.3-rc.2" / "x64" / "app-obj"
    cfg_checks: list[Path] = []

    def fake_verify_app_control_flow_guard(*_args, **kwargs):
        cfg_checks.append(kwargs["binary_path"])

    monkeypatch.setattr(release, "verify_app_control_flow_guard", fake_verify_app_control_flow_guard)

    def fake_invoke_msbuild_project(*_args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(release, "invoke_msbuild_project", fake_invoke_msbuild_project)

    release._build_package_app(
        session,
        app_root,
        package_app_output_root=package_app_output_root,
        package_app_intermediate_root=package_app_intermediate_root,
        clean=True,
    )

    assert captured["project_path"] == app_root / "srchybrid" / "emule.vcxproj"
    assert captured["target"] == "Rebuild"
    assert "/p:DependencyRoot=test" in captured["extra_properties"]
    assert "/p:EnableStartupDiagnostics=false" in captured["extra_properties"]
    assert "/p:EnablePacketDiagnostics=false" in captured["extra_properties"]
    assert "/p:EnableUploadSlotDiagnostics=false" in captured["extra_properties"]
    assert "/p:EnableDownloadSlotDiagnostics=false" in captured["extra_properties"]
    assert "/p:EnableBadPeerDiagnostics=false" in captured["extra_properties"]
    assert "/p:EnableKadDiagnostics=false" in captured["extra_properties"]
    assert f"/p:OutDir={release.with_trailing_separator(package_app_output_root)}" in captured["extra_properties"]
    assert f"/p:IntDir={release.with_trailing_separator(package_app_intermediate_root)}" in captured["extra_properties"]
    assert captured["max_cpu_count"] is None
    assert cfg_checks == [package_app_output_root / "emulebb.exe"]


def test_arm64_package_build_serializes_app_msbuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    session = SimpleNamespace(
        layout=SimpleNamespace(toolset_override_variable="EMULEBB_TEST_TOOLSET"),
        options=SimpleNamespace(configuration="Release", platform="ARM64"),
    )

    monkeypatch.setattr(release, "ensure_app_dependency_artifacts", lambda _layout, _options, *, clean: None)
    monkeypatch.setattr(release, "app_property_overrides", lambda _layout, _platform: ())
    monkeypatch.setattr(release, "env_override", lambda _name: None)
    monkeypatch.setattr(release, "verify_app_control_flow_guard", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(release, "invoke_msbuild_project", lambda *_args, **kwargs: captured.update(kwargs))

    release._build_package_app(
        session,
        tmp_path / "app",
        package_app_output_root=tmp_path / "out",
        package_app_intermediate_root=tmp_path / "obj",
        clean=True,
    )

    assert captured["max_cpu_count"] == 1


def test_diagnostics_package_build_enables_diagnostic_features(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_root = tmp_path / "app"
    captured: dict[str, object] = {}
    session = SimpleNamespace(
        layout=SimpleNamespace(toolset_override_variable="EMULEBB_TEST_TOOLSET"),
        options=SimpleNamespace(configuration="Release", platform="x64"),
    )

    monkeypatch.setattr(release, "ensure_app_dependency_artifacts", lambda _layout, _options, *, clean: None)
    monkeypatch.setattr(release, "app_property_overrides", lambda _layout, _platform: ())
    monkeypatch.setattr(release, "env_override", lambda _name: None)
    monkeypatch.setattr(release, "verify_app_control_flow_guard", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(release, "invoke_msbuild_project", lambda *_args, **kwargs: captured.update(kwargs))

    release._build_package_app(
        session,
        app_root,
        flavor=release.RELEASE_PACKAGE_FLAVORS[1],
        package_app_output_root=tmp_path / "out",
        package_app_intermediate_root=tmp_path / "obj",
        clean=False,
    )

    assert "/p:EnableStartupDiagnostics=true" in captured["extra_properties"]
    assert "/p:EnablePacketDiagnostics=true" in captured["extra_properties"]
    assert "/p:EnableUploadSlotDiagnostics=true" in captured["extra_properties"]
    assert "/p:EnableDownloadSlotDiagnostics=true" in captured["extra_properties"]
    assert "/p:EnableBadPeerDiagnostics=true" in captured["extra_properties"]
    assert "/p:EnableKadDiagnostics=true" in captured["extra_properties"]
    assert "/p:TargetName=emulebb-diagnostics" in captured["extra_properties"]
    assert captured["step_name"] == "APP main diagnostics package binary"


def test_release_package_rejects_startup_diagnostics_binary_marker(tmp_path: Path) -> None:
    exe_path = tmp_path / "emulebb.exe"
    exe_path.write_bytes(_pe_payload(0x8664) + "emulebb-diagnostics-startup.trace.json".encode("utf-16le"))

    with pytest.raises(RuntimeError, match="startup diagnostics support"):
        release._assert_startup_diagnostics_not_compiled(exe_path)


def test_release_package_accepts_binary_without_startup_diagnostics_marker(tmp_path: Path) -> None:
    exe_path = tmp_path / "emulebb.exe"
    exe_path.write_bytes(_pe_payload(0x8664) + b"regular release payload")

    release._assert_startup_diagnostics_not_compiled(exe_path)


def test_release_package_validates_diagnostics_markers(tmp_path: Path) -> None:
    exe_path = tmp_path / "emulebb.exe"
    exe_path.write_bytes(
        _pe_payload(0x8664)
        + "emulebb-diagnostics-startup.trace.json".encode("utf-16le")
        + b"emulebb-diagnostics-packet.log"
        + "emulebb-diagnostics-upload-slot.log".encode("utf-16le")
        + "emulebb-diagnostics-download-slot.log".encode("utf-16le")
        + b"emulebb-diagnostics-bad-peer.log"
        + "emulebb-diagnostics-kad.log".encode("utf-16le")
    )

    release._assert_release_binary_diagnostics(exe_path, release.RELEASE_PACKAGE_FLAVORS[1])


def test_standard_release_package_rejects_packet_diagnostics_marker(tmp_path: Path) -> None:
    exe_path = tmp_path / "emulebb.exe"
    exe_path.write_bytes(_pe_payload(0x8664) + b"emulebb-diagnostics-packet.log")

    with pytest.raises(RuntimeError, match="packet diagnostics support"):
        release._assert_release_binary_diagnostics(exe_path, release.RELEASE_PACKAGE_FLAVORS[0])


def test_standard_release_package_rejects_slot_diagnostics_markers(tmp_path: Path) -> None:
    exe_path = tmp_path / "emulebb.exe"
    exe_path.write_bytes(_pe_payload(0x8664) + b"emulebb-diagnostics-upload-slot.log")

    with pytest.raises(RuntimeError, match="upload slot diagnostics support"):
        release._assert_release_binary_diagnostics(exe_path, release.RELEASE_PACKAGE_FLAVORS[0])

    exe_path.write_bytes(_pe_payload(0x8664) + b"emulebb-diagnostics-download-slot.log")

    with pytest.raises(RuntimeError, match="download slot diagnostics support"):
        release._assert_release_binary_diagnostics(exe_path, release.RELEASE_PACKAGE_FLAVORS[0])


def test_standard_release_package_rejects_bad_peer_diagnostics_marker(tmp_path: Path) -> None:
    exe_path = tmp_path / "emulebb.exe"
    exe_path.write_bytes(_pe_payload(0x8664) + b"emulebb-diagnostics-bad-peer.log")

    with pytest.raises(RuntimeError, match="bad-peer diagnostics support"):
        release._assert_release_binary_diagnostics(exe_path, release.RELEASE_PACKAGE_FLAVORS[0])


def test_standard_release_package_rejects_kad_diagnostics_marker(tmp_path: Path) -> None:
    exe_path = tmp_path / "emulebb.exe"
    exe_path.write_bytes(_pe_payload(0x8664) + b"emulebb-diagnostics-kad.log")

    with pytest.raises(RuntimeError, match="Kad diagnostics support"):
        release._assert_release_binary_diagnostics(exe_path, release.RELEASE_PACKAGE_FLAVORS[0])


def test_diagnostics_release_package_requires_slot_diagnostics_markers(tmp_path: Path) -> None:
    exe_path = tmp_path / "emulebb.exe"
    exe_path.write_bytes(
        _pe_payload(0x8664)
        + b"emulebb-diagnostics-packet.log"
        + "emulebb-diagnostics-startup.trace.json".encode("utf-16le")
        + b"emulebb-diagnostics-bad-peer.log"
    )

    with pytest.raises(RuntimeError, match="upload slot diagnostics support"):
        release._assert_release_binary_diagnostics(exe_path, release.RELEASE_PACKAGE_FLAVORS[1])

    exe_path.write_bytes(
        _pe_payload(0x8664)
        + b"emulebb-diagnostics-packet.log"
        + "emulebb-diagnostics-startup.trace.json".encode("utf-16le")
        + b"emulebb-diagnostics-upload-slot.log"
        + b"emulebb-diagnostics-bad-peer.log"
    )

    with pytest.raises(RuntimeError, match="download slot diagnostics support"):
        release._assert_release_binary_diagnostics(exe_path, release.RELEASE_PACKAGE_FLAVORS[1])


def test_diagnostics_release_package_requires_bad_peer_diagnostics_marker(tmp_path: Path) -> None:
    exe_path = tmp_path / "emulebb.exe"
    exe_path.write_bytes(
        _pe_payload(0x8664)
        + b"emulebb-diagnostics-packet.log"
        + "emulebb-diagnostics-startup.trace.json".encode("utf-16le")
        + b"emulebb-diagnostics-upload-slot.log"
        + b"emulebb-diagnostics-download-slot.log"
    )

    with pytest.raises(RuntimeError, match="bad-peer diagnostics support"):
        release._assert_release_binary_diagnostics(exe_path, release.RELEASE_PACKAGE_FLAVORS[1])


def test_diagnostics_release_package_requires_kad_diagnostics_marker(tmp_path: Path) -> None:
    exe_path = tmp_path / "emulebb.exe"
    exe_path.write_bytes(
        _pe_payload(0x8664)
        + b"emulebb-diagnostics-packet.log"
        + "emulebb-diagnostics-startup.trace.json".encode("utf-16le")
        + b"emulebb-diagnostics-upload-slot.log"
        + b"emulebb-diagnostics-download-slot.log"
        + b"emulebb-diagnostics-bad-peer.log"
    )

    with pytest.raises(RuntimeError, match="Kad diagnostics support"):
        release._assert_release_binary_diagnostics(exe_path, release.RELEASE_PACKAGE_FLAVORS[1])


def test_package_language_resources_rebuild_serializes_msbuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_root = tmp_path / "app"
    language_solution = app_root / "srchybrid" / "lang" / "lang.sln"
    language_solution.parent.mkdir(parents=True)
    language_solution.write_text("solution\n", encoding="utf-8")
    (language_solution.parent / "de_DE.vcxproj").write_text("project\n", encoding="utf-8")
    captured: list[dict[str, object]] = []
    session = SimpleNamespace(
        layout=SimpleNamespace(toolset_override_variable="EMULEBB_TEST_TOOLSET"),
        options=SimpleNamespace(configuration="Release", platform="x64"),
    )

    monkeypatch.setattr(release, "_default_platform_toolset_property", lambda _layout: "/p:PlatformToolset=vTest")

    def fake_invoke_msbuild_project(*_args, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(release, "invoke_msbuild_project", fake_invoke_msbuild_project)

    language_build_root = tmp_path / "output" / "packages" / "build" / "emulebb-v0.7.3-rc.2" / "x64" / "language"

    release._build_language_resources(session, app_root, clean=True, language_build_root=language_build_root)

    assert len(captured) == 1
    assert captured[0]["project_path"] == language_solution.parent / "de_DE.vcxproj"
    assert captured[0]["configuration"] == "Dynamic"
    assert captured[0]["target"] == "Rebuild"
    assert captured[0]["max_cpu_count"] == 1
    assert f"/p:OutDir={release.with_trailing_separator(language_build_root / 'bin')}" in captured[0]["extra_properties"]
    assert f"/p:IntDir={release.with_trailing_separator(language_build_root / 'obj' / 'de_DE')}" in captured[0]["extra_properties"]
    assert "/p:PostBuildEventUseInBuild=false" in captured[0]["extra_properties"]
    assert (language_build_root / "obj" / "de_DE").is_dir()


def test_release_manifest_records_explicit_source_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_root = tmp_path / "workspaces" / "workspace" / "app" / "emulebb-main"
    build_root = tmp_path / "repos" / "emulebb-build"
    tests_root = tmp_path / "repos" / "emulebb-build-tests"
    tooling_root = tmp_path / "repos" / "emulebb-tooling"
    release_root = tmp_path / "workspaces" / "workspace" / "state" / "release" / "emulebb-v0.7.3-rc.2"
    zip_path = release_root / "emulebb-0.7.3-rc.2-x64.zip"
    for path in (app_root, build_root, tests_root, tooling_root, release_root):
        path.mkdir(parents=True)

    branches = {
        app_root: "main",
        build_root: "main",
        tests_root: "main",
        tooling_root: "main",
    }
    heads = {
        app_root: "app1234",
        build_root: "build12",
        tests_root: "tests12",
        tooling_root: "tools12",
    }
    monkeypatch.setattr(release, "repo_branch", lambda repo: branches[repo])
    monkeypatch.setattr(release, "repo_head", lambda repo: heads[repo])

    manifest = release._build_release_manifest(
        layout=SimpleNamespace(
            build_repo_root=build_root,
            tests_repo_root=tests_root,
            tooling_repo_root=tooling_root,
        ),
        workspace_options=SimpleNamespace(configuration="Release", platform="x64"),
        package_options=SimpleNamespace(release_version="0.7.3-rc.2"),
        app_variant=AppVariant(name="main", path=app_root, branch="main"),
        app_root=app_root,
        zip_path=zip_path,
        release_root=release_root,
        zip_hash="zip-sha",
        sbom_path=release_root / "emulebb-0.7.3-rc.2-x64.sbom.spdx.json",
        sbom_hash="sbom-sha",
        exe_hash="exe-sha",
        expected_language_dlls=("de_DE.dll", "fr_FR.dll"),
        package_file_hashes={"eMuleBB/emulebb.exe": "exe-entry-sha"},
        bootstrapper_asset_path=release_root / "Bootstrap-eMuleBBSuite.ps1",
        bootstrapper_hash_path=release_root / "Bootstrap-eMuleBBSuite.ps1.sha256",
        bootstrapper_hash="bootstrapper-sha",
        suite_scripts_asset_path=release_root / "suite-scripts-0.7.3-rc.2.zip",
        suite_scripts_manifest_path=release_root / "suite-scripts-0.7.3-rc.2.manifest.json",
        suite_scripts_hash="suite-scripts-sha",
        suite_scripts_manifest_hash="suite-scripts-manifest-sha",
        automation_examples_asset_path=release_root / "automation-examples-0.7.3-rc.2.zip",
        automation_examples_manifest_path=release_root / "automation-examples-0.7.3-rc.2.manifest.json",
        automation_examples_hash="automation-examples-sha",
        automation_examples_manifest_hash="automation-examples-manifest-sha",
        signature_policy={"mode": "unsigned", "required": False, "signedFiles": []},
    )

    assert manifest["appVariant"] == "main"
    assert manifest["packageFlavor"] == "standard"
    assert manifest["diagnosticFeatures"] == []
    assert manifest["executableName"] == "emulebb.exe"
    assert manifest["executablePath"] == "eMuleBB/emulebb.exe"
    assert manifest["appBranch"] == "main"
    assert manifest["appCommit"] == "app1234"
    assert manifest["buildBranch"] == "main"
    assert manifest["buildCommit"] == "build12"
    assert manifest["buildTestsBranch"] == "main"
    assert manifest["buildTestsCommit"] == "tests12"
    assert manifest["toolingBranch"] == "main"
    assert manifest["toolingCommit"] == "tools12"
    assert manifest["languageDllCount"] == 2
    assert manifest["languageDlls"] == ["de_DE.dll", "fr_FR.dll"]
    assert manifest["packageFileSha256"] == {"eMuleBB/emulebb.exe": "exe-entry-sha"}
    assert manifest["sbomFormat"] == "SPDX-2.3 JSON"
    assert manifest["sbomPath"] == "emulebb-0.7.3-rc.2-x64.sbom.spdx.json"
    assert manifest["sbomSha256"] == "sbom-sha"
    assert manifest["bootstrapperAsset"] == "Bootstrap-eMuleBBSuite.ps1"
    assert manifest["bootstrapperSha256"] == "bootstrapper-sha"
    assert manifest["bootstrapperSha256Path"] == "Bootstrap-eMuleBBSuite.ps1.sha256"
    assert manifest["suiteScriptsAsset"] == "suite-scripts-0.7.3-rc.2.zip"
    assert manifest["suiteScriptsManifest"] == "suite-scripts-0.7.3-rc.2.manifest.json"
    assert manifest["suiteScriptsSha256"] == "suite-scripts-sha"
    assert manifest["suiteScriptsManifestSha256"] == "suite-scripts-manifest-sha"
    assert manifest["automationExamplesAsset"] == "automation-examples-0.7.3-rc.2.zip"
    assert manifest["automationExamplesManifest"] == "automation-examples-0.7.3-rc.2.manifest.json"
    assert manifest["automationExamplesSha256"] == "automation-examples-sha"
    assert manifest["automationExamplesManifestSha256"] == "automation-examples-manifest-sha"
    assert manifest["signaturePolicy"] == {"mode": "unsigned", "required": False, "signedFiles": []}
    assert "eMuleBB/SBOM.spdx.json" in manifest["includedPaths"]
    assert "eMuleBB/scripts" in manifest["includedPaths"]
    assert "eMuleBB/skins" in manifest["includedPaths"]
    assert "eMuleBB/webserver" not in manifest["includedPaths"]


def test_expected_language_dlls_uses_release_language_manifest(tmp_path: Path) -> None:
    tooling_root = tmp_path / "repos" / "emulebb-tooling"
    manifest_path = tooling_root / "helpers" / "rc-release-languages.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps({"languages": [{"rc": "fr_FR.rc"}, {"rc": "de_DE.rc"}]}) + "\n",
        encoding="utf-8",
    )

    assert release._expected_language_dlls(tooling_root) == ("de_DE.dll", "fr_FR.dll")


def test_copy_language_dlls_excludes_linker_byproducts(tmp_path: Path) -> None:
    source_path = tmp_path / "language" / "bin"
    destination_path = tmp_path / "package" / "lang"
    source_path.mkdir(parents=True)
    (source_path / "de_DE.dll").write_bytes(b"dll")
    (source_path / "de_DE.pdb").write_bytes(b"pdb")

    release._copy_language_dlls(source_path, destination_path, ("de_DE.dll",))

    assert (destination_path / "de_DE.dll").read_bytes() == b"dll"
    assert not (destination_path / "de_DE.pdb").exists()


def test_standalone_bootstrapper_asset_is_hashed_next_to_release(tmp_path: Path) -> None:
    package_root = tmp_path / "staging" / "eMuleBB"
    release_root = tmp_path / "release"
    bootstrapper = package_root / "scripts" / "Bootstrap-eMuleBBSuite.ps1"
    bootstrapper.parent.mkdir(parents=True)
    bootstrapper.write_text(
        "#Requires -Version 5.1\nparam(\n    [string]$Version,\n    [string]$Platform = ''\n)\n",
        encoding="utf-8",
    )
    release_root.mkdir(parents=True)

    asset_path, hash_path, digest = release._write_standalone_bootstrapper_asset(
        package_root=package_root,
        release_root=release_root,
        release_version="0.7.3-rc.2",
    )

    assert asset_path == release_root / "Bootstrap-eMuleBBSuite.ps1"
    assert hash_path == release_root / "Bootstrap-eMuleBBSuite.ps1.sha256"
    assert "[string]$Version = '0.7.3-rc.2'," in asset_path.read_text(encoding="utf-8")
    assert digest == hashlib.sha256(asset_path.read_bytes()).hexdigest()
    assert hash_path.read_text(encoding="ascii") == f"{digest}  Bootstrap-eMuleBBSuite.ps1\n"


def test_suite_scripts_bundle_asset_is_hashed_next_to_release(tmp_path: Path) -> None:
    package_root = tmp_path / "staging" / "eMuleBB"
    release_root = tmp_path / "release"
    for relative_path in (*release.EMULEBB_RUNTIME_SCRIPT_PATHS, *release.EMULEBB_CONFIG_ASSET_PATHS):
        asset_path = package_root / relative_path
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_text("#Requires -Version 5.1\n", encoding="utf-8")
    release_root.mkdir(parents=True)

    asset_path, manifest_path, digest = release._write_suite_scripts_bundle_asset(
        package_root=package_root,
        release_root=release_root,
        release_version="0.7.3-rc.2",
    )

    assert asset_path == release_root / "suite-scripts-0.7.3-rc.2.zip"
    assert manifest_path == release_root / "suite-scripts-0.7.3-rc.2.manifest.json"
    assert digest == hashlib.sha256(asset_path.read_bytes()).hexdigest()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "emulebb.suite-scripts-manifest.v1"
    assert manifest["version"] == "0.7.3-rc.2"
    assert manifest["asset"] == asset_path.name
    assert manifest["sha256"] == digest
    with zipfile.ZipFile(asset_path, "r") as archive:
        entry_names = set(archive.namelist())
    expected_entries = {
        f"eMuleBB/{relative_path}"
        for relative_path in (*release.EMULEBB_RUNTIME_SCRIPT_PATHS, *release.EMULEBB_CONFIG_ASSET_PATHS)
    }
    assert entry_names == expected_entries
    assert {entry["path"] for entry in manifest["entries"]} == expected_entries
    for entry in manifest["entries"]:
        source_path = package_root / entry["path"].removeprefix("eMuleBB/")
        assert entry["sha256"] == hashlib.sha256(source_path.read_bytes()).hexdigest()
        assert entry["bytes"] == source_path.stat().st_size


def test_automation_examples_asset_is_hashed_next_to_release(tmp_path: Path) -> None:
    build_root = tmp_path / "repos" / "emulebb-build"
    release_root = tmp_path / "release"
    source_root = build_root / "emule_workspace" / "release_assets" / release.EMULEBB_AUTOMATION_EXAMPLE_ASSET_ROOT_NAME
    for relative_path in release.EMULEBB_AUTOMATION_EXAMPLE_PATHS:
        asset_path = source_root / relative_path
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_text("#Requires -Version 5.1\n", encoding="utf-8")
    release_root.mkdir(parents=True)

    asset_path, manifest_path, digest = release._write_automation_examples_asset(
        build_repo_root=build_root,
        release_root=release_root,
        release_version="0.7.3-rc.2",
    )

    assert asset_path == release_root / "automation-examples-0.7.3-rc.2.zip"
    assert manifest_path == release_root / "automation-examples-0.7.3-rc.2.manifest.json"
    assert digest == hashlib.sha256(asset_path.read_bytes()).hexdigest()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "emulebb.automation-examples-manifest.v1"
    assert manifest["version"] == "0.7.3-rc.2"
    assert manifest["asset"] == asset_path.name
    assert manifest["sha256"] == digest
    with zipfile.ZipFile(asset_path, "r") as archive:
        entry_names = set(archive.namelist())
    expected_entries = {f"eMuleBB/examples/{relative_path}" for relative_path in release.EMULEBB_AUTOMATION_EXAMPLE_PATHS}
    assert entry_names == expected_entries
    assert {entry["path"] for entry in manifest["entries"]} == expected_entries
    for entry in manifest["entries"]:
        source_path = source_root / entry["path"].removeprefix("eMuleBB/examples/")
        assert entry["sha256"] == hashlib.sha256(source_path.read_bytes()).hexdigest()
        assert entry["bytes"] == source_path.stat().st_size


def test_standalone_bootstrapper_asset_bakes_release_version(tmp_path: Path) -> None:
    package_root = tmp_path / "staging" / "eMuleBB"
    release_root = tmp_path / "release"
    bootstrapper = package_root / "scripts" / "Bootstrap-eMuleBBSuite.ps1"
    bootstrapper.parent.mkdir(parents=True)
    bootstrapper.write_text(
        "#Requires -Version 5.1\n"
        "param(\n"
        "    [string]$Version,\n"
        "    [ValidateSet('', 'x64', 'ARM64')]\n"
        "    [string]$Platform = ''\n"
        ")\n",
        encoding="utf-8",
    )
    release_root.mkdir(parents=True)

    asset_path, _hash_path, _digest = release._write_standalone_bootstrapper_asset(
        package_root=package_root,
        release_root=release_root,
        release_version="0.7.3-rc.2",
    )

    text = asset_path.read_text(encoding="utf-8")
    assert "[string]$Version = '0.7.3-rc.2'," in text
    assert "[string]$Platform = ''" in text


def test_release_signing_required_rejects_missing_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMULEBB_RELEASE_SIGN_CERT_SHA1", raising=False)
    monkeypatch.delenv("EMULEBB_RELEASE_SIGN_CERT_PATH", raising=False)

    with pytest.raises(RuntimeError, match="signing is required"):
        release._sign_release_package_files(Path("eMuleBB"), require_signing=True)


def test_release_signing_uses_signtool_for_authenticode_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "staging" / "eMuleBB"
    (package_root / "scripts").mkdir(parents=True)
    (package_root / "lang").mkdir()
    (package_root / "emulebb.exe").write_bytes(b"exe")
    (package_root / "lang" / "de_DE.dll").write_bytes(b"dll")
    (package_root / "scripts" / "Install-eMuleBBSuite.ps1").write_text("#Requires -Version 5.1\n", encoding="utf-8")
    (package_root / "README.md").write_text("readme\n", encoding="utf-8")
    commands: list[list[str]] = []

    monkeypatch.setenv("EMULEBB_RELEASE_SIGN_CERT_SHA1", "0123456789abcdef0123456789abcdef01234567")
    monkeypatch.setenv("EMULEBB_SIGNTOOL", str(tmp_path / "signtool.exe"))
    monkeypatch.setattr(release.subprocess, "run", lambda command, check: commands.append([str(part) for part in command]))

    result = release._sign_release_package_files(package_root, require_signing=True)

    assert result["mode"] == "authenticode"
    assert result["required"] is True
    assert result["signedFiles"] == [
        "eMuleBB/emulebb.exe",
        "eMuleBB/lang/de_DE.dll",
        "eMuleBB/scripts/Install-eMuleBBSuite.ps1",
    ]
    assert len(commands) == 3
    assert all("/fd" in command and "SHA256" in command for command in commands)
    assert all("/sha1" in command and "0123456789abcdef0123456789abcdef01234567" in command for command in commands)


def test_spdx_sbom_describes_staged_package_files_without_self_reference(tmp_path: Path) -> None:
    release_root = tmp_path / "state" / "release" / "emulebb-v0.7.3-rc.2"
    package_root = release_root / "staging" / "x64" / "eMuleBB"
    package_root.mkdir(parents=True)
    (package_root / "emulebb.exe").write_bytes(b"exe")
    (package_root / "SBOM.spdx.json").write_text("old\n", encoding="utf-8")

    document = release._build_spdx_sbom(
        name="test sbom",
        namespace="https://example.invalid/sbom",
        package_name="emulebb-0.7.3-rc.2-x64",
        package_version="0.7.3-rc.2",
        package_license="GPL-2.0-or-later",
        package_comment="test package",
        package_root=package_root,
        release_root=release_root,
        components=[
            release._component_spdx_package(
                name="component",
                declared_license="MIT",
                version="abc123",
                download_location="https://example.invalid/component.git",
            )
        ],
    )

    file_names = {entry["fileName"] for entry in document["files"]}
    assert document["spdxVersion"] == "SPDX-2.3"
    assert document["documentDescribes"] == ["SPDXRef-Package"]
    assert document["packages"][0]["packageVerificationCode"]["packageVerificationCodeValue"]
    assert "eMuleBB/emulebb.exe" in file_names
    assert "eMuleBB/SBOM.spdx.json" not in file_names
    assert any(package["name"] == "component" for package in document["packages"])
    assert any(relationship["relationshipType"] == "DEPENDS_ON" for relationship in document["relationships"])


def test_release_package_contents_require_exact_language_set(tmp_path: Path) -> None:
    zip_path = tmp_path / "package.zip"
    _write_release_zip(zip_path, language_payloads={"de_DE.dll": _pe_payload(0x8664)})

    with pytest.raises(RuntimeError, match="missing language DLLs"):
        release._assert_release_package_contents(zip_path, ("de_DE.dll", "fr_FR.dll"), "x64")


def test_release_package_contents_reject_unexpected_language_dll(tmp_path: Path) -> None:
    zip_path = tmp_path / "package.zip"
    _write_release_zip(
        zip_path,
        language_payloads={"de_DE.dll": _pe_payload(0x8664), "extra.dll": _pe_payload(0x8664)},
    )

    with pytest.raises(RuntimeError, match="unexpected language DLLs"):
        release._assert_release_package_contents(zip_path, ("de_DE.dll",), "x64")


def test_release_package_contents_reject_wrong_architecture(tmp_path: Path) -> None:
    zip_path = tmp_path / "package.zip"
    _write_release_zip(zip_path, language_payloads={"de_DE.dll": _pe_payload(0xAA64)})

    with pytest.raises(RuntimeError, match="PE architecture mismatch"):
        release._assert_release_package_contents(zip_path, ("de_DE.dll",), "x64")


def test_release_package_contents_reject_forbidden_artifacts(tmp_path: Path) -> None:
    zip_path = tmp_path / "package.zip"
    _write_release_zip(zip_path, extra_entries={"eMuleBB/build/emule.pdb": b"symbols"})

    with pytest.raises(RuntimeError, match="build/source artifacts"):
        release._assert_release_package_contents(zip_path, ("de_DE.dll",), "x64")


def test_release_package_contents_reject_legacy_webserver_payload(tmp_path: Path) -> None:
    zip_path = tmp_path / "package.zip"
    _write_release_zip(zip_path, extra_entries={"eMuleBB/webserver/eMule.tmpl": b"template\n"})

    with pytest.raises(RuntimeError, match="legacy webserver payload"):
        release._assert_release_package_contents(zip_path, ("de_DE.dll",), "x64")


def test_release_package_contents_reject_retired_emule_root(tmp_path: Path) -> None:
    zip_path = tmp_path / "package.zip"
    _write_release_zip(zip_path, extra_entries={"eMule/emulebb.exe": _pe_payload(0x8664)})

    with pytest.raises(RuntimeError, match="retired eMule root"):
        release._assert_release_package_contents(zip_path, ("de_DE.dll",), "x64")


def test_diagnostics_release_package_contents_require_named_executable_without_alias(tmp_path: Path) -> None:
    zip_path = tmp_path / "package.zip"
    _write_release_zip(zip_path, executable_name="emulebb-diagnostics.exe")

    release._assert_release_package_contents(
        zip_path,
        ("de_DE.dll",),
        "x64",
        flavor=release.RELEASE_PACKAGE_FLAVORS[1],
    )

    zip_with_alias = tmp_path / "package-with-alias.zip"
    _write_release_zip(
        zip_with_alias,
        executable_name="emulebb-diagnostics.exe",
        extra_entries={"eMuleBB/emulebb.exe": _pe_payload(0x8664)},
    )
    with pytest.raises(RuntimeError, match="compatibility alias"):
        release._assert_release_package_contents(
            zip_with_alias,
            ("de_DE.dll",),
            "x64",
            flavor=release.RELEASE_PACKAGE_FLAVORS[1],
        )


def test_release_package_contents_reject_runtime_script_without_powershell_51_header(tmp_path: Path) -> None:
    zip_path = tmp_path / "package.zip"
    _write_release_zip(
        zip_path,
        extra_entries={"eMuleBB/scripts/Register-Prowlarr.ps1": b"#Requires -Version 7.6\n"},
    )

    with pytest.raises(RuntimeError, match="PowerShell 5.1 compatibility"):
        release._assert_release_package_contents(zip_path, ("de_DE.dll",), "x64")


def test_release_package_contents_require_skin_and_toolbar_assets(tmp_path: Path) -> None:
    zip_path = tmp_path / "package.zip"
    _write_release_zip(zip_path, include_skin_assets=False)

    with pytest.raises(RuntimeError, match="missing required entry"):
        release._assert_release_package_contents(zip_path, ("de_DE.dll",), "x64")


def test_release_skin_assets_are_name_paired_without_source_theme_names() -> None:
    skin_names = {
        Path(relative_path).name.replace(".eMuleSkin.ini", "")
        for relative_path in release.EMULEBB_SKIN_ASSET_PATHS
        if relative_path.endswith(".eMuleSkin.ini")
    }
    toolbar_names = {
        Path(relative_path).name.replace(".eMuleToolbar.kad02.bmp", "")
        for relative_path in release.EMULEBB_SKIN_ASSET_PATHS
        if relative_path.endswith(".eMuleToolbar.kad02.bmp")
    }

    assert skin_names == toolbar_names
    assert len(skin_names) == 8
    forbidden_terms = ("bor" + "land", "mat" + "rix")
    assert not any(term in relative_path.lower() for term in forbidden_terms for relative_path in release.EMULEBB_SKIN_ASSET_PATHS)


def test_release_skin_profiles_define_readable_semantic_colors() -> None:
    required_keys = {
        "SearchResultsLvFg_AvblyBase",
        "SearchResultsLvFg_Downloading",
        "Fg_DownloadStopped",
        "SearchResultsLvFg_Sharing",
        "SearchResultsLvFg_Known",
        "SearchResultsLvFg_Cancelled",
        "SearchResultsLvFg_Incomplete",
        "TransferBarBackground",
        "TransferBarComplete",
        "TransferBarHave",
        "TransferBarMissing",
        "TransferBarPending",
        "TransferBarFileOp",
        "TransferBarPercentFg",
        "TransferBarSourceBase",
        "TransferBarSourceHot",
        "SharedPartsBarBackground",
        "SharedPartsBarMissing",
        "SharedPartsBarUnrequested",
        "SharedPartsBarAvailabilityBase",
        "SharedPartsBarAvailabilityHot",
        "TransferBarPeerBoth",
        "TransferBarPeerOnly",
        "TransferBarPeerActive",
        "TransferBarPeerNext",
        "UploadBarBackground",
        "UploadBarHave",
        "UploadBarSending",
        "UploadBarNext",
        "DetailProgressBackground",
        "DetailProgressStart",
        "DetailProgressEnd",
        "DetailProgressText",
        "ChatStatusFg",
        "ChatSentFg",
        "ChatReceivedFg",
        "ServersLvFg_Connected",
        "ServersLvFg_Failed",
        "ServersLvFg_Warning",
        "TreeGuideFg",
        "TreeBoxFg",
        "TooltipBk",
        "TooltipFg",
    }
    semantic_text_keys = {
        "SearchResultsLvFg_AvblyBase",
        "SearchResultsLvFg_Downloading",
        "Fg_DownloadStopped",
        "SearchResultsLvFg_Sharing",
        "SearchResultsLvFg_Known",
        "SearchResultsLvFg_Cancelled",
        "SearchResultsLvFg_Incomplete",
    }

    assets_root = Path(release.__file__).parent / "release_assets" / "emulebb"
    skin_paths = [
        assets_root / relative_path
        for relative_path in release.EMULEBB_SKIN_ASSET_PATHS
        if relative_path.endswith(".eMuleSkin.ini")
    ]

    for skin_path in skin_paths:
        parser = configparser.ConfigParser()
        parser.optionxform = str
        parser.read(skin_path, encoding="utf-8")
        colors = parser["Colors"]
        assert required_keys <= set(colors.keys()), skin_path.name

        background = _parse_rgb(colors["SearchResultsLvBk"])
        for key in semantic_text_keys:
            assert _contrast_ratio(background, _parse_rgb(colors[key])) >= 3.0, f"{skin_path.name}: {key}"
        for key in ("ChatStatusFg", "ChatSentFg", "ChatReceivedFg"):
            assert _contrast_ratio(_parse_rgb(colors["ChatBk"]), _parse_rgb(colors[key])) >= 3.0, f"{skin_path.name}: {key}"
        for key in ("ServersLvFg_Connected", "ServersLvFg_Failed", "ServersLvFg_Warning"):
            assert _contrast_ratio(_parse_rgb(colors["ServersLvBk"]), _parse_rgb(colors[key])) >= 3.0, f"{skin_path.name}: {key}"
        assert _contrast_ratio(_parse_rgb(colors["TooltipBk"]), _parse_rgb(colors["TooltipFg"])) >= 4.5, skin_path.name


def test_release_package_contents_accept_full_bundle_and_hash_entries(tmp_path: Path) -> None:
    zip_path = tmp_path / "package.zip"
    _write_release_zip(zip_path)

    release._assert_release_package_contents(zip_path, ("de_DE.dll",), "x64")

    hashes = release._zip_entry_hashes(zip_path)
    assert hashes["eMuleBB/README.md"] == hashlib.sha256(b"readme\n").hexdigest()
    assert "eMuleBB/THIRD-PARTY-NOTICES.txt" in hashes
    assert "eMuleBB/SBOM.spdx.json" in hashes
    assert "eMuleBB/scripts/Bootstrap-eMuleBBSuite.ps1" in hashes
    assert "eMuleBB/scripts/Install-eMuleBBSuite.ps1" in hashes
    assert "eMuleBB/scripts/Register-Prowlarr.ps1" in hashes
    assert "eMuleBB/skins/emulebb-slate.eMuleSkin.ini" in hashes
    assert "eMuleBB/skins/emulebb-slate.eMuleToolbar.kad02.bmp" in hashes
    assert "eMuleBB/skins/emulebb-retro-teal.eMuleSkin.ini" in hashes
    assert "eMuleBB/skins/emulebb-retro-teal.eMuleToolbar.kad02.bmp" in hashes


def test_amutorrent_manifest_records_runtime_policy_and_source_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    amutorrent_root = tmp_path / "repos" / "amutorrent"
    build_root = tmp_path / "repos" / "emulebb-build"
    tests_root = tmp_path / "repos" / "emulebb-build-tests"
    tooling_root = tmp_path / "repos" / "emulebb-tooling"
    release_root = tmp_path / "state" / "release" / "emulebb-v0.7.3-rc.2"
    zip_path = release_root / "emulebb-0.7.3-rc.2-amutorrent-arm64.zip"
    for path in (amutorrent_root, build_root, tests_root, tooling_root, release_root):
        path.mkdir(parents=True)
    (amutorrent_root / "fork-delta.json").write_text(
        json.dumps(
            {
                "upstream": {
                    "url": "https://github.com/got3nks/amutorrent.git",
                    "branch": "main",
                    "baseCommit": "24b13e440d39c3c4dc9ed4516d59e304ec1e61f0",
                    "baseVersion": "3.8.5",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    branches = {
        amutorrent_root: "main",
        build_root: "main",
        tests_root: "main",
        tooling_root: "main",
    }
    heads = {
        amutorrent_root: "amut123",
        build_root: "build12",
        tests_root: "tests12",
        tooling_root: "tools12",
    }
    monkeypatch.setattr(release, "repo_branch", lambda repo: branches[repo])
    monkeypatch.setattr(release, "repo_head", lambda repo: heads[repo])

    manifest = release._build_amutorrent_manifest(
        layout=SimpleNamespace(
            build_repo_root=build_root,
            tests_repo_root=tests_root,
            tooling_repo_root=tooling_root,
        ),
        workspace_options=SimpleNamespace(configuration="Release", platform="ARM64"),
        package_options=SimpleNamespace(release_version="0.7.3-rc.2"),
        amutorrent_root=amutorrent_root,
        zip_path=zip_path,
        release_root=release_root,
        zip_hash="zip-sha",
        sbom_path=release_root / "emulebb-0.7.3-rc.2-amutorrent-arm64.sbom.spdx.json",
        sbom_hash="sbom-sha",
        package_file_hashes={"aMuTorrent/server/server.js": "server-sha"},
    )

    assert manifest["package"] == "aMuTorrent optional controller"
    assert manifest["amutorrentBranch"] == "main"
    assert manifest["amutorrentCommit"] == "amut123"
    assert manifest["runtimePolicy"]["minimumPathNodeMajor"] == 24
    assert manifest["runtimePolicy"]["pinnedFallbackNodeVersion"] == "v24.16.0"
    assert manifest["runtimePolicy"]["pinnedFallbackNodeArchive"] == "node-v24.16.0-win-arm64.zip"
    assert manifest["runtimePolicy"]["runnerOwner"] == "eMuleBB suite installer"
    assert manifest["runtimePolicy"]["localAppDataUsed"] is False
    assert manifest["runtimePolicy"]["spacesInInstallPathAllowed"] is False
    assert manifest["upstreamBase"] == {
        "url": "https://github.com/got3nks/amutorrent.git",
        "branch": "main",
        "baseCommit": "24b13e440d39c3c4dc9ed4516d59e304ec1e61f0",
        "baseVersion": "3.8.5",
    }
    assert manifest["packageFileSha256"] == {"aMuTorrent/server/server.js": "server-sha"}
    assert manifest["sbomFormat"] == "SPDX-2.3 JSON"
    assert manifest["sbomPath"] == "emulebb-0.7.3-rc.2-amutorrent-arm64.sbom.spdx.json"
    assert manifest["sbomSha256"] == "sbom-sha"
    assert "aMuTorrent/SBOM.spdx.json" in manifest["includedPaths"]


def test_amutorrent_package_contents_accept_runtime_bundle(tmp_path: Path) -> None:
    zip_path = tmp_path / "amutorrent.zip"
    _write_amutorrent_zip(zip_path)

    release._assert_amutorrent_package_contents(zip_path)

    hashes = release._zip_entry_hashes(zip_path)
    assert hashes["aMuTorrent/README.md"] == hashlib.sha256(b"readme\n").hexdigest()
    assert "aMuTorrent/installer/windows/amutorrent.ps1" not in hashes
    assert "aMuTorrent/SBOM.spdx.json" in hashes


def test_emulebb_rust_package_reuses_staged_regular_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _make_rust_package_layout(tmp_path)
    _stage_rust_package_inputs(layout)
    monkeypatch.setattr(release, "repo_status_lines", lambda _repo: ["## main...origin/main [ahead 1]"])
    monkeypatch.setattr(
        release,
        "_package_repo_provenance",
        lambda repo: {"commit": f"{repo.name}-commit", "branch": "main", "remote": ""},
    )
    options = WorkspaceOptions(
        workspace_root=layout.emule_workspace_root,
        output_root=layout.output_root,
        configuration="Release",
        platform="x64",
    )

    release.create_emulebb_rust_package(
        layout,
        options,
        EmulebbRustPackageOptions(release_version="0.1.0-beta.1", skip_build=True),
    )

    release_root = layout.output_release_root / "rust-v0.1.0-beta.1"
    zip_path = release_root / "emulebb-rust-v0.1.0-beta.1-windows-x64.zip"
    manifest_path = release_root / "emulebb-rust-v0.1.0-beta.1-windows-x64.manifest.json"
    sbom_path = release_root / "emulebb-rust-v0.1.0-beta.1-windows-x64.sbom.spdx.json"
    sums_path = release_root / "SHA256SUMS"
    assert zip_path.is_file()
    assert manifest_path.is_file()
    assert sbom_path.is_file()
    assert sums_path.is_file()
    release._assert_emulebb_rust_package_contents(zip_path)
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    assert "emulebb-rust/emulebb-rust.exe" in names
    assert "emulebb-rust/webui/index.html" in names
    assert "emulebb-rust/webui/assets.js" in names
    assert "emulebb-rust/emulebb-rust-ui.exe" not in names
    assert "emulebb-rust/emulebb-rust-diagnostics.exe" not in names
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "emulebb.rust.package/1"
    assert manifest["version"] == "0.1.0-beta.1"
    assert manifest["tag"] == "rust-v0.1.0-beta.1"
    assert manifest["asset"] == zip_path.name
    assert manifest["executable"] == "emulebb-rust/emulebb-rust.exe"
    assert manifest["webuiRoot"] == "emulebb-rust/webui"
    assert "emulebb-rust/webui/index.html" in manifest["perFileSha256"]
    sums = sums_path.read_text(encoding="ascii").splitlines()
    assert [line.split("  ", 1)[1] for line in sums] == [zip_path.name, manifest_path.name, sbom_path.name]


def test_emulebb_rust_linux_package_emits_deb_appimage_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _make_rust_package_layout(tmp_path)
    _stage_rust_package_inputs(layout)
    staged_bin = layout.output_tools_root / "emulebb-rust" / "bin"
    (staged_bin / "emulebb-rust").write_bytes(b"\x7fELF-linux")
    monkeypatch.setattr(release, "repo_status_lines", lambda _repo: ["## main...origin/main"])
    monkeypatch.setattr(
        release,
        "_package_repo_provenance",
        lambda repo: {"commit": f"{repo.name}-commit", "branch": "main", "remote": ""},
    )
    monkeypatch.setattr(release.shutil, "which", lambda name: "/tools/appimagetool" if name == "appimagetool" else None)

    def fake_packaging_tool(command: tuple[str, ...], _label: str) -> None:
        Path(command[-1]).write_bytes(b"package")

    monkeypatch.setattr(release, "_run_packaging_tool", fake_packaging_tool)
    options = WorkspaceOptions(
        workspace_root=layout.emule_workspace_root,
        output_root=layout.output_root,
        configuration="Release",
        platform="x64",
    )

    release.create_emulebb_rust_package(
        layout,
        options,
        EmulebbRustPackageOptions(
            release_version="0.1.0-beta.1",
            skip_build=True,
            target_os="linux",
        ),
    )

    release_root = layout.output_release_root / "rust-v0.1.0-beta.1"
    deb = release_root / "emulebb-rust-v0.1.0-beta.1-linux-amd64.deb"
    appimage = release_root / "emulebb-rust-v0.1.0-beta.1-linux-x86_64.AppImage"
    sbom = release_root / "emulebb-rust-v0.1.0-beta.1-linux-x86_64.sbom.spdx.json"
    assert deb.is_file()
    assert appimage.is_file()
    assert sbom.is_file()
    assert deb.with_name(f"{deb.name}.manifest.json").is_file()
    assert appimage.with_name(f"{appimage.name}.manifest.json").is_file()
    sums = (release_root / "SHA256SUMS").read_text(encoding="ascii")
    assert deb.name in sums
    assert appimage.name in sums


def test_emulebb_rust_package_contents_reject_dead_ui_and_diagnostics(tmp_path: Path) -> None:
    zip_path = tmp_path / "emulebb-rust.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("emulebb-rust/emulebb-rust.exe", _pe_payload(0x8664))
        archive.writestr("emulebb-rust/webui/index.html", b"<html></html>\n")
        archive.writestr("emulebb-rust/emulebb-rust-settings.example.toml", b"[rest]\n")
        archive.writestr("emulebb-rust/LICENSE", b"license\n")
        archive.writestr("emulebb-rust/README.md", b"readme\n")
        archive.writestr("emulebb-rust/RELEASE-SCOPE.md", b"scope\n")
        archive.writestr("emulebb-rust/SBOM.spdx.json", b'{"spdxVersion":"SPDX-2.3"}\n')
        archive.writestr("emulebb-rust/emulebb-rust-ui.exe", _pe_payload(0x8664))
        archive.writestr("emulebb-rust/emulebb-rust-diagnostics.exe", _pe_payload(0x8664))

    with pytest.raises(RuntimeError, match="forbidden UI/diagnostics artifacts"):
        release._assert_emulebb_rust_package_contents(zip_path)


def test_copy_amutorrent_runtime_uses_output_root_static_bundle(tmp_path: Path) -> None:
    amutorrent_root = tmp_path / "repos" / "amutorrent"
    package_root = tmp_path / "package" / "aMuTorrent"
    static_build_root = tmp_path / "output" / "packages" / "build" / "amutorrent" / "x64" / "static"
    server_module = amutorrent_root / "server" / "node_modules" / "express" / "package.json"
    for path, payload in (
        (amutorrent_root / "server" / "server.js", b"server\n"),
        (amutorrent_root / "server" / "package.json", b"{}\n"),
        (server_module, b"{}\n"),
        (amutorrent_root / "static" / "index.html", b"<script></script>\n"),
        (amutorrent_root / "static" / "dist" / "app.bundle.js", b"stale repo bundle\n"),
        (amutorrent_root / "static" / "output.css", b"stale repo css\n"),
        (static_build_root / "dist" / "app.bundle.js", b"output root bundle\n"),
        (static_build_root / "output.css", b"output root css\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    release._copy_amutorrent_runtime(amutorrent_root, package_root, static_build_root)

    assert (package_root / "static" / "index.html").read_bytes() == b"<script></script>\n"
    assert (package_root / "static" / "dist" / "app.bundle.js").read_bytes() == b"output root bundle\n"
    assert (package_root / "static" / "output.css").read_bytes() == b"output root css\n"


def test_stage_amutorrent_source_excludes_repo_local_generated_outputs(tmp_path: Path) -> None:
    amutorrent_root = tmp_path / "repos" / "amutorrent"
    staged_source_root = tmp_path / "output" / "packages" / "build" / "amutorrent" / "x64" / "source"
    for path, payload in (
        (amutorrent_root / "package.json", b"{}\n"),
        (amutorrent_root / "server" / "server.js", b"server\n"),
        (amutorrent_root / "node_modules" / "react" / "package.json", b"{}\n"),
        (amutorrent_root / "server" / "node_modules" / "express" / "package.json", b"{}\n"),
        (amutorrent_root / "server" / "data" / "config.json", b"{}\n"),
        (amutorrent_root / "static" / "index.html", b"<html></html>\n"),
        (amutorrent_root / "static" / "dist" / "chart.umd.min.js", b"chart\n"),
        (amutorrent_root / "static" / "dist" / "app.bundle.js", b"repo bundle\n"),
        (amutorrent_root / "static" / "output.css", b"repo css\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    release._stage_amutorrent_source(amutorrent_root, staged_source_root)

    assert (staged_source_root / "package.json").is_file()
    assert (staged_source_root / "server" / "server.js").is_file()
    assert (staged_source_root / "static" / "index.html").is_file()
    assert (staged_source_root / "static" / "dist" / "chart.umd.min.js").is_file()
    assert not (staged_source_root / "node_modules").exists()
    assert not (staged_source_root / "server" / "node_modules").exists()
    assert not (staged_source_root / "server" / "data").exists()
    assert not (staged_source_root / "static" / "dist" / "app.bundle.js").exists()
    assert not (staged_source_root / "static" / "output.css").exists()


def test_build_amutorrent_webapp_runs_npm_in_staged_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged_source_root = tmp_path / "output" / "packages" / "build" / "amutorrent" / "x64" / "source"
    static_output_root = tmp_path / "output" / "packages" / "build" / "amutorrent" / "x64" / "static"
    node_bin_dir = tmp_path / "tools" / "node" / "node-v24.16.0-win-x64"
    node_bin_dir.mkdir(parents=True)
    (node_bin_dir / "npm.cmd").write_bytes(b"")
    (staged_source_root / "node_modules").mkdir(parents=True)
    (staged_source_root / "server" / "node_modules").mkdir(parents=True)
    static_output_root.mkdir(parents=True)
    commands: list[tuple[list[str], Path, dict[str, str] | None]] = []

    def fake_run(command, *, cwd, check, env=None):
        assert check is True
        commands.append(([str(part) for part in command], Path(cwd), env))

    monkeypatch.setattr(release.subprocess, "run", fake_run)

    release._build_amutorrent_webapp(
        staged_source_root,
        clean=True,
        static_output_root=static_output_root,
        node_bin_dir=node_bin_dir,
    )

    assert not (staged_source_root / "node_modules").exists()
    assert not (staged_source_root / "server" / "node_modules").exists()
    npm = str(node_bin_dir / "npm.cmd")
    assert commands[0][0] == [npm, "ci"]
    assert commands[0][1] == staged_source_root
    assert commands[1][0] == [npm, "ci", "--prefix", "server", "--omit=dev"]
    assert commands[2][0] == [npm, "run", "build"]
    assert commands[2][1] == staged_source_root
    # All three npm steps run under the pinned Node: its dir is prepended to PATH.
    for _command, _cwd, env in commands:
        assert env is not None
        assert env["PATH"].startswith(str(node_bin_dir))
    assert commands[2][2]["AMUTORRENT_STATIC_OUTPUT_ROOT"] == str(static_output_root)


def test_amutorrent_package_contents_reject_generated_state_and_source_maps(tmp_path: Path) -> None:
    zip_path = tmp_path / "amutorrent.zip"
    _write_amutorrent_zip(
        zip_path,
        extra_entries={
            "aMuTorrent/server/data/config.json": b"{}\n",
            "aMuTorrent/static/dist/app.bundle.js.map": b"{}\n",
        },
    )

    with pytest.raises(RuntimeError, match="forbidden generated or source artifacts"):
        release._assert_amutorrent_package_contents(zip_path)


def test_amutorrent_package_contents_reject_standalone_installer_payload(tmp_path: Path) -> None:
    zip_path = tmp_path / "amutorrent.zip"
    _write_amutorrent_zip(zip_path, extra_entries={"aMuTorrent/installer/windows/amutorrent.ps1": b"#Requires -Version 5.1\n"})

    with pytest.raises(RuntimeError, match="forbidden generated or source artifacts"):
        release._assert_amutorrent_package_contents(zip_path)


def test_ensure_amutorrent_build_node_reuses_cached_pinned_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The build must use the pinned Node deterministically, not the ambient PATH Node.
    archive_name, _ = release.AMUTORRENT_NODE_ARCHIVES["x64"]
    node_dir = tmp_path / "tools" / "node" / archive_name[: -len(".zip")]
    node_dir.mkdir(parents=True)
    (node_dir / "node.exe").write_bytes(b"")
    (node_dir / "npm.cmd").write_bytes(b"")
    layout = SimpleNamespace(output_tools_root=tmp_path / "tools")

    def no_download(*_args, **_kwargs):
        raise AssertionError("must not download when the pinned Node is already cached")

    monkeypatch.setattr(release.urllib.request, "urlopen", no_download)
    pinned = release.AMUTORRENT_NODE_VERSION.lstrip("v")
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=pinned + "\n"),
    )

    assert release._ensure_amutorrent_build_node(layout, "x64") == node_dir


def test_ensure_amutorrent_build_node_rejects_wrong_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A materialized Node that does not report the pinned version (corrupt extract or
    # a cross-arch archive that cannot execute) must fail rather than ship a bad build.
    archive_name, _ = release.AMUTORRENT_NODE_ARCHIVES["x64"]
    node_dir = tmp_path / "tools" / "node" / archive_name[: -len(".zip")]
    node_dir.mkdir(parents=True)
    (node_dir / "node.exe").write_bytes(b"")
    (node_dir / "npm.cmd").write_bytes(b"")
    layout = SimpleNamespace(output_tools_root=tmp_path / "tools")
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="25.8.1\n"),
    )

    with pytest.raises(RuntimeError, match=f"expected {release.AMUTORRENT_NODE_VERSION}"):
        release._ensure_amutorrent_build_node(layout, "x64")
