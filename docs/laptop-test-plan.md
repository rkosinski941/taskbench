# Laptop test: does the pinned home hide the real profile?

Run this in PowerShell 7 on the owner's machine, from the taskbench
checkout. PowerShell 7 is supported and recommended. Each block below stands
on its own. It recomputes paths from
`$env:USERPROFILE` or calls `scripts/laptop-probe.ps1`, which does that
itself. A new window can run cleanup without the variables from an earlier
window. Do not paste a real API key into this file or into a command.

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

Allow this script in the current window only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

`scripts/laptop-probe.ps1` takes exactly one of `-Backup`, `-Plant`, or
`-Cleanup`. Add `-DryRun` to print the actions and write nothing. `-Plant`
also takes `-Which grok` or `-Which claude`.

## Set the API key for this session

Run this in the same window you will use to start taskbench. `Read-Host
-AsSecureString` does not echo the key and does not put the key on the
command line, so it is not written to history. Do not run
`$env:XAI_API_KEY = "..."`.

```powershell
$secure = Read-Host -AsSecureString -Prompt "XAI_API_KEY"
$bstr = [IntPtr]::Zero
try {
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  $env:XAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
  if ($bstr -ne [IntPtr]::Zero) {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  }
  $bstr = [IntPtr]::Zero
  if ($null -ne $secure) { $secure.Dispose() }
  $secure = $null
}
```

Leave `copy_auth_json` at its default, false, so no `auth.json` is copied.
API-key-only mode is used with no copy.

## Step 0: read the flags Grok actually accepts

The headless guide lists `-p, --single` for an inline prompt and
`--prompt-file` for a file. It does not list a `--prompt` flag. `--cwd` sets
the working directory. The same page is
<https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md>.

taskbench does not hard-code the prompt-file flag. `config.json`
`runners.grok-build.args_template` already contains `"--prompt-file"` and
`"{prompt_file}"`. If `grok --help` prints a different prompt-file flag,
change that string in `config.json`. No code change.

```powershell
grok --help
```

Use the headless flag and the prompt-file flag that help prints. The
commands below use `--prompt-file`, which is the documented file flag and
the one taskbench sends. They also pass `--cwd`, `--permission-mode dontAsk`,
`--sandbox strict`, and `--output-format streaming-messages-json`.

## Back up, then plant one rule at a time

Dry-run first. It does not write.

```powershell
.\scripts\laptop-probe.ps1 -Backup -DryRun
.\scripts\laptop-probe.ps1 -Plant -Which grok -DryRun
```

Then back up for real. The script recomputes
`$env:USERPROFILE\.grok\config.toml` and
`$env:USERPROFILE\.claude\settings.json`. The backup name is
`config.toml.taskbench-backup-<yyyyMMdd-HHmmss>` (and the same suffix on
`settings.json`). It does not overwrite an existing backup. If the live
file already contains `TASKBENCH-PROBE`, the script stops and does not copy
it. Run the whole probe twice. The first run plants only the `.grok` rule.
The second run plants only the `.claude` rule.

```powershell
.\scripts\laptop-probe.ps1 -Backup
.\scripts\laptop-probe.ps1 -Plant -Which grok
```

The planted TOML block is the allow entry between marker comments. It is
not a replacement of the file. When the file has no `[permission]` table,
the block includes that table. When `[permission]` already exists and has
no `allow` key, the script inserts the allow entry inside that table,
between the same marker comments, and does not write a second
`[permission]` table. It then checks that the file parses with `python -c`
and `tomllib` when that module imports. If Python or `tomllib` is missing,
the documented check is one `[permission]` table and a matched pair of
marker comments.

```toml
# BEGIN TASKBENCH-PROBE
[permission]
allow = ["Bash(echo TASKBENCH-PROBE*)"]
# END TASKBENCH-PROBE
```

The rule sits under `[permission]`, not at the top of the file. When the
`.grok` file did not exist, the script also writes
`config.toml.taskbench-created` beside it. Leave
`$env:USERPROFILE\.claude\settings.json` without a `TASKBENCH-PROBE` rule
for this run.

Stop if the script says `allow` is already in the file. Do not edit a real
allow list; use a scratch Windows user instead.

## Prove the probe outside taskbench

This block creates the echo workdir, writes the prompt file, and runs Grok.
Copy it as a whole. The control run is Grok directly, outside taskbench,
with the real profile. Do not set `GROK_HOME`. Omitting the headless flag
opens the interactive screen.

```powershell
New-Item -ItemType Directory -Force -Path "C:\taskbench-probe" | Out-Null
$promptPath = "C:\taskbench-probe\control-prompt.txt"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($promptPath, "Run echo TASKBENCH-PROBE-1 and nothing else.`n", $utf8)
Remove-Item Env:\GROK_HOME -ErrorAction SilentlyContinue
grok --prompt-file $promptPath --cwd C:\taskbench-probe --permission-mode dontAsk --sandbox strict --output-format streaming-messages-json
```

Expect `echo TASKBENCH-PROBE-1` to be allowed. If it is denied, the planted
rule does not match and this test does not prove anything. Run
`.\scripts\laptop-probe.ps1 -Cleanup` and stop.

## Run under taskbench

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

## Second run: only the `.claude` rule

Stop taskbench. `-Cleanup` restores a live file from its backup only when
the probe marker or the `Bash(echo TASKBENCH-PROBE*)` rule is still in that
file, then renames the backup to `*.used` so a later cleanup cannot copy it
again. If the marker and the rule are absent, cleanup leaves the live file
and the backup alone. Then plant only the `.claude` rule. JSON has no
comments. The planted rule is the exact string `Bash(echo TASKBENCH-PROBE*)`
inside `permissions.allow`. Cleanup splices that exact string out of the
file and leaves every other byte in place, including dates, numbers, and
nested objects. On PowerShell 7 it checks the result with `System.Text.Json`.
It drops an allow array or a permissions object that is empty because this
script added it. When `settings.json` did not exist before planting, the script writes
`settings.json.taskbench-created`. Cleanup deletes `settings.json` only when
that sidecar exists and the file contains nothing but the probe rule. If
the sidecar is still there and the file has other content, cleanup removes
only the probe rule, deletes the sidecar, and keeps the file. A file that
already existed is not deleted.

```powershell
.\scripts\laptop-probe.ps1 -Cleanup
.\scripts\laptop-probe.ps1 -Plant -Which claude
```

`-Cleanup` also removes `C:\taskbench-probe`. Create it again, rewrite the
prompt file, and repeat the control command and the taskbench run from the
sections above. This pass plants only the `.claude` rule.

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
project `.claude/settings.json`. The TOML files also set `hooks = false` and
`mcps = false` under `[compat.claude]` and `[compat.cursor]`.

Queue a second task whose prompt tells Grok to read
`$env:USERPROFILE\.grok\config.toml`. PASS for this check uses the same rule
as the echo: Grok denies the read, and `var\runtime\<id>.raw.log` shows that
denial. Sent to **Needs you** does not count as a pass.

## Checklist item 6: renaming the workdir fails

Do this only after the planted `TASKBENCH-PROBE` rules are removed.
Otherwise the parent walk sends the task to Needs you and there is no
running task to hold the folder. `dontAsk` denies a prompt that asks Grok
to sleep, so this check does not use sleep. It points the runner at
`tests/fixtures/fake_runner.py` with `--scenario hang`. That process emits
nothing and stays alive for 10000 seconds, which is long enough to try the
rename. Cancel the task on the page when the rename attempts are done.

The workdir is `$env:USERPROFILE\taskbench-probe\outer\inner`. Its parent is
`outer`, not the drive root.

```powershell
.\scripts\laptop-probe.ps1 -Cleanup
$workdir = Join-Path $env:USERPROFILE "taskbench-probe\outer\inner"
New-Item -ItemType Directory -Force -Path $workdir | Out-Null
$cfg = Join-Path (Get-Location) "config.json"
$cfgDir = Split-Path -Parent $cfg
$cfgBackups = @(Get-ChildItem -LiteralPath $cfgDir -Force -File -ErrorAction SilentlyContinue | Where-Object { $_.Name.StartsWith("config.json.taskbench-backup-") } | Sort-Object Name)
if ($cfgBackups.Count -eq 0) {
  $cfgDest = "$cfg.taskbench-backup-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
  Copy-Item -LiteralPath $cfg -Destination $cfgDest
  Write-Output "backed up config to $cfgDest"
} else {
  Write-Output "config backup already exists; not overwritten"
}
python -c "import json, pathlib; p = pathlib.Path('config.json'); d = json.loads(p.read_text(encoding='utf-8')); d['runners']['grok-build']['executable'] = 'python'; d['runners']['grok-build']['args_template'] = ['tests/fixtures/fake_runner.py', '--scenario', 'hang', '--prompt-file', '{prompt_file}', '--project-dir', '{project_dir}']; p.write_text(json.dumps(d, indent=2) + '\n', encoding='utf-8')"
```

Restart taskbench:

```powershell
python -m taskbench --config .\config.json
```

On the loopback page, register `$workdir` and queue a task whose folder is
that path and whose prompt is "Reply done." Wait until that task's status
is running. The hang scenario does not exit, so the status stays running.
Then, in this window:

```powershell
Rename-Item -LiteralPath $workdir -NewName "inner-renamed"
Rename-Item -LiteralPath (Split-Path -Parent $workdir) -NewName "outer-renamed"
```

Renaming the workdir or a parent fails while a task runs. Both
`Rename-Item` commands must fail. Cancel that task on the page before the
next check. Stop taskbench. Restore `config.json` from the oldest backup
and leave the backup in place:

```powershell
$cfg = Join-Path (Get-Location) "config.json"
$cfgDir = Split-Path -Parent $cfg
$cfgBackups = @(Get-ChildItem -LiteralPath $cfgDir -Force -File | Where-Object { $_.Name.StartsWith("config.json.taskbench-backup-") } | Sort-Object Name)
if ($cfgBackups.Count -gt 0) {
  [System.IO.File]::Copy($cfgBackups[0].FullName, $cfg, $true)
  Write-Output "restored $cfg from $($cfgBackups[0].FullName)"
} else {
  Write-Output "no config.json backup; left config.json in place"
}
```

## Checklist item 7: a .cmd Grok shim

Tell which file `grok` is with `Get-Command grok | Select Source`.

```powershell
Get-Command grok | Select Source
$source = (Get-Command grok -ErrorAction Stop).Source
if ($source -notlike "*.cmd") {
  Write-Output "Item 7 is N/A: Grok is installed as $source, not a .cmd shim."
} else {
  Write-Output "Item 7 applies: $source is a grok.cmd shim."
}
```

If that prints `Item 7 is N/A`, checklist item 7 is N/A. Do not fail the
laptop test for an `.exe` install. If it prints that the source is a
`grok.cmd` shim, point the runner at it. In `config.json`, set
`runners.grok-build.executable` to `$source`. Leave `args_template` on
`--prompt-file` and `{prompt_file}`, not the hang scenario. Do not put
`--cwd`, `-c`, or `--config` in `args_template`: taskbench sets the working
directory itself. Do not put `--agent` in `args_template`: taskbench does not
pass `--agent`. The control command above still
passes `--cwd` because that command is Grok directly, outside taskbench.
Restart taskbench from this window:

```powershell
python -m taskbench --config .\config.json
```

Queue a trivial task whose prompt is "Reply with the single word hello."
A .cmd Grok shim starts correctly when that task reaches done and
`var\runtime\<id>.raw.log` shows the init line with permissionMode dontAsk.

## Remove

Run this even if an earlier step failed. It recomputes the profile paths.
It restores a file only when the probe marker or the rule is present, and
then renames that backup to `*.used`. A backup is not reused. If nothing is
planted, cleanup does not restore, so newer edits stay. When no backup
exists, it removes only the `# BEGIN TASKBENCH-PROBE` / `# END TASKBENCH-PROBE`
block, or the exact JSON allow string, and it does not delete a file that
existed before the probe. A file the script created, recorded by
`*.taskbench-created`, is deleted only when it contains nothing but the
probe rule. A second run finds no probe marker and does not write the file.
The script also removes the probe folders `C:\taskbench-probe` and
`$env:USERPROFILE\taskbench-probe`. It does not clear `XAI_API_KEY`, so a
cleanup between the two plantings leaves the key in place. The confirm
block below clears the key.

```powershell
.\scripts\laptop-probe.ps1 -Cleanup -DryRun
.\scripts\laptop-probe.ps1 -Cleanup
```

Confirm, in this same window. This block recomputes the paths again.

```powershell
$grokToml = Join-Path (Join-Path $env:USERPROFILE ".grok") "config.toml"
$claudeJson = Join-Path (Join-Path $env:USERPROFILE ".claude") "settings.json"
Select-String -LiteralPath $grokToml, $claudeJson -Pattern "TASKBENCH-PROBE" -ErrorAction SilentlyContinue
Remove-Item -LiteralPath "C:\taskbench-probe" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $env:USERPROFILE "taskbench-probe") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item Env:\XAI_API_KEY -ErrorAction SilentlyContinue
```

`Select-String` must print nothing. Both probe folders must be gone.
`$env:XAI_API_KEY` must be empty. The live `config.toml` and `settings.json`
must still exist when they existed before the probe. If the script created
one of them, that file is gone and so is its `.taskbench-created` sidecar.
Retired backups end in `.used`. After you have compared them to the live
files, you may delete only the backup files, including retired `.used`
copies:

```powershell
$grokDir = Join-Path $env:USERPROFILE ".grok"
$claudeDir = Join-Path $env:USERPROFILE ".claude"
foreach ($dir in @($grokDir, $claudeDir)) {
  if (-not (Test-Path -LiteralPath $dir)) { continue }
  Get-ChildItem -LiteralPath $dir -Force -File |
    Where-Object { $_.Name -like "*.taskbench-backup-*" } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName }
}
```

That last loop deletes backups only. It does not delete `config.toml` or
`settings.json`.

## If Grok still honors the real profile

The public docs do not mention the Windows known-folder API. If this run
shows Grok reading `$env:USERPROFILE\.grok` or `$env:USERPROFILE\.claude`
anyway (for example it allows `echo TASKBENCH-PROBE-1` only because that
rule exists in the real profile), the pin did not hide the profile.

Treat the Windows sandbox as absent. Keep the banner. Keep tasks on `plan`
or `ask`. `ask` stays blocked until a runner can actually ask this page.
Do not enable always-approve to "make the task run". Run
`.\scripts\laptop-probe.ps1 -Cleanup` before you do anything else.

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
7. A .cmd Grok shim starts correctly. If `Get-Command grok | Select Source`
   is an `.exe`, item 7 is N/A.
