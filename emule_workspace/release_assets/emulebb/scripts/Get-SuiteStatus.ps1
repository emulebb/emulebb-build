#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $Root 'manifests\suite-config.json'
if (-not (Test-Path -LiteralPath $configPath)) {
    throw "Suite config is missing: $configPath. Re-run scripts\Install-eMuleBBSuite.ps1 with -Force to rebuild it."
}
$config = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
Write-Host "Suite root: $Root"
Write-Host "Bundle: $($config.bundle)"
Write-Host "Apps: $((@($config.selectedApps) -join ', '))"
Write-Host "Language: $($config.language.name)"
foreach ($name in @($config.selectedApps)) {
    $service = $config.services.$name
    Write-Host ("{0}: {1}:{2}" -f $name, $service.bindAddress, $service.port)
}

$executableNames = @{
    emulebb = if ([string]::IsNullOrWhiteSpace([string]$config.emulebbExecutableName)) { 'emulebb.exe' } else { [string]$config.emulebbExecutableName }
    prowlarr = 'Prowlarr.exe'
    radarr = 'Radarr.exe'
    sonarr = 'Sonarr.exe'
    lidarr = 'Lidarr.exe'
    whisparr = 'Whisparr.exe'
}
$expectedExecutables = @()
foreach ($name in @($config.selectedApps)) {
    if ($executableNames.ContainsKey($name)) {
        $match = Get-ChildItem -Path (Join-Path $Root 'apps') -Filter $executableNames[$name] -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($match) {
            $expectedExecutables += $match.FullName
        }
    }
}
$processes = @(Get-CimInstance Win32_Process | Where-Object {
    $path = [string]$_.ExecutablePath
    -not [string]::IsNullOrWhiteSpace($path) -and ($expectedExecutables | Where-Object { [string]::Equals($_, $path, [StringComparison]::OrdinalIgnoreCase) })
} | Select-Object ProcessId, Name, ExecutablePath, CommandLine)
if ($processes.Count -eq 0) {
    Write-Host 'No eMuleBB Suite processes are running.'
} else {
    $processes
}
