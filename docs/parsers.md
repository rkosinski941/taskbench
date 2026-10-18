# Parsers

Grok settings are read with the standard library.

- TOML (`.grok/config.toml`) uses `tomllib.loads`. That module is in the
  standard library from Python 3.11, which is taskbench's minimum. The
  check is `taskbench.cli.require_python`, the README, and the CI workflow
  comment on the 3.12 runner.
- JSON (`.claude/settings.json`, `.claude/settings.local.json`, and
  `config.json`) uses `json.loads`.

There is no hand-written TOML or JSON parser.

## Why a YAML subset remains

`joy-feeder.task/v2` packets are YAML. The copy of that spec in
`docs/joy-feeder-task-v2.md` does not offer a JSON packet. `tomllib` cannot
parse YAML. The standard library has no YAML parser, and taskbench does not
take a third-party one.

`taskbench/yaml_subset.py` reads and writes only the subset the packets use:
mappings, sequences, scalars, and block strings. Anchors, tags, and merge
keys are rejected. That limit is the whole remaining home-made parser.
