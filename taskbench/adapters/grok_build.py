"""Grok Build adapter.

The real CLI's flags are not published. Configure ``executable`` and
``args_template`` with placeholders ``{prompt_file}``, ``{project_dir}``, and
``{timeout_s}``. The template must already put the CLI in plan/ask mode.
taskbench refuses templates that contain a bypass or yolo flag, and it always
sets ``TASKBENCH_APPROVALS=1``.

Assumed stdout protocol, one JSON object per line::

    TASKBENCH {"type":"model","model":"grok-4.7"}
    TASKBENCH {"type":"runner","runner":"grok-build"}
    TASKBENCH {"type":"text","text":"..."}
    TASKBENCH {"type":"action","action":{"op":"read","path":"README.md"}}
    TASKBENCH {"type":"action","action":{"op":"write","path":"a.py","content":"...","diff":"..."}}
    TASKBENCH {"type":"action","action":{"op":"shell","command":"..."}}
    TASKBENCH {"type":"action","action":{"op":"serve","command":"python -m http.server 8765 --bind 127.0.0.1"}}
    TASKBENCH {"type":"usage_limit","runner":"grok-build","message":"...","reset_at":"..."}
    TASKBENCH {"type":"result","summary":"...","artifacts":[]}
    TASKBENCH {"type":"retro","effort":"...","unclear":"...","would_change":"...","fit_1_5":4}
    TASKBENCH {"type":"done"}

The process waits for a JSON line on stdin after each action. taskbench is
the only executor. If the CLI runs commands itself, taskbench cannot see
them; do not point this adapter at a mode that self-approves.
"""

from __future__ import annotations

from pathlib import Path

from taskbench.adapters.base import Adapter
from taskbench.config import BANNED_ARG_PARTS, RunnerConfig


class GrokBuildAdapter(Adapter):
    name = "grok-build"
    implemented = True

    def __init__(self, runner: RunnerConfig) -> None:
        self.runner = runner
        self.can_ask = runner.can_ask

    def build_command(
        self,
        *,
        prompt_file: Path,
        project_dir: Path,
        timeout_s: int,
    ) -> list[str]:
        mapping = {
            "{prompt_file}": str(prompt_file),
            "{project_dir}": str(project_dir),
            "{timeout_s}": str(timeout_s),
        }
        argv = [self.runner.executable]
        for arg in self.runner.args_template:
            for key, value in mapping.items():
                arg = arg.replace(key, value)
            argv.append(arg)
        for arg in argv:
            lowered = arg.lower()
            if any(part in lowered for part in BANNED_ARG_PARTS):
                raise ValueError("refusing to launch Grok Build with a bypass flag")
        return argv
