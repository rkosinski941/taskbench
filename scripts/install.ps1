# taskbench install helper.
#
# Default: print how to run taskbench and change nothing.
# -WithTailscale is OPTIONAL. It needs the machine owner's approval.
# It installs Tailscale if needed, runs `tailscale serve` for the port in
# config.json, and checks that Funnel is OFF. The app itself stays on
# 127.0.0.1. This script never enables Funnel.
#
# -WhatIf prints the commands and does not install or reconfigure anything.
# A Funnel-on result still fails, including under -WhatIf.

#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch] $WithTailscale,
    [string] $ConfigPath = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Write-Step {
    param([string] $Message)
    Write-Host $Message
}

function Get-TaskbenchPort {
    param([string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Step "Config not found ($Path). Using port 8765."
        return 8765
    }
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    $config = $raw | ConvertFrom-Json
    if ($null -ne $config.bind_host -and "$($config.bind_host)" -ne "127.0.0.1") {
        throw "bind_host must be 127.0.0.1. Refusing to put Tailscale Serve in front of any other address."
    }
    $port = 8765
    if ($null -ne $config.port) {
        $port = [int] $config.port
    }
    if ($port -lt 1 -or $port -gt 65535) {
        throw "port must be an integer from 1 to 65535"
    }
    return $port
}

function Write-ExpectedFunnelCheck {
    param([int] $Port, [bool] $Executed)
    Write-Step "Funnel check: tailscale serve status; tailscale serve status --json; tailscale funnel status."
    Write-Step "Funnel is on when the text says 'Funnel on' or 'Available on the internet', or the JSON Funnel/AllowFunnel value is truthy."
    Write-Step "Unreadable JSON fails closed on a real run. -WhatIf does not treat a skipped or unreadable check as Funnel on."
    if (-not $Executed) {
        Write-Step "Funnel check was not executed because tailscale is not installed. Expected result when Funnel is off: off. This dry run is not a failure."
        Write-Step "A real run would serve 127.0.0.1:${Port} on the tailnet only after that check."
    }
}

function Invoke-FunnelCheck {
    param(
        [string] $ServeText,
        [string] $ServeJson,
        [string] $FunnelText
    )
    # -WhatIf must not skip the read-only check or leak PYTHONPATH into this session.
    $savedWhatIf = $WhatIfPreference
    $WhatIfPreference = $false
    $savedPythonPath = [System.Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")
    try {
        $py = Get-Command python -ErrorAction SilentlyContinue
        if (-not $py) {
            $py = Get-Command python3 -ErrorAction SilentlyContinue
        }
        if ($py) {
            $dir = Join-Path ([System.IO.Path]::GetTempPath()) ("taskbench-funnel-" + [guid]::NewGuid().ToString("n"))
            [System.IO.Directory]::CreateDirectory($dir) | Out-Null
            try {
                $serveFile = Join-Path $dir "serve.txt"
                $jsonFile = Join-Path $dir "serve.json"
                $funnelFile = Join-Path $dir "funnel.txt"
                [System.IO.File]::WriteAllText($serveFile, $ServeText)
                [System.IO.File]::WriteAllText($jsonFile, $ServeJson)
                [System.IO.File]::WriteAllText($funnelFile, $FunnelText)
                [System.Environment]::SetEnvironmentVariable("PYTHONPATH", $Root, "Process")
                $code = @"
from pathlib import Path
import sys
from taskbench.funnel_check import funnel_is_on
on = funnel_is_on(
    serve_text=Path(sys.argv[1]).read_text(encoding="utf-8"),
    serve_json=Path(sys.argv[2]).read_text(encoding="utf-8"),
    funnel_text=Path(sys.argv[3]).read_text(encoding="utf-8"),
)
print("on" if on else "off")
"@
                $result = & $py.Source -c $code $serveFile $jsonFile $funnelFile
                if ($LASTEXITCODE -eq 0 -and ($result -eq "on" -or $result -eq "off")) {
                    return ($result -eq "on")
                }
                Write-Step "Python funnel check failed. Falling back to the same text rules."
            } finally {
                [System.IO.Directory]::Delete($dir, $true)
            }
        }
    } finally {
        [System.Environment]::SetEnvironmentVariable("PYTHONPATH", $savedPythonPath, "Process")
        $WhatIfPreference = $savedWhatIf
    }
    $combined = "$ServeText`n$FunnelText"
    if ($combined -match '(?i)funnel on|available on the internet') {
        return $true
    }
    $trimmed = "$ServeJson".Trim()
    if ($trimmed) {
        try {
            $null = $trimmed | ConvertFrom-Json
        } catch {
            return $true
        }
        if ($trimmed -match '(?i)"[^"]*funnel[^"]*"\s*:\s*(true|\{[^{}]*[^{}\s])') {
            return $true
        }
    }
    return $false
}

function Get-TailscaleOutput {
    param([string[]] $TailArgs)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & tailscale @TailArgs 2>&1 | Out-String
        return $output
    } finally {
        $ErrorActionPreference = $previous
    }
}

if (-not $WithTailscale) {
    Write-Step "taskbench install helper: no changes."
    Write-Step "Local app (loopback only): python -m taskbench --config config.json"
    Write-Step "Optional remote access is off. -WithTailscale needs your approval before it installs Tailscale or runs tailscale serve."
    Write-Step "Add -WhatIf to print those commands without running them."
    exit 0
}

if (-not $ConfigPath) {
    $preferred = Join-Path $Root "config.json"
    if (Test-Path -LiteralPath $preferred) {
        $ConfigPath = $preferred
    } else {
        $ConfigPath = Join-Path $Root "config.example.json"
    }
}
$port = Get-TaskbenchPort -Path $ConfigPath
Write-Step "taskbench port from config: $port (listener stays 127.0.0.1)."

$existing = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $existing) {
    $wingetCmd = "winget install --id Tailscale.Tailscale -e --scope user --accept-package-agreements --accept-source-agreements"
    Write-Step "Command: $wingetCmd"
    Write-Step "Per-user scope is requested first. Tailscale's Windows package installs a system service (the VPN driver)."
    Write-Step "If winget says an administrator is required, approve the UAC prompt or re-run this script from an elevated PowerShell."
    Write-Step "This script does not elevate itself and does not call Start-Process -Verb RunAs."
    if (-not $PSCmdlet.ShouldProcess("Tailscale.Tailscale", "winget install --scope user")) {
        Write-Step "WhatIf: skipped winget, tailscale serve, and any Funnel change."
        Write-ExpectedFunnelCheck -Port $port -Executed $false
        exit 0
    }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget is not on PATH. Install Tailscale yourself, then re-run this script. Admin rights may be required for the service."
    }
    & winget install --id Tailscale.Tailscale -e --scope user --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "winget install failed (exit $LASTEXITCODE). If the message asked for administrator rights, re-run from an elevated PowerShell. This script will not turn Funnel on."
    }
} else {
    Write-Step "Tailscale is already on PATH. Skipping winget."
}

if (-not (Get-Command tailscale -ErrorAction SilentlyContinue)) {
    throw "tailscale is not on PATH after install. Open a new PowerShell window, or add the Tailscale directory to PATH, then re-run. Do not continue with Funnel."
}

$serveText = Get-TailscaleOutput -TailArgs @("serve", "status")
$serveJson = Get-TailscaleOutput -TailArgs @("serve", "status", "--json")
$funnelText = Get-TailscaleOutput -TailArgs @("funnel", "status")
$funnelOn = Invoke-FunnelCheck -ServeText $serveText -ServeJson $serveJson -FunnelText $funnelText
if ($funnelOn) {
    Write-Step "FUNNEL IS ON. taskbench will not expose this page to the public internet."
    Write-Step "Command: tailscale funnel off"
    if (-not $PSCmdlet.ShouldProcess("Tailscale Funnel", "turn off")) {
        Write-Step "WhatIf: Funnel is on. A real run would execute 'tailscale funnel off' and stop if it stayed on. No serve change was made."
        exit 1
    }
    & tailscale funnel off
    if ($LASTEXITCODE -ne 0) {
        Write-Step "tailscale funnel off failed. Retrying: tailscale funnel --https=443 off"
        & tailscale funnel --https=443 off
    }
    $serveText = Get-TailscaleOutput -TailArgs @("serve", "status")
    $serveJson = Get-TailscaleOutput -TailArgs @("serve", "status", "--json")
    $funnelText = Get-TailscaleOutput -TailArgs @("funnel", "status")
    if (Invoke-FunnelCheck -ServeText $serveText -ServeJson $serveJson -FunnelText $funnelText) {
        throw "Funnel is still on after turning it off. Refusing to leave taskbench published. Turn Funnel off in the Tailscale admin console and re-run."
    }
    Write-Step "Funnel is now off."
}

$already = ($serveText -match "127\.0\.0\.1:$port") -or ($serveJson -match "127\.0\.0\.1:$port")
if ($already) {
    Write-Step "tailscale serve already proxies 127.0.0.1:${port}. Not changing it."
} else {
    $serveCmd = "tailscale serve --bg $port"
    Write-Step "Command: $serveCmd"
    if (-not $PSCmdlet.ShouldProcess("127.0.0.1:$port", "tailscale serve --bg")) {
        Write-Step "WhatIf: skipped tailscale serve. Funnel was not enabled."
        exit 0
    }
    & tailscale serve --bg $port
    if ($LASTEXITCODE -ne 0) {
        Write-Step "tailscale serve --bg failed. Retrying: tailscale serve $port"
        & tailscale serve $port
        if ($LASTEXITCODE -ne 0) {
            throw "tailscale serve failed (exit $LASTEXITCODE)."
        }
    }
}

$serveText = Get-TailscaleOutput -TailArgs @("serve", "status")
$serveJson = Get-TailscaleOutput -TailArgs @("serve", "status", "--json")
$funnelText = Get-TailscaleOutput -TailArgs @("funnel", "status")
if (Invoke-FunnelCheck -ServeText $serveText -ServeJson $serveJson -FunnelText $funnelText) {
    Write-Step "FUNNEL IS ON after serve. Turning it off."
    if ($PSCmdlet.ShouldProcess("Tailscale Funnel", "turn off after serve")) {
        & tailscale funnel off
    }
    $serveText = Get-TailscaleOutput -TailArgs @("serve", "status")
    $serveJson = Get-TailscaleOutput -TailArgs @("serve", "status", "--json")
    $funnelText = Get-TailscaleOutput -TailArgs @("funnel", "status")
    if (Invoke-FunnelCheck -ServeText $serveText -ServeJson $serveJson -FunnelText $funnelText) {
        throw "Funnel is still on. taskbench stays loopback-only until Funnel is off."
    }
}

Write-Step "Funnel is OFF. Serve, when configured, is tailnet-only."
Write-Step "The taskbench process is still bound to 127.0.0.1 only."
Write-Step "Add the tailnet hostname to allowed_hosts in config.json (it is empty by default), then restart taskbench."
Write-Step "Find the hostname with: tailscale status --json"
Write-Step "Optional, so a plugged-in machine does not sleep: Settings > System > Power, or: powercfg /change standby-timeout-ac 0"
exit 0
