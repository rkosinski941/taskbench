"""Process-tree kill and loopback port scan."""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

from taskbench.process_tree import KILL_METHOD, ManagedProcess, listening_ports, pid_alive

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_runner.py"


class ProcessTreeTests(unittest.TestCase):
    def test_kill_method(self) -> None:
        if os.name == "nt":
            self.assertEqual(KILL_METHOD, "job_object")
        else:
            self.assertEqual(KILL_METHOD, "killpg")

    def test_tree_kill_stops_grandchildren(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        marker = Path(tmp.name)
        proc = ManagedProcess(
            [sys.executable, str(FAKE), "--scenario", "tree", "--marker-dir", str(marker)],
            cwd=marker,
            env=os.environ.copy(),
        )
        self.addCleanup(proc.kill_tree)
        deadline = time.time() + 5
        while time.time() < deadline and not (marker / "ready").is_file():
            time.sleep(0.05)
        self.assertTrue((marker / "ready").is_file())
        pids = [
            int((marker / name).read_text(encoding="utf-8"))
            for name in ("child.pid", "grandchild-started.pid", "leaf.pid")
        ]
        proc.kill_tree()
        time.sleep(0.2)
        for pid in pids:
            self.assertFalse(pid_alive(pid), pid)

    def test_listening_port_seen_then_closed(self) -> None:
        if not sys.platform.startswith("linux"):
            self.skipTest("port scan test uses /proc")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            found = listening_ports({os.getpid()})
            self.assertIn(port, found)
        finally:
            sock.close()
        self.assertNotIn(port, listening_ports({os.getpid()}))


if __name__ == "__main__":
    unittest.main()
