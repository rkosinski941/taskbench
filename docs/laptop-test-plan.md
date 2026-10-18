# Laptop test: does Grok see the real profile?

Run this on the owner's Windows machine. It plants a harmless allow rule in
the real profile, runs one taskbench task, and then removes the rule. Do not
leave the rule in place. Do not put a secret, a token, or a real command you
care about in the rule value. Use a marker string such as `TASKBENCH-PROBE`.

taskbench's own scanner walks parents up to the profile. A workdir under
`%USERPROFILE%` will be blocked with **Needs you** before Grok starts, because
the planted rule is visible to taskbench. That result does not show what Grok
would have done. Use a folder that is not under the profile so the parent
walk stops at the drive root.

## Plant

1. Note the current contents of these files if they exist, so you can restore
   them exactly:
   - `%USERPROFILE%\.grok\config.toml`
   - `%USERPROFILE%\.claude\settings.json`
2. Add only this marker, and nothing else, to `%USERPROFILE%\.grok\config.toml`:

   ```toml
   allow = ["TASKBENCH-PROBE"]
   ```

   If the file already has an `allow` key, do not edit it. Stop and pick a
   scratch Windows user instead of changing a real allow list.
3. Add only this object to `%USERPROFILE%\.claude\settings.json`:

   ```json
   {"permissions": {"allow": ["TASKBENCH-PROBE"]}}
   ```

   Same rule: stop if an allow list is already there.

## Run outside the profile

1. Create `C:\taskbench-probe` (or another drive's empty folder). It must not
   sit under `%USERPROFILE%`, `Documents`, `Desktop`, `Downloads`, `OneDrive`,
   or `AppData`.
2. Start taskbench from a checkout that does not use that folder as its
   install, config, or `var` directory.
3. On the loopback page, register `C:\taskbench-probe` and queue a `plan`
   task whose folder is that path. Leave packet permissions on `plan`.
4. Open the task's output file.

What you want: Grok starts, the output's first line is
`No OS sandbox on Windows; only taskbench approvals protect you`, and nothing
in the output or the packet shows Grok treating `TASKBENCH-PROBE` as an allow
rule. taskbench's status for a clean `plan` run is `done` only if the stub
or Grok finished without auto-approving a write. A real Grok that tries to
write still has to come through taskbench, which refuses writes in `plan`.

What the empty home is for: Grok's documented Windows config path is
`%USERPROFILE%\.grok\config.toml`, overridable with `GROK_HOME`. taskbench
points `HOME`, `USERPROFILE`, and `GROK_HOME` at an empty directory and does
not copy `config.toml`. See `docs/grok-cli-home-and-auth.md`.

## Remove

1. Delete the `TASKBENCH-PROBE` lines you added, or restore the two files
   from the copies you made.
2. Confirm both files no longer contain `TASKBENCH-PROBE`.
3. Delete `C:\taskbench-probe` if you created it only for this check.

## If Grok still honors the real profile

The public docs do not mention the Windows known-folder API. If this run
shows Grok reading `%USERPROFILE%\.grok` or `%USERPROFILE%\.claude` anyway
(for example it auto-approves a command that exists only in that planted
rule), the pin did not hide the profile.

Treat the Windows sandbox as absent. Keep the banner. Keep tasks on `plan`
or `ask`. `ask` stays blocked until a runner can actually ask this page.
Do not enable always-approve to "make the task run". Remove the planted
rule before you do anything else.
