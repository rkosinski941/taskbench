"""Runner adapter interface.

An adapter builds the command line, parses the tool's stdout, and reports the
model the tool actually named. It also says whether it can raise an approval
in taskbench. v1 implements Grok Build only. Antigravity and opencode are
stubs for v2 and do not launch.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class ProtocolError(ValueError):
    """A ``TASKBENCH `` line was not valid JSON."""


class Adapter(ABC):
    name = "adapter"
    implemented = False
    can_ask = False

    @abstractmethod
    def build_command(
        self,
        *,
        prompt_file: Path,
        project_dir: Path,
        timeout_s: int,
    ) -> list[str]:
        """Return argv. The command must hand approvals to taskbench."""

    def environment(self, base: dict[str, str]) -> dict[str, str]:
        """Return the child environment. Parent variables are not passed through."""
        from taskbench.permissions import scrub_environment

        return scrub_environment(base)

    def parse_line(self, line: str) -> dict[str, Any] | None:
        stripped = line.strip()
        if not stripped.startswith("TASKBENCH "):
            return None
        try:
            payload = json.loads(stripped[len("TASKBENCH ") :])
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"invalid TASKBENCH line: {exc}") from exc
        if not isinstance(payload, dict) or "type" not in payload:
            raise ProtocolError("TASKBENCH line is not an object with type")
        return payload

    def report_model(self, reported: str | None) -> str:
        """Return the model the tool reported. Never invent or copy a request."""
        text = (reported or "").strip()
        return text if text else "unknown"

    def handoff(
        self,
        previous: dict[str, str] | None,
        *,
        requested_runner: str,
        requested_model: str | None,
        actual_runner: str | None,
        actual_model: str | None,
    ) -> dict[str, Any] | None:
        if not previous:
            return None
        prev_runner = previous.get("runner") or ""
        prev_model = previous.get("model") or ""
        runner_now = actual_runner or requested_runner
        model_now = actual_model or requested_model or ""
        taking_over = False
        if prev_runner and runner_now and prev_runner != runner_now:
            taking_over = True
        if prev_model and model_now and prev_model != model_now and model_now != "unknown":
            taking_over = True
        if prev_model and requested_model and prev_model != requested_model:
            taking_over = True
        return {
            "previous_agent": prev_runner or None,
            "previous_model": prev_model or None,
            "taking_over": taking_over,
        }
