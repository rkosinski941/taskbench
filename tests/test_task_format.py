"""Packet schema tests, including v1 migration and from/project rejection."""

from __future__ import annotations

import unittest
from pathlib import Path

from taskbench.task_format import (
    INVALID_FROM,
    V1_REASON,
    interpret,
    load_packet_text,
    parse_yaml,
)
from tests.support import base_packet

ROOT = Path(__file__).resolve().parents[1]


class TaskFormatTests(unittest.TestCase):
    def test_samples_are_runnable_v2(self) -> None:
        for name in ("sample-readonly.yaml", "sample-needs-approval.yaml"):
            text = (ROOT / "examples" / name).read_text(encoding="utf-8")
            result = load_packet_text(text, human_owner="Rich")
            self.assertTrue(result.ok, result.reason)
            self.assertEqual(result.packet["status"], "incoming")
            self.assertIsNone(result.packet["runner"]["actual"])
            self.assertIsNone(result.packet["model"]["actual"])

    def test_missing_permissions_mean_ask_and_timeout_defaults(self) -> None:
        packet = base_packet()
        packet.pop("permissions", None)
        result = interpret(packet, human_owner="Rich")
        self.assertTrue(result.ok)
        self.assertEqual(result.packet["permissions"], "ask")
        self.assertEqual(result.packet["timeout_s"], 900)

    def test_unknown_permissions_mean_ask(self) -> None:
        result = interpret(base_packet(permissions="yolo"), human_owner="Rich")
        self.assertTrue(result.ok)
        self.assertEqual(result.packet["permissions"], "ask")

    def test_timeout_above_max_is_rejected(self) -> None:
        result = interpret(base_packet(timeout_s=7201), human_owner="Rich")
        self.assertFalse(result.ok)
        self.assertEqual(result.packet["status"], "blocked")
        self.assertIn("invalid timeout_s", result.reason or "")

    def test_from_empty_or_rich_is_rejected(self) -> None:
        for sender in ("", "   ", "Rich", "rich", "RICH", " Rich "):
            result = interpret(base_packet(**{"from": sender}), human_owner="Rich")
            self.assertFalse(result.ok, sender)
            self.assertEqual(result.packet["status"], "blocked")
            self.assertEqual(result.reason, INVALID_FROM)

    def test_bot_names_with_spaces_are_kept(self) -> None:
        result = interpret(base_packet(**{"from": "Joy Feeder"}), human_owner="Rich")
        self.assertTrue(result.ok)
        self.assertEqual(result.packet["from"], "Joy Feeder")

    def test_project_absolute_or_dotdot_is_rejected(self) -> None:
        for project in ("../secret", "/tmp/demo", "C:/demo", "demo/nested", ".."):
            result = interpret(base_packet(project=project), human_owner="Rich")
            self.assertFalse(result.ok, project)
            self.assertIn("invalid project", result.reason or "")

    def test_unknown_schema_is_blocked_and_not_ok(self) -> None:
        data = base_packet(schema="joy-feeder.task/v9")
        result = interpret(data, human_owner="Rich")
        self.assertFalse(result.ok)
        self.assertEqual(result.packet["status"], "blocked")
        self.assertEqual(result.reason, "unknown schema 'joy-feeder.task/v9'")

    def test_v1_packet_is_blocked_and_not_runnable(self) -> None:
        data = {
            "schema": "joy-feeder.task/v1",
            "id": "old-1",
            "created": "2026-01-01T00:00:00+00:00",
            "from": {"surface": "chat", "project": "C:/demo", "thread": "t1"},
            "goal": "do the thing",
            "status": "incoming",
        }
        result = interpret(data, human_owner="Rich")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, V1_REASON)
        self.assertEqual(result.packet["status"], "blocked")
        self.assertEqual(result.packet["permissions"], "ask")
        self.assertEqual(result.packet["timeout_s"], 900)
        self.assertEqual(result.packet["from"], "(unconverted-v1)")
        self.assertEqual(result.packet["origin"]["surface"], "chat")
        self.assertEqual(result.packet["origin"]["thread"], "t1")
        self.assertIsNone(result.packet["project"])
        self.assertEqual(result.packet["result"]["blocked_reason"], V1_REASON)

    def test_sender_model_actual_is_cleared(self) -> None:
        packet = base_packet()
        packet["model"] = {"requested": "grok-4.7", "actual": "grok-4.7"}
        packet["runner"] = {"requested": "grok-build", "actual": "opencode"}
        result = interpret(packet, human_owner="Rich")
        self.assertTrue(result.ok)
        self.assertIsNone(result.packet["model"]["actual"])
        self.assertIsNone(result.packet["runner"]["actual"])
        self.assertEqual(result.packet["model"]["requested"], "grok-4.7")

    def test_invalid_yaml_is_blocked(self) -> None:
        result = load_packet_text(":\n\tnope", human_owner="Rich")
        self.assertFalse(result.ok)
        self.assertEqual(result.packet["status"], "blocked")
        self.assertIn("invalid YAML", result.reason or "")

    def test_round_trip_preserves_goal(self) -> None:
        text = (ROOT / "examples" / "sample-readonly.yaml").read_text(encoding="utf-8")
        parsed = parse_yaml(text)
        result = interpret(parsed, human_owner="Rich")
        self.assertIn("Do not change any files.", result.packet["goal"])


if __name__ == "__main__":
    unittest.main()
