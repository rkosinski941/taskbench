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
from taskbench.config import ConfigError, RunnerConfig, load_config
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

    def test_claude_enabled_plugins_need_you(self) -> None:
        project = self._project()
        (project / ".claude").mkdir()
        (project / ".claude" / "settings.json").write_text(
            '{"enabledPlugins":{"some-plugin":true}}\n',
            encoding="utf-8",
        )
        message = needs_you_message(scan_project_permission_rules(project))
        self.assertIn(".claude/settings.json", message)
        self.assertIn("enabled_plugins", message)
        self.assertNotIn("some-plugin", message)
        for body in (
            '{"enabledPlugins":{}}\n',
            '{"enabledPlugins":false}\n',
            '{"enabledPlugins":{"some-plugin":false}}\n',
        ):
            empty = self._project()
            (empty / ".claude").mkdir()
            (empty / ".claude" / "settings.json").write_text(body, encoding="utf-8")
            self.assertEqual(scan_project_permission_rules(empty), [])

    def test_claude_extra_known_marketplaces_need_you(self) -> None:
        project = self._project()
        (project / ".claude").mkdir()
        (project / ".claude" / "settings.json").write_text(
            '{"extraKnownMarketplaces":{"my-market":{"source":"github"}}}\n',
            encoding="utf-8",
        )
        message = needs_you_message(scan_project_permission_rules(project))
        self.assertIn(".claude/settings.json", message)
        self.assertIn("extra_known_marketplaces", message)
        self.assertIn("extra known marketplaces", message)
        self.assertNotIn("my-market", message)
        self.assertNotIn("github", message)
        for body in (
            '{"extraKnownMarketplaces":{}}\n',
            '{"extraKnownMarketplaces":false}\n',
            '{"extraKnownMarketplaces":""}\n',
        ):
            empty = self._project()
            (empty / ".claude").mkdir()
            (empty / ".claude" / "settings.json").write_text(body, encoding="utf-8")
            self.assertEqual(scan_project_permission_rules(empty), [])


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

    def test_args_template_rejects_cwd_and_config_flags(self) -> None:
        text = (ROOT / "config.example.json").read_text(encoding="utf-8")
        self.assertNotIn('"--cwd"', text)
        loaded = load_config(ROOT / "config.example.json")
        self.assertNotIn("--cwd", loaded.runners["grok-build"].args_template)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for flag in ("--cwd", "-c", "--config", "--CWD", "--config=elsewhere"):
            target = Path(tmp.name) / "config.json"
            target.write_text(
                text.replace('"--prompt-file"', f'"{flag}", "--prompt-file"', 1),
                encoding="utf-8",
            )
            with self.subTest(flag=flag):
                with self.assertRaises(ConfigError) as caught:
                    load_config(target)
                self.assertIn(flag.split("=", 1)[0].lower(), str(caught.exception))
                self.assertIn("working directory", str(caught.exception))

    def test_args_template_rejects_agent_flag(self) -> None:
        text = (ROOT / "config.example.json").read_text(encoding="utf-8")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for flag in ("--agent", "--Agent", "--agent=name"):
            target = Path(tmp.name) / "config.json"
            target.write_text(
                text.replace('"--prompt-file"', f'"{flag}", "--prompt-file"', 1),
                encoding="utf-8",
            )
            with self.subTest(flag=flag):
                with self.assertRaises(ConfigError) as caught:
                    load_config(target)
                self.assertIn("--agent", str(caught.exception))
                self.assertIn("does not pass --agent", str(caught.exception))


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
        self.assertNotIn("ConvertFrom-Json", script)
        self.assertNotIn("ConvertTo-Json", script)
        self.assertIn("System.Text.Json", script)
        self.assertIn("JsonDocument", script)
        self.assertIn("PowerShell 7", script)
        self.assertIn("recommended", script)
        self.assertIn("other content", script)
        self.assertIn("Bash(echo TASKBENCH-PROBE*)", script)
        self.assertIn("taskbench-created", script)
        self.assertIn(".used", script)
        self.assertIn("no probe marker", script)
        self.assertIn("tomllib", script)
        code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("Set-Content", code)
        self.assertNotIn("\\s+", code)
        self.assertNotIn("%USERPROFILE%", script)
        self.assertNotIn("Remove-Item -LiteralPath $grokToml", script)
        self.assertNotIn("Remove-Item -LiteralPath $claudeJson", script)
        self.assertNotIn("Remove-Item -LiteralPath $path", script)
        for line in script.splitlines():
            if "Copy-Item" in line:
                self.assertNotIn("-Force", line)
        self.assertIn("-DryRun", script)
        self.assertNotIn("ConvertFrom-Json", plan)
        self.assertNotIn("5.1", plan)
        self.assertIn("PowerShell 7 is supported and recommended", plan)
        self.assertIn("System.Text.Json", plan)
        self.assertIn("other content", plan)
        self.assertIn("taskbench-created", plan)
        self.assertIn(".used", plan)
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

    def _probe_profile(self) -> tuple[str, Path]:
        if os.name != "nt":
            self.skipTest("the probe script runs under Windows PowerShell")
        if Path(r"C:\taskbench-probe").exists():
            self.skipTest("C:\\taskbench-probe already exists")
        requested = os.environ.get("TASKBENCH_PROBE_POWERSHELL", "").strip()
        if requested:
            powershell = shutil.which(requested)
            if powershell is None:
                self.fail(f"{requested} is not on PATH")
        else:
            powershell = shutil.which("powershell") or shutil.which("powershell.exe")
            if powershell is None:
                self.skipTest("powershell is not on PATH")
        scratch = ROOT / "test-scratch"
        scratch.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(tmp.cleanup)
        profile = Path(tmp.name) / "profile"
        profile.mkdir()
        return powershell, profile

    def _probe(self, powershell: str, profile: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["USERPROFILE"] = str(profile)
        return subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "laptop-probe.ps1"),
                *args,
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    @staticmethod
    def _backups(directory: Path, prefix: str) -> tuple[list[Path], list[Path]]:
        found = list(directory.glob(prefix + "*"))
        live = [path for path in found if not path.name.endswith(".used")]
        used = [path for path in found if path.name.endswith(".used")]
        return live, used

    @unittest.skipUnless(os.name == "nt", "the probe script runs under Windows PowerShell")
    def test_a_claude_created_file_is_removed_on_cleanup(self) -> None:
        powershell, profile = self._probe_profile()
        planted = self._probe(powershell, profile, "-Plant", "-Which", "claude")
        self.assertEqual(planted.returncode, 0, planted.stderr + planted.stdout)
        claude = profile / ".claude" / "settings.json"
        sidecar = Path(str(claude) + ".taskbench-created")
        self.assertTrue(claude.is_file())
        self.assertTrue(sidecar.is_file())
        self.assertIn("Bash(echo TASKBENCH-PROBE*)", claude.read_text(encoding="utf-8"))
        cleaned = self._probe(powershell, profile, "-Cleanup")
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr + cleaned.stdout)
        self.assertFalse(claude.exists())
        self.assertFalse(sidecar.exists())
        self.assertNotIn("no planted lines", cleaned.stdout)
        again = self._probe(powershell, profile, "-Cleanup")
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        self.assertFalse(claude.exists())

    @unittest.skipUnless(os.name == "nt", "the probe script runs under Windows PowerShell")
    def test_b_existing_claude_without_backup_keeps_other_keys(self) -> None:
        powershell, profile = self._probe_profile()
        claude = profile / ".claude" / "settings.json"
        claude.parent.mkdir()
        original = '{"model":"keep","theme":"dark"}\n'
        claude.write_text(original, encoding="utf-8", newline="\n")
        refused = self._probe(powershell, profile, "-Plant", "-Which", "claude")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("backup", (refused.stderr + refused.stdout).lower())
        self.assertEqual(claude.read_bytes(), original.encode("utf-8"))
        claude.write_text(
            '{"model":"keep","permissions":{"deny":["Read(C:\\\\secret)"],"allow":["Bash(git status)","Bash(echo TASKBENCH-PROBE*)"]},"theme":"dark"}\n',
            encoding="utf-8",
            newline="\n",
        )
        cleaned = self._probe(powershell, profile, "-Cleanup")
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr + cleaned.stdout)
        data = json.loads(claude.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "keep")
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["permissions"]["deny"], ["Read(C:\\secret)"])
        self.assertEqual(data["permissions"]["allow"], ["Bash(git status)"])
        self.assertNotIn("TASKBENCH-PROBE", claude.read_text(encoding="utf-8"))
        self.assertFalse(Path(str(claude) + ".taskbench-created").exists())
        self.assertEqual(self._backups(claude.parent, "settings.json.taskbench-backup-"), ([], []))

    @unittest.skipUnless(os.name == "nt", "the probe script runs under Windows PowerShell")
    def test_c_backup_restores_only_the_planted_file(self) -> None:
        powershell, profile = self._probe_profile()
        live = profile / ".grok" / "config.toml"
        live.parent.mkdir()
        original = 'owner = "keep-me"\n'
        live.write_text(original, encoding="utf-8", newline="\n")
        dry = self._probe(powershell, profile, "-Backup", "-DryRun")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(self._backups(live.parent, "config.toml.taskbench-backup-"), ([], []))
        backed = self._probe(powershell, profile, "-Backup")
        self.assertEqual(backed.returncode, 0, backed.stderr)
        live_backups, used = self._backups(live.parent, "config.toml.taskbench-backup-")
        self.assertEqual(len(live_backups), 1)
        self.assertEqual(used, [])
        self.assertEqual(live_backups[0].read_bytes(), original.encode("utf-8"))
        planted = self._probe(powershell, profile, "-Plant", "-Which", "grok")
        self.assertEqual(planted.returncode, 0, planted.stderr + planted.stdout)
        body = live.read_text(encoding="utf-8")
        self.assertIn('owner = "keep-me"', body)
        self.assertEqual(body.count("[permission]"), 1)
        self.assertIn("# BEGIN TASKBENCH-PROBE", body)
        self.assertIn("# END TASKBENCH-PROBE", body)
        tomllib.loads(body)
        self.assertNotIn("TASKBENCH-PROBE", live_backups[0].read_text(encoding="utf-8"))
        cleaned = self._probe(powershell, profile, "-Cleanup")
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr + cleaned.stdout)
        self.assertEqual(live.read_bytes(), original.encode("utf-8"))
        claude = profile / ".claude" / "settings.json"
        claude.parent.mkdir()
        claude.write_text(
            '{"model":"keep","permissions":{"deny":["Read(C:\\\\secret)"]}}\n',
            encoding="utf-8",
            newline="\n",
        )
        self.assertEqual(self._probe(powershell, profile, "-Backup").returncode, 0)
        planted_json = self._probe(powershell, profile, "-Plant", "-Which", "claude")
        self.assertEqual(planted_json.returncode, 0, planted_json.stderr + planted_json.stdout)
        planted_data = json.loads(claude.read_text(encoding="utf-8"))
        self.assertEqual(planted_data["model"], "keep")
        self.assertEqual(planted_data["permissions"]["deny"], ["Read(C:\\secret)"])
        self.assertEqual(planted_data["permissions"]["allow"], ["Bash(echo TASKBENCH-PROBE*)"])
        restored = self._probe(powershell, profile, "-Cleanup")
        self.assertEqual(restored.returncode, 0, restored.stderr + restored.stdout)
        self.assertEqual(
            json.loads(claude.read_text(encoding="utf-8")),
            {"model": "keep", "permissions": {"deny": ["Read(C:\\secret)"]}},
        )

    @unittest.skipUnless(os.name == "nt", "the probe script runs under Windows PowerShell")
    def test_d_second_cleanup_is_a_noop(self) -> None:
        powershell, profile = self._probe_profile()
        live = profile / ".grok" / "config.toml"
        live.parent.mkdir()
        original = 'owner = "keep-me"\n'
        live.write_text(original, encoding="utf-8", newline="\n")
        self.assertEqual(self._probe(powershell, profile, "-Backup").returncode, 0)
        self.assertEqual(self._probe(powershell, profile, "-Plant", "-Which", "grok").returncode, 0)
        self.assertEqual(self._probe(powershell, profile, "-Cleanup").returncode, 0)
        first = live.read_bytes()
        again = self._probe(powershell, profile, "-Cleanup")
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        self.assertEqual(live.read_bytes(), first)
        self.assertIn("no probe marker", again.stdout)
        live_backups, used = self._backups(live.parent, "config.toml.taskbench-backup-")
        self.assertEqual(live_backups, [])
        self.assertEqual(len(used), 1)

    @unittest.skipUnless(os.name == "nt", "the probe script runs under Windows PowerShell")
    def test_e_cleanup_without_a_probe_keeps_newer_edits(self) -> None:
        powershell, profile = self._probe_profile()
        live = profile / ".grok" / "config.toml"
        live.parent.mkdir()
        live.write_text('owner = "original"\n', encoding="utf-8", newline="\n")
        self.assertEqual(self._probe(powershell, profile, "-Backup").returncode, 0)
        newer = 'owner = "newer-edit"\n'
        live.write_text(newer, encoding="utf-8", newline="\n")
        cleaned = self._probe(powershell, profile, "-Cleanup")
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr + cleaned.stdout)
        self.assertEqual(live.read_bytes(), newer.encode("utf-8"))
        self.assertIn("no probe marker", cleaned.stdout)
        self.assertIn("unused", cleaned.stdout)
        live_backups, used = self._backups(live.parent, "config.toml.taskbench-backup-")
        self.assertEqual(len(live_backups), 1)
        self.assertEqual(used, [])
        self.assertEqual(live_backups[0].read_text(encoding="utf-8"), 'owner = "original"\n')

    @unittest.skipUnless(os.name == "nt", "the probe script runs under Windows PowerShell")
    def test_f_restored_backup_is_renamed_and_not_reused(self) -> None:
        powershell, profile = self._probe_profile()
        live = profile / ".grok" / "config.toml"
        live.parent.mkdir()
        original = 'owner = "original"\n'
        live.write_text(original, encoding="utf-8", newline="\n")
        self.assertEqual(self._probe(powershell, profile, "-Backup").returncode, 0)
        self.assertEqual(self._probe(powershell, profile, "-Plant", "-Which", "grok").returncode, 0)
        cleaned = self._probe(powershell, profile, "-Cleanup")
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr + cleaned.stdout)
        live_backups, used = self._backups(live.parent, "config.toml.taskbench-backup-")
        self.assertEqual(live_backups, [])
        self.assertEqual(len(used), 1)
        self.assertEqual(used[0].read_bytes(), original.encode("utf-8"))
        self.assertIn(".used", cleaned.stdout)
        newer = 'owner = "after-restore"\n'
        live.write_text(newer, encoding="utf-8", newline="\n")
        again = self._probe(powershell, profile, "-Cleanup")
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        self.assertEqual(live.read_bytes(), newer.encode("utf-8"))
        self.assertEqual(self._backups(live.parent, "config.toml.taskbench-backup-")[1], used)

    @unittest.skipUnless(os.name == "nt", "the probe script runs under Windows PowerShell")
    def test_g_existing_permission_table_merges_and_stays_valid(self) -> None:
        powershell, profile = self._probe_profile()
        live = profile / ".grok" / "config.toml"
        live.parent.mkdir()
        original = 'owner = "keep"\n\n[permission]\ndeny = ["Read(C:\\\\secret)"]\n'
        live.write_text(original, encoding="utf-8", newline="\n")
        self.assertEqual(self._probe(powershell, profile, "-Backup").returncode, 0)
        planted = self._probe(powershell, profile, "-Plant", "-Which", "grok")
        self.assertEqual(planted.returncode, 0, planted.stderr + planted.stdout)
        body = live.read_text(encoding="utf-8")
        self.assertEqual(body.count("[permission]"), 1)
        self.assertIn("# BEGIN TASKBENCH-PROBE", body)
        self.assertIn("# END TASKBENCH-PROBE", body)
        parsed = tomllib.loads(body)
        self.assertEqual(parsed["permission"]["allow"], ["Bash(echo TASKBENCH-PROBE*)"])
        self.assertEqual(parsed["permission"]["deny"], ["Read(C:\\secret)"])
        self.assertEqual(parsed["owner"], "keep")
        planted_bytes = live.read_bytes()
        cleaned = self._probe(powershell, profile, "-Cleanup")
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr + cleaned.stdout)
        self.assertEqual(live.read_bytes(), original.encode("utf-8"))
        tomllib.loads(live.read_text(encoding="utf-8"))
        live.write_bytes(planted_bytes)
        stripped = self._probe(powershell, profile, "-Cleanup")
        self.assertEqual(stripped.returncode, 0, stripped.stderr + stripped.stdout)
        after = live.read_text(encoding="utf-8")
        self.assertNotIn("TASKBENCH-PROBE", after)
        self.assertEqual(after.count("[permission]"), 1)
        restored = tomllib.loads(after)
        self.assertNotIn("allow", restored["permission"])
        self.assertEqual(restored["permission"]["deny"], ["Read(C:\\secret)"])
        self.assertEqual(restored["owner"], "keep")
        refused_body = '[permission]\nallow = ["Bash(git *)"]\n'
        live.write_text(refused_body, encoding="utf-8", newline="\n")
        self.assertEqual(self._probe(powershell, profile, "-Backup").returncode, 0)
        refused = self._probe(powershell, profile, "-Plant", "-Which", "grok")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("allow", (refused.stderr + refused.stdout).lower())
        self.assertEqual(live.read_text(encoding="utf-8"), refused_body)
        tomllib.loads(live.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "the probe script runs under Windows PowerShell")
    def test_h_crlf_bytes_round_trip(self) -> None:
        powershell, profile = self._probe_profile()
        live = profile / ".grok" / "config.toml"
        live.parent.mkdir()
        original = 'owner = "crlf"\r\nkeep = true\r\n'.encode("utf-8")
        live.write_bytes(original)
        untouched = self._probe(powershell, profile, "-Cleanup")
        self.assertEqual(untouched.returncode, 0, untouched.stderr + untouched.stdout)
        self.assertEqual(live.read_bytes(), original)
        self.assertEqual(self._probe(powershell, profile, "-Backup").returncode, 0)
        planted = self._probe(powershell, profile, "-Plant", "-Which", "grok")
        self.assertEqual(planted.returncode, 0, planted.stderr + planted.stdout)
        self.assertIn(b"TASKBENCH-PROBE", live.read_bytes())
        self.assertIn(b"\r\n", live.read_bytes())
        tomllib.loads(live.read_text(encoding="utf-8"))
        cleaned = self._probe(powershell, profile, "-Cleanup")
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr + cleaned.stdout)
        self.assertEqual(live.read_bytes(), original)
        prefix = 'owner = "crlf"\r\n'
        block = (
            "# BEGIN TASKBENCH-PROBE\r\n"
            "[permission]\r\n"
            'allow = ["Bash(echo TASKBENCH-PROBE*)"]\r\n'
            "# END TASKBENCH-PROBE\r\n"
        )
        live.write_bytes((prefix + block).encode("utf-8"))
        for path in live.parent.glob("config.toml.taskbench-backup-*"):
            path.unlink()
        stripped = self._probe(powershell, profile, "-Cleanup")
        self.assertEqual(stripped.returncode, 0, stripped.stderr + stripped.stdout)
        self.assertEqual(live.read_bytes(), prefix.encode("utf-8"))
        claude = profile / ".claude" / "settings.json"
        claude.parent.mkdir()
        raw = b'{\r\n  "model": "keep"\r\n}\r\n'
        claude.write_bytes(raw)
        self.assertEqual(self._probe(powershell, profile, "-Cleanup").returncode, 0)
        self.assertEqual(claude.read_bytes(), raw)

    @unittest.skipUnless(os.name == "nt", "the probe script runs under Windows PowerShell")
    def test_i_dates_numbers_and_nesting_survive_cleanup(self) -> None:
        powershell, profile = self._probe_profile()
        claude = profile / ".claude" / "settings.json"
        claude.parent.mkdir()
        original = (
            '{"created":"2024-01-02T03:04:05.1234567Z","when":"2024-01-02T03:04:05Z",'
            '"count":1.0,"big":9007199254740993,"signed":-2.50,'
            '"level":{"items":[1,{"when":"2020-12-31T00:00:00.000Z","inner":{"n":1.0}}]},'
            '"permissions":{"allow":["Bash(git status)","Bash(echo TASKBENCH-PROBE*)","Bash(git diff)"]},'
            '"note":"keep"}\n'
        )
        claude.write_text(original, encoding="utf-8", newline="\n")
        cleaned = self._probe(powershell, profile, "-Cleanup")
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr + cleaned.stdout)
        expected = (
            '{"created":"2024-01-02T03:04:05.1234567Z","when":"2024-01-02T03:04:05Z",'
            '"count":1.0,"big":9007199254740993,"signed":-2.50,'
            '"level":{"items":[1,{"when":"2020-12-31T00:00:00.000Z","inner":{"n":1.0}}]},'
            '"permissions":{"allow":["Bash(git status)","Bash(git diff)"]},'
            '"note":"keep"}\n'
        )
        self.assertEqual(claude.read_bytes(), expected.encode("utf-8"))
        crlf = (
            "{\r\n"
            '  "created": "2024-01-02T03:04:05Z",\r\n'
            '  "count": 1.0,\r\n'
            "  \"permissions\": {\r\n"
            '    "allow": ["Bash(git status)", "Bash(echo TASKBENCH-PROBE*)", "Bash(git diff)"]\r\n'
            "  }\r\n"
            "}\r\n"
        ).encode("utf-8")
        claude.write_bytes(crlf)
        cleaned_crlf = self._probe(powershell, profile, "-Cleanup")
        self.assertEqual(cleaned_crlf.returncode, 0, cleaned_crlf.stderr + cleaned_crlf.stdout)
        expected_crlf = (
            "{\r\n"
            '  "created": "2024-01-02T03:04:05Z",\r\n'
            '  "count": 1.0,\r\n'
            "  \"permissions\": {\r\n"
            '    "allow": ["Bash(git status)", "Bash(git diff)"]\r\n'
            "  }\r\n"
            "}\r\n"
        ).encode("utf-8")
        self.assertEqual(claude.read_bytes(), expected_crlf)
        planted_source = '{"created":"2024-01-02T03:04:05Z","count":1.0,"nested":{"keep":true}}\n'
        claude.write_text(planted_source, encoding="utf-8", newline="\n")
        backed = self._probe(powershell, profile, "-Backup")
        self.assertEqual(backed.returncode, 0, backed.stderr + backed.stdout)
        planted = self._probe(powershell, profile, "-Plant", "-Which", "claude")
        self.assertEqual(planted.returncode, 0, planted.stderr + planted.stdout)
        self.assertEqual(
            claude.read_bytes(),
            (
                '{"created":"2024-01-02T03:04:05Z","count":1.0,"nested":{"keep":true},'
                '"permissions":{"allow":["Bash(echo TASKBENCH-PROBE*)"]}}\n'
            ).encode("utf-8"),
        )

    @unittest.skipUnless(os.name == "nt", "the probe script runs under Windows PowerShell")
    def test_j_leftover_created_marker_keeps_real_content(self) -> None:
        powershell, profile = self._probe_profile()
        planted = self._probe(powershell, profile, "-Plant", "-Which", "claude")
        self.assertEqual(planted.returncode, 0, planted.stderr + planted.stdout)
        claude = profile / ".claude" / "settings.json"
        sidecar = Path(str(claude) + ".taskbench-created")
        self.assertTrue(claude.is_file())
        self.assertTrue(sidecar.is_file())
        edited = (
            '{"model":"owner-edit","created":"2024-01-02T03:04:05Z",'
            '"permissions":{"allow":["Bash(echo TASKBENCH-PROBE*)","Bash(git status)"]}}\n'
        )
        claude.write_text(edited, encoding="utf-8", newline="\n")
        cleaned = self._probe(powershell, profile, "-Cleanup")
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr + cleaned.stdout)
        self.assertTrue(claude.is_file())
        self.assertFalse(sidecar.exists())
        self.assertIn("created-file marker", cleaned.stdout)
        self.assertIn("other content", cleaned.stdout)
        self.assertEqual(
            claude.read_bytes(),
            (
                '{"model":"owner-edit","created":"2024-01-02T03:04:05Z",'
                '"permissions":{"allow":["Bash(git status)"]}}\n'
            ).encode("utf-8"),
        )

