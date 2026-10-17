"""Tests for the YAML subset used by joy-feeder packets."""

from __future__ import annotations

import unittest
from pathlib import Path

from taskbench.yaml_subset import YamlSubsetError, dump, load

ROOT = Path(__file__).resolve().parents[1]
READONLY = (ROOT / "examples" / "sample-readonly.yaml").read_text(encoding="utf-8")
APPROVAL = (ROOT / "examples" / "sample-needs-approval.yaml").read_text(encoding="utf-8")


class YamlSubsetTests(unittest.TestCase):
    def test_readonly_sample(self) -> None:
        data = load(READONLY)
        self.assertEqual(data["schema"], "joy-feeder.task/v2")
        self.assertEqual(data["id"], "jf-sample-readonly-001")
        self.assertEqual(data["from"], "Joy Feeder")
        self.assertEqual(data["runner"]["requested"], "grok-build")
        self.assertIsNone(data["runner"]["actual"])
        self.assertIsNone(data["model"]["requested"])
        self.assertEqual(data["project"], "demo-canvas")
        self.assertEqual(data["permissions"], "plan")
        self.assertEqual(data["timeout_s"], 600)
        self.assertTrue(data["goal"].startswith("Review canvas-v2.html"))
        self.assertIn("Do not change any files.", data["goal"])
        self.assertNotIn("\n", data["goal"].rstrip("\n"))
        self.assertEqual(
            data["constraints"],
            [
                "Read-only. No edits, no servers, no installs.",
                "Keep the report under 300 words.",
            ],
        )
        self.assertEqual(data["return"]["path"], "outbox/joy-feeder/done/")
        self.assertIsNone(data["due"])
        self.assertEqual(data["files"], ["canvas-v2.html"])
        self.assertIsNone(data["review_by"])
        self.assertIsNone(data["result"])
        self.assertEqual(data["status"], "incoming")

    def test_approval_sample(self) -> None:
        data = load(APPROVAL)
        self.assertEqual(data["from"], "Odd Jobs")
        self.assertEqual(data["runner"]["requested"], "opencode")
        self.assertEqual(data["model"]["requested"], "example-small-model")
        self.assertIn("Hello, canvas.", data["goal"])
        self.assertEqual(data["permissions"], "ask")
        self.assertEqual(data["timeout_s"], 900)
        self.assertEqual(data["review_by"]["runner"], "grok-build")
        self.assertIsNone(data["review_by"]["model"])
        self.assertEqual(data["review_by"]["permissions"], "plan")
        self.assertEqual(data["priority"], "low")

    def test_flow_sequence_and_round_trip(self) -> None:
        original = {
            "schema": "joy-feeder.task/v2",
            "id": "round-1",
            "from": "Joy Feeder",
            "runner": {"requested": "grok-build", "actual": None},
            "result": {"artifacts": ["app.js"], "blocked_reason": None},
            "approvals": [
                {"at": "2026-10-17T18:05:02-04:00", "command": "git diff", "decision": "approved"}
            ],
            "retro": {
                "effort": "Tracing where the counter is reset.",
                "unclear": "Whether frame 0 should be shown.",
                "would_change": "Put the expected output in success_criteria.",
                "fit_1_5": 4,
            },
            "timeout_s": 600,
            "goal": "Fix the off-by-one.\n",
        }
        again = load(dump(original))
        self.assertEqual(again, original)

    def test_samples_round_trip_values(self) -> None:
        for text in (READONLY, APPROVAL):
            parsed = load(text)
            self.assertEqual(load(dump(parsed)), parsed)

    def test_inline_comment_and_quotes(self) -> None:
        parsed = load('note: "hash # inside"  # real comment\ncount: 2\n')
        self.assertEqual(parsed["note"], "hash # inside")
        self.assertEqual(parsed["count"], 2)

    def test_tabs_rejected(self) -> None:
        with self.assertRaises(YamlSubsetError):
            load("key:\n\tvalue: 1\n")

    def test_literal_block(self) -> None:
        parsed = load("body: |\n  line one\n  line two\n")
        self.assertEqual(parsed["body"], "line one\nline two\n")


if __name__ == "__main__":
    unittest.main()
