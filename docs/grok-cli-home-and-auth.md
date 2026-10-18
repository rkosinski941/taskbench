# Grok CLI home and auth on Windows

taskbench does not refuse to run Grok on Windows. There is no OS sandbox
there. Each Grok launch gets an empty directory, and the page and the output
file start with:

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
or any other Windows known-folder API. taskbench still pins `HOME`,
`USERPROFILE`, `GROK_HOME`, and, when the path has a drive, `HOMEDRIVE` and
`HOMEPATH`, at a directory it recreates empty before the launch.

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

Preferred order, matching that precedence:

1. If `XAI_API_KEY` is set in the parent environment, it is copied through
   the environment allow-list. `auth.json` is not copied. Copying it would
   outrank the API key.
2. Otherwise taskbench copies only `auth.json` into the pinned home. The
   source is `$GROK_HOME/auth.json` when that variable is set, otherwise
   `%USERPROFILE%\.grok\auth.json` or `$HOME/.grok/auth.json`. `config.toml`,
   `settings.json`, and every other file stay behind.

Linux and macOS do not get this empty home. `--sandbox strict` is the OS
boundary there, and replacing `HOME` would drop that boundary. The allow-rule
scan still walks the workdir and its parents on every OS.

## If the pin is not enough

The docs do not say Grok resolves the profile with the Windows known-folder
API. They also do not promise that it ignores `USERPROFILE` after `GROK_HOME`
is set. `docs/laptop-test-plan.md` is the manual check.

If that check shows Grok still reads the real profile, treat the Windows
sandbox as absent. Keep the banner. Keep packet permissions on `plan` (or
`ask`, which taskbench blocks before launch because `dontAsk` cannot ask
this page). Do not switch the pin to `bypassPermissions`, `--yolo`, or
`--always-approve`.
