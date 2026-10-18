# taskbench

taskbench is a local task runner for one machine. It watches an inbox of
`joy-feeder.task/v2` YAML packets, runs one of them at a time, and serves a
web page on `127.0.0.1` only. Python 3.10+ and the standard library are enough.
There is no third-party package.

This replaces the Enjoy desktop pairing flow. The page and the watcher are the
same process on the same computer. Closing the browser does not stop a run.

## Run

```text
python -m taskbench --config config.json
```

Copy `config.example.json` to `config.json` and point `runners.grok-build` at
the Grok Build executable you actually have. `python -m taskbench check` prints
the bind address and project directory and does not start a run.

`scripts/install.ps1` does not install taskbench and does not change the
system unless you pass `-WithTailscale`. See
[Optional: remote access via Tailscale Serve](#optional-remote-access-via-tailscale-serve).

Put each project in `projects/<name>/`. If that folder contains
`INSTRUCTIONS.md`, the watcher puts it at the front of the prompt. Drop a
packet in `var/inbox/`. Examples live in `examples/`.

## Layout

| Path | Status |
|---|---|
| `var/inbox/` | `incoming` |
| `var/claimed/` | `claimed` (queued) |
| `var/running/` | `running` |
| `var/needs-approval/` | `needs-approval` |
| `var/outbox/<id>.yaml` | `done`, `failed`, `blocked`, `timed-out` |
| `var/outbox/<id>.prompt.md` | prompt that was sent |
| `var/outbox/<id>.output.log` | stdout, stderr, and the final status |
| `projects/<name>/.taskbench/LAST_AGENT.md` | watcher handoff, every terminal state |

The watcher is the only writer of `status`. A transition writes the
destination file, fsyncs it, then deletes the source. On startup, leftover
`.tmp` files are removed. If a crash left the same id in two places, the
further state is kept. A packet left in `running` is marked `failed` and is
not started again. A packet left in `needs-approval` stays there so a person
can still answer; the dead process group is killed first.

## Safety

- Default `permissions` is `ask`. Missing or unknown values are stored as `ask`.
- `plan` (and a runner with `sandbox_only`) may list, read, and diff inside
  the project. Writes, shell commands, and servers are refused.
- `allow-list` runs a command only when a glob matches the whole command.
  A `*` does not match `;`, `&`, `|`, or command substitution unless the
  pattern itself contains that separator. Servers still have to match an
  allow entry and bind `127.0.0.1` or `localhost`.
- Paths are resolved and must stay inside the project. `..`, absolute paths,
  UNC paths, drive letters, NUL, and symlinks that land outside are rejected.
  Writes under `<project>/.taskbench` are refused.
- The page binds to `127.0.0.1` and refuses any other address. `Host` must be
  `127.0.0.1` or `localhost` (with the port), plus names listed in
  `allowed_hosts` (empty by default). POST checks a per-session CSRF cookie.
- Grok Build is pinned to `--permission-mode dontAsk` and `--sandbox strict`.
  Those flags are placed immediately after the executable. A later copy is
  removed. `--` (end of options) is rejected in `args_template` and in the
  built argv. `can_ask` defaults to false, so `ask` and `allow-list` packets
  are blocked before the process starts. `--always-approve`, `--yolo`,
  `--allow`, `--auto-approve`, and any other permission flag are rejected.
- Mode detection is best-effort. The real Grok CLI does not emit
  `TASKBENCH` permission lines. The review named an init `permissionMode`
  and did not include a captured line, so taskbench does not guess from
  ordinary stdout. A run that never reports a mode is not proof of
  `dontAsk`. If a structured event does report another mode or sandbox,
  taskbench kills the process and marks the packet `failed`. On Linux,
  `/proc` is checked only to confirm the argv taskbench launched. That
  check does not exist on Windows.
- Before each run, taskbench scans the project for `.grok/config.toml`,
  `.claude/settings.json`, and `.claude/settings.local.json`, including
  nested directories. A non-empty `allow`, a true `yolo` / `always-approve`
  / `auto-approve` / `bypassPermissions`, or a `permission_mode` /
  `defaultMode` other than `dontAsk` blocks the task. The status stays
  `blocked`. The page shows **Needs you** and names the file and the rule
  keys, not the rule values. Deny-only files still run. On Linux, user-level
  `~/.grok` is not replaced, because `--sandbox strict` is the OS boundary
  there. `--permission-mode dontAsk` overrides a config `permission_mode`
  or `yolo`, but an explicit allow rule in the user config can still
  auto-approve inside that sandbox. `/etc/grok` cannot be redirected.
- Windows has no OS sandbox. Grok's `--sandbox strict` is Landlock and
  Seatbelt only, and the CLI continues when it cannot apply a sandbox.
  taskbench still runs on Windows. It shows `No OS sandbox on Windows; only taskbench approvals protect you`
  on the page and as the first line of each run's output file. For each
  Grok launch it recreates an empty `var/grok-home` and sets `HOME`,
  `USERPROFILE`, and `GROK_HOME` to that directory (and `HOMEDRIVE` /
  `HOMEPATH` when the path has a drive). The review names `HOME` and
  `USERPROFILE` as the variables that select `~/.grok` and `~/.claude`.
- The runner and any sidecar share an environment allow-list (`PATH`,
  locale, and a few Windows system variables). `GROK_*`, `PYTHONPATH`,
  `XDG_CONFIG_HOME`, and similar parent overrides are not passed through.
  A sidecar does not inherit the parent environment. On Windows the empty
  grok home is applied only to the Grok process, not to the sidecar.
- If the Windows job object or the POSIX process group cannot be set up,
  the task is `failed` and nothing is left running. A sidecar that cannot
  join that same job or group is killed instead of being left unconfined.
  A `grok.cmd` or `.bat` shim is started as `cmd.exe /d /s /c` with a
  quoted argument list. taskbench does not use `shell=True`. An argument
  that contains a cmd metacharacter is refused.
- There is no config key or CLI flag for bypass, yolo, or auto-approve.
  Runner argument templates that contain those words are rejected.
- Cancel, timeout, a normal exit, a failure, and watcher shutdown kill the
  process tree: a Windows Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`,
  or a POSIX process group killed with `killpg`. On Linux the child also gets
  `PR_SET_PDEATHSIG` so a hard crash of taskbench delivers `SIGKILL`.
- The page has no preview pane. Artifacts are links to files. An approved
  loopback server is a link to `http://127.0.0.1:<port>/`, not an iframe.
- `from` is the sending bot's name. Empty, or equal to `human_owner`
  (default `Rich`), is `blocked` with `invalid from`. Tasks created on the
  page are `from: web-ui` even if the form posts another name.
- `timeout_s` defaults to 900. Values below 1, above 7200, or not an integer
  are rejected. They are not clamped. The clock starts when the run starts
  and keeps running through `needs-approval`.
- `model.actual` is whatever the runner reports. If it reports nothing, the
  value is `unknown`. The requested model is never copied into `actual`.

## Runners

v1 implements the Grok Build adapter. Antigravity and opencode are stubs:
packets that request them are `blocked` and the process is not started.

Set `executable` and `args_template` yourself. Placeholders are
`{prompt_file}`, `{project_dir}`, and `{timeout_s}`. taskbench puts
`--permission-mode dontAsk` and `--sandbox strict` first, after the
executable, and refuses other permission flags and `--`. The child gets
`TASKBENCH_APPROVALS=1` and no Grok variables from the parent. On Windows
those config-dir variables are then set to the empty grok home. The real
CLI does not speak the permission lines in the protocol below; that check
is best-effort only.

Assumed stdout protocol, one JSON object per line, prefixed with `TASKBENCH `:

```text
TASKBENCH {"type":"model","model":"grok-4.7"}
TASKBENCH {"type":"action","action":{"op":"shell","command":"git status"}}
TASKBENCH {"type":"result","summary":"...","artifacts":[]}
TASKBENCH {"type":"retro","effort":"...","unclear":"...","would_change":"...","fit_1_5":4}
TASKBENCH {"type":"done"}
```

taskbench answers each action with one JSON line on stdin. If the CLI runs
commands itself, taskbench cannot see them. Do not point the adapter at a
mode that self-approves.

A usage-limit line names the runner that reported it and blocks the packet.
An exit with no result is `failed`, with the exit code and the last output
lines. It is never `done`.

## Assumptions

Where `docs/joy-feeder-task-v2.md` is silent:

- A schema other than `joy-feeder.task/v2` is blocked with
  `unknown schema '...'` and is never run. v1 packets (schema
  `joy-feeder.task/v1`, or a mapping `from`) are stored as blocked v2
  packets with reason `v1 packet: set runner and project`.
- The Grok Build wire protocol above is an assumption. Tests drive it with
  `tests/fixtures/fake_runner.py`.
- `LAST_AGENT.md` is written by the watcher, not by the runner, so a kill
  still leaves a handoff.

## Tests

```text
python -m unittest discover -s tests -v
```

`docs/enjoy-breakages.md` is the list of twelve scenarios. GitHub Actions
runs the suite on Ubuntu and Windows and runs gitleaks. Nothing in this repo
is a credential. Keep machine names, tailnet hostnames, and tokens out of
commits. `config.json` is gitignored; `config.example.json` is the sample.

## Optional: remote access via Tailscale Serve

This is off by default. Do not run it until the machine owner has agreed.

taskbench keeps listening on `127.0.0.1` only. Tailscale Serve, on the same
machine, proxies a tailnet HTTPS name to that loopback port. The public
internet is not involved. Tailscale Funnel would publish the port publicly,
and this setup refuses that.

1. Copy `config.example.json` to `config.json` and choose `port` (default
   `8765`). Leave `allowed_hosts` empty until you know the tailnet hostname.
   Leave `bind_host` as `127.0.0.1`.
2. From the repo root, review the helper without changing the system:

   ```text
   powershell -File scripts/install.ps1 -WhatIf
   ```

   With no `-WithTailscale` switch the script prints how to start taskbench
   and exits. It does not install software. `-WhatIf` still prints the Funnel
   check it would run (serve status, serve status --json, funnel status).
   A skipped check is not reported as Funnel on. The script sets `PYTHONPATH`
   only for the checker process and restores it before returning.
3. When you want the tailnet proxy, and only then:

   ```text
   powershell -File scripts/install.ps1 -WithTailscale -WhatIf
   powershell -File scripts/install.ps1 -WithTailscale
   ```

   `-WithTailscale` prints two winget commands if `tailscale` is not on
   `PATH`. It tries `winget install --id Tailscale.Tailscale -e --scope user`
   first. If that fails, it retries the same install without `--scope user`
   and says an administrator is required. The script does not elevate
   itself and does not call `Start-Process -Verb RunAs`. It then runs
   `tailscale serve` for the port in the config so the page is reachable
   on the tailnet.
4. The script reads `tailscale serve status`, `tailscale serve status --json`,
   and `tailscale funnel status`. A single Funnel that is tied to
   `127.0.0.1:<port>` is turned off with `tailscale funnel --https=443 off`
   only. A Funnel for a different service, or more than one Funnel, stops
   the script and is left alone. Not logged in, or an unreadable status, is
   not reported as "FUNNEL IS ON". The port match does not treat `:87650`
   as `:8765`. The script never turns Funnel on. Running it again is safe:
   an existing serve rule for the same port is left in place.
5. Put the machine's tailnet hostname in `allowed_hosts` and restart
   taskbench. The Host check then accepts that name in addition to
   `127.0.0.1` and `localhost`. CSRF is unchanged. `tailscale status --json`
   shows the hostname; do not commit it.
6. Tailscale may send `Tailscale-User-Login`. The page shows it when the
   header is present. That display is trustworthy only because the listener
   is loopback and Serve is the only client that should be able to connect.
   The assumption breaks if taskbench is ever bound to any other address,
   because then anyone who can reach the port can forge the header. The
   header is not used to authorize an approval, and it is not written into
   `from`. A task created on the page stays `web-ui`. A bot packet stays the
   bot named in the file.

If the computer should keep running while it is plugged in, set sleep to
never on AC power: Settings > System > Power, or
`powercfg /change standby-timeout-ac 0`.

## Working directory

`project` stays a short name. Optional `workdir` is an absolute folder for
that one task. When it is set, it is the root for the allow-list, for
`INSTRUCTIONS.md`, for `.taskbench/LAST_AGENT.md`, and for the project
allow-rule scan. The page shows the resolved path on the task and in the
approval prompt.

The folder field lists recent folders and accepts a typed path. On a
loopback page, Browse opens a local folder dialog when tkinter is
available. That control is not shown for a tailnet Host, and the picker
route refuses any host that is not `127.0.0.1` or `localhost`.

Symlinks and Windows junctions are resolved with `os.path.realpath` before
the path is accepted. Missing paths, files, drive roots, the user profile
root itself, and system folders are rejected. System folders include
`Windows`, `System32`, `Program Files`, `Program Files (x86)`, and
`ProgramData` on Windows, and `/`, `/etc`, `/usr`, `/bin`, and `/System`
on POSIX.

A task with `from: web-ui` may use a new valid folder. taskbench stores
the resolved path in its data directory (`workdir-registry.json`), not in
the repo. Any other sender may use only a folder already in that registry.
An unregistered bot path is `blocked`, shown as **Needs you**, with the
resolved path in the message, and it is not added to the registry.

See `docs/SPEC-ADDITIONS.md`.

## v2

Antigravity and opencode adapters, once their approval protocol is known.
v1 will not launch them.
