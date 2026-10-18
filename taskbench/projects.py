"""Project instructions, prompts, and the watcher handoff file.

``<project>/.taskbench/LAST_AGENT.md`` is written by the watcher on every
terminal state, including failure and timeout. The runner is not asked to
write it, so a killed process still leaves a record.

The prompt sent to the runner starts with ``INSTRUCTIONS.md`` when that file
exists, then tells the agent to read the handoff if it is there.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from taskbench.atomic import atomic_write_text


def project_dir(projects_root: Path, name: str) -> Path:
    return projects_root / name


def load_instructions(directory: Path) -> str:
    path = directory / "INSTRUCTIONS.md"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def last_agent_path(directory: Path) -> Path:
    return directory / ".taskbench" / "LAST_AGENT.md"


def read_last_agent(directory: Path) -> dict[str, str] | None:
    path = last_agent_path(directory)
    if not path.is_file():
        return None
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        body = stripped[2:]
        if ": " not in body:
            continue
        key, value = body.split(": ", 1)
        found[key.strip()] = value.strip()
    if not found:
        return None
    return found


def write_last_agent(
    directory: Path,
    *,
    runner: str,
    model: str,
    finished_at: str,
    task_id: str,
    sender: str,
    status: str,
    summary: str,
) -> None:
    one_line = " ".join(summary.split())
    if len(one_line) > 300:
        one_line = one_line[:300].rstrip()
    text = (
        "# LAST_AGENT\n"
        "\n"
        f"- runner: {runner or 'unknown'}\n"
        f"- model: {model or 'unknown'}\n"
        f"- finished_at: {finished_at}\n"
        f"- task_id: {task_id}\n"
        f"- from: {sender}\n"
        f"- status: {status}\n"
        f"- summary: {one_line or '(no summary)'}\n"
    )
    atomic_write_text(last_agent_path(directory), text)


def compose_prompt(
    *,
    instructions: str,
    packet: dict[str, Any],
    previous: dict[str, str] | None,
    taking_over: bool,
) -> str:
    parts: list[str] = []
    if instructions.strip():
        parts.append(instructions.strip())
        parts.append("\n\n---\n\n")
    parts.append(f"# Task {packet.get('id', '')}\n")
    parts.append(f"From: {packet.get('from', '')}\n")
    parts.append(f"Project: {packet.get('project', '')}\n")
    if isinstance(packet.get("workdir"), str) and packet["workdir"].strip():
        parts.append(f"Folder: {packet['workdir'].strip()}\n")
    parts.append(f"Permissions: {packet.get('permissions', 'ask')}\n")
    parts.append(
        "\nRead `.taskbench/LAST_AGENT.md` if it exists before you act. "
        "Do not edit that file. The watcher owns it.\n"
    )
    if previous:
        parts.append(
            "\nPrevious handoff: "
            f"runner {previous.get('runner', 'unknown')}, "
            f"model {previous.get('model', 'unknown')}, "
            f"status {previous.get('status', 'unknown')}.\n"
        )
        if taking_over:
            parts.append(
                "You are taking over from "
                f"{previous.get('runner', 'unknown')} "
                f"({previous.get('model', 'unknown')}). "
                "Summarise the last status before you act.\n"
            )
    parts.append(f"\n## Goal\n{packet.get('goal', '').strip()}\n")
    constraints = packet.get("constraints") or []
    if constraints:
        parts.append("\n## Constraints\n")
        for item in constraints:
            parts.append(f"- {item}\n")
    criteria = packet.get("success_criteria") or []
    if criteria:
        parts.append("\n## Success criteria\n")
        for item in criteria:
            parts.append(f"- {item}\n")
    files = packet.get("files") or []
    if files:
        parts.append("\n## Files\n")
        for item in files:
            parts.append(f"- {item}\n")
    parts.append(
        "\n## How to act\n"
        "You are in plan/ask mode. Do not run shell commands, write files, "
        "or start servers yourself. Emit one `TASKBENCH ` JSON object per line "
        "and wait for a JSON decision on stdin before assuming an action ran.\n"
        "Read-only actions use op `list`, `read`, or `diff`. "
        "Writes use op `write` with `path`, `content`, and `diff`. "
        "Commands use op `shell` with the full `command`. "
        "Servers and background processes use op `serve` with the full `command` "
        "and must bind to 127.0.0.1.\n"
        "Report the model you actually used as "
        '`TASKBENCH {"type":"model","model":"..."}`. '
        "Do not invent a model id; omit it if you cannot tell.\n"
        "When you finish, emit a result and, if you are still running, a retro:\n"
        '`TASKBENCH {"type":"result","summary":"...","artifacts":[]}`\n'
        '`TASKBENCH {"type":"retro","effort":"...","unclear":"...","would_change":"...","fit_1_5":3}`\n'
        '`TASKBENCH {"type":"done"}`\n'
    )
    return "".join(parts)


def retro_followup_prompt(packet: dict[str, Any]) -> str:
    return (
        f"Retro follow-up for {packet.get('id', '')}.\n"
        "Reply with one TASKBENCH retro object only. "
        "Where did the most effort go, what was unclear, what would you change, "
        "and a fit score from 1 to 5.\n"
        'TASKBENCH {"type":"retro","effort":"...","unclear":"...","would_change":"...","fit_1_5":3}\n'
    )


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
