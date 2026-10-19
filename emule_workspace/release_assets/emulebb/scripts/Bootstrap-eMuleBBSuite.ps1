#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('Core', 'Full')]
    [string]$Bundle = 'Full',

    [string[]]$Apps,

    [ValidateSet('English', 'Spanish', 'Italian', 'Portuguese')]
    [string]$Language = 'English',

    [ValidateSet('', 'English', 'Spanish', 'Italian', 'Portuguese')]
    [string]$UiLanguage = '',

    [ValidateRange(0, 65535)]
    [int]$PortBlockStart = 0,

    [string]$InstallRoot = 'C:\eMuleBBSuite',

    [string]$Version,

    [ValidateSet('', 'x64', 'ARM64')]
    [string]$Platform = '',

    [string]$EmulebbPackageZip,
    [string]$EmulebbPackageManifest,
    [string]$DependencyManifest,
    [string]$SuiteScriptsZip,
    [string]$SuiteScriptsManifest,
    [string]$AutomationExamplesZip,
    [string]$AutomationExamplesManifest,

    [string]$P2PBindInterface,
    [string]$ControlBindAddress,
    [string]$EmulebbBindAddress,
    [string]$ProwlarrBindAddress,
    [string]$RadarrBindAddress,
    [string]$SonarrBindAddress,
    [string]$LidarrBindAddress,
    [string]$WhisparrBindAddress,

    [ValidateRange(0, 65535)]
    [int]$EmulebbPort = 0,
    [ValidateRange(0, 65535)]
    [int]$ProwlarrPort = 0,
    [ValidateRange(0, 65535)]
    [int]$RadarrPort = 0,
    [ValidateRange(0, 65535)]
    [int]$SonarrPort = 0,
    [ValidateRange(0, 65535)]
    [int]$LidarrPort = 0,
    [ValidateRange(0, 65535)]
    [int]$WhisparrPort = 0,

    [switch]$IncludeNightly,
    [switch]$IncludePrerelease,
    [switch]$AllowRemoteServiceBind,
    [switch]$NonInteractive,
    [switch]$NoStart,
    [switch]$InstallMediaTools,
    [switch]$NoMediaTools,
    [switch]$NoSuiteScriptsBundle,
    [switch]$AllowSuiteScriptsVersionMismatch,
    [switch]$Force,
    [switch]$DryRun,
    [switch]$KeepDownloads
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Enable-Tls12 {
    try {
        $tls12 = [Net.SecurityProtocolType]::Tls12
        if (([Net.ServicePointManager]::SecurityProtocol -band $tls12) -ne $tls12) {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor $tls12
        }
    } catch {
        Write-Warning "Could not enable TLS 1.2 for HTTPS downloads. If GitHub downloads fail, use the local package ZIP parameters. $($_.Exception.Message)"
    }
}

Enable-Tls12

if ($InstallMediaTools -and $NoMediaTools) {
    throw 'Use either -InstallMediaTools or -NoMediaTools, not both.'
}

if ($IncludePrerelease) {
    Write-Warning 'The -IncludePrerelease switch is deprecated. Release candidates now install by default; pass -IncludeNightly to opt into nightly builds.'
    $IncludeNightly = $true
}

$Repository = 'emulebb/emulebb'
$ApiBase = 'https://api.github.com'
$UserAgent = 'emulebb-suite-bootstrapper'

function Write-Step {
    param([string]$Message)
    Write-Host "[eMuleBB bootstrap] $Message"
}

function Get-LocalPackageRecoveryMessage {
    return @(
        'If GitHub is blocked, offline, or rate limited, download the package assets in a browser from GitHub Releases.',
        'For eMuleBB, open https://github.com/emulebb/emulebb/releases and download the matching emulebb-<version>-<arch>.zip plus emulebb-<version>-<arch>.manifest.json.',
        'Then rerun this bootstrapper with -EmulebbPackageZip C:\Path\emulebb-<version>-<arch>.zip -EmulebbPackageManifest C:\Path\emulebb-<version>-<arch>.manifest.json.'
    ) -join ' '
}

function Get-HttpErrorDetail {
    param($Exception)
    if ($null -eq $Exception -or $null -eq $Exception.Response) {
        return ''
    }
    $response = $Exception.Response
    $status = 0
    try { $status = [int]$response.StatusCode } catch { $status = 0 }
    $statusText = if ($status -gt 0) { "HTTP $status" } else { 'HTTP request failed' }
    try {
        if (-not [string]::IsNullOrWhiteSpace([string]$response.StatusDescription)) {
            $statusText = "$statusText $($response.StatusDescription)"
        }
    } catch {
    }
    $detail = ''
    try {
        $stream = $response.GetResponseStream()
        if ($null -ne $stream) {
            $reader = New-Object IO.StreamReader($stream)
            try {
                $detail = $reader.ReadToEnd()
            } finally {
                $reader.Dispose()
            }
        }
    } catch {
    }
    $detail = ([string]$detail -replace '\s+', ' ').Trim()
    if ($detail.Length -gt 1200) {
        $detail = $detail.Substring(0, 1200) + '...'
    }
    if ([string]::IsNullOrWhiteSpace($detail)) {
        return $statusText
    }
    return "$statusText`: $detail"
}

function Get-ExceptionMessage {
    param($Exception)
    $detail = Get-HttpErrorDetail -Exception $Exception
    if (-not [string]::IsNullOrWhiteSpace($detail)) {
        return $detail
    }
    return $Exception.Message
}

function Read-JsonFile {
    param([string]$Path, [string]$Description)
    try {
        return Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
    } catch {
        throw "$Description is not valid JSON: $Path. Download or regenerate this file, then rerun the bootstrapper. $($_.Exception.Message)"
    }
}

function Get-HttpStatusCode {
    param($Exception)
    try {
        if ($null -ne $Exception -and $null -ne $Exception.Response) {
            return [int]$Exception.Response.StatusCode
        }
    } catch {
    }
    return 0
}

function Invoke-GitHubApi {
    param([string]$Uri, [string]$Description, [int]$MaxAttempts = 4)
    # Use Invoke-WebRequest + ConvertFrom-Json (Invoke-RestMethod can collapse a JSON
    # array into a single object on some hosts) and retry transient gateway / throttle /
    # network failures (e.g. GitHub HTTP 502/503/504, 429) so a flaky endpoint does not
    # abort the install.
    $attempt = 0
    while ($true) {
        $attempt++
        try {
            $response = Invoke-WebRequest -Uri $Uri -Headers @{ 'User-Agent' = $UserAgent } -UseBasicParsing -ErrorAction Stop
            return ($response.Content | ConvertFrom-Json)
        } catch {
            $status = Get-HttpStatusCode -Exception $_.Exception
            $transient = ($status -ge 500) -or ($status -eq 408) -or ($status -eq 429) -or ($status -eq 0)
            if ((-not $transient) -or ($attempt -ge $MaxAttempts)) {
                throw "Could not read $Description from GitHub: $(Get-ExceptionMessage -Exception $_.Exception) $(Get-LocalPackageRecoveryMessage)"
            }
            $reason = if ($status -gt 0) { "HTTP $status" } else { 'network error' }
            $delay = [int][Math]::Min(20, [Math]::Pow(2, $attempt))
            Write-Warning "Could not read $Description from GitHub - transient $reason; retrying in $delay s (attempt $attempt of $($MaxAttempts - 1))..."
            Start-Sleep -Seconds $delay
        }
    }
}

function Get-DefaultPlatform {
    if ([string]::IsNullOrWhiteSpace($Platform)) {
        if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') {
            return 'ARM64'
        }
        return 'x64'
    }
    return $Platform
}

function Get-ReleaseTag {
    if ([string]::IsNullOrWhiteSpace($Version)) {
        return $null
    }
    if ($Version -match '^emulebb-(v|nightly-)') {
        return $Version
    }
    if ($Version -match '^\d+\.\d+\.\d+-nightly\.(\d{8})\.([0-9a-fA-F]+)$') {
        return "emulebb-nightly-$($Matches[1])-$($Matches[2])"
    }
    return "emulebb-v$Version"
}

function Test-SupportedReleaseTag {
    param([string]$Tag)
    return $Tag -match '^emulebb-v.+' -or $Tag -match '^emulebb-nightly-\d{8}-[0-9a-fA-F]+(-run\d+)?$'
}

function Test-NightlyReleaseTag {
    param([string]$Tag)
    return $Tag -match '^emulebb-nightly-\d{8}-[0-9a-fA-F]+(-run\d+)?$'
}


function Get-Release {
    # Resolve everything from the /releases list (which already carries each release's
    # assets) and never call /releases/tags/<tag>: that endpoint can 504 while the list
    # stays healthy, and it would be a redundant second request.
    $releases = Invoke-GitHubApi -Uri "$ApiBase/repos/$Repository/releases?per_page=100" -Description 'eMuleBB releases'
    $explicitTag = Get-ReleaseTag
    if (-not [string]::IsNullOrWhiteSpace($explicitTag)) {
        foreach ($release in @($releases)) {
            if ([string]$release.tag_name -eq $explicitTag) {
                return $release
            }
        }
        throw "Release $explicitTag was not found among the latest eMuleBB releases. $(Get-LocalPackageRecoveryMessage)"
    }
    # GitHub returns releases newest-first, so the first supported non-nightly tag is the
    # latest release candidate or stable release. Nightly builds are opt-in via -IncludeNightly.
    foreach ($release in @($releases)) {
        if ($release.draft) {
            continue
        }
        $tag = [string]$release.tag_name
        if (-not (Test-SupportedReleaseTag -Tag $tag)) {
            continue
        }
        if ((Test-NightlyReleaseTag -Tag $tag) -and -not $IncludeNightly) {
            continue
        }
        return $release
    }
    if ($IncludeNightly) {
        throw 'No matching eMuleBB release was found.'
    }
    throw 'No matching eMuleBB stable release or release candidate was found. Pass -IncludeNightly to install the latest nightly build.'
}


function Get-ReleaseVersion {
    param([object]$Release, [string]$AssetArch)
    $tag = [string]$Release.tag_name
    if ($tag -match '^emulebb-v(.+)$') {
        return $Matches[1]
    }
    if ($tag -match '^emulebb-nightly-\d{8}-[0-9a-fA-F]+(-run\d+)?$') {
        if ([string]::IsNullOrWhiteSpace($AssetArch)) {
            throw "Cannot resolve nightly package version without an asset architecture."
        }
        $escapedAssetArch = [regex]::Escape($AssetArch)
        foreach ($asset in @($Release.assets)) {
            $name = [string]$asset.name
            if ($name -match "^emulebb-.+-diagnostics-$escapedAssetArch\.zip$") {
                continue
            }
            if ($name -match "^emulebb-(.+)-$escapedAssetArch\.zip$") {
                return $Matches[1]
            }
        }
        throw "Nightly release $tag does not contain a standard eMuleBB $AssetArch ZIP asset."
    }
    throw "Unexpected release tag: $tag"
}

function Get-LocalEmulebbPackageVersion {
    param([string]$ZipPath)
    $name = [IO.Path]::GetFileName($ZipPath)
    if ($name -match '^emulebb-(.+?)(-diagnostics)?-(x64|arm64)\.zip$') {
        return $Matches[1]
    }
    if (-not [string]::IsNullOrWhiteSpace($Version)) {
        return $Version
    }
    throw "Cannot infer eMuleBB package version from local ZIP name: $name. Pass -Version explicitly."
}

function Get-LocalEmulebbPackagePlatform {
    param([string]$ZipPath)
    $name = [IO.Path]::GetFileName($ZipPath)
    if ($name -match '^emulebb-.+?(-diagnostics)?-arm64\.zip$') {
        return 'ARM64'
    }
    if ($name -match '^emulebb-.+?(-diagnostics)?-x64\.zip$') {
        return 'x64'
    }
    return Get-DefaultPlatform
}



function Get-ReleaseBaseUrl {
    param([object]$Release, [string]$ReleaseRepository = $Repository)
    return "https://github.com/$ReleaseRepository/releases/download/$($Release.tag_name)"
}

function Get-AssetUrl {
    param([object]$Release, [string]$Name)
    $url = Find-AssetUrl -Release $Release -Name $Name
    if (-not [string]::IsNullOrWhiteSpace($url)) {
        return $url
    }
    throw "Release $($Release.tag_name) does not contain asset $Name."
}

function Find-AssetUrl {
    param([object]$Release, [string]$Name)
    foreach ($asset in @($Release.assets)) {
        if ([string]$asset.name -eq $Name) {
            return [string]$asset.browser_download_url
        }
    }
    return ''
}

function Get-SuiteScriptsBundleVersion {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ''
    }
    $uri = $null
    if ([Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri) -and -not [string]::IsNullOrWhiteSpace($uri.AbsolutePath)) {
        $leaf = [IO.Path]::GetFileName($uri.AbsolutePath)
    } else {
        $leaf = [IO.Path]::GetFileName($Value)
    }
    if ($leaf -match '^suite-scripts-(.+?)(?:\.manifest\.json|\.zip)$') {
        return $Matches[1]
    }
    return ''
}

function Get-AutomationExamplesBundleVersion {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ''
    }
    $uri = $null
    if ([Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri) -and -not [string]::IsNullOrWhiteSpace($uri.AbsolutePath)) {
        $leaf = [IO.Path]::GetFileName($uri.AbsolutePath)
    } else {
        $leaf = [IO.Path]::GetFileName($Value)
    }
    if ($leaf -match '^automation-examples-(.+?)(?:\.manifest\.json|\.zip)$') {
        return $Matches[1]
    }
    return ''
}

function Assert-SuiteScriptsBundleVersion {
    param([string]$Zip, [string]$Manifest, [string]$ExpectedVersion)
    $zipVersion = Get-SuiteScriptsBundleVersion -Value $Zip
    $manifestVersion = Get-SuiteScriptsBundleVersion -Value $Manifest
    if (-not [string]::IsNullOrWhiteSpace($zipVersion) -and -not [string]::IsNullOrWhiteSpace($manifestVersion) -and $zipVersion -ne $manifestVersion) {
        throw "Suite scripts bundle ZIP version $zipVersion does not match manifest version $manifestVersion."
    }
    $bundleVersion = if (-not [string]::IsNullOrWhiteSpace($zipVersion)) { $zipVersion } else { $manifestVersion }
    if ([string]::IsNullOrWhiteSpace($bundleVersion)) {
        return
    }
    if ($bundleVersion -ne $ExpectedVersion) {
        throw "Suite scripts bundle version $bundleVersion does not match eMuleBB package version $ExpectedVersion. Pass -AllowSuiteScriptsVersionMismatch only when testing a manually assembled local bundle."
    }
}

function Assert-AutomationExamplesBundleVersion {
    param([string]$Zip, [string]$Manifest, [string]$ExpectedVersion)
    $zipVersion = Get-AutomationExamplesBundleVersion -Value $Zip
    $manifestVersion = Get-AutomationExamplesBundleVersion -Value $Manifest
    if (-not [string]::IsNullOrWhiteSpace($zipVersion) -and -not [string]::IsNullOrWhiteSpace($manifestVersion) -and $zipVersion -ne $manifestVersion) {
        throw "Automation examples ZIP version $zipVersion does not match manifest version $manifestVersion."
    }
    $bundleVersion = if (-not [string]::IsNullOrWhiteSpace($zipVersion)) { $zipVersion } else { $manifestVersion }
    if ([string]::IsNullOrWhiteSpace($bundleVersion)) {
        return
    }
    if ($bundleVersion -ne $ExpectedVersion) {
        throw "Automation examples version $bundleVersion does not match eMuleBB package version $ExpectedVersion."
    }
}

function Resolve-ReleaseSuiteScriptsBundle {
    param([object]$Release, [string]$ResolvedVersion)
    $zipName = "suite-scripts-$ResolvedVersion.zip"
    $manifestName = "suite-scripts-$ResolvedVersion.manifest.json"
    $zipUrl = Find-AssetUrl -Release $Release -Name $zipName
    $manifestUrl = Find-AssetUrl -Release $Release -Name $manifestName
    if ([string]::IsNullOrWhiteSpace($zipUrl) -and [string]::IsNullOrWhiteSpace($manifestUrl)) {
        return $null
    }
    if ([string]::IsNullOrWhiteSpace($zipUrl) -or [string]::IsNullOrWhiteSpace($manifestUrl)) {
        throw "Release $($Release.tag_name) has incomplete suite scripts bundle assets. Expected $zipName and $manifestName."
    }
    return [ordered]@{
        Zip = $zipUrl
        Manifest = $manifestUrl
    }
}

function Resolve-ReleaseAutomationExamplesBundle {
    param([object]$Release, [string]$ResolvedVersion)
    $zipName = "automation-examples-$ResolvedVersion.zip"
    $manifestName = "automation-examples-$ResolvedVersion.manifest.json"
    $zipUrl = Find-AssetUrl -Release $Release -Name $zipName
    $manifestUrl = Find-AssetUrl -Release $Release -Name $manifestName
    if ([string]::IsNullOrWhiteSpace($zipUrl) -and [string]::IsNullOrWhiteSpace($manifestUrl)) {
        return $null
    }
    if ([string]::IsNullOrWhiteSpace($zipUrl) -or [string]::IsNullOrWhiteSpace($manifestUrl)) {
        throw "Release $($Release.tag_name) has incomplete automation examples assets. Expected $zipName and $manifestName."
    }
    return [ordered]@{
        Zip = $zipUrl
        Manifest = $manifestUrl
    }
}

function Resolve-AdjacentSuiteScriptsBundle {
    param([string]$PackageZip, [string]$ResolvedVersion)
    $packageDir = Split-Path -Parent ([IO.Path]::GetFullPath($PackageZip))
    $zipPath = Join-Path $packageDir "suite-scripts-$ResolvedVersion.zip"
    $manifestPath = Join-Path $packageDir "suite-scripts-$ResolvedVersion.manifest.json"
    $hasZip = Test-Path -LiteralPath $zipPath -PathType Leaf
    $hasManifest = Test-Path -LiteralPath $manifestPath -PathType Leaf
    if (-not $hasZip -and -not $hasManifest) {
        return $null
    }
    if (-not $hasZip -or -not $hasManifest) {
        throw "Local suite scripts bundle is incomplete. Expected $zipPath and $manifestPath."
    }
    return [ordered]@{
        Zip = [IO.Path]::GetFullPath($zipPath)
        Manifest = [IO.Path]::GetFullPath($manifestPath)
    }
}

function Resolve-AdjacentAutomationExamplesBundle {
    param([string]$PackageZip, [string]$ResolvedVersion)
    $packageDir = Split-Path -Parent ([IO.Path]::GetFullPath($PackageZip))
    $zipPath = Join-Path $packageDir "automation-examples-$ResolvedVersion.zip"
    $manifestPath = Join-Path $packageDir "automation-examples-$ResolvedVersion.manifest.json"
    $hasZip = Test-Path -LiteralPath $zipPath -PathType Leaf
    $hasManifest = Test-Path -LiteralPath $manifestPath -PathType Leaf
    if (-not $hasZip -and -not $hasManifest) {
        return $null
    }
    if (-not $hasZip -or -not $hasManifest) {
        throw "Local automation examples bundle is incomplete. Expected $zipPath and $manifestPath."
    }
    return [ordered]@{
        Zip = [IO.Path]::GetFullPath($zipPath)
        Manifest = [IO.Path]::GetFullPath($manifestPath)
    }
}

function Test-InteractiveConsole {
    try {
        return -not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected
    } catch {
        return $false
    }
}

function Resolve-EffectiveBundle {
    param([object]$Release, [string]$RequestedBundle, [string]$ResolvedPlatform, [string]$ResolvedVersion)
    if ($RequestedBundle -eq 'Core') {
        return 'Core'
    }
    return $RequestedBundle
}


function Format-DownloadSize {
    param([long]$Bytes)
    if ($Bytes -ge 1GB) {
        return ('{0:N1} GB' -f ($Bytes / 1GB))
    }
    if ($Bytes -ge 1MB) {
        return ('{0:N1} MB' -f ($Bytes / 1MB))
    }
    if ($Bytes -ge 1KB) {
        return ('{0:N1} KB' -f ($Bytes / 1KB))
    }
    return ('{0} bytes' -f $Bytes)
}

function Format-DownloadRate {
    param([long]$Bytes, [datetime]$StartedAt)
    $elapsed = [Math]::Max(0.001, ((Get-Date) - $StartedAt).TotalSeconds)
    return ('{0}/s' -f (Format-DownloadSize -Bytes ([long]($Bytes / $elapsed))))
}

function Invoke-HttpDownload {
    param([string]$Url, [string]$TempPath, [string]$Destination)
    $activity = "Downloading $(Split-Path -Leaf $Destination)"
    Write-Host "  $activity"
    $invokeWebRequestCommand = Get-Command Invoke-WebRequest -ErrorAction SilentlyContinue
    if ($null -ne $invokeWebRequestCommand -and $invokeWebRequestCommand.CommandType -eq 'Function') {
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $TempPath -Headers @{ 'User-Agent' = $UserAgent } -ErrorAction Stop
        if (Test-Path -LiteralPath $TempPath) {
            Write-Host ('  Downloaded {0}' -f (Format-DownloadSize -Bytes ([IO.FileInfo]$TempPath).Length))
        }
        return
    }
    $request = [System.Net.HttpWebRequest][System.Net.WebRequest]::Create($Url)
    $request.UserAgent = $UserAgent
    $request.AllowAutoRedirect = $true
    $response = $request.GetResponse()
    try {
        $total = [long]$response.ContentLength
        $inputStream = $response.GetResponseStream()
        $outputStream = [System.IO.File]::Open($TempPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        try {
            $buffer = New-Object byte[] (1024 * 1024)
            $downloaded = [long]0
            $startedAt = Get-Date
            $nextHostReport = $startedAt
            $lastStatusLength = 0
            do {
                $read = $inputStream.Read($buffer, 0, $buffer.Length)
                if ($read -le 0) {
                    break
                }
                $outputStream.Write($buffer, 0, $read)
                $downloaded += $read
                $now = Get-Date
                $rate = Format-DownloadRate -Bytes $downloaded -StartedAt $startedAt
                if ($total -gt 0) {
                    $percent = [int]([Math]::Min(100, [Math]::Floor(($downloaded * 100.0) / $total)))
                    $status = '{0} of {1} ({2}%, {3})' -f (Format-DownloadSize -Bytes $downloaded), (Format-DownloadSize -Bytes $total), $percent, $rate
                    Write-Progress -Activity $activity -Status $status -PercentComplete $percent
                } else {
                    $status = '{0} downloaded ({1})' -f (Format-DownloadSize -Bytes $downloaded), $rate
                    Write-Progress -Activity $activity -Status $status
                }
                if ($now -ge $nextHostReport) {
                    $line = "  $status"
                    $padding = if ($lastStatusLength -gt $line.Length) { ' ' * ($lastStatusLength - $line.Length) } else { '' }
                    Write-Host -NoNewline ("`r$line$padding")
                    $lastStatusLength = $line.Length
                    $nextHostReport = $now.AddSeconds(1)
                }
            } while ($true)
            Write-Progress -Activity $activity -Completed
            if ($lastStatusLength -gt 0) {
                Write-Host ''
            }
            Write-Host ('  Downloaded {0}' -f (Format-DownloadSize -Bytes $downloaded))
        } finally {
            $outputStream.Dispose()
            if ($null -ne $inputStream) {
                $inputStream.Dispose()
            }
        }
    } finally {
        $response.Dispose()
    }
}

function Invoke-Download {
    param([string]$Url, [string]$Destination)
    if ($DryRun) {
        Write-Step "Would download $Url -> $Destination"
        return
    }
    $parent = Split-Path -Parent $Destination
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $tmp = "$Destination.tmp"
    $maxAttempts = 4
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        Remove-Item -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
        try {
            Invoke-HttpDownload -Url $Url -TempPath $tmp -Destination $Destination
            Move-Item -Force -LiteralPath $tmp -Destination $Destination
            return
        } catch {
            $message = Get-ExceptionMessage -Exception $_.Exception
            if ($attempt -ge $maxAttempts) {
                throw "Failed to download $Url -> $Destination after $maxAttempts attempts. $message $(Get-LocalPackageRecoveryMessage)"
            }
            $delaySeconds = [Math]::Min(30, 5 * $attempt)
            Write-Warning "Download attempt $attempt of $maxAttempts failed for $Url. $message Retrying in $delaySeconds seconds..."
            Start-Sleep -Seconds $delaySeconds
        } finally {
            Remove-Item -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
        }
    }
}

function Get-Sha256 {
    param([string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        } finally {
            $sha.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

function Assert-FileHash {
    param([string]$Path, [string]$ExpectedSha256)
    if ($DryRun) {
        return
    }
    $actual = Get-Sha256 -Path $Path
    if ($actual -ne $ExpectedSha256.ToLowerInvariant()) {
        throw "SHA256 mismatch for $Path. Expected $ExpectedSha256, got $actual."
    }
}

function Assert-RequiredSha256 {
    param([string]$Value, [string]$Description)
    if ($Value -notmatch '^[0-9a-fA-F]{64}$') {
        throw "$Description must include a SHA256 hash."
    }
}

function Resolve-LocalManifestPath {
    param([string]$ZipPath, [string]$ManifestPath)
    if (-not [string]::IsNullOrWhiteSpace($ManifestPath)) {
        return [IO.Path]::GetFullPath($ManifestPath)
    }
    if ([string]::IsNullOrWhiteSpace($ZipPath)) {
        return ''
    }
    $candidate = [IO.Path]::Combine([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($ZipPath)), ([IO.Path]::GetFileNameWithoutExtension($ZipPath) + '.manifest.json'))
    if (Test-Path -LiteralPath $candidate) {
        return $candidate
    }
    return ''
}

function Assert-LocalPackage {
    param([string]$Name, [string]$ZipPath, [string]$ManifestPath)
    if ($DryRun) {
        return
    }
    if (-not (Test-Path -LiteralPath $ZipPath)) {
        throw "$Name local package ZIP is missing: $ZipPath"
    }
    $resolvedManifest = Resolve-LocalManifestPath -ZipPath $ZipPath -ManifestPath $ManifestPath
    if ([string]::IsNullOrWhiteSpace($resolvedManifest)) {
        Write-Step "Using local $Name package $ZipPath without a manifest sidecar"
        return
    }
    if (-not (Test-Path -LiteralPath $resolvedManifest)) {
        throw "$Name local package manifest is missing: $resolvedManifest"
    }
    $manifest = Read-JsonFile -Path $resolvedManifest -Description "$Name local package manifest"
    if ($null -eq $manifest.PSObject.Properties['sha256'] -or [string]::IsNullOrWhiteSpace([string]$manifest.sha256)) {
        throw "$Name local package manifest does not contain sha256: $resolvedManifest"
    }
    Assert-RequiredSha256 -Value ([string]$manifest.sha256) -Description "$Name local package manifest"
    Assert-FileHash -Path $ZipPath -ExpectedSha256 ([string]$manifest.sha256)
    Write-Step "Using local $Name package $ZipPath with manifest $resolvedManifest"
}

function Expand-Installer {
    param([string]$Archive, [string]$Destination)
    if ($DryRun) {
        Write-Step "Would extract suite installer from $Archive -> $Destination"
        return (Join-Path $Destination 'Install-eMuleBBSuite.ps1')
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        $entry = $zip.GetEntry('eMuleBB/scripts/Install-eMuleBBSuite.ps1')
        if ($null -eq $entry) {
            throw 'Release ZIP does not contain eMuleBB/scripts/Install-eMuleBBSuite.ps1.'
        }
        $target = Join-Path $Destination 'Install-eMuleBBSuite.ps1'
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
        $helperEntry = $zip.GetEntry('eMuleBB/scripts/Import-SuiteAppManifest.ps1')
        if ($null -ne $helperEntry) {
            [IO.Compression.ZipFileExtensions]::ExtractToFile($helperEntry, (Join-Path $Destination 'Import-SuiteAppManifest.ps1'), $true)
        }
        return $target
    } finally {
        $zip.Dispose()
    }
}

$localEmulebbPackage = -not [string]::IsNullOrWhiteSpace($EmulebbPackageZip)
if ($localEmulebbPackage) {
    $zipPath = [IO.Path]::GetFullPath($EmulebbPackageZip)
    $resolvedPlatform = if ([string]::IsNullOrWhiteSpace($Platform)) { Get-LocalEmulebbPackagePlatform -ZipPath $zipPath } else { $Platform }
    $assetArch = if ($resolvedPlatform -eq 'ARM64') { 'arm64' } else { 'x64' }
    $resolvedVersion = Get-LocalEmulebbPackageVersion -ZipPath $zipPath
    $releaseBaseUrl = ''
    $effectiveBundle = Resolve-EffectiveBundle -Release $null -RequestedBundle $Bundle -ResolvedPlatform $resolvedPlatform -ResolvedVersion $resolvedVersion
    Assert-LocalPackage -Name 'eMuleBB' -ZipPath $zipPath -ManifestPath $EmulebbPackageManifest
    Write-Step "Resolved local eMuleBB package $zipPath for $resolvedPlatform"
} else {
    $resolvedPlatform = Get-DefaultPlatform
    $release = Get-Release
    $assetArch = if ($resolvedPlatform -eq 'ARM64') { 'arm64' } else { 'x64' }
    $resolvedVersion = Get-ReleaseVersion -Release $release -AssetArch $assetArch
    $releaseBaseUrl = Get-ReleaseBaseUrl -Release $release
    $zipName = "emulebb-$resolvedVersion-$assetArch.zip"
    $manifestName = "emulebb-$resolvedVersion-$assetArch.manifest.json"
    $zipUrl = Get-AssetUrl -Release $release -Name $zipName
    $manifestUrl = Get-AssetUrl -Release $release -Name $manifestName
    $effectiveBundle = Resolve-EffectiveBundle -Release $release -RequestedBundle $Bundle -ResolvedPlatform $resolvedPlatform -ResolvedVersion $resolvedVersion
    $workRoot = Join-Path $env:TEMP "emulebb-suite-bootstrap-$resolvedVersion-$assetArch"
    $zipPath = Join-Path $workRoot $zipName
    $manifestPath = Join-Path $workRoot $manifestName
    Write-Step "Resolved release $($release.tag_name) for $resolvedPlatform"
    Invoke-Download -Url $manifestUrl -Destination $manifestPath
    Invoke-Download -Url $zipUrl -Destination $zipPath
    if (-not $DryRun) {
        $manifest = Read-JsonFile -Path $manifestPath -Description 'Downloaded eMuleBB release manifest'
        Assert-RequiredSha256 -Value ([string]$manifest.sha256) -Description 'Downloaded eMuleBB release manifest'
        Assert-FileHash -Path $zipPath -ExpectedSha256 ([string]$manifest.sha256)
    }
}

$suiteScriptsBundle = $null
$automationExamplesBundle = $null
$explicitSuiteScriptsBundle = -not [string]::IsNullOrWhiteSpace($SuiteScriptsZip) -or -not [string]::IsNullOrWhiteSpace($SuiteScriptsManifest)
if ($explicitSuiteScriptsBundle -and -not $AllowSuiteScriptsVersionMismatch) {
    Assert-SuiteScriptsBundleVersion -Zip $SuiteScriptsZip -Manifest $SuiteScriptsManifest -ExpectedVersion $resolvedVersion
}
$explicitAutomationExamplesBundle = -not [string]::IsNullOrWhiteSpace($AutomationExamplesZip) -or -not [string]::IsNullOrWhiteSpace($AutomationExamplesManifest)
if ($explicitAutomationExamplesBundle) {
    if ([string]::IsNullOrWhiteSpace($AutomationExamplesZip) -or [string]::IsNullOrWhiteSpace($AutomationExamplesManifest)) {
        throw 'AutomationExamplesZip requires -AutomationExamplesManifest with a SHA256 hash.'
    }
    Assert-AutomationExamplesBundleVersion -Zip $AutomationExamplesZip -Manifest $AutomationExamplesManifest -ExpectedVersion $resolvedVersion
}
if (-not $NoSuiteScriptsBundle -and -not $explicitSuiteScriptsBundle) {
    if ($localEmulebbPackage) {
        $suiteScriptsBundle = Resolve-AdjacentSuiteScriptsBundle -PackageZip $zipPath -ResolvedVersion $resolvedVersion
    } else {
        $suiteScriptsBundle = Resolve-ReleaseSuiteScriptsBundle -Release $release -ResolvedVersion $resolvedVersion
    }
    if ($null -ne $suiteScriptsBundle) {
        Write-Step "Resolved suite scripts bundle $($suiteScriptsBundle.Zip)"
    }
}
if (-not $explicitAutomationExamplesBundle) {
    if ($localEmulebbPackage) {
        $automationExamplesBundle = Resolve-AdjacentAutomationExamplesBundle -PackageZip $zipPath -ResolvedVersion $resolvedVersion
    } else {
        $automationExamplesBundle = Resolve-ReleaseAutomationExamplesBundle -Release $release -ResolvedVersion $resolvedVersion
    }
    if ($null -ne $automationExamplesBundle) {
        Write-Step "Resolved automation examples bundle $($automationExamplesBundle.Zip)"
    }
}
$workRoot = Join-Path $env:TEMP "emulebb-suite-bootstrap-$resolvedVersion-$assetArch"
$extractRoot = Join-Path $workRoot 'installer'
if (-not $DryRun -and (Test-Path -LiteralPath $extractRoot)) {
    Remove-Item -Recurse -Force -LiteralPath $extractRoot
}
$installer = Expand-Installer -Archive $zipPath -Destination $extractRoot
$installerParams = [ordered]@{
    Bundle = $effectiveBundle
    InstallRoot = $InstallRoot
    Version = $resolvedVersion
    Platform = $resolvedPlatform
    Language = $Language
}
if ($PSBoundParameters.ContainsKey('UiLanguage')) { $installerParams['UiLanguage'] = $UiLanguage }
if ($null -ne $Apps -and @($Apps).Count -gt 0) { $installerParams['Apps'] = $Apps }
if ($PSBoundParameters.ContainsKey('PortBlockStart')) { $installerParams['PortBlockStart'] = $PortBlockStart }
if (-not [string]::IsNullOrWhiteSpace($releaseBaseUrl)) { $installerParams['ReleaseBaseUrl'] = $releaseBaseUrl }
if ($localEmulebbPackage) {
    $installerParams['EmulebbPackageZip'] = $zipPath
    if (-not [string]::IsNullOrWhiteSpace($EmulebbPackageManifest)) { $installerParams['EmulebbPackageManifest'] = [IO.Path]::GetFullPath($EmulebbPackageManifest) }
}
if ($NonInteractive) { $installerParams['NonInteractive'] = $true }
if ($NoStart) { $installerParams['NoStart'] = $true }
if ($InstallMediaTools) { $installerParams['InstallMediaTools'] = $true }
if ($NoMediaTools) { $installerParams['NoMediaTools'] = $true }
if ($Force) { $installerParams['Force'] = $true }
if ($KeepDownloads) { $installerParams['KeepDownloads'] = $true }
if (-not [string]::IsNullOrWhiteSpace($P2PBindInterface)) { $installerParams['P2PBindInterface'] = $P2PBindInterface }
if (-not [string]::IsNullOrWhiteSpace($DependencyManifest)) { $installerParams['DependencyManifest'] = [IO.Path]::GetFullPath($DependencyManifest) }
if (-not [string]::IsNullOrWhiteSpace($SuiteScriptsZip)) { $installerParams['SuiteScriptsZip'] = $SuiteScriptsZip }
if (-not [string]::IsNullOrWhiteSpace($SuiteScriptsManifest)) { $installerParams['SuiteScriptsManifest'] = $SuiteScriptsManifest }
if (-not [string]::IsNullOrWhiteSpace($AutomationExamplesZip)) { $installerParams['AutomationExamplesZip'] = $AutomationExamplesZip }
if (-not [string]::IsNullOrWhiteSpace($AutomationExamplesManifest)) { $installerParams['AutomationExamplesManifest'] = $AutomationExamplesManifest }
if ($null -ne $suiteScriptsBundle) {
    $installerParams['SuiteScriptsZip'] = $suiteScriptsBundle.Zip
    $installerParams['SuiteScriptsManifest'] = $suiteScriptsBundle.Manifest
}
if ($null -ne $automationExamplesBundle) {
    $installerParams['AutomationExamplesZip'] = $automationExamplesBundle.Zip
    $installerParams['AutomationExamplesManifest'] = $automationExamplesBundle.Manifest
}
if (-not [string]::IsNullOrWhiteSpace($ControlBindAddress)) { $installerParams['ControlBindAddress'] = $ControlBindAddress }
if (-not [string]::IsNullOrWhiteSpace($EmulebbBindAddress)) { $installerParams['EmulebbBindAddress'] = $EmulebbBindAddress }
if (-not [string]::IsNullOrWhiteSpace($ProwlarrBindAddress)) { $installerParams['ProwlarrBindAddress'] = $ProwlarrBindAddress }
if (-not [string]::IsNullOrWhiteSpace($RadarrBindAddress)) { $installerParams['RadarrBindAddress'] = $RadarrBindAddress }
if (-not [string]::IsNullOrWhiteSpace($SonarrBindAddress)) { $installerParams['SonarrBindAddress'] = $SonarrBindAddress }
if (-not [string]::IsNullOrWhiteSpace($LidarrBindAddress)) { $installerParams['LidarrBindAddress'] = $LidarrBindAddress }
if (-not [string]::IsNullOrWhiteSpace($WhisparrBindAddress)) { $installerParams['WhisparrBindAddress'] = $WhisparrBindAddress }
if ($PSBoundParameters.ContainsKey('EmulebbPort')) { $installerParams['EmulebbPort'] = $EmulebbPort }
if ($PSBoundParameters.ContainsKey('ProwlarrPort')) { $installerParams['ProwlarrPort'] = $ProwlarrPort }
if ($PSBoundParameters.ContainsKey('RadarrPort')) { $installerParams['RadarrPort'] = $RadarrPort }
if ($PSBoundParameters.ContainsKey('SonarrPort')) { $installerParams['SonarrPort'] = $SonarrPort }
if ($PSBoundParameters.ContainsKey('LidarrPort')) { $installerParams['LidarrPort'] = $LidarrPort }
if ($PSBoundParameters.ContainsKey('WhisparrPort')) { $installerParams['WhisparrPort'] = $WhisparrPort }
if ($AllowRemoteServiceBind) { $installerParams['AllowRemoteServiceBind'] = $true }
$displayArgs = @()
foreach ($name in $installerParams.Keys) {
    $value = $installerParams[$name]
    if ($value -is [bool]) {
        if ($value) {
            $displayArgs += "-$name"
        }
    } else {
        $displayArgs += @("-$name", [string]$value)
    }
}
if ($DryRun) {
    Write-Step "Would run $installer $($displayArgs -join ' ')"
} else {
    & $installer @installerParams
}
if (-not $KeepDownloads -and -not $DryRun) {
    Remove-Item -Recurse -Force -LiteralPath $workRoot -ErrorAction SilentlyContinue
}
