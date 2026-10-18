# Grok CLI permission mode

taskbench pins Grok Build to `--permission-mode dontAsk`, `--sandbox strict`,
and `--output-format streaming-messages-json`. The watcher reads
`permissionMode` from the `system` / `init` event. If that event is missing,
or `permissionMode` is missing or is not `dontAsk`, taskbench kills the
process tree and marks the run `failed`.

These notes are from public docs fetched on 2026-10-18. taskbench does not
ship a real `grok` binary, and these tests do not launch one.

## Flags and the init event

- Output formats on the product headless page are `plain`, `json`, and
  `streaming-json`:
  <https://docs.x.ai/build/cli/headless-scripting>
- The Grok Build user guide also lists `streaming-messages-json`. Its
  `system` / `init` line is the one that carries `permissionMode`. Native
  `streaming-json` events in that guide (`text`, `tool_call`, `usage`,
  `end`) do not document `permissionMode`. taskbench therefore pins
  `streaming-messages-json`, not `streaming-json`.
  <https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md>
- That guide says `permissionMode` is the `--permission-mode` value, or
  `bypassPermissions` under `--yolo`, otherwise `default`. It says Grok-only
  modes such as `auto` collapse to `default`. The sample init line is
  `{"type":"system","subtype":"init",...,"permissionMode":"default",...}`.
  The init line is deferred until the first output line.

## Why `dontAsk` stays

`dontAsk` is a documented mode. The permissions guide lists it as "Only
pre-approved tools and built-in read-only handling" for strict CI allowlists:

<https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/22-permissions-and-safety.md>

The enterprise page shows a headless example that passes
`--permission-mode dontAsk` together with `--sandbox strict`:

<https://docs.x.ai/build/enterprise>

The product permissions page also points headless modes such as `dontAsk` at
that enterprise section:

<https://docs.x.ai/build/features/permissions>

`dontAsk` is not dropped. `auto` is not accepted: the headless guide says it
collapses to `default`, and `default` is not the pinned mode. `bypassPermissions`
(always-approve) is not accepted.

## Uncertainty

The same headless flag table says `--permission-mode` turns on always-approve
when the value is `bypassPermissions`, and it points deny-by-default at
`defaultMode` in `.claude/settings.json` rather than at the CLI flag. The
enterprise example still passes `--permission-mode dontAsk`, and the init-field
note says `permissionMode` echoes the `--permission-mode` value.

taskbench passes the flag and fails closed if the init event is absent or
`permissionMode` is not `dontAsk`. If a build collapses `dontAsk` to `default`
in that field, the run fails. That is the check. taskbench does not treat
`default` as success, and it does not write a project `.claude/settings.json`
to force `defaultMode`.

The real `grok` binary has to confirm this. The tests use a stub that prints
the init line. A missing event, an empty `permissionMode`, or `default` is a
failed run, not a successful one. Do not treat the stub as that confirmation.

The OS sandbox behind `--sandbox strict` is Linux Landlock and macOS Seatbelt.
The permissions guide's sandbox page is the boundary on those systems. Windows
has no such sandbox; see `docs/grok-cli-home-and-auth.md`.
