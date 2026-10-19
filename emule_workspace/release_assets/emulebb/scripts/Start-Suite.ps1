#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

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

function Test-ProcessRunning {
    param([string]$ExecutablePath, [string]$CommandLineContains = '')
    try {
        foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
            if ([string]::IsNullOrWhiteSpace($process.ExecutablePath)) { continue }
            if (-not [string]::Equals($process.ExecutablePath, $ExecutablePath, [StringComparison]::OrdinalIgnoreCase)) { continue }
            if ([string]::IsNullOrWhiteSpace($CommandLineContains) -or ([string]$process.CommandLine).IndexOf($CommandLineContains, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                return $true
            }
        }
    } catch {
        return [bool](Get-Process | Where-Object { $_.Path -and [string]::Equals($_.Path, $ExecutablePath, [StringComparison]::OrdinalIgnoreCase) } | Select-Object -First 1)
    }
    return $false
}

function Start-ProcessIfMissing {
    param([string]$Name, [string]$FilePath, [string[]]$ArgumentList = @(), [string]$WorkingDirectory = '', [string]$CommandLineContains = '', [switch]$Hidden)
    if (-not (Test-Path -LiteralPath $FilePath)) {
        throw "$Name executable is missing: $FilePath"
    }
    if (Test-ProcessRunning -ExecutablePath $FilePath -CommandLineContains $CommandLineContains) {
        Write-Host "$Name is already running: $FilePath"
        return
    }
    $startArgs = @{ FilePath = $FilePath; ArgumentList = $ArgumentList; ErrorAction = 'Stop' }
    if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) { $startArgs.WorkingDirectory = $WorkingDirectory }
    if ($Hidden) { $startArgs.WindowStyle = 'Hidden' }
    Write-Host "Starting ${Name}: $FilePath"
    Start-Process @startArgs | Out-Null
    Start-Sleep -Seconds 2
    if (-not (Test-ProcessRunning -ExecutablePath $FilePath -CommandLineContains $CommandLineContains)) {
        throw "$Name did not stay running after launch from $FilePath."
    }
}

function Start-ArrHost {
    param([string]$Name)
    $appRoot = Join-Path $Root ('apps\' + $Name)
    $exe = Get-ChildItem -Path $appRoot -Filter ($Name + '.exe') -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $exe) {
        throw "Missing Windows tray host for $Name under $appRoot"
    }
    $dataDir = Join-Path $Root ('data\' + $Name.ToLowerInvariant())
    Start-ProcessIfMissing -Name $Name -FilePath $exe.FullName -ArgumentList @('/data=' + $dataDir, '/nobrowser') -CommandLineContains $dataDir
}

$config = Read-SuiteConfig
& (Join-Path $Root 'scripts\Start-eMuleBB.ps1')
foreach ($arrName in @('prowlarr', 'radarr', 'sonarr', 'lidarr', 'whisparr')) {
    if (Test-SelectedApp -Config $config -Name $arrName) {
        Start-ArrHost -Name ($arrName.Substring(0, 1).ToUpperInvariant() + $arrName.Substring(1))
    }
}
