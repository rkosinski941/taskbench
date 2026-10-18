"""Working directory selection, registry, and path rejection."""

from __future__ import annotations

import http.client
import os
import re
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from taskbench.safety import classify
from taskbench.task_format import load_packet_text, parse_yaml
from taskbench.ui.server import TaskbenchServer, pick_local_folder
from taskbench.workdir import (
    WorkdirError,
    is_registered,
    load_registry,
    register_workdir,
    rejection_reason,
    validate_workdir,
)
from tests.support import TempApp, base_packet, command_write_marker, make_config, packet_status, run_until, write_packet


def _link_dir(link: Path, target: Path) -> bool:
    """Point ``link`` at ``target``. Try a symlink, then a Windows junction."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError:
        pass
    if os.name != "nt":
        return False
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    return created.returncode == 0 and link.exists()


def _safe_dir(parent: Path, name: str) -> Path:
    path = parent / name
    path.mkdir()
    return path


class RejectionTests(unittest.TestCase):
    def test_every_rejected_class(self) -> None:
        missing = Path(tempfile.gettempdir()) / "taskbench-missing-workdir"
        with self.assertRaises(WorkdirError) as missing_error:
            validate_workdir(str(missing))
        self.assertIn("does not exist", str(missing_error.exception))

        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        with self.assertRaises(WorkdirError) as file_error:
            validate_workdir(handle.name)
        self.assertIn("not a directory", str(file_error.exception))

        if os.name == "nt":
            drive = os.path.splitdrive(os.getcwd())[0] + "\\"
            with self.assertRaises(WorkdirError) as root_error:
                validate_workdir(drive)
        else:
            with self.assertRaises(WorkdirError) as root_error:
                validate_workdir("/")
        self.assertIn("drive root", str(root_error.exception))

        system_dirs = ("/etc", "/usr", "/bin") if os.name != "nt" else (
            os.environ.get("SYSTEMROOT", r"C:\Windows"),
            os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32"),
        )
        for system in system_dirs:
            if system and Path(system).is_dir():
                with self.subTest(system=system):
                    with self.assertRaises(WorkdirError) as caught:
                        validate_workdir(system)
                    self.assertIn("system folder", str(caught.exception))

        profile = str(Path.home())
        with self.assertRaises(WorkdirError) as profile_error:
            validate_workdir(profile)
        self.assertIn("user profile root", str(profile_error.exception))

        self.assertIn("drive root", rejection_reason("C:\\", platform="nt", profile_roots=[], system_roots=[]))
        self.assertIn(
            "drive root",
            rejection_reason(r"\\server\share", platform="nt", profile_roots=[], system_roots=[]),
        )
        self.assertIn(
            "system folder",
            rejection_reason(
                r"C:\Windows\System32",
                platform="nt",
                profile_roots=[],
                system_roots=[r"C:\Windows", r"C:\Windows\System32"],
            ),
        )
        self.assertIn(
            "system folder",
            rejection_reason(
                r"C:\Program Files\Tailscale",
                platform="nt",
                profile_roots=[],
                system_roots=[r"C:\Program Files"],
            ),
        )
        self.assertIn(
            "system folder",
            rejection_reason(
                r"C:\Program Files (x86)\App",
                platform="nt",
                profile_roots=[],
                system_roots=[r"C:\Program Files (x86)"],
            ),
        )
        self.assertIn(
            "system folder",
            rejection_reason(
                r"C:\ProgramData\taskbench",
                platform="nt",
                profile_roots=[],
                system_roots=[r"C:\ProgramData"],
            ),
        )
        self.assertIn(
            "user profile root",
            rejection_reason(
                r"C:\Users\Rich",
                platform="nt",
                profile_roots=[r"C:\Users\Rich"],
                system_roots=[],
            ),
        )
        self.assertIsNone(
            rejection_reason(
                r"C:\Users\Rich\src\app",
                platform="nt",
                profile_roots=[r"C:\Users\Rich"],
                system_roots=[r"C:\Windows", r"C:\Program Files", r"C:\ProgramData"],
            )
        )
        self.assertIn(
            "system folder",
            rejection_reason("/System/Library", platform="posix", profile_roots=[], system_roots=["/System"]),
        )

    def test_symlink_and_junction_resolve_before_accept(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        real = _safe_dir(root, "real")
        (real / "INSTRUCTIONS.md").write_text("from-real\n", encoding="utf-8")
        link = root / "jump"
        if not _link_dir(link, real):
            self.skipTest("symlinks and junctions are not permitted on this machine")
        resolved = validate_workdir(str(link))
        self.assertEqual(resolved, real.resolve())
        self.assertNotEqual(resolved, link)

        if os.name == "nt":
            forbidden = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        else:
            forbidden = Path("/etc")
        escape = root / "escape"
        if forbidden.is_dir() and _link_dir(escape, forbidden):
            with self.assertRaises(WorkdirError) as caught:
                validate_workdir(str(escape))
            self.assertIn("system folder", str(caught.exception))
            self.assertTrue(caught.exception.resolved)
            self.assertEqual(Path(caught.exception.resolved).resolve(), forbidden.resolve())
            self.assertNotEqual(Path(caught.exception.resolved), escape)
        outside = _safe_dir(root, "outside")
        (outside / "secret.txt").write_text("nope", encoding="utf-8")
        leak = real / "leak"
        if _link_dir(leak, outside):
            decision = classify(
                {"op": "read", "path": "leak/secret.txt"},
                permissions="plan",
                allow=[],
                sandbox_only=False,
                project_dir=resolved,
            )
            self.assertEqual(decision.kind, "refuse")


class RegistryAndRunTests(TempApp):
    def _workdir(self) -> Path:
        path = _safe_dir(self.root, "chosen-folder")
        (path / "INSTRUCTIONS.md").write_text("WORKDIR-INSTRUCTIONS-TOKEN\n", encoding="utf-8")
        return path

    def test_bot_unregistered_workdir_needs_you_and_is_not_stored(self) -> None:
        _config, _app, watcher = self.make(scenario="echo", can_ask=False)
        folder = self._workdir()
        packet = base_packet(id="bot-dir", permissions="plan", timeout_s=30)
        packet["workdir"] = str(folder)
        write_packet(self.store.inbox, packet)
        run_until(watcher, lambda: packet_status(self.store, "bot-dir") == "blocked")
        saved = parse_yaml((self.store.outbox / "bot-dir.yaml").read_text(encoding="utf-8"))
        reason = saved["result"]["blocked_reason"]
        self.assertTrue(reason.startswith("Needs you"))
        self.assertIn(str(folder.resolve()), reason)
        self.assertFalse(is_registered(self.root / "var", folder.resolve()))
        self.assertEqual(load_registry(self.root / "var"), [])
        self.assertFalse((folder / ".taskbench" / "LAST_AGENT.md").exists())

    def test_web_ui_registers_and_drives_scope(self) -> None:
        _config, _app, watcher = self.make(scenario="read-ok", can_ask=False)
        folder = self._workdir()
        packet = base_packet(id="ui-dir", permissions="plan", timeout_s=30, **{"from": "web-ui"})
        packet["workdir"] = str(folder)
        write_packet(self.store.inbox, packet)
        run_until(watcher, lambda: packet_status(self.store, "ui-dir") == "done")
        saved = parse_yaml((self.store.outbox / "ui-dir.yaml").read_text(encoding="utf-8"))
        self.assertEqual(saved["workdir"], str(folder.resolve()))
        self.assertTrue(is_registered(self.root / "var", folder.resolve()))
        prompt = (self.store.outbox / "ui-dir.prompt.md").read_text(encoding="utf-8")
        self.assertIn("WORKDIR-INSTRUCTIONS-TOKEN", prompt)
        self.assertIn(str(folder.resolve()), prompt)
        handoff = (folder / ".taskbench" / "LAST_AGENT.md").read_text(encoding="utf-8")
        self.assertIn("ui-dir", handoff)
        self.assertFalse((self.root / "projects" / "demo-canvas" / ".taskbench" / "LAST_AGENT.md").exists())
        output = (self.store.outbox / "ui-dir.output.log").read_text(encoding="utf-8")
        self.assertIn("WORKDIR-INSTRUCTIONS-TOKEN", output)

    def test_allow_list_runs_inside_the_workdir(self) -> None:
        folder = self._workdir()
        command = command_write_marker(folder, "ran.txt")
        _config, _app, watcher = self.make(
            scenario="approval-shell",
            can_ask=True,
            extra_args=["--command", command],
        )
        register_workdir(self.root / "var", folder.resolve())
        packet = base_packet(
            id="allow-dir",
            permissions="allow-list",
            timeout_s=30,
            allow=[command],
        )
        packet["workdir"] = str(folder.resolve())
        write_packet(self.store.inbox, packet)
        marker = folder / "ran.txt"
        run_until(watcher, lambda: marker.is_file())
        self.assertEqual(marker.read_text(encoding="utf-8"), "ran")
        self.assertFalse((self.root / "projects" / "demo-canvas" / "ran.txt").exists())

    def test_rules_are_scanned_in_the_workdir(self) -> None:
        _config, _app, watcher = self.make(scenario="echo", can_ask=False)
        folder = self._workdir()
        grok = folder / ".grok"
        grok.mkdir()
        grok.joinpath("config.toml").write_text('allow = ["SECRET_IN_WORKDIR"]\n', encoding="utf-8")
        packet = base_packet(id="scan-dir", permissions="plan", timeout_s=30, **{"from": "web-ui"})
        packet["workdir"] = str(folder)
        write_packet(self.store.inbox, packet)
        run_until(watcher, lambda: packet_status(self.store, "scan-dir") == "blocked")
        saved = parse_yaml((self.store.outbox / "scan-dir.yaml").read_text(encoding="utf-8"))
        reason = saved["result"]["blocked_reason"]
        self.assertTrue(reason.startswith("Needs you"))
        self.assertIn(".grok/config.toml", reason)
        self.assertIn("allow", reason)
        self.assertNotIn("SECRET_IN_WORKDIR", reason)

    def test_invalid_workdir_type_is_rejected_at_parse(self) -> None:
        packet = base_packet(id="bad-type")
        packet["workdir"] = ["not-a-path"]
        interpreted = load_packet_text(
            __import__("taskbench.task_format", fromlist=["emit_yaml"]).emit_yaml(packet),
            human_owner="Rich",
        )
        self.assertFalse(interpreted.ok)
        self.assertIn("invalid workdir", interpreted.reason or "")


class WebUiTests(TempApp):
    def test_form_registers_a_typed_folder_and_shows_it(self) -> None:
        self.make()
        folder = _safe_dir(self.root, "typed-folder")
        self.server = TaskbenchServer(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = int(self.server.server_address[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        page = conn.getresponse()
        body = page.read().decode("utf-8")
        conn.close()
        self.assertIn("name='workdir'", body)
        self.assertIn("recent-folders", body)
        token = re.search(r"name='csrf' value='([^']+)'", body)
        self.assertIsNotNone(token)
        cookie = page.getheader("Set-Cookie")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        form = (
            f"csrf={token.group(1)}&goal=Use+this+folder&project=demo-canvas"
            f"&runner=grok-build&permissions=plan&workdir={folder}"
        )
        conn.request(
            "POST",
            "/tasks",
            body=form,
            headers={"Cookie": cookie.split(";", 1)[0], "Content-Type": "application/x-www-form-urlencoded"},
        )
        created = conn.getresponse()
        created.read()
        conn.close()
        self.assertEqual(created.status, 303)
        self.assertTrue(is_registered(self.root / "var", folder.resolve()))
        inbox = list((self.root / "var" / "inbox").glob("*.yaml"))
        self.assertEqual(len(inbox), 1)
        saved = parse_yaml(inbox[0].read_text(encoding="utf-8"))
        self.assertEqual(saved["from"], "web-ui")
        self.assertEqual(saved["workdir"], str(folder.resolve()))

    def test_picker_is_refused_for_a_tailnet_host(self) -> None:
        import taskbench.ui.server as server

        config = make_config(self.root, allowed_hosts=["laptop.tailnet.ts.net"])
        from taskbench.ui.server import App

        app = App(config)
        self.app = app
        self.watcher = app.watcher
        self.server = TaskbenchServer(app, 0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        port = int(self.server.server_address[1])
        host = f"laptop.tailnet.ts.net:{port}"
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/", headers={"Host": host})
        page = conn.getresponse()
        body = page.read()
        conn.close()
        self.assertEqual(page.status, 200)
        self.assertNotIn(b"Browse folders", body)
        token = re.search(br"name='csrf' value='([^']+)'", body)
        self.assertIsNotNone(token)
        cookie = page.getheader("Set-Cookie")
        called = []

        def fail_if_called() -> str:
            called.append("called")
            return "/tmp"

        original = server.pick_local_folder
        server.pick_local_folder = fail_if_called
        self.addCleanup(lambda: setattr(server, "pick_local_folder", original))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/pick-folder",
            body=f"csrf={token.group(1).decode()}",
            headers={
                "Host": host,
                "Cookie": cookie.split(";", 1)[0],
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        refused = conn.getresponse()
        text = refused.read()
        conn.close()
        self.assertEqual(refused.status, 403)
        self.assertIn(b"local only", text)
        self.assertEqual(called, [])

    def test_picker_without_a_display_does_not_open(self) -> None:
        previous = os.environ.get("TASKBENCH_DISABLE_FOLDER_PICKER")
        os.environ["TASKBENCH_DISABLE_FOLDER_PICKER"] = "1"
        self.addCleanup(lambda: _restore_env("TASKBENCH_DISABLE_FOLDER_PICKER", previous))
        with self.assertRaises(WorkdirError) as caught:
            pick_local_folder()
        self.assertIn("not available", str(caught.exception))


def _restore_env(name: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


if __name__ == "__main__":
    unittest.main()
