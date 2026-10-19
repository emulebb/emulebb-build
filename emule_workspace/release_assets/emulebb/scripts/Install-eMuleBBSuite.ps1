#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Bundle = 'Full',

    [string]$InstallRoot = 'C:\eMuleBBSuite',

    [string]$Version = '0.7.3-rc.2',

    [ValidateSet('x64', 'ARM64')]
    [string]$Platform = 'x64',

    [ValidateSet('Production', 'Development', 'Test')]
    [string]$InstallKind = 'Production',

    [ValidateSet('Pinned', 'Latest')]
    [string]$DependencyChannel = 'Pinned',

    [string[]]$Apps,

    [ValidateSet('English', 'Spanish', 'Italian', 'Portuguese')]
    [string]$Language = 'English',

    [ValidateSet('', 'English', 'Spanish', 'Italian', 'Portuguese')]
    [string]$UiLanguage = '',

    [ValidateRange(0, 65535)]
    [int]$PortBlockStart = 0,

    [string]$ReleaseBaseUrl,
    [string]$EmulebbPackageZip,
    [string]$EmulebbPackageManifest,
    [string]$DependencyManifest,
    [string]$SuiteScriptsZip,
    [string]$SuiteScriptsManifest,
    [string]$AutomationExamplesZip,
    [string]$AutomationExamplesManifest,
    [string]$ImportProfileDir,
    [string]$EmulebbPdbPath,
    [ValidateSet('standard', 'diagnostics')]
    [string]$EmulebbPackageFlavor = 'standard',

    [string]$ConfigFile,

    [string]$ControlBindAddress,
    [string]$EmulebbBindAddress,
    [string]$ProwlarrBindAddress,
    [string]$RadarrBindAddress,
    [string]$SonarrBindAddress,
    [string]$LidarrBindAddress,
    [string]$WhisparrBindAddress,

    [string]$SuiteUsername = 'admin',
    [string]$SuitePassword,

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

    [string]$P2PBindInterface,

    [switch]$AllowRemoteServiceBind,
    [switch]$NonInteractive,
    [switch]$DryRun,
    [switch]$Force,
    [switch]$NoStart,
    [switch]$InstallMediaTools,
    [switch]$NoMediaTools,
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
        Write-Warning "Could not enable TLS 1.2 for HTTPS downloads. If downloads fail, download the ZIP assets in a browser and pass the local package parameters. $($_.Exception.Message)"
    }
}

Enable-Tls12

if ($Bundle -like '-*') {
    throw "Install-eMuleBBSuite.ps1 was invoked with positional parameter strings. Call it with named parameters, for example -Bundle Full, not an argv string array."
}
if ($InstallMediaTools -and $NoMediaTools) {
    throw 'Use either -InstallMediaTools or -NoMediaTools, not both.'
}
$script:InstallerBoundParameters = $PSBoundParameters
. (Join-Path $PSScriptRoot 'Import-SuiteAppManifest.ps1')

$AutoPortRangeStart = 49152
$AutoPortRangeEnd = 65535
$CoreServiceNames = @('emulebb')
$DefaultArrAppNames = @('prowlarr', 'radarr', 'sonarr', 'lidarr')
$AllArrAppNames = @('prowlarr', 'radarr', 'sonarr', 'lidarr', 'whisparr')
$SuiteServiceOrder = @('emulebb', 'prowlarr', 'radarr', 'sonarr', 'lidarr', 'whisparr')
# P2P companion clients are parked for the 0.7.3 RC3/final release path. Keep
# the parser/install helpers below for the future suite expansion, but ship no
# selectable side P2P client in the MFC PowerShell bundle.
$AllP2pClientNames = @()
$P2pClientMetadata = @{}
$FallbackLanguageOptions = @{
    english = @{
        DisplayName = 'English'
        EmuleLanguageId = 9
        EmuleLocale = 'en_US'
        ArrUiLanguage = 'English'
        ArrContentLanguage = 'English'
    }
    spanish = @{
        DisplayName = 'Spanish'
        EmuleLanguageId = 1034
        EmuleLocale = 'es_ES_T'
        ArrUiLanguage = 'Spanish'
        ArrContentLanguage = 'Spanish'
    }
    italian = @{
        DisplayName = 'Italian'
        EmuleLanguageId = 16
        EmuleLocale = 'it_IT'
        ArrUiLanguage = 'Italian'
        ArrContentLanguage = 'Italian'
    }
    portuguese = @{
        DisplayName = 'Portuguese'
        EmuleLanguageId = 2070
        EmuleLocale = 'pt_PT'
        ArrUiLanguage = 'Portuguese'
        ArrContentLanguage = 'Portuguese'
    }
}
$script:LanguageOptionsCache = $null
$ArrAppMetadata = @{
    prowlarr = @{
        DisplayName = 'Prowlarr'
        Exe = 'Prowlarr.exe'
        ApiPath = 'api/v1'
        DataDir = 'data\prowlarr'
        MediaRoot = ''
    }
    radarr = @{
        DisplayName = 'Radarr'
        Exe = 'Radarr.exe'
        ApiPath = 'api/v3'
        DataDir = 'data\radarr'
        MediaRoot = 'media\movies'
    }
    sonarr = @{
        DisplayName = 'Sonarr'
        Exe = 'Sonarr.exe'
        ApiPath = 'api/v3'
        DataDir = 'data\sonarr'
        MediaRoot = 'media\series'
    }
    lidarr = @{
        DisplayName = 'Lidarr'
        Exe = 'Lidarr.exe'
        ApiPath = 'api/v1'
        DataDir = 'data\lidarr'
        MediaRoot = 'media\music'
    }
    whisparr = @{
        DisplayName = 'Whisparr'
        Exe = 'Whisparr.exe'
        ApiPath = 'api/v3'
        DataDir = 'data\whisparr'
        MediaRoot = 'media\whisparr'
    }
}
$PinnedDependencies = @{
    prowlarr = @{
        Repo = 'Prowlarr/Prowlarr'
        Tag = 'v2.4.0.5397'
        Pattern = 'windows(?:-core)?-x64\.zip$'
        Exe = 'Prowlarr.exe'
        Url = 'https://github.com/Prowlarr/Prowlarr/releases/download/v2.4.0.5397/Prowlarr.master.2.4.0.5397.windows-core-x64.zip'
        Sha256 = 'c97754d8bf36b224e21f081b4f0bcc4d3cbf61fb84c087aeaa30f60d31012530'
    }
    radarr = @{
        Repo = 'Radarr/Radarr'
        Tag = 'v6.2.1.10461'
        Pattern = 'windows(?:-core)?-x64\.zip$'
        Exe = 'Radarr.exe'
        Url = 'https://github.com/Radarr/Radarr/releases/download/v6.2.1.10461/Radarr.master.6.2.1.10461.windows-core-x64.zip'
        Sha256 = 'c9a6ecd1d84923947f6f92f16b43780744834e0a2be7a1ee04bf86e82113500b'
    }
    sonarr = @{
        Repo = 'Sonarr/Sonarr'
        Tag = 'v4.0.17.2952'
        Pattern = 'win(?:dows)?(?:-core)?-x64\.zip$'
        Exe = 'Sonarr.exe'
        Url = 'https://github.com/Sonarr/Sonarr/releases/download/v4.0.17.2952/Sonarr.main.4.0.17.2952.win-x64.zip'
        Sha256 = '19a81e69dedd8d317b5fa8a1a9c48d63bc3b3f3ba87b84c94ff6d75b1803e419'
    }
    lidarr = @{
        Repo = 'Lidarr/Lidarr'
        Tag = 'v3.1.0.4875'
        Pattern = 'windows(?:-core)?-x64\.zip$'
        Exe = 'Lidarr.exe'
        Url = 'https://github.com/Lidarr/Lidarr/releases/download/v3.1.0.4875/Lidarr.master.3.1.0.4875.windows-core-x64.zip'
        Sha256 = '26f6da02a579aa84f49f2ececc27ad0d3a82760d3cd3b13fa4d99df7c6c12443'
    }
    whisparr = @{
        Repo = 'Whisparr/Whisparr'
        Tag = 'v2.2.0-release.108'
        Pattern = 'win(?:dows)?(?:-core)?-x64\.zip$'
        Exe = 'Whisparr.exe'
        Url = 'https://github.com/Whisparr/Whisparr/releases/download/v2.2.0-release.108/Whisparr.2.2.0-release.108.win-x64.zip'
        Sha256 = '7e90caa524c18bb6e8d63be2e1e3c79e1e06fa023e9db1568d6096deae83d9c7'
    }
}
$PinnedMediaTools = @{
    'mpc-hc' = @{
        DisplayName = 'MPC-HC'
        Destination = 'apps\MPC-HC'
        Executable = 'mpc-hc64.exe'
        Dependency = @{
            FileName = 'MPC-HC.2.7.2.x64.zip'
            Url = 'https://github.com/clsid2/mpc-hc/releases/download/2.7.2/MPC-HC.2.7.2.x64.zip'
            Sha256 = '658b755c069ac3c3ed6265378baad3e7d270be12bd9642abd5b402b9f95a54ed'
        }
    }
    ffmpeg = @{
        DisplayName = 'FFmpeg'
        Destination = 'tools\ffmpeg'
        Executable = 'ffmpeg.exe'
        Dependency = @{
            FileName = 'ffmpeg-release-essentials.zip'
            Url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
            Sha256 = '6f58ce889f59c311410f7d2b18895b33c03456463486f3b1ebc93d97a0f54541'
        }
    }
    mediainfo = @{
        DisplayName = 'MediaInfo DLL'
        Destination = 'tools\mediainfo'
        Executable = 'MediaInfo.dll'
        Dependency = @{
            FileName = 'MediaInfo_DLL_26.05_Windows_x64_WithoutInstaller.zip'
            Url = 'https://mediaarea.net/download/binary/libmediainfo0/26.05/MediaInfo_DLL_26.05_Windows_x64_WithoutInstaller.zip'
            Sha256 = 'f8c81699550a3a9425e9bdd1d6621587c463c51c568848ab8d3e36fe5efc222c'
        }
    }
}

function Write-Step {
    param([string]$Message)
    Write-Host "[eMuleBB suite] $Message"
}

function Assert-NoSpaces {
    param([string]$Path)
    if ($Path -match '\s') {
        throw "InstallRoot must not contain spaces for v1 suite installs: $Path. Choose a short folder such as C:\eMuleBBSuite or C:\eMuleBB, not C:\Program Files\... or a path under a user profile with spaces."
    }
}

function Assert-InstallRootValue {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw 'InstallRoot is required. Pass -InstallRoot C:\eMuleBBSuite or choose a short absolute folder in the installer wizard.'
    }
    if ($Path -match '[<>|"*?]') {
        throw "InstallRoot contains characters Windows cannot use in folder names: $Path. Choose a short absolute folder such as C:\eMuleBBSuite or C:\eMuleBB."
    }
    $root = [IO.Path]::GetPathRoot($Path)
    if ([string]::IsNullOrWhiteSpace($root) -or -not [IO.Path]::IsPathRooted($Path) -or $root -notmatch '^[A-Za-z]:\\$') {
        throw "InstallRoot must be an absolute drive path such as C:\eMuleBBSuite or D:\eMuleBB, not '$Path'."
    }
}

function New-Secret {
    param([int]$Length = 24)
    $alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
    $result = New-Object System.Text.StringBuilder
    $buffer = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        while ($result.Length -lt $Length) {
            $rng.GetBytes($buffer)
            foreach ($byte in $buffer) {
                if ($byte -ge 248) {
                    continue
                }
                [void]$result.Append($alphabet[$byte % $alphabet.Length])
                if ($result.Length -eq $Length) {
                    break
                }
            }
        }
    } finally {
        $rng.Dispose()
    }
    return $result.ToString()
}

function Assert-FixedSecret {
    param([string]$Name, [string]$Value)
    if ($Value -notmatch '^[A-Za-z0-9]{24}$') {
        throw "$Name must be exactly 24 alphanumeric characters."
    }
}

function New-SuitePassword {
    return New-Secret
}

function New-ApiKey {
    return New-Secret -Length 24
}

function Resolve-Secret {
    param([string]$Value, [string]$Name = 'Secret')
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return New-Secret
    }
    $trimmed = $Value.Trim()
    Assert-FixedSecret -Name $Name -Value $trimmed
    return $trimmed
}

function Resolve-ApiKey {
    param([string]$Value, [string]$Name)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return New-ApiKey
    }
    $trimmed = $Value.Trim()
    if ($trimmed -notmatch '^[A-Za-z0-9]{24}$') {
        throw "$Name must be exactly 24 alphanumeric characters, or blank to generate a new key."
    }
    return $trimmed
}

function Test-VpnLikeInterfaceName {
    param([string]$Name)
    $lowered = ([string]$Name).ToLowerInvariant()
    foreach ($token in @('vpn', 'hide.me', 'tap', 'tun', 'wireguard', 'tailscale')) {
        if ($lowered.Contains($token)) {
            return $true
        }
    }
    return $false
}

function Test-VirtualLikeInterfaceName {
    param([string]$Name)
    $lowered = ([string]$Name).ToLowerInvariant()
    foreach ($token in @('vethernet', 'default switch', 'hyper-v', 'vmware', 'virtualbox', 'virtual ', 'bluetooth')) {
        if ($lowered.Contains($token)) {
            return $true
        }
    }
    return $false
}

function Test-AutoLanIPv4Address {
    param([string]$Address)
    if ([string]::IsNullOrWhiteSpace($Address)) {
        return $false
    }
    try {
        $parsed = [Net.IPAddress]::Parse($Address)
    } catch {
        return $false
    }
    if ($parsed.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
        return $false
    }
    $bytes = $parsed.GetAddressBytes()
    if ($bytes[0] -eq 10) {
        return $true
    }
    if ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) {
        return $true
    }
    if ($bytes[0] -eq 192 -and $bytes[1] -eq 168) {
        return $true
    }
    return $false
}

function ConvertTo-IPv4SubnetMask {
    param([Nullable[int]]$PrefixLength)
    if ($null -eq $PrefixLength -or $PrefixLength -lt 0 -or $PrefixLength -gt 32) {
        return ''
    }
    $bits = ('1' * $PrefixLength) + ('0' * (32 - $PrefixLength))
    $octets = @()
    for ($offset = 0; $offset -lt 32; $offset += 8) {
        $octets += [Convert]::ToInt32($bits.Substring($offset, 8), 2)
    }
    return ($octets -join '.')
}

function Get-LocalIPv4InterfaceInfos {
    $candidates = @()
    try {
        $adaptersByName = @{}
        $adaptersByIndex = @{}
        try {
            foreach ($adapter in @(Get-NetAdapter -ErrorAction Stop)) {
                $adaptersByName[[string]$adapter.Name] = $adapter
                $adaptersByIndex[[int]$adapter.ifIndex] = $adapter
            }
        } catch {
        }
        $candidates += @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.AddressState -eq 'Preferred' -and $_.IPAddress -notlike '169.254.*' } |
            ForEach-Object {
                $adapter = $null
                if ($adaptersByName.ContainsKey([string]$_.InterfaceAlias)) {
                    $adapter = $adaptersByName[[string]$_.InterfaceAlias]
                } elseif ($adaptersByIndex.ContainsKey([int]$_.InterfaceIndex)) {
                    $adapter = $adaptersByIndex[[int]$_.InterfaceIndex]
                }
                $description = if ($null -eq $adapter) { '' } else { [string]$adapter.InterfaceDescription }
                $status = if ($null -eq $adapter) { '' } else { [string]$adapter.Status }
                $interfaceText = ('{0} {1}' -f $_.InterfaceAlias, $description)
                [pscustomobject]@{
                    InterfaceAlias = [string]$_.InterfaceAlias
                    InterfaceDescription = $description
                    AdapterStatus = $status
                    IPAddress = [string]$_.IPAddress
                    PrefixLength = [int]$_.PrefixLength
                    IsVpnLike = [bool](Test-VpnLikeInterfaceName -Name $interfaceText)
                    IsVirtualLike = [bool](Test-VirtualLikeInterfaceName -Name $interfaceText)
                }
            })
    } catch {
        try {
            $candidates += @([System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()) |
                Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork } |
                ForEach-Object {
                    [pscustomobject]@{
                        InterfaceAlias = ''
                        InterfaceDescription = ''
                        AdapterStatus = ''
                        IPAddress = [string]$_.IPAddressToString
                        PrefixLength = $null
                        IsVpnLike = $false
                        IsVirtualLike = $false
                    }
                })
        } catch {
        }
    }
    return @($candidates)
}

function Get-AutoLanCandidateRank {
    param($Candidate)
    if ([string]$Candidate.AdapterStatus -eq 'Up' -and -not [bool]$Candidate.IsVpnLike -and -not [bool]$Candidate.IsVirtualLike) {
        return 0
    }
    if ([string]$Candidate.AdapterStatus -eq 'Up' -and [bool]$Candidate.IsVpnLike) {
        return 1
    }
    if ([string]$Candidate.AdapterStatus -eq 'Up' -and -not [bool]$Candidate.IsVirtualLike) {
        return 2
    }
    if ([bool]$Candidate.IsVpnLike) {
        return 3
    }
    if ([bool]$Candidate.IsVirtualLike) {
        return 4
    }
    return 5
}

function Get-AutoLanBindAddress {
    $privateCandidates = @(Get-LocalIPv4InterfaceInfos | Where-Object { Test-AutoLanIPv4Address -Address $_.IPAddress })
    $candidate = @($privateCandidates | Sort-Object @{ Expression = { Get-AutoLanCandidateRank -Candidate $_ } }, InterfaceAlias, IPAddress | Select-Object -First 1)
    if ($candidate.Count -gt 0) {
        return $candidate[0].IPAddress
    }
    return ''
}

function Test-LoopbackAddressText {
    param([string]$Address)
    return $Address -eq 'localhost' -or $Address -eq '::1' -or $Address -eq '127.0.0.1' -or $Address -match '^127\.'
}

function Test-WildcardBindAddress {
    param([string]$Address)
    return $Address -eq '0.0.0.0' -or $Address -eq '::'
}

function Get-DefaultControlBindAddress {
    $lanAddress = Get-AutoLanBindAddress
    if (-not [string]::IsNullOrWhiteSpace($lanAddress)) {
        return $lanAddress
    }
    return ''
}

function Resolve-ServiceClientHost {
    param([string]$ServiceName, [string]$BindAddress, [string]$ExistingClientHost)
    $bind = ([string]$BindAddress).Trim()
    $existing = ([string]$ExistingClientHost).Trim()
    if (Test-WildcardBindAddress -Address $bind) {
        if (-not [string]::IsNullOrWhiteSpace($existing)) {
            if (Test-WildcardBindAddress -Address $existing) {
                throw "$ServiceName clientHost cannot be a wildcard address: $existing. Set services.$ServiceName.clientHost to a concrete LAN/VPN IP, or explicit 127.0.0.1 for local-only use."
            }
            Assert-ServiceClientHost -ServiceName $ServiceName -ClientHost $existing
            return $existing
        }
        $detected = Get-AutoLanBindAddress
        if (-not [string]::IsNullOrWhiteSpace($detected)) {
            return $detected
        }
        throw "$ServiceName binds to $bind, but no LAN/VPN IPv4 address was detected for generated client URLs. Pass -ControlBindAddress or the service-specific bind parameter with a concrete LAN/VPN IP, or pass explicit 127.0.0.1 for local-only use."
    }
    return $bind
}

function Assert-ServiceClientHost {
    param([string]$ServiceName, [string]$ClientHost)
    if ([string]::IsNullOrWhiteSpace($ClientHost)) {
        throw "$ServiceName clientHost must not be empty."
    }
    if (Test-LoopbackAddressText -Address $ClientHost) {
        return
    }
    if (Test-WildcardBindAddress -Address $ClientHost) {
        throw "$ServiceName clientHost cannot be a wildcard address: $ClientHost. Set services.$ServiceName.clientHost to a concrete LAN/VPN IP, or explicit 127.0.0.1 for local-only use."
    }
    try {
        $parsed = [Net.IPAddress]::Parse($ClientHost)
    } catch {
        throw "$ServiceName clientHost must be an IPv4 address, not '$ClientHost'. Set services.$ServiceName.clientHost to a concrete LAN/VPN IP, or explicit 127.0.0.1 for local-only use."
    }
    if ($parsed.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
        throw "$ServiceName clientHost must be an IPv4 address, not '$ClientHost'."
    }
    $localAddresses = @(Get-LocalIPv4Addresses)
    if ($localAddresses -notcontains $ClientHost) {
        throw "$ServiceName clientHost $ClientHost is not a local IPv4 address. Local addresses: $($localAddresses -join ', '). Re-run scripts\Install-eMuleBBSuite.ps1 with -Force, or edit manifests\suite-config.json and set services.$ServiceName.clientHost to a current LAN/VPN IP."
    }
}

function Set-SuiteClientHosts {
    param([hashtable]$Config)
    foreach ($serviceName in @(Get-SuiteServiceNames -Config $Config)) {
        $service = $Config.services[$serviceName]
        $existing = ''
        if ($service.Contains('clientHost')) {
            $existing = [string]$service.clientHost
        }
        $service.clientHost = Resolve-ServiceClientHost -ServiceName $serviceName -BindAddress ([string]$service.bindAddress) -ExistingClientHost $existing
    }
}

function Get-EmulebbExecutableNameForFlavor {
    param([string]$PackageFlavor)
    if ($PackageFlavor -eq 'diagnostics') {
        return 'emulebb-diagnostics.exe'
    }
    return 'emulebb.exe'
}

function Get-EmulebbPdbFileName {
    param([string]$ExecutableName)
    return [IO.Path]::ChangeExtension($ExecutableName, '.pdb')
}

function Assert-EmulebbExecutableName {
    param([string]$PackageFlavor, [string]$ExecutableName)
    if ([string]::IsNullOrWhiteSpace($ExecutableName)) {
        throw 'EmulebbExecutableName must not be empty.'
    }
    if ($ExecutableName -ne [IO.Path]::GetFileName($ExecutableName)) {
        throw "EmulebbExecutableName must be a file name, not a path: $ExecutableName"
    }
    if ([IO.Path]::GetExtension($ExecutableName) -ne '.exe') {
        throw "EmulebbExecutableName must end with .exe: $ExecutableName"
    }
    $expectedExecutableName = Get-EmulebbExecutableNameForFlavor -PackageFlavor $PackageFlavor
    if ($ExecutableName -ne $expectedExecutableName) {
        throw "EmulebbExecutableName must be $expectedExecutableName for package flavor $PackageFlavor."
    }
}

function Get-LocalPackageVersionFromZipName {
    param([string]$ZipPath, [string]$PackageName)
    if ([string]::IsNullOrWhiteSpace($ZipPath)) {
        return ''
    }
    $name = [IO.Path]::GetFileName($ZipPath)
    if ($PackageName -eq 'eMuleBB' -and $name -match '^emulebb-(.+?)(?:-diagnostics)?-(?:x64|ARM64)\.zip$') {
        return $Matches[1]
    }
    throw "Cannot infer $PackageName package version from local ZIP name: $name. Pass -Version explicitly."
}

function New-SuiteConfig {
    $controlBind = Resolve-OptionalValue -Value $ControlBindAddress -Default (Get-DefaultControlBindAddress)
    $selectedApps = @(Resolve-SelectedApps -BundleName $Bundle -RequestedApps $Apps)
    $languagePreference = Resolve-LanguagePreference -Value $Language
    $uiLanguagePreference = Resolve-LanguagePreference -Value (Resolve-OptionalValue -Value $UiLanguage -Default $Language)
    $resolvedVersion = $Version
    if (-not $script:InstallerBoundParameters.ContainsKey('Version')) {
        $inferredVersion = Get-LocalPackageVersionFromZipName -ZipPath $EmulebbPackageZip -PackageName 'eMuleBB'
        if (-not [string]::IsNullOrWhiteSpace($inferredVersion)) {
            $resolvedVersion = $inferredVersion
        }
    }
    $config = [ordered]@{
        schema = 'emulebb.suite-config.v1'
        bundle = $Bundle
        selectedApps = $selectedApps
        language = [ordered]@{
            name = [string]$languagePreference.DisplayName
            mediaLanguageName = [string]$languagePreference.DisplayName
            uiLanguageName = [string]$uiLanguagePreference.DisplayName
            emuleLanguageId = [int]$uiLanguagePreference.EmuleLanguageId
            emuleLocale = [string]$uiLanguagePreference.EmuleLocale
            arrUiLanguage = [string]$uiLanguagePreference.ArrUiLanguage
            arrContentLanguage = [string]$languagePreference.ArrContentLanguage
            arrContentLanguageMode = 'prefer'
        }
        portBlockStart = $PortBlockStart
        version = $resolvedVersion
        platform = $Platform
        installKind = $InstallKind
        installRoot = $InstallRoot
        dependencyChannel = $DependencyChannel
        releaseBaseUrl = $ReleaseBaseUrl
        packageSources = [ordered]@{
            emulebb = [ordered]@{
                zip = $EmulebbPackageZip
                manifest = $EmulebbPackageManifest
            }
        }
        suiteScripts = [ordered]@{
            zip = $SuiteScriptsZip
            manifest = $SuiteScriptsManifest
        }
        automationExamples = [ordered]@{
            zip = $AutomationExamplesZip
            manifest = $AutomationExamplesManifest
        }
        emulebbPackageFlavor = $EmulebbPackageFlavor
        emulebbExecutableName = (Get-EmulebbExecutableNameForFlavor -PackageFlavor $EmulebbPackageFlavor)
        dependencyManifest = $DependencyManifest
        importProfileDir = $ImportProfileDir
        credentials = [ordered]@{
            username = (Resolve-OptionalValue -Value $SuiteUsername -Default 'admin')
            password = $SuitePassword
        }
        symbols = [ordered]@{
            emulebbPdbPath = $EmulebbPdbPath
        }
        optionalTools = [ordered]@{
            install = [bool]$InstallMediaTools
            mpcHc = [ordered]@{ installed = $false; path = '' }
            ffmpeg = [ordered]@{ installed = $false; path = '' }
            mediainfo = [ordered]@{ installed = $false; path = '' }
        }
        allowRemoteServiceBind = [bool]$AllowRemoteServiceBind
        services = [ordered]@{
            emulebb = [ordered]@{ bindAddress = (Resolve-OptionalValue -Value $EmulebbBindAddress -Default $controlBind); clientHost = ''; port = $EmulebbPort; apiKey = '' }
            prowlarr = [ordered]@{ bindAddress = (Resolve-OptionalValue -Value $ProwlarrBindAddress -Default $controlBind); clientHost = ''; port = $ProwlarrPort; apiKey = '' }
            radarr = [ordered]@{ bindAddress = (Resolve-OptionalValue -Value $RadarrBindAddress -Default $controlBind); clientHost = ''; port = $RadarrPort; apiKey = '' }
            sonarr = [ordered]@{ bindAddress = (Resolve-OptionalValue -Value $SonarrBindAddress -Default $controlBind); clientHost = ''; port = $SonarrPort; apiKey = '' }
            lidarr = [ordered]@{ bindAddress = (Resolve-OptionalValue -Value $LidarrBindAddress -Default $controlBind); clientHost = ''; port = $LidarrPort; apiKey = '' }
            whisparr = [ordered]@{ bindAddress = (Resolve-OptionalValue -Value $WhisparrBindAddress -Default $controlBind); clientHost = ''; port = $WhisparrPort; apiKey = '' }
        }
        p2p = [ordered]@{
            bindInterface = (Resolve-OptionalValue -Value $P2PBindInterface -Default '')
            blockNetworkWhenBindUnavailableAtStartup = $false
            networkGuardMode = 'Off'
            networkGuardAllowedCIDRs = ''
        }
    }
    return $config
}

function Resolve-OptionalValue {
    param([string]$Value, [string]$Default)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $Default
    }
    return $Value.Trim()
}

function ConvertTo-Hashtable {
    param([object]$Value)
    if ($null -eq $Value) {
        return $null
    }
    if ($Value -is [System.Collections.IDictionary]) {
        $result = [ordered]@{}
        foreach ($key in $Value.Keys) {
            $result[$key] = ConvertTo-Hashtable $Value[$key]
        }
        return $result
    }
    if ($Value -is [pscustomobject]) {
        $result = [ordered]@{}
        foreach ($property in $Value.PSObject.Properties) {
            $result[$property.Name] = ConvertTo-Hashtable $property.Value
        }
        return $result
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        $items = @()
        foreach ($item in $Value) {
            $items += ,(ConvertTo-Hashtable $item)
        }
        return $items
    }
    return $Value
}

function Read-JsonFile {
    param([string]$Path, [string]$Description)
    try {
        return Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
    } catch {
        throw "$Description is not valid JSON: $Path. Fix or regenerate this file, then rerun scripts\Install-eMuleBBSuite.ps1. $($_.Exception.Message)"
    }
}

function Initialize-SuiteAppMetadata {
    $manifestPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'config\suite-apps.json'
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        return
    }
    $manifest = Read-SuiteAppManifest -Path $manifestPath
    $script:AllArrAppNames = $manifest.AllArrAppNames
    $script:ArrAppMetadata = $manifest.ArrAppMetadata
    if ($manifest.PinnedDependencies.Count -gt 0) { $script:PinnedDependencies = $manifest.PinnedDependencies }
    if ($manifest.MediaTools.Count -gt 0) { $script:PinnedMediaTools = $manifest.MediaTools }
    if (@($manifest.CoreServiceNames).Count -gt 0) { $script:CoreServiceNames = $manifest.CoreServiceNames }
    if (@($manifest.DefaultArrAppNames).Count -gt 0) { $script:DefaultArrAppNames = $manifest.DefaultArrAppNames }
    if (@($manifest.SuiteServiceOrder).Count -gt 0) { $script:SuiteServiceOrder = $manifest.SuiteServiceOrder }
    if (@($manifest.AllP2pClientNames).Count -gt 0) { $script:AllP2pClientNames = $manifest.AllP2pClientNames }
    if ($manifest.P2pClients.Count -gt 0) { $script:P2pClientMetadata = $manifest.P2pClients }
}

function Merge-Hashtable {
    param([System.Collections.IDictionary]$Target, [System.Collections.IDictionary]$Source)
    foreach ($key in $Source.Keys) {
        if ($Target.Contains($key) -and $Target[$key] -is [System.Collections.IDictionary] -and $Source[$key] -is [System.Collections.IDictionary]) {
            Merge-Hashtable -Target $Target[$key] -Source $Source[$key]
        } else {
            $Target[$key] = $Source[$key]
        }
    }
}

function Normalize-AppName {
    param([string]$Name)
    $normalized = (Resolve-OptionalValue -Value $Name -Default '').ToLowerInvariant()
    if ($normalized -eq 'emulebb' -or $normalized -eq 'core') { return 'emulebb' }
    if ($AllArrAppNames -contains $normalized) { return $normalized }
    throw "Unknown suite app '$Name'. Valid apps are: emulebb, $($AllArrAppNames -join ', '), or presets all, default-arr, all-arr, none-arr."
}

function ConvertTo-LanguageOptions {
    param($Payload)
    $options = @{}
    if ($null -eq $Payload -or $null -eq $Payload.PSObject.Properties['languages']) {
        return $options
    }
    foreach ($entry in @($Payload.languages)) {
        $key = (Resolve-OptionalValue -Value ([string]$entry.key) -Default '').ToLowerInvariant()
        if ([string]::IsNullOrWhiteSpace($key)) {
            continue
        }
        $options[$key] = @{
            DisplayName = [string]$entry.displayName
            EmuleLanguageId = [int]$entry.emuleLanguageId
            EmuleLocale = [string]$entry.emuleLocale
            ArrUiLanguage = [string]$entry.arrUiLanguage
            ArrContentLanguage = [string]$entry.arrContentLanguage
        }
    }
    return $options
}

function Get-LanguageOptions {
    if ($null -ne $script:LanguageOptionsCache) {
        return $script:LanguageOptionsCache
    }
    $manifestPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'config\suite-languages.json'
    if (Test-Path -LiteralPath $manifestPath) {
        $options = ConvertTo-LanguageOptions -Payload (Read-JsonFile -Path $manifestPath -Description 'suite language manifest')
        if ($options.Count -gt 0) {
            $script:LanguageOptionsCache = $options
            return $script:LanguageOptionsCache
        }
    }
    $script:LanguageOptionsCache = $FallbackLanguageOptions
    return $script:LanguageOptionsCache
}

function Resolve-LanguagePreference {
    param([string]$Value)
    $key = (Resolve-OptionalValue -Value $Value -Default 'English').ToLowerInvariant()
    $languageOptions = Get-LanguageOptions
    if (-not $languageOptions.ContainsKey($key)) {
        throw "Language must be English, Spanish, Italian, or Portuguese, not '$Value'."
    }
    return $languageOptions[$key]
}

function Set-MediaLanguagePreference {
    param([hashtable]$Config, [string]$Value)
    $pref = Resolve-LanguagePreference -Value $Value
    $Config.language.name = [string]$pref.DisplayName
    $Config.language.mediaLanguageName = [string]$pref.DisplayName
    $Config.language.arrContentLanguage = [string]$pref.ArrContentLanguage
    $Config.language.arrContentLanguageMode = 'prefer'
}

function Set-UiLanguagePreference {
    param([hashtable]$Config, [string]$Value)
    $pref = Resolve-LanguagePreference -Value $Value
    $Config.language.uiLanguageName = [string]$pref.DisplayName
    $Config.language.emuleLanguageId = [int]$pref.EmuleLanguageId
    $Config.language.emuleLocale = [string]$pref.EmuleLocale
    $Config.language.arrUiLanguage = [string]$pref.ArrUiLanguage
}

function Get-BundleServiceNames {
    param([string]$BundleName)
    if ($BundleName -eq 'Core') {
        return $CoreServiceNames
    }
    return @($CoreServiceNames + $DefaultArrAppNames)
}

function Resolve-SelectedApps {
    param([string]$BundleName, [string[]]$RequestedApps)
    $selected = New-Object 'System.Collections.Generic.List[string]'
    foreach ($serviceName in @(Get-BundleServiceNames -BundleName $BundleName)) {
        if (-not $selected.Contains($serviceName)) {
            $selected.Add($serviceName)
        }
    }
    if ($null -ne $RequestedApps -and @($RequestedApps).Count -gt 0) {
        $selected.Clear()
        foreach ($app in $RequestedApps) {
            foreach ($part in ([string]$app -split ',')) {
                if ([string]::IsNullOrWhiteSpace($part)) {
                    continue
                }
                $preset = $part.Trim().ToLowerInvariant()
                if ($preset -eq 'none-arr') {
                    continue
                }
                if ($preset -eq 'default-arr') {
                    foreach ($name in $DefaultArrAppNames) {
                        if (-not $selected.Contains($name)) {
                            $selected.Add($name)
                        }
                    }
                    continue
                }
                if ($preset -eq 'all-arr') {
                    foreach ($name in $AllArrAppNames) {
                        if (-not $selected.Contains($name)) {
                            $selected.Add($name)
                        }
                    }
                    continue
                }
                if ($preset -eq 'all' -or $preset -eq 'full') {
                    foreach ($name in $SuiteServiceOrder) {
                        if (-not $selected.Contains($name)) {
                            $selected.Add($name)
                        }
                    }
                    continue
                }
                $normalized = Normalize-AppName -Name $part
                if (-not $selected.Contains($normalized)) {
                    $selected.Add($normalized)
                }
            }
        }
        if (-not $selected.Contains('emulebb')) {
            $selected.Insert(0, 'emulebb')
        }
    }
    if (@($selected | Where-Object { ($_ -ne 'prowlarr') -and ($AllArrAppNames -contains $_) }).Count -gt 0 -and -not $selected.Contains('prowlarr')) {
        $insertAt = [Math]::Min($selected.Count, 2)
        $selected.Insert($insertAt, 'prowlarr')
    }
    $ordered = @()
    foreach ($serviceName in $SuiteServiceOrder) {
        if ($selected.Contains($serviceName)) {
            $ordered += $serviceName
        }
    }
    return $ordered
}

function Normalize-SelectedAppList {
    param([string]$BundleName, [object[]]$Names)
    $supported = @()
    foreach ($name in @($Names)) {
        $normalized = (Resolve-OptionalValue -Value ([string]$name) -Default '').ToLowerInvariant()
        if ($SuiteServiceOrder -contains $normalized) {
            $supported += $normalized
        }
    }
    if (@($supported).Count -eq 0) {
        return @(Resolve-SelectedApps -BundleName $BundleName -RequestedApps @())
    }
    return @(Resolve-SelectedApps -BundleName $BundleName -RequestedApps $supported)
}

function Get-SuiteServiceNames {
    param([hashtable]$Config = $null)
    if ($null -ne $Config -and $null -ne $Config.selectedApps) {
        return @($Config.selectedApps)
    }
    return @($SuiteServiceOrder)
}

function Get-SelectedArrAppNames {
    param([hashtable]$Config)
    return @((Get-SuiteServiceNames -Config $Config) | Where-Object { $AllArrAppNames -contains $_ })
}

function Get-SelectedMediaArrAppNames {
    param([hashtable]$Config)
    return @((Get-SelectedArrAppNames -Config $Config) | Where-Object { $_ -ne 'prowlarr' })
}

function Get-SelectedP2pClientNames {
    param([hashtable]$Config)
    return @((Get-SuiteServiceNames -Config $Config) | Where-Object { $AllP2pClientNames -contains $_ })
}

function Get-ServiceDisplayName {
    param([string]$ServiceName)
    switch ($ServiceName) {
        'emulebb' { return 'eMuleBB' }
        'prowlarr' { return 'Prowlarr' }
        'radarr' { return 'Radarr' }
        'sonarr' { return 'Sonarr' }
        'lidarr' { return 'Lidarr' }
        'whisparr' { return 'Whisparr' }
        default { return $ServiceName }
    }
}

function Get-InstallDescription {
    param([string[]]$ServiceNames)
    return (($ServiceNames | ForEach-Object { Get-ServiceDisplayName -ServiceName $_ }) -join ', ')
}

function Get-BundleInstallDescription {
    param([string]$BundleName)
    return (Get-InstallDescription -ServiceNames @(Get-BundleServiceNames -BundleName $BundleName))
}

function Get-ServicePortDisplayValue {
    param([int]$Port)
    if ($Port -le 0) {
        return "auto (free port from $AutoPortRangeStart-$AutoPortRangeEnd)"
    }
    return [string]$Port
}

function Write-BundlePortPreview {
    param([hashtable]$Config)
    $serviceNames = @(Get-SuiteServiceNames -Config $Config)
    Write-Host ''
    Write-Host ('Selected bundle: {0}' -f $Config.bundle) -ForegroundColor Cyan
    Write-Host ('  Installs: {0}' -f (Get-InstallDescription -ServiceNames $serviceNames))
    Write-Host '  Service ports:'
    foreach ($serviceName in $serviceNames) {
        $service = $Config.services[$serviceName]
        Write-Host ('    {0}: {1}' -f (Get-ServiceDisplayName -ServiceName $serviceName), (Get-ServicePortDisplayValue -Port ([int]$service.port)))
    }
}

function ConvertTo-BindIPAddress {
    param([string]$BindAddress)
    if ([string]::IsNullOrWhiteSpace($BindAddress) -or $BindAddress -eq '0.0.0.0') {
        return [Net.IPAddress]::Any
    }
    if ($BindAddress -eq '::') {
        return [Net.IPAddress]::IPv6Any
    }
    try {
        return [Net.IPAddress]::Parse($BindAddress)
    } catch {
        throw "Bind address must be an IP address for automatic port probing: $BindAddress"
    }
}

function Test-TcpPortAvailable {
    param([string]$BindAddress, [int]$Port)
    if ($Port -lt 1 -or $Port -gt 65535) {
        return $false
    }
    $listener = $null
    try {
        $ipAddress = ConvertTo-BindIPAddress -BindAddress $BindAddress
        $listener = [Net.Sockets.TcpListener]::new($ipAddress, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($null -ne $listener) {
            $listener.Stop()
        }
    }
}

function Resolve-ServicePorts {
    # WHY: IDictionary (not [hashtable]) so the [ordered] config is passed by
    # reference; the top-level $Config.portBlockStart write must reach the caller.
    param([System.Collections.IDictionary]$Config)
    $serviceNames = @(Get-SuiteServiceNames -Config $Config)
    if ($serviceNames.Count -eq 0) {
        throw 'At least one suite service must be selected.'
    }
    $explicit = @()
    foreach ($serviceName in $serviceNames) {
        $port = [int]$Config.services[$serviceName].port
        if ($port -gt 0) {
            $explicit += $port
        }
    }
    if ($explicit.Count -gt 0) {
        if ($explicit.Count -ne $serviceNames.Count) {
            throw 'Explicit service ports must be supplied for every selected service, or leave all selected service ports as 0 for automatic contiguous allocation.'
        }
        $basePort = [int]$Config.services[$serviceNames[0]].port
        if ($basePort -lt $AutoPortRangeStart -or ($basePort + $serviceNames.Count - 1) -gt $AutoPortRangeEnd) {
            throw "Explicit service port block must stay inside Dynamic/Private Ports $AutoPortRangeStart-$AutoPortRangeEnd."
        }
        for ($index = 0; $index -lt $serviceNames.Count; ++$index) {
            $serviceName = $serviceNames[$index]
            $expected = $basePort + $index
            $actual = [int]$Config.services[$serviceName].port
            if ($actual -ne $expected) {
                throw "Explicit service ports must be a contiguous block in selected service order. Expected $serviceName on $expected, got $actual."
            }
            if (-not (Test-TcpPortAvailable -BindAddress ([string]$Config.services[$serviceName].bindAddress) -Port $actual)) {
                throw "Configured $serviceName port $actual is not free on $($Config.services[$serviceName].bindAddress)."
            }
        }
        $Config.portBlockStart = $basePort
        return
    }

    $startPort = [int]$Config.portBlockStart
    if ($startPort -le 0) {
        $startPort = $AutoPortRangeStart
    }
    if ($startPort -lt $AutoPortRangeStart -or ($startPort + $serviceNames.Count - 1) -gt $AutoPortRangeEnd) {
        throw "PortBlockStart must allow a contiguous selected-service block inside Dynamic/Private Ports $AutoPortRangeStart-$AutoPortRangeEnd."
    }
    for ($basePort = $startPort; $basePort -le ($AutoPortRangeEnd - $serviceNames.Count + 1); ++$basePort) {
        $candidatePorts = @()
        for ($offset = 0; $offset -lt $serviceNames.Count; ++$offset) {
            $candidatePorts += ($basePort + $offset)
        }
        $available = $true
        for ($index = 0; $index -lt $serviceNames.Count; ++$index) {
            $serviceName = $serviceNames[$index]
            $port = $candidatePorts[$index]
            if (-not (Test-TcpPortAvailable -BindAddress ([string]$Config.services[$serviceName].bindAddress) -Port $port)) {
                $available = $false
                break
            }
        }
        if ($available) {
            for ($index = 0; $index -lt $serviceNames.Count; ++$index) {
                $Config.services[$serviceNames[$index]].port = $candidatePorts[$index]
            }
            $Config.portBlockStart = $basePort
            Write-Step "Selected contiguous service port block $basePort-$($basePort + $serviceNames.Count - 1) from Dynamic/Private Ports $AutoPortRangeStart-$AutoPortRangeEnd"
            return
        }
    }
    throw "Could not find a free contiguous service port block in $AutoPortRangeStart-$AutoPortRangeEnd for $($serviceNames.Count) service(s)."
}

function Resolve-SuiteConfig {
    $config = New-SuiteConfig
    if (-not [string]::IsNullOrWhiteSpace($ConfigFile)) {
        if (-not (Test-Path -LiteralPath $ConfigFile)) {
            throw "ConfigFile is missing: $ConfigFile"
        }
        $fileConfig = ConvertTo-Hashtable (Read-JsonFile -Path $ConfigFile -Description 'ConfigFile')
        Merge-Hashtable -Target $config -Source $fileConfig
    }

    foreach ($entry in @(
        @('Bundle', { param($c, $v) $c.bundle = $v }),
        @('Apps', { param($c, $v) $c.selectedApps = @(Resolve-SelectedApps -BundleName ([string]$c.bundle) -RequestedApps $v) }),
        @('Language', { param($c, $v) Set-MediaLanguagePreference -Config $c -Value $v; if (-not $script:InstallerBoundParameters.ContainsKey('UiLanguage')) { Set-UiLanguagePreference -Config $c -Value $v } }),
        @('UiLanguage', { param($c, $v) Set-UiLanguagePreference -Config $c -Value $v }),
        @('PortBlockStart', { param($c, $v) $c.portBlockStart = [int]$v }),
        @('InstallRoot', { param($c, $v) $c.installRoot = $v }),
        @('Version', { param($c, $v) $c.version = $v }),
        @('Platform', { param($c, $v) $c.platform = $v }),
        @('InstallKind', { param($c, $v) $c.installKind = $v }),
        @('DependencyChannel', { param($c, $v) $c.dependencyChannel = $v }),
        @('ReleaseBaseUrl', { param($c, $v) $c.releaseBaseUrl = $v }),
        @('EmulebbPackageZip', { param($c, $v) $c.packageSources.emulebb.zip = $v }),
        @('EmulebbPackageManifest', { param($c, $v) $c.packageSources.emulebb.manifest = $v }),
        @('SuiteScriptsZip', { param($c, $v) $c.suiteScripts.zip = $v }),
        @('SuiteScriptsManifest', { param($c, $v) $c.suiteScripts.manifest = $v }),
        @('AutomationExamplesZip', { param($c, $v) $c.automationExamples.zip = $v }),
        @('AutomationExamplesManifest', { param($c, $v) $c.automationExamples.manifest = $v }),
        @('DependencyManifest', { param($c, $v) $c.dependencyManifest = $v }),
        @('ImportProfileDir', { param($c, $v) $c.importProfileDir = $v }),
        @('EmulebbPdbPath', { param($c, $v) $c.symbols.emulebbPdbPath = $v }),
        @('EmulebbPackageFlavor', { param($c, $v) $c.emulebbPackageFlavor = $v }),
        @('ControlBindAddress', {
            param($c, $v)
            foreach ($serviceName in $SuiteServiceOrder) {
                $c.services[$serviceName].bindAddress = $v
            }
        }),
        @('EmulebbBindAddress', { param($c, $v) $c.services.emulebb.bindAddress = $v }),
        @('ProwlarrBindAddress', { param($c, $v) $c.services.prowlarr.bindAddress = $v }),
        @('RadarrBindAddress', { param($c, $v) $c.services.radarr.bindAddress = $v }),
        @('SonarrBindAddress', { param($c, $v) $c.services.sonarr.bindAddress = $v }),
        @('LidarrBindAddress', { param($c, $v) $c.services.lidarr.bindAddress = $v }),
        @('WhisparrBindAddress', { param($c, $v) $c.services.whisparr.bindAddress = $v }),
        @('EmulebbPort', { param($c, $v) $c.services.emulebb.port = $v }),
        @('ProwlarrPort', { param($c, $v) $c.services.prowlarr.port = $v }),
        @('RadarrPort', { param($c, $v) $c.services.radarr.port = $v }),
        @('SonarrPort', { param($c, $v) $c.services.sonarr.port = $v }),
        @('LidarrPort', { param($c, $v) $c.services.lidarr.port = $v }),
        @('WhisparrPort', { param($c, $v) $c.services.whisparr.port = $v }),
        @('P2PBindInterface', { param($c, $v) $c.p2p.bindInterface = $v })
    )) {
        $name = [string]$entry[0]
        if ($script:InstallerBoundParameters.ContainsKey($name)) {
            & $entry[1] $config $script:InstallerBoundParameters[$name]
        }
    }
    if ($script:InstallerBoundParameters.ContainsKey('AllowRemoteServiceBind')) {
        $config.allowRemoteServiceBind = [bool]$AllowRemoteServiceBind
    }
    if ($null -eq $config.optionalTools) {
        $config.optionalTools = [ordered]@{
            install = $false
            mpcHc = [ordered]@{ installed = $false; path = '' }
            ffmpeg = [ordered]@{ installed = $false; path = '' }
            mediainfo = [ordered]@{ installed = $false; path = '' }
        }
    }
    if (-not $config.optionalTools.Contains('install')) {
        $config.optionalTools.install = $false
    }
    if ($script:InstallerBoundParameters.ContainsKey('InstallMediaTools')) {
        $config.optionalTools.install = $true
    }
    if ($script:InstallerBoundParameters.ContainsKey('NoMediaTools')) {
        $config.optionalTools.install = $false
    }
    if ($null -eq $config.selectedApps -or @($config.selectedApps).Count -eq 0) {
        $config.selectedApps = @(Resolve-SelectedApps -BundleName ([string]$config.bundle) -RequestedApps @())
    } else {
        $config.selectedApps = @(Normalize-SelectedAppList -BundleName ([string]$config.bundle) -Names @($config.selectedApps))
    }
    foreach ($serviceName in $SuiteServiceOrder) {
        if ($null -eq $config.services[$serviceName]) {
            $config.services[$serviceName] = [ordered]@{ bindAddress = (Resolve-OptionalValue -Value $ControlBindAddress -Default (Get-DefaultControlBindAddress)); clientHost = ''; port = 0 }
            if ($serviceName -eq 'emulebb' -or $AllArrAppNames -contains $serviceName) {
                $config.services[$serviceName].apiKey = ''
            }
        }
        if (-not $config.services[$serviceName].Contains('clientHost')) {
            $config.services[$serviceName].clientHost = ''
        }
        if (($serviceName -eq 'emulebb' -or $AllArrAppNames -contains $serviceName) -and -not $config.services[$serviceName].Contains('apiKey')) {
            $config.services[$serviceName].apiKey = ''
        }
    }
    if (@($config.selectedApps | Where-Object { ($_ -ne 'prowlarr') -and ($AllArrAppNames -contains $_) }).Count -gt 0 -and @($config.selectedApps) -notcontains 'prowlarr') {
        $config.selectedApps = @(Resolve-SelectedApps -BundleName ([string]$config.bundle) -RequestedApps @($config.selectedApps))
    }
    if ($null -eq $config.language) {
        $pref = Resolve-LanguagePreference -Value 'English'
        $config.language = [ordered]@{
            name = [string]$pref.DisplayName
            mediaLanguageName = [string]$pref.DisplayName
            uiLanguageName = [string]$pref.DisplayName
            emuleLanguageId = [int]$pref.EmuleLanguageId
            emuleLocale = [string]$pref.EmuleLocale
            arrUiLanguage = [string]$pref.ArrUiLanguage
            arrContentLanguage = [string]$pref.ArrContentLanguage
            arrContentLanguageMode = 'prefer'
        }
    } else {
        if (-not $config.language.Contains('mediaLanguageName')) {
            $config.language.mediaLanguageName = Resolve-OptionalValue -Value ([string]$config.language.name) -Default ([string]$config.language.arrContentLanguage)
        }
        if (-not $config.language.Contains('uiLanguageName')) {
            $config.language.uiLanguageName = Resolve-OptionalValue -Value ([string]$config.language.name) -Default ([string]$config.language.arrUiLanguage)
        }
    }
    if ($null -eq $config.portBlockStart) {
        $config.portBlockStart = 0
    }
    if ($null -eq $config.suiteScripts) {
        $config.suiteScripts = [ordered]@{ zip = ''; manifest = '' }
    }
    if (-not $config.suiteScripts.Contains('zip')) {
        $config.suiteScripts.zip = ''
    }
    if (-not $config.suiteScripts.Contains('manifest')) {
        $config.suiteScripts.manifest = ''
    }
    if ($null -eq $config.automationExamples) {
        $config.automationExamples = [ordered]@{ zip = ''; manifest = '' }
    }
    if (-not $config.automationExamples.Contains('zip')) {
        $config.automationExamples.zip = ''
    }
    if (-not $config.automationExamples.Contains('manifest')) {
        $config.automationExamples.manifest = ''
    }
    $config.emulebbExecutableName = Get-EmulebbExecutableNameForFlavor -PackageFlavor ([string]$config.emulebbPackageFlavor)
    Set-SuiteClientHosts -Config $config
    return $config
}

function Test-InteractiveConsole {
    try {
        return -not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected
    } catch {
        return $false
    }
}

function Read-WizardValue {
    param([string]$Prompt, [string]$Default)
    $suffix = if ([string]::IsNullOrWhiteSpace($Default)) { '' } else { " [$Default]" }
    $value = Read-Host "$Prompt$suffix"
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $Default
    }
    return $value.Trim()
}

function Read-WizardPortValue {
    param([string]$Prompt, [int]$Default)
    while ($true) {
        $raw = Read-WizardValue -Prompt "$Prompt (0=auto, 1-65535=explicit)" -Default ([string]$Default)
        $port = 0
        if ([int]::TryParse($raw, [ref]$port) -and $port -ge 0 -and $port -le 65535) {
            return $port
        }
        Write-Host 'Enter a number from 0 to 65535. Use 0 to auto-select a free suite port.' -ForegroundColor Yellow
    }
}

function Read-WizardChoice {
    param([string]$Prompt, [string[]]$Choices, [int]$DefaultIndex = 0)
    while ($true) {
        Write-Host ''
        Write-Host $Prompt
        for ($i = 0; $i -lt $Choices.Count; $i++) {
            $marker = if ($i -eq $DefaultIndex) { '*' } else { ' ' }
            Write-Host ("  {0}{1}. {2}" -f $marker, ($i + 1), $Choices[$i])
        }
        $raw = Read-Host "Select 1-$($Choices.Count), B=back, Q=quit"
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return $DefaultIndex
        }
        if ($raw -match '^[Qq]$') {
            throw 'Installer cancelled.'
        }
        if ($raw -match '^[Bb]$') {
            return -1
        }
        $selected = 0
        if ([int]::TryParse($raw, [ref]$selected) -and $selected -ge 1 -and $selected -le $Choices.Count) {
            return ($selected - 1)
        }
        Write-Host 'Invalid selection.'
    }
}

function Read-WizardChecklist {
    param([string]$Prompt, [string[]]$Items, [string[]]$Selected)
    $selectedSet = @{}
    foreach ($item in @($Selected)) {
        $selectedSet[$item] = $true
    }
    while ($true) {
        Write-Host ''
        Write-Host $Prompt
        for ($i = 0; $i -lt $Items.Count; $i++) {
            $item = $Items[$i]
            $mark = if ($selectedSet.ContainsKey($item) -and $selectedSet[$item]) { 'x' } else { ' ' }
            Write-Host ("  {0}. [{1}] {2}" -f ($i + 1), $mark, (Get-ServiceDisplayName -ServiceName $item))
        }
        $raw = Read-Host 'Toggle numbers separated by commas, Enter=continue, B=back, Q=quit'
        if ([string]::IsNullOrWhiteSpace($raw)) {
            $result = @()
            foreach ($item in $Items) {
                if ($selectedSet.ContainsKey($item) -and $selectedSet[$item]) {
                    $result += $item
                }
            }
            return $result
        }
        if ($raw -match '^[Qq]$') {
            throw 'Installer cancelled.'
        }
        if ($raw -match '^[Bb]$') {
            return $null
        }
        foreach ($part in ($raw -split ',')) {
            $index = 0
            if (-not [int]::TryParse($part.Trim(), [ref]$index) -or $index -lt 1 -or $index -gt $Items.Count) {
                Write-Host "Invalid checklist item: $part" -ForegroundColor Yellow
                continue
            }
            $item = $Items[$index - 1]
            if ($item -eq 'prowlarr' -and ($selectedSet.ContainsKey('radarr') -or $selectedSet.ContainsKey('sonarr') -or $selectedSet.ContainsKey('lidarr') -or $selectedSet.ContainsKey('whisparr'))) {
                Write-Host 'Prowlarr stays selected while any media Arr app is selected.' -ForegroundColor Yellow
                continue
            }
            if ($selectedSet.ContainsKey($item) -and $selectedSet[$item]) {
                [void]$selectedSet.Remove($item)
            } else {
                $selectedSet[$item] = $true
            }
        }
        $hasMediaArr = $false
        foreach ($item in @('radarr', 'sonarr', 'lidarr', 'whisparr')) {
            if ($selectedSet.ContainsKey($item) -and $selectedSet[$item]) {
                $hasMediaArr = $true
            }
        }
        if ($hasMediaArr) {
            $selectedSet['prowlarr'] = $true
        }
    }
}

function Get-BindableInterfaceOptions {
    $options = @()
    foreach ($info in @(Get-LocalIPv4InterfaceInfos | Sort-Object InterfaceAlias, IPAddress)) {
        if ([string]::IsNullOrWhiteSpace($info.InterfaceAlias)) {
            continue
        }
        $prefix = if ($null -ne $info.PrefixLength) { "/$($info.PrefixLength)" } else { '' }
        $mask = ConvertTo-IPv4SubnetMask -PrefixLength $info.PrefixLength
        $maskText = if ([string]::IsNullOrWhiteSpace($mask)) { '' } else { " ($mask)" }
        $vpnText = if ($info.IsVpnLike) { ' [VPN-like]' } else { '' }
        $options += [pscustomobject]@{
            InterfaceAlias = [string]$info.InterfaceAlias
            Label = ('{0} - {1}{2}{3}{4}' -f $info.InterfaceAlias, $info.IPAddress, $prefix, $maskText, $vpnText)
        }
    }
    return @($options)
}

function Get-BindableInterfaceNames {
    return @(Get-BindableInterfaceOptions | ForEach-Object { $_.InterfaceAlias } | Sort-Object -Unique)
}

function Invoke-InstallWizard {
    # WHY: $Config is the script's [ordered] suite config. A [hashtable] parameter
    # type coerces an OrderedDictionary into a NEW shallow-copied hashtable, so the
    # wizard's top-level writes ($Config.bundle, $Config.selectedApps,
    # allowRemoteServiceBind, portBlockStart, dependencyChannel) would be lost while
    # nested edits (services/p2p/language) silently persisted via shared references.
    # Accept any IDictionary so the wizard mutates the caller's config in place.
    param([System.Collections.IDictionary]$Config)
    $step = 0
    while ($step -lt 7) {
        switch ($step) {
            0 {
                Write-Host ''
                Write-Host 'All selected components are installed in portable mode under the suite install root. Existing Arr apps, media tools, PATH entries, and system software are not modified.'
                $bundleChoices = @(
                    ('Full suite - installs {0}' -f (Get-BundleInstallDescription -BundleName 'Full')),
                    ('Core app only - installs {0}' -f (Get-BundleInstallDescription -BundleName 'Core'))
                )
                $choice = Read-WizardChoice -Prompt 'Bundle' -Choices $bundleChoices -DefaultIndex (@('Full', 'Core').IndexOf([string]$Config.bundle))
                if ($choice -lt 0) { $step = [Math]::Max(0, $step - 1); continue }
                $Config.bundle = @('Full', 'Core')[$choice]
                $Config.selectedApps = @(Resolve-SelectedApps -BundleName ([string]$Config.bundle) -RequestedApps @())
                if ($Config.bundle -eq 'Full') {
                    $selectedArr = Read-WizardChecklist -Prompt 'Arr apps' -Items $AllArrAppNames -Selected @(Get-SelectedArrAppNames -Config $Config)
                    if ($null -eq $selectedArr) { $step = [Math]::Max(0, $step - 1); continue }
                    $Config.selectedApps = @(Resolve-SelectedApps -BundleName ([string]$Config.bundle) -RequestedApps @($CoreServiceNames + $selectedArr))
                }
                $mediaToolsChoice = Read-WizardChoice -Prompt 'Optional media tools' -Choices @('Skip media tools', 'Download and install portable MPC-HC, ffmpeg, and MediaInfo under the suite path') -DefaultIndex $(if ([bool]$Config.optionalTools.install) { 1 } else { 0 })
                if ($mediaToolsChoice -lt 0) { continue }
                $Config.optionalTools.install = ($mediaToolsChoice -eq 1)
                Write-BundlePortPreview -Config $Config
                $Config.installRoot = Read-WizardValue -Prompt 'Install root' -Default $Config.installRoot
                $step++
            }
            1 {
                $defaultBind = Get-DefaultControlBindAddress
                if ([string]::IsNullOrWhiteSpace($defaultBind)) {
                    $choices = @('One custom bind address for eMuleBB REST and selected Arr services', 'Per-service bind addresses')
                    $choice = Read-WizardChoice -Prompt 'Control services bind policy (eMuleBB REST, selected Arr services)' -Choices $choices -DefaultIndex 0
                    if ($choice -ge 0) { $choice++ }
                } else {
                    $choice = Read-WizardChoice -Prompt 'Control services bind policy (eMuleBB REST and selected Arr services)' -Choices @("Detected LAN/VPN bind for eMuleBB REST and selected Arr services ($defaultBind)", 'One custom bind address for eMuleBB REST and selected Arr services', 'Per-service bind addresses') -DefaultIndex 0
                }
                if ($choice -lt 0) { $step--; continue }
                if ($choice -eq 0) {
                    foreach ($serviceName in @(Get-SuiteServiceNames -Config $Config)) {
                        $Config.services[$serviceName].bindAddress = $defaultBind
                    }
                } elseif ($choice -eq 1) {
                    $bind = Read-WizardValue -Prompt 'Bind address for eMuleBB REST and selected Arr services (localhost, LAN/VPN IP, or other network address)' -Default $defaultBind
                    foreach ($serviceName in @(Get-SuiteServiceNames -Config $Config)) {
                        $Config.services[$serviceName].bindAddress = $bind
                    }
                } else {
                    foreach ($serviceName in @(Get-SuiteServiceNames -Config $Config)) {
                        $Config.services[$serviceName].bindAddress = Read-WizardValue -Prompt "$serviceName bind address" -Default $Config.services[$serviceName].bindAddress
                    }
                }
                $Config.allowRemoteServiceBind = Test-HasRemoteServiceBind -Config $Config
                Set-SuiteClientHosts -Config $Config
                $step++
            }
            2 {
                $interfaceOptions = @(Get-BindableInterfaceOptions)
                $choices = @('Any interface (no P2P bind)')
                $choices += @($interfaceOptions | ForEach-Object { $_.Label })
                $choices += 'Custom interface name'
                $choice = Read-WizardChoice -Prompt 'eMuleBB P2P bind interface' -Choices $choices -DefaultIndex 0
                if ($choice -lt 0) { $step--; continue }
                if ($choice -eq 0) {
                    $Config.p2p.bindInterface = ''
                } elseif ($choice -le $interfaceOptions.Count) {
                    $Config.p2p.bindInterface = $interfaceOptions[$choice - 1].InterfaceAlias
                } else {
                    $Config.p2p.bindInterface = Read-WizardValue -Prompt 'P2P bind interface name' -Default $Config.p2p.bindInterface
                }
                $Config.p2p.blockNetworkWhenBindUnavailableAtStartup = $false
                $Config.p2p.networkGuardMode = 'Off'
                $Config.p2p.networkGuardAllowedCIDRs = ''
                $step++
            }
            3 {
                Write-BundlePortPreview -Config $Config
                $choice = Read-WizardChoice -Prompt 'Ports' -Choices @('Auto-select contiguous Dynamic/Private port block', 'Choose block start') -DefaultIndex 0
                if ($choice -lt 0) { $step--; continue }
                if ($choice -eq 1) {
                    $Config.portBlockStart = Read-WizardPortValue -Prompt "Port block start ($AutoPortRangeStart-$AutoPortRangeEnd)" -Default ([int]$Config.portBlockStart)
                } else {
                    $Config.portBlockStart = 0
                }
                foreach ($serviceName in @(Get-SuiteServiceNames -Config $Config)) {
                    $Config.services[$serviceName].port = 0
                }
                $step++
            }
            4 {
                $languageNames = @('English', 'Spanish', 'Italian', 'Portuguese')
                $defaultMediaLanguageIndex = [Math]::Max(0, [Array]::IndexOf($languageNames, [string]$Config.language.mediaLanguageName))
                $mediaChoice = Read-WizardChoice -Prompt 'Media language preference' -Choices $languageNames -DefaultIndex $defaultMediaLanguageIndex
                if ($mediaChoice -lt 0) { $step--; continue }
                Set-MediaLanguagePreference -Config $Config -Value $languageNames[$mediaChoice]
                $defaultUiLanguageIndex = [Math]::Max(0, [Array]::IndexOf($languageNames, [string]$Config.language.uiLanguageName))
                if ([string]::IsNullOrWhiteSpace([string]$Config.language.uiLanguageName)) {
                    $defaultUiLanguageIndex = $mediaChoice
                }
                $uiChoice = Read-WizardChoice -Prompt 'UI language preference' -Choices $languageNames -DefaultIndex $defaultUiLanguageIndex
                if ($uiChoice -lt 0) { continue }
                Set-UiLanguagePreference -Config $Config -Value $languageNames[$uiChoice]
                $step++
            }
            5 {
                if ([string]::IsNullOrWhiteSpace([string]$Config.dependencyManifest)) {
                    if ($Config.dependencyChannel -eq 'Latest') {
                        Write-Host 'Latest dependency releases require -DependencyManifest with exact URLs and SHA256 hashes. Using pinned dependency versions.' -ForegroundColor Yellow
                        $Config.dependencyChannel = 'Pinned'
                    }
                    Write-Host 'Latest dependency releases are unavailable unless you pass -DependencyManifest. Using pinned dependency versions.' -ForegroundColor Yellow
                    $Config.dependencyChannel = 'Pinned'
                    $step++
                    continue
                }
                $choice = Read-WizardChoice -Prompt 'Dependency resolution' -Choices @('Pinned dependency versions', 'Latest dependency releases') -DefaultIndex $(if ($Config.dependencyChannel -eq 'Latest') { 1 } else { 0 })
                if ($choice -lt 0) { $step--; continue }
                $Config.dependencyChannel = @('Pinned', 'Latest')[$choice]
                $step++
            }
            6 {
                Write-ConfigSummary -Config $Config
                $choice = Read-WizardChoice -Prompt 'Install plan' -Choices @('Install now', 'Back to edit', 'Quit') -DefaultIndex 0
                if ($choice -eq 0) { return }
                if ($choice -eq 1 -or $choice -lt 0) { $step--; continue }
                throw 'Installer cancelled.'
            }
        }
    }
}

function Test-HasRemoteServiceBind {
    param([hashtable]$Config)
    foreach ($serviceName in @(Get-SuiteServiceNames -Config $Config)) {
        $address = [string]$Config.services[$serviceName].bindAddress
        if (-not (Test-LoopbackAddress -Address $address)) {
            return $true
        }
    }
    return $false
}

function Write-ConfigSummary {
    param([hashtable]$Config)
    Write-Host ''
    Write-Host 'Install summary'
    Write-Host "  Bundle: $($Config.bundle)"
    Write-Host ('  Installs: {0}' -f (Get-InstallDescription -ServiceNames @(Get-SuiteServiceNames -Config $Config)))
    Write-Host '  Isolation: portable under the suite root; existing Arr apps, media tools, PATH entries, and system software are not modified'
    Write-Host "  Media language: $($Config.language.mediaLanguageName) (content language preference: prefer)"
    Write-Host "  UI language: $($Config.language.uiLanguageName) (applies to eMuleBB and Arr apps)"
    Write-Host "  Root: $($Config.installRoot)"
    Write-Host "  Version/platform: $($Config.version) / $($Config.platform)"
    foreach ($serviceName in @(Get-SuiteServiceNames -Config $Config)) {
        $service = $Config.services[$serviceName]
        $clientHostText = if ([string]::Equals([string]$service.clientHost, [string]$service.bindAddress, [StringComparison]::OrdinalIgnoreCase)) { '' } else { " (client URL host: $($service.clientHost))" }
        Write-Host ("  {0}: {1}:{2}{3}" -f $serviceName, $service.bindAddress, $service.port, $clientHostText)
    }
    if ([string]::IsNullOrWhiteSpace($Config.p2p.bindInterface)) {
        Write-Host '  P2P bind interface: Any interface (no bind; OS-selected route)'
    } else {
        Write-Host "  P2P bind interface: $($Config.p2p.bindInterface) (warn-only policy)"
    }
    if (-not [string]::IsNullOrWhiteSpace($Config.importProfileDir)) {
        Write-Host "  Import profile: $($Config.importProfileDir)"
    }
    $mediaToolsText = if ([bool]$Config.optionalTools.install) { 'Install MPC-HC, ffmpeg, and MediaInfo' } else { 'Skip' }
    Write-Host "  Optional media tools: $mediaToolsText"
}

function Get-LocalIPv4Addresses {
    $addresses = @('127.0.0.1')
    try {
        $addresses += @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            ForEach-Object { $_.IPAddress } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    } catch {
        try {
            $addresses += @([System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()) |
                Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork } |
                ForEach-Object { $_.IPAddressToString })
        } catch {
        }
    }
    return @($addresses | Sort-Object -Unique)
}

function Test-LoopbackAddress {
    param([string]$Address)
    return Test-LoopbackAddressText -Address $Address
}

function Assert-ServiceBindAddress {
    param([string]$ServiceName, [string]$Address, [bool]$AllowRemote)
    if ([string]::IsNullOrWhiteSpace($Address)) {
        throw "$ServiceName bind address must not be empty. No default LAN/VPN IPv4 address was detected. Pass -ControlBindAddress with a LAN/VPN IP, pass -ControlBindAddress 0.0.0.0 to bind all interfaces, or pass explicit -ControlBindAddress 127.0.0.1 for local-only use."
    }
    if (Test-LoopbackAddress -Address $Address) {
        return
    }
    if ($Address -eq '0.0.0.0') {
        return
    }
    $localAddresses = @(Get-LocalIPv4Addresses)
    if ($localAddresses -notcontains $Address) {
        throw "$ServiceName bind address $Address is not a local IPv4 address. Local addresses: $($localAddresses -join ', ')"
    }
    return
}

function Assert-Port {
    param([string]$ServiceName, [int]$Port)
    if ($Port -lt 1 -or $Port -gt 65535) {
        throw "$ServiceName port must be between 1 and 65535: $Port"
    }
}

function Test-PortAvailable {
    param([string]$Address, [int]$Port)
    $listener = $null
    try {
        $ip = if ($Address -eq 'localhost') { [Net.IPAddress]::Parse('127.0.0.1') } elseif ($Address -eq '0.0.0.0') { [Net.IPAddress]::Any } else { [Net.IPAddress]::Parse($Address) }
        $listener = New-Object Net.Sockets.TcpListener($ip, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($null -ne $listener) {
            $listener.Stop()
        }
    }
}

function Assert-SuiteConfig {
    # WHY: IDictionary (not [hashtable]) so the [ordered] config is passed by
    # reference; the top-level credentials/packageSources/optionalTools defaulting
    # writes must reach the caller instead of a shallow copy.
    param([System.Collections.IDictionary]$Config)
    if (@('Core', 'Full') -notcontains $Config.bundle) {
        throw "Bundle must be Core or Full: $($Config.bundle)"
    }
    if (@('x64', 'ARM64') -notcontains $Config.platform) {
        throw "Platform must be x64 or ARM64: $($Config.platform)"
    }
    if (@('standard', 'diagnostics') -notcontains $Config.emulebbPackageFlavor) {
        throw "EmulebbPackageFlavor must be standard or diagnostics: $($Config.emulebbPackageFlavor)"
    }
    if ($Config.dependencyChannel -eq 'Latest' -and [string]::IsNullOrWhiteSpace([string]$Config.dependencyManifest)) {
        throw 'DependencyChannel Latest requires -DependencyManifest with exact URLs and SHA256 hashes. Use the default pinned dependencies, or pass -DependencyManifest C:\Path\dependencies.json.'
    }
    Assert-EmulebbExecutableName -PackageFlavor ([string]$Config.emulebbPackageFlavor) -ExecutableName ([string]$Config.emulebbExecutableName)
    if ($null -eq $Config.credentials) {
        $Config.credentials = [ordered]@{ username = 'admin'; password = '' }
    }
    if ($null -eq $Config.packageSources) {
        $Config.packageSources = [ordered]@{
            emulebb = [ordered]@{ zip = ''; manifest = '' }
        }
    }
    if ($null -eq $Config.optionalTools) {
        $Config.optionalTools = [ordered]@{
            install = $false
            mpcHc = [ordered]@{ installed = $false; path = '' }
            ffmpeg = [ordered]@{ installed = $false; path = '' }
            mediainfo = [ordered]@{ installed = $false; path = '' }
        }
    }
    if (-not $Config.optionalTools.Contains('install')) {
        $Config.optionalTools.install = $false
    }
    if ([string]::IsNullOrWhiteSpace($Config.credentials.username) -or $Config.credentials.username -notmatch '^[a-zA-Z0-9_]{3,32}$') {
        throw "SuiteUsername must be 3-32 characters and contain only letters, numbers, or underscores: $($Config.credentials.username)"
    }
    foreach ($serviceName in @(Get-SuiteServiceNames -Config $Config)) {
        $service = $Config.services[$serviceName]
        Assert-ServiceBindAddress -ServiceName $serviceName -Address $service.bindAddress -AllowRemote ([bool]$Config.allowRemoteServiceBind)
        Assert-Port -ServiceName $serviceName -Port ([int]$service.port)
    }
    if (-not [string]::IsNullOrWhiteSpace($Config.p2p.bindInterface)) {
        $interfaces = @(Get-BindableInterfaceNames)
        if ($interfaces.Count -gt 0 -and $interfaces -notcontains $Config.p2p.bindInterface) {
            Write-Warning "P2P bind interface '$($Config.p2p.bindInterface)' was not found now. eMuleBB will warn/fallback according to its runtime policy."
        }
        $Config.p2p.blockNetworkWhenBindUnavailableAtStartup = $false
        $Config.p2p.networkGuardMode = 'Off'
        $Config.p2p.networkGuardAllowedCIDRs = ''
    }
    if (-not $DryRun -and -not $Force) {
        foreach ($serviceName in @(Get-SuiteServiceNames -Config $Config)) {
            $service = $Config.services[$serviceName]
            if (-not (Test-PortAvailable -Address $service.bindAddress -Port ([int]$service.port))) {
                throw "$serviceName port is not available on $($service.bindAddress):$($service.port)"
            }
        }
    }
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
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $TempPath -ErrorAction Stop
        if (Test-Path -LiteralPath $TempPath) {
            Write-Host ('  Downloaded {0}' -f (Format-DownloadSize -Bytes ([IO.FileInfo]$TempPath).Length))
        }
        return
    }
    $request = [System.Net.HttpWebRequest][System.Net.WebRequest]::Create($Url)
    $request.UserAgent = 'eMuleBB-Suite-Installer'
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

function Get-DependencyDownloadRecoveryMessage {
    return 'Check your network or proxy and rerun the installer. If GitHub downloads are blocked, pass -DependencyManifest with reachable file paths or URLs and SHA256 hashes for the Arr and Node dependency ZIPs.'
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
    $uri = $null
    $maxAttempts = 4
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        Remove-Item -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
        try {
            if ([Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$uri) -and $uri.IsFile) {
                Copy-Item -Force -LiteralPath $uri.LocalPath -Destination $tmp
                Move-Item -Force -LiteralPath $tmp -Destination $Destination
                return
            }
            if (Test-Path -LiteralPath $Url) {
                Copy-Item -Force -LiteralPath $Url -Destination $tmp
                Move-Item -Force -LiteralPath $tmp -Destination $Destination
                return
            }
            Invoke-HttpDownload -Url $Url -TempPath $tmp -Destination $Destination
            Move-Item -Force -LiteralPath $tmp -Destination $Destination
            return
        } catch {
            $message = $_.Exception.Message
            if ($attempt -ge $maxAttempts) {
                throw "Failed to download $Url -> $Destination after $maxAttempts attempts. $message $(Get-DependencyDownloadRecoveryMessage)"
            }
            $delaySeconds = [Math]::Min(30, 5 * $attempt)
            Write-Warning "Download attempt $attempt of $maxAttempts failed for $Url. $message Retrying in $delaySeconds seconds..."
            Start-Sleep -Seconds $delaySeconds
        } finally {
            Remove-Item -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
        }
    }
}

function Assert-FileHash {
    param([string]$Path, [string]$ExpectedSha256)
    if ([string]::IsNullOrWhiteSpace($ExpectedSha256) -or $DryRun) {
        return
    }
    $actual = Get-Sha256 -Path $Path
    if ($actual -ne $ExpectedSha256.ToLowerInvariant()) {
        throw "SHA256 mismatch for $Path. Expected $ExpectedSha256, got $actual."
    }
}

function Get-DependencyCacheRoot {
    param([string]$Kind)
    return (Join-Path (Join-Path ([IO.Path]::GetTempPath()) 'emulebb-suite-cache') $Kind)
}

function Get-DependencyCachePath {
    param([string]$Kind, [string]$AssetName, [string]$ExpectedSha256)
    return (Join-Path (Get-DependencyCacheRoot -Kind $Kind) ("$($ExpectedSha256.ToLowerInvariant())-$AssetName"))
}

function Restore-DependencyCache {
    param([string]$Kind, [string]$Name, [string]$AssetName, [string]$ExpectedSha256, [string]$Destination)
    if ([string]::IsNullOrWhiteSpace($ExpectedSha256) -or $DryRun) {
        return $false
    }
    $cachePath = Get-DependencyCachePath -Kind $Kind -AssetName $AssetName -ExpectedSha256 $ExpectedSha256
    if (-not (Test-Path -LiteralPath $cachePath)) {
        return $false
    }
    try {
        Assert-FileHash -Path $cachePath -ExpectedSha256 $ExpectedSha256
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
        Copy-Item -Force -LiteralPath $cachePath -Destination $Destination
        Write-Step "Using cached $Name $AssetName"
        return $true
    } catch {
        Write-Warning "Ignoring stale cached $Name $cachePath. $($_.Exception.Message)"
        Remove-Item -Force -LiteralPath $cachePath -ErrorAction SilentlyContinue
        return $false
    }
}

function Save-DependencyCache {
    param([string]$Kind, [string]$Name, [string]$AssetName, [string]$ExpectedSha256, [string]$Source)
    if ([string]::IsNullOrWhiteSpace($ExpectedSha256) -or $DryRun) {
        return
    }
    $cachePath = Get-DependencyCachePath -Kind $Kind -AssetName $AssetName -ExpectedSha256 $ExpectedSha256
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $cachePath) | Out-Null
    Copy-Item -Force -LiteralPath $Source -Destination $cachePath
    Write-Step "Cached $Name $AssetName"
}

function Assert-RequiredSha256 {
    param([string]$Value, [string]$Description)
    if ($Value -notmatch '^[0-9a-fA-F]{64}$') {
        throw "$Description must include a SHA256 hash."
    }
}

function Get-Sha256 {
    param([string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $hash = $sha.ComputeHash($stream)
            return ([BitConverter]::ToString($hash)).Replace('-', '').ToLowerInvariant()
        } finally {
            $sha.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

function Expand-ZipSafe {
    param([string]$Archive, [string]$Destination)
    if ($DryRun) {
        Write-Step "Would extract $Archive -> $Destination"
        return
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $destinationRoot = [IO.Path]::GetFullPath($Destination)
    if (-not $destinationRoot.EndsWith([IO.Path]::DirectorySeparatorChar)) {
        $destinationRoot += [IO.Path]::DirectorySeparatorChar
    }
    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -Recurse -Force -LiteralPath $Destination
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            $target = [IO.Path]::GetFullPath((Join-Path $Destination $entry.FullName))
            if (-not $target.StartsWith($destinationRoot, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Unsafe ZIP member escapes extraction root: $($entry.FullName)"
            }
        }
    } finally {
        $zip.Dispose()
    }
    [IO.Compression.ZipFile]::ExtractToDirectory($Archive, $Destination)
}

function Get-GithubReleaseAsset {
    param([string]$Repo, [string]$Tag, [string]$Pattern)
    $endpoint = if ($Tag -eq 'latest') { 'latest' } else { "tags/$Tag" }
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/$endpoint" -Headers @{ 'User-Agent' = 'emulebb-suite-installer' }
    foreach ($asset in @($release.assets)) {
        if ($asset.name -match $Pattern) {
            return $asset
        }
    }
    throw "No asset in $Repo $Tag matched $Pattern."
}

function Load-DependencyManifestPayload {
    param([string]$ManifestPath)
    if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
        return $null
    }
    if (-not (Test-Path -LiteralPath $ManifestPath)) {
        throw "DependencyManifest is missing: $ManifestPath"
    }
    return Read-JsonFile -Path $ManifestPath -Description 'DependencyManifest'
}

function Load-DependencyManifest {
    param([object]$Payload, [string]$Channel, [string[]]$Names)
    if ($null -eq $Payload) {
        if ($Channel -eq 'Latest') {
            throw 'Latest dependency resolution requires -DependencyManifest entries with exact URLs and SHA256 hashes.'
        }
        return $PinnedDependencies
    }
    $result = @{}
    foreach ($name in $Names) {
        $item = $Payload.$name
        if ($null -eq $item) {
            throw "Dependency manifest is missing '$name'."
        }
        $result[$name] = @{
            Repo = [string]$item.repo
            Tag = [string]$item.tag
            Pattern = [string]$item.assetPattern
            Exe = [string]$item.exeName
            Sha256 = [string]$item.sha256
            Url = [string]$item.url
        }
    }
    return $result
}

function Get-OptionalObjectProperty {
    param([object]$Item, [string]$Name)
    if ($Item -is [System.Collections.IDictionary]) {
        foreach ($key in $Item.Keys) {
            if ([string]::Equals([string]$key, $Name, [StringComparison]::OrdinalIgnoreCase)) {
                return [string]$Item[$key]
            }
        }
        return ''
    }
    if ($null -eq $Item -or $null -eq $Item.PSObject.Properties[$Name]) {
        return ''
    }
    return [string]$Item.PSObject.Properties[$Name].Value
}

function ConvertTo-DependencySpec {
    param([object]$Item, [string]$DefaultExe)
    if ($null -eq $Item) {
        return $null
    }
    $url = Get-OptionalObjectProperty -Item $Item -Name 'url'
    $fileName = Get-OptionalObjectProperty -Item $Item -Name 'fileName'
    if ([string]::IsNullOrWhiteSpace($fileName) -and -not [string]::IsNullOrWhiteSpace($url)) {
        $fileName = [IO.Path]::GetFileName($url)
    }
    return @{
        Repo = Get-OptionalObjectProperty -Item $Item -Name 'repo'
        Tag = Get-OptionalObjectProperty -Item $Item -Name 'tag'
        Pattern = Get-OptionalObjectProperty -Item $Item -Name 'assetPattern'
        Exe = (Resolve-OptionalValue -Value (Get-OptionalObjectProperty -Item $Item -Name 'exeName') -Default (Resolve-OptionalValue -Value (Get-OptionalObjectProperty -Item $Item -Name 'exe') -Default $DefaultExe))
        FileName = $fileName
        Sha256 = Get-OptionalObjectProperty -Item $Item -Name 'sha256'
        Url = $url
    }
}

function Get-ManifestMediaToolItem {
    param([object]$Payload, [string]$Name)
    if ($null -eq $Payload) {
        return $null
    }
    if ($null -ne $Payload.PSObject.Properties['mediaTools'] -and $null -ne $Payload.mediaTools) {
        $mediaTools = $Payload.mediaTools
        if ($null -ne $mediaTools.PSObject.Properties[$Name]) {
            return $mediaTools.$Name
        }
    }
    if ($null -ne $Payload.PSObject.Properties[$Name]) {
        return $Payload.$Name
    }
    return $null
}

function Load-MediaToolDependencies {
    param([object]$Payload)
    $result = @{}
    foreach ($name in @('mpc-hc', 'ffmpeg', 'mediainfo')) {
        $defaultTool = $PinnedMediaTools[$name]
        $tool = @{
            DisplayName = [string]$defaultTool.DisplayName
            Destination = [string]$defaultTool.Destination
            Executable = [string]$defaultTool.Executable
            Dependency = ConvertTo-DependencySpec -Item $defaultTool.Dependency -DefaultExe ([string]$defaultTool.Executable)
        }
        $override = Get-ManifestMediaToolItem -Payload $Payload -Name $name
        if ($null -ne $override) {
            if ($null -ne $override.PSObject.Properties['displayName']) { $tool.DisplayName = [string]$override.displayName }
            if ($null -ne $override.PSObject.Properties['destination']) { $tool.Destination = [string]$override.destination }
            if ($null -ne $override.PSObject.Properties['executable']) { $tool.Executable = [string]$override.executable }
            $dependencyPayload = if ($null -ne $override.PSObject.Properties['dependency']) { $override.dependency } else { $override }
            $tool.Dependency = ConvertTo-DependencySpec -Item $dependencyPayload -DefaultExe ([string]$tool.Executable)
        }
        $result[$name] = $tool
    }
    return $result
}


function Save-ReleaseZip {
    param([string]$Name, [string]$ZipUrl, [string]$ManifestUrl)
    $downloadRoot = Join-Path $script:Root 'downloads-cache'
    $archivePath = Join-Path $downloadRoot ([IO.Path]::GetFileName($ZipUrl))
    $manifestPath = Join-Path $downloadRoot ([IO.Path]::GetFileName($ManifestUrl))
    Write-Step "Downloading $Name manifest"
    Invoke-Download -Url $ManifestUrl -Destination $manifestPath
    Write-Step "Downloading $Name package"
    Invoke-Download -Url $ZipUrl -Destination $archivePath
    $expectedHash = ''
    if (-not $DryRun -and (Test-Path -LiteralPath $manifestPath)) {
        $manifest = Read-JsonFile -Path $manifestPath -Description "$Name release manifest"
        $sha256Property = @($manifest.PSObject.Properties | Where-Object { $_.Name -eq 'sha256' } | Select-Object -First 1)
        if ($sha256Property.Count -gt 0) {
            $expectedHash = [string]$sha256Property[0].Value
        }
        Assert-RequiredSha256 -Value $expectedHash -Description "$Name release manifest"
    }
    Write-Step "Verifying $Name package"
    Assert-FileHash -Path $archivePath -ExpectedSha256 $expectedHash
    return [ordered]@{
        Name = $Name
        ArchivePath = $archivePath
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

function Save-PackageZip {
    param(
        [string]$Name,
        [string]$ZipUrl,
        [string]$ManifestUrl,
        [string]$LocalZip,
        [string]$LocalManifest
    )
    if ([string]::IsNullOrWhiteSpace($LocalZip)) {
        return Save-ReleaseZip -Name $Name -ZipUrl $ZipUrl -ManifestUrl $ManifestUrl
    }
    $archivePath = [IO.Path]::GetFullPath($LocalZip)
    if (-not $DryRun -and -not (Test-Path -LiteralPath $archivePath)) {
        throw "$Name local package ZIP is missing: $archivePath"
    }
    $manifestPath = Resolve-LocalManifestPath -ZipPath $archivePath -ManifestPath $LocalManifest
    if (-not [string]::IsNullOrWhiteSpace($manifestPath)) {
        if (-not $DryRun -and -not (Test-Path -LiteralPath $manifestPath)) {
            throw "$Name local package manifest is missing: $manifestPath"
        }
        if (-not $DryRun) {
            $manifest = Read-JsonFile -Path $manifestPath -Description "$Name local package manifest"
            $sha256Property = @($manifest.PSObject.Properties | Where-Object { $_.Name -eq 'sha256' } | Select-Object -First 1)
            $expectedHash = ''
            if ($sha256Property.Count -gt 0) {
                $expectedHash = [string]$sha256Property[0].Value
            }
            Assert-RequiredSha256 -Value $expectedHash -Description "$Name local package manifest"
            Write-Step "Verifying local $Name package"
            Assert-FileHash -Path $archivePath -ExpectedSha256 $expectedHash
        }
        Write-Step "Using local $Name package $archivePath with manifest $manifestPath"
    } else {
        Write-Step "Using local $Name package $archivePath without a manifest sidecar"
    }
    return [ordered]@{
        Name = $Name
        ArchivePath = $archivePath
    }
}

function Install-VerifiedReleaseZip {
    param([string]$Name, [string]$ArchivePath, [string]$Destination)
    $downloadRoot = Join-Path $script:Root 'downloads-cache'
    $extractRoot = Join-Path $downloadRoot ("extract-$Name")
    Write-Step "Extracting $Name package"
    Expand-ZipSafe -Archive $archivePath -Destination $extractRoot
    if (-not $DryRun) {
        $extractedPackageRoot = Join-Path $extractRoot $Name
        if (-not (Test-Path -LiteralPath $extractedPackageRoot)) {
            throw "$Name release ZIP did not contain expected root directory '$Name'."
        }
        New-Item -ItemType Directory -Force -Path $Destination | Out-Null
        $targetPackageRoot = Join-Path $Destination $Name
        if (Test-Path -LiteralPath $targetPackageRoot) {
            Remove-Item -Recurse -Force -LiteralPath $targetPackageRoot
        }
        Move-Item -LiteralPath $extractedPackageRoot -Destination $targetPackageRoot
        Remove-Item -Recurse -Force -LiteralPath $extractRoot -ErrorAction SilentlyContinue
    }
    Write-Step "$Name installed"
}

function Install-ReleaseZip {
    param([string]$Name, [string]$ZipUrl, [string]$ManifestUrl, [string]$Destination)
    $package = Save-ReleaseZip -Name $Name -ZipUrl $ZipUrl -ManifestUrl $ManifestUrl
    Install-VerifiedReleaseZip -Name $Name -ArchivePath $package.ArchivePath -Destination $Destination
}

function Install-ArrDependency {
    param([string]$Name, [hashtable]$Spec, [string]$Channel)
    if ($script:SuiteConfig.platform -ne 'x64') {
        throw "Full suite Arr dependencies are x64-only in v1."
    }
    $tag = if ($Channel -eq 'Latest') { 'latest' } else { $Spec.Tag }
    $assetName = $null
    $assetUrl = $Spec.Url
    if ($DryRun -and [string]::IsNullOrWhiteSpace($assetUrl)) {
        Write-Step "Would resolve $Name from $($Spec.Repo) $tag using pattern $($Spec.Pattern)"
        return
    }
    if ([string]::IsNullOrWhiteSpace($assetUrl)) {
        $asset = Get-GithubReleaseAsset -Repo $Spec.Repo -Tag $tag -Pattern $Spec.Pattern
        $assetName = [string]$asset.name
        $assetUrl = [string]$asset.browser_download_url
    } else {
        $assetName = [IO.Path]::GetFileName($assetUrl)
    }
    $archivePath = Join-Path (Join-Path $script:Root 'downloads-cache') $assetName
    $extractRoot = Join-Path (Join-Path $script:Root 'apps') $Name
    if ([string]::IsNullOrWhiteSpace($Spec.Sha256)) {
        throw "$Name dependency download requires a SHA256 hash. Use pinned dependencies or provide -DependencyManifest with sha256."
    }
    if (-not (Restore-DependencyCache -Kind 'arr' -Name "$Name dependency" -AssetName $assetName -ExpectedSha256 $Spec.Sha256 -Destination $archivePath)) {
        Write-Step "Downloading $Name dependency $assetName"
        Invoke-Download -Url $assetUrl -Destination $archivePath
        Write-Step "Verifying $Name dependency"
        Assert-FileHash -Path $archivePath -ExpectedSha256 $Spec.Sha256
        Save-DependencyCache -Kind 'arr' -Name "$Name dependency" -AssetName $assetName -ExpectedSha256 $Spec.Sha256 -Source $archivePath
    }
    Write-Step "Extracting $Name dependency"
    Expand-ZipSafe -Archive $archivePath -Destination $extractRoot
    Write-Step "$Name installed"
}

function Install-P2pClient {
    param([string]$Name, [hashtable]$Meta)
    if ($script:SuiteConfig.platform -ne 'x64') {
        throw "P2P clients are x64-only in v1."
    }
    $display = if ([string]::IsNullOrWhiteSpace([string]$Meta.DisplayName)) { $Name } else { [string]$Meta.DisplayName }
    $spec = $Meta.Dependency
    if ($null -eq $spec) {
        throw "$display has no dependency spec in config\suite-apps.json."
    }
    if ([string]::IsNullOrWhiteSpace([string]$spec.Sha256)) {
        throw "$display has no pinned release yet. Cut a $display release, then set its sha256 (and optionally url/tag) under p2pClients in config\suite-apps.json before enabling it."
    }
    $tag = if ($script:SuiteConfig.dependencyChannel -eq 'Latest') { 'latest' } else { [string]$spec.Tag }
    $assetUrl = [string]$spec.Url
    $assetName = $null
    if ($DryRun -and [string]::IsNullOrWhiteSpace($assetUrl)) {
        Write-Step "Would resolve $display from $($spec.Repo) $tag using pattern $($spec.Pattern)"
        return
    }
    if ([string]::IsNullOrWhiteSpace($assetUrl)) {
        $asset = Get-GithubReleaseAsset -Repo $spec.Repo -Tag $tag -Pattern $spec.Pattern
        $assetName = [string]$asset.name
        $assetUrl = [string]$asset.browser_download_url
    } else {
        $assetName = [IO.Path]::GetFileName($assetUrl)
    }
    $archivePath = Join-Path (Join-Path $script:Root 'downloads-cache') $assetName
    if (-not (Restore-DependencyCache -Kind 'p2p' -Name "$display package" -AssetName $assetName -ExpectedSha256 $spec.Sha256 -Destination $archivePath)) {
        Write-Step "Downloading $display package $assetName"
        Invoke-Download -Url $assetUrl -Destination $archivePath
        Write-Step "Verifying $display package"
        Assert-FileHash -Path $archivePath -ExpectedSha256 $spec.Sha256
        Save-DependencyCache -Kind 'p2p' -Name "$display package" -AssetName $assetName -ExpectedSha256 $spec.Sha256 -Source $archivePath
    }
    # The release ZIP has a single top-level package folder; reuse the named-root
    # extractor so it lands at apps\<Root>.
    $destination = if ([string]::IsNullOrWhiteSpace([string]$Meta.Destination)) { "apps\$Name" } else { [string]$Meta.Destination }
    $packageRoot = [IO.Path]::GetFileName($destination.TrimEnd('\'))
    Install-VerifiedReleaseZip -Name $packageRoot -ArchivePath $archivePath -Destination (Join-Path $script:Root 'apps')
}

function Find-InstalledToolPath {
    param([string]$Root, [string]$Executable)
    if ($DryRun) {
        return ''
    }
    if ([string]::IsNullOrWhiteSpace($Executable)) {
        throw 'Optional tool executable name is required.'
    }
    $directPath = Join-Path $Root $Executable
    if (Test-Path -LiteralPath $directPath -PathType Leaf) {
        return $directPath
    }
    $matches = @(Get-ChildItem -LiteralPath $Root -Recurse -File -Filter $Executable -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($matches.Count -eq 0) {
        throw "Optional tool install did not contain expected file: $Executable"
    }
    return [string]$matches[0].FullName
}

function Install-OptionalToolDependency {
    param([string]$Name, [hashtable]$Spec)
    if ($script:SuiteConfig.platform -ne 'x64') {
        Write-Warning "$($Spec.DisplayName) is optional and is skipped on $($script:SuiteConfig.platform) because the pinned portable package is x64-only."
        return ''
    }
    $dependency = $Spec.Dependency
    if ($null -eq $dependency) {
        throw "$($Spec.DisplayName) optional dependency spec is missing."
    }
    $assetName = [string]$dependency.FileName
    $assetUrl = [string]$dependency.Url
    if ([string]::IsNullOrWhiteSpace($assetName) -and -not [string]::IsNullOrWhiteSpace($assetUrl)) {
        $assetName = [IO.Path]::GetFileName($assetUrl)
    }
    if ([string]::IsNullOrWhiteSpace($assetName)) {
        throw "$($Spec.DisplayName) optional dependency requires fileName or url."
    }
    if ([string]::IsNullOrWhiteSpace($assetUrl)) {
        throw "$($Spec.DisplayName) optional dependency requires a URL."
    }
    if ([string]::IsNullOrWhiteSpace([string]$dependency.Sha256)) {
        throw "$($Spec.DisplayName) optional dependency requires a SHA256 hash."
    }
    $archivePath = Join-Path (Join-Path $script:Root 'downloads-cache') $assetName
    $extractRoot = Join-Path $script:Root ([string]$Spec.Destination)
    if (-not (Restore-DependencyCache -Kind 'media-tool' -Name "$($Spec.DisplayName) optional dependency" -AssetName $assetName -ExpectedSha256 $dependency.Sha256 -Destination $archivePath)) {
        Write-Step "Downloading optional $($Spec.DisplayName) dependency $assetName"
        Invoke-Download -Url $assetUrl -Destination $archivePath
        Write-Step "Verifying optional $($Spec.DisplayName) dependency"
        Assert-FileHash -Path $archivePath -ExpectedSha256 $dependency.Sha256
        Save-DependencyCache -Kind 'media-tool' -Name "$($Spec.DisplayName) optional dependency" -AssetName $assetName -ExpectedSha256 $dependency.Sha256 -Source $archivePath
    }
    Write-Step "Extracting optional $($Spec.DisplayName) dependency"
    Expand-ZipSafe -Archive $archivePath -Destination $extractRoot
    $toolPath = Find-InstalledToolPath -Root $extractRoot -Executable ([string]$Spec.Executable)
    Write-Step "$($Spec.DisplayName) optional dependency installed"
    return $toolPath
}

function Install-OptionalMediaTools {
    param([hashtable]$Config, [object]$DependencyManifestPayload)
    if (-not [bool]$Config.optionalTools.install) {
        Write-Step 'Optional media tools skipped'
        return
    }
    $toolSpecs = Load-MediaToolDependencies -Payload $DependencyManifestPayload
    $configKeys = @{
        'mpc-hc' = 'mpcHc'
        ffmpeg = 'ffmpeg'
        mediainfo = 'mediainfo'
    }
    foreach ($name in @('mpc-hc', 'ffmpeg', 'mediainfo')) {
        $spec = $toolSpecs[$name]
        try {
            $toolPath = Install-OptionalToolDependency -Name $name -Spec $spec
            if (-not [string]::IsNullOrWhiteSpace($toolPath)) {
                $configKey = $configKeys[$name]
                $Config.optionalTools[$configKey].installed = $true
                $Config.optionalTools[$configKey].path = $toolPath
            }
        } catch {
            Write-Warning "$($spec.DisplayName) is optional and was not installed. $($_.Exception.Message)"
        }
    }
}

function ConvertTo-XmlText {
    param([string]$Value)
    return [Security.SecurityElement]::Escape($Value)
}

function Get-ArrUiLanguageConfigValue {
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
        'spanish' { return '3' }
        'italian' { return '5' }
        'portuguese' { return '18' }
        default { return '1' }
    }
}

function Write-ArrConfig {
    param([string]$Name, [int]$Port, [string]$BindAddress, [string]$ApiKey, [string]$Username, [string]$Password, [string]$Language)
    $dataDir = Join-Path (Join-Path $script:Root 'data') $Name
    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
        $safeUsername = ConvertTo-XmlText -Value $Username
        $safePassword = ConvertTo-XmlText -Value $Password
        $safeLanguage = ConvertTo-XmlText -Value (Get-ArrUiLanguageConfigValue -Name $Name -Language $Language)
        @(
            '<Config>'
            '  <LogLevel>info</LogLevel>'
            "  <Port>$Port</Port>"
            '  <UrlBase></UrlBase>'
            "  <BindAddress>$BindAddress</BindAddress>"
            '  <EnableSsl>False</EnableSsl>'
            "  <ApiKey>$ApiKey</ApiKey>"
            "  <UILanguage>$safeLanguage</UILanguage>"
            '  <AuthenticationMethod>Forms</AuthenticationMethod>'
            '  <AuthenticationRequired>Enabled</AuthenticationRequired>'
            "  <Username>$safeUsername</Username>"
            "  <Password>$safePassword</Password>"
            '  <LaunchBrowser>False</LaunchBrowser>'
            "  <InstanceName>eMuleBB $Name</InstanceName>"
            '</Config>'
            ''
        ) -join "`r`n" | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $dataDir 'config.xml')
    }
}

function New-DefaultEmulePreferencesText {
    param([hashtable]$Config)
    $p2pInterface = [string]$Config.p2p.bindInterface
    return @(
        '[eMule]'
        "IncomingDir=$(Join-Path $script:Root 'downloads\incoming')"
        "TempDir=$(Join-Path $script:Root 'downloads\temp')"
        'CreateCrashDump=2'
        'SaveLogToDisk=1'
        "Language=$([int]$Config.language.emuleLanguageId)"
        "BindInterface=$p2pInterface"
        'BindAddr='
        'BlockNetworkWhenBindUnavailableAtStartup=0'
        'NetworkGuardMode=Off'
        'NetworkGuardAllowedCIDRs='
        '[WebServer]'
        'Enabled=1'
        "BindAddr=$($Config.services.emulebb.bindAddress)"
        "Port=$($Config.services.emulebb.port)"
        'UseHTTPS=0'
        'WebUseUPnP=0'
        "ApiKey=$($Config.services.emulebb.apiKey)"
        ''
    ) -join "`r`n"
}

function Copy-DirectoryContents {
    param([string]$Source, [string]$Destination)
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    foreach ($child in @(Get-ChildItem -LiteralPath $Source -Force)) {
        $target = Join-Path $Destination $child.Name
        if ($child.PSIsContainer) {
            Copy-Item -Recurse -Force -LiteralPath $child.FullName -Destination $target
        } else {
            Copy-Item -Force -LiteralPath $child.FullName -Destination $target
        }
    }
}

function Append-PendingIniValues {
    param(
        [System.Collections.Generic.List[string]]$Output,
        [hashtable]$Pending,
        [string]$Section,
        [string]$Newline,
        [object[]]$Updates
    )
    foreach ($update in $Updates) {
        $pendingKey = "$(([string]$update.Section).ToLowerInvariant())|$(([string]$update.Key).ToLowerInvariant())"
        if ($pendingKey.StartsWith("$Section|") -and $Pending.ContainsKey($pendingKey)) {
            $entry = $Pending[$pendingKey]
            $Output.Add("$($entry.Key)=$($entry.Value)$Newline")
            $Pending.Remove($pendingKey)
        }
    }
}

function Update-IniText {
    param([string]$Text, [object[]]$Updates)
    $pending = @{}
    foreach ($update in $Updates) {
        $pending["$(([string]$update.Section).ToLowerInvariant())|$(([string]$update.Key).ToLowerInvariant())"] = $update
    }
    $lines = [regex]::Split($Text, "(`r`n|`n|`r)")
    $output = New-Object 'System.Collections.Generic.List[string]'
    $currentSection = ''
    $seenSections = @{}
    $newline = "`r`n"
    for ($i = 0; $i -lt $lines.Count; $i += 2) {
        $line = $lines[$i]
        if (($i + 1) -lt $lines.Count -and -not [string]::IsNullOrEmpty($lines[$i + 1])) {
            $newline = $lines[$i + 1]
        }
        if ($i -eq ($lines.Count - 1) -and [string]::IsNullOrEmpty($line)) {
            continue
        }
        $trimmed = $line.Trim()
        if ($trimmed.StartsWith('[') -and $trimmed.EndsWith(']')) {
            Append-PendingIniValues -Output $output -Pending $pending -Section $currentSection -Newline $newline -Updates $Updates
            $currentSection = $trimmed.Substring(1, $trimmed.Length - 2).Trim().ToLowerInvariant()
            $seenSections[$currentSection] = $true
            $output.Add("$line$newline")
            continue
        }
        $equalsIndex = $line.IndexOf('=')
        if ($equalsIndex -ge 0 -and -not [string]::IsNullOrWhiteSpace($currentSection)) {
            $key = $line.Substring(0, $equalsIndex).Trim()
            $pendingKey = "$currentSection|$($key.ToLowerInvariant())"
            if ($pending.ContainsKey($pendingKey)) {
                $entry = $pending[$pendingKey]
                $output.Add("$($entry.Key)=$($entry.Value)$newline")
                $pending.Remove($pendingKey)
                continue
            }
        }
        $output.Add("$line$newline")
    }
    Append-PendingIniValues -Output $output -Pending $pending -Section $currentSection -Newline $newline -Updates $Updates
    foreach ($update in $Updates) {
        $pendingKey = "$(([string]$update.Section).ToLowerInvariant())|$(([string]$update.Key).ToLowerInvariant())"
        if (-not $pending.ContainsKey($pendingKey)) {
            continue
        }
        $entry = $pending[$pendingKey]
        $sectionName = [string]$entry.Section
        if (-not $seenSections.ContainsKey($sectionName.ToLowerInvariant())) {
            $output.Add("[$sectionName]$newline")
            $seenSections[$sectionName.ToLowerInvariant()] = $true
        }
        $output.Add("$($entry.Key)=$($entry.Value)$newline")
        $pending.Remove($pendingKey)
    }
    return ($output -join '')
}

function Update-EmulePreferencesFile {
    param([string]$PreferencesPath, [hashtable]$Config)
    $updates = @(
        [pscustomobject]@{ Section = 'eMule'; Key = 'IncomingDir'; Value = (Join-Path $script:Root 'downloads\incoming') }
        [pscustomobject]@{ Section = 'eMule'; Key = 'TempDir'; Value = (Join-Path $script:Root 'downloads\temp') }
        [pscustomobject]@{ Section = 'eMule'; Key = 'CreateCrashDump'; Value = '2' }
        [pscustomobject]@{ Section = 'eMule'; Key = 'SaveLogToDisk'; Value = '1' }
        [pscustomobject]@{ Section = 'eMule'; Key = 'Language'; Value = [string]([int]$Config.language.emuleLanguageId) }
        [pscustomobject]@{ Section = 'eMule'; Key = 'BindInterface'; Value = [string]$Config.p2p.bindInterface }
        [pscustomobject]@{ Section = 'eMule'; Key = 'BindAddr'; Value = '' }
        [pscustomobject]@{ Section = 'eMule'; Key = 'BlockNetworkWhenBindUnavailableAtStartup'; Value = '0' }
        [pscustomobject]@{ Section = 'eMule'; Key = 'NetworkGuardMode'; Value = [string]$Config.p2p.networkGuardMode }
        [pscustomobject]@{ Section = 'eMule'; Key = 'NetworkGuardAllowedCIDRs'; Value = [string]$Config.p2p.networkGuardAllowedCIDRs }
        [pscustomobject]@{ Section = 'WebServer'; Key = 'Enabled'; Value = '1' }
        [pscustomobject]@{ Section = 'WebServer'; Key = 'BindAddr'; Value = [string]$Config.services.emulebb.bindAddress }
        [pscustomobject]@{ Section = 'WebServer'; Key = 'Port'; Value = [string]$Config.services.emulebb.port }
        [pscustomobject]@{ Section = 'WebServer'; Key = 'UseHTTPS'; Value = '0' }
        [pscustomobject]@{ Section = 'WebServer'; Key = 'WebUseUPnP'; Value = '0' }
        [pscustomobject]@{ Section = 'WebServer'; Key = 'ApiKey'; Value = [string]$Config.services.emulebb.apiKey }
    )
    if ($Config.optionalTools.mpcHc.installed -and -not [string]::IsNullOrWhiteSpace([string]$Config.optionalTools.mpcHc.path)) {
        $updates += [pscustomobject]@{ Section = 'eMule'; Key = 'VideoPlayer'; Value = [string]$Config.optionalTools.mpcHc.path }
        $updates += [pscustomobject]@{ Section = 'eMule'; Key = 'VideoPlayerArgs'; Value = '' }
    }
    if ($Config.optionalTools.ffmpeg.installed -and -not [string]::IsNullOrWhiteSpace([string]$Config.optionalTools.ffmpeg.path)) {
        $updates += [pscustomobject]@{ Section = 'eMule'; Key = 'VideoThumbnailFfmpegPath'; Value = [string]$Config.optionalTools.ffmpeg.path }
        $updates += [pscustomobject]@{ Section = 'eMule'; Key = 'VideoThumbnailIntervalSeconds'; Value = '90' }
        $updates += [pscustomobject]@{ Section = 'eMule'; Key = 'AllowPeerPreview'; Value = '0' }
    }
    if ($Config.optionalTools.mediainfo.installed -and -not [string]::IsNullOrWhiteSpace([string]$Config.optionalTools.mediainfo.path)) {
        $updates += [pscustomobject]@{ Section = 'eMule'; Key = 'MediaInfo_MediaInfoDllPath'; Value = [string]$Config.optionalTools.mediainfo.path }
    }
    $text = Get-Content -Raw -LiteralPath $PreferencesPath
    Update-IniText -Text $text -Updates $updates | Set-Content -Encoding Unicode -LiteralPath $PreferencesPath
}

function New-DefaultCategoryIniText {
    return @"
[General]
Count=0

[Cat#0]
Title=
Incoming=
Comment=
RegularExpression=
Color=-1
a4afPriority=1
AutoCat=
Filter=0
FilterNegator=0
AutoCatAsRegularExpression=0
downloadInAlphabeticalOrder=0
Care4All=0

"@
}

function Get-IniSections {
    param([string]$Text)
    $sections = @{}
    $currentSection = ''
    foreach ($line in [regex]::Split($Text, "(`r`n|`n|`r)")) {
        $trimmed = $line.Trim()
        if ($trimmed.StartsWith('[') -and $trimmed.EndsWith(']')) {
            $currentSection = $trimmed.Substring(1, $trimmed.Length - 2).Trim()
            if (-not $sections.ContainsKey($currentSection)) {
                $sections[$currentSection] = @{}
            }
            continue
        }
        $equalsIndex = $line.IndexOf('=')
        if ($equalsIndex -lt 0 -or [string]::IsNullOrWhiteSpace($currentSection)) {
            continue
        }
        $key = $line.Substring(0, $equalsIndex).Trim()
        $value = $line.Substring($equalsIndex + 1)
        $sections[$currentSection][$key] = $value
    }
    return $sections
}

function Ensure-EmuleCategoryIni {
    param([string]$ConfigDir, [hashtable]$Config)
    $categoryPath = Join-Path $ConfigDir 'Category.ini'

    $text = if (Test-Path -LiteralPath $categoryPath) { Get-Content -Raw -LiteralPath $categoryPath } else { New-DefaultCategoryIniText }
    $sections = Get-IniSections -Text $text
    $categoryCount = 0
    if ($sections.ContainsKey('General') -and $sections['General'].ContainsKey('Count')) {
        [void][int]::TryParse([string]$sections['General']['Count'], [ref]$categoryCount)
    }
    foreach ($sectionName in @($sections.Keys)) {
        if ($sectionName -match '^Cat#(\d+)$') {
            $categoryCount = [Math]::Max($categoryCount, [int]$Matches[1])
        }
    }

    $updates = New-Object 'System.Collections.Generic.List[object]'
    foreach ($arrName in @(Get-SelectedArrAppNames -Config $Config)) {
        $incoming = Join-Path $script:Root ("downloads\$arrName")
        New-Item -ItemType Directory -Force -Path $incoming | Out-Null
        $entry = [pscustomobject]@{ Title = "emulebb-$arrName"; Incoming = $incoming }
        $section = $null
        foreach ($sectionName in @($sections.Keys)) {
            if ($sectionName -notmatch '^Cat#\d+$') {
                continue
            }
            if ($sections[$sectionName].ContainsKey('Title') -and [string]::Equals([string]$sections[$sectionName]['Title'], [string]$entry.Title, [StringComparison]::OrdinalIgnoreCase)) {
                $section = $sectionName
                break
            }
        }
        if ([string]::IsNullOrWhiteSpace($section)) {
            $categoryCount += 1
            $section = "Cat#$categoryCount"
            $updates.Add([pscustomobject]@{ Section = $section; Key = 'Title'; Value = [string]$entry.Title })
            $updates.Add([pscustomobject]@{ Section = $section; Key = 'Comment'; Value = 'eMuleBB suite Arr integration' })
            $updates.Add([pscustomobject]@{ Section = $section; Key = 'RegularExpression'; Value = '' })
            $updates.Add([pscustomobject]@{ Section = $section; Key = 'Color'; Value = '-1' })
            $updates.Add([pscustomobject]@{ Section = $section; Key = 'a4afPriority'; Value = '1' })
            $updates.Add([pscustomobject]@{ Section = $section; Key = 'AutoCat'; Value = '' })
            $updates.Add([pscustomobject]@{ Section = $section; Key = 'Filter'; Value = '0' })
            $updates.Add([pscustomobject]@{ Section = $section; Key = 'FilterNegator'; Value = '0' })
            $updates.Add([pscustomobject]@{ Section = $section; Key = 'AutoCatAsRegularExpression'; Value = '0' })
            $updates.Add([pscustomobject]@{ Section = $section; Key = 'downloadInAlphabeticalOrder'; Value = '0' })
            $updates.Add([pscustomobject]@{ Section = $section; Key = 'Care4All'; Value = '0' })
        }
        $updates.Add([pscustomobject]@{ Section = $section; Key = 'Incoming'; Value = [IO.Path]::GetFullPath([string]$entry.Incoming) })
    }
    $updates.Add([pscustomobject]@{ Section = 'General'; Key = 'Count'; Value = [string]$categoryCount })
    Update-IniText -Text $text -Updates $updates | Set-Content -Encoding Unicode -LiteralPath $categoryPath
}

function Write-EmuleProfile {
    param([hashtable]$Config)
    $configDir = Join-Path (Join-Path $script:Root 'profiles\emulebb') 'config'
    $preferencesPath = Join-Path $configDir 'preferences.ini'
    $importResult = @{
        configured = -not [string]::IsNullOrWhiteSpace($Config.importProfileDir)
        source = if ([string]::IsNullOrWhiteSpace($Config.importProfileDir)) { $null } else { [string]$Config.importProfileDir }
        action = 'skipped-existing'
        sourcePreferencesSha256 = $null
    }
    if ($DryRun) {
        Write-Step "Would ensure eMuleBB profile under $configDir"
        return $importResult
    }
    if (-not (Test-Path -LiteralPath $preferencesPath)) {
        if (-not [string]::IsNullOrWhiteSpace($Config.importProfileDir)) {
            $sourceConfigDir = Join-Path ([IO.Path]::GetFullPath([string]$Config.importProfileDir)) 'config'
            $sourcePreferences = Join-Path $sourceConfigDir 'preferences.ini'
            if (-not (Test-Path -LiteralPath $sourcePreferences)) {
                throw "ImportProfileDir must contain config\preferences.ini: $sourcePreferences"
            }
            $importResult.action = 'imported'
            $importResult.sourcePreferencesSha256 = Get-Sha256 -Path $sourcePreferences
            if (Test-Path -LiteralPath $configDir) {
                Remove-Item -Recurse -Force -LiteralPath $configDir
            }
            Copy-DirectoryContents -Source $sourceConfigDir -Destination $configDir
        } else {
            $importResult.action = 'fresh'
            New-Item -ItemType Directory -Force -Path $configDir | Out-Null
            New-DefaultEmulePreferencesText -Config $Config | Set-Content -Encoding Unicode -LiteralPath $preferencesPath
        }
    }
    Update-EmulePreferencesFile -PreferencesPath $preferencesPath -Config $Config
    if (@(Get-SelectedArrAppNames -Config $Config).Count -gt 0) {
        Ensure-EmuleCategoryIni -ConfigDir $configDir -Config $Config
    }
    return $importResult
}

function Copy-OptionalEmuleSymbols {
    param([hashtable]$Config)
    $result = @{
        configured = -not [string]::IsNullOrWhiteSpace($Config.symbols.emulebbPdbPath)
        source = if ([string]::IsNullOrWhiteSpace($Config.symbols.emulebbPdbPath)) { $null } else { [string]$Config.symbols.emulebbPdbPath }
        adjacentPdb = $null
        versionedPdb = $null
        sourceSha256 = $null
        action = 'skipped-not-configured'
    }
    if ([string]::IsNullOrWhiteSpace($Config.symbols.emulebbPdbPath)) {
        return $result
    }
    $sourcePdb = [IO.Path]::GetFullPath([string]$Config.symbols.emulebbPdbPath)
    if (-not (Test-Path -LiteralPath $sourcePdb)) {
        throw "EmulebbPdbPath is missing: $sourcePdb"
    }
    $pdbFileName = Get-EmulebbPdbFileName -ExecutableName ([string]$Config.emulebbExecutableName)
    $adjacentPdb = Join-Path (Join-Path $script:Root 'apps\eMuleBB') $pdbFileName
    $symbolsDir = Join-Path (Join-Path (Join-Path $script:Root 'symbols') "emulebb-v$($Config.version)") ([string]$Config.platform)
    $versionedPdb = Join-Path $symbolsDir $pdbFileName
    $result.adjacentPdb = $adjacentPdb
    $result.versionedPdb = $versionedPdb
    $result.sourceSha256 = Get-Sha256 -Path $sourcePdb
    if ($DryRun) {
        $result.action = 'dry-run'
        Write-Step "Would copy eMuleBB symbols from $sourcePdb"
        return $result
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $adjacentPdb) | Out-Null
    New-Item -ItemType Directory -Force -Path $symbolsDir | Out-Null
    Copy-Item -Force -LiteralPath $sourcePdb -Destination $adjacentPdb
    Copy-Item -Force -LiteralPath $sourcePdb -Destination $versionedPdb
    $result.action = 'copied'
    return $result
}

function Assert-EmulebbExecutableInstalled {
    param([hashtable]$Config)
    if ($DryRun) {
        return
    }
    $exePath = Join-Path (Join-Path $script:Root 'apps\eMuleBB') ([string]$Config.emulebbExecutableName)
    if (-not (Test-Path -LiteralPath $exePath)) {
        throw "Installed eMuleBB package did not include selected executable: $($Config.emulebbExecutableName)"
    }
}

function Write-SuiteConfigFile {
    param([hashtable]$Config)
    if ($DryRun) {
        return
    }
    $manifestDir = Join-Path $script:Root 'manifests'
    New-Item -ItemType Directory -Force -Path $manifestDir | Out-Null
    # WHY: suite-config.json stores generated API keys and the suite password in
    # plaintext and is intentionally left at default inherited NTFS ACLs. The suite
    # is a single-user local install; the same secrets are also rendered to
    # credentials.html and used in cleartext on loopback/LAN. ACL-locking this file
    # is an accepted non-goal for this local tooling, not a packaging defect.
    $Config | ConvertTo-Json -Depth 10 | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $manifestDir 'suite-config.json')
}

function Get-ServiceUrl {
    param($Service)
    $hostName = [string]$Service.clientHost
    if ([string]::IsNullOrWhiteSpace($hostName)) { $hostName = [string]$Service.bindAddress }
    return "http://$hostName`:$([int]$Service.port)"
}

function ConvertTo-HtmlText {
    param([string]$Value)
    return [Security.SecurityElement]::Escape($Value)
}

function New-CopyFieldHtml {
    param([string]$Label, [string]$Value)
    $safeLabel = ConvertTo-HtmlText -Value $Label
    $safeValue = ConvertTo-HtmlText -Value $Value
    return "<div class=`"field`"><span>$safeLabel</span><code>$safeValue</code><button type=`"button`" data-copy=`"$safeValue`">Copy</button></div>"
}

function New-ServiceCardHtml {
    param([string]$Name, [string]$Url, [object[]]$Fields)
    $safeName = ConvertTo-HtmlText -Value $Name
    $safeUrl = ConvertTo-HtmlText -Value $Url
    $fieldHtml = ($Fields -join "`r`n")
    return @"
<section class="card">
  <div class="card-head">
    <h2>$safeName</h2>
    <a class="open" href="$safeUrl" target="_blank" rel="noopener noreferrer">Open</a>
  </div>
  <div class="url"><a href="$safeUrl" target="_blank" rel="noopener noreferrer">$safeUrl</a></div>
  <div class="fields">
$fieldHtml
  </div>
</section>
"@
}

function Write-CredentialsFile {
    param([hashtable]$Config)
    if ($DryRun) {
        return
    }
    $lines = New-Object 'System.Collections.Generic.List[string]'
    $lines.Add('eMuleBB Suite credentials')
    $lines.Add("Generated UTC: $([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))")
    $lines.Add("Install root: $script:Root")
    $lines.Add("Bundle: $($Config.bundle)")
    $lines.Add('Selected apps: ' + (Get-InstallDescription -ServiceNames @(Get-SuiteServiceNames -Config $Config)))
    $lines.Add("Media language: $($Config.language.mediaLanguageName)")
    $lines.Add("UI language: $($Config.language.uiLanguageName)")
    $lines.Add('')
    $lines.Add('Suite web login')
    $lines.Add("Username: $($Config.credentials.username)")
    $lines.Add("Password: $($Config.credentials.password)")
    $lines.Add('')
    $lines.Add('Services')
    $lines.Add("eMuleBB URL: $(Get-ServiceUrl -Service $Config.services.emulebb)")
    $lines.Add("eMuleBB API key: $($Config.services.emulebb.apiKey)")
    if (@(Get-SelectedArrAppNames -Config $Config).Count -gt 0) {
        foreach ($serviceName in @(Get-SelectedArrAppNames -Config $Config)) {
            $service = $Config.services[$serviceName]
            $lines.Add('')
            $lines.Add("$serviceName URL: $(Get-ServiceUrl -Service $service)")
            $lines.Add("$serviceName API key: $($service.apiKey)")
            $lines.Add("$serviceName web authentication: disabled by suite config; use the API key above for integrations.")
        }
        $lines.Add('')
        $lines.Add('Arr download client')
        $lines.Add('Name: eMuleBB Suite')
        $lines.Add('Username: emule')
        $lines.Add("Password: $($Config.services.emulebb.apiKey)")
    }
    if (@(Get-SelectedArrAppNames -Config $Config).Count -gt 0) {
        $lines.Add('')
        $lines.Add('First-run setup')
        $lines.Add('Run scripts\Initialize-Suite.ps1 once if install-time startup was skipped. It starts the suite, registers integrations, and creates selected Arr root folders.')
        $lines.Add('If a registration script asks for Action, press Enter to use the default Register option. Choose Unregister only when you are intentionally removing an integration.')
    }
    $lines.Add('')
    $lines.Add('Files')
    $lines.Add('Suite config: manifests\suite-config.json')
    $lines.Add('Incoming downloads: downloads\incoming')
    $lines.Add('Temporary downloads: downloads\temp')
    $installedTools = @()
    if ($Config.optionalTools.mpcHc.installed) { $installedTools += "MPC-HC: $($Config.optionalTools.mpcHc.path)" }
    if ($Config.optionalTools.ffmpeg.installed) { $installedTools += "FFmpeg: $($Config.optionalTools.ffmpeg.path)" }
    if ($Config.optionalTools.mediainfo.installed) { $installedTools += "MediaInfo DLL: $($Config.optionalTools.mediainfo.path)" }
    if ($installedTools.Count -gt 0) {
        $lines.Add('')
        $lines.Add('Optional media tools')
        foreach ($tool in $installedTools) {
            $lines.Add($tool)
        }
    }
    ($lines -join "`r`n") + "`r`n" | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $script:Root 'credentials.txt')

    $cards = New-Object 'System.Collections.Generic.List[string]'
    $suiteFields = @(
        (New-CopyFieldHtml -Label 'Username' -Value ([string]$Config.credentials.username)),
        (New-CopyFieldHtml -Label 'Password' -Value ([string]$Config.credentials.password))
    )
    $cards.Add((New-ServiceCardHtml -Name 'Suite Login' -Url (Get-ServiceUrl -Service $Config.services.emulebb) -Fields $suiteFields))

    $emuleFields = @(
        (New-CopyFieldHtml -Label 'API key' -Value ([string]$Config.services.emulebb.apiKey))
    )
    $cards.Add((New-ServiceCardHtml -Name 'eMuleBB' -Url (Get-ServiceUrl -Service $Config.services.emulebb) -Fields $emuleFields))

    if (@(Get-SelectedArrAppNames -Config $Config).Count -gt 0) {
        foreach ($serviceName in @(Get-SelectedArrAppNames -Config $Config)) {
            $service = $Config.services[$serviceName]
            $arrFields = @(
                (New-CopyFieldHtml -Label 'Username' -Value ([string]$Config.credentials.username)),
                (New-CopyFieldHtml -Label 'Password' -Value ([string]$Config.credentials.password)),
                (New-CopyFieldHtml -Label 'API key' -Value ([string]$service.apiKey))
            )
            $cards.Add((New-ServiceCardHtml -Name (Get-ServiceDisplayName -ServiceName $serviceName) -Url (Get-ServiceUrl -Service $service) -Fields $arrFields))
        }
        $downloadClientFields = @(
            (New-CopyFieldHtml -Label 'Name' -Value 'eMuleBB Suite'),
            (New-CopyFieldHtml -Label 'Username' -Value 'emule'),
            (New-CopyFieldHtml -Label 'Password' -Value ([string]$Config.services.emulebb.apiKey))
        )
        $cards.Add((New-ServiceCardHtml -Name 'Arr Download Client' -Url (Get-ServiceUrl -Service $Config.services.emulebb) -Fields $downloadClientFields))
    }

    $generatedUtc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    $safeRoot = ConvertTo-HtmlText -Value $script:Root
    $safeBundle = ConvertTo-HtmlText -Value ([string]$Config.bundle)
    $safeVersion = ConvertTo-HtmlText -Value ([string]$Config.version)
    $safePlatform = ConvertTo-HtmlText -Value ([string]$Config.platform)
    $safeMediaLanguage = ConvertTo-HtmlText -Value ([string]$Config.language.mediaLanguageName)
    $safeUiLanguage = ConvertTo-HtmlText -Value ([string]$Config.language.uiLanguageName)
    $safeApps = ConvertTo-HtmlText -Value (Get-InstallDescription -ServiceNames @(Get-SuiteServiceNames -Config $Config))
    $cardsHtml = ($cards -join "`r`n")
    $html = @"
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>eMuleBB Suite Credentials</title>
  <style>
    body { margin: 0; font: 14px/1.45 "Segoe UI", Arial, sans-serif; color: #1f2933; background: #f5f7fa; }
    main { max-width: 1040px; margin: 0 auto; padding: 28px; }
    h1 { margin: 0 0 8px; font-size: 28px; }
    h2 { margin: 0; font-size: 18px; }
    .summary { margin: 0 0 22px; color: #52606d; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(290px, 1fr)); gap: 14px; }
    .card { background: #fff; border: 1px solid #d9e2ec; border-radius: 8px; padding: 16px; box-shadow: 0 1px 2px rgba(16, 24, 40, .06); }
    .card-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .open, button { border: 1px solid #2563eb; background: #2563eb; color: #fff; border-radius: 6px; padding: 6px 10px; text-decoration: none; cursor: pointer; }
    .url { margin: 10px 0 12px; overflow-wrap: anywhere; }
    .url a { color: #1d4ed8; }
    .field { display: grid; grid-template-columns: 78px minmax(0, 1fr) auto; align-items: center; gap: 8px; padding: 7px 0; border-top: 1px solid #edf2f7; }
    .field span { color: #52606d; }
    code { background: #f1f5f9; border: 1px solid #d9e2ec; border-radius: 4px; padding: 4px 6px; overflow-wrap: anywhere; }
    footer { margin-top: 22px; color: #697586; }
  </style>
</head>
<body>
<main>
  <h1>eMuleBB Suite Credentials</h1>
  <p class="summary">Generated $generatedUtc. Bundle $safeBundle, version $safeVersion, platform $safePlatform. Media language $safeMediaLanguage. UI language $safeUiLanguage. Apps: $safeApps. Install root: $safeRoot.</p>
  <div class="grid">
$cardsHtml
  </div>
  <footer>Keep this file private. API keys and passwords are shown here for first-run setup and recovery. Run scripts\Initialize-Suite.ps1 once if install-time startup was skipped so selected Arr root folders and suite registrations are created. If a registration script asks for Action, press Enter to use the default Register option; choose Unregister only when removing an integration.</footer>
</main>
<script>
document.addEventListener('click', async function (event) {
  var button = event.target.closest('button[data-copy]');
  if (!button) return;
  var value = button.getAttribute('data-copy');
  try {
    await navigator.clipboard.writeText(value);
    button.textContent = 'Copied';
    setTimeout(function () { button.textContent = 'Copy'; }, 1200);
  } catch (err) {
    var input = document.createElement('input');
    input.value = value;
    document.body.appendChild(input);
    input.select();
    document.execCommand('copy');
    document.body.removeChild(input);
  }
});
</script>
</body>
</html>
"@
    $html | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $script:Root 'credentials.html')
}

function Get-SuiteScriptNames {
    return @(
        'Import-SuiteAppManifest.ps1',
        'Install-eMuleBBSuite.ps1',
        'Initialize-Suite.ps1',
        'Start-eMuleBB.ps1',
        'Start-Suite.ps1',
        'Stop-Suite.ps1',
        'Get-SuiteStatus.ps1',
        'Get-SuiteInfo.ps1',
        'Repair-Suite.ps1'
    )
}

function Get-SuiteConfigAssetNames {
    return @(
        'suite-apps.json',
        'suite-languages.json'
    )
}

function Get-AutomationExampleNames {
    return @(
        'README.md',
        'Import-eMuleBBRestExample.ps1',
        'Get-eMuleBBStatus.ps1',
        'Set-eMuleBBLimits.ps1',
        'Search-eMuleBB.ps1',
        'Download-ReleaseGroup.ps1'
    )
}

function Get-ArchiveLeafName {
    param([string]$Value, [string]$DefaultName)
    $uri = $null
    if ([Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri) -and -not [string]::IsNullOrWhiteSpace($uri.AbsolutePath)) {
        $leaf = [IO.Path]::GetFileName($uri.AbsolutePath)
        if (-not [string]::IsNullOrWhiteSpace($leaf)) {
            return $leaf
        }
    }
    $pathLeaf = [IO.Path]::GetFileName($Value)
    if ([string]::IsNullOrWhiteSpace($pathLeaf)) {
        return $DefaultName
    }
    return $pathLeaf
}

function Save-SuiteScriptsBundle {
    param([hashtable]$Config)
    $zipSpec = [string]$Config.suiteScripts.zip
    if ([string]::IsNullOrWhiteSpace($zipSpec)) {
        return ''
    }
    $manifestSpec = [string]$Config.suiteScripts.manifest
    if ([string]::IsNullOrWhiteSpace($manifestSpec)) {
        throw 'SuiteScriptsZip requires -SuiteScriptsManifest with a SHA256 hash.'
    }
    $downloadRoot = Join-Path $script:Root 'downloads-cache'
    $archivePath = Join-Path $downloadRoot (Get-ArchiveLeafName -Value $zipSpec -DefaultName 'suite-scripts.zip')
    $manifestPath = Join-Path $downloadRoot (Get-ArchiveLeafName -Value $manifestSpec -DefaultName 'suite-scripts.manifest.json')
    Write-Step 'Downloading suite scripts bundle'
    Invoke-Download -Url $zipSpec -Destination $archivePath
    Write-Step 'Downloading suite scripts manifest'
    Invoke-Download -Url $manifestSpec -Destination $manifestPath
    $manifest = Read-JsonFile -Path $manifestPath -Description 'SuiteScriptsManifest'
    if ($null -eq $manifest.PSObject.Properties['sha256'] -or [string]::IsNullOrWhiteSpace([string]$manifest.sha256)) {
        throw "SuiteScriptsManifest does not contain sha256: $manifestPath"
    }
    Assert-FileHash -Path $archivePath -ExpectedSha256 ([string]$manifest.sha256)
    return $archivePath
}

function Save-AutomationExamplesBundle {
    param([hashtable]$Config)
    $zipSpec = [string]$Config.automationExamples.zip
    if ([string]::IsNullOrWhiteSpace($zipSpec)) {
        return ''
    }
    $manifestSpec = [string]$Config.automationExamples.manifest
    if ([string]::IsNullOrWhiteSpace($manifestSpec)) {
        throw 'AutomationExamplesZip requires -AutomationExamplesManifest with a SHA256 hash.'
    }
    $downloadRoot = Join-Path $script:Root 'downloads-cache'
    $archivePath = Join-Path $downloadRoot (Get-ArchiveLeafName -Value $zipSpec -DefaultName 'automation-examples.zip')
    $manifestPath = Join-Path $downloadRoot (Get-ArchiveLeafName -Value $manifestSpec -DefaultName 'automation-examples.manifest.json')
    Write-Step 'Downloading automation examples bundle'
    Invoke-Download -Url $zipSpec -Destination $archivePath
    Write-Step 'Downloading automation examples manifest'
    Invoke-Download -Url $manifestSpec -Destination $manifestPath
    $manifest = Read-JsonFile -Path $manifestPath -Description 'AutomationExamplesManifest'
    if ($null -eq $manifest.PSObject.Properties['sha256'] -or [string]::IsNullOrWhiteSpace([string]$manifest.sha256)) {
        throw "AutomationExamplesManifest does not contain sha256: $manifestPath"
    }
    Assert-FileHash -Path $archivePath -ExpectedSha256 ([string]$manifest.sha256)
    return $archivePath
}

function Copy-SuiteScriptSet {
    param([string]$SourceRoot, [string]$DestinationRoot, [string]$Description)
    foreach ($scriptName in @(Get-SuiteScriptNames)) {
        $source = Get-ChildItem -Path $SourceRoot -Filter $scriptName -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $source) {
            throw "$Description did not include scripts\$scriptName."
        }
        Copy-Item -Force -LiteralPath $source.FullName -Destination (Join-Path $DestinationRoot $scriptName)
    }
}

function Copy-AutomationExampleSet {
    param([string]$SourceRoot, [string]$DestinationRoot)
    if (Test-Path -LiteralPath $DestinationRoot) {
        Remove-Item -Recurse -Force -LiteralPath $DestinationRoot
    }
    New-Item -ItemType Directory -Force -Path $DestinationRoot | Out-Null
    foreach ($assetName in @(Get-AutomationExampleNames)) {
        $source = Get-ChildItem -Path $SourceRoot -Filter $assetName -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $source) {
            throw "Automation examples bundle did not include examples\automation\$assetName."
        }
        Copy-Item -Force -LiteralPath $source.FullName -Destination (Join-Path $DestinationRoot $assetName)
    }
}

function Copy-SuiteConfigAssetSet {
    param([string]$SourceRoot, [string]$DestinationRoot)
    if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
        return
    }
    foreach ($assetName in @(Get-SuiteConfigAssetNames)) {
        $source = Get-ChildItem -Path $SourceRoot -Filter $assetName -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $source) {
            New-Item -ItemType Directory -Force -Path $DestinationRoot | Out-Null
            Copy-Item -Force -LiteralPath $source.FullName -Destination (Join-Path $DestinationRoot $assetName)
        }
    }
}

function Write-AutomationExamples {
    param([hashtable]$Config)
    $examplesDir = Join-Path $script:Root 'examples\automation'
    if ($DryRun) {
        Write-Step "Would install automation examples"
        return
    }
    $automationExamplesBundle = Save-AutomationExamplesBundle -Config $Config
    if ([string]::IsNullOrWhiteSpace($automationExamplesBundle)) {
        return
    }
    $extractRoot = Join-Path (Join-Path $script:Root 'downloads-cache') 'extract-automation-examples'
    if (Test-Path -LiteralPath $extractRoot) {
        Remove-Item -Recurse -Force -LiteralPath $extractRoot
    }
    Write-Step 'Extracting automation examples bundle'
    Expand-ZipSafe -Archive $automationExamplesBundle -Destination $extractRoot
    Copy-AutomationExampleSet -SourceRoot $extractRoot -DestinationRoot $examplesDir
}

function Write-SuiteScripts {
    param([hashtable]$Config)
    $scriptsDir = Join-Path $script:Root 'scripts'
    $configDir = Join-Path $script:Root 'config'
    if ($DryRun) {
        Write-Step "Would copy suite control scripts"
        return
    }
    New-Item -ItemType Directory -Force -Path $scriptsDir | Out-Null
    $suiteScriptsBundle = Save-SuiteScriptsBundle -Config $Config
    if (-not [string]::IsNullOrWhiteSpace($suiteScriptsBundle)) {
        $extractRoot = Join-Path (Join-Path $script:Root 'downloads-cache') 'extract-suite-scripts'
        if (Test-Path -LiteralPath $extractRoot) {
            Remove-Item -Recurse -Force -LiteralPath $extractRoot
        }
        Write-Step 'Extracting suite scripts bundle'
        Expand-ZipSafe -Archive $suiteScriptsBundle -Destination $extractRoot
        Copy-SuiteScriptSet -SourceRoot $extractRoot -DestinationRoot $scriptsDir -Description 'Suite scripts bundle'
        Copy-SuiteConfigAssetSet -SourceRoot $extractRoot -DestinationRoot $configDir
        return
    }
    $sourceScriptsDir = Join-Path $script:Root 'apps\eMuleBB\scripts'
    if (-not (Test-Path -LiteralPath $sourceScriptsDir -PathType Container)) {
        throw "Installed eMuleBB package did not include scripts directory: $sourceScriptsDir"
    }
    Copy-SuiteScriptSet -SourceRoot $sourceScriptsDir -DestinationRoot $scriptsDir -Description 'Installed eMuleBB package'
    Copy-SuiteConfigAssetSet -SourceRoot (Join-Path $script:Root 'apps\eMuleBB\config') -DestinationRoot $configDir
}
function Write-InstallManifest {
    param([hashtable]$Config, [hashtable]$ProfileImport, [hashtable]$Symbols)
    if ($DryRun) {
        return
    }
    $manifestDir = Join-Path $script:Root 'manifests'
    New-Item -ItemType Directory -Force -Path $manifestDir | Out-Null
    $serviceManifest = @{}
    foreach ($serviceName in @(Get-SuiteServiceNames -Config $Config)) {
        $service = $Config.services[$serviceName]
        $entry = @{ bindAddress = $service.bindAddress; clientHost = $service.clientHost; port = $service.port }
        if ($service.PSObject.Properties['apiKey'] -or ($service -is [System.Collections.IDictionary] -and $service.Contains('apiKey'))) {
            $entry.apiKeyPresent = -not [string]::IsNullOrWhiteSpace($service.apiKey)
        }
        $serviceManifest[$serviceName] = $entry
    }
    @{
        schema = 'emulebb.suite-install.v1'
        installedAtUtc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
        bundle = $Config.bundle
        selectedApps = @($Config.selectedApps)
        language = @{
            name = $Config.language.name
            mediaLanguageName = $Config.language.mediaLanguageName
            uiLanguageName = $Config.language.uiLanguageName
            emuleLanguageId = $Config.language.emuleLanguageId
            emuleLocale = $Config.language.emuleLocale
            arrUiLanguage = $Config.language.arrUiLanguage
            arrContentLanguage = $Config.language.arrContentLanguage
            arrContentLanguageMode = $Config.language.arrContentLanguageMode
        }
        portBlockStart = $Config.portBlockStart
        version = $Config.version
        platform = $Config.platform
        installKind = $Config.installKind
        installRoot = $script:Root
        emulebbPackageFlavor = $Config.emulebbPackageFlavor
        emulebbExecutableName = $Config.emulebbExecutableName
        profileImport = $ProfileImport
        symbols = $Symbols
        suiteScripts = @{
            zip = [string]$Config.suiteScripts.zip
            manifest = [string]$Config.suiteScripts.manifest
        }
        automationExamples = @{
            zip = [string]$Config.automationExamples.zip
            manifest = [string]$Config.automationExamples.manifest
            installed = -not [string]::IsNullOrWhiteSpace([string]$Config.automationExamples.zip)
        }
        optionalTools = @{
            install = [bool]$Config.optionalTools.install
            mpcHc = @{
                installed = [bool]$Config.optionalTools.mpcHc.installed
                path = [string]$Config.optionalTools.mpcHc.path
            }
            ffmpeg = @{
                installed = [bool]$Config.optionalTools.ffmpeg.installed
                path = [string]$Config.optionalTools.ffmpeg.path
            }
            mediainfo = @{
                installed = [bool]$Config.optionalTools.mediainfo.installed
                path = [string]$Config.optionalTools.mediainfo.path
            }
        }
        services = $serviceManifest
        p2p = @{
            bindInterfacePresent = -not [string]::IsNullOrWhiteSpace($Config.p2p.bindInterface)
            blockNetworkWhenBindUnavailableAtStartup = [bool]$Config.p2p.blockNetworkWhenBindUnavailableAtStartup
            networkGuardMode = [string]$Config.p2p.networkGuardMode
            networkGuardAllowedCIDRsPresent = -not [string]::IsNullOrWhiteSpace($Config.p2p.networkGuardAllowedCIDRs)
        }
    } | ConvertTo-Json -Depth 10 | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $manifestDir 'suite-install.json')
}

Initialize-SuiteAppMetadata
$script:SuiteConfig = Resolve-SuiteConfig
if (-not $NonInteractive -and (Test-InteractiveConsole)) {
    Invoke-InstallWizard -Config $script:SuiteConfig
}
$rawInstallRoot = [string]$script:SuiteConfig.installRoot
Assert-InstallRootValue -Path $rawInstallRoot
try {
    $script:Root = [IO.Path]::GetFullPath($rawInstallRoot)
} catch {
    throw "InstallRoot is not a valid Windows path: $rawInstallRoot. Choose a short absolute folder such as C:\eMuleBBSuite or C:\eMuleBB."
}
$script:SuiteConfig.installRoot = $script:Root
Assert-NoSpaces -Path $script:Root
Resolve-ServicePorts -Config $script:SuiteConfig
Assert-SuiteConfig -Config $script:SuiteConfig
Write-ConfigSummary -Config $script:SuiteConfig

if ((Test-Path -LiteralPath $script:Root) -and -not $Force -and -not $DryRun) {
    throw "InstallRoot already exists: $script:Root. Choose a different -InstallRoot, or rerun with -Force to refresh the suite files in this folder. Back up the folder first if you manually edited configuration."
}
if (-not $DryRun) {
    New-Item -ItemType Directory -Force -Path $script:Root | Out-Null
}

$script:SuiteConfig.services.emulebb.apiKey = Resolve-ApiKey -Value $script:SuiteConfig.services.emulebb.apiKey -Name 'eMuleBB API key'
foreach ($name in @(Get-SelectedArrAppNames -Config $script:SuiteConfig)) {
    $script:SuiteConfig.services[$name].apiKey = Resolve-ApiKey -Value $script:SuiteConfig.services[$name].apiKey -Name "$((Get-ServiceDisplayName -ServiceName $name)) API key"
}
$script:SuiteConfig.credentials.password = Resolve-Secret -Value $script:SuiteConfig.credentials.password -Name 'Suite password'

$releaseBase = Resolve-OptionalValue -Value $script:SuiteConfig.releaseBaseUrl -Default "https://github.com/emulebb/emulebb/releases/download/emulebb-v$($script:SuiteConfig.version)"
$dependencyManifestPayload = Load-DependencyManifestPayload -ManifestPath $script:SuiteConfig.dependencyManifest
$assetArch = if ($script:SuiteConfig.platform -eq 'ARM64') { 'arm64' } else { 'x64' }
$emulebbAssetSuffix = if ($script:SuiteConfig.emulebbPackageFlavor -eq 'diagnostics') { '-diagnostics' } else { '' }
$appRoot = Join-Path $script:Root 'apps'
$emulebbPackage = Save-PackageZip -Name 'eMuleBB' -ZipUrl "$releaseBase/emulebb-$($script:SuiteConfig.version)$emulebbAssetSuffix-$assetArch.zip" -ManifestUrl "$releaseBase/emulebb-$($script:SuiteConfig.version)$emulebbAssetSuffix-$assetArch.manifest.json" -LocalZip ([string]$script:SuiteConfig.packageSources.emulebb.zip) -LocalManifest ([string]$script:SuiteConfig.packageSources.emulebb.manifest)
Install-VerifiedReleaseZip -Name 'eMuleBB' -ArchivePath $emulebbPackage.ArchivePath -Destination $appRoot
Assert-EmulebbExecutableInstalled -Config $script:SuiteConfig

if (@(Get-SelectedArrAppNames -Config $script:SuiteConfig).Count -gt 0) {
    $selectedArrDependencies = @(Get-SelectedArrAppNames -Config $script:SuiteConfig)
    $dependencies = Load-DependencyManifest -Payload $dependencyManifestPayload -Channel $script:SuiteConfig.dependencyChannel -Names $selectedArrDependencies
    foreach ($name in $selectedArrDependencies) {
        Install-ArrDependency -Name $name -Spec $dependencies[$name] -Channel $script:SuiteConfig.dependencyChannel
    }
}

foreach ($name in @(Get-SelectedP2pClientNames -Config $script:SuiteConfig)) {
    Install-P2pClient -Name $name -Meta $script:P2pClientMetadata[$name]
}

Install-OptionalMediaTools -Config $script:SuiteConfig -DependencyManifestPayload $dependencyManifestPayload

$script:ProfileImport = Write-EmuleProfile -Config $script:SuiteConfig
$script:Symbols = Copy-OptionalEmuleSymbols -Config $script:SuiteConfig
if (@(Get-SelectedArrAppNames -Config $script:SuiteConfig).Count -gt 0) {
    foreach ($name in @(Get-SelectedArrAppNames -Config $script:SuiteConfig)) {
        Write-ArrConfig -Name $name -Port $script:SuiteConfig.services[$name].port -BindAddress $script:SuiteConfig.services[$name].bindAddress -ApiKey $script:SuiteConfig.services[$name].apiKey -Username $script:SuiteConfig.credentials.username -Password $script:SuiteConfig.credentials.password -Language ([string]$script:SuiteConfig.language.arrUiLanguage)
    }
}
Write-SuiteConfigFile -Config $script:SuiteConfig
Write-CredentialsFile -Config $script:SuiteConfig
Write-SuiteScripts -Config $script:SuiteConfig
Write-AutomationExamples -Config $script:SuiteConfig
Write-InstallManifest -Config $script:SuiteConfig -ProfileImport $script:ProfileImport -Symbols $script:Symbols

if (-not $KeepDownloads -and -not $DryRun) {
    Remove-Item -Recurse -Force -LiteralPath (Join-Path $script:Root 'downloads-cache') -ErrorAction SilentlyContinue
}
if (-not $NoStart -and -not $DryRun) {
    if (-not $NonInteractive -and (Test-InteractiveConsole)) {
        Write-Host ''
        Write-Host 'Press Enter to start the suite and complete its setup..' -NoNewline
        [void][Console]::ReadLine()
    }
    & (Join-Path $script:Root 'scripts\Initialize-Suite.ps1')
} elseif ($NoStart -and -not $DryRun) {
    Write-Step "Start skipped because -NoStart was used. Run $(Join-Path $script:Root 'scripts\Initialize-Suite.ps1') once to start services, register integrations, and create selected Arr root folders."
}
Write-Step "Installed $($script:SuiteConfig.bundle) bundle at $script:Root"
if (-not $DryRun -and -not $NonInteractive) {
    Write-Host 'The HTML install summary will open now. It contains suite URLs, credentials, and API keys.' -ForegroundColor Cyan
    Write-Host 'Continuing in 6 seconds...'
    Start-Sleep -Seconds 6
    Start-Process -FilePath (Join-Path $script:Root 'credentials.html') | Out-Null
}
