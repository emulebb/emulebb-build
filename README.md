# emulebb-build

`emulebb-build` is the canonical build and test orchestration layer for the
workspace rooted at `EMULEBB_WORKSPACE_ROOT`.

`emulebb-build` owns workspace materialization, generated workspace topology, and
build/test orchestration. It is responsible for:

- canonical repo and worktree materialization
- dependency builds under `repos\third_party`
- app builds for the canonical workspace app worktrees
- shared test builds from `repos\emulebb-build-tests`
- parity, coverage, and live-diff execution against the canonical app variants

`python -m emule_workspace` is the authoritative orchestration surface.

## Purpose

Clone this repo as `<workspace-root>\repos\emulebb-build`, then use the supported
`emule_workspace` command to materialize and operate the workspace. The package
owns typed command parsing, workspace topology loading, locking, subprocess
routing, build/test execution, live-test wrapping, and release packaging.

## Workspace Assumption

This repo is the source of truth for workspace materialization and the full
layout contract. A materialized workspace uses the standard
`EMULEBB_WORKSPACE_ROOT\repos\...` plus
`EMULEBB_WORKSPACE_ROOT\workspaces\workspace\...` layout.

In practice this repo needs:

- `repos\emulebb`
- `repos\emulebb-build`
- `repos\emulebb-build-tests`
- `repos\emulebb-tooling`
- `repos\ed2k-server`
- `repos\goed2k-server`
- `repos\emulebb-rust`
- `repos\qbittorrentbb`
- `repos\emulebb-pages`
- `repos\emulebb-org-profile`
- `repos\third_party\...`
- `workspaces\workspace\app\emulebb-main`
- `workspaces\workspace\app\emulebb-community-baseline`
- `workspaces\workspace\app\emulebb-community-tracing-harness`

Analysis reference clones under `analysis\...` are optional. Use
`python -m emule_workspace materialize --include-analysis` or
`python -m emule_workspace sync --include-analysis` when comparison work needs
those checkouts.

`repos\emulebb` is not a normal development checkout. Python materialization owns
it as the canonical app anchor, and it is expected to stay detached at
`origin/main`. Active app development belongs in the managed worktrees under
`workspaces\workspace\app\...`, especially `emulebb-main` for the mainline branch.

Canonical managed app variants use short variant keys in
`python -m emule_workspace` commands. The worktree folder and branch names stay
descriptive, but they are not CLI variant keys:

| Variant key | Worktree | Branch |
| --- | --- | --- |
| `main` | `workspaces\workspace\app\emulebb-main` | `main` |
| `community` | `workspaces\workspace\app\emulebb-community-baseline` | `baseline/community-0.72a` |
| `tracing-harness` | `workspaces\workspace\app\emulebb-community-tracing-harness` | `tracing-harness/community-0.72a` |

Branch roles, release intent, and baseline rules are owned by
`EMULEBB_WORKSPACE_ROOT\repos\emulebb-tooling\docs\WORKSPACE-POLICY.md`.

The active app layout and workspace repo paths are topology-driven from
`workspaces\workspace\deps.json`, with build-specific settings kept in this repo's
`deps.json`. Test, coverage, and live-diff flows resolve their app roots from
configured variant names rather than duplicating hardcoded worktree paths.
The generated manifest also tracks the public pages repo and organization
profile repo so web/documentation repos are materialized with the rest of the
workspace. `workspace.repos.ed2k_server` remains the deterministic Go test
fixture, while `workspace.repos.ed2k_index_server` identifies the managed Rust
Service/Lab fork without selecting it for any test lane.

## Supported Commands

Python-first commands:

```powershell
python -m emule_workspace materialize
python -m emule_workspace sync
python -m emule_workspace status
python -m emule_workspace dep-updates
python -m emule_workspace compare
python -m emule_workspace env-check
python -m emule_workspace dep-status
python -m emule_workspace validate
python -m emule_workspace cleanup
python -m emule_workspace build libs
python -m emule_workspace build app
python -m emule_workspace build tests
python -m emule_workspace build clients
python -m emule_workspace build all
python -m emule_workspace test python
python -m emule_workspace test all
python -m emule_workspace test live-diff
python -m emule_workspace test protocol-parity
python -m emule_workspace test live-e2e
python -m emule_workspace test windows-vm
python -m emule_workspace test amutorrent-session
python -m emule_workspace test amutorrent-session --backend rust
python -m emule_workspace test rust-webui
python -m emule_workspace test rust-network-proof --lane windows-consumer --help
python -m emule_workspace test community-core-coverage
python -m emule_workspace full
python -m emule_workspace package-release
python -m emule_workspace vm-lab prepare
```

`build clients` defaults to the materialized `emulebb-rust` client. The retired
aMule checkout is no longer materialized; use `--client amule` only with a
manual `repos\amule` checkout.

## Suite Installer Examples

The packaged suite installer supports non-interactive app and language selection:

```powershell
.\Bootstrap-eMuleBBSuite.ps1 -Apps none-arr -Language English
.\Bootstrap-eMuleBBSuite.ps1 -Apps all-arr -Language Italian
.\Bootstrap-eMuleBBSuite.ps1 -Apps lidarr,whisparr -Language Portuguese
```

`-Apps` also accepts individual apps (`radarr`, `sonarr`, `lidarr`,
`whisparr`) and presets (`default-arr`, `all-arr`, `none-arr`, `controller`,
`all`). Whisparr is explicit opt-in through `whisparr` or `all-arr`; the full
suite default selects Prowlarr, Radarr, Sonarr, and Lidarr. If a media Arr app
is selected, Prowlarr and aMuTorrent are included automatically.

Release assets publish `suite-scripts-<version>.zip` and
`suite-scripts-<version>.manifest.json` beside the normal package ZIPs. The
bundle carries installer/runtime PowerShell plus suite config such as language
preferences. The bootstrapper auto-discovers matching bundle assets from the
selected GitHub Release, or adjacent files when `-EmulebbPackageZip` points at a
local package. Pass `-NoSuiteScriptsBundle` to use the scripts embedded in the
package instead.

Explicit bundle URLs or file paths still override auto-discovery:

```powershell
.\Bootstrap-eMuleBBSuite.ps1 `
  -SuiteScriptsZip https://github.com/emulebb/emulebb/releases/download/emulebb-v0.7.3-rc.1/suite-scripts-0.7.3-rc.1.zip `
  -SuiteScriptsManifest https://github.com/emulebb/emulebb/releases/download/emulebb-v0.7.3-rc.1/suite-scripts-0.7.3-rc.1.manifest.json
```

Command behavior:

- `help` prints supported commands and common options.
- `env-check` verifies the core toolchain discovery for Git, Visual Studio, and MSBuild.
- `dep-status` reports branch and worktree status for the dependency repos and canonical app worktrees that exist locally.
- `validate` verifies required workspace paths, canonical app worktree presence, branch alignment, required test helper scripts, modified tracked-file editorconfig compliance, the PowerShell boundary, and the shared static policy audits from `emulebb-tooling\ci`.
- `cleanup` dry-runs generated artifact pruning by default. Use `--apply` only after reviewing the candidate summary.
- The Python `package-release` command may reanchor a clean `repos\emulebb`
  checkout back to detached `origin/main` before building package artifacts.
- `build libs` builds the shared dependency set for the selected `--config` and `--platform`.
- `build libs` includes the CMake-built `libpcpnatpmp` static library, and the current `main` app build now links it for the PCP/NAT-PMP NAT-mapping backend.
- `build app` builds all canonical app variants for the selected `--config` and `--platform`.
- `build tests` builds the shared test harness against the configured build variant.
- `test python` runs the fast pytest harness suite from `emulebb-build-tests`; use `--path`, `--expression`, and `--quiet` to narrow the pytest selection.
- `test all` runs parity tests, native coverage, and live diff using the configured test target variants.
- `test live-diff` runs parity and divergence comparison directly against any two configured app variants.
- `test protocol-parity` runs the focused Kad/eD2K gate: protocol surface diff, protocol oracle golden validation, and `protocol-parity` native live-diff.
- `test live-e2e` runs the aggregate UI, REST API, and live-wire E2E suite from `emulebb-build-tests`.
- `test windows-vm` runs package-smoke proof inside clean local Hyper-V Windows guests restored from a configured checkpoint.
- `test amutorrent-session` starts a disposable interactive aMuTorrent session against eMuleBB REST and leaves both processes running for operator testing. Use `--backend rust` to point aMuTorrent at the staged Rust client, falling back to the Rust repo Cargo launcher when needed.
- `test rust-webui` stages the SPA under the external output root, installs the locked npm graph, and runs unit tests, stateful Playwright E2E, type checking, and the production build. Its full-workflow browser scenario covers `server.met` and stopped-Kad `nodes.dat` imports, independent server/Kad disconnect and reconnect, filtered `linux` PDF search, rendered-result download, activity, and stop state.
- `test rust-network-proof --lane windows-consumer` verifies an exact hosted Rust Windows ZIP with a fresh default profile and the rendered WebUI. It enables best-effort UPnP in Settings and verifies the restart, imports `server.met` and `nodes.dat`, proves server and Kad disconnect/reconnect independently, runs the explicit runtime-only `--search-term` through automatic/server/Kad search, and clicks an exact allowlisted `.pdf` result smaller than 5 MiB. Trigger mode requires bytes or an actively transferring source and then verifies the stopped flag; `--complete-transfers` additionally verifies delivered size and SHA-256. Search text and candidate identity are redacted from retained evidence.
- `test community-core-coverage` runs community-core coverage checks with live REST E2E coverage enabled.
- `build all` runs `build libs`, `build app`, and `build tests`.
- `full` runs `build all`, then `test all`, then prints a workspace summary.
- `package-release` builds the main Release app, language DLLs, release ZIP, and release manifest. Use `--require-signing` for release-candidate or stable assets that must fail unless Authenticode signing is configured.
- `vm-lab prepare` creates or refreshes local Hyper-V Windows 10/11 guest images from operator-provided ISO paths and records the clean checkpoint used by `test windows-vm`.

All top-level `emule_workspace` commands are serialized per workspace root.
This single-owner workspace lock is intentional. It prevents overlapping
`env-check`, build, test, and live-diff commands from trampling shared state,
logs, and outputs in the same workspace.

If another command already owns the workspace lock, the next command fails fast
with a clear owner message instead of running concurrently against the same
workspace. When commands are launched back-to-back, the second command may need
to wait briefly for the first command's lock window to close fully before it
can start.

## Cleanup

`cleanup` prunes generated workspace artifacts only from known output roots. It
does not run broad `git clean`, does not remove tracked files, and defaults to a
dry run:

```powershell
python -m emule_workspace cleanup
python -m emule_workspace cleanup --apply
```

The default `routine` profile targets old scratch artifacts under
`workspaces\workspace\state\test-artifacts`, heavyweight payloads and
timestamped reports under `workspaces\workspace\state\test-reports`, old
`workspaces\workspace\state\build-logs` runs, and Python test caches. Legacy
`workspaces\workspace\state\live-e2e-artifacts` runs are also pruned when they
age out, but new harness output should not be written there. Build outputs and
superseded release rehearsal state require explicit flags:

```powershell
python -m emule_workspace cleanup --include-build-outputs
python -m emule_workspace cleanup --include-release-state
```

## Build Scope

Dependencies and app builds honor the selected invocation parameters:

- `-Config Debug|Release`
- `-Platform x64|ARM64`
- `-BuildOutputMode Full|Warnings|ErrorsOnly`
- `-Clean`
- `Win32` is not part of the active workspace build matrix

Examples:

- `build-app -Config Debug -Platform x64` builds only `Debug|x64`
- `build-libs -Config Release -Platform ARM64` builds only `Release|ARM64`
- `build-all` and `full` use the same selected target instead of expanding to a hidden multi-target matrix

Build commands default to a quiet filtered console view plus a short step recap.
Use `-BuildOutputMode Full` when you want raw MSBuild output for troubleshooting.
Use `-Clean` when you explicitly want rebuild/cleanup behavior; normal runs stay incremental.
Build runs write text logs, MSBuild binary logs, and a machine-readable recap under
`workspaces\<workspace>\state\build-logs\`.
Build and test run directories use UTC `YYYYMMDDTHHMMSSZ` prefixes. Build recaps
are named `build-result.json`; certification and release-campaign recaps are
named `certification-result.json` and `release-campaign-run-result.json`.
Test reports use `<suite>\latest` snapshots and suite-scoped leaves such as
`<suite>-result.json`, `<suite>-result.partial.json`, and `<suite>-summary.json`.

## Environment Overrides

The orchestration layer has a small set of supported environment overrides for
nonstandard tool locations and controlled toolchain diagnosis. They are not a
substitute for the pinned workspace topology, and release/CI runs should leave
them unset unless the run evidence explicitly records why an override was
needed.

| Variable | Used by | Default when unset | Intended use |
|---|---|---|---|
| `EMULEBB_MSYS2_ROOT` | `build clients --client amule` | First usable system MSYS2 root, normally a standard MSYS2 install under the system drive | Point the aMule Windows client build at a nonstandard MSYS2 install. |
| `EMULEBB_VS_PLATFORM_TOOLSET` | app, dependency, release MSBuild entrypoints, and Visual Studio CMake dependency builds | `v143` | Temporarily force a Visual Studio toolset such as `v143` or `v145` for compatibility diagnosis. |
| `EMULEBB_CMAKE_GENERATOR` | Visual Studio CMake dependency builds | Active Visual Studio generator | Force a specific CMake generator such as `Visual Studio 18 2026` or `Ninja` for dependency-build toolchain diagnosis. |
| `EMULEBB_CMAKE_PLATFORM` | Visual Studio CMake dependency builds | The requested build platform (`x64` or `ARM64`) | Override the CMake generator platform (`-A`); set empty to omit the platform argument. |

Use these only at the shell or CI-job boundary:

```powershell
$env:EMULEBB_MSYS2_ROOT = Join-Path $env:SystemDrive 'Tools\msys64'
$env:EMULEBB_VS_PLATFORM_TOOLSET = 'v143'
python -m emule_workspace env-check
```

If a build succeeds only with an override, record the variable and value in the
run notes. Do not commit generated project files or dependency metadata that
bake a local override into the workspace.

Shared test builds support `x64` and `ARM64`. Test execution remains `x64`
only:

- `test all --platform ARM64` fails with a clear unsupported-platform error

ARM64 remains available for dependency and app builds, but x64 is still the
primary stabilized acceptance path for the end-to-end canonical workflow.

Live-diff examples:

- `live-diff -Config Debug -Platform x64` uses the configured defaults (`main` vs `community`)
- `live-diff -Config Debug -Platform x64 -TestRunVariant main -BaselineVariant community` compares main against the parity/regression baseline
- `live-diff -Config Release -Platform x64 -TestRunVariant broadband -BaselineVariant community` compares broadband behavior against the parity/regression baseline

Live E2E examples:

- `live-e2e -Config Release -Platform x64` runs the full maintained UI, REST API, and live-wire lane, including the default six-term server/Kad release search matrix and one paused live search-result download trigger
- `python -m emule_workspace test live-e2e --profile beta-green --fail-fast` runs the short beta gate: REST contract coverage, REST smoke stress, shared-directories REST, and reduced Prowlarr/eMuleBB search resilience
- `python -m emule_workspace test live-e2e --profile controller-surface --fail-fast` runs the focused controller API surface gate: native `/api/v1`, qBittorrent-compatible `/api/v2`, Torznab/Arr, and aMuTorrent browser smoke
- `python -m emule_workspace test live-e2e --profile beta-release --fail-fast` extends the beta gate with Radarr and Sonarr full acquisition plus cold-start dump stress
- `live-e2e -Config Release -Platform x64 -RestDownloadTriggerCount 0` disables the REST live download trigger for diagnosis
- `live-e2e -Config Release -Platform x64 -LiveSuite preference-ui -LiveSuite rest-api` runs a focused subset
- `live-e2e -Config Release -Platform x64 -SkipLiveSeedRefresh` reuses the checked-in live seed files for offline diagnosis

Interactive aMuTorrent example:

- `amutorrent-session -Config Debug -Platform x64 -LiveNetwork` launches Debug x64 eMuleBB with a disposable profile, starts aMuTorrent against the eMuleBB REST API, opens the aMuTorrent URL, and writes a `stop-session.cmd` helper into the session report directory. Add `-Backend rust` to use the Rust client package.

Headless Rust client build example:

- `python -m emule_workspace build clients --client emulebb-rust --config Release --platform x64` builds `repos\emulebb-rust` through Cargo and stages `emulebb-rust.exe` under `EMULEBB_WORKSPACE_OUTPUT_ROOT\tools\emulebb-rust\bin`.

Rust eD2K index-server build example:

- `python -m emule_workspace build ed2k-server --config Release --platform x64` builds the Linux-only `repos\ed2k-server` fork through WSL on Windows (or native Cargo on Linux) and stages `ed2k-server` under `EMULEBB_WORKSPACE_OUTPUT_ROOT\tools\ed2k-server\bin`. It does not launch the server or select it for test campaigns.

Python test examples:

- `python-tests` runs the default fast pytest collection
- `python-tests -PythonTestPath tests/python/test_auto_browse_live.py -PythonTestExpression pending -PythonTestQuiet` runs one focused pytest expression

Windows VM package-smoke examples:

- Copy `vm-lab.example.json` to ignored `vm-lab.local.json`, set Windows 10/11 ISO paths, and set `EMULEBB_VM_TEST_PASSWORD` in the shell.
- `python -m emule_workspace vm-lab prepare --matrix win10,win11` prepares Hyper-V guests and the `emulebb-clean` checkpoint.
- `python -m emule_workspace test windows-vm --matrix win10,win11 --profile package-smoke --release-version 0.7.3-rc.1 --skip-build` restores each guest, copies the release ZIP, runs local package smoke, collects artifacts, and restores the clean checkpoint.
- Host VM orchestration is owned here; guest-side package-smoke harness templates are loaded from `repos\emulebb-build-tests\emule_test_harness`.

## Validation And Test Model

`validate` is intended to catch canonical workspace drift before a longer build:

- missing `repos` or `workspaces` roots
- missing dependency repos
- missing canonical app worktrees
- stale canonical app anchor state in `repos\emulebb`
- app worktrees checked out on the wrong branches
- dependency repos not aligned with their active local `origin/HEAD` pins
- active documentation hardcoding machine-specific absolute paths
- active workflow docs/scripts drifting back to `.sln` / `.slnx` entrypoints
- active warning suppressions drifting beyond the approved narrow third-party exceptions
- modified tracked text files drifting from repo `.editorconfig` / `.gitattributes`
- tracked PowerShell files outside the approved package runtime boundary,
  `repos\emulebb-build\emule_workspace\release_assets\emulebb\scripts\*.ps1`
- missing shared test helper scripts

Tracked-file cleanliness is intentionally a separate explicit audit via
`repos\emulebb-tooling\ci\check-clean-worktree.py`, so in-progress feature work
does not get blocked by routine `validate`.

The setup/build contract is intentionally narrow:

- `emulebb-build` owns workspace topology, managed app worktree creation, and full `sync` reconciliation.
- `emulebb-build` assumes that topology exists and only self-heals the clean setup-owned `repos\emulebb` anchor when validation needs it to match current `origin/main`.

The test flows use the manifest-configured app variants:

- test build target: `community`
- test run target: `main`
- live-diff baseline target: `community`

The tracing harness variant is part of the canonical buildable app set, but it
is not the default test-run or baseline target unless explicitly selected.

`build-tests` honors the selected `-Config` value for both `x64` and `ARM64`.
`test` honors the selected `-Config` value, but requires `-Platform x64`.

## Implementation Notes

- ARM64 Crypto++ overrides are generated under `workspaces\<workspace>\state\`
  instead of the workspace root.
- Tool-install fallbacks are allowed where they are genuinely about tool
  discovery, such as standard Perl or Visual Studio install locations.
- Workspace-specific hardcoded local paths are not part of the supported model.
- Legacy pre-canonical branch names are not part of the supported workflow.
