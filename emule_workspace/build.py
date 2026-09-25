"""Dependency and app build orchestration."""

from __future__ import annotations

import os
import shutil
import shlex
import subprocess
import time
import zipfile
from pathlib import Path

from .build_state import BuildSession
from .cmake import (
    cmake_generator_arguments,
    count_warnings,
    invoke_cmake_dependency_build,
    remove_tree_if_present,
    static_msvc_runtime_cmake_arguments,
)
from .config import BuildClientsOptions, WorkspaceOptions
from .git import repo_branch, test_app_branch_allowed
from .layout import WorkspaceLayout, file_token
from .msbuild import env_override, invoke_msbuild_project
from .process import find_tool
from .qbittorrentbb_runtime import stage_qbittorrentbb_runtime
from .toolchain import get_cmake_path, get_dumpbin_path, get_perl_path

APP_EXE_NAME = "emulebb.exe"
DIAGNOSTICS_APP_EXE_NAME = "emulebb-diagnostics.exe"
DIAGNOSTIC_INSTRUMENTATION_ENV_FLAGS = (
    ("EMULEBB_ENABLE_STARTUP_DIAGNOSTICS", "EnableStartupDiagnostics"),
    ("EMULEBB_ENABLE_PACKET_DIAGNOSTICS", "EnablePacketDiagnostics"),
    ("EMULEBB_ENABLE_UPLOAD_SLOT_DIAGNOSTICS", "EnableUploadSlotDiagnostics"),
    ("EMULEBB_ENABLE_DOWNLOAD_SLOT_DIAGNOSTICS", "EnableDownloadSlotDiagnostics"),
    ("EMULEBB_ENABLE_BAD_PEER_DIAGNOSTICS", "EnableBadPeerDiagnostics"),
    ("EMULEBB_ENABLE_KAD_DIAGNOSTICS", "EnableKadDiagnostics"),
)


def package_app_exe_name(package_flavor: str = "standard") -> str:
    """Returns the eMuleBB executable name for a release package flavor."""

    if package_flavor == "standard":
        return APP_EXE_NAME
    if package_flavor == "diagnostics":
        return DIAGNOSTICS_APP_EXE_NAME
    raise RuntimeError(f"Unsupported eMuleBB package flavor: {package_flavor}")


def app_pdb_name_for_exe(executable_name: str) -> str:
    """Returns the PDB file name matching one executable name."""

    return Path(executable_name).with_suffix(".pdb").name


def enabled_from_env_value(value: str) -> bool:
    """Returns whether a workspace boolean environment override is enabled."""

    return value.strip().lower() not in {"0", "false", "no", "off"}


def build_libs(layout: WorkspaceLayout, options: WorkspaceOptions, *, clean: bool) -> None:
    """Builds the workspace-owned third-party dependency set."""

    session = BuildSession(layout=layout, options=options, command_name="build libs", clean=clean)
    try:
        third_party = layout.resolve_workspace_path("repos/third_party")
        target = "Rebuild" if clean else "Build"
        toolset_property = default_platform_toolset_property(layout)
        dependency_max_cpu_count = 1 if options.platform == "ARM64" else None
        if options.platform == "ARM64":
            ensure_arm64_override_targets(layout)

        invoke_msbuild_project(
            session,
            project_path=third_party / "emulebb-cryptopp" / "cryptlib.vcxproj",
            extra_properties=crypto_pp_properties(layout, options.platform),
            environment_overrides=crypto_pp_environment(options.platform),
            target=target,
            step_name="DEP cryptopp",
            max_cpu_count=dependency_max_cpu_count,
        )
        invoke_msbuild_project(
            session,
            project_path=third_party / "emulebb-id3lib" / "libprj" / "id3lib.vcxproj",
            extra_properties=(toolset_property, *id3lib_properties(options.configuration, options.platform)),
            target=target,
            step_name="DEP id3lib",
            max_cpu_count=dependency_max_cpu_count,
        )
        invoke_msbuild_project(
            session,
            project_path=third_party / "emulebb-miniupnp" / "miniupnpc" / "msvc" / "miniupnpc.vcxproj",
            extra_properties=(toolset_property,),
            target=target,
            step_name="DEP miniupnp",
            max_cpu_count=dependency_max_cpu_count,
        )
        if clean:
            remove_tree_if_present(libpcpnatpmp_build_root(layout, options.platform))
        invoke_cmake_dependency_build(
            session,
            source_directory=third_party / "emulebb-libpcpnatpmp",
            build_directory=libpcpnatpmp_build_root(layout, options.platform),
            target_name="pcpnatpmp",
            step_name="DEP libpcpnatpmp",
            configure_arguments=static_msvc_runtime_cmake_arguments(),
        )
        invoke_msbuild_project(
            session,
            project_path=third_party / "emulebb-resizablelib" / "ResizableLib" / "ResizableLib.vcxproj",
            extra_properties=(toolset_property,),
            target=target,
            step_name="DEP ResizableLib",
            max_cpu_count=dependency_max_cpu_count,
        )
        if clean and options.platform == "x64":
            remove_stale_generated_artifacts(third_party / "emulebb-zlib", "zlib")
            remove_stale_generated_artifacts(third_party / "emulebb-mbedtls", "mbedtls")
        invoke_msbuild_project(
            session,
            project_path=third_party / "emulebb-zlib" / "contrib" / "vstudio" / "vc" / "zlib.vcxproj",
            extra_properties=(toolset_property, f"/p:WorkspaceCMakeExe={get_cmake_path()}"),
            target=target,
            step_name="DEP zlib",
            max_cpu_count=dependency_max_cpu_count,
        )
        invoke_msbuild_project(
            session,
            project_path=mbedtls_project_path(layout),
            extra_properties=(
                toolset_property,
                f"/p:WorkspaceCMakeExe={get_cmake_path()}",
                f"/p:WorkspacePerlExe={get_perl_path()}",
            ),
            target=target,
            step_name="DEP mbedtls",
            max_cpu_count=dependency_max_cpu_count,
        )
        stage_app_dependency_artifacts(layout, options.configuration, options.platform)
        prune_repo_local_dependency_outputs(layout)
    finally:
        session.write_recap()


def build_apps(
    layout: WorkspaceLayout,
    options: WorkspaceOptions,
    *,
    clean: bool,
    app_variant_names: tuple[str, ...],
    enable_startup_diagnostics: bool | None = None,
    enable_diagnostics: bool = False,
) -> None:
    """Builds selected managed app variants."""

    if enable_diagnostics and options.configuration != "Release":
        raise RuntimeError("build app --diagnostics requires --config Release.")

    session = BuildSession(layout=layout, options=options, command_name="build app", clean=clean)
    try:
        assert_app_layout(layout)
        selected_variant_names = app_variant_names
        if enable_diagnostics and not selected_variant_names:
            selected_variant_names = ("main",)
        variants = selected_app_variants(layout, selected_variant_names)
        if enable_diagnostics:
            unsupported = [variant.name for variant in variants if variant.name != "main"]
            if unsupported:
                raise RuntimeError(
                    "build app --diagnostics is only supported for the main app variant; "
                    f"unsupported variant(s): {', '.join(unsupported)}."
                )
        ensure_app_dependency_artifacts(layout, options, clean=clean)
        target = "Rebuild" if clean else "Build"
        for variant in variants:
            extra_properties = [*app_property_overrides(layout, options.platform)]
            diagnostics_flags: dict[str, bool] = (
                {property_name: True for _env_name, property_name in DIAGNOSTIC_INSTRUMENTATION_ENV_FLAGS}
                if enable_diagnostics
                else {}
            )
            for env_name, property_name in DIAGNOSTIC_INSTRUMENTATION_ENV_FLAGS:
                if enable_diagnostics:
                    enabled = True
                elif env_name == "EMULEBB_ENABLE_STARTUP_DIAGNOSTICS" and enable_startup_diagnostics is not None:
                    enabled = enable_startup_diagnostics
                elif value := env_override(env_name):
                    enabled = enabled_from_env_value(value)
                else:
                    continue
                diagnostics_flags[property_name] = enabled
                extra_properties.append(f"/p:{property_name}={'true' if enabled else 'false'}")
            local_executable_name = APP_EXE_NAME
            if options.configuration == "Release" and all(
                diagnostics_flags.get(property_name, False)
                for _env_name, property_name in DIAGNOSTIC_INSTRUMENTATION_ENV_FLAGS
            ):
                local_executable_name = DIAGNOSTICS_APP_EXE_NAME
                extra_properties.append(f"/p:TargetName={Path(DIAGNOSTICS_APP_EXE_NAME).stem}")
            step_suffix = " diagnostics" if local_executable_name == DIAGNOSTICS_APP_EXE_NAME else ""
            override = env_override(layout.toolset_override_variable)
            if override:
                extra_properties.append(f"/p:PlatformToolset={override}")
            build_output_root = app_build_output_root(
                layout,
                variant.name,
                options.configuration,
                options.platform,
                executable_name=local_executable_name,
            )
            extra_properties.append(f"/p:OutDir={with_trailing_separator(build_output_root / 'bin')}")
            extra_properties.append(f"/p:IntDir={with_trailing_separator(build_output_root / 'obj')}")
            invoke_msbuild_project(
                session,
                project_path=variant.path / "srchybrid" / "emule.vcxproj",
                extra_properties=extra_properties,
                target=target,
                step_name=f"APP {variant.name}{step_suffix}",
            )
            if variant.name == "main":
                verify_app_control_flow_guard(
                    session,
                    binary_path=app_build_binary_path(
                        layout,
                        variant.name,
                        options.configuration,
                        options.platform,
                        executable_name=local_executable_name,
                    ),
                    step_name=f"APP {variant.name}{step_suffix} CFG",
                )
    finally:
        session.write_recap()


def build_clients(layout: WorkspaceLayout, options: WorkspaceOptions, build_options: BuildClientsOptions) -> None:
    """Builds opt-in third-party P2P clients used by live multi-client tests."""

    session = BuildSession(layout=layout, options=options, command_name="build clients", clean=build_options.clean)
    try:
        selected_clients = tuple(dict.fromkeys(build_options.clients or ("emulebb-rust",)))
        for client in selected_clients:
            if client == "amule":
                build_amule_client(session, clean=build_options.clean)
            elif client == "emulebb-rust":
                build_emulebb_rust_client(
                    session,
                    clean=build_options.clean,
                    diagnostics=build_options.diagnostics,
                    target_os=build_options.target_os,
                )
            elif client == "qbittorrentbb":
                # Static is opt-in (default dynamic dev build); CI sets it.
                static = os.environ.get("EMULEBB_QBT_STATIC", "").strip().lower() in {"1", "true", "yes", "on"}
                build_qbittorrentbb_client(session, clean=build_options.clean, static=static)
            else:
                raise RuntimeError(f"Unsupported client build target: {client}")
    finally:
        session.write_recap()


RUST_CLIENT_TARGETS = {
    "windows": {
        "x64": "x86_64-pc-windows-msvc",
        "ARM64": "aarch64-pc-windows-msvc",
    },
    "linux": {
        "x64": "x86_64-unknown-linux-gnu",
        "ARM64": "aarch64-unknown-linux-gnu",
    },
    "macos": {
        "x64": "x86_64-apple-darwin",
        "ARM64": "aarch64-apple-darwin",
    },
}
STALE_EMULEBB_RUST_UI_ARTIFACTS = (
    "emulebb-rust-ui.exe",
    "emulebb-rust-ui.pdb",
    "emulebb_rust_ui.pdb",
)


def build_emulebb_rust_client(
    session: BuildSession,
    *,
    clean: bool,
    diagnostics: bool = False,
    target_os: str = "windows",
) -> None:
    """Builds and stages the headless Rust eMuleBB client under the output root."""

    repo_root = session.layout.emulebb_rust_repo_root
    if repo_root is None:
        raise RuntimeError("build clients --client emulebb-rust requires repos/emulebb-rust in the workspace manifest.")
    if not repo_root.is_dir():
        raise RuntimeError(f"eMuleBB Rust repo was not found: {repo_root}")
    target = rust_client_target(session.options.platform, target_os=target_os)
    use_wsl = os.name == "nt" and target_os == "linux"
    cargo_path = None if use_wsl else find_tool(("cargo.exe", "cargo"))
    if not use_wsl and cargo_path is None:
        raise RuntimeError("build clients --client emulebb-rust requires Rust cargo on PATH.")
    if clean:
        remove_tree_if_present(staged_emulebb_rust_root(session.layout))

    flavor = "diagnostics" if diagnostics else "release"
    # The diagnostics flavor is a distinct bin target (emulebb-rust-diagnostics)
    # so cargo emits emulebb-rust-diagnostics.exe DIRECTLY — same source, distinct
    # name, mirroring the MFC emulebb.exe vs emulebb-diagnostics.exe split.
    bin_name = "emulebb-rust-diagnostics" if diagnostics else "emulebb-rust"
    log_path = session.log_directory / f"client-emulebb-rust-build-{flavor}-{session.options.platform.lower()}.log"
    cargo_arguments = [
        "build",
        "-p",
        "emulebb-daemon",
        "--bin",
        bin_name,
        "--release",
        "--target",
        target,
    ]
    if diagnostics:
        cargo_arguments.extend(["--features", "packet-diagnostics"])
    source_target_root = session.layout.output_rust_target_root
    if use_wsl:
        source_target_root = session.layout.output_rust_target_root.parent / "target-wsl"
        wsl_workspace = wsl_path(session.layout.emule_workspace_root)
        wsl_output = wsl_path(session.layout.output_root)
        wsl_target = wsl_path(source_target_root)
        wsl_repo = f"{wsl_workspace}/repos/emulebb-rust"
        command = [
            "wsl.exe",
            "--",
            "bash",
            "-lc",
            " && ".join(
                (
                    f"export EMULEBB_WORKSPACE_ROOT={shlex.quote(wsl_workspace)}",
                    f"export EMULEBB_WORKSPACE_OUTPUT_ROOT={shlex.quote(wsl_output)}",
                    f"export CARGO_TARGET_DIR={shlex.quote(wsl_target)}",
                    f"cd {shlex.quote(wsl_repo)}",
                    f"exec cargo {shlex.join(cargo_arguments)}",
                )
            ),
        ]
    else:
        cargo_command = "cargo.exe" if os.name == "nt" else "cargo"
        command = [cargo_command, *cargo_arguments]
    env = subprocess_os_environ()
    env.update({name: str(value) for name, value in session.layout.subprocess_environment().items()})
    started_at = time.monotonic()
    try:
        with log_path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(f"Cargo: {'WSL ~/.cargo/bin/cargo' if use_wsl else cargo_path}\n")
            stream.write(f"Rust target: {target}\n")
            stream.write(f"Diagnostics: {diagnostics}\n")
            stream.write(f"CARGO_TARGET_DIR: {source_target_root}\n")
            stream.write(" ".join(shlex.quote(part) for part in command) + "\n\n")
            completed = subprocess.run(
                command,
                cwd=repo_root,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                env=env,
            )
        if completed.returncode != 0:
            raise RuntimeError(f"eMuleBB Rust client build failed with exit code {completed.returncode}. See {log_path}")
        stage_emulebb_rust_runtime(
            session.layout,
            target,
            diagnostics=diagnostics,
            target_os=target_os,
            source_target_root=source_target_root,
        )
        if use_wsl:
            remove_tree_if_present(session.layout.output_rust_target_root / target)
        build_emulebb_rust_webui(session, repo_root)
        session.add_step(
            name=f"CLIENT eMuleBB Rust{' diagnostics' if diagnostics else ''}",
            succeeded=True,
            log_path=log_path,
            duration_seconds=time.monotonic() - started_at,
            warning_count=0,
        )
    except Exception:
        session.add_step(
            name=f"CLIENT eMuleBB Rust{' diagnostics' if diagnostics else ''}",
            succeeded=False,
            log_path=log_path,
            duration_seconds=time.monotonic() - started_at,
            warning_count=0,
        )
        raise


def build_emulebb_rust_webui(session: BuildSession, repo_root: Path) -> None:
    """Builds and stages the embedded Rust WebUI bundle beside the staged daemon."""

    webui_root = repo_root / "webui"
    if not (webui_root / "package.json").is_file():
        raise RuntimeError(f"eMuleBB Rust WebUI package.json was not found: {webui_root / 'package.json'}")
    npm_candidates = ("npm.cmd", "npm.exe", "npm") if os.name == "nt" else ("npm",)
    npm_path = find_tool(npm_candidates)
    if npm_path is None:
        raise RuntimeError("build clients --client emulebb-rust requires Node npm on PATH to build the WebUI.")

    webui_build_parent = session.layout.output_build_root / "emulebb-rust"
    source_build_root = webui_build_parent / "webui-source"
    build_root = webui_build_parent / "webui-dist"
    remove_tree_if_present(source_build_root)
    remove_tree_if_present(build_root)
    webui_build_parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        webui_root,
        source_build_root,
        ignore=shutil.ignore_patterns("node_modules", "dist", "test-results"),
    )
    log_path = session.log_directory / f"client-emulebb-rust-webui-{session.options.platform.lower()}.log"
    npm_command = str(npm_path)
    commands = (
        [npm_command, "ci", "--ignore-scripts"],
        [npm_command, "run", "build", "--", "--outDir", str(build_root)],
    )
    npm_env = subprocess_os_environ()
    npm_env["NPM_CONFIG_CACHE"] = str(session.layout.output_cache_root / "npm")
    started_at = time.monotonic()
    try:
        with log_path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(f"npm: {npm_path}\n")
            stream.write(f"WebUI source copy: {source_build_root}\n")
            stream.write(f"WebUI output: {build_root}\n")
            for command in commands:
                stream.write(" ".join(shlex.quote(part) for part in command) + "\n\n")
                completed = subprocess.run(
                    command,
                    cwd=source_build_root,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    env=npm_env,
                )
                if completed.returncode != 0:
                    raise RuntimeError(
                        f"eMuleBB Rust WebUI build failed with exit code {completed.returncode}. See {log_path}"
                    )
        if not (build_root / "index.html").is_file():
            raise RuntimeError(f"eMuleBB Rust WebUI build did not produce index.html: {build_root / 'index.html'}")
        staged_webui = staged_emulebb_rust_root(session.layout) / "bin" / "webui"
        remove_tree_if_present(staged_webui)
        shutil.copytree(build_root, staged_webui)
        session.add_step(
            name="CLIENT eMuleBB Rust WebUI",
            succeeded=True,
            log_path=log_path,
            duration_seconds=time.monotonic() - started_at,
            warning_count=0,
        )
    except Exception:
        session.add_step(
            name="CLIENT eMuleBB Rust WebUI",
            succeeded=False,
            log_path=log_path,
            duration_seconds=time.monotonic() - started_at,
            warning_count=0,
        )
        raise


def rust_client_target(platform: str, *, target_os: str = "windows") -> str:
    """Returns the Rust target triple for one workspace package platform and OS."""

    try:
        targets = RUST_CLIENT_TARGETS[target_os]
        return targets[platform]
    except KeyError as error:
        supported = ", ".join(
            f"{os_name}/{platform_name}"
            for os_name, platforms in sorted(RUST_CLIENT_TARGETS.items())
            for platform_name in sorted(platforms)
        )
        raise RuntimeError(
            f"Unsupported eMuleBB Rust client target {target_os}/{platform}; supported: {supported}"
        ) from error


def wsl_path(path: Path) -> str:
    """Translates one Windows path for a persisted WSL orchestration boundary."""

    completed = subprocess.run(
        ["wsl.exe", "--", "wslpath", "-a", "-u", path.resolve().as_posix()],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    translated = completed.stdout.strip()
    if completed.returncode != 0 or not translated.startswith("/"):
        raise RuntimeError(f"Could not translate Windows path for WSL: {path}: {translated}")
    return translated


def rust_pdb_source_names(staged_pdb_name: str) -> tuple[str, ...]:
    """Returns PDB names cargo/rustc may emit for one staged Rust executable."""

    path = Path(staged_pdb_name)
    rustc_name = f"{path.stem.replace('-', '_')}{path.suffix}"
    if rustc_name == staged_pdb_name:
        return (staged_pdb_name,)
    return (staged_pdb_name, rustc_name)


def rust_pdb_stage_names(staged_pdb_name: str) -> tuple[str, ...]:
    """Returns staged PDB aliases needed by humans and Windows symbol loaders."""

    return rust_pdb_source_names(staged_pdb_name)


def remove_rust_target_runtime_artifacts(layout: WorkspaceLayout) -> None:
    """Removes runnable Rust client copies from Cargo target dirs after staging."""

    names = (
        "emulebb-rust",
        "emulebb-rust-diagnostics",
        "emulebb-rust.exe",
        "emulebb-rust.pdb",
        "emulebb_rust.pdb",
        "emulebb-rust-diagnostics.exe",
        "emulebb-rust-diagnostics.pdb",
        "emulebb_rust_diagnostics.pdb",
    )
    cargo_roots = (
        layout.output_rust_target_root,
        layout.output_rust_target_root.parent / "target-wsl",
    )
    release_roots = []
    for cargo_root in cargo_roots:
        release_roots.extend(
            [
                cargo_root / "release",
                *(cargo_root / target / "release" for targets in RUST_CLIENT_TARGETS.values() for target in targets.values()),
            ]
        )
    for release_root in release_roots:
        for name in names:
            path = release_root / name
            if path.exists():
                path.unlink()


def _qbt_vcpkg_toolchain() -> Path:
    """Resolves the vcpkg CMake toolchain file (VCPKG_ROOT, else the local tools dir)."""

    root = os.environ.get("VCPKG_ROOT", "").strip()
    base = Path(root) if root else Path(r"C:\tools\vcpkg")
    return base / "scripts" / "buildsystems" / "vcpkg.cmake"


def _qbt_qt_prefix() -> str:
    """Dynamic-build Qt6 prefix (static builds get Qt from vcpkg instead)."""

    return os.environ.get("EMULEBB_QT_PREFIX", r"C:\tools\Qt\6.8.1\msvc2022_64")


def _qbt_cmake_step(session: BuildSession, args: list[str], *, log_name: str, step_name: str) -> None:
    """Runs one cmake invocation, logging to the session and recording a step."""

    log_path = session.log_directory / log_name
    started_at = time.monotonic()
    try:
        with log_path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(" ".join(shlex.quote(part) for part in args) + "\n\n")
            completed = subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT, text=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"{step_name} failed with exit code {completed.returncode}. See {log_path}")
        session.add_step(
            name=step_name,
            succeeded=True,
            log_path=log_path,
            duration_seconds=time.monotonic() - started_at,
            warning_count=count_warnings(log_path),
        )
    except Exception:
        session.add_step(
            name=step_name,
            succeeded=False,
            log_path=log_path,
            duration_seconds=time.monotonic() - started_at,
            warning_count=count_warnings(log_path),
        )
        raise


def build_qbittorrentbb_client(session: BuildSession, *, clean: bool, static: bool, stage: str = "all") -> None:
    """Builds qBittorrentBB and its emulebb-libtorrent engine under the output root.

    ``static`` selects a fully self-contained build (vcpkg x64-windows-static incl.
    Qt6, static MSVC runtime, static libtorrent) producing a single qbittorrent.exe
    like upstream qBittorrent; otherwise a dynamic dev build (aqt Qt + libtorrent DLL).

    ``stage`` selects which half to build: ``"libtorrent"`` (engine only),
    ``"qbittorrent"`` (app only, reusing a previously installed engine) or ``"all"``.
    Splitting lets CI build the engine -- which needs only boost/openssl/zlib -- and
    surface its errors before installing Qt, instead of waiting for the slow Qt build.
    """

    if stage not in ("all", "libtorrent", "qbittorrent"):
        raise RuntimeError(f"Unsupported build stage: {stage}")
    build_lt = stage in ("all", "libtorrent")
    build_qb = stage in ("all", "qbittorrent")

    layout = session.layout
    # Repo roots are env-overridable so CI (fresh checkouts) and the source tree
    # can point at the forks directly; otherwise fall back to the materialized
    # workspace layout.
    qb_env = os.environ.get("EMULEBB_QBT_REPO", "").strip()
    lt_env = os.environ.get("EMULEBB_LIBTORRENT_REPO", "").strip()
    qb_root = Path(qb_env) if qb_env else layout.resolve_workspace_path("repos/qbittorrentbb")
    lt_root = Path(lt_env) if lt_env else layout.resolve_workspace_path("repos/third_party/emulebb-libtorrent")
    if not qb_root.is_dir():
        raise RuntimeError(f"qbittorrentbb repo not found: {qb_root} (set EMULEBB_QBT_REPO).")
    if not lt_root.is_dir():
        raise RuntimeError(f"emulebb-libtorrent repo not found: {lt_root} (set EMULEBB_LIBTORRENT_REPO).")

    cmake_path = str(get_cmake_path())
    toolchain = _qbt_vcpkg_toolchain()
    if not toolchain.is_file():
        raise RuntimeError(f"vcpkg toolchain not found: {toolchain} (set VCPKG_ROOT).")

    triplet = "x64-windows-static" if static else "x64-windows"
    config = session.options.configuration
    generator = list(
        cmake_generator_arguments(
            session.options.platform,
            toolset=os.environ.get(layout.toolset_override_variable, "").strip(),
        )
    )
    static_args = list(static_msvc_runtime_cmake_arguments()) if static else []

    lt_build = layout.output_third_party_build_root / "emulebb-libtorrent"
    deps_prefix = layout.output_third_party_build_root / "deps" / "libtorrent"
    qb_build = layout.output_build_root / "qbittorrentbb"
    if clean and build_lt:
        remove_tree_if_present(lt_build)
    if clean and build_qb:
        remove_tree_if_present(qb_build)
    lt_build.mkdir(parents=True, exist_ok=True)
    qb_build.mkdir(parents=True, exist_ok=True)
    suffix = f"{'static' if static else 'dynamic'}-{session.options.platform.lower()}"

    common = [
        *generator,
        f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
        f"-DVCPKG_TARGET_TRIPLET={triplet}",
        *static_args,
    ]

    # --- emulebb-libtorrent: configure, build, install to the deps prefix ---
    # Needs only boost/openssl/zlib (not Qt), so CI can run this stage first.
    if build_lt:
        _qbt_cmake_step(
            session,
            [cmake_path, "-S", str(lt_root), "-B", str(lt_build), *common,
             f"-DBUILD_SHARED_LIBS={'OFF' if static else 'ON'}"],
            log_name=f"qbt-libtorrent-configure-{suffix}.log",
            step_name="CLIENT qBittorrentBB libtorrent configure",
        )
        _qbt_cmake_step(
            session,
            [cmake_path, "--build", str(lt_build), "--config", config, "--parallel"],
            log_name=f"qbt-libtorrent-build-{suffix}.log",
            step_name="CLIENT qBittorrentBB libtorrent build",
        )
        _qbt_cmake_step(
            session,
            [cmake_path, "--install", str(lt_build), "--config", config, "--prefix", str(deps_prefix)],
            log_name=f"qbt-libtorrent-install-{suffix}.log",
            step_name="CLIENT qBittorrentBB libtorrent install",
        )

    # --- qbittorrentbb: configure, build (static gets Qt6 from vcpkg) ---
    if build_qb:
        if not deps_prefix.is_dir():
            raise RuntimeError(
                f"libtorrent install prefix not found: {deps_prefix}. "
                "Run the 'libtorrent' stage (or 'all') first."
            )
        prefix_path = [str(deps_prefix)]
        if not static:
            prefix_path.insert(0, _qbt_qt_prefix())
        # Static Qt6 from vcpkg exports Wrap targets (e.g. WrapSystemZLIB) whose
        # link interfaces reference imported targets created by find_package
        # (ZLIB::ZLIB, PNG::PNG, Freetype::Freetype). Those are directory-scoped by
        # default, so they are not visible at generate time from the package scope
        # that defines the Wrap targets, and the configure fails with
        # "target ... not found". Make find_package imported targets GLOBAL
        # (CMake 3.24+) so they resolve across scopes.
        global_targets = ["-DCMAKE_FIND_PACKAGE_TARGETS_GLOBAL=ON"] if static else []
        _qbt_cmake_step(
            session,
            [cmake_path, "-S", str(qb_root), "-B", str(qb_build), *common,
             *global_targets,
             f"-DCMAKE_PREFIX_PATH={';'.join(prefix_path)}"],
            log_name=f"qbt-configure-{suffix}.log",
            step_name="CLIENT qBittorrentBB configure",
        )
        _qbt_cmake_step(
            session,
            [cmake_path, "--build", str(qb_build), "--config", config, "--parallel"],
            log_name=f"qbt-build-{suffix}.log",
            step_name="CLIENT qBittorrentBB build",
        )

        exe = qb_build / config / "qbittorrent.exe"
        if not exe.is_file():
            raise RuntimeError(f"qbittorrentbb build did not produce qbittorrent.exe: {exe}")
        if not static:
            qt_prefix = Path(_qbt_qt_prefix())
            stage_qbittorrentbb_runtime(
                executable=exe,
                target_root=exe.parent,
                qt_prefix=qt_prefix,
                qbt_root=qb_root,
                search_dirs=[
                    exe.parent,
                    qt_prefix / "bin",
                    deps_prefix / "bin",
                    lt_build / config,
                ],
            )


def staged_emulebb_rust_root(layout: WorkspaceLayout) -> Path:
    """Returns the output-root eMuleBB Rust runtime staging root."""

    return layout.output_tools_root / "emulebb-rust"


def stage_emulebb_rust_runtime(
    layout: WorkspaceLayout,
    target: str,
    *,
    diagnostics: bool = False,
    target_os: str = "windows",
    source_target_root: Path | None = None,
) -> None:
    """Stages the built headless Rust client below the output root.

    The diagnostics flavor is built as its own bin target, so cargo emits
    ``emulebb-rust-diagnostics.exe`` directly (mirroring the MFC ``emulebb.exe`` vs
    ``emulebb-diagnostics.exe`` split). Each flavor is staged under its own name, so
    the plain release and diagnostics binaries never collide and consumers can pick
    the build that emits the ``ed2k_packet_v1`` / ``diag_event_v1`` dumps
    unambiguously.
    """

    executable_suffix = ".exe" if target_os == "windows" else ""
    exe_name = ("emulebb-rust-diagnostics" if diagnostics else "emulebb-rust") + executable_suffix
    pdb_name = "emulebb-rust-diagnostics.pdb" if diagnostics else "emulebb-rust.pdb"
    source_root = (source_target_root or layout.output_rust_target_root) / target / "release"
    exe = source_root / exe_name
    if not exe.is_file():
        raise RuntimeError(f"Built eMuleBB Rust client executable was not found: {exe}")
    target_root = staged_emulebb_rust_root(layout)
    bin_root = target_root / "bin"
    bin_root.mkdir(parents=True, exist_ok=True)
    for stale_artifact in STALE_EMULEBB_RUST_UI_ARTIFACTS:
        (bin_root / stale_artifact).unlink(missing_ok=True)
    shutil.copy2(exe, bin_root / exe_name)
    if target_os == "windows":
        staged_pdb_source = None
        for pdb_source_name in rust_pdb_source_names(pdb_name):
            pdb = source_root / pdb_source_name
            if pdb.is_file():
                staged_pdb_source = pdb
                break
        if staged_pdb_source is not None:
            for staged_pdb_name in rust_pdb_stage_names(pdb_name):
                shutil.copy2(staged_pdb_source, bin_root / staged_pdb_name)
    if not (bin_root / exe_name).is_file():
        raise RuntimeError(f"Staged eMuleBB Rust runtime is missing required file: {bin_root / exe_name}")
    remove_rust_target_runtime_artifacts(layout)


AMULE_MSYS2_PACKAGE_SNAPSHOT = (
    "base-devel",
    "git",
    "zip",
    "mingw-w64-x86_64-gcc",
    "mingw-w64-x86_64-cmake",
    "mingw-w64-x86_64-make",
    "mingw-w64-x86_64-pkgconf",
    "mingw-w64-x86_64-wxwidgets3.2-msw",
    "mingw-w64-x86_64-boost",
    "mingw-w64-x86_64-crypto++",
    "mingw-w64-x86_64-pupnp",
    "mingw-w64-x86_64-libmaxminddb",
    "mingw-w64-x86_64-gettext-runtime",
    "mingw-w64-x86_64-gettext-tools",
    "mingw-w64-x86_64-zlib",
    "mingw-w64-x86_64-libpng",
    "mingw-w64-x86_64-libgd",
    "mingw-w64-x86_64-readline",
)


def build_amule_client(session: BuildSession, *, clean: bool) -> None:
    """Builds the aMule Windows portable client and stages daemon tools under the output root."""

    if session.options.platform != "x64":
        raise RuntimeError("build clients --client amule currently supports only --platform x64 via MSYS2 MINGW64.")
    if session.layout.amule_repo_root is None:
        raise RuntimeError(
            "build clients --client amule requires a manual repos/amule checkout; "
            "it is no longer materialized by workspace setup."
        )
    msys2_root = resolve_msys2_root()
    bash = msys2_root / "usr" / "bin" / "bash.exe"
    script_path = session.layout.amule_repo_root / "packaging" / "windows" / "build.sh"
    if not script_path.is_file():
        raise RuntimeError(f"aMule Windows build script was not found: {script_path}")
    if clean:
        for path in (
            amule_build_output_root(session.layout),
            amule_release_output_root(session.layout),
            staged_amule_root(session.layout),
            session.layout.amule_repo_root / "build-windows-x64",
            session.layout.amule_repo_root / "build-windows-x86_64",
            session.layout.amule_repo_root / "amule-portable-x64",
            session.layout.amule_repo_root / "amule-portable-x86_64",
            session.layout.amule_repo_root / "dist",
        ):
            remove_tree_if_present(path)

    log_path = session.log_directory / "client-amule-build-release-x64.log"
    build_command = build_amule_msys2_command(session.layout.amule_repo_root)
    started_at = time.monotonic()
    try:
        with log_path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(f"MSYS2 root: {msys2_root}\n")
            stream.write(f"MSYS2 bash: {bash}\n")
            stream.write(f"MSYSTEM: MINGW64\n")
            stream.write(f"WINDOWS_MSYSTEM: MINGW64\n")
            stream.write(f"{bash} -lc {build_command}\n\n")
            completed = subprocess.run(
                [str(bash), "-lc", build_command],
                cwd=msys2_root,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                env=msys2_mingw64_environment(msys2_root, session.layout),
            )
        if completed.returncode != 0:
            raise RuntimeError(f"aMule Windows build failed with exit code {completed.returncode}. See {log_path}")
        stage_amule_runtime(session.layout)
        session.add_step(
            name="CLIENT aMule",
            succeeded=True,
            log_path=log_path,
            duration_seconds=time.monotonic() - started_at,
            warning_count=0,
        )
    except Exception:
        session.add_step(
            name="CLIENT aMule",
            succeeded=False,
            log_path=log_path,
            duration_seconds=time.monotonic() - started_at,
            warning_count=0,
        )
        raise


def resolve_msys2_root() -> Path:
    """Returns the system MSYS2 root used for aMule Windows builds."""

    import os

    candidates: list[Path] = []
    override = os.environ.get("EMULEBB_MSYS2_ROOT")
    if override:
        candidates.append(Path(override))
    candidates.extend((Path("C:/msys64"), Path("C:/tools/msys64")))
    for candidate in candidates:
        root = candidate.resolve()
        if (root / "usr" / "bin" / "bash.exe").is_file():
            return root
    checked = ", ".join(str(path) for path in candidates)
    raise RuntimeError(
        "build clients --client amule requires system MSYS2. "
        f"Install MSYS2 at C:\\msys64 or set EMULEBB_MSYS2_ROOT. Checked: {checked}"
    )


def msys2_mingw64_environment(msys2_root: Path, layout: WorkspaceLayout | None = None) -> dict[str, str]:
    """Builds the environment used to launch the MSYS2 MINGW64 aMule recipe."""

    env = subprocess_os_environ()
    env["MSYSTEM"] = "MINGW64"
    env["WINDOWS_MSYSTEM"] = "MINGW64"
    env["CHERE_INVOKING"] = "1"
    env["MSYS2_PATH_TYPE"] = "inherit"
    mingw_bin = msys2_root / "mingw64" / "bin"
    usr_bin = msys2_root / "usr" / "bin"
    env["PATH"] = f"{mingw_bin};{usr_bin};{env.get('PATH', '')}"
    if layout is not None:
        env.update({name: str(value) for name, value in layout.subprocess_environment().items()})
    return env


def build_amule_msys2_command(repo_root: Path) -> str:
    """Returns the shell command that runs the aMule Windows recipe inside MINGW64."""

    package_snapshot = " ".join(shlex.quote(package) for package in AMULE_MSYS2_PACKAGE_SNAPSHOT)
    repo = shlex.quote(windows_path_to_msys(repo_root))
    return (
        "set -euo pipefail; "
        "echo '==> MSYS2 probe'; "
        "echo \"MSYSTEM=${MSYSTEM:-unset}\"; "
        "echo \"WINDOWS_MSYSTEM=${WINDOWS_MSYSTEM:-unset}\"; "
        "command -v cmake; cmake --version; "
        "command -v mingw32-make; mingw32-make --version | head -n 1; "
        f"pacman -Q {package_snapshot}; "
        f"cd {repo}; "
        "./packaging/windows/build.sh"
    )


def windows_path_to_msys(path: Path) -> str:
    """Converts an absolute Windows path to the /c/... form accepted by MSYS2 bash."""

    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    if not drive:
        raise RuntimeError(f"Cannot convert path without drive to MSYS2 form: {resolved}")
    parts = [part for part in resolved.parts[1:]]
    return f"/{drive}/" + "/".join(parts)


def subprocess_os_environ() -> dict[str, str]:
    """Returns a mutable environment dictionary without importing `os` at call sites."""

    import os

    return os.environ.copy()


def staged_amule_root(layout: WorkspaceLayout) -> Path:
    """Returns the output-root aMule runtime staging root."""

    return layout.output_tools_root / "amule"


def amule_build_output_root(layout: WorkspaceLayout) -> Path:
    """Returns the output-root aMule build root used by the Windows recipe."""

    return layout.output_build_root / "amule"


def amule_release_output_root(layout: WorkspaceLayout) -> Path:
    """Returns the output-root aMule distribution root used by the Windows recipe."""

    return layout.output_release_root / "amule"


def stage_amule_runtime(layout: WorkspaceLayout) -> None:
    """Stages the latest aMule portable runtime below the output root."""

    source_root = find_amule_portable_root(layout)
    target_root = staged_amule_root(layout)
    remove_tree_if_present(target_root)
    shutil.copytree(source_root, target_root)
    for required in ("bin/amuled.exe", "bin/amulecmd.exe"):
        if not (target_root / required).is_file():
            raise RuntimeError(f"Staged aMule runtime is missing required file: {target_root / required}")


def find_amule_portable_root(layout: WorkspaceLayout) -> Path:
    """Finds or extracts the portable aMule runtime built by the Windows recipe."""

    candidates = sorted(
        (
            path.parent.parent
            for path in amule_build_output_root(layout).glob("**/bin/amuled.exe")
            if "amule-portable" in str(path.parent.parent).lower() or "dist" in str(path.parent.parent).lower()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        if (candidate / "bin" / "amulecmd.exe").is_file():
            return candidate

    zip_candidates = sorted(amule_release_output_root(layout).glob("*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not zip_candidates:
        raise RuntimeError(
            "aMule build did not produce a portable runtime or zip under "
            f"{amule_build_output_root(layout)} or {amule_release_output_root(layout)}"
        )
    extract_root = layout.output_tmp_root / "amule-portable-extracted"
    remove_tree_if_present(extract_root)
    with zipfile.ZipFile(zip_candidates[0]) as archive:
        archive.extractall(extract_root)
    extracted = sorted((path.parent.parent for path in extract_root.glob("**/bin/amuled.exe")), key=lambda path: str(path))
    for candidate in extracted:
        if (candidate / "bin" / "amulecmd.exe").is_file():
            return candidate
    raise RuntimeError(f"aMule zip does not contain bin/amuled.exe and bin/amulecmd.exe: {zip_candidates[0]}")


def selected_app_variants(layout: WorkspaceLayout, names: tuple[str, ...]):
    """Returns selected app variants, defaulting to all materialized variants."""

    if not names:
        return layout.app_variants
    selected = []
    for name in dict.fromkeys(name.strip() for name in names if name.strip()):
        selected.append(layout.get_app_variant(name))
    return tuple(selected)


def assert_app_layout(layout: WorkspaceLayout) -> None:
    """Checks that app worktrees exist and match branch policy."""

    missing = [variant.path for variant in layout.app_variants if not variant.path.exists()]
    if missing:
        raise RuntimeError("Missing app worktrees:\n" + "\n".join(str(path) for path in missing))
    for variant in layout.app_variants:
        current_branch = repo_branch(variant.path)
        if not test_app_branch_allowed(variant.branch, current_branch):
            raise RuntimeError(
                f"App checkout '{variant.path}' is on branch '{current_branch}', expected '{variant.branch}'."
            )


def ensure_app_dependency_artifacts(layout: WorkspaceLayout, options: WorkspaceOptions, *, clean: bool) -> None:
    """Builds dependencies when required app dependency outputs are missing."""

    missing = missing_app_dependency_artifacts(layout, options.configuration, options.platform)
    if not missing:
        return
    print(f"Missing dependency outputs for {options.configuration}|{options.platform}; running build libs.")
    build_libs(layout, options, clean=clean)
    missing = missing_app_dependency_artifacts(layout, options.configuration, options.platform)
    if missing:
        details = "\n".join(f"{name}: {path}" for name, path in missing)
        raise RuntimeError(f"Required dependency outputs are still missing for {options.configuration}|{options.platform}:\n{details}")


def missing_app_dependency_artifacts(layout: WorkspaceLayout, configuration: str, platform: str) -> list[tuple[str, Path]]:
    """Returns missing dependency artifacts required by app builds."""

    return [(name, path) for name, path in app_dependency_artifacts(layout, configuration, platform) if not path.exists()]


def app_dependency_artifacts(layout: WorkspaceLayout, configuration: str, platform: str) -> tuple[tuple[str, Path], ...]:
    """Returns required dependency library outputs for app builds."""

    return (
        ("cryptopp", dependency_library_path(layout, "cryptopp", configuration, platform, "cryptlib.lib")),
        ("id3lib", dependency_library_path(layout, "id3lib", configuration, platform, "id3lib.lib")),
        ("miniupnp", dependency_library_path(layout, "miniupnp", configuration, platform, "miniupnpc.lib")),
        ("libpcpnatpmp", dependency_library_path(layout, "libpcpnatpmp", configuration, platform, "pcpnatpmp.lib")),
        ("ResizableLib", dependency_library_path(layout, "ResizableLib", configuration, platform, "ResizableLib.lib")),
        ("zlib", dependency_library_path(layout, "zlib", configuration, platform, "zlib.lib")),
        ("mbedtls", dependency_library_path(layout, "mbedtls", configuration, platform, "mbedtls.lib")),
        ("mbedx509", dependency_library_path(layout, "mbedtls", configuration, platform, "mbedx509.lib")),
        ("tfpsacrypto", dependency_library_path(layout, "mbedtls", configuration, platform, "tfpsacrypto.lib")),
    )


def arm64_host_tool_architecture() -> str:
    """Returns the 64-bit MSBuild host-tool architecture to use for ARM64 app builds.

    WHY: Building the large MFC precompiled header with the default 32-bit host
    cl.exe exhausts its address space - the app build fails with C3859 "Failed to
    create virtual memory for PCH" and C1076 "internal heap limit reached". A
    64-bit host has room. On a native ARM64 host (e.g. the windows-11-arm CI
    runner) the native HostARM64 compiler is correct and avoids the x64-emulation
    PCH exhaustion; on an x64 host cross-compiling ARM64 the HostX64 compiler is
    correct. PROCESSOR_ARCHITEW6432 reports the true machine architecture even when
    this process itself runs under emulation.
    """

    native = (os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE") or "").upper()
    return "ARM64" if native == "ARM64" else "x64"


def app_property_overrides(layout: WorkspaceLayout, platform: str) -> tuple[str, ...]:
    """Returns app MSBuild dependency root properties."""

    third_party = layout.resolve_workspace_path("repos/third_party")
    properties = [
        f"/p:WorkspaceRoot={with_trailing_separator(layout.emule_workspace_root)}",
        f"/p:CryptoPpRoot={with_trailing_separator(third_party / 'emulebb-cryptopp')}",
        f"/p:CryptoPpLibRoot={with_trailing_separator(dependency_library_root(layout, 'cryptopp', '$(Configuration)', platform))}",
        f"/p:Id3libRoot={with_trailing_separator(third_party / 'emulebb-id3lib')}",
        f"/p:Id3libLibRoot={with_trailing_separator(dependency_library_root(layout, 'id3lib', '$(Configuration)', platform))}",
        f"/p:MbedTlsRoot={with_trailing_separator(third_party / 'emulebb-mbedtls')}",
        f"/p:MbedTlsLibRoot={with_trailing_separator(dependency_library_root(layout, 'mbedtls', '$(Configuration)', platform))}",
        f"/p:MbedTlsCryptoLibRoot={with_trailing_separator(dependency_library_root(layout, 'mbedtls', '$(Configuration)', platform))}",
        f"/p:MiniUpnpRoot={with_trailing_separator(third_party / 'emulebb-miniupnp')}",
        f"/p:MiniUpnpLibRoot={with_trailing_separator(dependency_library_root(layout, 'miniupnp', '$(Configuration)', platform))}",
        f"/p:NlohmannJsonRoot={with_trailing_separator(third_party / 'emulebb-nlohmann-json' / 'single_include')}",
        f"/p:PcpNatPmpRoot={with_trailing_separator(third_party / 'emulebb-libpcpnatpmp')}",
        f"/p:PcpNatPmpLibRoot={with_trailing_separator(dependency_library_root(layout, 'libpcpnatpmp', '$(Configuration)', platform))}",
        f"/p:ResizableLibRoot={with_trailing_separator(third_party / 'emulebb-resizablelib')}",
        f"/p:ResizableLibLibRoot={with_trailing_separator(dependency_library_root(layout, 'ResizableLib', '$(Configuration)', platform))}",
        f"/p:ZlibRoot={with_trailing_separator(third_party / 'emulebb-zlib')}",
        f"/p:ZlibLibRoot={with_trailing_separator(dependency_library_root(layout, 'zlib', '$(Configuration)', platform))}",
    ]
    if platform == "ARM64":
        # WHY: select a 64-bit host compiler so the large MFC PCH fits; native
        # ARM64 hosts use HostARM64 (no x64 emulation), x64 hosts use HostX64.
        # See arm64_host_tool_architecture for the C3859/C1076 rationale.
        properties.append(f"/p:PreferredToolArchitecture={arm64_host_tool_architecture()}")
    return tuple(properties)


def crypto_pp_properties(layout: WorkspaceLayout, platform: str) -> tuple[str, ...]:
    """Returns Crypto++ MSBuild policy overrides."""

    properties = [default_platform_toolset_property(layout)]
    if platform == "ARM64":
        properties.extend(
            [
                f"/p:ForceImportAfterCppProps={arm64_overrides_props_path(layout)}",
                f"/p:ForceImportAfterCppTargets={arm64_overrides_targets_path(layout)}",
            ]
        )
    return tuple(properties)


def id3lib_properties(configuration: str, platform: str) -> tuple[str, ...]:
    """Returns id3lib MSBuild policy overrides."""

    if configuration == "Release" and platform == "ARM64":
        return ("/p:ConfigurationType=StaticLibrary",)
    return ()


def default_platform_toolset_property(layout: WorkspaceLayout) -> str:
    """Returns the active MSBuild PlatformToolset policy property."""

    return f"/p:PlatformToolset={env_override(layout.toolset_override_variable) or 'v143'}"


def crypto_pp_environment(platform: str) -> dict[str, str]:
    """Returns Crypto++ compiler environment overrides."""

    if platform != "ARM64":
        return {}
    return {"CL": "/DCRYPTOPP_DISABLE_ASM /DCRYPTOPP_NO_CPU_FEATURE_PROBES"}


def ensure_arm64_override_targets(layout: WorkspaceLayout) -> None:
    """Writes ARM64 Crypto++ override files under workspace state."""

    props_path = arm64_overrides_props_path(layout)
    targets_path = arm64_overrides_targets_path(layout)
    props_path.parent.mkdir(parents=True, exist_ok=True)
    props_path.write_text(
        """<Project ToolsVersion="Current" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemDefinitionGroup Condition="'$(Platform)'=='ARM64'">
    <ClCompile>
      <MultiProcessorCompilation>false</MultiProcessorCompilation>
      <AdditionalOptions>/DCRYPTOPP_DISABLE_ASM /DCRYPTOPP_NO_CPU_FEATURE_PROBES %(AdditionalOptions)</AdditionalOptions>
    </ClCompile>
  </ItemDefinitionGroup>
</Project>
""",
        encoding="utf-8",
        newline="\n",
    )
    targets_path.write_text(
        """<Project ToolsVersion="Current" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup Condition="'$(Platform)'=='ARM64'">
    <ClCompile Remove="blake2s_simd.cpp;blake2b_simd.cpp;chacha_simd.cpp;crc_simd.cpp;gcm_simd.cpp;gf2n_simd.cpp;lea_simd.cpp;rijndael_simd.cpp;sha_simd.cpp;simon128_simd.cpp;speck128_simd.cpp" />
  </ItemGroup>
</Project>
""",
        encoding="utf-8",
        newline="\n",
    )


def verify_app_control_flow_guard(session: BuildSession, *, binary_path: Path, step_name: str) -> None:
    """Verifies Control Flow Guard metadata in a built app executable."""

    log_path = session.log_directory / f"{file_token(str(binary_path.resolve().with_suffix('')))}-cfg.log"
    started_at = time.monotonic()
    try:
        if not binary_path.is_file():
            raise RuntimeError(f"Built app binary not found: {binary_path}")
        completed = subprocess.run(
            [str(get_dumpbin_path()), "/headers", "/loadconfig", str(binary_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        log_path.write_text(completed.stdout, encoding="utf-8", newline="\n")
        if completed.returncode != 0:
            raise RuntimeError(f"dumpbin failed with exit code {completed.returncode} for {binary_path}")
        dumpbin_output = completed.stdout.lower()
        for pattern in ("cf instrumented", "fid table present"):
            if pattern not in dumpbin_output:
                raise RuntimeError(f"CFG verification failed for {binary_path}: missing '{pattern}' in dumpbin output.")
        session.add_step(
            name=step_name,
            succeeded=True,
            log_path=log_path,
            duration_seconds=time.monotonic() - started_at,
            warning_count=0,
        )
    except Exception:
        session.add_step(
            name=step_name,
            succeeded=False,
            log_path=log_path,
            duration_seconds=time.monotonic() - started_at,
            warning_count=0,
        )
        raise


def app_binary_path(app_root: Path, configuration: str, platform: str, *, executable_name: str = APP_EXE_NAME) -> Path:
    """Returns the built eMuleBB executable path."""

    return app_root / "srchybrid" / platform / configuration / executable_name


def app_build_output_root(
    layout: WorkspaceLayout,
    variant_name: str,
    configuration: str,
    platform: str,
    *,
    executable_name: str = APP_EXE_NAME,
) -> Path:
    """Returns the external routine app build root for one variant and flavor."""

    flavor = "diagnostics" if executable_name == DIAGNOSTICS_APP_EXE_NAME else "standard"
    return layout.output_build_root / "app" / variant_name / platform / configuration / flavor


def app_build_binary_path(
    layout: WorkspaceLayout,
    variant_name: str,
    configuration: str,
    platform: str,
    *,
    executable_name: str = APP_EXE_NAME,
) -> Path:
    """Returns the external routine app executable path."""

    return app_build_output_root(
        layout,
        variant_name,
        configuration,
        platform,
        executable_name=executable_name,
    ) / "bin" / executable_name


def mbedtls_project_path(layout: WorkspaceLayout) -> Path:
    """Returns the mbedTLS Visual Studio project path."""

    return layout.resolve_workspace_path("repos/third_party/emulebb-mbedtls") / "visualc" / "VS2017" / "mbedTLS.vcxproj"


def mbedtls_library_root(layout: WorkspaceLayout, platform: str) -> Path:
    """Returns the mbedTLS library output root for a target platform."""

    return layout.resolve_workspace_path("repos/third_party/emulebb-mbedtls") / "visualc" / f"VS2017-{platform}" / "library"


def libpcpnatpmp_build_root(layout: WorkspaceLayout, platform: str) -> Path:
    """Returns the libpcpnatpmp CMake build root."""

    return layout.output_third_party_build_root / "libpcpnatpmp" / platform / "cmake-build"


def libpcpnatpmp_library_path(layout: WorkspaceLayout, configuration: str, platform: str) -> Path:
    """Returns the libpcpnatpmp static library path."""

    return libpcpnatpmp_build_root(layout, platform) / "lib" / configuration / "pcpnatpmp.lib"


def dependency_library_root(layout: WorkspaceLayout, dependency: str, configuration: str, platform: str) -> Path:
    """Returns the canonical staged library root for one dependency."""

    return layout.output_third_party_build_root / dependency / platform / configuration


def dependency_library_path(
    layout: WorkspaceLayout,
    dependency: str,
    configuration: str,
    platform: str,
    file_name: str,
) -> Path:
    """Returns one canonical staged dependency library path."""

    return dependency_library_root(layout, dependency, configuration, platform) / file_name


def stage_app_dependency_artifacts(layout: WorkspaceLayout, configuration: str, platform: str) -> None:
    """Copies dependency libraries from native build layouts into the canonical contract."""

    missing: list[str] = []
    for name, source_path, destination_path in staged_dependency_artifact_map(layout, configuration, platform):
        if not source_path.is_file():
            missing.append(f"{name}: {source_path}")
            continue
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path.resolve() != destination_path.resolve():
            shutil.copy2(source_path, destination_path)
    if missing:
        raise RuntimeError(
            f"Cannot stage dependency outputs for {configuration}|{platform}; missing native outputs:\n"
            + "\n".join(missing)
        )


def staged_dependency_artifact_map(layout: WorkspaceLayout, configuration: str, platform: str) -> tuple[tuple[str, Path, Path], ...]:
    """Returns native dependency output paths and their canonical staged paths."""

    third_party = layout.resolve_workspace_path("repos/third_party")
    mbedtls_root = mbedtls_library_root(layout, platform)
    tfpsa_configured = mbedtls_root.parent / "tf-psa-crypto" / "core" / configuration / "tfpsacrypto.lib"
    tfpsa_flat = mbedtls_root / "tfpsacrypto.lib"
    tfpsa_source = tfpsa_configured if tfpsa_configured.is_file() else tfpsa_flat
    return (
        (
            "cryptopp",
            third_party / "emulebb-cryptopp" / platform / "Output" / configuration / "cryptlib.lib",
            dependency_library_path(layout, "cryptopp", configuration, platform, "cryptlib.lib"),
        ),
        (
            "id3lib",
            third_party / "emulebb-id3lib" / "libprj" / platform / configuration / "id3lib.lib",
            dependency_library_path(layout, "id3lib", configuration, platform, "id3lib.lib"),
        ),
        (
            "miniupnp",
            third_party / "emulebb-miniupnp" / "miniupnpc" / "msvc" / platform / configuration / "miniupnpc.lib",
            dependency_library_path(layout, "miniupnp", configuration, platform, "miniupnpc.lib"),
        ),
        (
            "libpcpnatpmp",
            libpcpnatpmp_library_path(layout, configuration, platform),
            dependency_library_path(layout, "libpcpnatpmp", configuration, platform, "pcpnatpmp.lib"),
        ),
        (
            "ResizableLib",
            third_party / "emulebb-resizablelib" / "ResizableLib" / platform / configuration / "ResizableLib.lib",
            dependency_library_path(layout, "ResizableLib", configuration, platform, "ResizableLib.lib"),
        ),
        (
            "zlib",
            third_party / "emulebb-zlib" / "contrib" / "vstudio" / "vc" / platform / configuration / "zlib.lib",
            dependency_library_path(layout, "zlib", configuration, platform, "zlib.lib"),
        ),
        (
            "mbedtls",
            mbedtls_root / configuration / "mbedtls.lib",
            dependency_library_path(layout, "mbedtls", configuration, platform, "mbedtls.lib"),
        ),
        (
            "mbedx509",
            mbedtls_root / configuration / "mbedx509.lib",
            dependency_library_path(layout, "mbedtls", configuration, platform, "mbedx509.lib"),
        ),
        (
            "tfpsacrypto",
            tfpsa_source,
            dependency_library_path(layout, "mbedtls", configuration, platform, "tfpsacrypto.lib"),
        ),
    )


def prune_repo_local_dependency_outputs(layout: WorkspaceLayout) -> None:
    """Removes known repo-local dependency outputs after canonical staging."""

    for path in repo_local_dependency_output_paths(layout):
        if path.is_dir():
            shutil.rmtree(path)
        elif path.is_file():
            path.unlink()


def repo_local_dependency_output_paths(layout: WorkspaceLayout) -> tuple[Path, ...]:
    """Returns known dependency build outputs that must not survive orchestration."""

    third_party = layout.resolve_workspace_path("repos/third_party")
    return (
        third_party / "emulebb-cryptopp" / "x64",
        third_party / "emulebb-cryptopp" / "ARM64",
        third_party / "emulebb-cryptopp" / "adhoc.cpp",
        third_party / "emulebb-cryptopp" / "adhoc.cpp.copied",
        third_party / "emulebb-id3lib" / "libprj" / "x64",
        third_party / "emulebb-id3lib" / "libprj" / "ARM64",
        third_party / "emulebb-id3lib" / "libprj" / "id3lib",
        third_party / "emulebb-libpcpnatpmp" / "cmake-build-x64",
        third_party / "emulebb-libpcpnatpmp" / "cmake-build-arm64",
        third_party / "emulebb-mbedtls" / "visualc" / "VS2017-x64",
        third_party / "emulebb-mbedtls" / "visualc" / "VS2017-ARM64",
        third_party / "emulebb-mbedtls" / "visualc" / "VS2017" / "x64",
        third_party / "emulebb-mbedtls" / "visualc" / "VS2017" / "ARM64",
        third_party / "emulebb-miniupnp" / "miniupnpc" / "msvc" / "x64",
        third_party / "emulebb-miniupnp" / "miniupnpc" / "msvc" / "ARM64",
        third_party / "emulebb-miniupnp" / "miniupnpc" / "miniupnpcstrings.h",
        third_party / "emulebb-miniupnp" / "miniupnpc" / "rc_version.h",
        third_party / "emulebb-resizablelib" / "ResizableLib" / "x64",
        third_party / "emulebb-resizablelib" / "ResizableLib" / "ARM64",
        third_party / "emulebb-zlib" / "cmake-build-x64",
        third_party / "emulebb-zlib" / "cmake-build-ARM64",
        third_party / "emulebb-zlib" / "contrib" / "vstudio" / "vc" / "x64",
        third_party / "emulebb-zlib" / "contrib" / "vstudio" / "vc" / "ARM64",
        third_party / "emulebb-zlib" / "contrib" / "vstudio" / "vc" / "zlib",
    )


def with_trailing_separator(path: Path) -> str:
    """Formats an absolute path with a trailing separator for MSBuild properties."""

    text = str(path.resolve())
    return text if text.endswith("\\") else text + "\\"


def arm64_overrides_props_path(layout: WorkspaceLayout) -> Path:
    """Returns the generated ARM64 Crypto++ props path."""

    return layout.output_build_root / "arm64-overrides" / "arm64-build-overrides.props"


def arm64_overrides_targets_path(layout: WorkspaceLayout) -> Path:
    """Returns the generated ARM64 Crypto++ targets path."""

    return layout.output_build_root / "arm64-overrides" / "arm64-build-overrides.targets"


def remove_stale_generated_artifacts(repo_path: Path, kind: str) -> None:
    """Removes stale generated dependency artifacts for clean x64 rebuilds."""

    paths = {
        "zlib": (repo_path / "cmake-build-x64",),
        "mbedtls": (repo_path / "visualc" / "VS2017-x64", repo_path / "visualc" / "VS2017" / "x64"),
    }[kind]
    for path in paths:
        if path.exists():
            shutil.rmtree(path)
