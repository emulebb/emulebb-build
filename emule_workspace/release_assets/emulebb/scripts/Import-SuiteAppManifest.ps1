#Requires -Version 5.1

function ConvertTo-SuiteManifestStringArray {
    param([object]$Value)
    $items = @()
    foreach ($item in @($Value)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$item)) {
            $items += [string]$item
        }
    }
    return $items
}

function Get-SuiteManifestProperty {
    param([object]$Value, [string]$Name)
    if ($null -eq $Value) {
        return ''
    }
    if ($Value -is [System.Collections.IDictionary]) {
        foreach ($key in $Value.Keys) {
            if ([string]::Equals([string]$key, $Name, [StringComparison]::OrdinalIgnoreCase)) {
                return [string]$Value[$key]
            }
        }
        return ''
    }
    if ($null -eq $Value.PSObject.Properties[$Name]) {
        return ''
    }
    return [string]$Value.PSObject.Properties[$Name].Value
}

function ConvertTo-SuiteDependencySpec {
    param([object]$Dependency)
    if ($null -eq $Dependency) {
        return $null
    }
    return @{
        Repo = Get-SuiteManifestProperty -Value $Dependency -Name 'repo'
        Tag = Get-SuiteManifestProperty -Value $Dependency -Name 'tag'
        Pattern = Get-SuiteManifestProperty -Value $Dependency -Name 'assetPattern'
        Exe = Get-SuiteManifestProperty -Value $Dependency -Name 'exe'
        FileName = Get-SuiteManifestProperty -Value $Dependency -Name 'fileName'
        Url = Get-SuiteManifestProperty -Value $Dependency -Name 'url'
        Sha256 = Get-SuiteManifestProperty -Value $Dependency -Name 'sha256'
    }
}

function ConvertTo-SuiteAppManifest {
    param([object]$Payload, [string]$Path = '')
    if ($null -eq $Payload) {
        throw "suite app manifest is empty: $Path"
    }
    if ($null -eq $Payload.PSObject.Properties['schema'] -or [string]$Payload.schema -ne 'emulebb.suite-apps.v1') {
        throw "suite app manifest has an unsupported schema: $Path"
    }
    if ($null -eq $Payload.PSObject.Properties['arrApps']) {
        throw "suite app manifest does not contain arrApps: $Path"
    }

    $arrNames = @()
    $arrMetadata = @{}
    $dependencyPins = @{}
    $indexerCategories = @{}
    foreach ($entry in @($Payload.arrApps)) {
        $keyProperty = $entry.PSObject.Properties['key']
        if ($null -eq $keyProperty) {
            continue
        }
        $key = ([string]$keyProperty.Value).ToLowerInvariant()
        if ([string]::IsNullOrWhiteSpace($key)) {
            continue
        }

        $arrNames += $key
        $arrMetadata[$key] = @{
            DisplayName = [string]$entry.displayName
            Exe = [string]$entry.exe
            ApiPath = [string]$entry.apiPath
            DataDir = [string]$entry.dataDir
            MediaRoot = [string]$entry.mediaRoot
        }

        $dependencyProperty = $entry.PSObject.Properties['dependency']
        if ($null -ne $dependencyProperty -and $null -ne $dependencyProperty.Value) {
            $dependencyPins[$key] = ConvertTo-SuiteDependencySpec -Dependency $dependencyProperty.Value
        }

        $categoryProperty = $entry.PSObject.Properties['indexerCategories']
        if ($null -ne $categoryProperty -and $null -ne $categoryProperty.Value) {
            $categories = @()
            foreach ($category in @($categoryProperty.Value)) {
                if (-not [string]::IsNullOrWhiteSpace([string]$category)) {
                    $categories += [int]$category
                }
            }
            if ($categories.Count -gt 0) {
                $indexerCategories[$key] = $categories
            }
        }
    }
    if ($arrNames.Count -eq 0) {
        throw "suite app manifest does not define any Arr apps: $Path"
    }

    $mediaTools = @{}
    if ($null -ne $Payload.PSObject.Properties['mediaTools']) {
        foreach ($entry in @($Payload.mediaTools)) {
            $keyProperty = $entry.PSObject.Properties['key']
            if ($null -eq $keyProperty) {
                continue
            }
            $key = ([string]$keyProperty.Value).ToLowerInvariant()
            if ([string]::IsNullOrWhiteSpace($key)) {
                continue
            }
            $mediaTools[$key] = @{
                DisplayName = [string]$entry.displayName
                Destination = [string]$entry.destination
                Executable = [string]$entry.executable
                Dependency = if ($null -ne $entry.PSObject.Properties['dependency']) { ConvertTo-SuiteDependencySpec -Dependency $entry.dependency } else { $null }
            }
        }
    }

    $p2pClientNames = @()
    $p2pClients = @{}
    if ($null -ne $Payload.PSObject.Properties['p2pClients']) {
        foreach ($entry in @($Payload.p2pClients)) {
            $keyProperty = $entry.PSObject.Properties['key']
            if ($null -eq $keyProperty) {
                continue
            }
            $key = ([string]$keyProperty.Value).ToLowerInvariant()
            if ([string]::IsNullOrWhiteSpace($key)) {
                continue
            }
            $p2pClientNames += $key
            $p2pClients[$key] = @{
                DisplayName = [string]$entry.displayName
                Exe = [string]$entry.exe
                Destination = [string]$entry.destination
                ConfigRelativePath = if ($null -ne $entry.PSObject.Properties['configRelativePath']) { [string]$entry.configRelativePath } else { '' }
                Dependency = if ($null -ne $entry.PSObject.Properties['dependency']) { ConvertTo-SuiteDependencySpec -Dependency $entry.dependency } else { $null }
            }
        }
    }

    return [ordered]@{
        AllArrAppNames = $arrNames
        ArrAppMetadata = $arrMetadata
        AllP2pClientNames = $p2pClientNames
        P2pClients = $p2pClients
        PinnedDependencies = $dependencyPins
        ArrIndexerCategories = $indexerCategories
        MediaTools = $mediaTools
        CoreServiceNames = if ($null -ne $Payload.PSObject.Properties['coreServiceNames']) { ConvertTo-SuiteManifestStringArray -Value $Payload.coreServiceNames } else { @() }
        DefaultArrAppNames = if ($null -ne $Payload.PSObject.Properties['defaultArrAppNames']) { ConvertTo-SuiteManifestStringArray -Value $Payload.defaultArrAppNames } else { @() }
        SuiteServiceOrder = if ($null -ne $Payload.PSObject.Properties['suiteServiceOrder']) { ConvertTo-SuiteManifestStringArray -Value $Payload.suiteServiceOrder } else { @() }
    }
}

function Read-SuiteAppManifest {
    param([string]$Path)
    try {
        $payload = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
    } catch {
        throw "suite app manifest is not valid JSON: $Path. $($_.Exception.Message)"
    }
    return ConvertTo-SuiteAppManifest -Payload $payload -Path $Path
}
