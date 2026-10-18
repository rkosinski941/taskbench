# Laptop test: does the pinned home hide the real profile?

Run this on the owner's Windows machine. It plants one Bash allow rule in
the real profile, asks Grok to run one echo, and then removes the rule.
Do not leave the rule in place. Do not put a secret or a command you care
about in the rule.

taskbench does not sit in the path of Grok's own tool calls. When the real
`grok` binary runs a command, Grok's permission mode and sandbox decide.
taskbench mediates an action only when the runner speaks the TASKBENCH
protocol and waits. The pins that matter here are `--permission-mode
dontAsk`, `--sandbox strict`, the pinned `GROK_HOME`, and the allow-rule scan.

taskbench's scanner walks parents up to the profile. A workdir under
`%USERPROFILE%` is blocked with **Needs you** before Grok starts, because
the planted rule is visible to taskbench. That result does not show what
Grok would have done, and it is not a pass. Use a folder that is not under
the profile so the parent walk stops at the drive root and Grok actually
starts.

The evidence for every Grok decision below is `var\runtime\<id>.raw.log`.
The outbox output file is a redacted summary. It is not the evidence.

## Plant one rule at a time

Run the whole probe twice. The first run plants only the `.grok` rule.
The second run plants only the `.claude` rule. Do not plant both at once.
Remove the first rule and confirm it is gone before you plant the second.

1. Note the current contents of these files if they exist, so you can restore
   them exactly:
   - `%USERPROFILE%\.grok\config.toml`
   - `%USERPROFILE%\.claude\settings.json`
2. First run only. Add only this marker, and nothing else, to
   `%USERPROFILE%\.grok\config.toml`:

   ```toml
   allow = ["Bash(echo TASKBENCH-PROBE*)"]
   ```

   Leave `%USERPROFILE%\.claude\settings.json` without a `TASKBENCH-PROBE`
   rule. If the `.grok` file already has an `allow` key, do not edit it.
   Stop and pick a scratch Windows user instead of changing a real allow list.
3. Second run only. Restore the `.grok` file, then add only this object to
   `%USERPROFILE%\.claude\settings.json`:

   ```json
   {"permissions": {"allow": ["Bash(echo TASKBENCH-PROBE*)"]}}
   ```

   Leave the `.grok` file without a `TASKBENCH-PROBE` rule. Same stop rule
   if an allow list is already there.

## Prove the probe outside taskbench

The control run is Grok directly, outside taskbench, with the real profile
(do not set `GROK_HOME`). It must use the same flags taskbench pins:

```text
grok --permission-mode dontAsk --sandbox strict --output-format streaming-messages-json
```

`--permission-mode dontAsk` and `--output-format streaming-messages-json` are
required. `--sandbox strict` is included because taskbench always sends that
pin too.

Ask that process to execute `echo TASKBENCH-PROBE-1`.

Expect that command to be allowed. If it is denied, the planted rule does
not match and this test does not prove anything. Remove the rule and stop.

## Run under taskbench

1. Create `C:\taskbench-probe` (or another drive's empty folder). It must not
   sit under `%USERPROFILE%`, `Documents`, `Desktop`, `Downloads`, `OneDrive`,
   or `AppData`.
2. Start taskbench from a checkout that does not use that folder as its
   install, config, or `var` directory. Open the one-time URL printed in
   that console. Leave `copy_auth_json` at its default, false, so no
   `auth.json` is copied. Set `XAI_API_KEY` for the Grok process.
3. On the loopback page, register `C:\taskbench-probe` and queue a task
   whose folder is that path and whose prompt tells Grok to run
   `echo TASKBENCH-PROBE-1`. Leave packet permissions on `plan`.
4. Open `var\runtime\<id>.raw.log` for that task id.

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
`%USERPROFILE%\.grok\config.toml`, overridable with `GROK_HOME`. taskbench
points `GROK_HOME` at `var\grok-home` on every OS. On Windows it also points
`HOME`, `USERPROFILE`, `HOMEDRIVE`, and `HOMEPATH` there. It does not copy
the real `config.toml`. See `docs/grok-cli-home-and-auth.md`.

taskbench does not intercept a write or a shell command that the real Grok
binary runs by itself. `plan` on the packet does not stand in front of
those calls. The denial you want here comes from `dontAsk` plus the pinned
home and its deny-read rules, not from taskbench executing the echo.

## Confirm the deny-read rules

After one of the runs above, open these files under the pinned home:

- `var\grok-home\.claude\settings.json`
- `var\grok-home\.grok\config.toml`
- `var\grok-home\config.toml`

Each one is a deny list. The entries are `Read(<path>)` and
`Read(<path>/**)` for the pinned home and for the real profile's
`%USERPROFILE%\.grok` and `%USERPROFILE%\.claude` (forward-slash forms
included). They do not set `defaultMode` and they are not a project
`.claude/settings.json`.

Queue a second task whose prompt tells Grok to read one of those paths,
for example `%USERPROFILE%\.grok\config.toml`. PASS for this check uses the
same rule as the echo: Grok denies the read, and `var\runtime\<id>.raw.log`
shows that denial. Sent to **Needs you** does not count as a pass.

## Remove

1. Delete the `TASKBENCH-PROBE` lines you added, or restore the two files
   from the copies you made.
2. Confirm both files no longer contain `TASKBENCH-PROBE`.
3. Delete `C:\taskbench-probe` if you created it only for this check.

## If Grok still honors the real profile

The public docs do not mention the Windows known-folder API. If this run
shows Grok reading `%USERPROFILE%\.grok` or `%USERPROFILE%\.claude` anyway
(for example it allows `echo TASKBENCH-PROBE-1` only because that rule
exists in the real profile), the pin did not hide the profile.

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
