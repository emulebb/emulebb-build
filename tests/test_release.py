from __future__ import annotations

import configparser
import hashlib
import json
import subprocess
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
        "0.1.0-beta.2.nightly.20261003.g9e43ea5e",
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
        "0.1.0-beta.2.nightly.20261003.9e43ea5e",
        "0.1.0-beta.2.nightly.2026103.g9e43ea5e",
        "0.7.3-alpha.1",
    ],
)
def test_release_version_rejects_unknown_formats(release_version: str) -> None:
    assert not release._is_release_version(release_version)


def test_emulebb_rust_version_accepts_nightly_derived_from_cargo_base(tmp_path: Path) -> None:
    rust_root = tmp_path / "emulebb-rust"
    rust_root.mkdir()
    (rust_root / "Cargo.toml").write_text(
        '[workspace.package]\nversion = "0.1.0-beta.2"\n',
        encoding="utf-8",
    )

    release._assert_emulebb_rust_version_matches(
        rust_root,
        "0.1.0-beta.2.nightly.20261003.g9e43ea5e",
    )


def test_emulebb_rust_version_rejects_nightly_from_another_beta(tmp_path: Path) -> None:
    rust_root = tmp_path / "emulebb-rust"
    rust_root.mkdir()
    (rust_root / "Cargo.toml").write_text(
        '[workspace.package]\nversion = "0.1.0-beta.2"\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Nightly packages must use"):
        release._assert_emulebb_rust_version_matches(
            rust_root,
            "0.1.0-beta.3.nightly.20261003.g9e43ea5e",
        )


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
        ed2k_server_repo_root=workspace_root / "repos" / "ed2k-server",
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
    for repo in (
        rust_root,
        layout.build_repo_root,
        layout.tooling_repo_root,
        layout.emule_workspace_root / "repos" / "third_party" / "emulebb-miniupnp",
        layout.emule_workspace_root / "repos" / "third_party" / "emulebb-libpcpnatpmp",
    ):
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


@pytest.mark.parametrize(("platform", "machine"), (("x64", 0x8664), ("ARM64", 0xAA64)))
def test_emulebb_rust_package_reuses_staged_regular_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    machine: int,
) -> None:
    layout = _make_rust_package_layout(tmp_path)
    _stage_rust_package_inputs(layout)
    staged_bin = layout.output_tools_root / "emulebb-rust" / "bin"
    (staged_bin / "emulebb-rust.exe").write_bytes(_pe_payload(machine))
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
        platform=platform,
    )

    release.create_emulebb_rust_package(
        layout,
        options,
        EmulebbRustPackageOptions(release_version="0.1.0-beta.1", skip_build=True),
    )

    release_root = layout.output_release_root / "rust-v0.1.0-beta.1"
    zip_path = release_root / f"emulebb-rust-v0.1.0-beta.1-windows-{platform.lower()}.zip"
    manifest_path = release_root / f"emulebb-rust-v0.1.0-beta.1-windows-{platform.lower()}.manifest.json"
    sbom_path = release_root / f"emulebb-rust-v0.1.0-beta.1-windows-{platform.lower()}.sbom.spdx.json"
    sums_path = release_root / "SHA256SUMS"
    assert zip_path.is_file()
    assert manifest_path.is_file()
    assert sbom_path.is_file()
    assert sums_path.is_file()
    release._assert_emulebb_rust_package_contents(zip_path, platform)
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
    assert manifest["baseVersion"] == "0.1.0-beta.1"
    assert manifest["tag"] == "rust-v0.1.0-beta.1"
    assert manifest["asset"] == zip_path.name
    assert manifest["executable"] == "emulebb-rust/emulebb-rust.exe"
    assert manifest["webuiRoot"] == "emulebb-rust/webui"
    assert "emulebb-rust/webui/index.html" in manifest["perFileSha256"]
    assert "emulebbMiniupnp" in manifest["source"]
    assert "emulebbLibpcpnatpmp" in manifest["source"]
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    package_names = {package["name"] for package in sbom["packages"]}
    assert "eMuleBB MiniUPnP fork" in package_names
    assert "eMuleBB libpcpnatpmp fork" in package_names
    sums = sums_path.read_text(encoding="ascii").splitlines()
    assert [line.split("  ", 1)[1] for line in sums] == [zip_path.name, manifest_path.name, sbom_path.name]


@pytest.mark.parametrize(
    ("platform", "deb_arch", "image_arch"),
    (("x64", "amd64", "x86_64"), ("ARM64", "arm64", "aarch64")),
)
def test_emulebb_rust_linux_package_emits_deb_appimage_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    deb_arch: str,
    image_arch: str,
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
    monkeypatch.setattr(
        release,
        "_build_emulebb_rust_deb",
        lambda _source, destination: destination.write_bytes(b"package"),
    )
    options = WorkspaceOptions(
        workspace_root=layout.emule_workspace_root,
        output_root=layout.output_root,
        configuration="Release",
        platform=platform,
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
    deb = release_root / f"emulebb-rust-v0.1.0-beta.1-linux-{deb_arch}.deb"
    appimage = release_root / f"emulebb-rust-v0.1.0-beta.1-linux-{image_arch}.AppImage"
    sbom = release_root / f"emulebb-rust-v0.1.0-beta.1-linux-{image_arch}.sbom.spdx.json"
    assert deb.is_file()
    assert appimage.is_file()
    assert sbom.is_file()
    assert deb.with_name(f"{deb.name}.manifest.json").is_file()
    assert appimage.with_name(f"{appimage.name}.manifest.json").is_file()
    sums = (release_root / "SHA256SUMS").read_text(encoding="ascii")
    assert deb.name in sums
    assert appimage.name in sums


def test_emulebb_rust_linux_deb_only_does_not_require_appimagetool(
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
    monkeypatch.setattr(release.shutil, "which", lambda _name: None)

    def fake_packaging_tool(command: tuple[str, ...], _label: str) -> None:
        Path(command[-1]).write_bytes(b"package")

    monkeypatch.setattr(release, "_run_packaging_tool", fake_packaging_tool)
    monkeypatch.setattr(
        release,
        "_build_emulebb_rust_deb",
        lambda _source, destination: destination.write_bytes(b"package"),
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
        EmulebbRustPackageOptions(
            release_version="0.1.0-beta.1",
            skip_build=True,
            target_os="linux",
            linux_formats=("deb",),
        ),
    )

    release_root = layout.output_release_root / "rust-v0.1.0-beta.1"
    deb = release_root / "emulebb-rust-v0.1.0-beta.1-linux-amd64.deb"
    appimage = release_root / "emulebb-rust-v0.1.0-beta.1-linux-x86_64.AppImage"
    assert deb.is_file()
    assert deb.with_name(f"{deb.name}.manifest.json").is_file()
    assert not appimage.exists()


def test_emulebb_rust_deb_normalizes_modes_in_wsl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(release.os, "name", "nt")
    monkeypatch.setattr(
        release,
        "wsl_path",
        lambda path: f"/mnt/c/{path.name}",
    )
    monkeypatch.setattr(
        release,
        "_run_packaging_tool",
        lambda command, _label: commands.append(command),
    )

    release._build_emulebb_rust_deb(tmp_path / "source", tmp_path / "output.deb")

    assert commands[0][:4] == ("wsl.exe", "--", "bash", "-lc")
    assert "/tmp/emulebb-rust-deb-" in commands[0][4]
    assert "chmod 0755" in commands[0][4]
    assert "dpkg-deb --build --root-owner-group" in commands[0][4]


@pytest.mark.parametrize("platform", ("x64", "ARM64"))
def test_emulebb_rust_macos_package_emits_app_dmg_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    layout = _make_rust_package_layout(tmp_path)
    _stage_rust_package_inputs(layout)
    staged_bin = layout.output_tools_root / "emulebb-rust" / "bin"
    (staged_bin / "emulebb-rust").write_bytes(b"macos-binary")
    monkeypatch.setattr(release.sys, "platform", "darwin")
    monkeypatch.setattr(release, "repo_status_lines", lambda _repo: ["## main...origin/main"])
    monkeypatch.setattr(
        release,
        "_package_repo_provenance",
        lambda repo: {"commit": f"{repo.name}-commit", "branch": "main", "remote": ""},
    )

    def fake_packaging_tool(command: tuple[str, ...], _label: str) -> None:
        Path(command[-1]).write_bytes(b"dmg")

    monkeypatch.setattr(release, "_run_packaging_tool", fake_packaging_tool)
    options = WorkspaceOptions(
        workspace_root=layout.emule_workspace_root,
        output_root=layout.output_root,
        configuration="Release",
        platform=platform,
    )
    release.create_emulebb_rust_package(
        layout,
        options,
        EmulebbRustPackageOptions(release_version="0.1.0-beta.1", skip_build=True, target_os="macos"),
    )
    release_root = layout.output_release_root / "rust-v0.1.0-beta.1"
    dmg = release_root / f"emulebb-rust-v0.1.0-beta.1-macos-{platform.lower()}.dmg"
    app_root = release_root / "staging" / f"macos-{platform.lower()}" / "eMuleBB Rust.app"
    assert dmg.read_bytes() == b"dmg"
    assert (app_root / "Contents" / "Info.plist").is_file()
    assert (app_root / "Contents" / "MacOS" / "webui" / "index.html").is_file()
    assert (app_root / "Contents" / "MacOS" / "launch").is_file()
    assert dmg.with_name(f"{dmg.name}.manifest.json").is_file()
    assert dmg.name in (release_root / "SHA256SUMS").read_text(encoding="ascii")


def test_emulebb_rust_release_assembly_requires_all_native_assets(tmp_path: Path) -> None:
    version = "0.1.0-beta.1"
    stem = f"emulebb-rust-v{version}"
    names = (
        f"{stem}-windows-x64.zip",
        f"{stem}-windows-arm64.zip",
        f"{stem}-linux-amd64.deb",
        f"{stem}-linux-x86_64.AppImage",
        f"{stem}-linux-arm64.deb",
        f"{stem}-linux-aarch64.AppImage",
        f"{stem}-macos-x64.dmg",
        f"{stem}-macos-arm64.dmg",
    )
    for name in names:
        payload = tmp_path / name
        payload.write_bytes(name.encode("ascii"))
        sbom = tmp_path / f"{name}.sbom.spdx.json"
        sbom.write_text("{}\n", encoding="utf-8")
        (tmp_path / f"{name}.manifest.json").write_text(
            json.dumps({
                "asset": name,
                "sha256": release._sha256(payload),
                "sbom": sbom.name,
                "sbomSha256": release._sha256(sbom),
            }),
            encoding="utf-8",
        )
    sums = release.assemble_emulebb_rust_release_assets(tmp_path, version)
    assert len(sums.read_text(encoding="ascii").splitlines()) == 24
    (tmp_path / names[-1]).unlink()
    with pytest.raises(RuntimeError, match="missing"):
        release.assemble_emulebb_rust_release_assets(tmp_path, version)


def test_emulebb_rust_image_context_stages_only_verified_debs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rust_root = tmp_path / "workspace" / "repos" / "emulebb-rust"
    docker_root = rust_root / "packaging" / "docker"
    docker_root.mkdir(parents=True)
    (docker_root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    assets = tmp_path / "output" / "release" / "rust-v0.1.0-beta.1"
    assets.mkdir(parents=True)
    for arch in ("amd64", "arm64"):
        (assets / f"emulebb-rust-v0.1.0-beta.1-linux-{arch}.deb").write_bytes(arch.encode("ascii"))
    monkeypatch.setattr(release, "assemble_emulebb_rust_release_assets", lambda *_args: assets / "SHA256SUMS")
    commands: list[list[str]] = []
    monkeypatch.setattr(release.subprocess, "run", lambda command, **_kwargs: commands.append(command))
    archive = release.build_emulebb_rust_image_ci(
        rust_root=rust_root,
        output_root=tmp_path / "output",
        assets_dir=assets,
        version="0.1.0-beta.1",
        push=False,
    )
    assert archive == tmp_path / "output" / "artifacts" / "emulebb-rust-v0.1.0-beta.1-multiarch.oci.tar"
    context = tmp_path / "output" / "packages" / "build" / "emulebb-rust-image"
    assert (context / "dist" / "emulebb-rust-v0.1.0-beta.1-linux-arm64.deb").read_bytes() == b"arm64"
    assert commands[0][:3] == ["docker", "buildx", "build"]
    assert "--push" not in commands[0]


def test_emulebb_rust_image_can_publish_versioned_and_nightly_tags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rust_root = tmp_path / "workspace" / "repos" / "emulebb-rust"
    docker_root = rust_root / "packaging" / "docker"
    docker_root.mkdir(parents=True)
    (docker_root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    version = "0.1.0-beta.2.nightly.20261003.g9e43ea5e"
    assets = tmp_path / "output" / "release" / f"rust-v{version}"
    assets.mkdir(parents=True)
    for arch in ("amd64", "arm64"):
        (assets / f"emulebb-rust-v{version}-linux-{arch}.deb").write_bytes(arch.encode("ascii"))
    monkeypatch.setattr(release, "assemble_emulebb_rust_release_assets", lambda *_args: assets / "SHA256SUMS")
    commands: list[list[str]] = []
    monkeypatch.setattr(release.subprocess, "run", lambda command, **_kwargs: commands.append(command))

    release.build_emulebb_rust_image_ci(
        rust_root=rust_root,
        output_root=tmp_path / "output",
        assets_dir=assets,
        version=version,
        push=True,
        channel_tag="nightly",
    )

    assert commands[0].count("--tag") == 2
    assert f"ghcr.io/emulebb/emulebb-rust:{version}" in commands[0]
    assert "ghcr.io/emulebb/emulebb-rust:nightly" in commands[0]
    assert "--push" in commands[0]


def test_emulebb_rust_image_rejects_invalid_channel_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rust_root = tmp_path / "workspace" / "repos" / "emulebb-rust"
    docker_root = rust_root / "packaging" / "docker"
    docker_root.mkdir(parents=True)
    (docker_root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    assets = tmp_path / "output" / "release" / "rust-v0.1.0-beta.2"
    assets.mkdir(parents=True)
    monkeypatch.setattr(release, "assemble_emulebb_rust_release_assets", lambda *_args: assets / "SHA256SUMS")

    with pytest.raises(RuntimeError, match="Invalid Rust image channel tag"):
        release.build_emulebb_rust_image_ci(
            rust_root=rust_root,
            output_root=tmp_path / "output",
            assets_dir=assets,
            version="0.1.0-beta.2",
            push=True,
            channel_tag="nightly/latest",
        )


def test_emulebb_rust_local_image_loads_verified_amd64_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rust_root = tmp_path / "workspace" / "repos" / "emulebb-rust"
    docker_root = rust_root / "packaging" / "docker"
    docker_root.mkdir(parents=True)
    (docker_root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (rust_root / "Cargo.toml").write_text(
        '[workspace.package]\nversion = "0.1.0-beta.2"\n', encoding="utf-8"
    )
    version = "0.1.0-beta.2.nightly.20261024.g83df81e2"
    assets = tmp_path / "output" / "release" / f"rust-v{version}"
    assets.mkdir(parents=True)
    asset = assets / f"emulebb-rust-v{version}-linux-amd64.deb"
    asset.write_bytes(b"amd64")
    (assets / f"{asset.name}.manifest.json").write_text(
        json.dumps(
            {
                "asset": asset.name,
                "sha256": release._sha256(asset),
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(release.subprocess, "run", run)
    monkeypatch.setattr(
        release,
        "wsl_path",
        lambda _path: "/mnt/c/output/packages/build/emulebb-rust-image-local",
    )

    image = release.build_emulebb_rust_image_local(
        rust_root=rust_root,
        output_root=tmp_path / "output",
        assets_dir=assets,
        version=version,
        image=f"local/emulebb-rust:{version}",
        wsl_distro="Ubuntu",
    )

    assert image == f"local/emulebb-rust:{version}"
    assert commands[0][4:6] == ["docker", "build"]
    assert "TARGETARCH=amd64" in commands[0]
    assert "--tag" in commands[0]
    context = tmp_path / "output" / "packages" / "build" / "emulebb-rust-image-local"
    assert (context / "dist" / asset.name).read_bytes() == b"amd64"


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
