# SPEC-ADDITIONS

Additions to `joy-feeder.task/v2` that are not in the original field table.
The watcher is still the only writer of `status`.

## `workdir`

Optional string. An absolute path to the folder for this one task.

When `workdir` is absent, `project` is a folder name under the taskbench
projects root, as in the v2 spec.

When `workdir` is present:

- taskbench resolves symlinks and Windows junctions first
  (`os.path.realpath` with strict checks), then rejects a path that does
  not exist, is not a directory, is a drive root, is the user profile root
  itself, or is inside a system folder.
- The resolved folder is the project root for the allow-list, for
  `INSTRUCTIONS.md`, for `<folder>/.taskbench/LAST_AGENT.md`, and for the
  scan of `.grok` and `.claude` permission files.
- The resolved path is shown on the task and in the approval prompt.
- `from: web-ui` may introduce a new valid folder. taskbench records it in
  the local data directory (`workdir-registry.json`). That file is not part
  of the project repo.
- Any other `from` may use only a folder already in that registry. An
  unregistered path goes to `needs-approval`. The page shows **Needs you**
  and the resolved path. It is not added to the registry until the owner
  approves that folder.
- A rejected path (missing, not a directory, drive root, profile root, or
  system folder, including a symlink or junction that lands there) is
  `blocked` with reason `invalid workdir`.

`project` is still required and is still a short name, not a path.
