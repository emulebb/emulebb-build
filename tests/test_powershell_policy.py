from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_tooling_module(workspace_root: Path, module_name: str, relative_path: str) -> ModuleType:
    module_path = workspace_root / "repos" / "emulebb-tooling" / relative_path
    ci_path = str(module_path.parent)
    if ci_path not in sys.path:
        sys.path.insert(0, ci_path)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


def test_basic_hygiene_accepts_emulebb_runtime_script_header(workspace_root: Path, tmp_path: Path) -> None:
    policy_guards = _load_tooling_module(workspace_root, "policy_guards_under_test", "ci/policy_guards.py")
    repo_root = tmp_path / "emulebb-build"
    script_path = repo_root / "emule_workspace" / "release_assets" / "emulebb" / "scripts" / "Register-Prowlarr.ps1"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("#Requires -Version 5.1\n", encoding="utf-8")

    issue = policy_guards.test_powershell_version_header(
        repo_root,
        "emulebb-build",
        "emule_workspace/release_assets/emulebb/scripts/Register-Prowlarr.ps1",
        script_path,
    )

    assert issue is None


def test_basic_hygiene_accepts_emulebb_automation_example_header(workspace_root: Path, tmp_path: Path) -> None:
    policy_guards = _load_tooling_module(workspace_root, "policy_guards_under_test_examples", "ci/policy_guards.py")
    repo_root = tmp_path / "emulebb-build"
    script_path = (
        repo_root
        / "emule_workspace"
        / "release_assets"
        / "emulebb_automation_examples"
        / "automation"
        / "Get-eMuleBBStatus.ps1"
    )
    script_path.parent.mkdir(parents=True)
    script_path.write_text("#Requires -Version 5.1\n", encoding="utf-8")

    issue = policy_guards.test_powershell_version_header(
        repo_root,
        "emulebb-build",
        "emule_workspace/release_assets/emulebb_automation_examples/automation/Get-eMuleBBStatus.ps1",
        script_path,
    )

    assert issue is None


def test_basic_hygiene_rejects_bad_emulebb_runtime_script_header(workspace_root: Path, tmp_path: Path) -> None:
    policy_guards = _load_tooling_module(workspace_root, "policy_guards_under_test_bad_header", "ci/policy_guards.py")
    repo_root = tmp_path / "emulebb-build"
    script_path = repo_root / "emule_workspace" / "release_assets" / "emulebb" / "scripts" / "Register-Prowlarr.ps1"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("#Requires -Version 7.6\n", encoding="utf-8")

    issue = policy_guards.test_powershell_version_header(
        repo_root,
        "emulebb-build",
        "emule_workspace/release_assets/emulebb/scripts/Register-Prowlarr.ps1",
        script_path,
    )

    assert issue == "Expected PowerShell version header '#Requires -Version 5.1' but found '#Requires -Version 7.6'."


def test_basic_hygiene_rejects_bad_emulebb_runtime_script_name(workspace_root: Path, tmp_path: Path) -> None:
    policy_guards = _load_tooling_module(workspace_root, "policy_guards_under_test_bad_name", "ci/policy_guards.py")
    repo_root = tmp_path / "emulebb-build"

    issue = policy_guards.test_powershell_runtime_script_name(
        repo_root,
        "emulebb-build",
        "emule_workspace/release_assets/emulebb/scripts/register-prowlarr.ps1",
    )

    assert issue == "eMuleBB runtime PowerShell scripts must use Verb-Noun.ps1 names."


def test_basic_hygiene_keeps_emulebb_script_exception_to_direct_children(workspace_root: Path, tmp_path: Path) -> None:
    policy_guards = _load_tooling_module(workspace_root, "policy_guards_under_test_nested", "ci/policy_guards.py")
    repo_root = tmp_path / "emulebb-build"
    script_path = repo_root / "emule_workspace" / "release_assets" / "emulebb" / "scripts" / "nested" / "tool.ps1"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("#Requires -Version 5.1\n", encoding="utf-8")

    issue = policy_guards.test_powershell_version_header(
        repo_root,
        "emulebb-build",
        "emule_workspace/release_assets/emulebb/scripts/nested/tool.ps1",
        script_path,
    )

    assert issue == "Expected PowerShell version header '#Requires -Version 7.6' but found '#Requires -Version 5.1'."


def test_workspace_policy_accepts_allowed_emulebb_runtime_script(
    workspace_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _load_tooling_module(workspace_root, "check_workspace_policy_under_test", "ci/check-workspace-policy.py")
    build_root = tmp_path / "repos" / "emulebb-build"
    script_path = build_root / "emule_workspace" / "release_assets" / "emulebb" / "scripts" / "Register-Prowlarr.ps1"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("#Requires -Version 5.1\n", encoding="utf-8")

    def fake_tracked_powershell_paths(repo_root: Path) -> tuple[str, ...]:
        if repo_root == build_root.resolve():
            return ("emule_workspace/release_assets/emulebb/scripts/Register-Prowlarr.ps1",)
        return ()

    monkeypatch.setattr(policy, "tracked_powershell_paths", fake_tracked_powershell_paths)

    policy.audit_powershell_boundary(tmp_path)


def test_workspace_policy_accepts_allowed_emulebb_automation_example(
    workspace_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _load_tooling_module(workspace_root, "check_workspace_policy_under_test_example", "ci/check-workspace-policy.py")
    build_root = tmp_path / "repos" / "emulebb-build"
    script_path = (
        build_root
        / "emule_workspace"
        / "release_assets"
        / "emulebb_automation_examples"
        / "automation"
        / "Get-eMuleBBStatus.ps1"
    )
    script_path.parent.mkdir(parents=True)
    script_path.write_text("#Requires -Version 5.1\n", encoding="utf-8")

    def fake_tracked_powershell_paths(repo_root: Path) -> tuple[str, ...]:
        if repo_root == build_root.resolve():
            return ("emule_workspace/release_assets/emulebb_automation_examples/automation/Get-eMuleBBStatus.ps1",)
        return ()

    monkeypatch.setattr(policy, "tracked_powershell_paths", fake_tracked_powershell_paths)

    policy.audit_powershell_boundary(tmp_path)


def test_workspace_policy_rejects_emulebb_runtime_script_without_required_header(
    workspace_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _load_tooling_module(workspace_root, "check_workspace_policy_under_test_bad_header", "ci/check-workspace-policy.py")
    build_root = tmp_path / "repos" / "emulebb-build"
    script_path = build_root / "emule_workspace" / "release_assets" / "emulebb" / "scripts" / "Register-Prowlarr.ps1"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("#Requires -Version 7.6\n", encoding="utf-8")

    def fake_tracked_powershell_paths(repo_root: Path) -> tuple[str, ...]:
        if repo_root == build_root.resolve():
            return ("emule_workspace/release_assets/emulebb/scripts/Register-Prowlarr.ps1",)
        return ()

    monkeypatch.setattr(policy, "tracked_powershell_paths", fake_tracked_powershell_paths)

    with pytest.raises(RuntimeError, match="eMuleBB runtime PowerShell must declare #Requires -Version 5.1"):
        policy.audit_powershell_boundary(tmp_path)


def test_workspace_policy_rejects_non_verb_noun_emulebb_runtime_script_name(
    workspace_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _load_tooling_module(workspace_root, "check_workspace_policy_under_test_bad_script_name", "ci/check-workspace-policy.py")
    build_root = tmp_path / "repos" / "emulebb-build"
    script_path = build_root / "emule_workspace" / "release_assets" / "emulebb" / "scripts" / "register-prowlarr.ps1"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("#Requires -Version 5.1\n", encoding="utf-8")

    def fake_tracked_powershell_paths(repo_root: Path) -> tuple[str, ...]:
        if repo_root == build_root.resolve():
            return ("emule_workspace/release_assets/emulebb/scripts/register-prowlarr.ps1",)
        return ()

    monkeypatch.setattr(policy, "tracked_powershell_paths", fake_tracked_powershell_paths)

    with pytest.raises(RuntimeError, match="eMuleBB runtime PowerShell scripts must use Verb-Noun.ps1 names"):
        policy.audit_powershell_boundary(tmp_path)


def test_workspace_policy_rejects_nested_emulebb_runtime_script(
    workspace_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _load_tooling_module(workspace_root, "check_workspace_policy_under_test_nested", "ci/check-workspace-policy.py")
    build_root = tmp_path / "repos" / "emulebb-build"
    script_path = build_root / "emule_workspace" / "release_assets" / "emulebb" / "scripts" / "nested" / "tool.ps1"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("#Requires -Version 5.1\n", encoding="utf-8")

    def fake_tracked_powershell_paths(repo_root: Path) -> tuple[str, ...]:
        if repo_root == build_root.resolve():
            return ("emule_workspace/release_assets/emulebb/scripts/nested/tool.ps1",)
        return ()

    monkeypatch.setattr(policy, "tracked_powershell_paths", fake_tracked_powershell_paths)

    with pytest.raises(RuntimeError, match="tracked PowerShell is only allowed"):
        policy.audit_powershell_boundary(tmp_path)


@pytest.mark.parametrize(
    "script_name",
    [
        "Register-Prowlarr.ps1",
        "Register-ArrStack.ps1",
    ],
)
def test_runtime_scripts_set_provider_field_adds_missing_value_property(
    workspace_root: Path,
    tmp_path: Path,
    script_name: str,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / script_name
    )
    function_text = _extract_powershell_function(script_path.read_text(encoding="utf-8"), "Set-ProviderField")
    test_script = tmp_path / "set-provider-field-test.ps1"
    test_script.write_text(
        function_text
        + """
$provider = [pscustomobject]@{
    fields = @(
        [pscustomobject]@{ name = 'baseUrl' },
        [pscustomobject]@{ name = 'apiKey'; value = '' }
    )
}
if (-not (Set-ProviderField -Provider $provider -Name 'baseUrl' -Value 'http://127.0.0.1')) {
    throw 'baseUrl was not set'
}
if ($provider.fields[0].value -ne 'http://127.0.0.1') {
    throw 'baseUrl value property was not added'
}
if (-not (Set-ProviderField -Provider $provider -Name 'apiKey' -Value 'secret')) {
    throw 'apiKey was not set'
}
if ($provider.fields[1].value -ne 'secret') {
    throw 'apiKey existing value property was not updated'
}
if (Set-ProviderField -Provider $provider -Name 'missing' -Value 'ignored' -Optional) {
    throw 'optional missing field should return false'
}
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


@pytest.mark.parametrize(
    "script_name",
    [
        "Register-Prowlarr.ps1",
        "Register-ArrStack.ps1",
    ],
)
def test_runtime_scripts_accept_register_unregister_action_aliases(
    workspace_root: Path,
    tmp_path: Path,
    script_name: str,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / script_name
    )
    function_text = _extract_powershell_function(script_path.read_text(encoding="utf-8"), "Read-ActionValue")
    test_script = tmp_path / "read-action-value-test.ps1"
    test_script.write_text(
        function_text
        + """
if ((Read-ActionValue -Value 'register') -ne 'Register') { throw 'register action was not accepted' }
if ((Read-ActionValue -Value 'R') -ne 'Register') { throw 'register alias was not accepted' }
if ((Read-ActionValue -Value 'unregister') -ne 'Unregister') { throw 'unregister action was not accepted' }
if ((Read-ActionValue -Value 'u') -ne 'Unregister') { throw 'unregister alias was not accepted' }
try {
    [void](Read-ActionValue -Value 'delete')
    throw 'invalid action was accepted'
} catch {
    if ($_.Exception.Message -notlike '*Action must be Register or Unregister*') { throw }
}
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_register_prowlarr_unregister_deletes_named_indexer(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / "Register-Prowlarr.ps1"
    )
    script_text = script_path.read_text(encoding="utf-8")
    test_script = tmp_path / "remove-prowlarr-indexer-test.ps1"
    test_script.write_text(
        """
$script:Calls = @()
function Invoke-JsonApi {
    param([string]$BaseUrl, [string]$ApiKey, [string]$Path, [string]$Method = 'GET', $Body = $null)
    $script:Calls += [pscustomobject]@{ BaseUrl = $BaseUrl; ApiKey = $ApiKey; Path = $Path; Method = $Method }
    if ($Path -eq '/api/v1/indexer') {
        return @(
            [pscustomobject]@{ name = 'Other'; id = 7 },
            [pscustomobject]@{ name = 'eMuleBB'; id = 42 }
        )
    }
    return $null
}
"""
        + _extract_powershell_function(script_text, "Get-ExistingIndexer")
        + "\n"
        + _extract_powershell_function(script_text, "Remove-Indexer")
        + """
Remove-Indexer -BaseUrl 'http://prowlarr' -ApiKey 'secret' -Name 'eMuleBB'
if ($script:Calls.Count -ne 2) { throw ('expected 2 API calls, got {0}' -f $script:Calls.Count) }
if ($script:Calls[1].Path -ne '/api/v1/indexer/42') { throw ('unexpected delete path: {0}' -f $script:Calls[1].Path) }
if ($script:Calls[1].Method -ne 'DELETE') { throw ('unexpected delete method: {0}' -f $script:Calls[1].Method) }
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_register_prowlarr_save_indexer_returns_only_saved_provider(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / "Register-Prowlarr.ps1"
    )
    script_text = script_path.read_text(encoding="utf-8")
    test_script = tmp_path / "save-prowlarr-indexer-test.ps1"
    test_script.write_text(
        """
function Invoke-JsonApi {
    param([string]$BaseUrl, [string]$ApiKey, [string]$Path, [string]$Method = 'GET', $Body = $null)
    if ($Path -eq '/api/v1/indexer') { return @() }
    if ($Path -eq '/api/v1/indexer/schema') {
        return @([pscustomobject]@{
            name = 'Generic Torznab'
            implementation = 'Torznab'
            appProfileId = 1
            priority = 25
            fields = @(
                [pscustomobject]@{ name = 'baseUrl' },
                [pscustomobject]@{ name = 'apiPath' },
                [pscustomobject]@{ name = 'apiKey' },
                [pscustomobject]@{ name = 'torrentBaseSettings.preferMagnetUrl' }
            )
        })
    }
    if ($Path -eq '/api/v1/indexer?forceSave=true' -and $Method -eq 'POST') {
        $baseUrl = ($Body.fields | Where-Object { $_.name -eq 'baseUrl' }).value
        if ($baseUrl -ne 'http://emule/indexer/emulebb') { throw ('unexpected baseUrl: {0}' -f $baseUrl) }
        if ($Body.appProfileId -ne 77) { throw ('unexpected appProfileId: {0}' -f $Body.appProfileId) }
        return [pscustomobject]@{ id = 99; name = $Body.name }
    }
    throw ('unexpected call: {0} {1}' -f $Method, $Path)
}
"""
        + _extract_powershell_function(script_text, "Get-HttpStatusCode")
        + "\n"
        + _extract_powershell_function(script_text, "Copy-JsonObject")
        + "\n"
        + _extract_powershell_function(script_text, "Set-ObjectProperty")
        + "\n"
        + _extract_powershell_function(script_text, "Set-ProviderField")
        + "\n"
        + _extract_powershell_function(script_text, "Normalize-ArgumentValue")
        + "\n"
        + _extract_powershell_function(script_text, "Normalize-HttpBaseUrl")
        + "\n"
        + _extract_powershell_function(script_text, "Get-GenericTorznabSchema")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ExistingIndexer")
        + "\n"
        + _extract_powershell_function(script_text, "Save-Indexer")
        + """
$saved = Save-Indexer -BaseUrl 'http://prowlarr' -ApiKey 'secret' -Name 'eMuleBB' -TorznabBaseUrl "'http://emule/indexer/emulebb/'" -TorznabApiKey 'emule-key' -AppProfileId 77
if ($saved -is [array]) { throw ('Save-Indexer emitted an array with {0} items' -f $saved.Count) }
if ($saved.id -ne 99) { throw ('unexpected saved id: {0}' -f $saved.id) }
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_register_prowlarr_creates_rss_capable_app_profile(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / "Register-Prowlarr.ps1"
    )
    script_text = script_path.read_text(encoding="utf-8")
    test_script = tmp_path / "prowlarr-app-profile-create-test.ps1"
    test_script.write_text(
        """
function Invoke-JsonApi {
    param([string]$BaseUrl, [string]$ApiKey, [string]$Path, [string]$Method = 'GET', $Body = $null)
    if ($Path -eq '/api/v1/appprofile' -and $Method -eq 'GET') { return @() }
    if ($Path -eq '/api/v1/appprofile' -and $Method -eq 'POST') {
        if ($Body.name -ne 'eMuleBB Suite') { throw ('unexpected name: {0}' -f $Body.name) }
        if ($Body.enableRss -ne $true) { throw 'RSS was not enabled' }
        if ($Body.enableAutomaticSearch -ne $true) { throw 'automatic search was not enabled' }
        if ($Body.enableInteractiveSearch -ne $true) { throw 'interactive search was not enabled' }
        if ($Body.minimumSeeders -ne 1) { throw ('unexpected minimumSeeders: {0}' -f $Body.minimumSeeders) }
        return [pscustomobject]@{ id = 77; name = $Body.name }
    }
    throw ('unexpected call: {0} {1}' -f $Method, $Path)
}
"""
        + _extract_powershell_function(script_text, "Set-ObjectProperty")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ExistingAppProfile")
        + "\n"
        + _extract_powershell_function(script_text, "Save-AppProfile")
        + """
$saved = Save-AppProfile -BaseUrl 'http://prowlarr' -ApiKey 'secret' -Name 'eMuleBB Suite'
if ($saved.id -ne 77) { throw ('unexpected saved id: {0}' -f $saved.id) }
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_register_prowlarr_updates_disabled_app_profile(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / "Register-Prowlarr.ps1"
    )
    script_text = script_path.read_text(encoding="utf-8")
    test_script = tmp_path / "prowlarr-app-profile-update-test.ps1"
    test_script.write_text(
        """
function Invoke-JsonApi {
    param([string]$BaseUrl, [string]$ApiKey, [string]$Path, [string]$Method = 'GET', $Body = $null)
    if ($Path -eq '/api/v1/appprofile' -and $Method -eq 'GET') {
        return @([pscustomobject]@{
            id = 42
            name = 'eMuleBB Suite'
            enableRss = $false
            enableAutomaticSearch = $false
            enableInteractiveSearch = $false
            minimumSeeders = 0
        })
    }
    if ($Path -eq '/api/v1/appprofile/42' -and $Method -eq 'PUT') {
        if ($Body.enableRss -ne $true) { throw 'RSS was not enabled' }
        if ($Body.enableAutomaticSearch -ne $true) { throw 'automatic search was not enabled' }
        if ($Body.enableInteractiveSearch -ne $true) { throw 'interactive search was not enabled' }
        if ($Body.minimumSeeders -ne 1) { throw ('unexpected minimumSeeders: {0}' -f $Body.minimumSeeders) }
        return $Body
    }
    throw ('unexpected call: {0} {1}' -f $Method, $Path)
}
"""
        + _extract_powershell_function(script_text, "Set-ObjectProperty")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ExistingAppProfile")
        + "\n"
        + _extract_powershell_function(script_text, "Save-AppProfile")
        + """
$saved = Save-AppProfile -BaseUrl 'http://prowlarr' -ApiKey 'secret' -Name 'eMuleBB Suite'
if ($saved.id -ne 42) { throw ('unexpected saved id: {0}' -f $saved.id) }
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_register_prowlarr_relaxes_local_https_without_windows_cert_store(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / "Register-Prowlarr.ps1"
    )
    script_text = script_path.read_text(encoding="utf-8")
    forbidden = ("Import-Certificate", "certutil", "X509Store", "StoreName", "CertOpenStore")
    assert not any(token in script_text for token in forbidden)
    test_script = tmp_path / "prowlarr-local-https-cert-policy-test.ps1"
    test_script.write_text(
        """
$script:Calls = @()
function Invoke-JsonApi {
    param([string]$BaseUrl, [string]$ApiKey, [string]$Path, [string]$Method = 'GET', $Body = $null)
    $script:Calls += [pscustomobject]@{ Path = $Path; Method = $Method; Body = $Body }
    if ($Path -eq '/api/v1/config/host' -and $Method -eq 'GET') {
        return [pscustomobject]@{ id = 1; certificateValidation = 'enabled' }
    }
    if ($Path -eq '/api/v1/config/host' -and $Method -eq 'PUT') {
        if ($Body.certificateValidation -ne 'disabledForLocalAddresses') {
            throw ('unexpected certificateValidation: {0}' -f $Body.certificateValidation)
        }
        return $Body
    }
    throw ('unexpected call: {0} {1}' -f $Method, $Path)
}
        """
        + _extract_powershell_function(script_text, "Set-ObjectProperty")
        + "\n"
        + _extract_powershell_function(script_text, "Set-LocalCertificateValidation")
        + """
$saved = Set-LocalCertificateValidation -BaseUrl 'http://prowlarr' -ApiKey 'secret'
if ($saved.certificateValidation -ne 'disabledForLocalAddresses') { throw 'host config was not relaxed for local HTTPS' }
if ($script:Calls.Count -ne 2) { throw ('expected 2 API calls, got {0}' -f $script:Calls.Count) }
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_register_arr_stack_unregister_deletes_named_download_client(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / "Register-ArrStack.ps1"
    )
    script_text = script_path.read_text(encoding="utf-8")
    test_script = tmp_path / "remove-arr-client-test.ps1"
    test_script.write_text(
        """
$script:Calls = @()
function Invoke-JsonApi {
    param([string]$BaseUrl, [string]$ApiKey, [string]$Path, [string]$Method = 'GET', $Body = $null)
    $script:Calls += [pscustomobject]@{ BaseUrl = $BaseUrl; ApiKey = $ApiKey; Path = $Path; Method = $Method }
    if ($Path -eq '/api/v3/downloadclient') {
        return @(
            [pscustomobject]@{ name = 'Other'; id = 12 },
            [pscustomobject]@{ name = 'eMuleBB'; id = 77 }
        )
    }
    if ($Path -eq '/api/v1/downloadclient') {
        return @(
            [pscustomobject]@{ name = 'eMuleBB'; id = 88 }
        )
    }
    return $null
}
"""
        + _extract_powershell_function(script_text, "Get-ArrApiBasePath")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ExistingDownloadClient")
        + "\n"
        + _extract_powershell_function(script_text, "Get-HttpStatusCode")
        + "\n"
        + _extract_powershell_function(script_text, "Invoke-JsonApiWithRetry")
        + "\n"
        + _extract_powershell_function(script_text, "Invoke-DeleteJsonApiWithRetry")
        + "\n"
        + _extract_powershell_function(script_text, "Remove-QbitClient")
        + """
Remove-QbitClient -Kind 'radarr' -BaseUrl 'http://radarr' -ApiKey 'secret' -Name 'eMuleBB'
Remove-QbitClient -Kind 'lidarr' -BaseUrl 'http://lidarr' -ApiKey 'secret' -Name 'eMuleBB'
if ($script:Calls.Count -ne 4) { throw ('expected 4 API calls, got {0}' -f $script:Calls.Count) }
if ($script:Calls[1].Path -ne '/api/v3/downloadclient/77') { throw ('unexpected delete path: {0}' -f $script:Calls[1].Path) }
if ($script:Calls[1].Method -ne 'DELETE') { throw ('unexpected delete method: {0}' -f $script:Calls[1].Method) }
if ($script:Calls[3].Path -ne '/api/v1/downloadclient/88') { throw ('unexpected v1 delete path: {0}' -f $script:Calls[3].Path) }
if ($script:Calls[3].Method -ne 'DELETE') { throw ('unexpected v1 delete method: {0}' -f $script:Calls[3].Method) }
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_register_arr_stack_http_error_helpers_tolerate_missing_response(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / "Register-ArrStack.ps1"
    )
    script_text = script_path.read_text(encoding="utf-8")
    test_script = tmp_path / "missing-response-error-test.ps1"
    test_script.write_text(
        _extract_powershell_function(script_text, "Get-ExceptionResponse")
        + "\n"
        + _extract_powershell_function(script_text, "Get-HttpStatusCode")
        + "\n"
        + _extract_powershell_function(script_text, "Get-HttpErrorDetail")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ExceptionMessage")
        + """
$exception = New-Object System.InvalidOperationException('plain failure')
$status = Get-HttpStatusCode -Exception $exception
if ($status -ne 0) { throw ('unexpected status: {0}' -f $status) }
$detail = Get-HttpErrorDetail -Exception $exception
if ($detail -ne '') { throw ('unexpected detail: {0}' -f $detail) }
$message = Get-ExceptionMessage -Exception $exception
if ($message -ne 'plain failure') { throw ('unexpected message: {0}' -f $message) }
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_register_arr_stack_save_client_returns_only_saved_provider(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / "Register-ArrStack.ps1"
    )
    script_text = script_path.read_text(encoding="utf-8")
    test_script = tmp_path / "save-arr-client-test.ps1"
    test_script.write_text(
        """
function Invoke-JsonApi {
    param([string]$BaseUrl, [string]$ApiKey, [string]$Path, [string]$Method = 'GET', $Body = $null)
    if ($Path -eq '/api/v3/downloadclient') { return @() }
    if ($Path -eq '/api/v1/downloadclient') { return @() }
    if ($Path -eq '/api/v3/downloadclient/schema') {
        return @([pscustomobject]@{
            implementation = 'QBittorrent'
            fields = @(
                [pscustomobject]@{ name = 'host' },
                [pscustomobject]@{ name = 'port' },
                [pscustomobject]@{ name = 'useSsl' },
                [pscustomobject]@{ name = 'urlBase' },
                [pscustomobject]@{ name = 'username' },
                [pscustomobject]@{ name = 'password' },
                [pscustomobject]@{ name = 'movieCategory' },
                [pscustomobject]@{ name = 'initialState' }
            )
        })
    }
    if ($Path -eq '/api/v1/downloadclient/schema') {
        return @([pscustomobject]@{
            implementation = 'QBittorrent'
            fields = @(
                [pscustomobject]@{ name = 'host' },
                [pscustomobject]@{ name = 'port' },
                [pscustomobject]@{ name = 'useSsl' },
                [pscustomobject]@{ name = 'urlBase' },
                [pscustomobject]@{ name = 'username' },
                [pscustomobject]@{ name = 'password' },
                [pscustomobject]@{ name = 'musicCategory' },
                [pscustomobject]@{ name = 'initialState' }
            )
        })
    }
    if ($Path -eq '/api/v3/downloadclient?forceSave=true' -and $Method -eq 'POST') {
        $hostName = ($Body.fields | Where-Object { $_.name -eq 'host' }).value
        $urlBase = ($Body.fields | Where-Object { $_.name -eq 'urlBase' }).value
        $category = ($Body.fields | Where-Object { $_.name -eq 'movieCategory' }).value
        if ($hostName -ne 'emule') { throw ('unexpected host: {0}' -f $hostName) }
        if ($urlBase -ne '/proxy') { throw ('unexpected urlBase: {0}' -f $urlBase) }
        if ($category -ne 'emulebb-radarr') { throw ('unexpected category: {0}' -f $category) }
        return [pscustomobject]@{ id = 88; name = $Body.name }
    }
    if ($Path -eq '/api/v1/downloadclient?forceSave=true' -and $Method -eq 'POST') {
        $category = ($Body.fields | Where-Object { $_.name -eq 'musicCategory' }).value
        if ($category -ne 'emulebb-lidarr') { throw ('unexpected lidarr category: {0}' -f $category) }
        return [pscustomobject]@{ id = 89; name = $Body.name }
    }
    throw ('unexpected call: {0} {1}' -f $Method, $Path)
}
        """
        + _extract_powershell_function(script_text, "Set-ObjectProperty")
        + "\n"
        + _extract_powershell_function(script_text, "Set-ProviderField")
        + "\n"
        + _extract_powershell_function(script_text, "Normalize-ArgumentValue")
        + "\n"
        + _extract_powershell_function(script_text, "Normalize-HttpBaseUrl")
        + "\n"
        + _extract_powershell_function(script_text, "Get-HttpStatusCode")
        + "\n"
        + _extract_powershell_function(script_text, "Invoke-JsonApiWithRetry")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ArrApiBasePath")
        + "\n"
        + _extract_powershell_function(script_text, "Get-QbitSchema")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ExistingDownloadClient")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ArrCategoryInfo")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ArrCategoryName")
        + "\n"
        + _extract_powershell_function(script_text, "Save-QbitClient")
        + """
$saved = Save-QbitClient -Kind 'radarr' -BaseUrl 'http://radarr' -ApiKey 'secret' -EmuleBaseUrl "'http://emule:4711/proxy/'" -EmuleApiKey 'emule-key' -Name 'eMuleBB'
if ($saved -is [array]) { throw ('Save-QbitClient emitted an array with {0} items' -f $saved.Count) }
if ($saved.id -ne 88) { throw ('unexpected saved id: {0}' -f $saved.id) }
$v1Saved = Save-QbitClient -Kind 'lidarr' -BaseUrl 'http://lidarr' -ApiKey 'secret' -EmuleBaseUrl "'http://emule:4711/proxy/'" -EmuleApiKey 'emule-key' -Name 'eMuleBB'
if ($v1Saved.id -ne 89) { throw ('unexpected v1 saved id: {0}' -f $v1Saved.id) }
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_register_arr_stack_saves_prowlarr_download_client(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / "Register-ArrStack.ps1"
    )
    script_text = script_path.read_text(encoding="utf-8")
    test_script = tmp_path / "save-prowlarr-client-test.ps1"
    test_script.write_text(
        """
function Invoke-JsonApi {
    param([string]$BaseUrl, [string]$ApiKey, [string]$Path, [string]$Method = 'GET', $Body = $null)
    if ($Path -eq '/api/v1/downloadclient') { return @() }
    if ($Path -eq '/api/v1/downloadclient/schema') {
        return @([pscustomobject]@{
            implementation = 'QBittorrent'
            fields = @(
                [pscustomobject]@{ name = 'host' },
                [pscustomobject]@{ name = 'port' },
                [pscustomobject]@{ name = 'useSsl' },
                [pscustomobject]@{ name = 'urlBase' },
                [pscustomobject]@{ name = 'username' },
                [pscustomobject]@{ name = 'password' },
                [pscustomobject]@{ name = 'category' },
                [pscustomobject]@{ name = 'initialState' }
            )
        })
    }
    if ($Path -eq '/api/v1/downloadclient?forceSave=true' -and $Method -eq 'POST') {
        $hostName = ($Body.fields | Where-Object { $_.name -eq 'host' }).value
        $urlBase = ($Body.fields | Where-Object { $_.name -eq 'urlBase' }).value
        $category = ($Body.fields | Where-Object { $_.name -eq 'category' }).value
        if ($hostName -ne 'emule') { throw ('unexpected host: {0}' -f $hostName) }
        if ($urlBase -ne '/proxy') { throw ('unexpected urlBase: {0}' -f $urlBase) }
        if ($category -ne '') { throw ('unexpected category: {0}' -f $category) }
        return [pscustomobject]@{ id = 99; name = $Body.name }
    }
    throw ('unexpected call: {0} {1}' -f $Method, $Path)
}
        """
        + _extract_powershell_function(script_text, "Set-ObjectProperty")
        + "\n"
        + _extract_powershell_function(script_text, "Set-ProviderField")
        + "\n"
        + _extract_powershell_function(script_text, "Normalize-ArgumentValue")
        + "\n"
        + _extract_powershell_function(script_text, "Normalize-HttpBaseUrl")
        + "\n"
        + _extract_powershell_function(script_text, "Get-HttpStatusCode")
        + "\n"
        + _extract_powershell_function(script_text, "Invoke-JsonApiWithRetry")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ProwlarrQbitSchema")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ExistingProwlarrDownloadClient")
        + "\n"
        + _extract_powershell_function(script_text, "Save-ProwlarrQbitClient")
        + """
$saved = Save-ProwlarrQbitClient -BaseUrl 'http://prowlarr' -ApiKey 'secret' -EmuleBaseUrl "'http://emule:4711/proxy/'" -EmuleApiKey 'emule-key' -Name 'eMuleBB'
if ($saved -is [array]) { throw ('Save-ProwlarrQbitClient emitted an array with {0} items' -f $saved.Count) }
if ($saved.id -ne 99) { throw ('unexpected saved id: {0}' -f $saved.id) }
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_register_arr_stack_ensures_matching_emulebb_category(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / "Register-ArrStack.ps1"
    )
    script_text = script_path.read_text(encoding="utf-8")
    test_script = tmp_path / "ensure-emule-category-test.ps1"
    test_script.write_text(
        """
$script:Calls = @()
function Invoke-JsonApi {
    param([string]$BaseUrl, [string]$ApiKey, [string]$Path, [string]$Method = 'GET', $Body = $null)
    $script:Calls += [pscustomobject]@{ BaseUrl = $BaseUrl; ApiKey = $ApiKey; Path = $Path; Method = $Method; Body = $Body }
    if ($Path -eq '/api/v1/categories' -and $Method -eq 'GET') {
        return [pscustomobject]@{ data = [pscustomobject]@{ items = @([pscustomobject]@{ name = 'Default' }) } }
    }
    if ($Path -eq '/api/v1/categories' -and $Method -eq 'POST') {
        if ($Body.name -ne 'emulebb-sonarr') { throw ('unexpected category name: {0}' -f $Body.name) }
        if ($Body.path -ne 'C:\\suite\\downloads\\sonarr') { throw ('unexpected category path: {0}' -f $Body.path) }
        return [pscustomobject]@{ id = 2; name = $Body.name }
    }
    throw ('unexpected call: {0} {1}' -f $Method, $Path)
}
"""
        + _extract_powershell_function(script_text, "Normalize-ArgumentValue")
        + "\n"
        + _extract_powershell_function(script_text, "Get-HttpStatusCode")
        + "\n"
        + _extract_powershell_function(script_text, "Invoke-JsonApiWithRetry")
        + "\n"
        + _extract_powershell_function(script_text, "Normalize-OptionalCategoryPath")
        + "\n"
        + _extract_powershell_function(script_text, "Normalize-ComparablePath")
        + "\n"
        + _extract_powershell_function(script_text, "Find-EmuleCategory")
        + "\n"
        + _extract_powershell_function(script_text, "Test-EmuleCategoryExists")
        + "\n"
        + _extract_powershell_function(script_text, "Ensure-EmuleCategory")
        + """
Ensure-EmuleCategory -BaseUrl 'http://emule' -ApiKey 'emule-key' -Name 'emulebb-sonarr' -Path 'C:\\suite\\downloads\\sonarr'
if ($script:Calls.Count -ne 2) { throw ('expected 2 calls, got {0}' -f $script:Calls.Count) }
if ($script:Calls[1].Method -ne 'POST') { throw ('expected POST, got {0}' -f $script:Calls[1].Method) }
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_register_arr_stack_updates_existing_emulebb_category_path(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / "Register-ArrStack.ps1"
    )
    script_text = script_path.read_text(encoding="utf-8")
    test_script = tmp_path / "update-emule-category-path-test.ps1"
    test_script.write_text(
        """
$script:Calls = @()
function Invoke-JsonApi {
    param([string]$BaseUrl, [string]$ApiKey, [string]$Path, [string]$Method = 'GET', $Body = $null)
    $script:Calls += [pscustomobject]@{ BaseUrl = $BaseUrl; ApiKey = $ApiKey; Path = $Path; Method = $Method; Body = $Body }
    if ($Path -eq '/api/v1/categories' -and $Method -eq 'GET') {
        return @([pscustomobject]@{ id = 5; name = 'emulebb-radarr'; path = 'C:\\old\\radarr' })
    }
    if ($Path -eq '/api/v1/categories/5' -and $Method -eq 'PATCH') {
        if ($Body.path -ne 'C:\\suite\\downloads\\radarr') { throw ('unexpected patched path: {0}' -f $Body.path) }
        return [pscustomobject]@{ id = 5; name = 'emulebb-radarr'; path = $Body.path }
    }
    throw ('unexpected call: {0} {1}' -f $Method, $Path)
}
"""
        + _extract_powershell_function(script_text, "Normalize-ArgumentValue")
        + "\n"
        + _extract_powershell_function(script_text, "Get-HttpStatusCode")
        + "\n"
        + _extract_powershell_function(script_text, "Invoke-JsonApiWithRetry")
        + "\n"
        + _extract_powershell_function(script_text, "Normalize-OptionalCategoryPath")
        + "\n"
        + _extract_powershell_function(script_text, "Normalize-ComparablePath")
        + "\n"
        + _extract_powershell_function(script_text, "Find-EmuleCategory")
        + "\n"
        + _extract_powershell_function(script_text, "Ensure-EmuleCategory")
        + """
Ensure-EmuleCategory -BaseUrl 'http://emule' -ApiKey 'emule-key' -Name 'emulebb-radarr' -Path 'C:\\suite\\downloads\\radarr'
if ($script:Calls.Count -ne 2) { throw ('expected 2 calls, got {0}' -f $script:Calls.Count) }
if ($script:Calls[1].Method -ne 'PATCH') { throw ('expected PATCH, got {0}' -f $script:Calls[1].Method) }
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_register_arr_stack_relaxes_local_https_without_windows_cert_store(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / "Register-ArrStack.ps1"
    )
    script_text = script_path.read_text(encoding="utf-8")
    forbidden = ("Import-Certificate", "certutil", "X509Store", "StoreName", "CertOpenStore")
    assert not any(token in script_text for token in forbidden)
    test_script = tmp_path / "arr-local-https-cert-policy-test.ps1"
    test_script.write_text(
        """
$script:Calls = @()
function Invoke-JsonApi {
    param([string]$BaseUrl, [string]$ApiKey, [string]$Path, [string]$Method = 'GET', $Body = $null)
    $script:Calls += [pscustomobject]@{ Path = $Path; Method = $Method; Body = $Body }
    if ($Path -eq '/api/v3/config/host' -and $Method -eq 'GET') {
        return [pscustomobject]@{ id = 1; certificateValidation = 'enabled' }
    }
    if ($Path -eq '/api/v3/config/host' -and $Method -eq 'PUT') {
        if ($Body.certificateValidation -ne 'disabledForLocalAddresses') {
            throw ('unexpected certificateValidation: {0}' -f $Body.certificateValidation)
        }
        return $Body
    }
    throw ('unexpected call: {0} {1}' -f $Method, $Path)
}
        """
        + _extract_powershell_function(script_text, "Set-ObjectProperty")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ArrApiBasePath")
        + "\n"
        + _extract_powershell_function(script_text, "Set-LocalCertificateValidation")
        + """
$saved = Set-LocalCertificateValidation -Kind 'radarr' -BaseUrl 'http://radarr' -ApiKey 'secret'
if ($saved.certificateValidation -ne 'disabledForLocalAddresses') { throw 'host config was not relaxed for local HTTPS' }
if ($script:Calls.Count -ne 2) { throw ('expected 2 API calls, got {0}' -f $script:Calls.Count) }
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_register_arr_stack_save_prowlarr_application_adds_missing_root_name(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / "Register-ArrStack.ps1"
    )
    script_text = script_path.read_text(encoding="utf-8")
    test_script = tmp_path / "save-prowlarr-application-test.ps1"
    test_script.write_text(
        """
function Invoke-JsonApi {
    param([string]$BaseUrl, [string]$ApiKey, [string]$Path, [string]$Method = 'GET', $Body = $null)
    if ($Path -eq '/api/v1/applications') { return @() }
    if ($Path -eq '/api/v1/applications/schema') {
        return @([pscustomobject]@{
            implementation = 'Radarr'
            fields = @(
                [pscustomobject]@{ name = 'baseUrl' },
                [pscustomobject]@{ name = 'apiKey' },
                [pscustomobject]@{ name = 'prowlarrUrl' },
                [pscustomobject]@{ name = 'syncCategories' },
                [pscustomobject]@{ name = 'animeSyncCategories' }
            )
        })
    }
    if ($Path -eq '/api/v1/applications?forceSave=true' -and $Method -eq 'POST') {
        if (-not $Body.name) { throw 'missing root name' }
        if ($Body.syncLevel -ne 'fullSync') { throw ('unexpected syncLevel: {0}' -f $Body.syncLevel) }
        return [pscustomobject]@{ id = 66; name = $Body.name }
    }
    throw ('unexpected call: {0} {1}' -f $Method, $Path)
}
"""
        + _extract_powershell_function(script_text, "Set-ObjectProperty")
        + "\n"
        + _extract_powershell_function(script_text, "Set-ProviderField")
        + "\n"
        + _extract_powershell_function(script_text, "Normalize-ArgumentValue")
        + "\n"
        + _extract_powershell_function(script_text, "Normalize-HttpBaseUrl")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ProviderFieldValue")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ExistingProwlarrApplication")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ProwlarrApplicationSchema")
        + "\n"
        + _extract_powershell_function(script_text, "Save-ProwlarrApplication")
        + """
$saved = Save-ProwlarrApplication -ProwlarrBaseUrl 'http://prowlarr' -ProwlarrKey 'secret' -Kind 'radarr' -ArrUrl 'http://radarr' -ArrKey 'arr-key'
if ($saved -is [array]) { throw ('Save-ProwlarrApplication emitted an array with {0} items' -f $saved.Count) }
if ($saved.id -ne 66) { throw ('unexpected saved id: {0}' -f $saved.id) }
if ($saved.name -ne 'Radarr') { throw ('unexpected saved name: {0}' -f $saved.name) }
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_register_arr_stack_waits_for_prowlarr_sync_completion(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / "Register-ArrStack.ps1"
    )
    script_text = script_path.read_text(encoding="utf-8")
    test_script = tmp_path / "prowlarr-sync-wait-test.ps1"
    test_script.write_text(
        """
$script:PollCount = 0
function Start-Sleep { param([int]$Seconds) }
function Invoke-JsonApi {
    param([string]$BaseUrl, [string]$ApiKey, [string]$Path, [string]$Method = 'GET', $Body = $null)
    if ($Path -eq '/api/v1/command' -and $Method -eq 'POST') { return [pscustomobject]@{ id = 123 } }
    if ($Path -eq '/api/v1/command/123') {
        $script:PollCount += 1
        return [pscustomobject]@{ status = 'completed' }
    }
    throw ('unexpected call: {0} {1}' -f $Method, $Path)
}
"""
        + _extract_powershell_function(script_text, "Invoke-ProwlarrSync")
        + """
Invoke-ProwlarrSync -BaseUrl 'http://prowlarr' -ApiKey 'secret'
if ($script:PollCount -ne 1) { throw ('expected one status poll, got {0}' -f $script:PollCount) }
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_register_arr_stack_verifies_arr_indexer_without_resaving(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / "Register-ArrStack.ps1"
    )
    script_text = script_path.read_text(encoding="utf-8")
    test_script = tmp_path / "verify-arr-prowlarr-indexer-test.ps1"
    test_script.write_text(
        """
$script:Calls = @()
function Invoke-JsonApi {
    param([string]$BaseUrl, [string]$ApiKey, [string]$Path, [string]$Method = 'GET', $Body = $null)
    $script:Calls += [pscustomobject]@{ BaseUrl = $BaseUrl; ApiKey = $ApiKey; Path = $Path; Method = $Method; Body = $Body }
    if ($Method -ne 'GET') {
        throw ('verification must not mutate Arr indexers: {0} {1}' -f $Method, $Path)
    }
    if ($Path -eq '/api/v1/indexer') {
        return @([pscustomobject]@{ id = 7; name = 'eMuleBB Suite' })
    }
    if ($Path -eq '/api/v3/indexer') {
        return @([pscustomobject]@{
            id = 44
            name = 'eMuleBB Suite (Prowlarr)'
            fields = @(
                [pscustomobject]@{ name = 'baseUrl'; value = 'http://prowlarr/7/' },
                [pscustomobject]@{ name = 'apiPath'; value = '/api' },
                [pscustomobject]@{ name = 'apiKey'; value = '********' },
                [pscustomobject]@{ name = 'categories'; value = @(2000) }
            )
        })
    }
    throw ('unexpected call: {0} {1}' -f $Method, $Path)
}
function Test-ApiKeyRejectedError { param($Exception) return $false }
"""
        + _extract_powershell_function(script_text, "Normalize-ArgumentValue")
        + "\n"
        + _extract_powershell_function(script_text, "Normalize-HttpBaseUrl")
        + "\n"
        + _extract_powershell_function(script_text, "Get-HttpStatusCode")
        + "\n"
        + _extract_powershell_function(script_text, "Invoke-JsonApiWithRetry")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ProviderFieldValue")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ArrProwlarrIndexerName")
        + "\n"
        + _extract_powershell_function(script_text, "Test-ArrProwlarrIndexerName")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ArrApiBasePath")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ExistingArrIndexers")
        + "\n"
        + _extract_powershell_function(script_text, "Get-PreferredArrIndexer")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ArrIndexerCategories")
        + "\n"
        + _extract_powershell_function(script_text, "Get-ProwlarrIndexer")
        + "\n"
        + _extract_powershell_function(script_text, "Assert-ProviderFieldEquals")
        + "\n"
        + _extract_powershell_function(script_text, "Assert-ProviderFieldPresent")
        + "\n"
        + _extract_powershell_function(script_text, "Test-CategorySetEquals")
        + "\n"
        + _extract_powershell_function(script_text, "Verify-ArrProwlarrIndexer")
        + """
$verified = Verify-ArrProwlarrIndexer -Kind 'radarr' -BaseUrl 'http://radarr' -ApiKey 'radarr-key' -Name 'eMuleBB Suite' -ProwlarrBaseUrl 'http://prowlarr' -ProwlarrKey 'prowlarr-key'
if ($verified.id -ne 44) { throw ('unexpected verified id: {0}' -f $verified.id) }
$mutatingCalls = @($script:Calls | Where-Object { $_.Method -ne 'GET' })
if ($mutatingCalls.Count -ne 0) { throw ('expected no mutating calls, got {0}' -f $mutatingCalls.Count) }
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_register_arr_stack_retry_wrapper_does_not_shadow_selected_action(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / "Register-ArrStack.ps1"
    )
    script_text = script_path.read_text(encoding="utf-8")
    test_script = tmp_path / "run-target-action-scope-test.ps1"
    test_script.write_text(
        _extract_powershell_function(script_text, "Run-TargetWithRetry")
        + """
$Action = 'Unregister'
$script:Observed = ''
Run-TargetWithRetry -Name 'scope check' -NoRetry -Operation {
    $script:Observed = $Action
}
if ($script:Observed -ne 'Unregister') {
    throw ('Run-TargetWithRetry shadowed selected action as {0}' -f $script:Observed)
}
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_register_arr_stack_normalizes_quoted_http_urls(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    script_path = (
        workspace_root
        / "repos"
        / "emulebb-build"
        / "emule_workspace"
        / "release_assets"
        / "emulebb"
        / "scripts"
        / "Register-ArrStack.ps1"
    )
    script_text = script_path.read_text(encoding="utf-8")
    test_script = tmp_path / "normalize-arr-url-test.ps1"
    test_script.write_text(
        _extract_powershell_function(script_text, "Normalize-ArgumentValue")
        + "\n"
        + _extract_powershell_function(script_text, "Normalize-HttpBaseUrl")
        + """
$url = Normalize-HttpBaseUrl -Value "'http://127.0.0.1:4711/'" -Name 'EmulebbBaseUrl'
if ($url -ne 'http://127.0.0.1:4711') { throw ('unexpected normalized URL: {0}' -f $url) }
try {
    [void](Normalize-HttpBaseUrl -Value "'ftp://127.0.0.1'" -Name 'BadUrl')
    throw 'expected unsupported scheme rejection'
} catch {
    if ($_.Exception.Message -notmatch 'http or https') { throw }
}
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(test_script),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout


def _extract_powershell_function(script_text: str, function_name: str) -> str:
    start_token = f"function {function_name} "
    start = script_text.index(start_token)
    brace_start = script_text.index("{", start)
    depth = 0
    for index in range(brace_start, len(script_text)):
        char = script_text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return script_text[start : index + 1]
    raise AssertionError(f"Could not extract PowerShell function {function_name}")
