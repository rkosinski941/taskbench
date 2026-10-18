# Grok CLI home and auth on Windows

taskbench does not refuse to run Grok on Windows. There is no OS sandbox
there. Each Grok launch gets a directory taskbench recreates, and the page
and the output file start with:

`No OS sandbox on Windows; only taskbench approvals protect you`

These notes are from public docs fetched on 2026-10-18. They are not a trace
from a Windows binary.

## Where Grok looks for its home

The settings page says config lives at `~/.grok/config.toml`, and on Windows
that path is `%USERPROFILE%\.grok\config.toml`. `$GROK_HOME` overrides the
home. When `GROK_HOME` is unset, the example config is
`~/.grok/config.toml`.

- <https://docs.x.ai/build/settings>
- <https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md>
  (`GROK_HOME` overrides the config directory; default `~/.grok`)

The public pages name `USERPROFILE` for the Windows path and `GROK_HOME` as
the override. They do not mention `SHGetKnownFolderPath`, `FOLDERID_Profile`,
or any other Windows known-folder API. On Windows, taskbench pins `HOME`,
`USERPROFILE`, `GROK_HOME`, and, when the path has a drive, `HOMEDRIVE` and
`HOMEPATH`, at a directory it recreates before the launch. On Linux and
macOS it pins `GROK_HOME` only, so the real `HOME` (and `--sandbox strict`)
stay in place.

`/etc/grok/managed_config.toml` and `/etc/grok/requirements.toml` are
system layers. Pointing `GROK_HOME` elsewhere does not move them. On Windows
those Unix paths are usually absent. A managed file under the real profile
(`%USERPROFILE%\.grok\requirements.toml`) is not copied into the pinned home.

## How Grok authenticates

The authentication guide's precedence, highest first:

1. Per-model `api_key` or `env_key` in `config.toml`.
2. The session token in `~/.grok/auth.json` (browser, device code, OIDC, or
   an external auth provider).
3. `XAI_API_KEY`, only when no session token is active.

A stored session outranks `XAI_API_KEY`. The guide says to run `grok logout`
or delete `~/.grok/auth.json` before the API key is used.

- <https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/02-authentication.md>
- <https://docs.x.ai/build/enterprise> (same order: model key, then `env_key`,
  then the session, then `XAI_API_KEY`)
- <https://docs.x.ai/build/overview> describes first-launch browser auth and
  `XAI_API_KEY` for non-browser use.

The credentials file is `auth.json`. Settings live in `config.toml` and in
Claude-compatible `settings.json`. Those are not credentials.

## What taskbench does

API-key-only is the default on every OS. Copying `auth.json` is off unless
`copy_auth_json` is `true`.

1. `XAI_API_KEY` is not on the environment allow-list. After the scrub,
   taskbench adds it to the Grok process only. Companion processes do not
   receive it.
2. `auth.json` is not copied. A session token in that file outranks
   `XAI_API_KEY`, so the default leaves it behind.
3. Opt-in only: `copy_auth_json: true` copies `auth.json` even when an API
   key is also set. The page shows a warning when that flag is on. The
   source is `$GROK_HOME/auth.json` when that variable is set in the parent,
   otherwise `%USERPROFILE%\.grok\auth.json` or `$HOME/.grok/auth.json`.
   The user's `config.toml` and `settings.json` are still not copied.
   Prefer the default. Copying `auth.json` is the louder, worse choice.

The copy, when opted in, is written into a directory that is already locked
to the current user. POSIX creates the file as mode `0600`. Windows resolves
`icacls.exe` from `GetSystemDirectoryW` (not `PATH`), reads the user SID from
the process token (not `USERNAME`), locks the directory, then writes the
file. The stdlib cannot pass a security descriptor to `mkdir`, so there is a
short gap before `icacls`. The file is not created in that gap. taskbench
deletes `var/grok-home/auth.json` at startup and after every run.

## Deny-read rules

Every Grok launch, in every mode, writes deny-read rules into the pinned
home:

- `var/grok-home/.claude/settings.json`
- `var/grok-home/.grok/config.toml`
- `var/grok-home/config.toml`

The TOML files use `[permission]` (singular). That is the section in
<https://docs.x.ai/build/settings/reference>. The JSON file keeps the
Claude-compatible `permissions` object. The deny list is `Read(<path>)` and
`Read(<path>/**)` for the pinned home and for the real profile's `.grok` and
`.claude` (from `HOME` and `USERPROFILE` before those variables are replaced,
plus forward-slash forms when the path contains backslashes). On Windows each
path is also written with the drive and the rest of the path lowercased.
The rules do not set `defaultMode`.
They are not a project `.claude/settings.json`. `docs/laptop-test-plan.md`
says how to confirm Grok's own read tools deny those paths: the denial is
in `var\runtime\<id>.raw.log`, and **Needs you** does not count.

## Redaction is not the boundary

Published outbox text is passed through a best-effort redactor. It strips
zero-width characters, matches known secrets without case, and removes
base64 and url-safe base64 of those known values, plus a few token shapes.
A backslash-u escape, a secret split across lines, or a token taskbench
never saw (including one Grok refreshes) can still get through. Do not treat
redaction as a boundary. The boundary is API-key-only mode plus the
deny-read rules above.

## If the pin is not enough

The docs do not say Grok resolves the profile with the Windows known-folder
API. They also do not promise that it ignores `USERPROFILE` after `GROK_HOME`
is set. `docs/laptop-test-plan.md` is the manual check.

If that check shows Grok still reads the real profile, treat the Windows
sandbox as absent. Keep the banner. Keep packet permissions on `plan` (or
`ask`, which taskbench blocks before launch because `dontAsk` cannot ask
this page). Do not switch the pin to `bypassPermissions`, `--yolo`, or
`--always-approve`.
