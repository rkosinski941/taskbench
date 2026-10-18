"""Grok Build adapter.

``args_template`` may use ``{prompt_file}``, ``{project_dir}``, and
``{timeout_s}``. taskbench places ``--permission-mode dontAsk`` and
``--sandbox strict`` immediately after the executable and strips any later
copy of those flags. ``--`` is rejected in the template and in the built
argv. Other permission flags (``--always-approve``, ``--allow``,
``--auto-approve``, ``--yolo``, and unknown permission flags) are rejected.
The child environment is an allow-list. Parent ``GROK_*``, ``PYTHONPATH``,
and config-home overrides are not passed through. On Windows the caller
passes an empty config home and this adapter sets ``HOME``, ``USERPROFILE``,
and ``GROK_HOME`` to it. ``can_ask`` defaults to false because ``dontAsk``
does not ask taskbench.

Permission-mode detection is best-effort. The real Grok CLI does not emit
the ``TASKBENCH`` permission lines below. A missing line is not proof of
``dontAsk``.

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
from taskbench.config import RunnerConfig
from taskbench.permissions import apply_config_home, ensure_pinned_args


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
        return ensure_pinned_args(argv)

    def environment(self, base: dict[str, str], *, config_home: Path | None = None) -> dict[str, str]:
        env = super().environment(base)
        if config_home is not None:
            env = apply_config_home(env, config_home)
        return env
