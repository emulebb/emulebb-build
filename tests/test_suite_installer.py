from __future__ import annotations

import hashlib
import json
import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from emule_workspace import release

import suite_install_fixtures


INSTALLER = Path("emule_workspace/release_assets/emulebb/scripts/Install-eMuleBBSuite.ps1")
BOOTSTRAPPER = Path("emule_workspace/release_assets/emulebb/scripts/Bootstrap-eMuleBBSuite.ps1")


def _write_manifest(path: Path, zip_path: Path) -> None:
    path.write_text(
        json.dumps({"sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest()}) + "\n",
        encoding="utf-8",
    )


def _automation_example_entries() -> dict[str, bytes]:
    return {
        "eMuleBB/examples/automation/README.md": b"examples\n",
        "eMuleBB/examples/automation/Import-eMuleBBRestExample.ps1": b"#Requires -Version 5.1\n",
        "eMuleBB/examples/automation/Get-eMuleBBStatus.ps1": b"#Requires -Version 5.1\n",
        "eMuleBB/examples/automation/Set-eMuleBBLimits.ps1": b"#Requires -Version 5.1\n",
        "eMuleBB/examples/automation/Search-eMuleBB.ps1": b"#Requires -Version 5.1\n",
        "eMuleBB/examples/automation/Download-ReleaseGroup.ps1": b"#Requires -Version 5.1\n",
    }


def _write_automation_examples_component(release_root: Path, version: str) -> tuple[Path, Path]:
    zip_path = release_root / f"automation-examples-{version}.zip"
    manifest_path = release_root / f"automation-examples-{version}.manifest.json"
    suite_install_fixtures.write_zip(zip_path, _automation_example_entries())
    suite_install_fixtures.write_manifest(manifest_path, zip_path)
    return zip_path, manifest_path


def _run_powershell(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    powershell = shutil.which("powershell")
    assert powershell is not None
    return subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )


def _assert_powershell_parse(path: Path, *, cwd: Path) -> None:
    command = (
        "$tokens=$null; $errors=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors) { $errors | Format-List *; exit 1 }"
    )
    _run_powershell(["-Command", command], cwd=cwd)


def _read_ini_sections(path: Path, *, encoding: str = "utf-16") -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in path.read_text(encoding=encoding).splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = {}
            sections[stripped[1:-1]] = current
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        current[key.strip()] = value
    return sections


def _find_free_loopback_port(start: int = 49152, end: int = 65535) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No free loopback port found in {start}-{end}.")


def test_suite_installer_core_install_writes_bind_aware_config_and_scripts(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    install_root = tmp_path / "suite"
    control_bind = "127.0.0.1"
    emulebb_port = _find_free_loopback_port()
    suite_install_fixtures.write_core_release(release_root)

    repo_root = Path.cwd()
    _run_powershell(
        [
            "-File",
            str((repo_root / INSTALLER).resolve()),
            "-NonInteractive",
            "-NoStart",
            "-Force",
            "-Bundle",
            "Core",
            "-InstallRoot",
            str(install_root),
            "-ReleaseBaseUrl",
            release_root.as_uri(),
            "-ControlBindAddress",
            control_bind,
            "-P2PBindInterface",
            "hide.me",
            "-EmulebbPort",
            str(emulebb_port),
        ],
        cwd=repo_root,
    )

    preferences = (install_root / "profiles" / "emulebb" / "config" / "preferences.ini").read_text(encoding="utf-16")
    assert "[eMule]" in preferences
    assert "BindInterface=hide.me" in preferences
    assert "BindAddr=\n" in preferences
    assert "BlockNetworkWhenBindUnavailableAtStartup=0" in preferences
    assert "NetworkGuardMode=Off" in preferences
    assert "NetworkGuardAllowedCIDRs=" in preferences
    assert "[WebServer]" in preferences
    assert f"BindAddr={control_bind}" in preferences
    assert f"Port={emulebb_port}" in preferences

    suite_config = json.loads((install_root / "manifests" / "suite-config.json").read_text(encoding="utf-8-sig"))
    assert suite_config["schema"] == "emulebb.suite-config.v1"
    assert suite_config["installKind"] == "Production"
    assert suite_config["emulebbPackageFlavor"] == "standard"
    assert suite_config["emulebbExecutableName"] == "emulebb.exe"
    assert suite_config["services"]["emulebb"]["bindAddress"] == control_bind
    assert suite_config["services"]["emulebb"]["clientHost"] == control_bind
    assert suite_config["services"]["emulebb"]["port"] == emulebb_port
    assert suite_config["selectedApps"] == ["emulebb"]
    assert suite_config["credentials"]["username"] == "admin"
    assert re.fullmatch(r"[A-Za-z0-9]{24}", suite_config["credentials"]["password"])
    assert re.fullmatch(r"[A-Za-z0-9]{24}", suite_config["services"]["emulebb"]["apiKey"])
    assert suite_config["p2p"] == {
        "bindInterface": "hide.me",
        "blockNetworkWhenBindUnavailableAtStartup": False,
        "networkGuardMode": "Off",
        "networkGuardAllowedCIDRs": "",
    }

    install_manifest = json.loads((install_root / "manifests" / "suite-install.json").read_text(encoding="utf-8-sig"))
    assert install_manifest["profileImport"] == {
        "action": "fresh",
        "configured": False,
        "source": None,
        "sourcePreferencesSha256": None,
    }
    assert install_manifest["installKind"] == "Production"
    assert install_manifest["emulebbPackageFlavor"] == "standard"
    assert install_manifest["emulebbExecutableName"] == "emulebb.exe"
    assert install_manifest["services"]["emulebb"]["apiKeyPresent"] is True
    assert install_manifest["services"]["emulebb"]["clientHost"] == control_bind
    assert "apiKey" not in install_manifest["services"]["emulebb"]
    assert suite_config["services"]["emulebb"]["apiKey"] not in json.dumps(install_manifest)
    assert suite_config["credentials"]["password"] not in json.dumps(install_manifest)

    credentials = (install_root / "credentials.txt").read_text(encoding="utf-8-sig")
    assert "eMuleBB Suite credentials" in credentials
    assert "Suite web login" in credentials
    assert "Username: admin" in credentials
    assert f"Password: {suite_config['credentials']['password']}" in credentials
    assert f"eMuleBB URL: http://{control_bind}:{emulebb_port}" in credentials
    assert f"eMuleBB API key: {suite_config['services']['emulebb']['apiKey']}" in credentials
    credentials_html = (install_root / "credentials.html").read_text(encoding="utf-8-sig")
    assert "eMuleBB Suite Credentials" in credentials_html
    assert f"http://{control_bind}:{emulebb_port}" in credentials_html
    assert 'target="_blank"' in credentials_html
    assert 'rel="noopener noreferrer"' in credentials_html
    assert 'data-copy="' in credentials_html
    assert suite_config["credentials"]["password"] in credentials_html
    assert suite_config["services"]["emulebb"]["apiKey"] in credentials_html

    start_emulebb = (install_root / "scripts" / "Start-eMuleBB.ps1").read_text(encoding="utf-8-sig")
    assert "apps\\eMuleBB" in start_emulebb
    assert "emulebbExecutableName" in start_emulebb
    assert "'emulebb.exe'" in start_emulebb
    assert "profiles\\emulebb" in start_emulebb
    assert "function Test-ProcessRunning" in start_emulebb
    assert "Starting eMuleBB: $emule" in start_emulebb
    assert "function Invoke-EmuleBootstrapFileDownload" not in start_emulebb
    assert "function Ensure-EmuleBootstrapFiles" not in start_emulebb
    assert "return to this PowerShell window so setup can complete the app registrations" not in start_emulebb
    assert "Continuing in 6 seconds..." not in start_emulebb
    assert "Start-Sleep -Seconds 6" not in start_emulebb
    assert "eMuleBB executable is missing" in start_emulebb
    assert "eMuleBB could not be started from" in start_emulebb
    assert "eMuleBB did not stay running after launch" in start_emulebb

    runtime_start_suite = (install_root / "scripts" / "Start-Suite.ps1").read_text(encoding="utf-8-sig")
    initialize_suite = (install_root / "scripts" / "Initialize-Suite.ps1").read_text(encoding="utf-8-sig")
    assert "suite-config.json" in runtime_start_suite
    assert "Start-eMuleBB.ps1" in runtime_start_suite
    assert "Register-Prowlarr.ps1') -Action Register" not in runtime_start_suite
    assert "Register-ArrStack.ps1') -Action Register" not in runtime_start_suite
    assert "/api/auth/status" not in runtime_start_suite
    assert "Wait-Json" not in runtime_start_suite

    start_suite = initialize_suite
    assert "suite-config.json" in start_suite
    assert "Start-Suite.ps1" in start_suite
    assert "& (Join-Path $Root 'scripts\\Start-Suite.ps1')" in start_suite
    assert start_suite.index("Ensure-EmuleBootstrapFiles") < start_suite.index("& (Join-Path $Root 'scripts\\Start-Suite.ps1')")
    assert "$EmuleBootstrapFiles" in start_suite
    assert "server.met" in start_suite
    assert "https://upd.emule-security.org/server.met" in start_suite
    assert "nodes.dat" in start_suite
    assert "https://upd.emule-security.org/nodes.dat" in start_suite
    assert "function Invoke-EmuleBootstrapFileDownload" in start_suite
    assert "function Ensure-EmuleBootstrapFiles" in start_suite
    assert "profiles\\emulebb" in start_suite
    assert "Downloaded $Name was empty." in start_suite
    assert "function Write-Utf8NoBomFile" in start_suite
    assert "New-Object System.Text.UTF8Encoding($false)" in start_suite
    assert "Register-Prowlarr.ps1') -Action Register" in start_suite
    assert "Register-ArrStack.ps1') @args" in start_suite
    assert "$args[\"${display}Url\"] = $arrUrls[$arrName]" in start_suite
    assert "$args[\"${display}ApiKey\"] = [string]$Config.services.$arrName.apiKey" in start_suite
    assert "function Set-ArrHostCredentials" in start_suite
    assert "Set-ObjectProperty -Target $hostConfig -Name 'authenticationMethod' -Value 'forms'" in start_suite
    assert "Set-ObjectProperty -Target $hostConfig -Name 'authenticationRequired' -Value 'enabled'" in start_suite
    assert "Set-ObjectProperty -Target $hostConfig -Name 'username' -Value ([string]$Config.credentials.username)" in start_suite
    assert "Set-ObjectProperty -Target $hostConfig -Name 'password' -Value ([string]$Config.credentials.password)" in start_suite
    assert "function Set-ArrUiLanguage" in start_suite
    assert "Set-ObjectProperty -Target $uiConfig -Name 'uiLanguage' -Value (Get-ArrUiLanguageValue -Name $Name -Language $Language)" in start_suite
    assert "$hostConfig.authenticationMethod = 'forms'" not in start_suite
    assert "Set-ArrHostCredentials -Name $display" in start_suite
    assert "function Get-ExceptionMessage" in start_suite
    assert "$responseProperty = $Exception.PSObject.Properties['Response']" in start_suite
    assert "$null -ne $responseProperty" in start_suite
    assert "$null -ne $Exception -and $null -ne $Exception.Response" not in start_suite
    assert "function Invoke-SuiteJsonApi" in start_suite
    assert "function Invoke-SuiteJsonApiList" in start_suite
    assert "Invoke-SuiteJsonApi -Name $Name -Uri $Uri -Headers $Headers | ForEach-Object { $_ }" in start_suite
    assert "function Get-ServiceClientHost" in start_suite
    assert "Last error:" in start_suite
    assert "Timed out waiting for $Name at $Uri" in start_suite
    assert "Invoke-SuiteJsonApi -Name \"$Name web login update\"" in start_suite
    assert "Invoke-StepWithRetry -Name \"$display web login setup\"" in start_suite
    assert "function Ensure-ArrRootFolder" in start_suite
    assert "function Wait-ArrDefaultProfiles" in start_suite
    assert "function Get-FirstObjectWithId" in start_suite
    assert "Invoke-SuiteJsonApiList -Name \"$Name quality profile list\"" in start_suite
    assert "$rootFolderUrl = \"$Url/$ApiPath/rootfolder\"" in start_suite
    assert "New-Item -ItemType Directory -Force -Path $Path" in start_suite
    assert "Invoke-SuiteJsonApi -Name \"$Name root folder create\"" in start_suite
    assert "$Name metadata profile list" in start_suite
    assert "$Url/$ApiPath/metadataprofile" in start_suite
    assert "name = 'eMuleBB Music'" in start_suite
    assert "'eMuleBB Music'" in start_suite
    assert "defaultQualityProfileId = [int]$profiles.QualityProfile.id" in start_suite
    assert "defaultMetadataProfileId = [int]$profiles.MetadataProfile.id" in start_suite
    assert "already configured as a root folder" in start_suite
    assert "$rootFolder.PSObject.Properties['path']" in start_suite
    assert "'radarr' { return 'media\\movies' }" in start_suite
    assert "'sonarr' { return 'media\\series' }" in start_suite
    assert "'lidarr' { return 'media\\music' }" in start_suite
    assert "'whisparr' { return 'media\\whisparr' }" in start_suite
    assert "Ensure-ArrRootFolder -Name $display" in start_suite
    assert "EmulebbCategoryPath = (Join-Path $Root \"downloads\\$arrName\")" in start_suite
    assert "Wait-Json -Name $display -Uri \"$($arrUrls[$arrName])/$apiPath/system/status\"" in start_suite
    assert "function Invoke-StepWithRetry" in start_suite
    assert "Invoke-StepWithRetry -Name \"$display registration\"" in start_suite
    assert "ProwlarrApiKey = [string]$Config.services.prowlarr.apiKey" in start_suite
    assert "SkipProwlarrSync = $true" in start_suite
    assert "Invoke-StepWithRetry -Name 'Prowlarr application sync'" in start_suite
    assert "-SyncProwlarrOnly" in start_suite
    assert start_suite.index("Invoke-StepWithRetry -Name \"$display web login setup\"") < start_suite.index("Invoke-StepWithRetry -Name \"$display root folder setup\"")
    assert start_suite.index("Invoke-StepWithRetry -Name \"$display root folder setup\"") < start_suite.index("Invoke-StepWithRetry -Name 'Prowlarr registration'")
    assert start_suite.index("Invoke-StepWithRetry -Name \"$display registration\"") < start_suite.index("Invoke-StepWithRetry -Name 'Prowlarr application sync'")

    stop_suite = (install_root / "scripts" / "Stop-Suite.ps1").read_text(encoding="utf-8-sig")
    assert "Get-CimInstance Win32_Process" in stop_suite
    assert "function Get-FirstSuiteExecutable" in stop_suite
    assert "Get-ChildItem -Path $appRoot -Filter $FileName -Recurse -File" in stop_suite
    assert "Get-FirstSuiteExecutable -RelativeRoot 'apps\\Prowlarr' -FileName 'Prowlarr.exe'" in stop_suite
    assert "StartsWith($Root" not in stop_suite
    assert "No eMuleBB Suite processes are running." in stop_suite
    assert "return" in stop_suite
    assert "exit 0" not in stop_suite
    assert "Stopping {0} (PID {1})" in stop_suite
    assert "eMuleBB Suite stop request completed." in stop_suite

    for generated_script in (
        "Initialize-Suite.ps1",
        "Start-eMuleBB.ps1",
        "Start-Suite.ps1",
        "Stop-Suite.ps1",
        "Get-SuiteStatus.ps1",
        "Get-SuiteInfo.ps1",
        "Repair-Suite.ps1",
    ):
        _assert_powershell_parse(install_root / "scripts" / generated_script, cwd=repo_root)


def test_suite_installer_copies_packaged_installer_into_suite_scripts(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    install_root = tmp_path / "suite"
    installer_payload = b"#Requires -Version 5.1\nWrite-Host 'fresh packaged installer'\n"
    suite_install_fixtures.write_core_release(release_root, installer_payload=installer_payload)

    repo_root = Path.cwd()
    _run_powershell(
        [
            "-File",
            str((repo_root / INSTALLER).resolve()),
            "-NonInteractive",
            "-NoStart",
            "-Force",
            "-Bundle",
            "Core",
            "-InstallRoot",
            str(install_root),
            "-ReleaseBaseUrl",
            release_root.as_uri(),
        ],
        cwd=repo_root,
    )

    assert (install_root / "scripts" / "Install-eMuleBBSuite.ps1").read_bytes() == installer_payload


def test_suite_installer_accepts_hashed_suite_scripts_bundle(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    install_root = tmp_path / "suite"
    scripts_zip = tmp_path / "suite-scripts.zip"
    scripts_manifest = tmp_path / "suite-scripts.manifest.json"
    suite_install_fixtures.write_core_release(release_root)
    script_entries = suite_install_fixtures.runtime_script_entries(
        installer_payload=INSTALLER.read_bytes(),
    )
    script_entries["eMuleBB/scripts/Start-Suite.ps1"] += b"\n# suite-scripts-bundle-marker\n"
    script_entries["eMuleBB/config/suite-languages.json"] = json.dumps(
        {
            "schema": "emulebb.suite-languages.v1",
            "languages": [
                {
                    "key": "english",
                    "displayName": "English",
                    "emuleLanguageId": 9,
                    "emuleLocale": "en_US",
                    "arrUiLanguage": "English",
                    "arrContentLanguage": "English",
                }
            ],
            "testMarker": "suite-config-bundle-marker",
        }
    ).encode("utf-8")
    suite_install_fixtures.write_zip(scripts_zip, script_entries)
    _write_manifest(scripts_manifest, scripts_zip)

    repo_root = Path.cwd()
    _run_powershell(
        [
            "-File",
            str((repo_root / INSTALLER).resolve()),
            "-NonInteractive",
            "-NoStart",
            "-Force",
            "-Bundle",
            "Core",
            "-InstallRoot",
            str(install_root),
            "-ReleaseBaseUrl",
            release_root.as_uri(),
            "-SuiteScriptsZip",
            str(scripts_zip),
            "-SuiteScriptsManifest",
            str(scripts_manifest),
        ],
        cwd=repo_root,
    )

    start_suite = (install_root / "scripts" / "Start-Suite.ps1").read_text(encoding="utf-8-sig")
    assert "suite-scripts-bundle-marker" in start_suite
    language_config = (install_root / "config" / "suite-languages.json").read_text(encoding="utf-8-sig")
    assert "suite-config-bundle-marker" in language_config
    install_manifest = suite_install_fixtures.read_suite_install_manifest(install_root)
    assert install_manifest["suiteScripts"] == {
        "zip": str(scripts_zip),
        "manifest": str(scripts_manifest),
    }


def test_suite_installer_accepts_hashed_automation_examples_bundle(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    install_root = tmp_path / "suite"
    examples_zip = tmp_path / "automation-examples.zip"
    examples_manifest = tmp_path / "automation-examples.manifest.json"
    suite_install_fixtures.write_core_release(release_root)
    entries = _automation_example_entries()
    entries["eMuleBB/examples/automation/README.md"] = b"examples-marker\n"
    suite_install_fixtures.write_zip(examples_zip, entries)
    _write_manifest(examples_manifest, examples_zip)

    repo_root = Path.cwd()
    _run_powershell(
        [
            "-File",
            str((repo_root / INSTALLER).resolve()),
            "-NonInteractive",
            "-NoStart",
            "-Force",
            "-Bundle",
            "Core",
            "-InstallRoot",
            str(install_root),
            "-ReleaseBaseUrl",
            release_root.as_uri(),
            "-AutomationExamplesZip",
            str(examples_zip),
            "-AutomationExamplesManifest",
            str(examples_manifest),
        ],
        cwd=repo_root,
    )

    readme = (install_root / "examples" / "automation" / "README.md").read_text(encoding="utf-8-sig")
    assert "examples-marker" in readme
    assert (install_root / "examples" / "automation" / "Download-ReleaseGroup.ps1").is_file()
    install_manifest = suite_install_fixtures.read_suite_install_manifest(install_root)
    assert install_manifest["automationExamples"] == {
        "zip": str(examples_zip),
        "manifest": str(examples_manifest),
        "installed": True,
    }


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing-manifest", "AutomationExamplesZip requires -AutomationExamplesManifest with a SHA256 hash."),
        ("bad-hash", "SHA256 mismatch"),
        ("missing-example", "Automation examples bundle did not include examples\\automation\\Search-eMuleBB.ps1."),
    ],
)
def test_suite_installer_rejects_invalid_automation_examples_bundle(
    tmp_path: Path,
    case: str,
    expected: str,
) -> None:
    release_root = tmp_path / "release"
    install_root = tmp_path / "suite"
    examples_zip = tmp_path / "automation-examples.zip"
    examples_manifest = tmp_path / "automation-examples.manifest.json"
    suite_install_fixtures.write_core_release(release_root)
    entries = _automation_example_entries()
    if case == "missing-example":
        del entries["eMuleBB/examples/automation/Search-eMuleBB.ps1"]
    suite_install_fixtures.write_zip(examples_zip, entries)
    if case == "bad-hash":
        examples_manifest.write_text(json.dumps({"sha256": "0" * 64}) + "\n", encoding="utf-8")
    elif case != "missing-manifest":
        _write_manifest(examples_manifest, examples_zip)

    args = [
        "-NonInteractive",
        "-NoStart",
        "-Force",
        "-Bundle",
        "Core",
        "-InstallRoot",
        str(install_root),
        "-ReleaseBaseUrl",
        release_root.as_uri(),
        "-AutomationExamplesZip",
        str(examples_zip),
    ]
    if case != "missing-manifest":
        args.extend(["-AutomationExamplesManifest", str(examples_manifest)])

    completed = suite_install_fixtures.run_installer(
        (Path.cwd() / INSTALLER).resolve(),
        args,
        cwd=Path.cwd(),
        check=False,
    )

    assert completed.returncode != 0
    assert expected in completed.stdout


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing-manifest", "SuiteScriptsZip requires -SuiteScriptsManifest with a SHA256 hash."),
        ("bad-hash", "SHA256 mismatch"),
        ("missing-script", "Suite scripts bundle did not include scripts\\Repair-Suite.ps1."),
    ],
)
def test_suite_installer_rejects_invalid_suite_scripts_bundle(
    tmp_path: Path,
    case: str,
    expected: str,
) -> None:
    release_root = tmp_path / "release"
    install_root = tmp_path / "suite"
    scripts_zip = tmp_path / "suite-scripts.zip"
    scripts_manifest = tmp_path / "suite-scripts.manifest.json"
    suite_install_fixtures.write_core_release(release_root)
    script_entries = suite_install_fixtures.runtime_script_entries(installer_payload=INSTALLER.read_bytes())
    if case == "missing-script":
        del script_entries["eMuleBB/scripts/Repair-Suite.ps1"]
    suite_install_fixtures.write_zip(scripts_zip, script_entries)
    if case == "bad-hash":
        scripts_manifest.write_text(json.dumps({"sha256": "0" * 64}) + "\n", encoding="utf-8")
    elif case != "missing-manifest":
        _write_manifest(scripts_manifest, scripts_zip)

    args = [
        "-NonInteractive",
        "-NoStart",
        "-Force",
        "-Bundle",
        "Core",
        "-InstallRoot",
        str(install_root),
        "-ReleaseBaseUrl",
        release_root.as_uri(),
        "-SuiteScriptsZip",
        str(scripts_zip),
    ]
    if case != "missing-manifest":
        args.extend(["-SuiteScriptsManifest", str(scripts_manifest)])

    completed = suite_install_fixtures.run_installer(
        (Path.cwd() / INSTALLER).resolve(),
        args,
        cwd=Path.cwd(),
        check=False,
    )

    assert completed.returncode != 0
    assert expected in completed.stdout


def test_suite_installer_accepts_local_emulebb_package_zip_override(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    install_root = tmp_path / "suite"
    package_version = "0.7.3-nightly.20260604.localabc"
    release = suite_install_fixtures.write_core_release(release_root, version=package_version)

    repo_root = Path.cwd()
    _run_powershell(
        [
            "-File",
            str((repo_root / INSTALLER).resolve()),
            "-NonInteractive",
            "-NoStart",
            "-Force",
            "-Bundle",
            "Core",
            "-InstallRoot",
            str(install_root),
            "-EmulebbPackageZip",
            str(release.package_zip),
            "-EmulebbPackageManifest",
            str(release.manifest),
        ],
        cwd=repo_root,
    )

    suite_config = json.loads((install_root / "manifests" / "suite-config.json").read_text(encoding="utf-8-sig"))
    assert suite_config["version"] == package_version
    assert Path(suite_config["packageSources"]["emulebb"]["zip"]) == release.package_zip
    assert Path(suite_config["packageSources"]["emulebb"]["manifest"]) == release.manifest
    assert (install_root / "apps" / "eMuleBB" / "emulebb.exe").is_file()


def test_suite_installer_core_install_uses_diagnostics_executable_name(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    install_root = tmp_path / "suite"
    suite_install_fixtures.write_core_release(
        release_root,
        package_flavor="diagnostics",
        executable_name="emulebb-diagnostics.exe",
    )

    repo_root = Path.cwd()
    suite_install_fixtures.run_installer(
        (repo_root / INSTALLER).resolve(),
        [
            "-NonInteractive",
            "-NoStart",
            "-Force",
            "-Bundle",
            "Core",
            "-InstallRoot",
            str(install_root),
            "-ReleaseBaseUrl",
            release_root.as_uri(),
            "-EmulebbPackageFlavor",
            "diagnostics",
        ],
        cwd=repo_root,
    )

    assert (install_root / "apps" / "eMuleBB" / "emulebb-diagnostics.exe").is_file()
    assert not (install_root / "apps" / "eMuleBB" / "emulebb.exe").exists()
    suite_config = suite_install_fixtures.read_suite_config(install_root)
    assert suite_config["emulebbPackageFlavor"] == "diagnostics"
    assert suite_config["emulebbExecutableName"] == "emulebb-diagnostics.exe"
    install_manifest = suite_install_fixtures.read_suite_install_manifest(install_root)
    assert install_manifest["emulebbPackageFlavor"] == "diagnostics"
    assert install_manifest["emulebbExecutableName"] == "emulebb-diagnostics.exe"
    start_emulebb = (install_root / "scripts" / "Start-eMuleBB.ps1").read_text(encoding="utf-8-sig")
    assert "emulebbExecutableName" in start_emulebb
    assert "emulebb-diagnostics.exe" not in start_emulebb


def test_suite_installer_recomputes_executable_name_from_package_flavor(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    install_root = tmp_path / "suite"
    suite_install_fixtures.write_core_release(
        release_root,
        package_flavor="diagnostics",
        executable_name="emulebb-diagnostics.exe",
    )
    config_path = tmp_path / "stale-suite-config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema": "emulebb.suite-config.v1",
                "emulebbPackageFlavor": "standard",
                "emulebbExecutableName": "emulebb.exe",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    repo_root = Path.cwd()
    suite_install_fixtures.run_installer(
        (repo_root / INSTALLER).resolve(),
        [
            "-NonInteractive",
            "-NoStart",
            "-Force",
            "-Bundle",
            "Core",
            "-InstallRoot",
            str(install_root),
            "-ConfigFile",
            str(config_path),
            "-ReleaseBaseUrl",
            release_root.as_uri(),
            "-EmulebbPackageFlavor",
            "diagnostics",
        ],
        cwd=repo_root,
    )

    suite_config = suite_install_fixtures.read_suite_config(install_root)
    assert suite_config["emulebbPackageFlavor"] == "diagnostics"
    assert suite_config["emulebbExecutableName"] == "emulebb-diagnostics.exe"
    assert (install_root / "apps" / "eMuleBB" / "emulebb-diagnostics.exe").is_file()


def test_suite_installer_rejects_release_manifest_without_sha256(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    install_root = tmp_path / "suite"
    package_zip = release_root / "emulebb-0.7.3-rc.2-x64.zip"
    manifest = release_root / "emulebb-0.7.3-rc.2-x64.manifest.json"
    suite_install_fixtures.write_zip(
        package_zip,
        {
            "eMuleBB/emulebb.exe": b"exe\n",
            "eMuleBB/scripts/Install-eMuleBBSuite.ps1": b"#Requires -Version 5.1\n",
        },
    )
    manifest.write_text("{}\n", encoding="utf-8")

    repo_root = Path.cwd()
    completed = subprocess.run(
        [
            shutil.which("powershell") or "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str((repo_root / INSTALLER).resolve()),
            "-NonInteractive",
            "-NoStart",
            "-Force",
            "-Bundle",
            "Core",
            "-InstallRoot",
            str(install_root),
            "-ReleaseBaseUrl",
            release_root.as_uri(),
        ],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert completed.returncode != 0
    assert "eMuleBB release manifest must include a SHA256 hash." in completed.stdout


def test_suite_installer_apps_none_arr_preset_installs_core_only(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    install_root = tmp_path / "suite"
    suite_install_fixtures.write_core_release(release_root)

    repo_root = Path.cwd()
    suite_install_fixtures.run_installer(
        (repo_root / INSTALLER).resolve(),
        [
            "-NonInteractive",
            "-NoStart",
            "-Force",
            "-Bundle",
            "Full",
            "-Apps",
            "none-arr",
            "-InstallRoot",
            str(install_root),
            "-ReleaseBaseUrl",
            release_root.as_uri(),
        ],
        cwd=repo_root,
    )

    suite_config = suite_install_fixtures.read_suite_config(install_root)
    assert suite_config["selectedApps"] == ["emulebb"]
    assert not (install_root / "data" / "prowlarr").exists()
    assert not (install_root / "data" / "radarr").exists()


def test_suite_installer_imports_profile_config_only_once_and_preserves_refresh_profile(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    install_root = tmp_path / "suite"
    source_profile = tmp_path / "source-profile"
    suite_install_fixtures.write_core_release(release_root)
    source_config = source_profile / "config"
    source_config.mkdir(parents=True)
    source_preferences = source_config / "preferences.ini"
    source_preferences.write_text(
        "\n".join(
            [
                "[eMule]",
                "Nick=imported-user",
                r"IncomingDir=F:\old\incoming",
                "[WebServer]",
                "ApiKey=source-api-key",
                "Enabled=0",
                "CustomWebSetting=keep",
                "",
            ]
        ),
        encoding="utf-16",
    )
    source_preferences_hash = suite_install_fixtures.sha256_bytes(source_preferences.read_bytes())
    (source_config / "known.met").write_text("identity\n", encoding="utf-8")
    (source_profile / "runtime.log").write_text("do not import\n", encoding="utf-8")

    repo_root = Path.cwd()
    suite_install_fixtures.run_installer(
        (repo_root / INSTALLER).resolve(),
        [
            "-NonInteractive",
            "-NoStart",
            "-Force",
            "-Bundle",
            "Core",
            "-InstallRoot",
            str(install_root),
            "-ReleaseBaseUrl",
            release_root.as_uri(),
            "-ImportProfileDir",
            str(source_profile),
            "-P2PBindInterface",
            "hide.me",
            "-EmulebbPort",
            "49152",
        ],
        cwd=repo_root,
    )

    profile_config = install_root / "profiles" / "emulebb" / "config"
    preferences_path = profile_config / "preferences.ini"
    preferences = preferences_path.read_text(encoding="utf-16")
    suite_config_path = install_root / "manifests" / "suite-config.json"
    suite_config = suite_install_fixtures.read_suite_config(install_root)
    assert "Nick=imported-user" in preferences
    assert "CustomWebSetting=keep" in preferences
    assert "IncomingDir=" + str(install_root / "downloads" / "incoming") in preferences
    assert "TempDir=" + str(install_root / "downloads" / "temp") in preferences
    assert "BindInterface=hide.me" in preferences
    assert "Enabled=1" in preferences
    assert "Port=49152" in preferences
    assert "ApiKey=" + suite_config["services"]["emulebb"]["apiKey"] in preferences
    assert "source-api-key" not in preferences
    assert (profile_config / "known.met").read_text(encoding="utf-8") == "identity\n"
    assert not (install_root / "profiles" / "emulebb" / "runtime.log").exists()
    assert source_preferences.read_text(encoding="utf-16").count(r"IncomingDir=F:\old\incoming") == 1
    assert suite_config["importProfileDir"] == str(source_profile)
    install_manifest = suite_install_fixtures.read_suite_install_manifest(install_root)
    assert install_manifest["profileImport"] == {
        "action": "imported",
        "configured": True,
        "source": str(source_profile),
        "sourcePreferencesSha256": source_preferences_hash,
    }

    preferences_path.write_text(
        preferences.replace("Nick=imported-user", "Nick=edited-after-bootstrap") + "RefreshOnly=keep\n",
        encoding="utf-16",
    )
    shutil.rmtree(source_profile)

    suite_install_fixtures.run_installer(
        (repo_root / INSTALLER).resolve(),
        [
            "-NonInteractive",
            "-NoStart",
            "-Force",
            "-ConfigFile",
            str(suite_config_path),
        ],
        cwd=repo_root,
    )

    refreshed_preferences = preferences_path.read_text(encoding="utf-16")
    refreshed_install_manifest = suite_install_fixtures.read_suite_install_manifest(install_root)
    assert "Nick=edited-after-bootstrap" in refreshed_preferences
    assert "RefreshOnly=keep" in refreshed_preferences
    assert "IncomingDir=" + str(install_root / "downloads" / "incoming") in refreshed_preferences
    assert refreshed_install_manifest["profileImport"] == {
        "action": "skipped-existing",
        "configured": True,
        "source": str(source_profile),
        "sourcePreferencesSha256": None,
    }


def test_suite_installer_profile_import_appends_missing_ini_sections(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    install_root = tmp_path / "suite"
    source_profile = tmp_path / "source-profile"
    suite_install_fixtures.write_core_release(release_root)
    source_config = source_profile / "config"
    source_config.mkdir(parents=True)
    (source_config / "preferences.ini").write_text("[eMule]\nNick=missing-webserver\n", encoding="utf-16")

    repo_root = Path.cwd()
    emulebb_port = _find_free_loopback_port()
    suite_install_fixtures.run_installer(
        (repo_root / INSTALLER).resolve(),
        [
            "-NonInteractive",
            "-NoStart",
            "-Force",
            "-Bundle",
            "Core",
            "-InstallRoot",
            str(install_root),
            "-ReleaseBaseUrl",
            release_root.as_uri(),
            "-ImportProfileDir",
            str(source_profile),
            "-EmulebbPort",
            str(emulebb_port),
        ],
        cwd=repo_root,
    )

    preferences = suite_install_fixtures.read_suite_preferences(install_root)
    suite_config = suite_install_fixtures.read_suite_config(install_root)
    assert "Nick=missing-webserver" in preferences
    assert "[WebServer]" in preferences
    assert "Enabled=1" in preferences
    assert f"Port={emulebb_port}" in preferences
    assert "ApiKey=" + suite_config["services"]["emulebb"]["apiKey"] in preferences


def test_suite_installer_import_profile_requires_source_before_bootstrap(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    install_root = tmp_path / "suite"
    missing_source = tmp_path / "missing-profile"
    package_zip = release_root / "emulebb-0.7.3-rc.2-x64.zip"
    manifest = release_root / "emulebb-0.7.3-rc.2-x64.manifest.json"
    suite_install_fixtures.write_zip(package_zip, {"eMuleBB/emulebb.exe": b"exe\n"})
    _write_manifest(manifest, package_zip)

    repo_root = Path.cwd()
    completed = subprocess.run(
        [
            shutil.which("powershell") or "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str((repo_root / INSTALLER).resolve()),
            "-NonInteractive",
            "-NoStart",
            "-Force",
            "-Bundle",
            "Core",
            "-InstallRoot",
            str(install_root),
            "-ReleaseBaseUrl",
            release_root.as_uri(),
            "-ImportProfileDir",
            str(missing_source),
        ],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert completed.returncode != 0
    assert "ImportProfileDir must contain" in completed.stdout
    assert "preferences.ini" in completed.stdout


def test_suite_installer_copies_configured_emulebb_symbols(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    install_root = tmp_path / "suite"
    pdb_path = tmp_path / "build" / "emulebb.pdb"
    suite_install_fixtures.write_core_release(release_root)
    pdb_path.parent.mkdir()
    pdb_path.write_bytes(b"private-symbols\n")
    expected_hash = suite_install_fixtures.sha256_bytes(pdb_path.read_bytes())

    repo_root = Path.cwd()
    suite_install_fixtures.run_installer(
        (repo_root / INSTALLER).resolve(),
        [
            "-NonInteractive",
            "-NoStart",
            "-Force",
            "-Bundle",
            "Core",
            "-InstallRoot",
            str(install_root),
            "-ReleaseBaseUrl",
            release_root.as_uri(),
            "-EmulebbPdbPath",
            str(pdb_path),
        ],
        cwd=repo_root,
    )

    adjacent_pdb = install_root / "apps" / "eMuleBB" / "emulebb.pdb"
    versioned_pdb = install_root / "symbols" / "emulebb-v0.7.3-rc.2" / "x64" / "emulebb.pdb"
    assert adjacent_pdb.read_bytes() == b"private-symbols\n"
    assert versioned_pdb.read_bytes() == b"private-symbols\n"
    install_manifest = suite_install_fixtures.read_suite_install_manifest(install_root)
    assert install_manifest["symbols"]["action"] == "copied"
    assert install_manifest["symbols"]["source"] == str(pdb_path)
    assert install_manifest["symbols"]["sourceSha256"] == expected_hash
    assert install_manifest["symbols"]["adjacentPdb"] == str(adjacent_pdb)
    assert install_manifest["symbols"]["versionedPdb"] == str(versioned_pdb)


def test_suite_installer_preserves_app_roots_when_extracting_multiple_packages() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")

    assert "$extractRoot = Join-Path $downloadRoot (\"extract-$Name\")" in installer
    assert "$extractedPackageRoot = Join-Path $extractRoot $Name" in installer
    assert "$targetPackageRoot = Join-Path $Destination $Name" in installer
    assert "Move-Item -LiteralPath $extractedPackageRoot -Destination $targetPackageRoot" in installer


def test_suite_installer_requires_hashed_pinned_dependencies() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")

    assert "function Read-JsonFile" in installer
    assert "$Description is not valid JSON: $Path" in installer
    assert "Fix or regenerate this file, then rerun scripts\\Install-eMuleBBSuite.ps1." in installer
    assert "Read-JsonFile -Path $ConfigFile -Description 'ConfigFile'" in installer
    assert "Read-JsonFile -Path $ManifestPath -Description 'DependencyManifest'" in installer
    assert "c97754d8bf36b224e21f081b4f0bcc4d3cbf61fb84c087aeaa30f60d31012530" in installer
    assert "c9a6ecd1d84923947f6f92f16b43780744834e0a2be7a1ee04bf86e82113500b" in installer
    assert "19a81e69dedd8d317b5fa8a1a9c48d63bc3b3f3ba87b84c94ff6d75b1803e419" in installer
    assert "658b755c069ac3c3ed6265378baad3e7d270be12bd9642abd5b402b9f95a54ed" in installer
    assert "6f58ce889f59c311410f7d2b18895b33c03456463486f3b1ebc93d97a0f54541" in installer
    assert "f8c81699550a3a9425e9bdd1d6621587c463c51c568848ab8d3e36fe5efc222c" in installer
    assert "Latest dependency resolution requires -DependencyManifest entries with exact URLs and SHA256 hashes." in installer
    assert "DependencyChannel Latest requires -DependencyManifest with exact URLs and SHA256 hashes." in installer
    assert "Latest dependency releases are unavailable unless you pass -DependencyManifest." in installer
    assert "$Name dependency download requires a SHA256 hash." in installer
    assert "function Get-DependencyDownloadRecoveryMessage" in installer
    assert "pass -DependencyManifest with reachable file paths or URLs and SHA256 hashes" in installer
    assert "Failed to download $Url -> $Destination after $maxAttempts attempts. $message $(Get-DependencyDownloadRecoveryMessage)" in installer
    assert "function Get-DependencyCacheRoot" in installer
    assert "'emulebb-suite-cache'" in installer
    assert "function Restore-DependencyCache" in installer
    assert "Assert-FileHash -Path $cachePath -ExpectedSha256 $ExpectedSha256" in installer
    assert "Using cached $Name $AssetName" in installer
    assert "Ignoring stale cached $Name $cachePath" in installer
    assert "function Save-DependencyCache" in installer
    assert "Cached $Name $AssetName" in installer
    assert "Restore-DependencyCache -Kind 'arr' -Name \"$Name dependency\" -AssetName $assetName -ExpectedSha256 $Spec.Sha256 -Destination $archivePath" in installer
    assert "Downloading $Name dependency $assetName" in installer
    assert "Verifying $Name dependency" in installer
    assert "Extracting $Name dependency" in installer
    assert "Restore-DependencyCache -Kind 'media-tool'" in installer
    assert "Downloading optional $($Spec.DisplayName) dependency $assetName" in installer
    assert "Verifying optional $($Spec.DisplayName) dependency" in installer
    assert "Extracting optional $($Spec.DisplayName) dependency" in installer
    assert "Write-Warning \"$($spec.DisplayName) is optional and was not installed." in installer
    assert "[switch]$InstallMediaTools" in installer
    assert "[switch]$NoMediaTools" in installer
    assert "Use either -InstallMediaTools or -NoMediaTools, not both." in installer
    assert "Optional media tools" in installer
    assert "All selected components are installed in portable mode under the suite install root." in installer
    assert "Existing Arr apps, media tools, PATH entries, and system software are not modified." in installer
    assert "Download and install portable MPC-HC, ffmpeg, and MediaInfo under the suite path" in installer
    assert "Isolation: portable under the suite root; existing Arr apps, media tools, PATH entries, and system software are not modified" in installer
    assert "Optional media tools skipped" in installer
    assert installer.index("Read-WizardChecklist -Prompt 'Arr apps'") < installer.index("Read-WizardChoice -Prompt 'Optional media tools'")
    assert installer.index("Read-WizardChoice -Prompt 'Optional media tools'") < installer.index("Read-WizardValue -Prompt 'Install root'")


def test_suite_installer_uses_packaged_language_manifest() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    language_manifest = Path("emule_workspace/release_assets/emulebb/config/suite-languages.json")
    payload = json.loads(language_manifest.read_text(encoding="utf-8"))

    assert payload["schema"] == "emulebb.suite-languages.v1"
    assert {entry["key"] for entry in payload["languages"]} == {"english", "spanish", "italian", "portuguese"}
    assert "config\\suite-languages.json" in installer
    assert "function Get-LanguageOptions" in installer
    assert "$FallbackLanguageOptions" in installer


def test_suite_installer_acquires_selected_p2p_clients() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")

    # Acquisition + selection helpers exist.
    assert "function Install-P2pClient" in installer
    assert "function Get-SelectedP2pClientNames" in installer

    # Verified, named-root extraction into apps\<Root>, mirroring the Arr path.
    assert "Assert-FileHash -Path $archivePath -ExpectedSha256 $spec.Sha256" in installer
    assert "Install-VerifiedReleaseZip -Name $packageRoot" in installer

    # Unpinned clients refuse to install with a clear, actionable message.
    assert "has no pinned release yet" in installer

    # Wired into the main install flow, gated on selection (inert until activated).
    assert "Get-SelectedP2pClientNames -Config $script:SuiteConfig" in installer
    assert "Install-P2pClient -Name $name -Meta $script:P2pClientMetadata[$name]" in installer


def test_suite_initializer_applies_arr_content_language_profiles() -> None:
    script_path = Path("emule_workspace/release_assets/emulebb/scripts/Initialize-Suite.ps1")
    _assert_powershell_parse(Path.cwd() / script_path, cwd=Path.cwd())
    initialize_suite = script_path.read_text(encoding="utf-8")

    assert "function Set-ArrPreferredContentLanguage" in initialize_suite
    assert "function Set-ArrUiLanguage" in initialize_suite
    assert "function Wait-ArrContentLanguage" in initialize_suite
    assert "function Find-ArrLanguage" in initialize_suite
    assert "'italian' { return @('Italian') }" in initialize_suite
    assert '"$Url/$ApiPath/language"' in initialize_suite
    assert '"$Url/$ApiPath/qualityprofile"' in initialize_suite
    assert "Invoke-SuiteJsonApiList -Name \"$Name language list\"" in initialize_suite
    assert "$profile.language = $languageMatch" in initialize_suite
    assert "$profile.languages = @($ordered)" in initialize_suite
    assert '"$Url/$ApiPath/qualityprofile/$([int]$profile.id)"' in initialize_suite
    assert 'Invoke-StepWithRetry -Name "$display content language preference"' in initialize_suite
    assert "Set-ArrPreferredContentLanguage -Name $display" in initialize_suite
    assert "-Language ([string]$Config.language.arrContentLanguage)" in initialize_suite
    assert "Set-ArrUiLanguage -Name $display" in initialize_suite
    assert "-Language ([string]$Config.language.arrUiLanguage)" in initialize_suite
    assert "$suiteAppsManifest = Join-Path $Root 'config\\suite-apps.json'" in initialize_suite
    assert "SuiteAppsManifest = $suiteAppsManifest" in initialize_suite
    assert "-SuiteAppsManifest $suiteAppsManifest" in initialize_suite


def test_suite_arr_registration_defers_prowlarr_sync_until_all_apps_are_saved() -> None:
    script_path = Path("emule_workspace/release_assets/emulebb/scripts/Register-ArrStack.ps1")
    _assert_powershell_parse(Path.cwd() / script_path, cwd=Path.cwd())
    register_arr_stack = script_path.read_text(encoding="utf-8")

    assert "example http://127.0.0.1" not in register_arr_stack
    assert "example http://LAN-IP:4711" in register_arr_stack
    assert "example http://LAN-IP:9696" in register_arr_stack
    assert "example http://LAN-IP:$($ArrTargetPorts[$Target])" in register_arr_stack
    assert "Radarr = 7878" in register_arr_stack
    assert "Sonarr = 8989" in register_arr_stack
    assert "Lidarr = 8686" in register_arr_stack
    assert "Whisparr = 6969" in register_arr_stack
    assert "[switch]$SkipProwlarrSync" in register_arr_stack
    assert "[switch]$SyncProwlarrOnly" in register_arr_stack
    assert "function Get-ArrUrlPrompt" in register_arr_stack
    assert "function Get-HttpErrorDetail" in register_arr_stack
    assert "function Get-ExceptionMessage" in register_arr_stack
    assert "Get-ExceptionMessage -Exception $_.Exception" in register_arr_stack
    assert "Arr API key was rejected by $uri." in register_arr_stack
    assert "Copy the API key from Settings > General in the matching Radarr/Sonarr web UI" in register_arr_stack
    assert "function Test-ApiKeyRejectedError" in register_arr_stack
    assert "if (Test-ApiKeyRejectedError -Exception $_.Exception)" in register_arr_stack
    assert "function Read-RequiredSecretValue" in register_arr_stack
    assert "-AsSecureString" not in register_arr_stack
    assert "return Normalize-ArgumentValue -Value (Read-Host $Prompt)" in register_arr_stack
    assert "[scriptblock]$OnRetry = $null" in register_arr_stack
    assert "\nexit " not in register_arr_stack
    assert 'throw "$Name cancelled by user."' in register_arr_stack
    assert "$script:EmulebbBaseUrl = ''" in register_arr_stack
    assert "$script:ProwlarrUrl = ''" in register_arr_stack
    assert "$script:targetUrl = ''" in register_arr_stack
    assert "function Get-ProwlarrCommandFailureDetail" in register_arr_stack
    assert "Prowlarr application sync failed: {0}. {1}" in register_arr_stack
    assert "function Save-ProwlarrQbitClient" in register_arr_stack
    assert "Run-TargetWithRetry -Name 'Prowlarr download client registration'" in register_arr_stack
    assert "/api/v1/downloadclient?forceSave=true" in register_arr_stack
    assert "[switch]$VerifyIndexerOnly" in register_arr_stack
    assert "function Get-ArrProwlarrIndexerName" in register_arr_stack
    assert "function Get-ExistingArrIndexers" in register_arr_stack
    assert "function Get-ArrIndexerCategories" in register_arr_stack
    assert "[string]$SuiteAppsManifest" in register_arr_stack
    assert "SuiteAppsManifest does not exist" in register_arr_stack
    assert "function Initialize-ArrIndexerCategories" in register_arr_stack
    assert "Import-SuiteAppManifest.ps1" in register_arr_stack
    assert "Read-SuiteAppManifest -Path $manifestPath" in register_arr_stack
    assert "config\\suite-apps.json" in register_arr_stack
    assert "Prowlarr indexer '$Name' is not registered. Run Register-Prowlarr.ps1 first" in register_arr_stack
    assert "Prowlarr URL for indexer verification (example http://LAN-IP:9696)" in register_arr_stack
    assert "First-time setup or repair: press Enter to register. Choose U only to remove this Arr integration." in register_arr_stack
    assert "Run-TargetWithRetry -Name \"$Target indexer verification\"" in register_arr_stack
    assert "/api/v3/indexer?forceSave=true" not in register_arr_stack
    assert "/api/v3/indexer/{0}?forceSave=true" not in register_arr_stack
    assert "Set-ProviderField -Provider $payload -Name 'syncCategories' -Value (Get-ArrIndexerCategories -Kind $Kind) -Optional" in register_arr_stack
    assert "'radarr' { return ,@(2000) }" not in register_arr_stack
    assert "Read-RequiredSecretValue -Prompt 'Prowlarr API key' -Value $script:ProwlarrApiKey -Name 'ProwlarrApiKey'" in register_arr_stack
    assert "Read-RequiredSecretValue -Prompt 'eMuleBB API key' -Value $script:EmulebbApiKey -Name 'EmulebbApiKey'" in register_arr_stack
    assert "Read-RequiredSecretValue -Prompt \"$Target API key\" -Value $script:targetApiKey -Name (\"${Target}ApiKey\")" in register_arr_stack
    assert "$script:EmulebbBaseUrl = Normalize-HttpBaseUrl -Value (Read-RequiredValue" in register_arr_stack
    assert "$script:targetUrl = Normalize-HttpBaseUrl -Value (Read-RequiredValue" in register_arr_stack
    assert "function Get-TargetUrlParameter" in register_arr_stack
    assert "'Radarr' { return Normalize-ArgumentValue -Value $RadarrUrl }" in register_arr_stack
    assert "$script:targetUrl = Normalize-HttpBaseUrl -Value (Read-RequiredValue" in register_arr_stack
    assert register_arr_stack.index("Run-TargetWithRetry -Name 'eMuleBB category registration'") < register_arr_stack.index("$script:EmulebbBaseUrl = Normalize-HttpBaseUrl -Value (Read-RequiredValue")
    assert register_arr_stack.index("Run-TargetWithRetry -Name \"Prowlarr $Target application registration\"") < register_arr_stack.index("Run-TargetWithRetry -Name 'Prowlarr download client registration'")
    assert register_arr_stack.index("Run-TargetWithRetry -Name (\"$Target download client {0}\"") < register_arr_stack.index("$script:targetUrl = Normalize-HttpBaseUrl -Value (Read-RequiredValue", register_arr_stack.index("Run-TargetWithRetry -Name (\"$Target download client {0}\""))
    assert "function Set-EmuleQbitCredential" in register_arr_stack
    # Prefer the qBittorrent API-key (bearer) field when the Arr schema exposes it.
    assert "[string]$field.name -match '(?i)apikey'" in register_arr_stack
    assert "Set-ProviderField -Provider $Provider -Name $apiKeyFieldName -Value $EmuleApiKey" in register_arr_stack
    # Fall back to the username/password login flow otherwise.
    assert "Set-ProviderField -Provider $Provider -Name 'password' -Value $EmuleApiKey" in register_arr_stack
    # Both the Arr and Prowlarr qBittorrent client savers use the shared helper.
    assert register_arr_stack.count("Set-EmuleQbitCredential -Provider $payload -EmuleApiKey $EmuleApiKey") == 2
    assert "Set-ProviderField -Provider $payload -Name 'apiKey' -Value $ArrKey" in register_arr_stack
    assert "if ($SyncProwlarrOnly)" in register_arr_stack
    assert "if ($VerifyIndexerOnly)" in register_arr_stack
    assert "Read-RequiredValue -Prompt 'Prowlarr URL for application sync (example http://LAN-IP:9696)'" in register_arr_stack
    assert "throw 'ProwlarrUrl is required for -SyncProwlarrOnly.'" not in register_arr_stack
    assert "ProwlarrUrl is required for Arr registration." in register_arr_stack
    assert register_arr_stack.index("if ($SyncProwlarrOnly)") < register_arr_stack.index("$ProwlarrUrl = Read-OptionalValue")
    assert register_arr_stack.index("if ($VerifyIndexerOnly)") < register_arr_stack.index("$ProwlarrUrl = Read-OptionalValue")
    assert "if ($ProwlarrUrl -and -not $SkipProwlarrSync)" in register_arr_stack
    assert "$ProwlarrUrl = Normalize-HttpBaseUrl -Value $ProwlarrUrl" not in register_arr_stack
    assert register_arr_stack.index("if ($ProwlarrUrl -and -not $SkipProwlarrSync)") < register_arr_stack.rindex("Run-TargetWithRetry -Name \"$Target indexer verification\"")


def test_suite_prowlarr_registration_requires_and_passes_api_keys() -> None:
    script_path = Path("emule_workspace/release_assets/emulebb/scripts/Register-Prowlarr.ps1")
    _assert_powershell_parse(Path.cwd() / script_path, cwd=Path.cwd())
    register_prowlarr = script_path.read_text(encoding="utf-8")

    assert "example http://127.0.0.1" not in register_prowlarr
    assert "example http://LAN-IP:9696" in register_prowlarr
    assert "example http://LAN-IP:4711" in register_prowlarr
    assert "function Get-HttpErrorDetail" in register_prowlarr
    assert "function Get-ExceptionMessage" in register_prowlarr
    assert "Get-ExceptionMessage -Exception $_.Exception" in register_prowlarr
    assert "Prowlarr API key was rejected by $uri." in register_prowlarr
    assert "Copy the API key from Settings > General in the Prowlarr web UI" in register_prowlarr
    assert "function Read-RequiredSecretValue" in register_prowlarr
    assert "-AsSecureString" not in register_prowlarr
    assert "return Normalize-ArgumentValue -Value (Read-Host $Prompt)" in register_prowlarr
    assert "\nexit " not in register_prowlarr
    assert "if ($NoRetry) {" in register_prowlarr
    assert 'throw "$Action cancelled by user."' in register_prowlarr
    assert "Read-RequiredSecretValue -Prompt 'Prowlarr API key' -Value $ProwlarrApiKey -Name 'ProwlarrApiKey'" in register_prowlarr
    assert "Read-RequiredSecretValue -Prompt 'eMuleBB API key' -Value $EmulebbApiKey -Name 'EmulebbApiKey'" in register_prowlarr
    assert "$EmulebbBaseUrl = ''" in register_prowlarr
    assert "$EmulebbApiKey = ''" in register_prowlarr
    assert "Set-ProviderField -Provider $payload -Name 'apiKey' -Value $TorznabApiKey" in register_prowlarr
    assert "First-time setup or repair: press Enter to register. Choose U only to remove this Prowlarr indexer." in register_prowlarr


def test_suite_installer_wizard_receives_config_by_reference() -> None:
    # Regression: the interactive wizard must take the [ordered] suite config as an
    # IDictionary, NOT a [hashtable]. A [hashtable] parameter coerces the
    # OrderedDictionary into a shallow copy, so the wizard's top-level writes
    # (bundle, selectedApps, allowRemoteServiceBind, portBlockStart, dependencyChannel)
    # are dropped and the install silently reverts to the -Bundle default (Full),
    # while nested edits (services/p2p/language) persist via shared references.
    installer = INSTALLER.read_text(encoding="utf-8")
    wizard = installer.split("function Invoke-InstallWizard", 1)[1].split("\nfunction ", 1)[0]
    assert "param([System.Collections.IDictionary]$Config)" in wizard
    assert "param([hashtable]$Config)" not in wizard


def test_suite_windows_system_helpers_explain_admin_requirement() -> None:
    for script_name, expected in {
        "Enable-LongPaths.ps1": "Windows long-path enable requires an elevated PowerShell window.",
        "Repair-Firewall.ps1": "Windows Firewall repair requires an elevated PowerShell window.",
        "Set-DefenderExclusions.ps1": "Microsoft Defender exclusion updates require an elevated PowerShell window.",
    }.items():
        script_path = Path("emule_workspace/release_assets/emulebb/scripts") / script_name
        _assert_powershell_parse(Path.cwd() / script_path, cwd=Path.cwd())
        script = script_path.read_text(encoding="utf-8")
        assert "function Assert-Administrator" in script
        assert "WindowsBuiltInRole]::Administrator" in script
        assert "Run as administrator" in script
        assert expected in script


@pytest.mark.parametrize("allow_mismatch", [False, True])
def test_suite_bootstrapper_validates_explicit_suite_scripts_bundle_version(allow_mismatch: bool) -> None:
    repo_root = Path.cwd()
    bootstrapper_path = (repo_root / BOOTSTRAPPER).resolve()
    override_arg = "-AllowSuiteScriptsVersionMismatch" if allow_mismatch else ""
    command = rf"""
function Invoke-RestMethod {{
    param([string]$Uri, [hashtable]$Headers)
    if ($Uri -ne 'https://api.github.com/repos/emulebb/emulebb/releases/tags/emulebb-v0.7.3-rc.2') {{
        throw "Unexpected release URI: $Uri"
    }}
    return [pscustomobject]@{{
        tag_name = 'emulebb-v0.7.3-rc.2'
        draft = $false
        prerelease = $true
        assets = @(
            [pscustomobject]@{{
                name = 'emulebb-0.7.3-rc.2-x64.zip'
                browser_download_url = 'https://example.invalid/emulebb-0.7.3-rc.2-x64.zip'
            }},
            [pscustomobject]@{{
                name = 'emulebb-0.7.3-rc.2-x64.manifest.json'
                browser_download_url = 'https://example.invalid/emulebb-0.7.3-rc.2-x64.manifest.json'
            }},
            [pscustomobject]@{{
                name = 'automation-examples-0.7.3-rc.2.zip'
                browser_download_url = 'https://example.invalid/automation-examples-0.7.3-rc.2.zip'
            }},
            [pscustomobject]@{{
                name = 'automation-examples-0.7.3-rc.2.manifest.json'
                browser_download_url = 'https://example.invalid/automation-examples-0.7.3-rc.2.manifest.json'
            }}
        )
    }}
}}
& '{bootstrapper_path}' -Version 0.7.3-rc.2 -Platform x64 -Bundle Core -DryRun -NoStart -SuiteScriptsZip https://example.invalid/suite-scripts-0.7.3-rc.1.zip -SuiteScriptsManifest https://example.invalid/suite-scripts-0.7.3-rc.1.manifest.json {override_arg}
"""

    powershell = shutil.which("powershell")
    assert powershell is not None
    completed = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    if allow_mismatch:
        assert completed.returncode == 0
        assert "-SuiteScriptsZip https://example.invalid/suite-scripts-0.7.3-rc.1.zip" in completed.stdout
    else:
        assert completed.returncode != 0
        assert "Suite scripts bundle version 0.7.3-rc.1 does not match" in completed.stdout
        assert "eMuleBB package version 0.7.3-rc.2" in completed.stdout


@pytest.mark.parametrize(
    "version_arg",
    [
        "0.7.3-nightly.20260524.ae562c1",
        "emulebb-nightly-20260524-ae562c1",
    ],
)
def test_suite_bootstrapper_resolves_nightly_release_assets(version_arg: str) -> None:
    repo_root = Path.cwd()
    bootstrapper_path = (repo_root / BOOTSTRAPPER).resolve()
    command = rf"""
function Invoke-RestMethod {{
    param([string]$Uri, [hashtable]$Headers)
    if ($Uri -ne 'https://api.github.com/repos/emulebb/emulebb/releases/tags/emulebb-nightly-20260524-ae562c1') {{
        throw "Unexpected release URI: $Uri"
    }}
    return [pscustomobject]@{{
        tag_name = 'emulebb-nightly-20260524-ae562c1'
        draft = $false
        prerelease = $true
        assets = @(
            [pscustomobject]@{{
                name = 'emulebb-0.7.3-nightly.20260524.ae562c1-diagnostics-x64.zip'
                browser_download_url = 'https://example.invalid/emulebb-0.7.3-nightly.20260524.ae562c1-diagnostics-x64.zip'
            }},
            [pscustomobject]@{{
                name = 'emulebb-0.7.3-nightly.20260524.ae562c1-diagnostics-x64.manifest.json'
                browser_download_url = 'https://example.invalid/emulebb-0.7.3-nightly.20260524.ae562c1-diagnostics-x64.manifest.json'
            }},
            [pscustomobject]@{{
                name = 'emulebb-0.7.3-nightly.20260524.ae562c1-x64.zip'
                browser_download_url = 'https://example.invalid/emulebb-0.7.3-nightly.20260524.ae562c1-x64.zip'
            }},
            [pscustomobject]@{{
                name = 'emulebb-0.7.3-nightly.20260524.ae562c1-x64.manifest.json'
                browser_download_url = 'https://example.invalid/emulebb-0.7.3-nightly.20260524.ae562c1-x64.manifest.json'
            }},
            [pscustomobject]@{{
                name = 'automation-examples-0.7.3-nightly.20260524.ae562c1.zip'
                browser_download_url = 'https://example.invalid/automation-examples-0.7.3-nightly.20260524.ae562c1.zip'
            }},
            [pscustomobject]@{{
                name = 'automation-examples-0.7.3-nightly.20260524.ae562c1.manifest.json'
                browser_download_url = 'https://example.invalid/automation-examples-0.7.3-nightly.20260524.ae562c1.manifest.json'
            }}
        )
    }}
}}
& '{bootstrapper_path}' -Version '{version_arg}' -Platform x64 -Bundle Core -DryRun -NoStart
"""

    completed = _run_powershell(["-Command", command], cwd=repo_root)

    assert "Resolved release emulebb-nightly-20260524-ae562c1 for x64" in completed.stdout
    assert "emulebb-0.7.3-nightly.20260524.ae562c1-x64.zip" in completed.stdout
    assert "emulebb-0.7.3-nightly.20260524.ae562c1-diagnostics-x64.zip" not in completed.stdout
    assert (
        "-ReleaseBaseUrl https://github.com/emulebb/emulebb/releases/download/"
        "emulebb-nightly-20260524-ae562c1"
    ) in completed.stdout


def test_suite_bootstrapper_auto_detects_platform_when_omitted() -> None:
    repo_root = Path.cwd()
    bootstrapper_path = (repo_root / BOOTSTRAPPER).resolve()
    command = rf"""
$env:PROCESSOR_ARCHITECTURE = 'AMD64'
Remove-Item Env:\PROCESSOR_ARCHITEW6432 -ErrorAction SilentlyContinue
function Invoke-RestMethod {{
    param([string]$Uri, [hashtable]$Headers)
    if ($Uri -ne 'https://api.github.com/repos/emulebb/emulebb/releases/tags/emulebb-nightly-20260524-ae562c1') {{
        throw "Unexpected release URI: $Uri"
    }}
    return [pscustomobject]@{{
        tag_name = 'emulebb-nightly-20260524-ae562c1'
        draft = $false
        prerelease = $true
        assets = @(
            [pscustomobject]@{{
                name = 'emulebb-0.7.3-nightly.20260524.ae562c1-x64.zip'
                browser_download_url = 'https://example.invalid/emulebb-0.7.3-nightly.20260524.ae562c1-x64.zip'
            }},
            [pscustomobject]@{{
                name = 'emulebb-0.7.3-nightly.20260524.ae562c1-x64.manifest.json'
                browser_download_url = 'https://example.invalid/emulebb-0.7.3-nightly.20260524.ae562c1-x64.manifest.json'
            }},
            [pscustomobject]@{{
                name = 'automation-examples-0.7.3-nightly.20260524.ae562c1.zip'
                browser_download_url = 'https://example.invalid/automation-examples-0.7.3-nightly.20260524.ae562c1.zip'
            }},
            [pscustomobject]@{{
                name = 'automation-examples-0.7.3-nightly.20260524.ae562c1.manifest.json'
                browser_download_url = 'https://example.invalid/automation-examples-0.7.3-nightly.20260524.ae562c1.manifest.json'
            }}
        )
    }}
}}
& '{bootstrapper_path}' -Version 'emulebb-nightly-20260524-ae562c1' -Bundle Core -DryRun -NoStart
"""

    completed = _run_powershell(["-Command", command], cwd=repo_root)

    assert "Resolved release emulebb-nightly-20260524-ae562c1 for x64" in completed.stdout
    assert "emulebb-0.7.3-nightly.20260524.ae562c1-x64.zip" in completed.stdout


def test_suite_bootstrapper_prefers_release_candidate_over_nightly_by_default() -> None:
    repo_root = Path.cwd()
    bootstrapper_path = (repo_root / BOOTSTRAPPER).resolve()
    command = rf"""
function Invoke-RestMethod {{
    param([string]$Uri, [hashtable]$Headers)
    if ($Uri -ne 'https://api.github.com/repos/emulebb/emulebb/releases') {{
        throw "Unexpected release URI: $Uri"
    }}
    return @(
        [pscustomobject]@{{
            tag_name = 'emulebb-nightly-20260610-abcdef0'
            draft = $false
            prerelease = $true
            assets = @(
                [pscustomobject]@{{
                    name = 'emulebb-0.7.3-nightly.20260610.abcdef0-x64.zip'
                    browser_download_url = 'https://example.invalid/emulebb-0.7.3-nightly.20260610.abcdef0-x64.zip'
                }},
                [pscustomobject]@{{
                    name = 'emulebb-0.7.3-nightly.20260610.abcdef0-x64.manifest.json'
                    browser_download_url = 'https://example.invalid/emulebb-0.7.3-nightly.20260610.abcdef0-x64.manifest.json'
                }}
            )
        }},
        [pscustomobject]@{{
            tag_name = 'emulebb-v0.7.3-rc.2'
            draft = $false
            prerelease = $true
            assets = @(
                [pscustomobject]@{{
                    name = 'emulebb-0.7.3-rc.2-x64.zip'
                    browser_download_url = 'https://example.invalid/emulebb-0.7.3-rc.2-x64.zip'
                }},
                [pscustomobject]@{{
                    name = 'emulebb-0.7.3-rc.2-x64.manifest.json'
                    browser_download_url = 'https://example.invalid/emulebb-0.7.3-rc.2-x64.manifest.json'
                }}
            )
        }}
    )
}}
& '{bootstrapper_path}' -Platform x64 -Bundle Core -DryRun -NoStart
"""

    completed = _run_powershell(["-Command", command], cwd=repo_root)

    assert "Resolved release emulebb-v0.7.3-rc.2 for x64" in completed.stdout
    assert "emulebb-nightly-20260610-abcdef0" not in completed.stdout


def test_suite_bootstrapper_requires_opt_in_for_nightly_only_releases() -> None:
    repo_root = Path.cwd()
    bootstrapper_path = (repo_root / BOOTSTRAPPER).resolve()
    powershell = shutil.which("powershell")
    assert powershell is not None
    command = rf"""
function Invoke-RestMethod {{
    param([string]$Uri, [hashtable]$Headers)
    if ($Uri -ne 'https://api.github.com/repos/emulebb/emulebb/releases') {{
        throw "Unexpected release URI: $Uri"
    }}
    return @(
        [pscustomobject]@{{
            tag_name = 'emulebb-nightly-20260610-abcdef0'
            draft = $false
            prerelease = $true
            assets = @()
        }}
    )
}}
& '{bootstrapper_path}' -Platform x64 -Bundle Core -DryRun -NoStart
"""

    completed = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert completed.returncode != 0
    output = " ".join(completed.stdout.split())
    assert "Pass -IncludeNightly to install the latest nightly build." in output


@pytest.mark.parametrize("disable_bundle", [False, True])
def test_suite_bootstrapper_discovers_release_suite_scripts_bundle(tmp_path: Path, disable_bundle: bool) -> None:
    repo_root = Path.cwd()
    release_root = tmp_path / "release"
    captured = tmp_path / "captured-suite-scripts.json"
    installer_payload = f"""#Requires -Version 5.1
param(
    [string]$Bundle,
    [string]$InstallRoot,
    [string]$Version,
    [string]$Platform,
    [string]$ReleaseBaseUrl,
    [string]$SuiteScriptsZip,
    [string]$SuiteScriptsManifest,
    [string]$AutomationExamplesZip,
    [string]$AutomationExamplesManifest,
    [switch]$NoStart
)
@{{
    hasSuiteScriptsZip = $PSBoundParameters.ContainsKey('SuiteScriptsZip')
    hasSuiteScriptsManifest = $PSBoundParameters.ContainsKey('SuiteScriptsManifest')
    suiteScriptsZip = $SuiteScriptsZip
    suiteScriptsManifest = $SuiteScriptsManifest
}} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath '{captured.as_posix()}'
""".encode("utf-8")
    version = "0.7.3-nightly.20260604.5169162"
    package_zip = release_root / f"emulebb-{version}-x64.zip"
    manifest = release_root / f"emulebb-{version}-x64.manifest.json"
    suite_install_fixtures.write_zip(
        package_zip,
        {
            "eMuleBB/emulebb.exe": b"exe\n",
            "eMuleBB/scripts/Install-eMuleBBSuite.ps1": installer_payload,
        },
    )
    suite_install_fixtures.write_manifest(manifest, package_zip)
    bootstrapper_path = (repo_root / BOOTSTRAPPER).resolve()
    disable_arg = "-NoSuiteScriptsBundle" if disable_bundle else ""
    command = rf"""
function Invoke-RestMethod {{
    param([string]$Uri, [hashtable]$Headers)
    if ($Uri -ne 'https://api.github.com/repos/emulebb/emulebb/releases/tags/emulebb-nightly-20260604-5169162') {{
        throw "Unexpected release URI: $Uri"
    }}
    return [pscustomobject]@{{
        tag_name = 'emulebb-nightly-20260604-5169162'
        draft = $false
        prerelease = $true
        assets = @(
            [pscustomobject]@{{
                name = 'emulebb-{version}-x64.zip'
                browser_download_url = 'https://example.invalid/emulebb-{version}-x64.zip'
            }},
            [pscustomobject]@{{
                name = 'emulebb-{version}-x64.manifest.json'
                browser_download_url = 'https://example.invalid/emulebb-{version}-x64.manifest.json'
            }},
            [pscustomobject]@{{
                name = 'suite-scripts-{version}.zip'
                browser_download_url = 'https://example.invalid/suite-scripts-{version}.zip'
            }},
            [pscustomobject]@{{
                name = 'suite-scripts-{version}.manifest.json'
                browser_download_url = 'https://example.invalid/suite-scripts-{version}.manifest.json'
            }},
            [pscustomobject]@{{
                name = 'automation-examples-{version}.zip'
                browser_download_url = 'https://example.invalid/automation-examples-{version}.zip'
            }},
            [pscustomobject]@{{
                name = 'automation-examples-{version}.manifest.json'
                browser_download_url = 'https://example.invalid/automation-examples-{version}.manifest.json'
            }}
        )
    }}
}}
function Invoke-WebRequest {{
    param([switch]$UseBasicParsing, [string]$Uri, [string]$OutFile, [hashtable]$Headers)
    if ($Uri.EndsWith('emulebb-{version}-x64.manifest.json')) {{
        Copy-Item -Force -LiteralPath '{manifest.as_posix()}' -Destination $OutFile
        return
    }}
    if ($Uri.EndsWith('emulebb-{version}-x64.zip')) {{
        Copy-Item -Force -LiteralPath '{package_zip.as_posix()}' -Destination $OutFile
        return
    }}
    throw "Unexpected download URI: $Uri"
}}
& '{bootstrapper_path}' -Version 'emulebb-nightly-20260604-5169162' -Platform x64 -Bundle Core -NoStart {disable_arg}
"""

    completed = _run_powershell(["-Command", command], cwd=repo_root)
    captured_payload = json.loads(captured.read_text(encoding="utf-8-sig"))

    if disable_bundle:
        assert "Resolved suite scripts bundle" not in completed.stdout
        assert captured_payload["hasSuiteScriptsZip"] is False
        assert captured_payload["hasSuiteScriptsManifest"] is False
    else:
        assert "Resolved suite scripts bundle https://example.invalid/suite-scripts-" in completed.stdout
        assert captured_payload["hasSuiteScriptsZip"] is True
        assert captured_payload["hasSuiteScriptsManifest"] is True
        assert captured_payload["suiteScriptsZip"] == f"https://example.invalid/suite-scripts-{version}.zip"
        assert (
            captured_payload["suiteScriptsManifest"]
            == f"https://example.invalid/suite-scripts-{version}.manifest.json"
        )


def test_suite_bootstrapper_smokes_release_package_and_suite_scripts_assets(tmp_path: Path) -> None:
    repo_root = Path.cwd()
    release_root = tmp_path / "release"
    package_root = tmp_path / "staging" / "eMuleBB"
    captured = tmp_path / "captured-release-assets.json"
    installer_payload = f"""#Requires -Version 5.1
param(
    [string]$Bundle,
    [string]$Version,
    [string]$Platform,
    [string]$ReleaseBaseUrl,
    [string]$SuiteScriptsZip,
    [string]$SuiteScriptsManifest,
    [string]$AutomationExamplesZip,
    [string]$AutomationExamplesManifest,
    [switch]$NoStart
)
@{{
    bundle = $Bundle
    version = $Version
    platform = $Platform
    releaseBaseUrl = $ReleaseBaseUrl
    suiteScriptsZip = $SuiteScriptsZip
    suiteScriptsManifest = $SuiteScriptsManifest
    automationExamplesZip = $AutomationExamplesZip
    automationExamplesManifest = $AutomationExamplesManifest
}} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath '{captured.as_posix()}'
""".encode("utf-8")
    package_entries = {
        "eMuleBB/emulebb.exe": b"exe\n",
        **suite_install_fixtures.runtime_script_entries(installer_payload=installer_payload),
    }
    for entry_name, payload in package_entries.items():
        if not entry_name.startswith("eMuleBB/"):
            continue
        target = package_root / entry_name.removeprefix("eMuleBB/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    version = "0.7.3-nightly.20260604.5169162"
    package_zip = release_root / f"emulebb-{version}-x64.zip"
    manifest = release_root / f"emulebb-{version}-x64.manifest.json"
    suite_install_fixtures.write_zip(package_zip, package_entries)
    suite_install_fixtures.write_manifest(manifest, package_zip)
    suite_asset, suite_manifest, suite_digest = release._write_suite_scripts_bundle_asset(
        package_root=package_root,
        release_root=release_root,
        release_version=version,
    )
    automation_source_root = (
        repo_root
        / "emule_workspace"
        / "release_assets"
        / release.EMULEBB_AUTOMATION_EXAMPLE_ASSET_ROOT_NAME
    )
    automation_asset, automation_manifest, automation_digest = release._write_automation_examples_asset(
        build_repo_root=repo_root,
        release_root=release_root,
        release_version=version,
    )
    suite_manifest_payload = json.loads(suite_manifest.read_text(encoding="utf-8"))
    automation_manifest_payload = json.loads(automation_manifest.read_text(encoding="utf-8"))

    bootstrapper_path = (repo_root / BOOTSTRAPPER).resolve()
    command = rf"""
function Invoke-RestMethod {{
    param([string]$Uri, [hashtable]$Headers)
    if ($Uri -ne 'https://api.github.com/repos/emulebb/emulebb/releases/tags/emulebb-nightly-20260604-5169162') {{
        throw "Unexpected release URI: $Uri"
    }}
    return [pscustomobject]@{{
        tag_name = 'emulebb-nightly-20260604-5169162'
        draft = $false
        prerelease = $true
        assets = @(
            [pscustomobject]@{{
                name = '{package_zip.name}'
                browser_download_url = 'https://example.invalid/{package_zip.name}'
            }},
            [pscustomobject]@{{
                name = '{manifest.name}'
                browser_download_url = 'https://example.invalid/{manifest.name}'
            }},
            [pscustomobject]@{{
                name = '{suite_asset.name}'
                browser_download_url = 'https://example.invalid/{suite_asset.name}'
            }},
            [pscustomobject]@{{
                name = '{suite_manifest.name}'
                browser_download_url = 'https://example.invalid/{suite_manifest.name}'
            }},
            [pscustomobject]@{{
                name = '{automation_asset.name}'
                browser_download_url = 'https://example.invalid/{automation_asset.name}'
            }},
            [pscustomobject]@{{
                name = '{automation_manifest.name}'
                browser_download_url = 'https://example.invalid/{automation_manifest.name}'
            }}
        )
    }}
}}
function Invoke-WebRequest {{
    param([switch]$UseBasicParsing, [string]$Uri, [string]$OutFile, [hashtable]$Headers)
    if ($Uri.EndsWith('{manifest.name}')) {{
        Copy-Item -Force -LiteralPath '{manifest.as_posix()}' -Destination $OutFile
        return
    }}
    if ($Uri.EndsWith('{package_zip.name}')) {{
        Copy-Item -Force -LiteralPath '{package_zip.as_posix()}' -Destination $OutFile
        return
    }}
    throw "Unexpected download URI: $Uri"
}}
& '{bootstrapper_path}' -Version 'emulebb-nightly-20260604-5169162' -Platform x64 -Bundle Core -NoStart
"""

    completed = _run_powershell(["-Command", command], cwd=repo_root)
    captured_payload = json.loads(captured.read_text(encoding="utf-8-sig"))

    assert "Resolved suite scripts bundle https://example.invalid/suite-scripts-" in completed.stdout
    assert "Resolved automation examples bundle https://example.invalid/automation-examples-" in completed.stdout
    assert suite_manifest_payload["schema"] == "emulebb.suite-scripts-manifest.v1"
    assert suite_manifest_payload["sha256"] == suite_digest
    assert automation_source_root.is_dir()
    assert automation_manifest_payload["schema"] == "emulebb.automation-examples-manifest.v1"
    assert automation_manifest_payload["sha256"] == automation_digest
    assert captured_payload["version"] == version
    assert captured_payload["suiteScriptsZip"] == f"https://example.invalid/{suite_asset.name}"
    assert captured_payload["suiteScriptsManifest"] == f"https://example.invalid/{suite_manifest.name}"
    assert captured_payload["automationExamplesZip"] == f"https://example.invalid/{automation_asset.name}"
    assert captured_payload["automationExamplesManifest"] == f"https://example.invalid/{automation_manifest.name}"


@pytest.mark.parametrize("missing_asset", ["zip", "manifest"])
def test_suite_bootstrapper_rejects_incomplete_release_suite_scripts_bundle(missing_asset: str) -> None:
    repo_root = Path.cwd()
    bootstrapper_path = (repo_root / BOOTSTRAPPER).resolve()
    version = "0.7.3-nightly.20260604.5169162"
    suite_assets = []
    if missing_asset != "zip":
        suite_assets.append("""
            [pscustomobject]@{
                name = 'suite-scripts-0.7.3-nightly.20260604.5169162.zip'
                browser_download_url = 'https://example.invalid/suite-scripts-0.7.3-nightly.20260604.5169162.zip'
            }""")
    if missing_asset != "manifest":
        suite_assets.append("""
            [pscustomobject]@{
                name = 'suite-scripts-0.7.3-nightly.20260604.5169162.manifest.json'
                browser_download_url = 'https://example.invalid/suite-scripts-0.7.3-nightly.20260604.5169162.manifest.json'
            }""")
    suite_asset_text = ",\n".join(suite_assets)
    command = rf"""
function Invoke-RestMethod {{
    param([string]$Uri, [hashtable]$Headers)
    if ($Uri -ne 'https://api.github.com/repos/emulebb/emulebb/releases/tags/emulebb-nightly-20260604-5169162') {{
        throw "Unexpected release URI: $Uri"
    }}
    return [pscustomobject]@{{
        tag_name = 'emulebb-nightly-20260604-5169162'
        draft = $false
        prerelease = $true
        assets = @(
            [pscustomobject]@{{
                name = 'emulebb-{version}-x64.zip'
                browser_download_url = 'https://example.invalid/emulebb-{version}-x64.zip'
            }},
            [pscustomobject]@{{
                name = 'emulebb-{version}-x64.manifest.json'
                browser_download_url = 'https://example.invalid/emulebb-{version}-x64.manifest.json'
            }},
            [pscustomobject]@{{
                name = 'automation-examples-{version}.zip'
                browser_download_url = 'https://example.invalid/automation-examples-{version}.zip'
            }},
            [pscustomobject]@{{
                name = 'automation-examples-{version}.manifest.json'
                browser_download_url = 'https://example.invalid/automation-examples-{version}.manifest.json'
            }},
{suite_asset_text}
        )
    }}
}}
& '{bootstrapper_path}' -Version 'emulebb-nightly-20260604-5169162' -Platform x64 -Bundle Core -DryRun -NoStart
"""
    powershell = shutil.which("powershell")
    assert powershell is not None
    completed = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert completed.returncode != 0
    assert "has incomplete suite scripts bundle assets" in " ".join(completed.stdout.split())


@pytest.mark.parametrize("pass_install_root", [False, True])
def test_suite_bootstrapper_hands_resolved_install_root_to_installer(tmp_path: Path, pass_install_root: bool) -> None:
    repo_root = Path.cwd()
    release_root = tmp_path / "release"
    captured = tmp_path / "captured-install-root.json"
    installer_payload = f"""#Requires -Version 5.1
param(
    [string]$Bundle,
    [string]$InstallRoot,
    [string]$Version,
    [string]$Platform,
    [string]$Language,
    [string]$UiLanguage,
    [string]$EmulebbPackageZip,
    [string]$EmulebbPackageManifest,
    [string]$AutomationExamplesZip,
    [string]$AutomationExamplesManifest,
    [switch]$NoStart
)
@{{
    hasInstallRoot = $PSBoundParameters.ContainsKey('InstallRoot')
    installRoot = $InstallRoot
}} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath '{captured.as_posix()}'
""".encode("utf-8")
    package_zip = release_root / "emulebb-0.7.3-local.20260604-x64.zip"
    package_manifest = release_root / "emulebb-0.7.3-local.20260604-x64.manifest.json"
    suite_install_fixtures.write_zip(
        package_zip,
        {
            "eMuleBB/emulebb.exe": b"exe\n",
            "eMuleBB/scripts/Install-eMuleBBSuite.ps1": installer_payload,
        },
    )
    suite_install_fixtures.write_manifest(package_manifest, package_zip)
    _write_automation_examples_component(release_root, "0.7.3-local.20260604")

    bootstrapper_path = (repo_root / BOOTSTRAPPER).resolve()
    command = [
        "-File",
        str(bootstrapper_path),
        "-Bundle",
        "Core",
        "-NoStart",
        "-EmulebbPackageZip",
        str(package_zip),
        "-EmulebbPackageManifest",
        str(package_manifest),
    ]
    if pass_install_root:
        command.extend(["-InstallRoot", r"C:\SuiteSample"])

    _run_powershell(command, cwd=repo_root)
    captured_payload = json.loads(captured.read_text(encoding="utf-8-sig"))

    assert captured_payload["hasInstallRoot"] is True
    if pass_install_root:
        assert captured_payload["installRoot"] == r"C:\SuiteSample"
    else:
        assert captured_payload["installRoot"] == r"C:\eMuleBBSuite"


def test_suite_bootstrapper_discovers_adjacent_local_suite_scripts_bundle(tmp_path: Path) -> None:
    repo_root = Path.cwd()
    release_root = tmp_path / "release"
    captured = tmp_path / "captured-local-suite-scripts.json"
    installer_payload = f"""#Requires -Version 5.1
param(
    [string]$Bundle,
    [string]$InstallRoot,
    [string]$Version,
    [string]$Platform,
    [string]$EmulebbPackageZip,
    [string]$EmulebbPackageManifest,
    [string]$SuiteScriptsZip,
    [string]$SuiteScriptsManifest,
    [string]$AutomationExamplesZip,
    [string]$AutomationExamplesManifest,
    [switch]$NoStart
)
@{{
    suiteScriptsZip = $SuiteScriptsZip
    suiteScriptsManifest = $SuiteScriptsManifest
}} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath '{captured.as_posix()}'
""".encode("utf-8")
    package_zip = release_root / "emulebb-0.7.3-local.20260604-x64.zip"
    package_manifest = release_root / "emulebb-0.7.3-local.20260604-x64.manifest.json"
    suite_scripts_zip = release_root / "suite-scripts-0.7.3-local.20260604.zip"
    suite_scripts_manifest = release_root / "suite-scripts-0.7.3-local.20260604.manifest.json"
    suite_install_fixtures.write_zip(
        package_zip,
        {
            "eMuleBB/emulebb.exe": b"exe\n",
            "eMuleBB/scripts/Install-eMuleBBSuite.ps1": installer_payload,
        },
    )
    suite_install_fixtures.write_manifest(package_manifest, package_zip)
    suite_install_fixtures.write_zip(suite_scripts_zip, {"eMuleBB/scripts/Start-Suite.ps1": b"#Requires -Version 5.1\n"})
    suite_install_fixtures.write_manifest(suite_scripts_manifest, suite_scripts_zip)
    _write_automation_examples_component(release_root, "0.7.3-local.20260604")
    bootstrapper_path = (repo_root / BOOTSTRAPPER).resolve()
    command = rf"""
function Invoke-RestMethod {{
    throw 'GitHub API should not be called for local package overrides.'
}}
function Invoke-WebRequest {{
    throw 'Downloads should not be used for local package overrides.'
}}
& '{bootstrapper_path}' -Bundle Core -NoStart -EmulebbPackageZip '{package_zip.as_posix()}' -EmulebbPackageManifest '{package_manifest.as_posix()}'
"""

    completed = _run_powershell(["-Command", command], cwd=repo_root)
    captured_payload = json.loads(captured.read_text(encoding="utf-8-sig"))

    assert "Resolved suite scripts bundle" in completed.stdout
    assert Path(captured_payload["suiteScriptsZip"]) == suite_scripts_zip
    assert Path(captured_payload["suiteScriptsManifest"]) == suite_scripts_manifest


def test_suite_installer_rejects_positional_parameter_string_splat() -> None:
    repo_root = Path.cwd()
    installer_path = (repo_root / INSTALLER).resolve()
    command = rf"""
$installer = '{installer_path}'
$argv = @('-Bundle')
& $installer @argv
"""

    completed = subprocess.run(
        [shutil.which("powershell"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert completed.returncode != 0
    assert "Install-eMuleBBSuite.ps1 was invoked with positional parameter strings" in completed.stdout


# Signatures PowerShell raises when a [ValidateSet] parameter has no valid default
# and the script text is bound via Invoke-Expression (the `irm <url> | iex` path).
_IEX_PARAM_BIND_FAILURE_SIGNATURE = (
    "attribute cannot be added because variable|"
    "does not belong to the set|"
    "Cannot validate argument"
)


def _top_level_param_block(text: str) -> str:
    """Return the script's leading param(...) block via balanced-paren matching."""
    match = re.search(r"param\s*\(", text, re.IGNORECASE)
    assert match is not None, "no top-level param( block found"
    depth = 0
    index = match.end() - 1
    while index < len(text):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[match.start() : index + 1]
        index += 1
    raise AssertionError("unbalanced param( block")


@pytest.mark.parametrize("script", [BOOTSTRAPPER, INSTALLER], ids=["bootstrapper", "installer"])
def test_release_irm_iex_scripts_param_block_binds_under_invoke_expression(
    script: Path, tmp_path: Path
) -> None:
    # Regression for BUG-017: a [ValidateSet] parameter with no valid default (e.g.
    # an optional -UiLanguage) binds fine under `& script.ps1` but fails only under
    # `irm <url> | iex`, because Invoke-Expression applies the validation attribute
    # to the variable's empty default and rejects it. Both irm-invokable release
    # scripts (bootstrapper and installer) must bind their param block under iex.
    # Only the param block is bound here (a sentinel throw replaces the body) so the
    # check stays side-effect free.
    param_block = _top_level_param_block((Path.cwd() / script).resolve().read_text(encoding="utf-8"))
    smoke = tmp_path / "param-iex-smoke.ps1"
    smoke.write_text(param_block + "\nthrow 'PARAM_BIND_OK'\n", encoding="utf-8")
    command = (
        "$ErrorActionPreference='Stop'; "
        f"$text = Get-Content -Raw -LiteralPath '{smoke.as_posix()}'; "
        "try { Invoke-Expression $text } "
        "catch { $m = \"$($_.Exception.Message)\"; "
        f"if ($m -match '{_IEX_PARAM_BIND_FAILURE_SIGNATURE}') {{ Write-Output \"PARAM_BIND_FAILED: $m\" }} "
        "else { Write-Output \"PARAM_BIND_OK: $m\" } }"
    )
    completed = _run_powershell(["-Command", command], cwd=Path.cwd())
    assert "PARAM_BIND_FAILED" not in completed.stdout, completed.stdout
    assert "PARAM_BIND_OK" in completed.stdout, completed.stdout
