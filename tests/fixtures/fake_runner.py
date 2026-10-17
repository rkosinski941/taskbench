"""Stub runner for tests.

Speaks the TASKBENCH line protocol and does not run shell commands itself
unless ``--scenario uncontrolled`` is used and ``TASKBENCH_APPROVALS`` is
missing. The real Grok Build CLI is assumed to behave the same way when
``TASKBENCH_APPROVALS=1``: it proposes actions and waits.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def emit(payload: dict) -> None:
    sys.stdout.write("TASKBENCH " + json.dumps(payload) + "\n")
    sys.stdout.flush()


def read_prompt(path: str | None) -> str:
    if not path:
        return ""
    file = Path(path)
    if not file.is_file():
        return ""
    return file.read_text(encoding="utf-8")


def wait_decision() -> dict:
    line = sys.stdin.readline()
    if not line:
        return {}
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def spawn_sleeper(marker: Path, role: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--scenario",
            "sleeper",
            "--marker-dir",
            str(marker),
            "--role",
            role,
        ]
    )


def scenario_sleeper(marker: Path, role: str) -> int:
    marker.mkdir(parents=True, exist_ok=True)
    if role == "grandchild":
        leaf = spawn_sleeper(marker, "leaf")
        (marker / "grandchild.pid").write_text(str(os.getpid()), encoding="utf-8")
        (marker / "leaf.pid").write_text(str(leaf.pid), encoding="utf-8")
        (marker / "ready").write_text("1", encoding="utf-8")
    elif role == "leaf":
        (marker / "leaf-alive.pid").write_text(str(os.getpid()), encoding="utf-8")
    else:
        (marker / f"{role}.pid").write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(10000)
    return 0


def scenario_tree(marker: Path) -> int:
    marker.mkdir(parents=True, exist_ok=True)
    (marker / "child.pid").write_text(str(os.getpid()), encoding="utf-8")
    grand = spawn_sleeper(marker, "grandchild")
    (marker / "grandchild-started.pid").write_text(str(grand.pid), encoding="utf-8")
    deadline = time.time() + 5
    ready = marker / "ready"
    while time.time() < deadline and not ready.is_file():
        time.sleep(0.02)
    time.sleep(10000)
    return 0


def scenario_server(marker: Path) -> int:
    marker.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    (marker / "port").write_text(str(port), encoding="utf-8")
    (marker / "child.pid").write_text(str(os.getpid()), encoding="utf-8")
    grand = spawn_sleeper(marker, "leaf")
    (marker / "grandchild.pid").write_text(str(grand.pid), encoding="utf-8")
    (marker / "ready").write_text("1", encoding="utf-8")
    time.sleep(10000)
    return 0


def scenario_spawn_then_exit(marker: Path) -> int:
    marker.mkdir(parents=True, exist_ok=True)
    (marker / "child.pid").write_text(str(os.getpid()), encoding="utf-8")
    grand = spawn_sleeper(marker, "orphan")
    (marker / "grandchild.pid").write_text(str(grand.pid), encoding="utf-8")
    deadline = time.time() + 5
    while time.time() < deadline and not (marker / "orphan.pid").is_file():
        time.sleep(0.02)
    emit({"type": "model", "model": "grok-fixture-1"})
    emit({"type": "runner", "runner": "grok-build"})
    emit({"type": "result", "summary": "parent finished", "artifacts": []})
    emit({"type": "done"})
    return 0


def scenario_echo(prompt: str) -> int:
    emit({"type": "model", "model": "grok-fixture-1"})
    emit({"type": "runner", "runner": "grok-build"})
    emit({"type": "text", "text": prompt})
    emit({"type": "result", "summary": "echoed the prompt", "artifacts": ["canvas-v2.html"]})
    emit(
        {
            "type": "retro",
            "effort": "Reading the project.",
            "unclear": "None recorded.",
            "would_change": "Nothing.",
            "fit_1_5": 4,
        }
    )
    emit({"type": "done"})
    return 0


def scenario_model(model: str | None) -> int:
    if model:
        emit({"type": "model", "model": model})
    emit({"type": "runner", "runner": "grok-build"})
    emit({"type": "result", "summary": "finished", "artifacts": []})
    emit({"type": "done"})
    return 0


def scenario_usage() -> int:
    emit(
        {
            "type": "usage_limit",
            "runner": "grok-build",
            "message": "quota exceeded raw",
            "reset_at": "2026-10-18T00:00:00+00:00",
        }
    )
    time.sleep(30)
    return 0


def scenario_exit_no_result() -> int:
    sys.stderr.write("boom line\n")
    sys.stderr.flush()
    return 3


def scenario_action(op_payload: dict, then_result: bool) -> int:
    emit({"type": "runner", "runner": "grok-build"})
    emit({"type": "model", "model": "grok-fixture-1"})
    emit({"type": "action", "action": op_payload})
    decision = wait_decision()
    if then_result:
        if decision.get("refused"):
            emit({"type": "text", "text": "refused"})
        emit({"type": "result", "summary": "action handled", "artifacts": []})
        emit({"type": "done"})
    else:
        time.sleep(10000)
    return 0


def scenario_uncontrolled(command: str, marker: Path) -> int:
    marker.mkdir(parents=True, exist_ok=True)
    (marker / "approvals-env.txt").write_text(os.environ.get("TASKBENCH_APPROVALS", ""), encoding="utf-8")
    if os.environ.get("TASKBENCH_APPROVALS") != "1":
        subprocess.run(command, shell=True, check=False)
        return 0
    return scenario_action({"op": "shell", "command": command}, then_result=True)


def scenario_retro(prompt: str) -> int:
    if "Retro follow-up" in prompt:
        emit(
            {
                "type": "retro",
                "effort": "The follow-up.",
                "unclear": "The score scale.",
                "would_change": "Ask once.",
                "fit_1_5": 3,
            }
        )
        return 0
    emit({"type": "runner", "runner": "grok-build"})
    emit({"type": "result", "summary": "work without retro", "artifacts": []})
    emit({"type": "done"})
    return 0


def scenario_preview(command: str) -> int:
    emit({"type": "runner", "runner": "grok-build"})
    emit({"type": "model", "model": "grok-fixture-1"})
    emit({"type": "action", "action": {"op": "serve", "command": command}})
    wait_decision()
    emit({"type": "text", "text": "preview up"})
    time.sleep(10000)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="echo")
    parser.add_argument("--prompt-file", default="")
    parser.add_argument("--project-dir", default="")
    parser.add_argument("--marker-dir", default="")
    parser.add_argument("--command", default="")
    parser.add_argument("--length", type=int, default=1)
    parser.add_argument("--role", default="grandchild")
    parser.add_argument("--model", default="model-b")
    args = parser.parse_args(argv)
    prompt = read_prompt(args.prompt_file)
    marker = Path(args.marker_dir) if args.marker_dir else Path(".")
    scenario = args.scenario
    if scenario == "sleeper":
        return scenario_sleeper(marker, args.role)
    if scenario == "tree":
        return scenario_tree(marker)
    if scenario == "server":
        return scenario_server(marker)
    if scenario == "spawn-then-exit":
        return scenario_spawn_then_exit(marker)
    if scenario == "echo":
        return scenario_echo(prompt)
    if scenario == "model-mismatch":
        return scenario_model(args.model)
    if scenario == "no-model":
        return scenario_model(None)
    if scenario == "usage-limit":
        return scenario_usage()
    if scenario == "exit-no-result":
        return scenario_exit_no_result()
    if scenario == "hang":
        if args.marker_dir:
            marker.mkdir(parents=True, exist_ok=True)
            (marker / "child.pid").write_text(str(os.getpid()), encoding="utf-8")
            (marker / "ready").write_text("1", encoding="utf-8")
        time.sleep(10000)
        return 0
    if scenario == "long-command":
        return scenario_action({"op": "shell", "command": "A" * args.length}, then_result=False)
    if scenario == "approval-shell":
        return scenario_action({"op": "shell", "command": args.command}, then_result=False)
    if scenario == "approval-write":
        return scenario_action(
            {
                "op": "write",
                "path": "notes.txt",
                "content": "hello from taskbench\n",
                "diff": "--- notes.txt\n+++ notes.txt\n@@\n+hello from taskbench\n",
            },
            then_result=True,
        )
    if scenario == "plan-write":
        return scenario_action(
            {"op": "write", "path": "blocked.txt", "content": "nope\n"},
            then_result=True,
        )
    if scenario == "serve-action":
        return scenario_action({"op": "serve", "command": args.command}, then_result=False)
    if scenario == "uncontrolled":
        return scenario_uncontrolled(args.command, marker)
    if scenario == "needs-retro-followup":
        return scenario_retro(prompt)
    if scenario == "preview":
        return scenario_preview(args.command)
    if scenario == "read-ok":
        emit({"type": "action", "action": {"op": "read", "path": "INSTRUCTIONS.md"}})
        wait_decision()
        emit({"type": "model", "model": "grok-fixture-1"})
        emit({"type": "result", "summary": "read instructions", "artifacts": []})
        emit({"type": "done"})
        return 0
    sys.stderr.write(f"unknown scenario {scenario}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
