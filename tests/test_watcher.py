"""Instructions, run artifacts, retro, handoff, and v1 staying blocked."""

from __future__ import annotations

import unittest
from pathlib import Path

from taskbench.task_format import V1_REASON, parse_yaml
from tests.support import TempApp, base_packet, packet_status, run_until, write_packet


class WatcherArtifactTests(TempApp):
    def test_instructions_prefix_and_run_files(self) -> None:
        _config, _app, watcher = self.make(scenario="echo")
        instructions = (self.root / "projects" / "demo-canvas" / "INSTRUCTIONS.md").read_text(encoding="utf-8")
        write_packet(self.store.inbox, base_packet(id="art-1", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "art-1") == "done")
        prompt = (self.store.outbox / "art-1.prompt.md").read_text(encoding="utf-8")
        self.assertTrue(prompt.startswith(instructions.strip()))
        self.assertIn("LAST_AGENT.md", prompt)
        self.assertTrue((self.store.outbox / "art-1.json").is_file())
        self.assertTrue((self.store.outbox / "art-1.output.log").is_file())
        saved = parse_yaml((self.store.outbox / "art-1.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["model"]["actual"], "grok-fixture-1")
        self.assertEqual(saved["runner"]["actual"], "grok-build")
        self.assertEqual(saved["retro"]["fit_1_5"], 4)
        self.assertEqual(saved["retro"]["effort"], "Reading the project.")
        handoff = Path(self.root / "projects" / "demo-canvas" / ".taskbench" / "LAST_AGENT.md")
        text = handoff.read_text(encoding="utf-8")
        self.assertIn("runner: grok-build", text)
        self.assertIn("model: grok-fixture-1", text)
        self.assertIn("task_id: art-1", text)
        self.assertIn("from: Joy Feeder", text)
        self.assertIn("status: done", text)
        self.assertNotIn(".enjoy-logs", text)

    def test_second_run_overwrites_last_agent(self) -> None:
        _config, _app, watcher = self.make(scenario="echo")
        write_packet(self.store.inbox, base_packet(id="first-1", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "first-1") == "done")
        write_packet(self.store.inbox, base_packet(id="second-1", timeout_s=30, **{"from": "Odd Jobs"}))
        run_until(watcher, lambda: packet_status(self.store, "second-1") == "done")
        text = (self.root / "projects" / "demo-canvas" / ".taskbench" / "LAST_AGENT.md").read_text(encoding="utf-8")
        self.assertIn("task_id: second-1", text)
        self.assertNotIn("task_id: first-1", text)
        self.assertIn("from: Odd Jobs", text)

    def test_retro_followup_and_null_when_killed(self) -> None:
        _config, _app, watcher = self.make(scenario="needs-retro-followup")
        write_packet(self.store.inbox, base_packet(id="retro-1", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "retro-1") == "done", timeout=10)
        saved = parse_yaml((self.store.outbox / "retro-1.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["retro"]["effort"], "The follow-up.")
        self.assertEqual(saved["retro"]["fit_1_5"], 3)

    def test_failed_and_timed_out_still_write_artifacts_and_handoff(self) -> None:
        _config, _app, watcher = self.make(scenario="exit-no-result")
        write_packet(self.store.inbox, base_packet(id="fail-1", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "fail-1") == "failed")
        for suffix in (".json", ".prompt.md", ".output.log"):
            self.assertTrue((self.store.outbox / f"fail-1{suffix}").is_file(), suffix)
        handoff = (self.root / "projects" / "demo-canvas" / ".taskbench" / "LAST_AGENT.md").read_text(encoding="utf-8")
        self.assertIn("status: failed", handoff)
        self.assertIn("task_id: fail-1", handoff)

    def test_v1_packet_never_runs(self) -> None:
        _config, _app, watcher = self.make(scenario="echo")
        v1 = {
            "schema": "joy-feeder.task/v1",
            "id": "old-9",
            "created": "2026-01-01T00:00:00+00:00",
            "from": {"surface": "chat", "project": "demo-canvas", "thread": "1"},
            "goal": "run this anyway",
            "status": "incoming",
        }
        write_packet(self.store.inbox, v1)
        run_until(watcher, lambda: packet_status(self.store, "old-9") == "blocked")
        saved = parse_yaml((self.store.outbox / "old-9.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["result"]["blocked_reason"], V1_REASON)
        self.assertIsNone(saved["runner"]["actual"])
        output = (self.store.outbox / "old-9.output.log").read_text(encoding="utf-8")
        self.assertNotIn("echoed the prompt", output)

    def test_rich_and_bad_project_are_blocked(self) -> None:
        _config, _app, watcher = self.make(scenario="echo")
        write_packet(self.store.inbox, base_packet(id="human-1", **{"from": "Rich"}))
        write_packet(self.store.inbox, base_packet(id="path-1", project="../demo-canvas"))
        run_until(
            watcher,
            lambda: packet_status(self.store, "human-1") == "blocked"
            and packet_status(self.store, "path-1") == "blocked",
        )
        human = parse_yaml((self.store.outbox / "human-1.json").read_text(encoding="utf-8"))
        path = parse_yaml((self.store.outbox / "path-1.json").read_text(encoding="utf-8"))
        self.assertEqual(human["result"]["blocked_reason"], "invalid from")
        self.assertIn("invalid project", path["result"]["blocked_reason"])

    def test_review_child_is_queued_after_done(self) -> None:
        _config, _app, watcher = self.make(scenario="echo")
        packet = base_packet(id="rev-1", timeout_s=30)
        packet["review_by"] = {"runner": "grok-build", "model": None, "permissions": "plan"}
        write_packet(self.store.inbox, packet)
        run_until(watcher, lambda: packet_status(self.store, "rev-1") == "done")
        parent = parse_yaml((self.store.outbox / "rev-1.json").read_text(encoding="utf-8"))
        self.assertEqual(parent["result"]["review_task"], "rev-1-review")
        run_until(watcher, lambda: packet_status(self.store, "rev-1-review") == "done", timeout=8)
        child = parse_yaml((self.store.outbox / "rev-1-review.json").read_text(encoding="utf-8"))
        self.assertEqual(child["from"], "taskbench")
        self.assertEqual(child["permissions"], "plan")
        self.assertIn("Review the changes made by rev-1", child["goal"])

    def test_plan_refuses_a_write(self) -> None:
        _config, _app, watcher = self.make(scenario="plan-write")
        write_packet(self.store.inbox, base_packet(id="planw-1", permissions="plan", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "planw-1") == "done")
        self.assertFalse((self.root / "projects" / "demo-canvas" / "blocked.txt").exists())
        output = (self.store.outbox / "planw-1.output.log").read_text(encoding="utf-8")
        self.assertIn("refused", output)


if __name__ == "__main__":
    unittest.main()
