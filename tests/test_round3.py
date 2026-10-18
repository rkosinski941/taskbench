"""Round-3 review fixes. These do not launch a real Grok binary."""

from __future__ import annotations

import http.client
import json
import os
import re
import stat
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

from taskbench.cli import require_python
from taskbench.permissions import (
    needs_you_message,
    prepare_grok_config_home,
    scan_project_permission_rules,
    scrub_environment,
    seed_grok_credentials,
)
from taskbench.task_format import parse_yaml
from taskbench.workdir import (
    WorkdirError,
    _strip_extended_prefix,
    is_registered,
    register_workdir,
    rejection_reason,
    validate_workdir,
    windows_canonical_path,
)
from tests.support import ROOT, TempApp, base_packet, packet_status, run_until, write_packet


class ProvenanceTests(TempApp):
    def _serve(self):
        from taskbench.ui.server import TaskbenchServer

        self.server = TaskbenchServer(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return int(self.server.server_address[1])

    def _page(self, port: int, path: str = "/") -> tuple[str, str]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        cookie = response.getheader("Set-Cookie") or ""
        conn.close()
        return body, cookie.split(";", 1)[0]

    def test_packet_and_handoff_never_register(self) -> None:
        _config, _app, watcher = self.make(scenario="echo", can_ask=False)
        folder = self.root / "from-packet"
        folder.mkdir()
        (folder / "INSTRUCTIONS.md").write_text("token\n", encoding="utf-8")
        for sender, packet_id in (("web-ui", "prov-ui"), ("Joy Feeder", "prov-bot")):
            packet = base_packet(id=packet_id, permissions="plan", timeout_s=30, **{"from": sender})
            packet["workdir"] = str(folder)
            write_packet(self.store.inbox, packet)
            run_until(watcher, lambda packet_id=packet_id: packet_status(self.store, packet_id) == "needs-approval")
            saved = parse_yaml((self.store.needs_approval / f"{packet_id}.yaml").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "needs-approval")
            self.assertEqual(saved["workdir"], str(folder.resolve()))
            self.assertFalse(is_registered(self.root / "var", folder.resolve()))
            self.assertFalse((folder / ".taskbench" / "LAST_AGENT.md").exists())
            if packet_id == "prov-ui":
                port = self._serve()
                body, _cookie = self._page(port, "/task/prov-ui")
                self.assertIn("Needs you", body)
                self.assertIn(str(folder.resolve()), body)
                self.assertIn("data-status='needs-approval'", body)
            watcher.request_decision(packet_id, "denied")
            run_until(watcher, lambda packet_id=packet_id: packet_status(self.store, packet_id) == "blocked")
            self.assertFalse(is_registered(self.root / "var", folder.resolve()))

    def test_approve_does_not_register_and_the_endpoint_does(self) -> None:
        _config, app, watcher = self.make(scenario="echo", can_ask=False)
        folder = self.root / "endpoint-folder"
        folder.mkdir()
        (folder / "INSTRUCTIONS.md").write_text("ENDPOINT-TOKEN\n", encoding="utf-8")
        packet = base_packet(id="prov-wait", permissions="plan", timeout_s=30, **{"from": "web-ui"})
        packet["workdir"] = str(folder)
        write_packet(self.store.inbox, packet)
        run_until(watcher, lambda: packet_status(self.store, "prov-wait") == "needs-approval")
        watcher.request_decision("prov-wait", "approved")
        watcher.tick()
        self.assertEqual(packet_status(self.store, "prov-wait"), "needs-approval")
        self.assertFalse(is_registered(self.root / "var", folder.resolve()))
        port = self._serve()
        body, cookie = self._page(port, "/task/prov-wait")
        csrf = re.search(r"name='csrf' value='([^']+)'", body).group(1)
        launch = re.search(r"name='launch' value='([^']+)'", body).group(1)
        self.assertNotIn(launch, (self.root / "var" / "workdir-registry.json").read_text(encoding="utf-8") if (self.root / "var" / "workdir-registry.json").is_file() else "")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/register-workdir",
            body=f"csrf={csrf}&launch={launch}&path={quote(str(folder.resolve()))}",
            headers={
                "Cookie": cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": f"http://127.0.0.1:{port}",
                "Host": f"127.0.0.1:{port}",
            },
        )
        registered = conn.getresponse()
        registered.read()
        conn.close()
        self.assertEqual(registered.status, 303)
        self.assertTrue(is_registered(self.root / "var", folder.resolve()))
        run_until(watcher, lambda: packet_status(self.store, "prov-wait") == "done")

    def test_launch_token_and_origin_are_required(self) -> None:
        self.make()
        port = self._serve()
        body, cookie = self._page(port)
        csrf = re.search(r"name='csrf' value='([^']+)'", body).group(1)
        launch = re.search(r"name='launch' value='([^']+)'", body).group(1)
        cases = (
            (f"csrf={csrf}", {"Origin": f"http://127.0.0.1:{port}"}, 403, b"launch token"),
            (f"csrf={csrf}&launch={launch}", {}, 403, b"origin"),
            (
                f"csrf={csrf}&launch={launch}",
                {"Origin": "http://evil.example"},
                403,
                b"origin",
            ),
            (
                f"csrf={csrf}&launch=short",
                {"Origin": f"http://127.0.0.1:{port}"},
                403,
                b"launch token",
            ),
        )
        for form, extra, status, needle in cases:
            headers = {
                "Cookie": cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Host": f"127.0.0.1:{port}",
            }
            headers.update(extra)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/register-workdir", body=form + "&path=/tmp", headers=headers)
            response = conn.getresponse()
            payload = response.read()
            conn.close()
            self.assertEqual(response.status, status)
            self.assertIn(needle, payload)


class WorkdirDenialTests(unittest.TestCase):
    def test_personal_appdata_dot_and_ancestors(self) -> None:
        profile = r"C:\Users\Rich"
        common = {"platform": "nt", "profile_roots": [profile], "system_roots": []}
        self.assertIn("personal folder", rejection_reason(profile + r"\Documents", **common))
        self.assertIn("personal folder", rejection_reason(profile + r"\Desktop", **common))
        self.assertIn("personal folder", rejection_reason(profile + r"\Downloads", **common))
        self.assertIn("personal folder", rejection_reason(profile + r"\OneDrive", **common))
        self.assertIn("personal folder", rejection_reason(profile + r"\OneDrive - Contoso", **common))
        self.assertIsNone(rejection_reason(profile + r"\Documents\proj", **common))
        self.assertIsNone(rejection_reason(profile + r"\OneDrive - Contoso\docs", **common))
        self.assertIn("AppData", rejection_reason(profile + r"\AppData\Local\Temp", **common))
        self.assertIn("dot-folder", rejection_reason(profile + r"\.ssh", **common))
        self.assertIn("dot-folder", rejection_reason(profile + r"\.ssh\keys", **common))
        self.assertIn("dot-folder", rejection_reason(profile + r"\.grok", **common))
        self.assertIn("contains a denied path", rejection_reason(r"C:\Users", **common))
        self.assertIn(
            "taskbench folder",
            rejection_reason(
                r"C:\tb\state\runs",
                platform="nt",
                profile_roots=[],
                system_roots=[],
                protected_roots=[r"C:\tb\state"],
            ),
        )
        self.assertIn(
            "contains a denied path",
            rejection_reason(
                r"C:\tb",
                platform="nt",
                profile_roots=[],
                system_roots=[],
                protected_roots=[r"C:\tb\state"],
            ),
        )
        self.assertIn(
            "personal folder",
            rejection_reason(r"c:\users\rich\documents", platform="nt", profile_roots=[profile], system_roots=[]),
        )
        self.assertIn(
            "personal folder",
            rejection_reason("/Users/Rich/Desktop", platform="darwin", profile_roots=["/users/rich"], system_roots=[]),
        )
        self.assertIsNone(
            rejection_reason(
                "/Users/Rich/Desktop/app",
                platform="darwin",
                profile_roots=["/users/rich"],
                system_roots=[],
            )
        )

    def test_extended_prefix_and_short_names(self) -> None:
        self.assertEqual(_strip_extended_prefix(r"\\?\C:\Users\Rich"), r"C:\Users\Rich")
        self.assertEqual(_strip_extended_prefix(r"\\?\UNC\server\share\dir"), r"\\server\share\dir")
        mapped = windows_canonical_path(
            r"C:\Users\RICH~1\proj",
            short_names={r"C:\Users\RICH~1\proj": r"C:\Users\Rich\proj"},
        )
        self.assertEqual(mapped, r"C:\Users\Rich\proj")

    def test_real_directory_rules(self) -> None:
        scratch = ROOT / "test-scratch"
        scratch.mkdir(exist_ok=True)
        import tempfile

        tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        secret = root / "has-ssh"
        secret.mkdir()
        (secret / ".ssh").mkdir()
        with self.assertRaises(WorkdirError) as caught:
            validate_workdir(str(secret))
        self.assertIn("contains a denied path", str(caught.exception))
        settings = root / "has-grok"
        settings.mkdir()
        (settings / ".grok").mkdir()
        self.assertTrue(validate_workdir(str(settings)).is_dir())
        git = root / "has-git"
        git.mkdir()
        (git / ".git").mkdir()
        self.assertTrue(validate_workdir(str(git)).is_dir())
        parent = root / "ancestor"
        state = parent / "state"
        state.mkdir(parents=True)
        with self.assertRaises(WorkdirError) as ancestor:
            validate_workdir(str(parent), state_dir=state)
        self.assertIn("contains a denied path", str(ancestor.exception))


class ToctouTests(TempApp):
    def test_canonical_path_change_blocks_before_launch(self) -> None:
        import taskbench.watcher as watcher_mod

        folder = self.root / "stable"
        folder.mkdir()
        (folder / "INSTRUCTIONS.md").write_text("stay\n", encoding="utf-8")
        other = self.root / "swapped"
        other.mkdir()
        register_workdir(self.root / "var", folder.resolve())
        original = watcher_mod.validate_workdir
        calls = {"n": 0}

        def wrapped(raw: str, **kwargs):
            calls["n"] += 1
            resolved = original(raw, **kwargs)
            if calls["n"] >= 2:
                return other.resolve()
            return resolved

        watcher_mod.validate_workdir = wrapped
        self.addCleanup(lambda: setattr(watcher_mod, "validate_workdir", original))
        _config, _app, watcher = self.make(scenario="echo", can_ask=False)
        packet = base_packet(id="toctou-1", permissions="plan", timeout_s=30)
        packet["workdir"] = str(folder)
        write_packet(self.store.inbox, packet)
        run_until(watcher, lambda: packet_status(self.store, "toctou-1") == "blocked")
        saved = parse_yaml((self.store.outbox / "toctou-1.yaml").read_text(encoding="utf-8"))
        self.assertIn("workdir changed before launch", saved["result"]["blocked_reason"])
        self.assertIsNone(watcher.proc)


class AllowScanTests(unittest.TestCase):
    def test_case_symlink_parents_and_unreadable(self) -> None:
        import tempfile

        scratch = ROOT / "test-scratch"
        scratch.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        profile = root / "profile"
        mid = profile / "mid"
        project = mid / "work"
        project.mkdir(parents=True)
        above = root / "above-marker"
        # The parent of the profile is ``root``. A rule there must not be scanned.
        (root / ".grok").mkdir()
        (root / ".grok" / "config.toml").write_text('allow = ["ABOVE_SECRET"]\n', encoding="utf-8")
        (profile / ".grok").mkdir()
        (profile / ".grok" / "config.toml").write_text(
            'Allow = ["PROFILE_SECRET"]\npermission_mode = "DontAsk"\n',
            encoding="utf-8",
        )
        (mid / ".claude").mkdir()
        (mid / ".claude" / "settings.json").write_text(
            '{"DefaultMode": "bypassPermissions", "permissions": {"allow": ["MID_SECRET"]}}\n',
            encoding="utf-8",
        )
        target = root / "linked-target"
        target.mkdir()
        (target / "config.toml").write_text(
            '[permission]\nallow = [\n  "LINK_SECRET",\n]\n',
            encoding="utf-8",
        )
        try:
            (project / ".grok").symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks are not permitted on this machine")
        import taskbench.workdir as workdir_mod

        original = workdir_mod._profile_roots
        workdir_mod._profile_roots = lambda: [str(profile.resolve())]
        self.addCleanup(lambda: setattr(workdir_mod, "_profile_roots", original))
        findings = scan_project_permission_rules(project)
        message = needs_you_message(findings)
        self.assertIn("allow", message)
        self.assertIn("defaultMode", message)
        self.assertNotIn("PROFILE_SECRET", message)
        self.assertNotIn("MID_SECRET", message)
        self.assertNotIn("LINK_SECRET", message)
        self.assertNotIn("ABOVE_SECRET", message)
        self.assertNotIn("bypassPermissions", message)
        self.assertIn(".grok/config.toml", message)

        broken = root / "broken-project"
        broken.mkdir()
        grok = broken / ".grok"
        grok.mkdir()
        bad = grok / "config.toml"
        bad.write_text("allow = [\n", encoding="utf-8")
        bad_findings = scan_project_permission_rules(broken)
        self.assertIn("unparsed", needs_you_message(bad_findings))
        hidden = root / "hidden-project"
        hidden.mkdir()
        (hidden / ".claude").mkdir()
        unreadable = hidden / ".claude" / "settings.json"
        unreadable.write_text("{}\n", encoding="utf-8")
        unreadable.chmod(0)
        try:
            if os.access(unreadable, os.R_OK):
                self.skipTest("this platform still allows the owner to read mode 0 files")
            hidden_findings = scan_project_permission_rules(hidden)
        finally:
            unreadable.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.assertIn("unreadable", needs_you_message(hidden_findings))
        self.assertFalse(above.exists())


class ParserTests(unittest.TestCase):
    def test_tomllib_and_python_311(self) -> None:
        source = (ROOT / "taskbench" / "permissions.py").read_text(encoding="utf-8")
        self.assertIn("tomllib.loads", source)
        self.assertNotIn("_parse_loose_value", source)
        yaml_source = (ROOT / "taskbench" / "yaml_subset.py").read_text(encoding="utf-8")
        self.assertIn("standard library", yaml_source.lower())
        note = (ROOT / "docs" / "parsers.md").read_text(encoding="utf-8")
        self.assertIn("tomllib", note)
        self.assertIn("joy-feeder.task/v2", note)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Python 3.11+", readme)
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("3.11", workflow)
        with self.assertRaises(SystemExit):
            require_python((3, 10))
        require_python((3, 11))
        require_python((3, 12))


class ModeInitTests(TempApp):
    def test_missing_or_wrong_init_fails_and_dontask_runs(self) -> None:
        _config, _app, watcher = self.make(scenario="init-no-mode", can_ask=False)
        write_packet(self.store.inbox, base_packet(id="init-none", permissions="plan", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "init-none") == "failed")
        missing = parse_yaml((self.store.outbox / "init-none.yaml").read_text(encoding="utf-8"))
        self.assertIn("missing permissionMode", missing["result"]["blocked_reason"])

        _config, _app, watcher = self.make(scenario="text-before-init", can_ask=False)
        write_packet(self.store.inbox, base_packet(id="init-late", permissions="plan", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "init-late") == "failed")
        late = parse_yaml((self.store.outbox / "init-late.yaml").read_text(encoding="utf-8"))
        self.assertIn("missing permissionMode init event", late["result"]["blocked_reason"])

        _config, _app, watcher = self.make(scenario="bad-permission", can_ask=False)
        write_packet(self.store.inbox, base_packet(id="init-bad", permissions="plan", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "init-bad") == "failed")
        bad = parse_yaml((self.store.outbox / "init-bad.yaml").read_text(encoding="utf-8"))
        self.assertIn("unexpected permission mode", bad["result"]["blocked_reason"])
        self.assertIn("always-approve", bad["result"]["blocked_reason"])

        _config, _app, watcher = self.make(scenario="echo", can_ask=False)
        write_packet(self.store.inbox, base_packet(id="init-ok", permissions="plan", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "init-ok") == "done")
        modes = (ROOT / "docs" / "grok-cli-modes.md").read_text(encoding="utf-8")
        self.assertIn("https://docs.x.ai/build/enterprise", modes)
        self.assertIn("streaming-messages-json", modes)
        self.assertIn("dontAsk", modes)


class CredentialSeedTests(unittest.TestCase):
    def test_api_key_or_auth_json_only(self) -> None:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source_home = root / "real"
        grok = source_home / ".grok"
        grok.mkdir(parents=True)
        (grok / "auth.json").write_text('{"token":"sekrit-token"}\n', encoding="utf-8")
        (grok / "config.toml").write_text('allow = ["DO_NOT_COPY"]\n', encoding="utf-8")
        parent = {
            "PATH": "/usr/bin",
            "HOME": str(source_home),
            "USERPROFILE": str(source_home),
            "XAI_API_KEY": "xai-test-key",
            "GROK_HOME": str(grok),
        }
        scrubbed = scrub_environment(parent)
        self.assertEqual(scrubbed["XAI_API_KEY"], "xai-test-key")
        self.assertNotIn("GROK_HOME", scrubbed)
        pinned = prepare_grok_config_home(root / "with-key")
        seed_grok_credentials(pinned, parent)
        self.assertEqual(list(pinned.iterdir()), [])
        no_key = {key: value for key, value in parent.items() if key != "XAI_API_KEY"}
        home = prepare_grok_config_home(root / "with-file")
        seed_grok_credentials(home, no_key)
        self.assertEqual((home / "auth.json").read_text(encoding="utf-8"), '{"token":"sekrit-token"}\n')
        self.assertFalse((home / "config.toml").exists())
        names = {path.name for path in home.iterdir()}
        self.assertEqual(names, {"auth.json"})
        note = (ROOT / "docs" / "grok-cli-home-and-auth.md").read_text(encoding="utf-8")
        plan = (ROOT / "docs" / "laptop-test-plan.md").read_text(encoding="utf-8")
        self.assertIn("XAI_API_KEY", note)
        self.assertIn("auth.json", note)
        self.assertIn("USERPROFILE", note)
        self.assertIn("known-folder", note)
        self.assertIn("TASKBENCH-PROBE", plan)
        self.assertNotIn("sekrit-token", note)
        self.assertTrue(json.loads((grok / "auth.json").read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
