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
# comments. The exact rule string is spliced out of permissions.allow.
# Dates, numbers, key order, and nested values are not rewritten.
# PowerShell 7 is the primary host: System.Text.Json JsonDocument checks
# the edited JSON. The same splice runs on Windows PowerShell 5.1, which
# does not ship System.Text.Json. An allow array or permissions object
# that this script added is dropped when it is empty afterwards. The file
# is deleted only when a sidecar "<file>.taskbench-created" exists and the
# file contains nothing but the probe rule. If that sidecar is left over
# and the file has other content, cleanup removes the probe rule, deletes
# the sidecar, keeps the file, and says so. The sidecar is written only
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
# PowerShell 7 is supported and recommended. Windows PowerShell 5.1 still
# runs this script. Set-Content -Encoding utf8 writes a BOM on 5.1, so this
# script uses UTF-8 without a BOM on both.

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

function Skip-JsonWhitespace {
    while ($script:JsonIndex -lt $script:JsonLength) {
        $char = $script:JsonText[$script:JsonIndex]
        if ($char -eq ' ' -or $char -eq "`t" -or $char -eq "`n" -or $char -eq "`r") {
            $script:JsonIndex += 1
        } else {
            break
        }
    }
}

function Peek-JsonChar {
    if ($script:JsonIndex -ge $script:JsonLength) {
        return [char]0
    }
    return $script:JsonText[$script:JsonIndex]
}

function Read-JsonString {
    $start = $script:JsonIndex
    $script:JsonIndex += 1
    $builder = New-Object System.Text.StringBuilder
    while ($script:JsonIndex -lt $script:JsonLength) {
        $char = $script:JsonText[$script:JsonIndex]
        if ($char -eq '"') {
            $script:JsonIndex += 1
            return @{ Kind = "string"; Start = $start; End = $script:JsonIndex; Value = $builder.ToString() }
        }
        if ($char -eq '\') {
            $script:JsonIndex += 1
            if ($script:JsonIndex -ge $script:JsonLength) { throw "bad JSON escape" }
            $escaped = $script:JsonText[$script:JsonIndex]
            $script:JsonIndex += 1
            if ($escaped -eq '"') { [void]$builder.Append('"') }
            elseif ($escaped -eq '\') { [void]$builder.Append('\') }
            elseif ($escaped -eq '/') { [void]$builder.Append('/') }
            elseif ($escaped -eq 'b') { [void]$builder.Append([char]8) }
            elseif ($escaped -eq 'f') { [void]$builder.Append([char]12) }
            elseif ($escaped -eq 'n') { [void]$builder.Append("`n") }
            elseif ($escaped -eq 'r') { [void]$builder.Append("`r") }
            elseif ($escaped -eq 't') { [void]$builder.Append("`t") }
            elseif ($escaped -eq 'u') {
                if (($script:JsonIndex + 4) -gt $script:JsonLength) { throw "bad JSON unicode escape" }
                $hex = $script:JsonText.Substring($script:JsonIndex, 4)
                $script:JsonIndex += 4
                $codePoint = 0
                $parsedHex = [int]::TryParse(
                    $hex,
                    [System.Globalization.NumberStyles]::HexNumber,
                    [System.Globalization.CultureInfo]::InvariantCulture,
                    [ref]$codePoint
                )
                if (-not $parsedHex) { throw "bad JSON unicode escape" }
                [void]$builder.Append([char]$codePoint)
            }
            else { throw "bad JSON escape" }
        } else {
            [void]$builder.Append($char)
            $script:JsonIndex += 1
        }
    }
    throw "unterminated JSON string"
}

function Read-JsonLiteral {
    param([string] $Literal, [string] $Kind)
    if (-not $script:JsonText.Substring($script:JsonIndex).StartsWith($Literal)) {
        throw "expected $Literal"
    }
    $start = $script:JsonIndex
    $script:JsonIndex += $Literal.Length
    return @{ Kind = $Kind; Start = $start; End = $script:JsonIndex; Value = $Literal }
}

function Read-JsonNumber {
    $start = $script:JsonIndex
    if ((Peek-JsonChar) -eq '-') { $script:JsonIndex += 1 }
    while ($script:JsonIndex -lt $script:JsonLength) {
        $char = $script:JsonText[$script:JsonIndex]
        if ($char -eq ',' -or $char -eq ']' -or $char -eq '}' -or $char -eq ' ' -or $char -eq "`t" -or $char -eq "`n" -or $char -eq "`r") {
            break
        }
        $script:JsonIndex += 1
    }
    if ($script:JsonIndex -le $start -or ($script:JsonText[$start] -eq '-' -and $script:JsonIndex -eq ($start + 1))) {
        throw "bad JSON number"
    }
    return @{ Kind = "number"; Start = $start; End = $script:JsonIndex; Value = $script:JsonText.Substring($start, $script:JsonIndex - $start) }
}

function Read-JsonValue {
    Skip-JsonWhitespace
    if ($script:JsonIndex -ge $script:JsonLength) { throw "unexpected end of JSON" }
    $char = Peek-JsonChar
    if ($char -eq '{') { return Read-JsonObject }
    if ($char -eq '[') { return Read-JsonArray }
    if ($char -eq '"') { return Read-JsonString }
    if ($char -eq 't') { return Read-JsonLiteral -Literal "true" -Kind "bool" }
    if ($char -eq 'f') { return Read-JsonLiteral -Literal "false" -Kind "bool" }
    if ($char -eq 'n') { return Read-JsonLiteral -Literal "null" -Kind "null" }
    if ($char -eq '-' -or (($char -ge '0') -and ($char -le '9'))) { return Read-JsonNumber }
    throw "unexpected JSON at index $($script:JsonIndex)"
}

function Read-JsonObject {
    $start = $script:JsonIndex
    $script:JsonIndex += 1
    $properties = New-Object System.Collections.Generic.List[object]
    Skip-JsonWhitespace
    if ((Peek-JsonChar) -eq '}') {
        $script:JsonIndex += 1
        return @{ Kind = "object"; Start = $start; End = $script:JsonIndex; Properties = $properties }
    }
    while ($true) {
        Skip-JsonWhitespace
        if ((Peek-JsonChar) -ne '"') { throw "expected a JSON property name" }
        $name = Read-JsonString
        Skip-JsonWhitespace
        if ((Peek-JsonChar) -ne ':') { throw "expected a colon" }
        $script:JsonIndex += 1
        $value = Read-JsonValue
        $properties.Add(@{ Name = $name.Value; NameStart = $name.Start; NameEnd = $name.End; Value = $value })
        Skip-JsonWhitespace
        $char = Peek-JsonChar
        if ($char -eq ',') {
            $script:JsonIndex += 1
            continue
        }
        if ($char -eq '}') {
            $script:JsonIndex += 1
            break
        }
        throw "expected a comma or the end of an object"
    }
    return @{ Kind = "object"; Start = $start; End = $script:JsonIndex; Properties = $properties }
}

function Read-JsonArray {
    $start = $script:JsonIndex
    $script:JsonIndex += 1
    $elements = New-Object System.Collections.Generic.List[object]
    Skip-JsonWhitespace
    if ((Peek-JsonChar) -eq ']') {
        $script:JsonIndex += 1
        return @{ Kind = "array"; Start = $start; End = $script:JsonIndex; Elements = $elements }
    }
    while ($true) {
        $value = Read-JsonValue
        $elements.Add($value)
        Skip-JsonWhitespace
        $char = Peek-JsonChar
        if ($char -eq ',') {
            $script:JsonIndex += 1
            continue
        }
        if ($char -eq ']') {
            $script:JsonIndex += 1
            break
        }
        throw "expected a comma or the end of an array"
    }
    return @{ Kind = "array"; Start = $start; End = $script:JsonIndex; Elements = $elements }
}

function Parse-JsonRoot {
    param([string] $Text)
    $script:JsonText = $Text
    $script:JsonIndex = 0
    $script:JsonLength = $Text.Length
    $root = Read-JsonValue
    Skip-JsonWhitespace
    if ($script:JsonIndex -ne $script:JsonLength) { throw "trailing data after JSON" }
    if ($root.Kind -ne "object") { throw "JSON root is not an object" }
    return $root
}

function Find-PermissionsAllow {
    param($Root)
    for ($index = 0; $index -lt $Root.Properties.Count; $index++) {
        $property = $Root.Properties[$index]
        if ($property.Name -cne "permissions" -or $property.Value.Kind -ne "object") { continue }
        $children = $property.Value.Properties
        for ($childIndex = 0; $childIndex -lt $children.Count; $childIndex++) {
            if ($children[$childIndex].Name -ceq "allow") {
                return @{
                    Permissions = $property
                    Allow = $children[$childIndex]
                    AllowIndex = $childIndex
                }
            }
        }
    }
    return $null
}

function Test-JsonOnlyProbeNode {
    param($Root)
    if ($Root.Properties.Count -ne 1 -or $Root.Properties[0].Name -cne "permissions") { return $false }
    $permissions = $Root.Properties[0].Value
    if ($permissions.Kind -ne "object" -or $permissions.Properties.Count -ne 1) { return $false }
    $allow = $permissions.Properties[0]
    if ($allow.Name -cne "allow") { return $false }
    if ($allow.Value.Kind -eq "string") {
        return $allow.Value.Value -ceq $script:ProbeRule
    }
    if ($allow.Value.Kind -ne "array" -or $allow.Value.Elements.Count -ne 1) { return $false }
    $element = $allow.Value.Elements[0]
    return ($element.Kind -eq "string") -and ($element.Value -ceq $script:ProbeRule)
}

function Remove-ObjectProperty {
    param([string] $Text, $ObjectNode, [int] $PropIndex)
    $properties = $ObjectNode.Properties
    $property = $properties[$PropIndex]
    if ($properties.Count -eq 1) {
        return $Text.Substring(0, $ObjectNode.Start + 1) + $Text.Substring($ObjectNode.End - 1)
    }
    if ($PropIndex -gt 0) {
        $previous = $properties[$PropIndex - 1]
        return $Text.Substring(0, $previous.Value.End) + $Text.Substring($property.Value.End)
    }
    $next = $properties[$PropIndex + 1]
    return $Text.Substring(0, $property.NameStart) + $Text.Substring($next.NameStart)
}

function Remove-EmptyPermissions {
    param([string] $Text)
    $root = Parse-JsonRoot -Text $Text
    for ($index = 0; $index -lt $root.Properties.Count; $index++) {
        $property = $root.Properties[$index]
        if ($property.Name -ceq "permissions" -and $property.Value.Kind -eq "object" -and $property.Value.Properties.Count -eq 0) {
            return Remove-ObjectProperty -Text $Text -ObjectNode $root -PropIndex $index
        }
    }
    return $Text
}

function Update-JsonProbeText {
    param([string] $Text)
    $root = Parse-JsonRoot -Text $Text
    $found = Find-PermissionsAllow -Root $root
    if ($null -eq $found) {
        return @{ HasProbe = $false; OnlyProbe = $false; Changed = $false; Text = $Text }
    }
    $onlyProbe = Test-JsonOnlyProbeNode -Root $root
    $allowValue = $found.Allow.Value
    if ($allowValue.Kind -eq "string") {
        if ($allowValue.Value -cne $script:ProbeRule) {
            return @{ HasProbe = $false; OnlyProbe = $false; Changed = $false; Text = $Text }
        }
        $updated = Remove-ObjectProperty -Text $Text -ObjectNode $found.Permissions.Value -PropIndex $found.AllowIndex
        $updated = Remove-EmptyPermissions -Text $updated
        return @{ HasProbe = $true; OnlyProbe = $onlyProbe; Changed = $true; Text = $updated }
    }
    if ($allowValue.Kind -ne "array") {
        return @{ HasProbe = $false; OnlyProbe = $false; Changed = $false; Text = $Text }
    }
    $elements = $allowValue.Elements
    $kept = New-Object System.Collections.Generic.List[int]
    $hasProbe = $false
    for ($index = 0; $index -lt $elements.Count; $index++) {
        $element = $elements[$index]
        if ($element.Kind -eq "string" -and $element.Value -ceq $script:ProbeRule) {
            $hasProbe = $true
        } else {
            $kept.Add($index)
        }
    }
    if (-not $hasProbe) {
        return @{ HasProbe = $false; OnlyProbe = $false; Changed = $false; Text = $Text }
    }
    if ($kept.Count -eq 0) {
        $updated = Remove-ObjectProperty -Text $Text -ObjectNode $found.Permissions.Value -PropIndex $found.AllowIndex
        $updated = Remove-EmptyPermissions -Text $updated
        return @{ HasProbe = $true; OnlyProbe = $onlyProbe; Changed = $true; Text = $updated }
    }
    $builder = New-Object System.Text.StringBuilder
    $firstKept = $elements[$kept[0]]
    [void]$builder.Append($Text.Substring($firstKept.Start, $firstKept.End - $firstKept.Start))
    for ($keptIndex = 1; $keptIndex -lt $kept.Count; $keptIndex++) {
        $currentIndex = $kept[$keptIndex]
        $current = $elements[$currentIndex]
        $before = $elements[$currentIndex - 1]
        [void]$builder.Append($Text.Substring($before.End, $current.Start - $before.End))
        [void]$builder.Append($Text.Substring($current.Start, $current.End - $current.Start))
    }
    $spanStart = $elements[0].Start
    $spanEnd = $elements[$elements.Count - 1].End
    $updated = $Text.Substring(0, $spanStart) + $builder.ToString() + $Text.Substring($spanEnd)
    return @{ HasProbe = $true; OnlyProbe = $false; Changed = $true; Text = $updated }
}

function Add-JsonProperty {
    param([string] $Text, $ObjectNode, [string] $PropertyJson)
    $innerStart = $ObjectNode.Start + 1
    $innerEnd = $ObjectNode.End - 1
    $inner = ""
    if ($innerEnd -gt $innerStart) {
        $inner = $Text.Substring($innerStart, $innerEnd - $innerStart)
    }
    if ([string]::IsNullOrWhiteSpace($inner)) {
        return $Text.Substring(0, $innerStart) + $PropertyJson + $Text.Substring($innerEnd)
    }
    return $Text.Substring(0, $innerEnd) + "," + $PropertyJson + $Text.Substring($innerEnd)
}

function Add-ClaudeProbe {
    param([string] $Text)
    $root = Parse-JsonRoot -Text $Text
    if ($null -ne (Find-PermissionsAllow -Root $root)) {
        throw "allow is already in the .claude file; stop"
    }
    $permissions = $null
    for ($index = 0; $index -lt $root.Properties.Count; $index++) {
        if ($root.Properties[$index].Name -ceq "permissions") {
            $permissions = $root.Properties[$index]
            break
        }
    }
    $ruleJson = '"allow":["' + $script:ProbeRule + '"]'
    if ($null -ne $permissions) {
        if ($permissions.Value.Kind -ne "object") {
            throw "permissions in the .claude file is not an object; stop"
        }
        return Add-JsonProperty -Text $Text -ObjectNode $permissions.Value -PropertyJson $ruleJson
    }
    return Add-JsonProperty -Text $Text -ObjectNode $root -PropertyJson ('"permissions":{"allow":["' + $script:ProbeRule + '"]}')
}

function Assert-JsonDocument {
    param([string] $Text, [string] $Path)
    $jsonDocumentType = "System.Text.Json.JsonDocument" -as [type]
    if ($null -eq $jsonDocumentType) {
        return
    }
    $document = $null
    try {
        $document = $jsonDocumentType::Parse($Text)
        if ($document.RootElement.ValueKind -ne [System.Text.Json.JsonValueKind]::Object) {
            throw "root is not an object"
        }
    } catch {
        throw "could not parse $Path as JSON: $($_.Exception.Message)"
    } finally {
        if ($null -ne $document) { $document.Dispose() }
    }
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
            try {
                $updated = Add-ClaudeProbe -Text $text
            } catch {
                $message = $_.Exception.Message
                if ($message -like "allow is already*" -or $message -like "permissions in the*") {
                    throw
                }
                throw "could not parse $path as JSON"
            }
        }
        Assert-JsonDocument -Text $updated -Path $path
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
    $jsonEdit = $null
    $jsonUnparsed = $false
    if ($exists -and -not [string]::IsNullOrWhiteSpace($text)) {
        if ($isToml) {
            $present = Test-TomlHasProbe -Text $text
        } else {
            try {
                $jsonEdit = Update-JsonProbeText -Text $text
                $present = [bool]$jsonEdit.HasProbe
            } catch {
                $jsonEdit = $null
                $jsonUnparsed = $true
                if ($text.Contains($script:ProbeRule)) { $present = $true }
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
        if ($hasCreated) {
            if ($DryRun) {
                Write-Output "dry-run cleanup: would delete $created because $Path is already gone"
            } else {
                [System.IO.File]::Delete($created)
                Write-Output "cleanup: no file $Path; deleted the created-file marker"
            }
            return
        }
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
            if ($hasCreated) {
                if (-not $DryRun) { [System.IO.File]::Delete($created) }
                Write-Output "cleanup: no probe marker in $Path; deleted the created-file marker because the file has other content"
                return
            }
            Write-Output "cleanup: no probe marker in $Path; left the file unchanged"
            return
        }
        if ($DryRun) {
            Write-Output "dry-run cleanup: would remove planted lines from $Path"
            return
        }
        Write-Utf8NoBom -Path $Path -Text $stripped
        if ($hasCreated) {
            [System.IO.File]::Delete($created)
            Write-Output "cleanup: removed planted lines from $Path and deleted the created-file marker because the file has other content"
            return
        }
        Write-Output "cleanup: removed planted lines from $Path"
        return
    }
    if ($jsonUnparsed -or $null -eq $jsonEdit) {
        if ($text.Contains($script:ProbeRule) -or $text.Contains($script:BeginMark)) {
            Write-Output "cleanup: could not parse $Path; left the file untouched"
            return
        }
        if ($hasCreated) {
            if (-not $DryRun) { [System.IO.File]::Delete($created) }
            Write-Output "cleanup: no probe marker in $Path; deleted the created-file marker because the file has other content"
            return
        }
        Write-Output "cleanup: no probe marker in $Path; left the file unchanged"
        return
    }
    if ($jsonEdit.OnlyProbe -and $hasCreated) {
        Remove-CreatedProbeFile -Path $Path -Reason "it contained only the probe rule"
        return
    }
    if (-not $jsonEdit.HasProbe) {
        if ($hasCreated) {
            if (-not $DryRun) { [System.IO.File]::Delete($created) }
            Write-Output "cleanup: no probe marker in $Path; deleted the created-file marker because the file has other content"
            return
        }
        Write-Output "cleanup: no probe marker in $Path; left the file unchanged"
        return
    }
    if ($DryRun) {
        Write-Output "dry-run cleanup: would remove the probe rule from $Path"
        return
    }
    Assert-JsonDocument -Text $jsonEdit.Text -Path $Path
    Write-Utf8NoBom -Path $Path -Text $jsonEdit.Text
    if ($hasCreated) {
        [System.IO.File]::Delete($created)
        Write-Output "cleanup: removed the probe rule from $Path and deleted the created-file marker because the file has other content"
        return
    }
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
