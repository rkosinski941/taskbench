"""Round-2 review fixes. These do not launch a real Grok or Tailscale binary."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from taskbench.adapters.grok_build import GrokBuildAdapter
from taskbench.config import ConfigError, load_config
from taskbench.funnel_check import funnel_plan, proxies_port
from taskbench.permissions import (
    WINDOWS_SANDBOX_BANNER,
    PermissionFlagError,
    apply_config_home,
    check_permission_args,
    ensure_pinned_args,
    needs_you_message,
    prefix_output,
    prepare_grok_config_home,
    scan_project_permission_rules,
    scrub_environment,
    unexpected_permission,
)
from taskbench.process_tree import ManagedProcess, ProcessTreeError, windows_process_command
from taskbench.task_format import parse_yaml
from tests.support import ROOT, TempApp, base_packet, packet_status, run_until, write_packet
from tests.test_review_fixes import _runner


class PinOrderTests(unittest.TestCase):
    def test_pins_are_first_and_end_of_options_is_rejected(self) -> None:
        argv = ensure_pinned_args(
            [
                "grok",
                "--cwd",
                "x",
                "--permission-mode",
                "dontAsk",
                "--sandbox",
                "strict",
                "--prompt-file",
                "p",
            ]
        )
        self.assertEqual(
            argv,
            [
                "grok",
                "--permission-mode",
                "dontAsk",
                "--sandbox",
                "strict",
                "--output-format",
                "streaming-messages-json",
                "--cwd",
                "x",
                "--prompt-file",
                "p",
            ],
        )
        interpreter = ensure_pinned_args(
            [sys.executable, "fake_runner.py", "--cwd", "x", "--permission-mode", "dontAsk"]
        )
        self.assertEqual(interpreter[1], "fake_runner.py")
        self.assertEqual(
            interpreter[2:8],
            ["--permission-mode", "dontAsk", "--sandbox", "strict", "--output-format", "streaming-messages-json"],
        )
        for args in (["--"], ["--cwd", "x", "--", "--allow", "Bash"], ["--prompt", "--"]):
            with self.subTest(args=args):
                with self.assertRaises(PermissionFlagError):
                    check_permission_args(args)
                with self.assertRaises(PermissionFlagError):
                    ensure_pinned_args(["grok", *args])

    def test_config_rejects_end_of_options(self) -> None:
        text = (ROOT / "config.example.json").read_text(encoding="utf-8")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = Path(tmp.name) / "config.json"
        target.write_text(text.replace('"--cwd"', '"--", "--cwd"', 1), encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            load_config(target)
        self.assertIn("end-of-options", str(caught.exception))


class ConfigHomeTests(unittest.TestCase):
    def test_home_is_recreated_empty_and_pinned(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        dirty = prepare_grok_config_home(root)
        (dirty / ".grok").mkdir()
        (dirty / ".grok" / "config.toml").write_text(
            'allow = ["SECRET_ALLOW_VALUE"]\n',
            encoding="utf-8",
        )
        home = prepare_grok_config_home(root)
        self.assertEqual(list(home.iterdir()), [])
        env = apply_config_home(
            {"PATH": "/usr/bin", "HOME": "/real/home", "USERPROFILE": "/real/profile", "GROK_HOME": "/injected"},
            home,
        )
        self.assertEqual(env["HOME"], str(home))
        self.assertEqual(env["USERPROFILE"], str(home))
        self.assertEqual(env["GROK_HOME"], str(home))
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("SECRET_ALLOW_VALUE", " ".join(env.values()))
        pinned = GrokBuildAdapter(_runner()).environment(
            {"PATH": "/usr/bin", "HOME": "/real", "GROK_PERMISSION_MODE": "always-approve"},
            config_home=home,
        )
        self.assertEqual(pinned["HOME"], str(home))
        self.assertNotIn("GROK_PERMISSION_MODE", pinned)


class ProjectRuleTests(TempApp):
    def test_allow_rule_blocks_with_keys_only(self) -> None:
        _config, _app, watcher = self.make(scenario="echo", can_ask=False)
        project = self.root / "projects" / "demo-canvas"
        grok = project / ".grok"
        grok.mkdir()
        grok.joinpath("config.toml").write_text(
            'allow = ["SECRET_RULE_VALUE"]\n',
            encoding="utf-8",
        )
        write_packet(
            self.store.inbox,
            base_packet(id="rules-1", permissions="plan", timeout_s=30),
        )
        run_until(watcher, lambda: packet_status(self.store, "rules-1") == "blocked")
        saved = parse_yaml((self.store.outbox / "rules-1.json").read_text(encoding="utf-8"))
        reason = saved["result"]["blocked_reason"]
        self.assertTrue(reason.startswith("Needs you"))
        self.assertIn(".grok/config.toml", reason)
        self.assertIn("allow", reason)
        self.assertNotIn("SECRET_RULE_VALUE", reason)
        output = (self.store.outbox / "rules-1.output.log").read_text(encoding="utf-8")
        self.assertNotIn("SECRET_RULE_VALUE", output)
        self.assertIsNone(watcher.proc)

    def test_deny_only_settings_still_run(self) -> None:
        _config, _app, watcher = self.make(scenario="echo", can_ask=False)
        claude = self.root / "projects" / "demo-canvas" / ".claude"
        claude.mkdir()
        claude.joinpath("settings.json").write_text(
            '{"permissions": {"deny": ["Bash(rm *)"]}}\n',
            encoding="utf-8",
        )
        write_packet(
            self.store.inbox,
            base_packet(id="rules-ok", permissions="plan", timeout_s=30),
        )
        run_until(watcher, lambda: packet_status(self.store, "rules-ok") == "done")

    def test_scanner_names_keys_not_values(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        nested = root / "src" / ".claude"
        nested.mkdir(parents=True)
        nested.joinpath("settings.local.json").write_text(
            '{"defaultMode": "bypassPermissions", "permissions": {"allow": ["SECRET"]}}\n',
            encoding="utf-8",
        )
        (root / ".grok").mkdir()
        (root / ".grok" / "config.toml").write_text("yolo = false\ndeny = ['rm']\n", encoding="utf-8")
        findings = scan_project_permission_rules(root)
        message = needs_you_message(findings)
        self.assertIn("src/.claude/settings.local.json", message)
        self.assertIn("defaultMode", message)
        self.assertIn("allow", message)
        self.assertNotIn("SECRET", message)
        self.assertNotIn("bypassPermissions", message)
        self.assertNotIn(".grok/config.toml", message)


class ModeDetectionTests(unittest.TestCase):
    def test_unstructured_text_is_not_treated_as_a_mode(self) -> None:
        adapter = GrokBuildAdapter(_runner())
        self.assertIsNone(adapter.parse_line("permissionMode: bypassPermissions\n"))
        self.assertIsNone(
            unexpected_permission({"type": "text", "text": "permissionMode bypassPermissions"})
        )
        self.assertIsNone(unexpected_permission({"type": "result", "summary": "done"}))
        reason = unexpected_permission(
            {"type": "permission_mode", "mode": "always-approve", "sandbox": "strict"}
        )
        self.assertIn("unexpected permission mode", reason or "")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("streaming-messages-json", readme)
        self.assertIn("docs/grok-cli-modes.md", readme)
        modes = (ROOT / "docs" / "grok-cli-modes.md").read_text(encoding="utf-8")
        self.assertIn("dontAsk", modes)
        self.assertIn("permissionMode", modes)


class BannerTests(unittest.TestCase):
    def test_banner_text_and_output_prefix(self) -> None:
        self.assertEqual(
            WINDOWS_SANDBOX_BANNER,
            "No OS sandbox on Windows; only taskbench approvals protect you",
        )
        prefixed = prefix_output("hello\n", platform="nt")
        self.assertTrue(prefixed.startswith(WINDOWS_SANDBOX_BANNER + "\n"))
        self.assertTrue(prefix_output(prefixed, platform="nt").startswith(WINDOWS_SANDBOX_BANNER))
        self.assertEqual(prefix_output("hello\n", platform="posix"), "hello\n")


class CompanionEnvTests(unittest.TestCase):
    def test_sidecar_gets_the_allow_list(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        marker = folder / "child-env.txt"
        os.environ["PYTHONPATH"] = "/tmp/injected-pythonpath"
        self.addCleanup(lambda: os.environ.pop("PYTHONPATH", None))
        parent = ManagedProcess(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=folder,
            env=scrub_environment(os.environ.copy()),
        )
        self.addCleanup(parent.kill_tree)
        child_env = scrub_environment(os.environ.copy())
        self.assertNotIn("PYTHONPATH", child_env)
        code = (
            "import os, pathlib; "
            f"pathlib.Path({str(marker)!r}).write_text("
            "'\\n'.join(f'{k}={v}' for k, v in os.environ.items()), encoding='utf-8')"
        )
        parent.spawn_companion([sys.executable, "-c", code], folder, child_env)
        deadline = __import__("time").time() + 8
        while not marker.is_file() and __import__("time").time() < deadline:
            __import__("time").sleep(0.05)
        self.assertTrue(marker.is_file(), "companion did not write its environment")
        text = marker.read_text(encoding="utf-8")
        self.assertNotIn("PYTHONPATH=", text)
        self.assertIn("TASKBENCH_APPROVALS=1", text)
        self.assertNotIn("injected-pythonpath", text)


class CmdShimTests(unittest.TestCase):
    def test_cmd_shim_uses_cmd_exe_without_shell(self) -> None:
        def exists(path: str) -> bool:
            return path.replace("/", "\\").lower().endswith("\\grok.cmd")

        application, command = windows_process_command(
            ["grok", "--permission-mode", "dontAsk"],
            path_env=r"C:\Tools",
            pathext=".CMD;.EXE",
            comspec=r"C:\Windows\System32\cmd.exe",
            exists=exists,
        )
        self.assertEqual(application, r"C:\Windows\System32\cmd.exe")
        self.assertIn("/d", command)
        self.assertIn("/s", command)
        self.assertIn("/c", command)
        self.assertIn("grok.cmd", command.lower())
        self.assertNotIn("shell=True", command)
        with self.assertRaises(ProcessTreeError):
            windows_process_command(
                ["grok.cmd", "safe&whoami"],
                path_env=r"C:\Tools",
                pathext=".CMD",
                comspec="cmd.exe",
                exists=lambda path: path.lower().endswith("grok.cmd"),
            )

    def test_exe_is_not_wrapped_in_cmd(self) -> None:
        application, command = windows_process_command(
            [r"C:\Python\python.exe", "-c", "print(1)"],
            exists=lambda _path: True,
        )
        self.assertEqual(application, r"C:\Python\python.exe")
        self.assertNotIn("/c", command)


class FunnelPlanTests(unittest.TestCase):
    def test_scoped_off_is_the_only_disable_command(self) -> None:
        plan = funnel_plan(
            serve_text="http://127.0.0.1:8765\nhttps://box.ts.net (Funnel on)\n",
            serve_json="{}",
            funnel_text="",
            port=8765,
        )
        self.assertEqual(plan["kind"], "on-scoped")
        self.assertEqual(plan["command"], ["tailscale", "funnel", "--https=443", "off"])

    def test_unrelated_logged_out_and_port_boundary(self) -> None:
        unrelated = funnel_plan(
            serve_text="https://other.ts.net (Funnel on)\nhttp://127.0.0.1:9\n",
            port=8765,
        )
        self.assertEqual(unrelated["kind"], "unrelated")
        self.assertIsNone(unrelated["command"])
        logged_out = funnel_plan(serve_text="Logged out.\n", serve_json="not-json")
        self.assertEqual(logged_out["kind"], "not-logged-in")
        unreadable = funnel_plan(serve_json="{")
        self.assertEqual(unreadable["kind"], "unreadable")
        self.assertFalse(proxies_port("127.0.0.1:87650", 8765))
        self.assertTrue(proxies_port("127.0.0.1:8765", 8765))
        many = funnel_plan(
            serve_text="(Funnel on)\n(Funnel on)\n127.0.0.1:8765\n",
            port=8765,
        )
        self.assertEqual(many["kind"], "unrelated")


class PageBannerTests(TempApp):
    def test_windows_banner_and_needs_you_label(self) -> None:
        import taskbench.ui.server as server

        _config, app, _watcher = self.make()
        original = server.windows_sandbox_banner
        server.windows_sandbox_banner = lambda platform=None: WINDOWS_SANDBOX_BANNER
        self.addCleanup(lambda: setattr(server, "windows_sandbox_banner", original))
        page = server.render_index(app, "token", None)
        self.assertIn(WINDOWS_SANDBOX_BANNER, page)
        self.assertIn("sandbox-banner", page)
        packet = base_packet(id="needs-1", status="blocked")
        packet["result"] = {
            "summary": None,
            "artifacts": [],
            "blocked_reason": "Needs you: project settings contain auto-approve or allow rules",
            "completed_at": "2026-10-18T00:00:00+00:00",
        }
        card = server.render_task(app, packet, "token", None)
        self.assertIn("Needs you", card)
        self.assertIn("data-status='blocked'", card)


if __name__ == "__main__":
    unittest.main()
