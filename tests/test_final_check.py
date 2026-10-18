"""Final-check fixes. These do not launch a real Grok binary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from taskbench.adapters.grok_build import GrokBuildAdapter
from taskbench.config import RunnerConfig
from taskbench.permissions import (
    GROK_SCANNER_DISABLES,
    apply_grok_scanner_locks,
    needs_you_message,
    prepare_grok_config_home,
    scan_project_permission_rules,
    scrub_environment,
    write_grok_deny_rules,
)
from tests.support import ROOT


def _runner(args: list[str]) -> RunnerConfig:
    return RunnerConfig("grok-build", "grok_build", "grok", args, can_ask=False)


class ScannerLockTests(unittest.TestCase):
    def test_grok_env_forces_hook_and_mcp_scanners_off(self) -> None:
        parent = {
            "PATH": "/usr/bin",
            "HOME": "/real/home",
            "GROK_CLAUDE_HOOKS_ENABLED": "1",
            "GROK_CURSOR_HOOKS_ENABLED": "1",
            "GROK_CLAUDE_MCPS_ENABLED": "1",
            "GROK_CURSOR_MCPS_ENABLED": "1",
            "GROK_HOME": "/injected",
            "GROK_PERMISSION_MODE": "always-approve",
        }
        scrubbed = scrub_environment(parent)
        for name in GROK_SCANNER_DISABLES:
            self.assertNotIn(name, scrubbed)
        self.assertNotIn("GROK_HOME", scrubbed)
        locked = apply_grok_scanner_locks(scrubbed)
        for name, value in GROK_SCANNER_DISABLES.items():
            self.assertEqual(locked[name], "0")
            self.assertEqual(value, "0")
        scratch = ROOT / "test-scratch"
        scratch.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(tmp.cleanup)
        home = prepare_grok_config_home(Path(tmp.name) / "data")
        env = GrokBuildAdapter(_runner(["--prompt-file", "{prompt_file}"])).environment(
            parent,
            config_home=home,
            pin_profile_home=False,
        )
        self.assertEqual(env["GROK_HOME"], str(home))
        self.assertEqual(env["HOME"], "/real/home")
        self.assertNotIn("GROK_PERMISSION_MODE", env)
        for name in GROK_SCANNER_DISABLES:
            self.assertEqual(env[name], "0")

    def test_pinned_config_turns_compat_hooks_and_mcp_off(self) -> None:
        scratch = ROOT / "test-scratch"
        scratch.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(tmp.cleanup)
        home = prepare_grok_config_home(Path(tmp.name) / "data")
        write_grok_deny_rules(home, {"HOME": "/real/home", "USERPROFILE": r"C:\Users\Owner"})
        for relative in ("config.toml", Path(".grok") / "config.toml"):
            parsed = tomllib.loads((home / relative).read_text(encoding="utf-8"))
            self.assertIn("permission", parsed)
            self.assertIs(parsed["compat"]["claude"]["hooks"], False)
            self.assertIs(parsed["compat"]["claude"]["mcps"], False)
            self.assertIs(parsed["compat"]["cursor"]["hooks"], False)
            self.assertIs(parsed["compat"]["cursor"]["mcps"], False)
        note = (ROOT / "docs" / "grok-cli-home-and-auth.md").read_text(encoding="utf-8")
        self.assertIn("https://docs.x.ai/build/settings/reference", note)
        self.assertIn("GROK_CLAUDE_HOOKS_ENABLED", note)
        self.assertIn("[compat.claude]", note)


class ExecScanTests(unittest.TestCase):
    def _project(self) -> Path:
        scratch = ROOT / "test-scratch"
        scratch.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(tmp.cleanup)
        project = Path(tmp.name) / "work"
        project.mkdir()
        return project

    def test_mcp_plugins_and_hooks_need_you_and_name_the_file(self) -> None:
        project = self._project()
        grok = project / ".grok"
        grok.mkdir()
        grok.joinpath("config.toml").write_text(
            '[mcp_servers.local]\ncommand = "npx"\nargs = ["-y", "some-mcp"]\n',
            encoding="utf-8",
        )
        message = needs_you_message(scan_project_permission_rules(project))
        self.assertIn(".grok/config.toml", message)
        self.assertIn("mcp_servers", message)
        self.assertNotIn("npx", message)
        self.assertNotIn("some-mcp", message)

        plugins = self._project()
        folder = plugins / ".grok"
        folder.mkdir()
        folder.joinpath("config.toml").write_text(
            '[plugins]\nenabled = ["pwn-plugin"]\n',
            encoding="utf-8",
        )
        plugin_message = needs_you_message(scan_project_permission_rules(plugins))
        self.assertIn("plugins", plugin_message)
        self.assertNotIn("pwn-plugin", plugin_message)

        claude = self._project()
        (claude / ".claude").mkdir()
        (claude / ".claude" / "settings.json").write_text(
            '{"hooks":{"PreToolUse":[{"command":"evil-hook"}]}}\n',
            encoding="utf-8",
        )
        hook_message = needs_you_message(scan_project_permission_rules(claude))
        self.assertIn(".claude/settings.json", hook_message)
        self.assertIn("hooks", hook_message)
        self.assertNotIn("evil-hook", hook_message)

        cursor = self._project()
        (cursor / ".cursor").mkdir()
        (cursor / ".cursor" / "mcp.json").write_text(
            '{"mcpServers":{"box":{"command":"npx","args":["box-mcp"]}}}\n',
            encoding="utf-8",
        )
        cursor_message = needs_you_message(scan_project_permission_rules(cursor))
        self.assertIn(".cursor/mcp.json", cursor_message)
        self.assertIn("mcp_servers", cursor_message)
        self.assertNotIn("box-mcp", cursor_message)

        listed = self._project()
        (listed / ".cursor").mkdir()
        (listed / ".cursor" / "hooks.json").write_text(
            '[{"command":"listed-hook"}]\n',
            encoding="utf-8",
        )
        listed_message = needs_you_message(scan_project_permission_rules(listed))
        self.assertIn(".cursor/hooks.json", listed_message)
        self.assertIn("hooks", listed_message)
        self.assertNotIn("listed-hook", listed_message)

    def test_empty_switches_and_deny_rules_still_run(self) -> None:
        project = self._project()
        (project / ".grok").mkdir()
        (project / ".grok" / "config.toml").write_text(
            "\n".join(
                [
                    "[permission]",
                    'deny = ["Read(secret)"]',
                    "",
                    "[plugins]",
                    "disabled = []",
                    "",
                    "[compat.claude]",
                    "hooks = false",
                    "mcps = false",
                    "",
                    "[compat.cursor]",
                    "hooks = false",
                    "mcps = false",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (project / ".cursor").mkdir()
        (project / ".cursor" / "mcp.json").write_text("{}\n", encoding="utf-8")
        (project / ".cursor" / "hooks.json").write_text('{"hooks":[]}\n', encoding="utf-8")
        self.assertEqual(scan_project_permission_rules(project), [])

    def test_a_parent_directory_is_scanned(self) -> None:
        project = self._project()
        parent = project.parent
        (parent / ".claude").mkdir()
        (parent / ".claude" / "settings.json").write_text(
            '{"hooks":{"Stop":[{"command":"parent-hook"}]}}\n',
            encoding="utf-8",
        )
        message = needs_you_message(scan_project_permission_rules(project))
        self.assertIn(".claude/settings.json", message)
        self.assertIn("hooks", message)
        self.assertNotIn("parent-hook", message)


class PromptFlagTests(unittest.TestCase):
    def test_prompt_file_flag_comes_from_the_runner_config(self) -> None:
        documented = _runner(
            ["--prompt-file", "{prompt_file}", "--cwd", "{project_dir}"]
        )
        argv = GrokBuildAdapter(documented).build_command(
            prompt_file=Path("prompt.md"),
            project_dir=Path("proj"),
            timeout_s=30,
        )
        self.assertIn("--prompt-file", argv)
        self.assertIn("prompt.md", argv)
        self.assertNotIn("--prompt", argv)
        renamed = _runner(["--from-help", "{prompt_file}", "--cwd", "{project_dir}"])
        argv2 = GrokBuildAdapter(renamed).build_command(
            prompt_file=Path("prompt.md"),
            project_dir=Path("proj"),
            timeout_s=30,
        )
        self.assertIn("--from-help", argv2)
        self.assertNotIn("--prompt-file", argv2)
        self.assertNotIn("--prompt", argv2)
        modes = (ROOT / "docs" / "grok-cli-modes.md").read_text(encoding="utf-8")
        self.assertIn("-p, --single", modes)
        self.assertIn("--prompt-file", modes)
        self.assertIn("14-headless-mode.md", modes)
        self.assertIn("no `--prompt` flag", modes)


class LaptopProbeScriptTests(unittest.TestCase):
    def test_script_and_plan_do_not_delete_real_settings(self) -> None:
        script = (ROOT / "scripts" / "laptop-probe.ps1").read_text(encoding="utf-8")
        plan = (ROOT / "docs" / "laptop-test-plan.md").read_text(encoding="utf-8")
        paths_body = script.split("function Get-ProbePaths", 1)[1].split("function ", 1)[0]
        self.assertIn("$env:USERPROFILE", paths_body)
        for name in ("Invoke-ProbeBackup", "Invoke-ProbePlant", "Invoke-ProbeCleanup"):
            body = script.split(f"function {name}", 1)[1].split("function ", 1)[0]
            self.assertIn("$paths = Get-ProbePaths", body)
        self.assertIn("taskbench-backup-", script)
        self.assertIn("yyyyMMdd-HHmmss", script)
        self.assertIn("refusing to back up", script)
        self.assertIn("not overwritten", script)
        self.assertIn("# BEGIN TASKBENCH-PROBE", script)
        self.assertIn("# END TASKBENCH-PROBE", script)
        self.assertIn("UTF8Encoding($false)", script)
        self.assertIn("[System.IO.File]::WriteAllText", script)
        code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("Set-Content", code)
        self.assertNotIn("%USERPROFILE%", script)
        self.assertNotIn("Remove-Item -LiteralPath $grokToml", script)
        self.assertNotIn("Remove-Item -LiteralPath $claudeJson", script)
        self.assertNotIn("Remove-Item -LiteralPath $path", script)
        for line in script.splitlines():
            if "Copy-Item" in line:
                self.assertNotIn("-Force", line)
        self.assertIn("-DryRun", script)
        self.assertIn("already matches", script)
        self.assertIn("no planted lines", script)
        self.assertIn("laptop-probe.ps1", plan)
        self.assertIn("-Backup", plan)
        self.assertIn("-Plant", plan)
        self.assertIn("-Cleanup", plan)
        self.assertIn("-DryRun", plan)
        self.assertIn("ZeroFreeBSTR", plan)
        self.assertIn("$secure = $null", plan)
        self.assertIn("$bstr = [IntPtr]::Zero", plan)
        self.assertIn("grok --help", plan)
        self.assertIn("--prompt-file", plan)
        self.assertIn("--cwd C:\\taskbench-probe", plan)
        self.assertIn("does not list a `--prompt` flag", plan)
        self.assertIsNone(__import__("re").search(r"grok --prompt(?!-file)", plan))
        self.assertNotIn("%USERPROFILE%", plan)
        self.assertIn("--scenario", plan)
        self.assertIn("hang", plan)
        self.assertIn("fake_runner.py", plan)
        self.assertIn("10000", plan)
        self.assertIn("Item 7 is N/A", plan)
        self.assertIn("Get-Command grok | Select Source", plan)
        self.assertNotIn("Remove-Item -LiteralPath $grokToml", plan)
        self.assertNotIn("Remove-Item -LiteralPath $claudeJson", plan)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("--prompt-file", readme)
        self.assertIn("no `--prompt` flag", readme)

    @unittest.skipUnless(os.name == "nt", "the probe script runs under Windows PowerShell")
    def test_cleanup_restores_and_a_second_run_keeps_the_file(self) -> None:
        if Path(r"C:\taskbench-probe").exists():
            self.skipTest("C:\\taskbench-probe already exists")
        powershell = shutil.which("powershell") or shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("powershell is not on PATH")
        scratch = ROOT / "test-scratch"
        scratch.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(tmp.cleanup)
        profile = Path(tmp.name) / "profile"
        (profile / ".grok").mkdir(parents=True)
        live = profile / ".grok" / "config.toml"
        original = 'owner = "keep-me"\n'
        live.write_text(original, encoding="utf-8")
        script = ROOT / "scripts" / "laptop-probe.ps1"

        def run(*args: str) -> subprocess.CompletedProcess[str]:
            env = os.environ.copy()
            env["USERPROFILE"] = str(profile)
            return subprocess.run(
                [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

        dry = run("-Backup", "-DryRun")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(list((profile / ".grok").glob("config.toml.taskbench-backup-*")), [])
        backed = run("-Backup")
        self.assertEqual(backed.returncode, 0, backed.stderr)
        backups = list((profile / ".grok").glob("config.toml.taskbench-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)
        again = run("-Backup")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(len(list((profile / ".grok").glob("config.toml.taskbench-backup-*"))), 1)
        planted = run("-Plant", "-Which", "grok")
        self.assertEqual(planted.returncode, 0, planted.stderr)
        body = live.read_text(encoding="utf-8")
        self.assertIn('owner = "keep-me"', body)
        self.assertIn("# BEGIN TASKBENCH-PROBE", body)
        self.assertIn("# END TASKBENCH-PROBE", body)
        self.assertNotIn("TASKBENCH-PROBE", backups[0].read_text(encoding="utf-8"))
        refused = run("-Backup")
        self.assertEqual(refused.returncode, 0, refused.stderr)
        self.assertEqual(len(list((profile / ".grok").glob("config.toml.taskbench-backup-*"))), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)
        cleaned = run("-Cleanup")
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        self.assertEqual(live.read_text(encoding="utf-8"), original)
        self.assertTrue(backups[0].is_file())
        cleaned_again = run("-Cleanup")
        self.assertEqual(cleaned_again.returncode, 0, cleaned_again.stderr)
        self.assertEqual(live.read_text(encoding="utf-8"), original)
        backups[0].unlink()
        live.write_text(
            "# BEGIN TASKBENCH-PROBE\n[permission]\nallow = [\"Bash(echo TASKBENCH-PROBE*)\"]\n# END TASKBENCH-PROBE\n",
            encoding="utf-8",
            newline="\n",
        )
        stripped = run("-Cleanup")
        self.assertEqual(stripped.returncode, 0, stripped.stderr)
        self.assertTrue(live.is_file())
        self.assertNotIn("TASKBENCH-PROBE", live.read_text(encoding="utf-8"))
        stripped_again = run("-Cleanup")
        self.assertEqual(stripped_again.returncode, 0, stripped_again.stderr)
        self.assertTrue(live.is_file())
        claude = profile / ".claude" / "settings.json"
        (profile / ".claude").mkdir()
        claude.write_text('{"model":"keep"}\n', encoding="utf-8")
        self.assertEqual(run("-Backup").returncode, 0)
        self.assertEqual(run("-Plant", "-Which", "claude").returncode, 0)
        self.assertIn("TASKBENCH-PROBE", claude.read_text(encoding="utf-8"))
        claude_backups = list((profile / ".claude").glob("settings.json.taskbench-backup-*"))
        self.assertEqual(len(claude_backups), 1)
        self.assertNotIn("TASKBENCH-PROBE", claude_backups[0].read_text(encoding="utf-8"))
        self.assertEqual(run("-Cleanup").returncode, 0)
        self.assertIn("keep", claude.read_text(encoding="utf-8"))
        self.assertNotIn("TASKBENCH-PROBE", claude.read_text(encoding="utf-8"))
        self.assertTrue(claude.is_file())
        self.assertEqual(run("-Cleanup").returncode, 0)
        self.assertTrue(claude.is_file())
        self.assertIn("keep", claude.read_text(encoding="utf-8"))
