#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$EmuleBootstrapFiles = @(
    @{ Name = 'server.met'; Url = 'https://upd.emule-security.org/server.met' },
    @{ Name = 'nodes.dat'; Url = 'https://upd.emule-security.org/nodes.dat' }
)

function Read-SuiteConfig {
    $configPath = Join-Path $Root 'manifests\suite-config.json'
    if (-not (Test-Path -LiteralPath $configPath)) {
        throw "Suite config is missing: $configPath. Re-run scripts\Install-eMuleBBSuite.ps1 with -Force to rebuild it."
    }
    return Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
}

function Test-SelectedApp {
    param($Config, [string]$Name)
    return @($Config.selectedApps) -contains $Name
}

function Get-ServiceClientHost {
    param([string]$ServiceName, $Service)
    if (-not [string]::IsNullOrWhiteSpace([string]$Service.clientHost)) {
        return [string]$Service.clientHost
    }
    return [string]$Service.bindAddress
}

function Get-ServiceUrl {
    param([string]$Name, $Service)
    return "http://$(Get-ServiceClientHost -ServiceName $Name -Service $Service):$([int]$Service.port)"
}

function Get-ExceptionMessage {
    param($Exception)
    $response = $null
    if ($null -ne $Exception) {
        try {
            $responseProperty = $Exception.PSObject.Properties['Response']
            if ($null -ne $responseProperty) {
                $response = $responseProperty.Value
            }
        } catch {
        }
    }
    if ($null -ne $response) {
        try {
            return "HTTP $([int]$response.StatusCode) $($response.StatusDescription)"
        } catch {
        }
    }
    if ($null -eq $Exception) {
        return ''
    }
    return $Exception.Message
}

function Wait-Json {
    param([string]$Name, [string]$Uri, [hashtable]$Headers = @{})
    $lastError = ''
    for ($i = 0; $i -lt 90; $i++) {
        try {
            Invoke-RestMethod -Uri $Uri -Headers $Headers -TimeoutSec 2 -ErrorAction Stop | Out-Null
            return
        } catch {
            $lastError = Get-ExceptionMessage -Exception $_.Exception
            Start-Sleep -Seconds 1
        }
    }
    throw "Timed out waiting for $Name at $Uri. Last error: $lastError"
}

function Invoke-SuiteJsonApi {
    param([string]$Name, [string]$Uri, [string]$Method = 'GET', [hashtable]$Headers = @{}, $Body = $null)
    try {
        if ($null -eq $Body) {
            return Invoke-RestMethod -Uri $Uri -Method $Method -Headers $Headers -TimeoutSec 20 -ErrorAction Stop
        }
        return Invoke-RestMethod -Uri $Uri -Method $Method -Headers $Headers -Body ($Body | ConvertTo-Json -Depth 20) -ContentType 'application/json' -TimeoutSec 20 -ErrorAction Stop
    } catch {
        throw "$Name failed at $Uri. $(Get-ExceptionMessage -Exception $_.Exception)"
    }
}

function Invoke-SuiteJsonApiList {
    param([string]$Name, [string]$Uri, [hashtable]$Headers = @{})
    return @(Invoke-SuiteJsonApi -Name $Name -Uri $Uri -Headers $Headers | ForEach-Object { $_ })
}

function Write-Utf8NoBomFile {
    param([string]$Path, [string]$Text)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Invoke-EmuleBootstrapFileDownload {
    param([string]$Name, [string]$Url, [string]$Destination)
    $parent = Split-Path -Parent $Destination
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    if ((Test-Path -LiteralPath $Destination) -and ([IO.FileInfo]$Destination).Length -gt 0) {
        Write-Host "eMuleBB bootstrap file already present: $Name"
        return
    }
    $tmp = "$Destination.tmp"
    $maxAttempts = 4
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        Remove-Item -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
        try {
            Write-Host "Downloading eMuleBB bootstrap file $Name"
            Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $tmp -ErrorAction Stop
            if (-not (Test-Path -LiteralPath $tmp) -or ([IO.FileInfo]$tmp).Length -le 0) {
                throw "Downloaded $Name was empty."
            }
            Move-Item -Force -LiteralPath $tmp -Destination $Destination
            Write-Host "eMuleBB bootstrap file ready: $Name"
            return
        } catch {
            $message = Get-ExceptionMessage -Exception $_.Exception
            if ($attempt -ge $maxAttempts) {
                throw "Failed to download eMuleBB bootstrap file $Name from $Url after $maxAttempts attempts. $message"
            }
            $delaySeconds = [Math]::Min(30, 5 * $attempt)
            Write-Warning "Download attempt $attempt of $maxAttempts failed for eMuleBB bootstrap file $Name. $message Retrying in $delaySeconds seconds..."
            Start-Sleep -Seconds $delaySeconds
        } finally {
            Remove-Item -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
        }
    }
}

function Ensure-EmuleBootstrapFiles {
    $configDir = Join-Path (Join-Path $Root 'profiles\emulebb') 'config'
    foreach ($file in $EmuleBootstrapFiles) {
        Invoke-EmuleBootstrapFileDownload -Name ([string]$file.Name) -Url ([string]$file.Url) -Destination (Join-Path $configDir ([string]$file.Name))
    }
}

function Set-ObjectProperty {
    param($Target, [string]$Name, $Value)
    if ($null -ne $Target.PSObject.Properties[$Name]) {
        $Target.$Name = $Value
    } else {
        $Target | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force
    }
}

function Get-ObjectPropertyValue {
    param($Target, [string]$Name, $Default = $null)
    if ($null -eq $Target -or $null -eq $Target.PSObject.Properties[$Name]) {
        return $Default
    }
    return $Target.PSObject.Properties[$Name].Value
}

function Get-OrCreateObjectProperty {
    param($Target, [string]$Name)
    $value = Get-ObjectPropertyValue -Target $Target -Name $Name -Default $null
    if ($null -eq $value) {
        $value = [pscustomobject]@{}
        Set-ObjectProperty -Target $Target -Name $Name -Value $value
    }
    return $value
}



function Set-ArrHostCredentials {
    param([string]$Name, [string]$Url, [string]$ApiPath, [string]$ApiKey)
    $hostConfigUrl = "$Url/$ApiPath/config/host"
    $headers = @{ 'X-Api-Key' = $ApiKey }
    $hostConfig = Invoke-SuiteJsonApi -Name "$Name host config read" -Uri $hostConfigUrl -Headers $headers
    Set-ObjectProperty -Target $hostConfig -Name 'authenticationMethod' -Value 'forms'
    Set-ObjectProperty -Target $hostConfig -Name 'authenticationRequired' -Value 'enabled'
    Set-ObjectProperty -Target $hostConfig -Name 'username' -Value ([string]$Config.credentials.username)
    Set-ObjectProperty -Target $hostConfig -Name 'password' -Value ([string]$Config.credentials.password)
    Set-ObjectProperty -Target $hostConfig -Name 'passwordConfirmation' -Value ([string]$Config.credentials.password)
    [void](Invoke-SuiteJsonApi -Name "$Name web login update" -Uri $hostConfigUrl -Method 'PUT' -Headers $headers -Body $hostConfig)
}

function Get-ArrUiLanguageValue {
    param([string]$Name, [string]$Language)
    $normalizedLanguage = $Language.ToLowerInvariant()
    if ($Name.ToLowerInvariant() -eq 'prowlarr') {
        switch ($normalizedLanguage) {
            'spanish' { return 'es' }
            'italian' { return 'it' }
            'portuguese' { return 'pt' }
            default { return 'en' }
        }
    }
    switch ($normalizedLanguage) {
        'spanish' { return 3 }
        'italian' { return 5 }
        'portuguese' { return 18 }
        default { return 1 }
    }
}

function Set-ArrUiLanguage {
    param([string]$Name, [string]$Url, [string]$ApiPath, [string]$ApiKey, [string]$Language)
    if ([string]::IsNullOrWhiteSpace($Language)) {
        return
    }
    $uiConfigUrl = "$Url/$ApiPath/config/ui"
    $headers = @{ 'X-Api-Key' = $ApiKey }
    $uiConfig = Invoke-SuiteJsonApi -Name "$Name UI config read" -Uri $uiConfigUrl -Headers $headers
    Set-ObjectProperty -Target $uiConfig -Name 'uiLanguage' -Value (Get-ArrUiLanguageValue -Name $Name -Language $Language)
    [void](Invoke-SuiteJsonApi -Name "$Name UI language update" -Uri $uiConfigUrl -Method 'PUT' -Headers $headers -Body $uiConfig)
    Write-Host "$Name UI language set to $Language."
}

function Get-FirstObjectWithId {
    param($Items)
    foreach ($item in @($Items)) {
        if ($null -ne $item -and $item.PSObject.Properties['id']) {
            return $item
        }
    }
    return $null
}

function Wait-ArrDefaultProfiles {
    param([string]$Name, [string]$Url, [string]$ApiPath, [hashtable]$Headers, [switch]$IncludeMetadata)
    $lastError = ''
    for ($attempt = 1; $attempt -le 60; $attempt++) {
        try {
            $qualityProfiles = @(Invoke-SuiteJsonApiList -Name "$Name quality profile list" -Uri "$Url/$ApiPath/qualityprofile" -Headers $Headers)
            $qualityProfile = Get-FirstObjectWithId -Items $qualityProfiles
            $metadataProfile = $null
            if ($IncludeMetadata) {
                $metadataProfiles = @(Invoke-SuiteJsonApiList -Name "$Name metadata profile list" -Uri "$Url/$ApiPath/metadataprofile" -Headers $Headers)
                $metadataProfile = Get-FirstObjectWithId -Items $metadataProfiles
            }
            if ($null -ne $qualityProfile -and (-not $IncludeMetadata -or $null -ne $metadataProfile)) {
                return [pscustomobject]@{
                    QualityProfile = $qualityProfile
                    MetadataProfile = $metadataProfile
                }
            }
            $lastError = 'profile list was empty'
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Seconds 1
    }
    throw "$Name did not expose the default profiles required to create a root folder. Last error: $lastError"
}

function Ensure-ArrRootFolder {
    param([string]$Name, [string]$Url, [string]$ApiPath, [string]$ApiKey, [string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return
    }
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
    $rootFolderUrl = "$Url/$ApiPath/rootfolder"
    $headers = @{ 'X-Api-Key' = $ApiKey }
    $normalizedPath = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $rootFolders = @(Invoke-SuiteJsonApiList -Name "$Name root folder list" -Uri $rootFolderUrl -Headers $headers)
    foreach ($rootFolder in $rootFolders) {
        if ($rootFolder.PSObject.Properties['path'] -and [string]::Equals(([IO.Path]::GetFullPath([string]$rootFolder.path).TrimEnd('\')), $normalizedPath, [StringComparison]::OrdinalIgnoreCase)) {
            Write-Host "$Name root folder already configured: $normalizedPath"
            return
        }
    }
    try {
        $body = @{ path = $normalizedPath }
        if ([string]::Equals($Name, 'Lidarr', [StringComparison]::OrdinalIgnoreCase)) {
            $profiles = Wait-ArrDefaultProfiles -Name $Name -Url $Url -ApiPath $ApiPath -Headers $headers -IncludeMetadata
            $body = @{
                path = $normalizedPath
                name = 'eMuleBB Music'
                defaultQualityProfileId = [int]$profiles.QualityProfile.id
                defaultMetadataProfileId = [int]$profiles.MetadataProfile.id
            }
        }
        [void](Invoke-SuiteJsonApi -Name "$Name root folder create" -Uri $rootFolderUrl -Method 'POST' -Headers $headers -Body $body)
        Write-Host "$Name root folder configured: $normalizedPath"
    } catch {
        $message = Get-ExceptionMessage -Exception $_.Exception
        if ($message -match 'already configured as a root folder') {
            Write-Host "$Name root folder already configured: $normalizedPath"
            return
        }
        throw
    }
}

function Get-ArrLanguageCandidates {
    param([string]$Language)
    switch ($Language.ToLowerInvariant()) {
        'spanish' { return @('Spanish', 'Spanish (Latino)') }
        'italian' { return @('Italian') }
        'portuguese' { return @('Portuguese', 'Portuguese (Brazil)') }
        default { return @($Language) }
    }
}

function Find-ArrLanguage {
    param($Languages, [string]$Language)
    $candidates = @(Get-ArrLanguageCandidates -Language $Language)
    foreach ($candidate in $candidates) {
        foreach ($item in @($Languages)) {
            if ([string]::Equals([string]$item.name, $candidate, [StringComparison]::OrdinalIgnoreCase) -or
                [string]::Equals([string]$item.nameLower, $candidate.ToLowerInvariant(), [StringComparison]::OrdinalIgnoreCase)) {
                return $item
            }
        }
    }
    return $null
}

function Wait-ArrContentLanguage {
    param([string]$Name, [string]$Url, [string]$ApiPath, [hashtable]$Headers, [string]$Language)
    $lastError = ''
    for ($attempt = 1; $attempt -le 60; $attempt++) {
        try {
            $languages = @(Invoke-SuiteJsonApiList -Name "$Name language list" -Uri "$Url/$ApiPath/language" -Headers $Headers)
            $languageMatch = Find-ArrLanguage -Languages $languages -Language $Language
            if ($null -ne $languageMatch) {
                return $languageMatch
            }
            $lastError = "language list did not include '$Language'"
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Seconds 1
    }
    throw "$Name did not expose content language '$Language'. Last error: $lastError"
}

function Set-ArrPreferredContentLanguage {
    param([string]$Name, [string]$Url, [string]$ApiPath, [string]$ApiKey, [string]$Language)
    if ([string]::IsNullOrWhiteSpace($Language) -or $Language -eq 'English') {
        return
    }
    $headers = @{ 'X-Api-Key' = $ApiKey }
    try {
        $languageMatch = Wait-ArrContentLanguage -Name $Name -Url $Url -ApiPath $ApiPath -Headers $headers -Language $Language
        $profiles = @(Invoke-SuiteJsonApiList -Name "$Name quality profile list" -Uri "$Url/$ApiPath/qualityprofile" -Headers $headers)
        foreach ($profile in $profiles) {
            $changed = $false
            if ($profile.PSObject.Properties['language']) {
                $profile.language = $languageMatch
                $changed = $true
            }
            if ($profile.PSObject.Properties['languages'] -and $profile.languages -is [System.Collections.IEnumerable]) {
                $ordered = New-Object System.Collections.Generic.List[object]
                $ordered.Add($languageMatch)
                foreach ($language in @($profile.languages)) {
                    if (-not [string]::Equals([string]$language.name, [string]$languageMatch.name, [StringComparison]::OrdinalIgnoreCase)) {
                        $ordered.Add($language)
                    }
                }
                $profile.languages = @($ordered)
                $changed = $true
            }
            if ($changed -and $profile.PSObject.Properties['id']) {
                [void](Invoke-SuiteJsonApi -Name "$Name quality profile language update" -Uri "$Url/$ApiPath/qualityprofile/$([int]$profile.id)" -Method 'PUT' -Headers $headers -Body $profile)
            }
        }
        Write-Host "$Name content language preference set to prefer $Language where supported."
    } catch {
        Write-Warning "$Name content language preference could not be applied. Existing profiles were left usable. $($_.Exception.Message)"
    }
}

function Invoke-StepWithRetry {
    param([string]$Name, [scriptblock]$Operation)
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            & $Operation
            return
        } catch {
            if ($attempt -ge 3) { throw }
            Write-Warning "$Name failed on attempt ${attempt}: $($_.Exception.Message)"
            & (Join-Path $Root 'scripts\Start-Suite.ps1')
            Start-Sleep -Seconds 3
        }
    }
}

function Get-ArrDisplayName {
    param([string]$Name)
    return $Name.Substring(0, 1).ToUpperInvariant() + $Name.Substring(1)
}

function Get-ArrApiPath {
    param([string]$Name)
    switch ($Name) {
        'prowlarr' { return 'api/v1' }
        'lidarr' { return 'api/v1' }
        default { return 'api/v3' }
    }
}

function Get-ArrMediaRoot {
    param([string]$Name)
    switch ($Name) {
        'radarr' { return 'media\movies' }
        'sonarr' { return 'media\series' }
        'lidarr' { return 'media\music' }
        'whisparr' { return 'media\whisparr' }
        default { return '' }
    }
}

$Config = Read-SuiteConfig
$EmuleUrl = Get-ServiceUrl -Name 'emulebb' -Service $Config.services.emulebb
$EmuleKey = [string]$Config.services.emulebb.apiKey

Ensure-EmuleBootstrapFiles
& (Join-Path $Root 'scripts\Start-Suite.ps1')
Wait-Json -Name 'eMuleBB' -Uri "$EmuleUrl/api/v1/app" -Headers @{ 'X-API-Key' = $EmuleKey }

# WHY: the Register-* helpers below intentionally receive suite credentials (API
# keys, password) as plaintext command-line arguments. This is local single-user
# suite setup; the same values are generated by and stored in
# manifests\suite-config.json. Transient command-line visibility to other local
# users is an accepted trade-off for this local automation, not a defect.

$selectedArr = @(@($Config.selectedApps) | Where-Object { @('prowlarr', 'radarr', 'sonarr', 'lidarr', 'whisparr') -contains $_ })
if (@($selectedArr).Count -gt 0) {
    $arrUrls = @{}
    $suiteAppsManifest = Join-Path $Root 'config\suite-apps.json'
    foreach ($arrName in $selectedArr) {
        $arrUrls[$arrName] = Get-ServiceUrl -Name $arrName -Service $Config.services.$arrName
        $display = Get-ArrDisplayName -Name $arrName
        $apiPath = Get-ArrApiPath -Name $arrName
        Wait-Json -Name $display -Uri "$($arrUrls[$arrName])/$apiPath/system/status" -Headers @{ 'X-Api-Key' = [string]$Config.services.$arrName.apiKey }
        Invoke-StepWithRetry -Name "$display web login setup" -Operation {
            Set-ArrHostCredentials -Name $display -Url $arrUrls[$arrName] -ApiPath $apiPath -ApiKey ([string]$Config.services.$arrName.apiKey)
            Set-ArrUiLanguage -Name $display -Url $arrUrls[$arrName] -ApiPath $apiPath -ApiKey ([string]$Config.services.$arrName.apiKey) -Language ([string]$Config.language.arrUiLanguage)
        }
        $mediaRoot = Get-ArrMediaRoot -Name $arrName
        if (-not [string]::IsNullOrWhiteSpace($mediaRoot)) {
            Invoke-StepWithRetry -Name "$display root folder setup" -Operation {
                Ensure-ArrRootFolder -Name $display -Url $arrUrls[$arrName] -ApiPath $apiPath -ApiKey ([string]$Config.services.$arrName.apiKey) -Path (Join-Path $Root $mediaRoot)
            }
            Invoke-StepWithRetry -Name "$display content language preference" -Operation {
                Set-ArrPreferredContentLanguage -Name $display -Url $arrUrls[$arrName] -ApiPath $apiPath -ApiKey ([string]$Config.services.$arrName.apiKey) -Language ([string]$Config.language.arrContentLanguage)
            }
        }
    }

    if (Test-SelectedApp -Config $Config -Name 'prowlarr') {
        Invoke-StepWithRetry -Name 'Prowlarr registration' -Operation {
            & (Join-Path $Root 'apps\eMuleBB\scripts\Register-Prowlarr.ps1') -Action Register -ProwlarrUrl $arrUrls['prowlarr'] -ProwlarrApiKey ([string]$Config.services.prowlarr.apiKey) -EmulebbBaseUrl $EmuleUrl -EmulebbApiKey $EmuleKey -IndexerName 'eMuleBB Suite' -AppProfileName 'eMuleBB Suite' -NoRetry
        }
        foreach ($arrName in @($selectedArr | Where-Object { $_ -ne 'prowlarr' })) {
            $display = Get-ArrDisplayName -Name $arrName
            Invoke-StepWithRetry -Name "$display registration" -Operation {
                $args = @{
                    Action = 'Register'
                    Target = $display
                    EmulebbBaseUrl = $EmuleUrl
                    EmulebbApiKey = $EmuleKey
                    EmulebbCategoryPath = (Join-Path $Root "downloads\$arrName")
                    ProwlarrUrl = $arrUrls['prowlarr']
                    ProwlarrApiKey = [string]$Config.services.prowlarr.apiKey
                    SuiteAppsManifest = $suiteAppsManifest
                    DownloadClientName = 'eMuleBB Suite'
                    SkipProwlarrSync = $true
                    NoRetry = $true
                }
                $args["${display}Url"] = $arrUrls[$arrName]
                $args["${display}ApiKey"] = [string]$Config.services.$arrName.apiKey
                & (Join-Path $Root 'apps\eMuleBB\scripts\Register-ArrStack.ps1') @args
            }
        }
        Invoke-StepWithRetry -Name 'Prowlarr application sync' -Operation {
            & (Join-Path $Root 'apps\eMuleBB\scripts\Register-ArrStack.ps1') -SyncProwlarrOnly -ProwlarrUrl $arrUrls['prowlarr'] -ProwlarrApiKey ([string]$Config.services.prowlarr.apiKey) -SuiteAppsManifest $suiteAppsManifest -NoRetry
        }
    }
}
