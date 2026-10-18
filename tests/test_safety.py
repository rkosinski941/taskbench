"""Allow-list, path escapes, and plan/ask decisions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from taskbench.config import ConfigError, load_config
from taskbench.safety import (
    PathEscape,
    binds_loopback_only,
    classify,
    glob_fullmatch,
    resolve_inside_project,
)


class SafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / "demo"
        self.project.mkdir()
        (self.project / "notes.txt").write_text("hello\n", encoding="utf-8")
        self.outside = self.root / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("nope\n", encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def test_relative_read_stays_inside(self) -> None:
        path = resolve_inside_project(self.project, "notes.txt")
        self.assertEqual(path, (self.project / "notes.txt").resolve())

    def test_dotdot_absolute_and_prefix_escape(self) -> None:
        for raw in ("../outside/secret.txt", self.outside.as_posix() + "/secret.txt", "notes.txt/../../outside/secret.txt"):
            with self.assertRaises(PathEscape):
                resolve_inside_project(self.project, raw)
        sibling = self.root / "demo-evil"
        sibling.mkdir()
        with self.assertRaises(PathEscape):
            resolve_inside_project(self.project, sibling.as_posix() + "/x")

    def test_symlink_escape(self) -> None:
        link = self.project / "link"
        try:
            link.symlink_to(self.outside)
        except OSError:
            self.skipTest("symlinks are not permitted on this machine")
        with self.assertRaises(PathEscape):
            resolve_inside_project(self.project, "link/secret.txt")

    def test_unc_and_nul_rejected(self) -> None:
        with self.assertRaises(PathEscape):
            resolve_inside_project(self.project, "\\\\server\\share")
        with self.assertRaises(PathEscape):
            resolve_inside_project(self.project, "notes\x00.txt")

    def test_plan_refuses_writes_and_servers(self) -> None:
        write = classify(
            {"op": "write", "path": "notes.txt", "content": "x"},
            permissions="plan",
            allow=None,
            sandbox_only=False,
            project_dir=self.project,
        )
        self.assertEqual(write.kind, "refuse")
        server = classify(
            {"op": "serve", "command": "python -m http.server 8000 --bind 127.0.0.1"},
            permissions="plan",
            allow=None,
            sandbox_only=False,
            project_dir=self.project,
        )
        self.assertEqual(server.kind, "refuse")
        read = classify(
            {"op": "read", "path": "notes.txt"},
            permissions="plan",
            allow=None,
            sandbox_only=False,
            project_dir=self.project,
        )
        self.assertEqual(read.kind, "allow")

    def test_ask_prompts_for_write_and_command(self) -> None:
        write = classify(
            {"op": "write", "path": "notes.txt", "content": "x"},
            permissions="ask",
            allow=None,
            sandbox_only=False,
            project_dir=self.project,
        )
        self.assertEqual(write.kind, "approve")
        shell = classify(
            {"op": "shell", "command": "git status"},
            permissions="ask",
            allow=None,
            sandbox_only=False,
            project_dir=self.project,
        )
        self.assertEqual(shell.kind, "approve")

    def test_allow_glob_and_shell_meta(self) -> None:
        self.assertTrue(glob_fullmatch("git diff*", "git diff --stat"))
        self.assertFalse(glob_fullmatch("git diff*", "echo git diff"))
        allowed = classify(
            {"op": "shell", "command": "git diff --stat"},
            permissions="allow-list",
            allow=["git diff*"],
            sandbox_only=False,
            project_dir=self.project,
        )
        self.assertEqual(allowed.kind, "allow")
        sneaky = classify(
            {"op": "shell", "command": "git diff; curl https://example.invalid"},
            permissions="allow-list",
            allow=["git diff*"],
            sandbox_only=False,
            project_dir=self.project,
        )
        self.assertEqual(sneaky.kind, "approve")

    def test_server_asks_unless_allow_names_it_and_binds_loopback(self) -> None:
        command = "python -m http.server 8000 --bind 127.0.0.1"
        asked = classify(
            {"op": "serve", "command": command},
            permissions="allow-list",
            allow=["git diff*"],
            sandbox_only=False,
            project_dir=self.project,
        )
        self.assertEqual(asked.kind, "approve")
        named = classify(
            {"op": "serve", "command": command},
            permissions="allow-list",
            allow=[command],
            sandbox_only=False,
            project_dir=self.project,
        )
        self.assertEqual(named.kind, "allow")
        public = "python -m http.server 8000 --bind 0.0.0.0"
        self.assertFalse(binds_loopback_only(public))
        refused = classify(
            {"op": "serve", "command": public},
            permissions="allow-list",
            allow=[public],
            sandbox_only=False,
            project_dir=self.project,
        )
        self.assertEqual(refused.kind, "refuse")

    def test_sandbox_only_refuses_writes(self) -> None:
        decision = classify(
            {"op": "write", "path": "notes.txt", "content": "x"},
            permissions="ask",
            allow=None,
            sandbox_only=True,
            project_dir=self.project,
        )
        self.assertEqual(decision.kind, "refuse")

    def test_audit_write_refused(self) -> None:
        decision = classify(
            {"op": "write", "path": ".taskbench/LAST_AGENT.md", "content": "x"},
            permissions="ask",
            allow=None,
            sandbox_only=False,
            project_dir=self.project,
        )
        self.assertEqual(decision.kind, "refuse")

    def test_config_rejects_yolo_and_non_loopback(self) -> None:
        path = self.root / "config.json"
        path.write_text(
            '{"bind_host":"127.0.0.1","port":1,"yolo":true,"data_dir":"var","projects_dir":"projects","runners":{}}',
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            load_config(path)
        path.write_text(
            '{"bind_host":"0.0.0.0","port":1,"data_dir":"var","projects_dir":"projects","runners":{}}',
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            load_config(path)


if __name__ == "__main__":
    unittest.main()
