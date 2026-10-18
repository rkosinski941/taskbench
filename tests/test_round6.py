"""Round-6 review fixes. These do not launch a real Grok binary."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path

from taskbench.cli import watch_once
from taskbench.config import load_config
from taskbench.permissions import (
    _read_deny_patterns,
    command_warnings,
    prepare_grok_config_home,
    write_grok_deny_rules,
)
from taskbench.safety import classify
from taskbench.ui.server import TaskbenchServer, render_index, render_task
from tests.support import ROOT, TempApp, base_packet


def _decide(folder: Path, command: str, *, permissions: str = "ask", allow: list[str] | None = None) -> tuple[str, str]:
    decision = classify(
        {"op": "shell", "command": command},
        permissions=permissions,
        allow=allow or [],
        sandbox_only=False,
        project_dir=folder,
    )
    return decision.kind, decision.reason


class DenySectionTests(unittest.TestCase):
    def test_toml_deny_rules_use_the_documented_permission_key(self) -> None:
        scratch = ROOT / "test-scratch"
        scratch.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        home = prepare_grok_config_home(root / "data")
        write_grok_deny_rules(
            home,
            {"HOME": "/real/home", "USERPROFILE": r"C:\Users\Owner", "PATH": "/usr/bin"},
        )
        for relative in ("config.toml", Path(".grok") / "config.toml"):
            text = (home / relative).read_text(encoding="utf-8")
            parsed = tomllib.loads(text)
            self.assertIn("permission", parsed)
            self.assertNotIn("permissions", parsed)
            for side in ("claude", "cursor"):
                compat = parsed["compat"][side]
                self.assertIs(compat["hooks"], False)
                self.assertIs(compat["mcps"], False)
            deny = parsed["permission"]["deny"]
            self.assertTrue(deny)
            self.assertTrue(all(item.startswith("Read(") for item in deny))
            self.assertTrue(any(str(home) in item for item in deny))
            if os.name == "nt":
                folded = " ".join(deny).lower()
                self.assertIn(r"c:\users\owner\.grok", folded)
                self.assertIn("c:/users/owner/.grok", folded)
        settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertTrue(settings["permissions"]["deny"])

    def test_windows_paths_are_written_in_both_letter_cases(self) -> None:
        patterns = _read_deny_patterns([r"C:\Users\Owner\.grok"], windows_case=True)
        for item in (
            r"Read(C:\Users\Owner\.grok)",
            r"Read(c:\users\owner\.grok)",
            "Read(C:/Users/Owner/.grok)",
            "Read(c:/users/owner/.grok)",
            r"Read(C:\Users\Owner\.grok/**)",
            r"Read(c:\users\owner\.grok/**)",
        ):
            self.assertIn(item, patterns)
        plain = _read_deny_patterns([r"C:\Users\Owner\.grok"], windows_case=False)
        self.assertIn(r"Read(C:\Users\Owner\.grok)", plain)
        self.assertNotIn(r"Read(c:\users\owner\.grok)", plain)


class WatcherTickHealthTests(TempApp):
    def test_repeated_tick_failures_are_unhealthy_while_the_thread_lives(self) -> None:
        import http.client

        _config, app, watcher = self.make()

        def boom() -> None:
            raise RuntimeError("tick failed")

        watcher.tick = boom  # type: ignore[method-assign]
        app.begin_watch()
        for _ in range(app.TICK_FAILURE_LIMIT):
            watch_once(app)
        holder = threading.Thread(target=lambda: time.sleep(30), daemon=True)
        holder.start()
        app.watcher_thread = holder
        self.assertTrue(holder.is_alive())
        self.assertFalse(app.watcher_healthy())
        self.assertEqual(app.watcher_problem(), "watcher tick is failing")
        server = TaskbenchServer(app, 0)
        self.server = server
        port = int(server.server_address[1])
        threading.Thread(target=server.serve_forever, daemon=True).start()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/health")
        response = conn.getresponse()
        body = response.read()
        status = response.status
        conn.close()
        self.assertEqual(status, 503)
        self.assertIn(b'"watcher":"down"', body)
        self.assertIn(b"watcher tick is failing", body)
        page = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        page.request("GET", "/")
        html = page.getresponse().read()
        page.close()
        self.assertIn(b"data-watcher='down'", html)
        app.note_tick_ok()
        self.assertTrue(app.watcher_healthy())

    def test_no_successful_tick_within_the_window_is_unhealthy(self) -> None:
        _config, app, _watcher = self.make()
        app.begin_watch()
        app.tick_started = time.monotonic() - app.TICK_SUCCESS_WINDOW_S - 1
        self.assertEqual(app.watcher_problem(), "watcher has not completed a tick")
        self.assertFalse(app.watcher_healthy())
        app.note_tick_ok()
        app.last_tick_ok = time.monotonic()
        self.assertTrue(app.watcher_healthy())


class AuthBannerTests(unittest.TestCase):
    def test_copy_auth_json_banner_is_on_the_rendered_page(self) -> None:
        scratch = ROOT / "test-scratch"
        scratch.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "var").mkdir()
        (root / "projects").mkdir()
        path = root / "config.json"

        def page(flag: bool) -> str:
            path.write_text(
                json.dumps(
                    {
                        "bind_host": "127.0.0.1",
                        "port": 8765,
                        "data_dir": "var",
                        "projects_dir": "projects",
                        "copy_auth_json": flag,
                    }
                ),
                encoding="utf-8",
            )
            from taskbench.ui.server import App

            app = App(load_config(path))
            return render_index(app, "", None)

        shown = page(True)
        self.assertIn("data-copy-auth='on'", shown)
        self.assertIn("copy_auth_json is on", shown)
        self.assertIn("auth-warning", shown)
        self.assertIn("role='alert'", shown)
        hidden = page(False)
        self.assertNotIn("data-copy-auth", hidden)
        self.assertNotIn("copy_auth_json is on", hidden)


class CommandWarningTests(TempApp):
    def test_each_hidden_form_needs_approval_with_a_warning(self) -> None:
        folder = self.root / "projects" / "demo-canvas"
        encoded = base64.b64encode("grok --yolo".encode("utf-16-le")).decode("ascii")
        cases = {
            "caret": "g^rok --y^olo",
            "encoded": f"powershell -EncodedCommand {encoded}",
            "encoded-short": f"pwsh -enc {encoded}",
            "splat": "echo hi @args",
            "expansion": "echo $env:PATH",
            "grok-yolo": "GROK_YOLO=1 grok -p hi",
            "grok-sandbox": "$env:GROK_SANDBOX='off'; grok -p hi",
            "config": r"grok --config C:\other\config.toml -p hi",
            "config-short": r"grok -c C:\other\config.toml -p hi",
        }
        expected = {
            "caret": "caret escape",
            "encoded": "EncodedCommand",
            "encoded-short": "EncodedCommand",
            "splat": "splatting",
            "expansion": "variable expansion",
            "grok-yolo": "GROK_*",
            "grok-sandbox": "GROK_*",
            "config": "another config",
            "config-short": "another config",
        }
        for name, command in cases.items():
            with self.subTest(name=name):
                kind, reason = _decide(folder, command, permissions="allow-list", allow=["*"])
                self.assertEqual(kind, "approve")
                self.assertIn(expected[name], reason)
                self.assertTrue(command_warnings(command))
        kind, reason = _decide(folder, "echo hi", permissions="allow-list", allow=["*"])
        self.assertEqual(kind, "allow", reason)
        self.assertEqual(command_warnings("echo hi"), [])
        self.assertEqual(command_warnings("git -c color.ui=auto status"), [])
        self.assertEqual(command_warnings('python -c "print(1)"'), [])
        refused, _why = _decide(folder, "grok --yolo")
        self.assertEqual(refused, "refuse")
        decoded_kind, decoded_reason = _decide(folder, cases["encoded"])
        self.assertEqual(decoded_kind, "approve")
        self.assertIn("decoded command", decoded_reason)
        self.assertIn("always-approve", decoded_reason)

    def test_approval_prompt_shows_the_warning(self) -> None:
        _config, app, _watcher = self.make()
        packet = base_packet(id="warn-1", status="needs-approval")
        app.store.write_packet("needs-approval", packet)
        app.store.write_meta("warn-1", {"pending_command": "echo hi @args"})
        html = render_task(app, packet, "", None)
        self.assertIn("data-approval-warning='1'", html)
        self.assertIn("approval-warning", html)
        self.assertIn("splatting", html)
        self.assertIn("echo hi @args", html)


class LaptopPlanPowerShellTests(unittest.TestCase):
    def test_plan_is_powershell_and_uses_the_permission_section(self) -> None:
        plan = (ROOT / "docs" / "laptop-test-plan.md").read_text(encoding="utf-8")
        self.assertNotIn("%USERPROFILE%", plan)
        self.assertIn("$env:USERPROFILE", plan)
        self.assertIn("[permission]", plan)
        self.assertIn('allow = ["Bash(echo TASKBENCH-PROBE*)"]', plan)
        self.assertIn("-p, --single", plan)
        self.assertIn("--prompt-file", plan)
        self.assertIsNone(__import__("re").search(r"grok --prompt(?!-file)", plan))
        self.assertIn("--permission-mode dontAsk", plan)
        self.assertIn("--output-format streaming-messages-json", plan)
        self.assertIn("Read-Host -AsSecureString", plan)
        self.assertIn("Remove-Item Env:\\XAI_API_KEY", plan)
        self.assertIn("$env:USERPROFILE\\taskbench-probe\\outer\\inner", plan)
        self.assertIn("Rename-Item", plan)
        self.assertIn("Get-Command grok", plan)
        self.assertIn("grok.cmd", plan)
        self.assertIn("Remove-Item -LiteralPath", plan)
        self.assertIn("only the `.grok` rule", plan)
        self.assertIn("only the `.claude` rule", plan)


if __name__ == "__main__":
    unittest.main()
