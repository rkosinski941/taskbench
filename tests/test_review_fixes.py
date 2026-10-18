"""Review fixes: pinned Grok flags, env scrub, confinement refusal."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from taskbench.adapters.grok_build import GrokBuildAdapter
from taskbench.config import ConfigError, RunnerConfig, load_config
from taskbench.permissions import PermissionFlagError, check_permission_args, scrub_environment
from taskbench.process_tree import ManagedProcess, ProcessTreeError
from taskbench.task_format import parse_yaml
from tests.support import ROOT, TempApp, base_packet, packet_status, run_until, write_packet


def _runner(**kwargs: object) -> RunnerConfig:
    body = dict(
        name="grok-build",
        adapter="grok_build",
        executable="grok",
        args_template=["--prompt-file", "{prompt_file}", "--cwd", "{project_dir}"],
        can_ask=False,
    )
    body.update(kwargs)
    return RunnerConfig(**body)  # type: ignore[arg-type]


class PinnedPermissionTests(unittest.TestCase):
    def test_default_args_are_pinned_and_can_ask_defaults_false(self) -> None:
        loaded = load_config(ROOT / "config.example.json")
        runner = loaded.runners["grok-build"]
        self.assertFalse(runner.can_ask)
        argv = GrokBuildAdapter(runner).build_command(
            prompt_file=Path("prompt.md"),
            project_dir=Path("."),
            timeout_s=30,
        )
        self.assertEqual(
            argv[:7],
            ["grok", "--permission-mode", "dontAsk", "--sandbox", "strict", "--output-format", "streaming-messages-json"],
        )

        omitted = _runner()
        self.assertFalse(omitted.can_ask)
        pinned = GrokBuildAdapter(omitted).build_command(
            prompt_file=Path("prompt.md"),
            project_dir=Path("."),
            timeout_s=30,
        )
        self.assertEqual(
            pinned[:7],
            ["grok", "--permission-mode", "dontAsk", "--sandbox", "strict", "--output-format", "streaming-messages-json"],
        )
        self.assertEqual(pinned[7:9], ["--prompt-file", "prompt.md"])

    def test_banned_and_unknown_permission_flags_are_rejected(self) -> None:
        banned = [
            ["--always-approve"],
            ["--always-approve=true"],
            ["--Always-Approve"],
            ["--always_approve"],
            ["--yolo"],
            ["--allow", "Bash(git *)"],
            ["--allow=Read"],
            ["--auto-approve"],
            ["--auto-approve=yes"],
            ["--allowedTools", "Bash"],
            ["--dangerously-skip-permissions"],
            ["--permission-mode", "bypassPermissions"],
            ["--permission-mode=auto"],
            ["--permission-mode", "always-approve"],
            ["--sandbox", "off"],
            ["--sandbox=workspace"],
            ["--permission-profile", "custom"],
            ["--autoapprove"],
        ]
        for args in banned:
            with self.subTest(args=args):
                with self.assertRaises(PermissionFlagError):
                    check_permission_args(args)
                with self.assertRaises(ValueError):
                    GrokBuildAdapter(
                        _runner(args_template=args + ["--prompt-file", "{prompt_file}"])
                    ).build_command(
                        prompt_file=Path("prompt.md"),
                        project_dir=Path("."),
                        timeout_s=30,
                    )

    def test_config_rejects_permission_flags_and_keys(self) -> None:
        import tempfile

        text = (ROOT / "config.example.json").read_text(encoding="utf-8")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for snippet, replacement in (
            ('"--cwd"', '"--always-approve", "--cwd"'),
            ('"can_ask"', '"allow": ["Bash"], "can_ask"'),
            ('"can_ask"', '"auto_approve": true, "can_ask"'),
        ):
            with self.subTest(snippet=replacement[:24]):
                target = Path(tmp.name) / "config.json"
                target.write_text(text.replace(snippet, replacement, 1), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(target)

    def test_environment_is_an_allow_list(self) -> None:
        env = scrub_environment(
            {
                "PATH": "/usr/bin",
                "HOME": "/home/me",
                "LANG": "C.UTF-8",
                "GROK_PERMISSION_MODE": "always-approve",
                "GROK_HOME": "/tmp/injected",
                "XDG_CONFIG_HOME": "/tmp/injected",
                "XDG_DATA_HOME": "/tmp/injected",
                "PYTHONPATH": "/tmp/injected",
                "PYTHONHOME": "/tmp/injected",
                "LD_PRELOAD": "evil.so",
                "TASKBENCH_YOLO": "1",
                "APPDATA": "/tmp/injected",
            }
        )
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["HOME"], "/home/me")
        self.assertEqual(env["LANG"], "C.UTF-8")
        self.assertEqual(env["TASKBENCH_APPROVALS"], "1")
        for leaked in (
            "GROK_PERMISSION_MODE",
            "GROK_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "PYTHONPATH",
            "PYTHONHOME",
            "LD_PRELOAD",
            "TASKBENCH_YOLO",
            "APPDATA",
        ):
            self.assertNotIn(leaked, env)
        adapter_env = GrokBuildAdapter(_runner()).environment(os.environ.copy())
        self.assertEqual(adapter_env["TASKBENCH_APPROVALS"], "1")
        self.assertNotIn("PYTHONPATH", {key.upper() for key in adapter_env})
        self.assertFalse(any(key.upper().startswith("GROK_") for key in adapter_env))


class RunnerReactionTests(TempApp):
    def test_unexpected_permission_mode_kills_the_run(self) -> None:
        _config, _app, watcher = self.make(scenario="bad-permission")
        write_packet(self.store.inbox, base_packet(id="mode-bad", permissions="plan", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "mode-bad") == "failed")
        saved = parse_yaml((self.store.outbox / "mode-bad.json").read_text(encoding="utf-8"))
        self.assertIn("unexpected permission mode", saved["result"]["blocked_reason"])
        self.assertIn("always-approve", saved["result"]["blocked_reason"])

    def test_pinned_mode_report_still_finishes(self) -> None:
        _config, _app, watcher = self.make(scenario="ok-permission")
        write_packet(self.store.inbox, base_packet(id="mode-ok", permissions="plan", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "mode-ok") == "done")

    def test_confinement_failure_refuses_the_task(self) -> None:
        ManagedProcess.force_confinement_failure = True
        self.addCleanup(lambda: setattr(ManagedProcess, "force_confinement_failure", False))
        _config, _app, watcher = self.make(scenario="echo")
        write_packet(self.store.inbox, base_packet(id="confine-1", permissions="plan", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "confine-1") == "failed")
        saved = parse_yaml((self.store.outbox / "confine-1.json").read_text(encoding="utf-8"))
        reason = saved["result"]["blocked_reason"]
        self.assertIn("process confinement", reason)
        self.assertIn("simulated job or process-group assignment failure", reason)
        self.assertIsNone(watcher.proc)


class CompanionConfinementTests(unittest.TestCase):
    def test_sidecar_that_cannot_join_is_not_left_running(self) -> None:
        import tempfile

        if os.name == "nt":
            self.skipTest("POSIX process-group join")

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        marker = Path(tmp.name)
        proc = ManagedProcess(
            [os.sys.executable if False else __import__("sys").executable, "-c", "import time; time.sleep(30)"],
            cwd=marker,
            env=os.environ.copy(),
        )
        self.addCleanup(proc.kill_tree)
        real = os.setpgid

        def fail_join(pid: int, pgid: int) -> None:
            if pgid == proc.group_id:
                raise OSError("cannot join")
            return real(pid, pgid)

        os.setpgid = fail_join  # type: ignore[assignment]
        try:
            with self.assertRaises(ProcessTreeError):
                proc.spawn_companion(
                    [__import__("sys").executable, "-c", "import time; time.sleep(30)"],
                    marker,
                )
        finally:
            os.setpgid = real  # type: ignore[assignment]
        self.assertEqual(proc.companions, [])


class InstallScriptTests(unittest.TestCase):
    def test_whatif_reports_funnel_check_without_leaking_pythonpath(self) -> None:
        script = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("Write-ExpectedFunnelCheck", script)
        self.assertIn("This dry run is not a failure", script)
        self.assertIn('$WhatIfPreference = $false', script)
        self.assertIn('SetEnvironmentVariable("PYTHONPATH", $savedPythonPath, "Process")', script)
        self.assertNotIn("$env:PYTHONPATH = $Root", script)
        self.assertIn("--scope user", script)
        self.assertIn("winget install --id Tailscale.Tailscale -e --accept-package-agreements", script)
        self.assertIn("tailscale funnel --https=443 off", script)
        self.assertNotIn("& tailscale funnel off\n", script)
        self.assertIn("(?!\\d)", script)


if __name__ == "__main__":
    unittest.main()
