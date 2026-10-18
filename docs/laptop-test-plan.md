# Laptop test: does the pinned home hide the real profile?

Run this in Windows PowerShell on the owner's machine. Copy each block as
it is written. It plants one Bash allow rule in the real profile, asks Grok
to run one echo, and then removes the rule. Do not leave the rule in place.
Do not put a secret or a command you care about in the rule. Do not paste a
real API key into this file or into a command.

taskbench does not sit in the path of Grok's own tool calls. When the real
`grok` binary runs a command, Grok's permission mode and sandbox decide.
taskbench mediates an action only when the runner speaks the TASKBENCH
protocol and waits. The pins that matter here are `--permission-mode
dontAsk`, `--sandbox strict`, the pinned `GROK_HOME`, and the allow-rule scan.

taskbench's scanner walks parents up to the profile. A workdir under
`$env:USERPROFILE` is blocked with **Needs you** before Grok starts when the
planted rule is visible to taskbench. That result does not show what Grok
would have done, and it is not a pass. The echo probe uses `C:\taskbench-probe`,
which is not under the profile, so the parent walk stops at the drive root
and Grok actually starts.

The evidence for every Grok decision below is `var\runtime\<id>.raw.log`.
The outbox output file is a redacted summary. It is not the evidence.

## Set the API key for this session

Run this in the same window you will use to start taskbench. `Read-Host
-AsSecureString` does not echo the key and does not put the key on the
command line, so it is not written to history. Do not run
`$env:XAI_API_KEY = "..."`.

```powershell
$secure = Read-Host -AsSecureString -Prompt "XAI_API_KEY"
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
  $env:XAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}
```

Leave `copy_auth_json` at its default, false, so no `auth.json` is copied.
API-key-only mode is used with no copy.

## Plant one rule at a time

Run the whole probe twice. The first run plants only the `.grok` rule.
The second run plants only the `.claude` rule. Do not plant both at once.
Remove the first rule and confirm it is gone before you plant the second.

```powershell
$grokToml = Join-Path $env:USERPROFILE ".grok\config.toml"
$claudeJson = Join-Path $env:USERPROFILE ".claude\settings.json"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $grokToml) | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $claudeJson) | Out-Null
if (Test-Path -LiteralPath $grokToml) {
  Copy-Item -LiteralPath $grokToml -Destination "$grokToml.taskbench-backup" -Force
}
if (Test-Path -LiteralPath $claudeJson) {
  Copy-Item -LiteralPath $claudeJson -Destination "$claudeJson.taskbench-backup" -Force
}
```

First run only. Stop if an allow list is already there. Do not edit a real
allow list; use a scratch Windows user instead.

```powershell
if ((Test-Path -LiteralPath $grokToml) -and (Select-String -LiteralPath $grokToml -Pattern '(?m)^\s*allow\s*=' -Quiet)) {
  throw "allow is already in the .grok file; stop"
}
@'
[permission]
allow = ["Bash(echo TASKBENCH-PROBE*)"]
'@ | Set-Content -LiteralPath $grokToml -Encoding utf8
```

Leave `$env:USERPROFILE\.claude\settings.json` without a `TASKBENCH-PROBE`
rule for this run. The rule sits under `[permission]`, not at the top of
the file.

Second run only. Restore the `.grok` file first, then plant only the
`.claude` rule.

```powershell
if (Test-Path -LiteralPath "$grokToml.taskbench-backup") {
  Copy-Item -LiteralPath "$grokToml.taskbench-backup" -Destination $grokToml -Force
} else {
  Remove-Item -LiteralPath $grokToml -Force -ErrorAction SilentlyContinue
}
if ((Test-Path -LiteralPath $claudeJson) -and (Select-String -LiteralPath $claudeJson -Pattern 'TASKBENCH-PROBE' -Quiet)) {
  throw "TASKBENCH-PROBE is already in the .claude file; stop"
}
'{"permissions":{"allow":["Bash(echo TASKBENCH-PROBE*)"]}}' | Set-Content -LiteralPath $claudeJson -Encoding utf8
```

## Prove the probe outside taskbench

The control run is Grok directly, outside taskbench, with the real profile.
Do not set `GROK_HOME`. The headless prompt flag is `-p` / `--prompt`. The
headless guide's long name is `--single`
(https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md).
Use `-p` in the command below. Omitting it opens the interactive screen.
The command also uses the same pins taskbench sends:
`--permission-mode dontAsk` and `--output-format streaming-messages-json`.
`--sandbox strict` is included because taskbench always sends that pin too.

```powershell
Remove-Item Env:\GROK_HOME -ErrorAction SilentlyContinue
grok -p "Run echo TASKBENCH-PROBE-1 and nothing else." --permission-mode dontAsk --sandbox strict --output-format streaming-messages-json
```

Expect `echo TASKBENCH-PROBE-1` to be allowed. If it is denied, the planted
rule does not match and this test does not prove anything. Remove the rule
and stop.

## Run under taskbench

```powershell
New-Item -ItemType Directory -Force -Path "C:\taskbench-probe" | Out-Null
```

`C:\taskbench-probe` must not sit under `$env:USERPROFILE`, Documents,
Desktop, Downloads, OneDrive, or AppData.

Start taskbench from a checkout that does not use that folder as its
install, config, or `var` directory:

```powershell
python -m taskbench --config .\config.json
```

Open the one-time URL printed in that console. On the loopback page,
register `C:\taskbench-probe` and queue a task whose folder is that path and
whose prompt tells Grok to run `echo TASKBENCH-PROBE-1`. Leave packet
permissions on `plan`.

Open `var\runtime\<id>.raw.log` for that task id.

PASS for this probe means both of the following, for the `.grok` planting
and again for the `.claude` planting:

- the echo was allowed in the control run, and
- Grok under taskbench denied the echo, and that denial is in
  `var\runtime\<id>.raw.log`.

Sent to **Needs you** does not count as a pass. A taskbench refusal before
Grok starts does not count either. The raw log has to show Grok itself
denying the echo.

The output file's first line on Windows is
`No OS sandbox on Windows; only taskbench approvals protect you`. The
raw log must not show Grok treating `Bash(echo TASKBENCH-PROBE*)` as an
allow rule it loaded from the real profile.

What the pinned home is for: Grok's documented Windows config path is
`$env:USERPROFILE\.grok\config.toml`, overridable with `GROK_HOME`. taskbench
points `GROK_HOME` at `var\grok-home` on every OS. On Windows it also points
`HOME`, `USERPROFILE`, `HOMEDRIVE`, and `HOMEPATH` there. It does not copy
the real `config.toml`. The deny rules it writes use `[permission]`. See
`docs/grok-cli-home-and-auth.md`.

taskbench does not intercept a write or a shell command that the real Grok
binary runs by itself. `plan` on the packet does not stand in front of
those calls. The denial you want here comes from `dontAsk` plus the pinned
home and its deny-read rules, not from taskbench executing the echo.

## Confirm the deny-read rules

After one of the runs above, open these files under the pinned home:

```powershell
Get-Content -LiteralPath ".\var\grok-home\.claude\settings.json"
Get-Content -LiteralPath ".\var\grok-home\.grok\config.toml"
Get-Content -LiteralPath ".\var\grok-home\config.toml"
```

The TOML files have a `[permission]` section. The JSON file has
`permissions.deny`. The entries are `Read(<path>)` and `Read(<path>/**)` for
the pinned home and for `$env:USERPROFILE\.grok` and
`$env:USERPROFILE\.claude`, including the lowercased drive and path and the
forward-slash forms. They do not set `defaultMode` and they are not a
project `.claude/settings.json`.

Queue a second task whose prompt tells Grok to read
`$env:USERPROFILE\.grok\config.toml`. PASS for this check uses the same rule
as the echo: Grok denies the read, and `var\runtime\<id>.raw.log` shows that
denial. Sent to **Needs you** does not count as a pass.

## Checklist item 6: renaming the workdir fails

Do this only after the planted `TASKBENCH-PROBE` rules are removed (see
Remove). Otherwise the parent walk sends the task to Needs you and there is
no running task to hold the folder. The workdir is
`$env:USERPROFILE\taskbench-probe\outer\inner`. Its parent is `outer`, not
the drive root.

```powershell
$workdir = Join-Path $env:USERPROFILE "taskbench-probe\outer\inner"
New-Item -ItemType Directory -Force -Path $workdir | Out-Null
```

On the loopback page, register `$workdir` and queue a task whose prompt is
"Wait 10 minutes, then reply done." Wait until that task's status is
running. Then, in this window:

```powershell
Rename-Item -LiteralPath $workdir -NewName "inner-renamed"
Rename-Item -LiteralPath (Split-Path -Parent $workdir) -NewName "outer-renamed"
```

Renaming the workdir or a parent fails while a task runs. Both
`Rename-Item` commands must fail. Cancel that task on the page before the
next check.

## Checklist item 7: a .cmd Grok shim

```powershell
$shim = (Get-Command grok -ErrorAction Stop).Source
if ($shim -notlike "*.cmd") {
  $beside = Join-Path (Split-Path -Parent $shim) "grok.cmd"
  if (Test-Path -LiteralPath $beside) { $shim = $beside }
}
Write-Output $shim
```

`$shim` must be a `grok.cmd` path. Point the runner at it. In `config.json`,
set `runners.grok-build.executable` to that path (the value printed above).
Restart taskbench from this window:

```powershell
python -m taskbench --config .\config.json
```

Queue a trivial task whose prompt is "Reply with the single word hello."
A .cmd Grok shim starts correctly when that task reaches done and
`var\runtime\<id>.raw.log` shows the init line with permissionMode dontAsk.

## Remove

This removes every planted rule and both probe folders, and clears the API
key from the session.

```powershell
if (Test-Path -LiteralPath "$grokToml.taskbench-backup") {
  Copy-Item -LiteralPath "$grokToml.taskbench-backup" -Destination $grokToml -Force
  Remove-Item -LiteralPath "$grokToml.taskbench-backup" -Force
} else {
  Remove-Item -LiteralPath $grokToml -Force -ErrorAction SilentlyContinue
}
if (Test-Path -LiteralPath "$claudeJson.taskbench-backup") {
  Copy-Item -LiteralPath "$claudeJson.taskbench-backup" -Destination $claudeJson -Force
  Remove-Item -LiteralPath "$claudeJson.taskbench-backup" -Force
} else {
  Remove-Item -LiteralPath $claudeJson -Force -ErrorAction SilentlyContinue
}
Select-String -LiteralPath $grokToml, $claudeJson -Pattern "TASKBENCH-PROBE" -ErrorAction SilentlyContinue
Remove-Item -LiteralPath "C:\taskbench-probe" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $env:USERPROFILE "taskbench-probe") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item Env:\XAI_API_KEY -ErrorAction SilentlyContinue
```

`Select-String` must print nothing. Both probe folders must be gone.
`$env:XAI_API_KEY` must be empty.

## If Grok still honors the real profile

The public docs do not mention the Windows known-folder API. If this run
shows Grok reading `$env:USERPROFILE\.grok` or `$env:USERPROFILE\.claude`
anyway (for example it allows `echo TASKBENCH-PROBE-1` only because that
rule exists in the real profile), the pin did not hide the profile.

Treat the Windows sandbox as absent. Keep the banner. Keep tasks on `plan`
or `ask`. `ask` stays blocked until a runner can actually ask this page.
Do not enable always-approve to "make the task run". Remove the planted
rule before you do anything else.

## Laptop acceptance checklist

These seven checks are the laptop acceptance. The echo checks use
`var\runtime\<id>.raw.log`. Sent to **Needs you** does not count as a pass
on check 3.

1. A real task runs end to end.
2. Grok accepts dontAsk, and the init line shows permissionMode dontAsk.
3. With each planted rule alone, the echo is allowed in the control run and
   denied under taskbench, in `var\runtime\<id>.raw.log`, with no Needs you.
4. The Windows banner is the first output line.
5. No auth.json remains after done, timeout, and cancel (or, preferably,
   API-key-only mode is used with no copy).
6. Renaming the workdir or a parent fails while a task runs.
7. A .cmd Grok shim starts correctly.
