from __future__ import annotations

import subprocess
from pathlib import Path

from emule_workspace.build_state import BuildSession
from emule_workspace.cmake import cmake_generator_arguments, invoke_cmake_dependency_build
from emule_workspace.config import WorkspaceOptions
from emule_workspace.layout import TestTargets as LayoutTestTargets, WorkspaceLayout


def test_cmake_generator_defaults_to_installed_visual_studio(monkeypatch) -> None:
    monkeypatch.delenv("EMULEBB_CMAKE_GENERATOR", raising=False)
    monkeypatch.delenv("EMULEBB_CMAKE_PLATFORM", raising=False)

    assert cmake_generator_arguments("x64") == ("-A", "x64")


def test_cmake_generator_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("EMULEBB_CMAKE_GENERATOR", "Visual Studio 18 2026")
    monkeypatch.delenv("EMULEBB_CMAKE_PLATFORM", raising=False)

    assert cmake_generator_arguments("ARM64") == ("-G", "Visual Studio 18 2026", "-A", "ARM64")


def test_cmake_generator_accepts_toolset_for_implicit_visual_studio(monkeypatch) -> None:
    monkeypatch.delenv("EMULEBB_CMAKE_GENERATOR", raising=False)
    monkeypatch.delenv("EMULEBB_CMAKE_PLATFORM", raising=False)

    assert cmake_generator_arguments("x64", toolset="v145") == ("-A", "x64", "-T", "v145")


def test_cmake_generator_accepts_toolset_for_explicit_visual_studio(monkeypatch) -> None:
    monkeypatch.setenv("EMULEBB_CMAKE_GENERATOR", "Visual Studio 18 2026")
    monkeypatch.delenv("EMULEBB_CMAKE_PLATFORM", raising=False)

    assert cmake_generator_arguments("x64", toolset="v145") == (
        "-G",
        "Visual Studio 18 2026",
        "-A",
        "x64",
        "-T",
        "v145",
    )


def test_cmake_generator_platform_can_be_suppressed(monkeypatch) -> None:
    monkeypatch.setenv("EMULEBB_CMAKE_GENERATOR", "Ninja")
    monkeypatch.setenv("EMULEBB_CMAKE_PLATFORM", "")

    assert cmake_generator_arguments("x64") == ("-G", "Ninja")


def test_cmake_generator_suppresses_toolset_for_non_visual_studio_generator(monkeypatch) -> None:
    monkeypatch.setenv("EMULEBB_CMAKE_GENERATOR", "Ninja")
    monkeypatch.setenv("EMULEBB_CMAKE_PLATFORM", "")

    assert cmake_generator_arguments("x64", toolset="v145") == ("-G", "Ninja")


def test_cmake_dependency_build_uses_workspace_toolset_override(tmp_path: Path, monkeypatch) -> None:
    layout = make_layout(tmp_path)
    source_directory = layout.emule_workspace_root / "repos" / "third_party" / "emulebb-libpcpnatpmp"
    build_directory = source_directory / "cmake-build-x64"
    commands: list[list[str]] = []

    monkeypatch.setenv("EMULEBB_VS_PLATFORM_TOOLSET", "v145")
    monkeypatch.delenv("EMULEBB_CMAKE_GENERATOR", raising=False)
    monkeypatch.delenv("EMULEBB_CMAKE_PLATFORM", raising=False)
    monkeypatch.setattr("emule_workspace.cmake.get_cmake_path", lambda: Path("cmake.exe"))

    def fake_run(command, **_kwargs):
        commands.append([str(part) for part in command])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("emule_workspace.cmake.subprocess.run", fake_run)

    invoke_cmake_dependency_build(
        BuildSession(
            layout=layout,
            options=WorkspaceOptions(workspace_root=layout.emule_workspace_root, configuration="Release", platform="x64"),
            command_name="build libs",
        ),
        source_directory=source_directory,
        build_directory=build_directory,
        step_name="DEP libpcpnatpmp",
        target_name="pcpnatpmp",
    )

    assert commands
    assert commands[0] == [
        "cmake.exe",
        "-S",
        str(source_directory),
        "-B",
        str(build_directory),
        "-A",
        "x64",
        "-T",
        "v145",
        "-DBUILD_SHARED_LIBS=OFF",
    ]


def make_layout(tmp_path: Path) -> WorkspaceLayout:
    emule_workspace_root = tmp_path / "workspace-root"
    workspace_root = emule_workspace_root / "workspaces" / "workspace"
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
        app_variants=(),
        test_targets=LayoutTestTargets(test_build_variant="main", test_run_variant="main", baseline_variant="community"),
        toolset_override_variable="EMULEBB_VS_PLATFORM_TOOLSET",
        output_root=emule_workspace_root / "output",
    )
