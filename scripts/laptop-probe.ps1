# Plant and remove the laptop-probe allow rule without touching the rest
# of the owner's settings.
#
# Each mode recomputes paths from $env:USERPROFILE. A new window can run
# -Cleanup on its own. -Backup is one-time and timestamped: it never
# overwrites an existing *.taskbench-backup-* file, and it refuses to copy
# a file that already contains TASKBENCH-PROBE. -Cleanup restores from the
# oldest backup when one exists. Otherwise it removes only the planted
# block and leaves the file in place. Running -Cleanup twice does not
# delete the restored file.
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
$script:ProbeMarker = "TASKBENCH-PROBE"
$script:BeginMark = "# BEGIN TASKBENCH-PROBE"
$script:EndMark = "# END TASKBENCH-PROBE"
$script:ProbeJson = '{"permissions":{"allow":["Bash(echo TASKBENCH-PROBE*)"]},"taskbenchProbe":"BEGIN TASKBENCH-PROBE"}'

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
            Where-Object { $_.Name.StartsWith($prefix) } |
            Sort-Object -Property Name
    )
}

function Test-SameBytes {
    param([string] $Left, [string] $Right)
    if (-not (Test-Path -LiteralPath $Left) -or -not (Test-Path -LiteralPath $Right)) {
        return $false
    }
    $leftBytes = [System.IO.File]::ReadAllBytes($Left)
    $rightBytes = [System.IO.File]::ReadAllBytes($Right)
    if ($leftBytes.Length -ne $rightBytes.Length) {
        return $false
    }
    for ($index = 0; $index -lt $leftBytes.Length; $index++) {
        if ($leftBytes[$index] -ne $rightBytes[$index]) {
            return $false
        }
    }
    return $true
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
        if ($text -like "*$($script:ProbeMarker)*") {
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
        if ($text -like "*$($script:ProbeMarker)*") {
            throw "$($script:ProbeMarker) is already in $path; stop"
        }
        if (@(Get-ProbeBackups -Path $path).Count -eq 0) {
            throw "refusing to plant into $path with no backup; run -Backup first"
        }
        if ($Which -eq "grok" -and [regex]::IsMatch($text, '(?m)^\s*allow\s*=')) {
            throw "allow is already in the .grok file; stop"
        }
        if ($Which -eq "claude" -and $text -match '"allow"') {
            throw "allow is already in the .claude file; stop"
        }
    }
    if ($Which -eq "grok") {
        $block = $script:BeginMark + "`r`n[permission]`r`nallow = [`"Bash(echo TASKBENCH-PROBE*)`"]`r`n" + $script:EndMark + "`r`n"
        if (-not $blank -and -not $text.EndsWith("`n")) {
            $block = "`r`n" + $block
        }
        if ($blank) {
            $updated = $block
        } else {
            $updated = $text + $block
        }
    } else {
        $updated = $script:ProbeJson + "`n"
    }
    if (-not (Test-Path -LiteralPath $directory)) {
        if ($DryRun) {
            Write-Output "dry-run plant: would create directory $directory"
        } else {
            New-Item -ItemType Directory -Path $directory | Out-Null
            Write-Output "plant: created directory $directory"
        }
    }
    if ($DryRun) {
        Write-Output "dry-run plant: would write $Which rule into $path"
        return
    }
    Write-Utf8NoBom -Path $path -Text $updated
    Write-Output "plant: wrote $Which rule into $path"
}

function Remove-MarkerBlock {
    param([string] $Text)
    $pattern = '(?s)# BEGIN TASKBENCH-PROBE\r*\n.*?# END TASKBENCH-PROBE\r*\n?'
    return [regex]::Replace($Text, $pattern, "")
}

function Invoke-CleanFile {
    param([string] $Path)
    $backups = @(Get-ProbeBackups -Path $Path)
    if ($backups.Count -gt 0) {
        $backup = $backups[0].FullName
        if ((Test-Path -LiteralPath $Path) -and (Test-SameBytes -Left $Path -Right $backup)) {
            Write-Output "cleanup: $Path already matches $backup"
            return
        }
        if ($DryRun) {
            Write-Output "dry-run cleanup: would restore $Path from $backup"
            return
        }
        [System.IO.File]::Copy($backup, $Path, $true)
        Write-Output "cleanup: restored $Path from $backup"
        return
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Output "cleanup: no file $Path"
        return
    }
    $text = [System.IO.File]::ReadAllText($Path)
    if ($text.Contains($script:BeginMark) -and -not $text.Contains($script:EndMark)) {
        Write-Output "cleanup: unclosed marker in $Path; left the file untouched"
        return
    }
    $stripped = Remove-MarkerBlock -Text $text
    $compact = [regex]::Replace($stripped, '\s+', '')
    if ($compact -eq $script:ProbeJson) {
        $stripped = "{}" + "`n"
    }
    if ($stripped -eq $text) {
        Write-Output "cleanup: no planted lines in $Path"
        return
    }
    if ($DryRun) {
        Write-Output "dry-run cleanup: would remove planted lines from $Path"
        return
    }
    Write-Utf8NoBom -Path $Path -Text $stripped
    Write-Output "cleanup: removed planted lines from $Path"
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
