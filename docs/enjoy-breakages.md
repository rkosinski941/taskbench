# Enjoy breakages: test list for taskbench

Things that went wrong in the previous tool, written as test scenarios for
Code quality scanner. Each item has a scenario and the behaviour taskbench
should show. Generic project and runner names only.

| # | Scenario | Expected taskbench behaviour |
|---|---|---|
| 1 | **Remote pairing drops.** The web page loses its link to the local watcher in the middle of a run. | taskbench has no pairing: the page and the watcher are on the same machine (127.0.0.1). If the page reconnects, it reloads state from the outbox and run logs. The run itself keeps going and doesn't depend on the page. |
| 2 | **Stale model label.** The UI shows model A, but the run actually used model B. | The UI shows `model.actual` as the runner reported it, next to `model.requested`, and flags any mismatch. If the runner can't report one, the label reads `unknown`, never the requested value. |
| 3 | **Wrong usage notice, silent stop.** Runner X runs out of usage, but the notice blames runner Y. Or a run stops mid-task with no message. | The limit is tied to the runner that reported it. The packet goes `blocked` with that runner named, the raw message, and the reset time if it's known. A runner exit with no result is never shown as `done`: it's `failed` with the exit code and the last output lines. |
| 4 | **Long approval title crashes.** A command over 500 characters is sent for approval, and the run errors before the prompt shows. | The title is cut to 120 characters and the full command shows in a scrollable box. Test with commands of 1, 500, 501, 5,000 and 50,000 characters: the prompt shows every time and the run doesn't error. |
| 5 | **A runner can't ask, so it fails.** A runner needs approval but has no approval channel, so the run dies. | The adapter declares whether it can ask. `ask` or `allow-list` packets for a runner that can't ask are refused before the run starts (`blocked`, reason `runner cannot ask`). They're never started and then left to fail. |
| 6 | **A mode runs commands with no prompt.** One runner's default mode runs shell commands without approval. | With `permissions` missing, the default is `ask`. Under `ask`, every command and write goes through the page. Test that each adapter launches its tool in a mode that hands approvals to taskbench, and that no command runs without a logged approval or an `allow` match. |
| 7 | **Unasked preview servers that hang the run.** The agent starts a local web server to preview its work, without asking, and the run hangs waiting on it. | Starting anything that listens on a port or keeps running is always an approval item. If it's allowed, it binds to 127.0.0.1. A run that's waiting on a long-lived child still ends at `timeout_s` (`timed-out`), and the server is killed with the process tree. |
| 8 | **Closing the app orphans children.** Closing the app killed the run, but its child processes (servers, watchers) kept running. | Closing the page does nothing to the run. Cancel, timeout, completion and watcher shutdown each kill the whole process tree. Afterwards, check that none of the run's processes are alive and that ports opened during the run are closed. |
| 9 | **A queued message is stuck behind a hung run.** A new task sat at "waiting to send" because an earlier run never ended. | The queue shows position and wait time. The hung run ends at `timeout_s`, and Cancel works at any time. The next task starts within a few seconds of the previous one reaching a terminal state. |
| 10 | **Direct links go to the wrong project.** A link to a task or thread opened a different project. | Each task has a stable URL built from its `id` alone (e.g. `/task/<id>`). Test that opening it shows that task and its `project`, and that an unknown id gives a "not found" page rather than a redirect. |
| 11 | **Remote edits need desktop review.** Edits started from the web couldn't be approved there and had to be reviewed on the desktop. | Every approval, including file edits with a diff, can be made from the web page alone. No step needs a separate desktop app. |
| 12 | **Previews never render remotely.** The preview pane only ever said it was busy. | taskbench has no built-in preview pane in v1. Artifacts are linked as files from the outbox. If an approved local preview is running, the page links to its 127.0.0.1 address. Nothing shows a pane that can't load. |
| 13 | **A bot names a folder the owner has not used.** The packet sets `workdir` to an absolute path. The path may be new, or a symlink or junction into a system directory, a drive root, or the profile root. | The watcher resolves links first. A rejected path is `blocked` with reason `invalid workdir`. A valid folder that is not in the local registry goes to `needs-approval` (the page shows Needs you) with the resolved path. `from`, including `web-ui`, does not register it. The owner registers it through the local UI endpoint. A folder already in the registry is the run root for the allow-list, instructions, and logs. |

## General checks

- Every run leaves `<id>.json`, `<id>.prompt.md` and `<id>.output.log` in the
  outbox, even when it fails or times out.
- `LAST_AGENT.md` is updated by the watcher on every terminal state,
  including `timed-out` and `failed`.
- A packet whose `from` is empty or names the human owner is rejected.
- A `project` that is absolute or contains `..` is rejected.
