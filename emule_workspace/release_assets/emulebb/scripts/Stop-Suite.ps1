#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

$configPath = Join-Path $Root 'manifests\suite-config.json'
$emuleExeName = 'emulebb.exe'
if (Test-Path -LiteralPath $configPath) {
    try {
        $config = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
        if (-not [string]::IsNullOrWhiteSpace([string]$config.emulebbExecutableName)) {
            $emuleExeName = [string]$config.emulebbExecutableName
        }
    } catch {
        Write-Warning "Could not read $configPath. Using default eMuleBB executable name. $($_.Exception.Message)"
    }
}

function Get-FirstSuiteExecutable {
    param([string]$RelativeRoot, [string]$FileName)
    $appRoot = Join-Path $Root $RelativeRoot
    $match = Get-ChildItem -Path $appRoot -Filter $FileName -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($match) { return $match.FullName }
    return Join-Path $appRoot $FileName
}

$serviceExecutables = @(
    (Join-Path (Join-Path $Root 'apps\eMuleBB') $emuleExeName),
    (Get-FirstSuiteExecutable -RelativeRoot 'apps\Prowlarr' -FileName 'Prowlarr.exe'),
    (Get-FirstSuiteExecutable -RelativeRoot 'apps\Radarr' -FileName 'Radarr.exe'),
    (Get-FirstSuiteExecutable -RelativeRoot 'apps\Sonarr' -FileName 'Sonarr.exe'),
    (Get-FirstSuiteExecutable -RelativeRoot 'apps\Lidarr' -FileName 'Lidarr.exe'),
    (Get-FirstSuiteExecutable -RelativeRoot 'apps\Whisparr' -FileName 'Whisparr.exe')
)

function Test-SuiteProcess {
    param($Process)
    $executablePath = [string]$Process.ExecutablePath
    $commandLine = [string]$Process.CommandLine
    if ([string]::IsNullOrWhiteSpace($executablePath)) {
        return $false
    }
    foreach ($serviceExecutable in $serviceExecutables) {
        if ([string]::Equals($executablePath, $serviceExecutable, [StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

$processes = @(Get-CimInstance Win32_Process | Where-Object { Test-SuiteProcess -Process $_ })
if ($processes.Count -eq 0) {
    Write-Host 'No eMuleBB Suite processes are running.'
    return
}
foreach ($process in $processes) {
    $label = if ([string]::IsNullOrWhiteSpace([string]$process.ExecutablePath)) { [string]$process.Name } else { [string]$process.ExecutablePath }
    Write-Host ("Stopping {0} (PID {1})" -f $label, $process.ProcessId)
    try {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
    } catch {
        Write-Warning "Could not stop PID $($process.ProcessId): $($_.Exception.Message)"
    }
}
Write-Host 'eMuleBB Suite stop request completed.'
