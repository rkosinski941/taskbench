"""Crash-safe moves: one active run, recovery keeps the further copy."""

from __future__ import annotations

import unittest

from taskbench.state import StateStore
from taskbench.task_format import emit_yaml
from tests.support import TempApp, base_packet, packet_status, run_until, write_packet


class StateMachineTests(TempApp):
    def test_one_running_at_a_time(self) -> None:
        _config, _app, watcher = self.make(scenario="hang", extra_args=["--marker-dir", str(self.root / "mark")])
        (self.root / "mark").mkdir()
        write_packet(self.store.inbox, base_packet(id="a-hang", timeout_s=30))
        write_packet(self.store.inbox, base_packet(id="b-next", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "a-hang") == "running")
        self.assertEqual(packet_status(self.store, "b-next"), "claimed")
        self.assertEqual(len(list(self.store.running.glob("*.yaml"))), 1)

    def test_recover_keeps_further_state_and_drops_tmp(self) -> None:
        store = StateStore(self.root / "var")
        packet = base_packet(id="dup-1", status="claimed")
        (store.inbox / "dup-1.yaml").write_text(emit_yaml(packet), encoding="utf-8")
        store.write_packet("claimed", packet)
        (store.inbox / "partial.yaml.tmp").write_text("nope", encoding="utf-8")
        notes = store.recover()
        self.assertFalse((store.inbox / "dup-1.yaml").exists())
        self.assertTrue((store.claimed / "dup-1.yaml").exists())
        self.assertFalse((store.inbox / "partial.yaml.tmp").exists())
        self.assertTrue(any("kept claimed" in note for note in notes))

    def test_terminal_wins_over_running(self) -> None:
        store = StateStore(self.root / "var2")
        packet = base_packet(id="done-1", status="done")
        store.write_packet("running", base_packet(id="done-1", status="running"))
        store.write_packet("outbox", packet)
        store.recover()
        self.assertFalse((store.running / "done-1.yaml").exists())
        self.assertTrue((store.outbox / "done-1.yaml").exists())


if __name__ == "__main__":
    unittest.main()
