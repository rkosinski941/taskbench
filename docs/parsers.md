# Parsers

Grok settings are read with the standard library.

- TOML (`.grok/config.toml`) uses `tomllib.loads`. That module is in the
  standard library from Python 3.11, which is taskbench's minimum. The
  check is `taskbench.cli.require_python`, `pyproject.toml`
  (`requires-python >= 3.11`), `scripts/install.ps1`, and the CI matrix
  (3.11, 3.12, and 3.13 on Ubuntu and Windows).
- JSON (`.claude/settings.json`, `.claude/settings.local.json`,
  `config.json`, and watcher-owned packets) uses `json.loads`. Duplicate
  keys in a packet are rejected with `object_pairs_hook`.

There is no hand-written TOML or JSON parser.

## YAML subset, then JSON on disk

Incoming `joy-feeder.task/v2` packets may be YAML. The copy of that spec
in `docs/joy-feeder-task-v2.md` is YAML, and the standard library has no
YAML parser. `taskbench/yaml_subset.py` reads that subset only:

- lines are split on `\n` after `\r\n` is normalized to `\n` (a lone `\r`,
  U+0085, U+2028, or U+2029 is not a new line)
- the writer escapes `\r`, U+0085, U+2028, U+2029, vertical tab, form feed,
  and other control characters as a double-quoted JSON scalar
- duplicate keys are an error
- anchors (`&`), aliases (`*`), and tags (`!`, `!!`) are rejected

After the watcher accepts a packet it stores that copy as JSON
(`ensure_ascii`). Summary and retro live in that JSON and in
`runtime/<id>.model.json`. They are not written back through the YAML
subset, so model text cannot add a `permissions` or `workdir` key.
