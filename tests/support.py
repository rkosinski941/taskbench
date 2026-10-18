"""Shared fixtures for taskbench tests. Nothing here is a runtime dependency."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from taskbench.config import Config, RunnerConfig
from taskbench.state import StateStore
from taskbench.task_format import emit_yaml
from taskbench.ui.server import App
from taskbench.watcher import Watcher

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_runner.py"
ROOT = Path(__file__).resolve().parents[1]


def make_config(
    root: Path,
    *,
    scenario: str = "echo",
    can_ask: bool = True,
    sandbox_only: bool = False,
    extra_args: list[str] | None = None,
    port: int = 8765,
    allowed_hosts: list[str] | None = None,
    human_owner: str = "Rich",
) -> Config:
    projects = root / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    demo = projects / "demo-canvas"
    if not (demo / "INSTRUCTIONS.md").is_file():
        shutil.copytree(ROOT / "examples" / "demo-canvas", demo, dirs_exist_ok=True)
    args = [
        str(FAKE),
        "--scenario",
        scenario,
        "--prompt-file",
        "{prompt_file}",
        "--project-dir",
        "{project_dir}",
    ]
    if extra_args:
        args.extend(extra_args)
    runner = RunnerConfig(
        name="grok-build",
        adapter="grok_build",
        executable=__import__("sys").executable,
        args_template=args,
        can_ask=can_ask,
        sandbox_only=sandbox_only,
    )
    return Config(
        bind_host="127.0.0.1",
        port=port,
        allowed_hosts=list(allowed_hosts or []),
        human_owner=human_owner,
        watcher_name="taskbench-local",
        data_dir=root / "var",
        projects_dir=projects,
        runners={"grok-build": runner},
    )


def write_packet(inbox: Path, packet: dict) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{packet['id']}.yaml"
    path.write_text(emit_yaml(packet), encoding="utf-8")
    return path


def base_packet(**overrides) -> dict:
    packet = {
        "schema": "joy-feeder.task/v2",
        "id": "task-001",
        "created": "2026-10-17T18:00:00+00:00",
        "from": "Joy Feeder",
        "runner": {"requested": "grok-build", "actual": None},
        "model": {"requested": None, "actual": None},
        "project": "demo-canvas",
        "goal": "List the files and summarise the app.",
        "status": "incoming",
    }
    packet.update(overrides)
    return packet


def run_until(watcher: Watcher, predicate, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        watcher.tick()
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting for watcher")


def packet_status(store: StateStore, packet_id: str) -> str | None:
    found = store.find(packet_id)
    if found is None:
        return None
    return str(found[1].get("status"))


class TempApp(unittest.TestCase):
    def setUp(self) -> None:
        scratch = ROOT / "test-scratch"
        scratch.mkdir(exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self._tmp.name)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        watcher = getattr(self, "watcher", None)
        if watcher is not None:
            try:
                watcher.shutdown()
            except Exception:
                pass
        server = getattr(self, "server", None)
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        self._tmp.cleanup()

    def make(self, **kwargs) -> tuple[Config, App, Watcher]:
        config = make_config(self.root, **kwargs)
        app = App(config)
        self.watcher = app.watcher
        self.app = app
        self.store = app.store
        return config, app, app.watcher


def command_write_marker(project: Path, name: str = "ran.txt") -> str:
    script = project / "do_run.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "from pathlib import Path\nPath(" + json.dumps(name) + ").write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    return subprocess.list2cmdline([__import__("sys").executable, str(script)])
