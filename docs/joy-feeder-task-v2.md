# joy-feeder.task/v2

Task packet format for **taskbench**, the local task runner. A packet is one
YAML file dropped into the inbox. The watcher claims it, runs it with one
runner (one task at a time), and writes the finished packet plus its prompt
and output to the outbox as plain files.

v2 keeps every v1 field and adds runner and model tracking, a project scope,
a permission mode, a hard timeout, an end-of-task retro, a handoff check, and
optional review chaining.

## Field table

Who writes each field: **S** = sender, **W** = watcher, **R** = runner adapter.

| Field | Type | Req | Who | Notes |
|---|---|---|---|---|
| `schema` | string | yes | S | Always `joy-feeder.task/v2`. |
| `id` | string | yes | S | Unique, stable. Used for the outbox file names. |
| `created` | ISO 8601 with offset | yes | S | When the sender wrote it. |
| `from` | string | yes | S | **The sending bot's name**, e.g. `Joy Feeder`. Never the human owner. Tasks typed into the web page use `from: web-ui`. The UI shows this label. |
| `origin` | object | no | S | Optional `{surface, thread}` context (this held v1's `from` data). |
| `target` | object | no | S | `{bot, job}` as in v1. Optional when `runner` is set. |
| `runner.requested` | enum | yes | S | `grok-build` \| `antigravity` \| `opencode`. |
| `runner.actual` | enum | no | **R** | What actually ran. The sender leaves it `null`. |
| `model.requested` | string | no | S | e.g. `grok-4.7`. `null` = runner default. |
| `model.actual` | string | no | **R** | The model id the runner reports for the run, not the UI label. |
| `project` | string | yes | S | Folder name relative to the taskbench projects root, e.g. `demo-canvas`. **Never an absolute path**, no `..`, no drive letters. |
| `permissions` | enum | no | S | `plan` \| `ask` \| `allow-list`. **Default `ask`.** |
| `allow` | list of strings | if `allow-list` | S | Command patterns that may run without a prompt (glob-style, matched against the full command line). Anything else still asks. |
| `timeout_s` | integer | no | S | Wall-clock limit in seconds. Default 900. Max 7200. |
| `goal` | string | yes | S | What to do. |
| `constraints` | list | no | S | Hard rules. |
| `success_criteria` | list | no | S | How to judge done. |
| `return` | object | no | S | `{path, notify}` as in v1. Default path `outbox/<from>/done/`. |
| `priority` | enum | no | S | `low` \| `normal` \| `high`. |
| `due` | ISO 8601 \| null | no | S | |
| `files` | list | no | S | Paths **relative to `project`**. |
| `review_by` | object \| null | no | S | `{runner, model, permissions}` for an optional second, review-only pass. See "Review chaining". |
| `status` | enum | yes | S then W | See "Status values". The sender sets `incoming`. |
| `claimed_by` | string | no | W | Watcher instance name. |
| `claimed_at` | ISO 8601 | no | W | |
| `worker_thread` | string | no | W | Run id or log name. |
| `handoff` | object | no | R | `{previous_agent, previous_model, taking_over}` from the LAST_AGENT.md check. |
| `approvals` | list | no | W | Each prompt shown: `{at, command, decision}`. |
| `retro.effort` | string | no | R | Where the most effort went, and why. |
| `retro.unclear` | string | no | R | What was unclear in the request. |
| `retro.would_change` | string | no | R | What to change about the task or tools. |
| `retro.fit_1_5` | integer 1-5 | no | R | How well the task fit the runner and model. |
| `result.summary` | string | no | R | |
| `result.artifacts` | list | no | R | Paths relative to `project`. |
| `result.blocked_reason` | string \| null | no | W/R | Required when `status` is `blocked`, `failed` or `timed-out`. |
| `result.completed_at` | ISO 8601 | no | W | |

## Status values

| Status | Meaning |
|---|---|
| `incoming` | In the inbox, not yet picked up. |
| `claimed` | The watcher owns it. It's queued behind the current run. |
| `running` | The runner process is live. |
| `needs-approval` | Paused on an approval prompt. The timeout clock keeps running. |
| `done` | Finished and the success criteria were reported on. |
| `failed` | The runner exited with an error. |
| `blocked` | It can't proceed without a human or another bot (missing access, usage limit, refused approval). |
| `timed-out` | Hit `timeout_s`. The process tree was killed. |

Terminal states: `done`, `failed`, `blocked`, `timed-out`. Only the watcher
moves a packet between states, and every change goes in the run log.

## Behaviours

1. **The runner writes actuals.** `runner.actual` and `model.actual` are
   filled in by the runner adapter from what the tool reports for that run.
   A value the sender wrote there is overwritten. If the model can't be
   determined, write `unknown`. Never copy `requested`. The UI shows
   actuals and flags any mismatch with `requested`.
2. **`from` is a bot.** The watcher rejects a packet whose `from` is empty or
   names the human owner (`status: blocked`, reason `invalid from`). The UI
   labels each task with its `from`.
3. **Default permissions is `ask`.** A missing or unknown value means `ask`.
   - `plan`: read-only. No writes, no commands except read-only ones the
     adapter whitelists (list, read, diff). Anything else is refused, not
     prompted.
   - `ask`: every write and command raises an approval in the web page.
   - `allow-list`: commands matching `allow` run. Everything else asks.
   If a runner can't raise approvals, the adapter must refuse `ask` and
   `allow-list` packets up front (`blocked`, reason `runner cannot ask`)
   instead of failing mid-run.
4. **Approval prompts are safe to show.** A prompt shows the full command in
   a scrollable box, with a short title cut down to 120 characters. No
   command length crashes the UI or the run.
5. **Servers and background processes need approval.** Starting anything
   that listens on a port or outlives its command is always an approval
   item, even under `allow-list`, unless the `allow` entry names it. Bind
   to 127.0.0.1 only.
6. **LAST_AGENT.md handoff check.** Before running, the adapter tells the
   agent to read `<project>/.taskbench/LAST_AGENT.md` if it exists. If it
   names a different runner or model (including a different version of the
   same model), the agent says it's taking over and summarises the last
   status before it acts. On a terminal state, the **watcher** (not the
   agent) overwrites the file with: runner, model actual, finished at, task
   id, `from`, status, and a one-line summary. The watcher writes it so a
   killed or cut-off run still updates it.
7. **Kill the whole process tree.** On timeout, completion, failure, cancel,
   or watcher shutdown, the watcher kills the runner's whole process tree
   (Windows: a Job Object or `taskkill /T /F`; POSIX: a process group with
   `kill -- -PGID`). It then checks that no child is left and logs any
   ports that were opened during the run. Closing the web page never kills
   a run. Only Cancel does.
8. **One at a time, never stuck.** A hung run can't block the queue past
   `timeout_s`. The UI has Cancel on the running task, and queued tasks show
   their position.
9. **Retro.** On a terminal state the adapter asks the agent for the retro
   in one short follow-up: where the most effort went, what was unclear,
   what it would change, and a 1-5 fit score. If the run was killed,
   `retro` stays `null`.
10. **Usage limits are explicit.** If a runner reports a usage limit, the
    packet goes `blocked` with the runner named and the reset time if it's
    known. Never a silent stop.
11. **Review chaining.** If `review_by` is set and the first run ends `done`,
    the watcher queues a child packet `<id>-review`. That packet has
    `from: taskbench`, runner and model from `review_by`, `permissions`
    from `review_by` (default `plan`), the same `project`, and a goal of
    "Review the changes made by `<id>`" with the first result attached. The
    child's id goes in the parent's `result.review_task`.
12. **Every run is plain files.** The outbox gets `<id>.yaml` (the final
    packet), `<id>.prompt.md` (the exact prompt sent), and `<id>.output.log`
    (the full runner output).

## Examples

Minimal packet as sent:

```yaml
schema: joy-feeder.task/v2
id: jf-demo-001
created: 2026-10-17T18:00:00-04:00
from: Joy Feeder
runner:
  requested: grok-build
  actual: null
model:
  requested: null
  actual: null
project: demo-canvas
goal: List the files in the project and summarise what the app does.
status: incoming
```

With an allow-list and review chaining:

```yaml
schema: joy-feeder.task/v2
id: jf-demo-002
created: 2026-10-17T18:05:00-04:00
from: Odd Jobs
runner:
  requested: opencode
  actual: null
model:
  requested: example-small-model
  actual: null
project: demo-canvas
permissions: allow-list
allow:
  - "node --check *.js"
  - "git diff*"
timeout_s: 600
goal: Fix the off-by-one in the frame counter in app.js.
constraints:
  - Change only app.js
review_by:
  runner: grok-build
  model: null
  permissions: plan
status: incoming
```

The same packet after the run, as written to the outbox:

```yaml
# ...sender fields unchanged...
runner:
  requested: opencode
  actual: opencode
model:
  requested: example-small-model
  actual: example-small-model-v2
status: done
claimed_by: taskbench-local
claimed_at: 2026-10-17T18:05:02-04:00
worker_thread: run-0042
handoff:
  previous_agent: grok-build
  previous_model: grok-4.7
  taking_over: true
approvals: []
retro:
  effort: Tracing where the counter is reset.
  unclear: Whether frame 0 should be shown.
  would_change: Put the expected output in success_criteria.
  fit_1_5: 4
result:
  summary: Counter now starts at 0. One line changed in app.js.
  artifacts: [app.js]
  blocked_reason: null
  completed_at: 2026-10-17T18:07:40-04:00
  review_task: jf-demo-002-review
```

## Migration from v1

- Change `schema` to `joy-feeder.task/v2`.
- `from` was an object `{surface, project, thread}`. It's now a string with
  the sending bot's name. Move `surface` and `thread` to `origin`, and move
  `project` to the top-level `project` as a relative folder name. Drop any
  absolute path or display name with spaces, and map it to the folder name.
- Add `runner.requested` (required). `target` can stay for routing between
  bots, but taskbench runs on `runner`.
- Add `permissions` (or omit it to get `ask`), and `timeout_s` if the
  default of 900 is wrong.
- All v1 fields keep their meaning. A v1 packet the watcher finds is treated
  as v2 with `permissions: ask`, `timeout_s: 900`, and `status: blocked`
  (reason `v1 packet: set runner and project`) until a sender upgrades it.
