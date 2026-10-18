# Plant and remove the laptop-probe allow rule without touching the rest
# of the owner's settings.
#
# Each mode recomputes paths from $env:USERPROFILE. A new window can run
# -Cleanup on its own. -Backup is one-time and timestamped: it never
# overwrites an existing *.taskbench-backup-* file, and it refuses to copy
# a file that already contains TASKBENCH-PROBE.
#
# -Cleanup restores a live file only when the probe marker or the exact
# rule Bash(echo TASKBENCH-PROBE*) is still in that file. It then renames
# the backup to *.used so a later cleanup cannot copy it again. If the
# marker and the rule are absent, cleanup does nothing to that file and
# says so, even when a backup exists.
#
# TOML is removed only between the comment lines
# "# BEGIN TASKBENCH-PROBE" and "# END TASKBENCH-PROBE". JSON has no
# comments, so cleanup parses with ConvertFrom-Json and removes the exact
# rule string from permissions.allow. An allow array or permissions object
# that this script added is dropped when it is empty afterwards. The file
# is deleted only when a sidecar "<file>.taskbench-created" exists and the
# file contains nothing but the probe rule. That sidecar is written only
# when the live file did not exist before -Plant.
#
# Probe text is the marker comments or the exact rule string. This script
# does not strip whitespace and then compare. The rule contains a space
# ("echo TASKBENCH"), and a stripped compare misses a file that is only
# the planted rule.
#
# An existing [permission] table is reused. The allow entry is inserted
# inside it, between the marker comments. A second [permission] table is
# not written. If allow is already present, planting stops. The result is
# checked with python tomllib when that module imports. Otherwise the
# check is one [permission] table and a matched pair of marker comments.
#
# -DryRun prints the actions and does not write.
#
# PowerShell 5.1 Set-Content -Encoding utf8 writes a BOM. This script uses
# UTF-8 without a BOM.

#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch] $Backup,
    [switch] $Plant,
    [switch] $Cleanup,
    [ValidateSet("grok", "claude")]
    [string] $Which = "grok",
    [switch] $DryRun
)

$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}
$script:ProbeMarker = "TASKBENCH-PROBE"
$script:ProbeRule = 'Bash(echo TASKBENCH-PROBE*)'
$script:BeginMark = "# BEGIN TASKBENCH-PROBE"
$script:EndMark = "# END TASKBENCH-PROBE"

function Get-ProbePaths {
    $root = $env:USERPROFILE
    if ([string]::IsNullOrWhiteSpace($root)) {
        throw "USERPROFILE is not set"
    }
    return @{
        Root = $root
        GrokToml = (Join-Path (Join-Path $root ".grok") "config.toml")
        ClaudeJson = (Join-Path (Join-Path $root ".claude") "settings.json")
    }
}

function Write-Utf8NoBom {
    param([string] $Path, [string] $Text)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Get-ProbeBackups {
    param([string] $Path)
    $directory = Split-Path -Parent $Path
    $leaf = Split-Path -Leaf $Path
    if (-not (Test-Path -LiteralPath $directory)) {
        return @()
    }
    $prefix = "$leaf.taskbench-backup-"
    return @(
        Get-ChildItem -LiteralPath $directory -Force -File |
            Where-Object { $_.Name.StartsWith($prefix) -and -not $_.Name.EndsWith(".used") } |
            Sort-Object -Property Name
    )
}

function Get-CreatedMarkerPath {
    param([string] $Path)
    return "$Path.taskbench-created"
}

function Get-Newline {
    param([string] $Text)
    if ($Text.Contains("`r`n")) {
        return "`r`n"
    }
    return "`n"
}

function ConvertTo-JsonString {
    param([string] $Text)
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    foreach ($char in $Text.ToCharArray()) {
        $code = [int]$char
        if ($code -eq 34) {
            [void]$builder.Append('\"')
        } elseif ($code -eq 92) {
            [void]$builder.Append('\\')
        } elseif ($code -eq 8) {
            [void]$builder.Append('\b')
        } elseif ($code -eq 9) {
            [void]$builder.Append('\t')
        } elseif ($code -eq 10) {
            [void]$builder.Append('\n')
        } elseif ($code -eq 12) {
            [void]$builder.Append('\f')
        } elseif ($code -eq 13) {
            [void]$builder.Append('\r')
        } elseif ($code -lt 32) {
            [void]$builder.Append(('\\u{0:x4}' -f $code))
        } else {
            [void]$builder.Append($char)
        }
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Write-JsonValue {
    # [object[]] plus a unary comma keeps a one-element array intact.
    # An untyped parameter would unwrap it and turn ["rule"] into a string.
    param([object[]]$Wrapped)
    if ($null -eq $Wrapped -or $Wrapped.Count -eq 0) {
        return "null"
    }
    $InputObject = $Wrapped[0]
    if ($null -eq $InputObject) {
        return "null"
    }
    if ($InputObject -is [string]) {
        return (ConvertTo-JsonString -Text $InputObject)
    }
    if ($InputObject -is [bool]) {
        if ($InputObject) { return "true" }
        return "false"
    }
    if ($InputObject -is [System.Management.Automation.PSCustomObject]) {
        $parts = New-Object System.Collections.Generic.List[string]
        foreach ($prop in $InputObject.PSObject.Properties) {
            $name = ConvertTo-JsonString -Text ([string]$prop.Name)
            $child = Write-JsonValue -Wrapped (, $prop.Value)
            $parts.Add("${name}:${child}")
        }
        return "{" + ($parts -join ",") + "}"
    }
    if ($InputObject -is [System.Collections.IDictionary]) {
        $parts = New-Object System.Collections.Generic.List[string]
        foreach ($key in $InputObject.Keys) {
            $name = ConvertTo-JsonString -Text ([string]$key)
            $child = Write-JsonValue -Wrapped (, $InputObject[$key])
            $parts.Add("${name}:${child}")
        }
        return "{" + ($parts -join ",") + "}"
    }
    $isList = ($InputObject -is [System.Array]) -or ($InputObject -is [System.Collections.IList])
    if ($isList) {
        $parts = New-Object System.Collections.Generic.List[string]
        foreach ($item in $InputObject) {
            $parts.Add((Write-JsonValue -Wrapped (, $item)))
        }
        return "[" + ($parts -join ",") + "]"
    }
    if ($InputObject -is [ValueType]) {
        return [System.Convert]::ToString($InputObject, [System.Globalization.CultureInfo]::InvariantCulture)
    }
    throw "cannot write JSON for $($InputObject.GetType().FullName)"
}

function Read-JsonObject {
    param([string] $Text, [string] $Path)
    try {
        $parsed = $Text | ConvertFrom-Json
    } catch {
        throw "could not parse $Path as JSON"
    }
    if ($parsed -isnot [System.Management.Automation.PSCustomObject]) {
        throw "refusing to change $Path because the JSON root is not an object"
    }
    return $parsed
}

function Get-AllowItems {
    param($Parsed)
    $permProp = $Parsed.PSObject.Properties['permissions']
    if ($null -eq $permProp -or $null -eq $permProp.Value) {
        return @()
    }
    if ($permProp.Value -isnot [System.Management.Automation.PSCustomObject]) {
        return @()
    }
    $allowProp = $permProp.Value.PSObject.Properties['allow']
    if ($null -eq $allowProp) {
        return @()
    }
    return @($allowProp.Value)
}

function Test-JsonHasProbeRule {
    param($Parsed)
    foreach ($item in @(Get-AllowItems -Parsed $Parsed)) {
        if (($item -is [string]) -and ($item -ceq $script:ProbeRule)) {
            return $true
        }
    }
    return $false
}

function Test-JsonOnlyProbe {
    param($Parsed)
    $props = @($Parsed.PSObject.Properties)
    if ($props.Count -ne 1 -or $props[0].Name -cne "permissions") {
        return $false
    }
    $perm = $props[0].Value
    if ($null -eq $perm -or $perm -isnot [System.Management.Automation.PSCustomObject]) {
        return $false
    }
    $permProps = @($perm.PSObject.Properties)
    if ($permProps.Count -ne 1 -or $permProps[0].Name -cne "allow") {
        return $false
    }
    $items = @($permProps[0].Value)
    if ($items.Count -ne 1) {
        return $false
    }
    return ($items[0] -is [string]) -and ($items[0] -ceq $script:ProbeRule)
}

function Remove-JsonProbeRule {
    param($Parsed)
    $permProp = $Parsed.PSObject.Properties['permissions']
    if ($null -eq $permProp -or $null -eq $permProp.Value) {
        return $false
    }
    $perm = $permProp.Value
    if ($perm -isnot [System.Management.Automation.PSCustomObject]) {
        return $false
    }
    $allowProp = $perm.PSObject.Properties['allow']
    if ($null -eq $allowProp) {
        return $false
    }
    $kept = New-Object System.Collections.Generic.List[object]
    $removed = $false
    foreach ($item in @($allowProp.Value)) {
        if (($item -is [string]) -and ($item -ceq $script:ProbeRule)) {
            $removed = $true
        } else {
            $kept.Add($item)
        }
    }
    if (-not $removed) {
        return $false
    }
    if ($kept.Count -eq 0) {
        $perm.PSObject.Properties.Remove('allow')
    } else {
        $allowProp.Value = $kept
    }
    if (@($perm.PSObject.Properties).Count -eq 0) {
        $Parsed.PSObject.Properties.Remove('permissions')
    }
    return $true
}

function Remove-MarkerBlock {
    param([string] $Text)
    $pattern = '(?s)# BEGIN TASKBENCH-PROBE\r*\n.*?# END TASKBENCH-PROBE\r*\n?'
    return [regex]::Replace($Text, $pattern, "")
}

function Test-TomlHasProbe {
    param([string] $Text)
    if ($Text.Contains($script:BeginMark) -or $Text.Contains($script:EndMark)) {
        return $true
    }
    return $Text.Contains($script:ProbeRule)
}

function Test-TomlOnlyProbe {
    param([string] $Text)
    if (-not ($Text.Contains($script:BeginMark) -and $Text.Contains($script:EndMark))) {
        return $false
    }
    $rest = Remove-MarkerBlock -Text $Text
    return [string]::IsNullOrWhiteSpace($rest)
}

function Get-ProbeEntry {
    param([string] $Newline, [bool] $IncludeTable)
    $line = 'allow = ["Bash(echo TASKBENCH-PROBE*)"]'
    if ($IncludeTable) {
        return $script:BeginMark + $Newline + "[permission]" + $Newline + $line + $Newline + $script:EndMark + $Newline
    }
    return $script:BeginMark + $Newline + $line + $Newline + $script:EndMark + $Newline
}

function Assert-PlantedToml {
    param([string] $Text)
    $py = $null
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) {
            $py = $cmd.Source
            break
        }
    }
    $importOk = $false
    if ($py) {
        & $py -c "import tomllib" | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $importOk = $true
        }
    }
    if (-not $importOk) {
        $tables = [regex]::Matches($Text, '(?m)^\[permission\]')
        if ($tables.Count -ne 1 -or -not $Text.Contains($script:BeginMark) -or -not $Text.Contains($script:EndMark)) {
            throw "planted TOML failed the documented check (one [permission] table and paired markers), and python tomllib is not available"
        }
        Write-Output "plant: python tomllib is not available; checked one [permission] table and paired markers"
        return
    }
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("taskbench-probe-" + [guid]::NewGuid().ToString() + ".toml")
    try {
        Write-Utf8NoBom -Path $tmp -Text $Text
        $code = "import pathlib,sys,tomllib; tomllib.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))"
        $output = & $py -c $code $tmp 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "planted TOML did not parse with tomllib: $output"
        }
    } finally {
        if (Test-Path -LiteralPath $tmp) {
            Remove-Item -LiteralPath $tmp
        }
    }
}

function Invoke-ProbeBackup {
    $paths = Get-ProbePaths
    foreach ($path in @($paths.GrokToml, $paths.ClaudeJson)) {
        if (-not (Test-Path -LiteralPath $path)) {
            Write-Output "backup: no file, skipped $path"
            continue
        }
        $existing = @(Get-ProbeBackups -Path $path)
        if ($existing.Count -gt 0) {
            Write-Output "backup: already exists $($existing[0].FullName); not overwritten"
            continue
        }
        $text = [System.IO.File]::ReadAllText($path)
        if ($text.Contains($script:ProbeMarker)) {
            throw "refusing to back up $path because it already contains $($script:ProbeMarker)"
        }
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $dest = "$path.taskbench-backup-$stamp"
        if (Test-Path -LiteralPath $dest) {
            throw "refusing to overwrite backup $dest"
        }
        if ($DryRun) {
            Write-Output "dry-run backup: would copy $path to $dest"
            continue
        }
        Copy-Item -LiteralPath $path -Destination $dest
        Write-Output "backup: wrote $dest"
    }
}

function Get-PlantPath {
    param($Paths)
    if ($Which -eq "claude") {
        return $Paths.ClaudeJson
    }
    return $Paths.GrokToml
}

function Invoke-ProbePlant {
    $paths = Get-ProbePaths
    $path = Get-PlantPath -Paths $paths
    $directory = Split-Path -Parent $path
    $exists = Test-Path -LiteralPath $path
    $text = ""
    if ($exists) {
        $text = [System.IO.File]::ReadAllText($path)
    }
    $blank = [string]::IsNullOrWhiteSpace($text) -or ($text.Trim() -eq "{}")
    if (-not $blank) {
        if ($text.Contains($script:ProbeMarker)) {
            throw "$($script:ProbeMarker) is already in $path; stop"
        }
        if (@(Get-ProbeBackups -Path $path).Count -eq 0) {
            throw "refusing to plant into $path with no backup; run -Backup first"
        }
        if ($Which -eq "grok" -and [regex]::IsMatch($text, '(?m)^\s*allow\s*=')) {
            throw "allow is already in the .grok file; stop"
        }
        if ($Which -eq "claude") {
            $existingJson = Read-JsonObject -Text $text -Path $path
            $permProp = $existingJson.PSObject.Properties['permissions']
            if ($permProp -and $permProp.Value -and $permProp.Value.PSObject.Properties['allow']) {
                throw "allow is already in the .claude file; stop"
            }
        }
    }
    $newline = "`n"
    if (-not $blank) {
        $newline = Get-Newline -Text $text
    }
    if ($Which -eq "grok") {
        $permissionCount = [regex]::Matches($text, '(?m)^\[permission\]').Count
        if ($permissionCount -gt 1) {
            throw "the .grok file already has more than one [permission] table; stop"
        }
        if ($blank) {
            $updated = Get-ProbeEntry -Newline $newline -IncludeTable $true
        } elseif ($permissionCount -eq 1) {
            $header = [regex]::Match($text, '(?m)^\[permission\][^\r\n]*')
            if (-not $header.Success) {
                throw "the .grok file has a [permission] table this script could not merge into; stop"
            }
            $at = $header.Index + $header.Length
            $prefix = $text.Substring(0, $at)
            $suffix = $text.Substring($at)
            if (-not $prefix.EndsWith("`n")) {
                $prefix = $prefix + $newline
            }
            $updated = $prefix + (Get-ProbeEntry -Newline $newline -IncludeTable $false) + $suffix
        } else {
            $block = Get-ProbeEntry -Newline $newline -IncludeTable $true
            if (-not $text.EndsWith("`n")) {
                $block = $newline + $block
            }
            $updated = $text + $block
        }
        Assert-PlantedToml -Text $updated
    } else {
        if ($blank) {
            $updated = '{"permissions":{"allow":["Bash(echo TASKBENCH-PROBE*)"]}}' + $newline
        } else {
            $parsed = Read-JsonObject -Text $text -Path $path
            $permProp = $parsed.PSObject.Properties['permissions']
            $allowList = New-Object System.Collections.Generic.List[string]
            $allowList.Add($script:ProbeRule)
            if ($null -eq $permProp) {
                $perm = New-Object System.Management.Automation.PSObject
                $perm | Add-Member -NotePropertyName "allow" -NotePropertyValue $allowList
                $parsed | Add-Member -NotePropertyName "permissions" -NotePropertyValue $perm
            } else {
                $permProp.Value | Add-Member -NotePropertyName "allow" -NotePropertyValue $allowList
            }
            $updated = (Write-JsonValue -Wrapped (, $parsed)) + $newline
            $null = Read-JsonObject -Text $updated -Path $path
        }
    }
    if (-not (Test-Path -LiteralPath $directory)) {
        if ($DryRun) {
            Write-Output "dry-run plant: would create directory $directory"
        } else {
            New-Item -ItemType Directory -Path $directory | Out-Null
            Write-Output "plant: created directory $directory"
        }
    }
    $createdPath = Get-CreatedMarkerPath -Path $path
    if ($DryRun) {
        Write-Output "dry-run plant: would write $Which rule into $path"
        if (-not $exists) {
            Write-Output "dry-run plant: would record $createdPath"
        }
        return
    }
    Write-Utf8NoBom -Path $path -Text $updated
    if (-not $exists) {
        Write-Utf8NoBom -Path $createdPath -Text "taskbench created this file`n"
        Write-Output "plant: recorded $createdPath"
    }
    Write-Output "plant: wrote $Which rule into $path"
}

function Remove-CreatedProbeFile {
    param([string] $Path, [string] $Reason)
    $created = Get-CreatedMarkerPath -Path $Path
    if ($DryRun) {
        Write-Output "dry-run cleanup: would delete $Path because $Reason and $created exists"
        return
    }
    [System.IO.File]::Delete($Path)
    if (Test-Path -LiteralPath $created) {
        [System.IO.File]::Delete($created)
    }
    Write-Output "cleanup: deleted $Path because $Reason"
}

function Invoke-CleanFile {
    param([string] $Path)
    $isToml = (Split-Path -Leaf $Path) -eq "config.toml"
    $backups = @(Get-ProbeBackups -Path $Path)
    $exists = Test-Path -LiteralPath $Path
    $text = ""
    if ($exists) {
        $text = [System.IO.File]::ReadAllText($Path)
    }
    $created = Get-CreatedMarkerPath -Path $Path
    $hasCreated = Test-Path -LiteralPath $created
    $present = $false
    $parsed = $null
    $jsonUnparsed = $false
    if ($exists -and -not [string]::IsNullOrWhiteSpace($text)) {
        if ($isToml) {
            $present = Test-TomlHasProbe -Text $text
        } else {
            try {
                $parsed = $text | ConvertFrom-Json
            } catch {
                $parsed = $null
                $jsonUnparsed = $true
            }
            if ($parsed -is [System.Management.Automation.PSCustomObject]) {
                $present = Test-JsonHasProbeRule -Parsed $parsed
            } elseif ($text.Contains($script:ProbeRule)) {
                $jsonUnparsed = $true
                $present = $true
            }
        }
    }
    if ($backups.Count -gt 0) {
        if (-not $present) {
            Write-Output "cleanup: no probe marker in $Path; left the file and the backup unused"
            return
        }
        $backup = $backups[0].FullName
        $used = "$backup.used"
        if (Test-Path -LiteralPath $used) {
            throw "refusing to overwrite retired backup $used"
        }
        if ($DryRun) {
            Write-Output "dry-run cleanup: would restore $Path from $backup and rename it to $used"
            return
        }
        [System.IO.File]::Copy($backup, $Path, $true)
        [System.IO.File]::Move($backup, $used)
        Write-Output "cleanup: restored $Path from $backup and renamed the backup to $used"
        return
    }
    if ($jsonUnparsed -and $text.Contains($script:ProbeRule)) {
        Write-Output "cleanup: could not parse $Path; left the file untouched"
        return
    }
    if (-not $exists) {
        Write-Output "cleanup: no file $Path"
        return
    }
    if ($isToml -and $text.Contains($script:BeginMark) -and -not $text.Contains($script:EndMark)) {
        Write-Output "cleanup: unclosed marker in $Path; left the file untouched"
        return
    }
    if ($isToml) {
        if ($hasCreated -and (Test-TomlOnlyProbe -Text $text)) {
            Remove-CreatedProbeFile -Path $Path -Reason "it contained only the probe rule"
            return
        }
        $stripped = Remove-MarkerBlock -Text $text
        if ($stripped -eq $text) {
            Write-Output "cleanup: no probe marker in $Path; left the file unchanged"
            return
        }
        if ($DryRun) {
            Write-Output "dry-run cleanup: would remove planted lines from $Path"
            return
        }
        Write-Utf8NoBom -Path $Path -Text $stripped
        Write-Output "cleanup: removed planted lines from $Path"
        return
    }
    if ($null -eq $parsed -or $parsed -isnot [System.Management.Automation.PSCustomObject]) {
        if ($text.Contains($script:ProbeRule) -or $text.Contains($script:BeginMark)) {
            Write-Output "cleanup: could not parse $Path; left the file untouched"
            return
        }
        Write-Output "cleanup: no probe marker in $Path; left the file unchanged"
        return
    }
    if ($hasCreated -and (Test-JsonOnlyProbe -Parsed $parsed)) {
        Remove-CreatedProbeFile -Path $Path -Reason "it contained only the probe rule"
        return
    }
    $removed = Remove-JsonProbeRule -Parsed $parsed
    if (-not $removed) {
        Write-Output "cleanup: no probe marker in $Path; left the file unchanged"
        return
    }
    if ($DryRun) {
        Write-Output "dry-run cleanup: would remove the probe rule from $Path"
        return
    }
    $rewritten = (Write-JsonValue -Wrapped (, $parsed)) + (Get-Newline -Text $text)
    Write-Utf8NoBom -Path $Path -Text $rewritten
    Write-Output "cleanup: removed the probe rule from $Path"
}

function Remove-ProbeFolder {
    param([string] $Path)
    $leaf = Split-Path -Leaf $Path
    if ($leaf -ne "taskbench-probe") {
        throw "refusing to remove $Path"
    }
    if ($Path -eq $env:USERPROFILE) {
        throw "refusing to remove the profile"
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Output "cleanup: no probe folder $Path"
        return
    }
    if ($DryRun) {
        Write-Output "dry-run cleanup: would remove folder $Path"
        return
    }
    Remove-Item -LiteralPath $Path -Recurse -Force
    Write-Output "cleanup: removed folder $Path"
}

function Invoke-ProbeCleanup {
    $paths = Get-ProbePaths
    Invoke-CleanFile -Path $paths.GrokToml
    Invoke-CleanFile -Path $paths.ClaudeJson
    Remove-ProbeFolder -Path "C:\taskbench-probe"
    Remove-ProbeFolder -Path (Join-Path $paths.Root "taskbench-probe")
}

$modeCount = 0
if ($Backup) { $modeCount += 1 }
if ($Plant) { $modeCount += 1 }
if ($Cleanup) { $modeCount += 1 }
if ($modeCount -ne 1) {
    throw "Pass exactly one of -Backup, -Plant, or -Cleanup"
}
if ($Backup) {
    Invoke-ProbeBackup
} elseif ($Plant) {
    Invoke-ProbePlant
} else {
    Invoke-ProbeCleanup
}
