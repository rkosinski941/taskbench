# Laptop test: does the pinned home hide the real profile?

Run this on the owner's Windows machine. It plants a real Bash allow rule
in the real profile, asks Grok to run one echo, and then removes the rule.
Do not leave the rule in place. Do not put a secret or a command you care
about in the rule.

taskbench does not sit in the path of Grok's own tool calls. When the real
`grok` binary runs a command, Grok's permission mode and sandbox decide.
taskbench mediates an action only when the runner speaks the TASKBENCH
protocol and waits. The pins that matter here are `--permission-mode
dontAsk`, the empty `GROK_HOME`, and the allow-rule scan.

taskbench's scanner walks parents up to the profile. A workdir under
`%USERPROFILE%` is blocked with **Needs you** before Grok starts, because
the planted rule is visible to taskbench. That result does not show what
Grok would have done. Use a folder that is not under the profile so the
parent walk stops at the drive root.

## Plant

1. Note the current contents of these files if they exist, so you can restore
   them exactly:
   - `%USERPROFILE%\.grok\config.toml`
   - `%USERPROFILE%\.claude\settings.json`
2. Add only this marker, and nothing else, to `%USERPROFILE%\.grok\config.toml`:

   ```toml
   allow = ["Bash(echo TASKBENCH-PROBE*)"]
   ```

   If the file already has an `allow` key, do not edit it. Stop and pick a
   scratch Windows user instead of changing a real allow list.
3. Add only this object to `%USERPROFILE%\.claude\settings.json`:

   ```json
   {"permissions": {"allow": ["Bash(echo TASKBENCH-PROBE*)"]}}
   ```

   Same rule: stop if an allow list is already there.

## Prove the probe outside taskbench

1. In a terminal that is not taskbench, with the real profile (do not set
   `GROK_HOME`), run Grok so it executes `echo TASKBENCH-PROBE-1`.
2. Expect that command to be allowed. If it is denied, the planted rule
   does not match and this test does not prove anything. Remove the rule
   and stop.

## Run under taskbench

1. Create `C:\taskbench-probe` (or another drive's empty folder). It must not
   sit under `%USERPROFILE%`, `Documents`, `Desktop`, `Downloads`, `OneDrive`,
   or `AppData`.
2. Start taskbench from a checkout that does not use that folder as its
   install, config, or `var` directory. Open the one-time URL printed in
   that console.
3. On the loopback page, register `C:\taskbench-probe` and queue a task
   whose folder is that path and whose prompt tells Grok to run
   `echo TASKBENCH-PROBE-1`. Leave packet permissions on `plan`.
4. Open the task's output file.

What you want: the command is denied, or the task is sent to **Needs you**,
under the pinned home. The output's first line on Windows is
`No OS sandbox on Windows; only taskbench approvals protect you`. The
packet must not show Grok treating `Bash(echo TASKBENCH-PROBE*)` as an
allow rule it loaded from the real profile.

What the empty home is for: Grok's documented Windows config path is
`%USERPROFILE%\.grok\config.toml`, overridable with `GROK_HOME`. taskbench
points `HOME`, `USERPROFILE`, and `GROK_HOME` at an empty directory and does
not copy `config.toml`. See `docs/grok-cli-home-and-auth.md`.

taskbench does not intercept a write or a shell command that the real Grok
binary runs by itself. `plan` on the packet does not stand in front of
those calls. The denial you want here comes from `dontAsk` plus the empty
pinned home, not from taskbench executing the echo.

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
