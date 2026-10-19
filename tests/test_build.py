from __future__ import annotations

from pathlib import Path

import pytest

from emule_workspace import build
from emule_workspace.config import WorkspaceOptions
from emule_workspace.layout import AppVariant, TestTargets as LayoutTestTargets, WorkspaceLayout


def test_build_libs_passes_default_toolset_to_msbuild_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture_build_libs_msbuild_calls(tmp_path, monkeypatch)

    assert captured
    for call in captured:
        assert "/p:PlatformToolset=v143" in call["extra_properties"]


def test_build_libs_honors_toolset_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMULEBB_VS_PLATFORM_TOOLSET", "v145")

    captured = capture_build_libs_msbuild_calls(tmp_path, monkeypatch)

    assert captured
    for call in captured:
        assert "/p:PlatformToolset=v145" in call["extra_properties"]
        assert "/p:PlatformToolset=v143" not in call["extra_properties"]


def test_build_libs_clean_release_x64_removes_generated_dependency_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed: list[tuple[Path, str]] = []

    capture_build_libs_msbuild_calls(tmp_path, monkeypatch, clean=True, removed_generated=removed)

    assert [kind for _repo, kind in removed] == ["zlib", "mbedtls"]


def test_build_libs_serializes_arm64_msbuild_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture_build_libs_msbuild_calls(tmp_path, monkeypatch, platform="ARM64")

    assert captured
    assert {call["max_cpu_count"] for call in captured} == {1}


def test_arm64_crypto_overrides_disable_compiler_multiprocessing(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)

    build.ensure_arm64_override_targets(layout)

    props_text = build.arm64_overrides_props_path(layout).read_text(encoding="utf-8")
    assert "<MultiProcessorCompilation>false</MultiProcessorCompilation>" in props_text
    assert "/DCRYPTOPP_DISABLE_ASM /DCRYPTOPP_NO_CPU_FEATURE_PROBES" in props_text


def test_build_apps_forwards_startup_diagnostics_option(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture_build_apps_msbuild_call(
        tmp_path,
        monkeypatch,
        enable_startup_diagnostics=True,
    )

    assert "/p:EnableStartupDiagnostics=true" in captured["extra_properties"]


def test_build_apps_uses_output_root_dependency_lib_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture_build_apps_msbuild_call(tmp_path, monkeypatch)
    properties = captured["extra_properties"]
    output_root = tmp_path / "emulebb-output" / "builds" / "third_party"

    assert f"/p:CryptoPpLibRoot={build.with_trailing_separator(output_root / 'cryptopp' / 'x64' / '$(Configuration)')}" in properties
    assert f"/p:Id3libLibRoot={build.with_trailing_separator(output_root / 'id3lib' / 'x64' / '$(Configuration)')}" in properties
    assert f"/p:MbedTlsLibRoot={build.with_trailing_separator(output_root / 'mbedtls' / 'x64' / '$(Configuration)')}" in properties
    assert f"/p:MbedTlsCryptoLibRoot={build.with_trailing_separator(output_root / 'mbedtls' / 'x64' / '$(Configuration)')}" in properties
    assert f"/p:MiniUpnpLibRoot={build.with_trailing_separator(output_root / 'miniupnp' / 'x64' / '$(Configuration)')}" in properties
    assert f"/p:PcpNatPmpLibRoot={build.with_trailing_separator(output_root / 'libpcpnatpmp' / 'x64' / '$(Configuration)')}" in properties
    assert f"/p:ResizableLibLibRoot={build.with_trailing_separator(output_root / 'ResizableLib' / 'x64' / '$(Configuration)')}" in properties
    assert f"/p:ZlibLibRoot={build.with_trailing_separator(output_root / 'zlib' / 'x64' / '$(Configuration)')}" in properties


def test_stage_app_dependency_artifacts_copies_native_outputs_to_output_root(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    for _name, source_path, _destination_path in build.staged_dependency_artifact_map(layout, "Release", "x64"):
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(source_path.name.encode("utf-8"))

    build.stage_app_dependency_artifacts(layout, "Release", "x64")

    artifacts = dict(build.app_dependency_artifacts(layout, "Release", "x64"))
    assert artifacts["cryptopp"].read_bytes() == b"cryptlib.lib"
    assert artifacts["id3lib"].read_bytes() == b"id3lib.lib"
    assert artifacts["miniupnp"].read_bytes() == b"miniupnpc.lib"
    assert artifacts["libpcpnatpmp"].read_bytes() == b"pcpnatpmp.lib"
    assert artifacts["ResizableLib"].read_bytes() == b"ResizableLib.lib"
    assert artifacts["zlib"].read_bytes() == b"zlib.lib"
    assert artifacts["mbedtls"].read_bytes() == b"mbedtls.lib"
    assert artifacts["mbedx509"].read_bytes() == b"mbedx509.lib"
    assert artifacts["tfpsacrypto"].read_bytes() == b"tfpsacrypto.lib"


def test_build_apps_honors_startup_diagnostics_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMULEBB_ENABLE_STARTUP_DIAGNOSTICS", "off")

    captured = capture_build_apps_msbuild_call(tmp_path, monkeypatch)

    assert "/p:EnableStartupDiagnostics=false" in captured["extra_properties"]


def test_build_apps_honors_diagnostics_env_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMULEBB_ENABLE_STARTUP_DIAGNOSTICS", "1")
    monkeypatch.setenv("EMULEBB_ENABLE_UPLOAD_SLOT_DIAGNOSTICS", "1")
    monkeypatch.setenv("EMULEBB_ENABLE_DOWNLOAD_SLOT_DIAGNOSTICS", "true")
    monkeypatch.setenv("EMULEBB_ENABLE_BAD_PEER_DIAGNOSTICS", "yes")
    monkeypatch.setenv("EMULEBB_ENABLE_KAD_DIAGNOSTICS", "yes")
    monkeypatch.setenv("EMULEBB_ENABLE_PACKET_DIAGNOSTICS", "on")

    captured = capture_build_apps_msbuild_call(tmp_path, monkeypatch)

    assert "/p:EnableStartupDiagnostics=true" in captured["extra_properties"]
    assert "/p:EnableUploadSlotDiagnostics=true" in captured["extra_properties"]
    assert "/p:EnableDownloadSlotDiagnostics=true" in captured["extra_properties"]
    assert "/p:EnableBadPeerDiagnostics=true" in captured["extra_properties"]
    assert "/p:EnableKadDiagnostics=true" in captured["extra_properties"]
    assert "/p:EnablePacketDiagnostics=true" in captured["extra_properties"]
    assert "/p:TargetName=emulebb-diagnostics" in captured["extra_properties"]
    assert captured["cfg_binary_path"].name == "emulebb-diagnostics.exe"
    assert f"/p:OutDir={build.with_trailing_separator(tmp_path / 'emulebb-output' / 'builds' / 'app' / 'main' / 'x64' / 'Release' / 'diagnostics' / 'bin')}" in captured["extra_properties"]
    assert f"/p:IntDir={build.with_trailing_separator(tmp_path / 'emulebb-output' / 'builds' / 'app' / 'main' / 'x64' / 'Release' / 'diagnostics' / 'obj')}" in captured["extra_properties"]


def test_build_apps_diagnostics_option_enables_all_release_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMULEBB_ENABLE_PACKET_DIAGNOSTICS", "off")

    captured = capture_build_apps_msbuild_call(tmp_path, monkeypatch, enable_diagnostics=True)

    assert "/p:EnableStartupDiagnostics=true" in captured["extra_properties"]
    assert "/p:EnableUploadSlotDiagnostics=true" in captured["extra_properties"]
    assert "/p:EnableDownloadSlotDiagnostics=true" in captured["extra_properties"]
    assert "/p:EnableBadPeerDiagnostics=true" in captured["extra_properties"]
    assert "/p:EnableKadDiagnostics=true" in captured["extra_properties"]
    assert "/p:EnablePacketDiagnostics=true" in captured["extra_properties"]
    assert "/p:EnablePacketDiagnostics=false" not in captured["extra_properties"]
    assert "/p:TargetName=emulebb-diagnostics" in captured["extra_properties"]
    assert captured["cfg_binary_path"].name == "emulebb-diagnostics.exe"
    assert captured["step_name"] == "APP main diagnostics"


def test_build_apps_diagnostics_option_defaults_to_main_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture_build_apps_msbuild_calls(
        tmp_path,
        monkeypatch,
        app_variant_names=(),
        enable_diagnostics=True,
        include_community=True,
    )

    assert [call["step_name"] for call in captured["msbuild_calls"]] == ["APP main diagnostics"]


def test_build_apps_diagnostics_option_rejects_non_main_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="--diagnostics is only supported for the main app variant"):
        capture_build_apps_msbuild_calls(
            tmp_path,
            monkeypatch,
            app_variant_names=("community",),
            enable_diagnostics=True,
            include_community=True,
        )


def test_build_apps_diagnostics_option_requires_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="--diagnostics requires --config Release"):
        capture_build_apps_msbuild_call(
            tmp_path,
            monkeypatch,
            configuration="Debug",
            enable_diagnostics=True,
        )


def test_build_apps_can_disable_packet_diagnostics_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMULEBB_ENABLE_PACKET_DIAGNOSTICS", "off")

    captured = capture_build_apps_msbuild_call(tmp_path, monkeypatch)

    assert "/p:EnablePacketDiagnostics=false" in captured["extra_properties"]
    assert "/p:TargetName=emulebb-diagnostics" not in captured["extra_properties"]


def test_build_apps_keeps_standard_name_without_full_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMULEBB_ENABLE_STARTUP_DIAGNOSTICS", "1")
    monkeypatch.setenv("EMULEBB_ENABLE_PACKET_DIAGNOSTICS", "on")

    captured = capture_build_apps_msbuild_call(tmp_path, monkeypatch)

    assert "/p:TargetName=emulebb-diagnostics" not in captured["extra_properties"]
    assert captured["cfg_binary_path"].name == "emulebb.exe"
    assert f"/p:OutDir={build.with_trailing_separator(tmp_path / 'emulebb-output' / 'builds' / 'app' / 'main' / 'x64' / 'Release' / 'standard' / 'bin')}" in captured["extra_properties"]
    assert f"/p:IntDir={build.with_trailing_separator(tmp_path / 'emulebb-output' / 'builds' / 'app' / 'main' / 'x64' / 'Release' / 'standard' / 'obj')}" in captured["extra_properties"]


def capture_build_libs_msbuild_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    clean: bool = False,
    platform: str = "x64",
    removed_generated: list[tuple[Path, str]] | None = None,
) -> list[dict[str, object]]:
    layout = make_layout(tmp_path)
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(build, "get_cmake_path", lambda: Path("cmake.exe"))
    monkeypatch.setattr(build, "get_perl_path", lambda: Path("perl.exe"))
    monkeypatch.setattr(build, "invoke_cmake_dependency_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(build, "ensure_arm64_override_targets", lambda _layout: None)
    monkeypatch.setattr(build, "stage_app_dependency_artifacts", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(build, "prune_repo_local_dependency_outputs", lambda _layout: None)
    if removed_generated is not None:
        monkeypatch.setattr(
            build,
            "remove_stale_generated_artifacts",
            lambda repo_path, kind: removed_generated.append((repo_path, kind)),
        )

    def fake_invoke_msbuild_project(*_args, **kwargs):
        calls.append(
            {
                "project_path": kwargs["project_path"],
                "extra_properties": tuple(kwargs.get("extra_properties") or ()),
                "step_name": kwargs["step_name"],
                "max_cpu_count": kwargs.get("max_cpu_count"),
            }
        )

    monkeypatch.setattr(build, "invoke_msbuild_project", fake_invoke_msbuild_project)

    build.build_libs(
        layout,
        WorkspaceOptions(workspace_root=layout.emule_workspace_root, configuration="Release", platform=platform),
        clean=clean,
    )

    return calls


def capture_build_apps_msbuild_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enable_startup_diagnostics: bool | None = None,
    enable_diagnostics: bool = False,
    configuration: str = "Release",
) -> dict[str, object]:
    captured = capture_build_apps_msbuild_calls(
        tmp_path,
        monkeypatch,
        app_variant_names=("main",),
        enable_startup_diagnostics=enable_startup_diagnostics,
        enable_diagnostics=enable_diagnostics,
        configuration=configuration,
    )
    assert len(captured["msbuild_calls"]) == 1
    return {
        **captured["msbuild_calls"][0],
        "cfg_binary_path": captured.get("cfg_binary_path"),
    }


def capture_build_apps_msbuild_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    app_variant_names: tuple[str, ...],
    enable_startup_diagnostics: bool | None = None,
    enable_diagnostics: bool = False,
    configuration: str = "Release",
    include_community: bool = False,
) -> dict[str, object]:
    layout = make_layout(tmp_path, app_variants=True, include_community=include_community)
    captured: dict[str, object] = {"msbuild_calls": []}

    monkeypatch.setattr(build, "assert_app_layout", lambda _layout: None)
    monkeypatch.setattr(build, "ensure_app_dependency_artifacts", lambda *_args, **_kwargs: None)

    def fake_verify_app_control_flow_guard(*_args, **kwargs):
        captured["cfg_binary_path"] = kwargs["binary_path"]

    monkeypatch.setattr(build, "verify_app_control_flow_guard", fake_verify_app_control_flow_guard)

    def fake_invoke_msbuild_project(*_args, **kwargs):
        captured["msbuild_calls"].append(
            {
                "project_path": kwargs["project_path"],
                "extra_properties": tuple(kwargs.get("extra_properties") or ()),
                "step_name": kwargs["step_name"],
            }
        )

    monkeypatch.setattr(build, "invoke_msbuild_project", fake_invoke_msbuild_project)

    build.build_apps(
        layout,
        WorkspaceOptions(workspace_root=layout.emule_workspace_root, configuration=configuration, platform="x64"),
        clean=False,
        app_variant_names=app_variant_names,
        enable_startup_diagnostics=enable_startup_diagnostics,
        enable_diagnostics=enable_diagnostics,
    )

    return captured


def make_layout(tmp_path: Path, *, app_variants: bool = False, include_community: bool = False) -> WorkspaceLayout:
    emule_workspace_root = tmp_path / "workspace-root"
    workspace_root = emule_workspace_root / "workspaces" / "workspace"
    app_root = workspace_root / "app" / "emulebb-main"
    variants = ()
    if app_variants:
        variants = (AppVariant(name="main", path=app_root, branch="main"),)
        if include_community:
            variants = (
                *variants,
                AppVariant(
                    name="community",
                    path=workspace_root / "app" / "emulebb-community-baseline",
                    branch="baseline/community-0.72a",
                ),
            )
    return WorkspaceLayout(
        emule_workspace_root=emule_workspace_root,
        workspace_name="workspace",
        workspace_root=workspace_root,
        build_repo_root=emule_workspace_root / "repos" / "emulebb-build",
        tests_repo_root=emule_workspace_root / "repos" / "emulebb-build-tests",
        tooling_repo_root=emule_workspace_root / "repos" / "emulebb-tooling",
        ed2k_server_repo_root=emule_workspace_root / "repos" / "ed2k-server",
        amule_repo_root=emule_workspace_root / "repos" / "amule",
        seed_repo_path=emule_workspace_root / "repos" / "emulebb",
        seed_repo_branch="main",
        dependencies=(),
        app_variants=variants,
        test_targets=LayoutTestTargets(test_build_variant="main", test_run_variant="main", baseline_variant="community"),
        toolset_override_variable="EMULEBB_VS_PLATFORM_TOOLSET",
        output_root=tmp_path / "emulebb-output",
    )


def test_arm64_host_tool_architecture_prefers_native_arm64(monkeypatch: pytest.MonkeyPatch) -> None:
    """A native ARM64 host selects the native ARM64 compiler instead of emulated x64."""

    monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "ARM64")
    assert build.arm64_host_tool_architecture() == "ARM64"


def test_arm64_host_tool_architecture_detects_arm64_under_emulation(monkeypatch: pytest.MonkeyPatch) -> None:
    """An x64-emulated process on ARM64 still resolves the native ARM64 host via ARCHITEW6432."""

    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    monkeypatch.setenv("PROCESSOR_ARCHITEW6432", "ARM64")
    assert build.arm64_host_tool_architecture() == "ARM64"


def test_arm64_host_tool_architecture_uses_x64_on_x64_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """An x64 host cross-compiling ARM64 keeps the existing HostX64 selection."""

    monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    assert build.arm64_host_tool_architecture() == "x64"
