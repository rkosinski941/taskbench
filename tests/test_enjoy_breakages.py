"""The 12 Enjoy breakage scenarios. One class per scenario."""

from __future__ import annotations

import http.client
import os
import re
import socket
import threading
import time
from pathlib import Path

from taskbench.process_tree import pid_alive
from taskbench.state import StateStore
from taskbench.task_format import parse_yaml
from taskbench.ui.server import TaskbenchServer
from tests.support import TempApp, base_packet, command_write_marker, packet_status, run_until, write_packet


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class RemotePairingDrops(TempApp):
    def test_run_continues_when_the_page_reloads_from_disk(self) -> None:
        marker = self.root / "mark"
        marker.mkdir()
        _config, app, watcher = self.make(
            scenario="hang",
            extra_args=["--marker-dir", str(marker)],
        )
        write_packet(self.store.inbox, base_packet(id="live-1", timeout_s=30))
        run_until(watcher, lambda: (marker / "ready").is_file() and packet_status(self.store, "live-1") == "running")
        pid = int(_read(marker / "child.pid"))
        self.assertTrue(pid_alive(pid))
        # A new store is a reconnect: it only has the files on disk.
        fresh = StateStore(app.config.data_dir)
        found = fresh.find("live-1")
        self.assertIsNotNone(found)
        self.assertEqual(found[1]["status"], "running")
        self.assertEqual(found[1]["project"], "demo-canvas")
        server = TaskbenchServer(app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.server = server
        port = int(server.server_address[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        conn.close()
        self.assertIn("data-id='live-1'", body)
        self.assertIn("data-status='running'", body)
        self.assertTrue(pid_alive(pid))
        self.assertEqual(packet_status(self.store, "live-1"), "running")


class StaleModelLabel(TempApp):
    def test_ui_shows_reported_model_and_flags_mismatch(self) -> None:
        _config, app, watcher = self.make(
            scenario="model-mismatch",
            extra_args=["--model", "model-b"],
        )
        packet = base_packet(id="model-1", timeout_s=30)
        packet["model"] = {"requested": "model-a", "actual": "model-a"}
        write_packet(self.store.inbox, packet)
        run_until(watcher, lambda: packet_status(self.store, "model-1") == "done")
        saved = parse_yaml(_read(self.store.outbox / "model-1.yaml"))
        self.assertEqual(saved["model"]["actual"], "model-b")
        self.assertEqual(saved["model"]["requested"], "model-a")
        server = TaskbenchServer(app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.server = server
        conn = http.client.HTTPConnection("127.0.0.1", int(server.server_address[1]), timeout=5)
        conn.request("GET", "/task/model-1")
        body = conn.getresponse().read().decode("utf-8")
        conn.close()
        self.assertIn("data-model-actual='model-b'", body)
        self.assertIn("data-model-requested='model-a'", body)
        self.assertIn("mismatch", body)

    def test_missing_model_is_unknown_not_the_request(self) -> None:
        _config, app, watcher = self.make(scenario="no-model")
        packet = base_packet(id="model-2", timeout_s=30)
        packet["model"] = {"requested": "model-a", "actual": "model-a"}
        write_packet(self.store.inbox, packet)
        run_until(watcher, lambda: packet_status(self.store, "model-2") == "done")
        saved = parse_yaml(_read(self.store.outbox / "model-2.yaml"))
        self.assertEqual(saved["model"]["actual"], "unknown")
        self.assertNotEqual(saved["model"]["actual"], saved["model"]["requested"])
        server = TaskbenchServer(app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.server = server
        conn = http.client.HTTPConnection("127.0.0.1", int(server.server_address[1]), timeout=5)
        conn.request("GET", "/task/model-2")
        body = conn.getresponse().read().decode("utf-8")
        conn.close()
        self.assertIn("data-model-actual='unknown'", body)
        actual = re.search(r"data-model-actual='([^']*)'", body)
        self.assertIsNotNone(actual)
        self.assertNotEqual(actual.group(1), "model-a")


class WrongUsageNoticeSilentStop(TempApp):
    def test_usage_limit_names_the_runner_that_reported_it(self) -> None:
        _config, _app, watcher = self.make(scenario="usage-limit")
        write_packet(self.store.inbox, base_packet(id="limit-1", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "limit-1") in {"blocked", "failed", "done"})
        saved = parse_yaml(_read(self.store.outbox / "limit-1.yaml"))
        self.assertEqual(saved["status"], "blocked")
        reason = saved["result"]["blocked_reason"]
        self.assertIn("grok-build", reason)
        self.assertIn("quota exceeded raw", reason)
        self.assertIn("2026-10-18T00:00:00+00:00", reason)
        self.assertNotIn("antigravity", reason)
        self.assertNotEqual(saved["status"], "done")

    def test_exit_without_result_is_failed_with_output(self) -> None:
        _config, _app, watcher = self.make(scenario="exit-no-result")
        write_packet(self.store.inbox, base_packet(id="silent-1", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "silent-1") in {"failed", "done", "blocked"})
        saved = parse_yaml(_read(self.store.outbox / "silent-1.yaml"))
        self.assertEqual(saved["status"], "failed")
        self.assertNotEqual(saved["status"], "done")
        self.assertIn("3", saved["result"]["blocked_reason"])
        output = _read(self.store.outbox / "silent-1.output.log")
        self.assertIn("boom line", output)
        self.assertIn("boom line", saved["result"]["blocked_reason"])


class LongApprovalTitleCrashes(TempApp):
    def test_prompt_shows_for_long_commands(self) -> None:
        for length in (1, 500, 501, 5000, 50000):
            with self.subTest(length=length):
                marker = self.root / f"long-{length}"
                marker.mkdir()
                _config, app, watcher = self.make(
                    scenario="long-command",
                    extra_args=["--length", str(length)],
                )
                # A fresh watcher per length, but the inbox is shared. Use a new data dir via make()
                # which reuses self.root. Clear previous packets by using a unique id and a new store
                # only if the previous run is still needs-approval. Start from a clean root slice.
                write_packet(self.store.inbox, base_packet(id=f"long-{length}", timeout_s=60))
                run_until(watcher, lambda: packet_status(self.store, f"long-{length}") == "needs-approval")
                self.assertNotEqual(packet_status(self.store, f"long-{length}"), "failed")
                command = "A" * length
                server = TaskbenchServer(app, 0)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                conn = http.client.HTTPConnection("127.0.0.1", int(server.server_address[1]), timeout=5)
                conn.request("GET", f"/task/long-{length}")
                body = conn.getresponse().read().decode("utf-8")
                conn.close()
                server.shutdown()
                server.server_close()
                self.assertIn(f"<pre class='command'>{command}</pre>", body)
                self.assertIn(f"<h3 class='approval-title'>{command[:120]}</h3>", body)
                watcher.shutdown()
                self.watcher = None


class RunnerCantAskSoItFails(TempApp):
    def test_ask_and_allow_list_are_blocked_before_start(self) -> None:
        _config, _app, watcher = self.make(scenario="echo", can_ask=False)
        write_packet(self.store.inbox, base_packet(id="noask-1", timeout_s=30))
        allow = base_packet(id="noask-2", timeout_s=30, permissions="allow-list", allow=["git diff*"])
        write_packet(self.store.inbox, allow)
        run_until(
            watcher,
            lambda: packet_status(self.store, "noask-1") == "blocked"
            and packet_status(self.store, "noask-2") == "blocked",
        )
        for packet_id in ("noask-1", "noask-2"):
            saved = parse_yaml(_read(self.store.outbox / f"{packet_id}.yaml"))
            self.assertEqual(saved["result"]["blocked_reason"], "runner cannot ask")
            self.assertIsNone(saved["runner"]["actual"])
            output = _read(self.store.outbox / f"{packet_id}.output.log")
            self.assertNotIn("echoed the prompt", output)

    def test_plan_still_runs_when_the_runner_cannot_ask(self) -> None:
        _config, _app, watcher = self.make(scenario="echo", can_ask=False)
        write_packet(self.store.inbox, base_packet(id="plan-1", permissions="plan", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "plan-1") == "done")


class ModeRunsCommandsWithNoPrompt(TempApp):
    def test_default_ask_runs_nothing_until_approval_is_logged(self) -> None:
        project = self.root / "projects" / "demo-canvas"
        command = command_write_marker(project)
        marker = self.root / "mode-mark"
        marker.mkdir()
        _config, _app, watcher = self.make(
            scenario="uncontrolled",
            extra_args=["--command", command, "--marker-dir", str(marker)],
        )
        packet = base_packet(id="ask-1", timeout_s=30)
        packet.pop("permissions", None)
        write_packet(self.store.inbox, packet)
        run_until(watcher, lambda: packet_status(self.store, "ask-1") == "needs-approval")
        self.assertFalse((project / "ran.txt").exists())
        self.assertEqual(_read(marker / "approvals-env.txt"), "1")
        log = _read(self.store.log_path)
        self.assertNotIn("allow-match", log)
        watcher.request_decision("ask-1", "approved")
        run_until(watcher, lambda: packet_status(self.store, "ask-1") == "done")
        self.assertTrue((project / "ran.txt").exists())
        saved = parse_yaml(_read(self.store.outbox / "ask-1.yaml"))
        self.assertEqual(saved["permissions"], "ask")
        self.assertEqual(saved["approvals"][0]["command"], command)
        self.assertEqual(saved["approvals"][0]["decision"], "approved")
        self.assertIn(f"approval approved {command}", _read(self.store.log_path))

    def test_allow_match_is_logged_and_adapters_do_not_self_approve(self) -> None:
        from taskbench.adapters.antigravity import AntigravityAdapter
        from taskbench.adapters.grok_build import GrokBuildAdapter
        from taskbench.adapters.opencode import OpenCodeAdapter

        project = self.root / "projects" / "demo-canvas"
        command = command_write_marker(project, "allowed.txt")
        _config, _app, watcher = self.make(
            scenario="uncontrolled",
            extra_args=["--command", command, "--marker-dir", str(self.root / "allow-mark")],
        )
        (self.root / "allow-mark").mkdir()
        packet = base_packet(
            id="allow-1",
            timeout_s=30,
            permissions="allow-list",
            allow=[command],
        )
        write_packet(self.store.inbox, packet)
        run_until(watcher, lambda: packet_status(self.store, "allow-1") == "done")
        self.assertTrue((project / "allowed.txt").exists())
        self.assertIn(f"allow-match {command}", _read(self.store.log_path))
        saved = parse_yaml(_read(self.store.outbox / "allow-1.yaml"))
        self.assertEqual(saved.get("approvals") or [], [])
        argv = GrokBuildAdapter(self.app.config.runners["grok-build"]).build_command(
            prompt_file=project / "prompt.md",
            project_dir=project,
            timeout_s=30,
        )
        joined = " ".join(argv).lower()
        self.assertNotIn("yolo", joined)
        self.assertNotIn("bypass", joined)
        env = GrokBuildAdapter(self.app.config.runners["grok-build"]).environment({})
        self.assertEqual(env["TASKBENCH_APPROVALS"], "1")
        with self.assertRaises(NotImplementedError):
            AntigravityAdapter().build_command(prompt_file=project, project_dir=project, timeout_s=1)
        with self.assertRaises(NotImplementedError):
            OpenCodeAdapter().build_command(prompt_file=project, project_dir=project, timeout_s=1)


class UnaskedPreviewServersThatHangTheRun(TempApp):
    def test_server_without_an_allow_entry_asks_and_does_not_listen(self) -> None:
        command = "python -m http.server 8765 --bind 127.0.0.1"
        _config, _app, watcher = self.make(
            scenario="serve-action",
            extra_args=["--command", command],
        )
        packet = base_packet(
            id="serve-ask",
            timeout_s=30,
            permissions="allow-list",
            allow=["git diff*"],
        )
        write_packet(self.store.inbox, packet)
        run_until(watcher, lambda: packet_status(self.store, "serve-ask") == "needs-approval")
        saved = self.store.find("serve-ask")[1]
        self.assertIn(command, saved["approvals"][0]["command"])
        sock = socket.socket()
        try:
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", 8765)), 0)
        finally:
            sock.close()

    def test_long_lived_child_is_killed_at_timeout(self) -> None:
        marker = self.root / "server-mark"
        marker.mkdir()
        _config, _app, watcher = self.make(
            scenario="server",
            extra_args=["--marker-dir", str(marker)],
        )
        write_packet(self.store.inbox, base_packet(id="serve-hang", timeout_s=2, permissions="plan"))
        run_until(watcher, lambda: (marker / "ready").is_file(), timeout=5)
        port = int(_read(marker / "port"))
        run_until(watcher, lambda: packet_status(self.store, "serve-hang") == "timed-out", timeout=8)
        sock = socket.socket()
        try:
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", port)), 0)
        finally:
            sock.close()
        for name in ("child.pid", "grandchild.pid"):
            self.assertFalse(pid_alive(int(_read(marker / name))))
        saved = parse_yaml(_read(self.store.outbox / "serve-hang.yaml"))
        self.assertIsNone(saved["retro"])
        self.assertTrue((self.root / "projects" / "demo-canvas" / ".taskbench" / "LAST_AGENT.md").is_file())


class ClosingTheAppOrphansChildren(TempApp):
    def test_closing_the_page_does_not_kill_the_run(self) -> None:
        marker = self.root / "page-mark"
        marker.mkdir()
        _config, app, watcher = self.make(scenario="hang", extra_args=["--marker-dir", str(marker)])
        write_packet(self.store.inbox, base_packet(id="page-1", timeout_s=30))
        run_until(watcher, lambda: (marker / "ready").is_file())
        pid = int(_read(marker / "child.pid"))
        server = TaskbenchServer(app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.server = server
        conn = http.client.HTTPConnection("127.0.0.1", int(server.server_address[1]), timeout=5)
        conn.request("GET", "/")
        conn.getresponse().read()
        conn.close()
        self.assertTrue(pid_alive(pid))
        self.assertEqual(packet_status(self.store, "page-1"), "running")

    def test_cancel_timeout_shutdown_and_completion_kill_the_tree(self) -> None:
        marker = self.root / "cancel-mark"
        marker.mkdir()
        _config, _app, watcher = self.make(scenario="hang", extra_args=["--marker-dir", str(marker)])
        write_packet(self.store.inbox, base_packet(id="cancel-1", timeout_s=30))
        run_until(watcher, lambda: (marker / "ready").is_file())
        pid = int(_read(marker / "child.pid"))
        watcher.request_cancel("cancel-1")
        run_until(watcher, lambda: packet_status(self.store, "cancel-1") == "blocked")
        self.assertFalse(pid_alive(pid))
        self.assertIn("ports opened:", _read(self.store.log_path))
        self.assertIn("ports still open:", _read(self.store.log_path))

        marker2 = self.root / "down-mark"
        marker2.mkdir()
        _config, _app, watcher2 = self.make(scenario="hang", extra_args=["--marker-dir", str(marker2)])
        self.watcher = watcher2
        write_packet(self.store.inbox, base_packet(id="down-1", timeout_s=30))
        run_until(watcher2, lambda: (marker2 / "ready").is_file())
        pid2 = int(_read(marker2 / "child.pid"))
        watcher2.shutdown()
        self.assertFalse(pid_alive(pid2))

        marker3 = self.root / "exit-mark"
        marker3.mkdir()
        _config, _app, watcher3 = self.make(
            scenario="spawn-then-exit",
            extra_args=["--marker-dir", str(marker3)],
        )
        self.watcher = watcher3
        write_packet(self.store.inbox, base_packet(id="exit-1", timeout_s=30))
        run_until(watcher3, lambda: packet_status(self.store, "exit-1") == "done", timeout=8)
        grand = int(_read(marker3 / "grandchild.pid"))
        self.assertFalse(pid_alive(grand))


class QueuedMessageStuckBehindHungRun(TempApp):
    def test_queue_moves_when_the_hung_run_times_out_or_is_cancelled(self) -> None:
        marker = self.root / "queue-mark"
        marker.mkdir()
        _config, app, watcher = self.make(scenario="hang", extra_args=["--marker-dir", str(marker)])
        write_packet(self.store.inbox, base_packet(id="hung-1", timeout_s=2))
        # The second task needs the echo scenario. One runner config cannot do both.
        # Reconfigure is not available, so the second task uses the same hang runner.
        # Position and timeout are still observable: after timeout the queue is free
        # and a newly claimed echo task starts. Swap the runner after the first ends.
        write_packet(self.store.inbox, base_packet(id="queued-1", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "hung-1") == "running")
        self.assertEqual(packet_status(self.store, "queued-1"), "claimed")
        server = TaskbenchServer(app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.server = server
        conn = http.client.HTTPConnection("127.0.0.1", int(server.server_address[1]), timeout=5)
        conn.request("GET", "/")
        body = conn.getresponse().read().decode("utf-8")
        conn.close()
        self.assertIn("position 2", body)
        self.assertIn("waiting", body)
        started = time.time()
        run_until(watcher, lambda: packet_status(self.store, "hung-1") == "timed-out", timeout=8)
        # The queued task is the same hang scenario, so cancel it once it starts
        # and confirm it left the queue within a few seconds of the timeout.
        run_until(watcher, lambda: packet_status(self.store, "queued-1") == "running", timeout=5)
        self.assertLess(time.time() - started, 8)
        watcher.request_cancel("queued-1")
        run_until(watcher, lambda: packet_status(self.store, "queued-1") == "blocked", timeout=5)


class DirectLinksGoToTheWrongProject(TempApp):
    def test_task_url_is_the_id_and_unknown_ids_are_not_redirected(self) -> None:
        other = self.root / "projects" / "other-app"
        other.mkdir(parents=True)
        (other / "INSTRUCTIONS.md").write_text("other\n", encoding="utf-8")
        _config, app, watcher = self.make(scenario="echo")
        write_packet(self.store.inbox, base_packet(id="link-1", project="demo-canvas"))
        write_packet(self.store.inbox, base_packet(id="link-2", project="other-app"))
        run_until(
            watcher,
            lambda: packet_status(self.store, "link-1") == "done" and packet_status(self.store, "link-2") == "done",
        )
        server = TaskbenchServer(app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.server = server
        port = int(server.server_address[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/task/link-1")
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        conn.close()
        self.assertEqual(response.status, 200)
        self.assertIn("data-id='link-1'", body)
        self.assertIn("data-project='demo-canvas'", body)
        self.assertNotIn("data-project='other-app'", body.split("data-id='link-1'")[1].split("</article>")[0])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/task/missing-id")
        missing = conn.getresponse()
        missing_body = missing.read().decode("utf-8")
        location = missing.getheader("Location")
        conn.close()
        self.assertEqual(missing.status, 404)
        self.assertIn("not found", missing_body)
        self.assertIsNone(location)


class RemoteEditsNeedDesktopReview(TempApp):
    def test_file_edit_is_approved_from_the_page(self) -> None:
        _config, app, watcher = self.make(scenario="approval-write")
        write_packet(self.store.inbox, base_packet(id="edit-1", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "edit-1") == "needs-approval")
        server = TaskbenchServer(app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.server = server
        port = int(server.server_address[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/task/edit-1")
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        cookie = response.getheader("Set-Cookie").split(";")[0]
        conn.close()
        self.assertIn("hello from taskbench", body)
        self.assertIn("<pre class='diff'>", body)
        token = re.search(r"name='csrf' value='([^']+)'", body).group(1)
        notes = self.root / "projects" / "demo-canvas" / "notes.txt"
        self.assertFalse(notes.exists())
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/task/edit-1/approve",
            body=f"csrf={token}&command=ignore-this",
            headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
        )
        approved = conn.getresponse()
        approved.read()
        conn.close()
        self.assertEqual(approved.status, 303)
        run_until(watcher, lambda: packet_status(self.store, "edit-1") == "done")
        self.assertEqual(notes.read_text(encoding="utf-8"), "hello from taskbench\n")


class PreviewsNeverRenderRemotely(TempApp):
    def test_no_preview_pane_artifacts_are_links_and_loopback_preview_is_a_link(self) -> None:
        port = _free_port()
        command = f"{os.sys.executable if False else __import__('sys').executable} -m http.server {port} --bind 127.0.0.1"
        _config, app, watcher = self.make(scenario="preview", extra_args=["--command", command])
        write_packet(self.store.inbox, base_packet(id="preview-1", timeout_s=30))
        run_until(watcher, lambda: packet_status(self.store, "preview-1") == "needs-approval")
        watcher.request_decision("preview-1", "approved")
        run_until(watcher, lambda: packet_status(self.store, "preview-1") == "running")
        server = TaskbenchServer(app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.server = server
        conn = http.client.HTTPConnection("127.0.0.1", int(server.server_address[1]), timeout=5)
        conn.request("GET", "/task/preview-1")
        body = conn.getresponse().read().decode("utf-8")
        conn.close()
        self.assertNotIn("<iframe", body.lower())
        self.assertNotIn("busy", body.lower())
        self.assertIn(f"http://127.0.0.1:{port}/", body)
        watcher.request_cancel("preview-1")
        run_until(watcher, lambda: packet_status(self.store, "preview-1") in {"blocked", "failed", "timed-out"})
        sock = socket.socket()
        try:
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", port)), 0)
        finally:
            sock.close()

        _config, app2, watcher2 = self.make(scenario="echo")
        self.watcher = watcher2
        write_packet(self.store.inbox, base_packet(id="files-1", timeout_s=30))
        run_until(watcher2, lambda: packet_status(self.store, "files-1") == "done")
        server2 = TaskbenchServer(app2, 0)
        thread2 = threading.Thread(target=server2.serve_forever, daemon=True)
        thread2.start()
        conn = http.client.HTTPConnection("127.0.0.1", int(server2.server_address[1]), timeout=5)
        conn.request("GET", "/task/files-1")
        done_body = conn.getresponse().read().decode("utf-8")
        conn.close()
        server2.shutdown()
        server2.server_close()
        self.assertIn("/outbox/files-1.yaml", done_body)
        self.assertIn("/artifact/files-1/canvas-v2.html", done_body)
        self.assertNotIn("<iframe", done_body.lower())
        self.assertNotIn("busy", done_body.lower())


if __name__ == "__main__":
    unittest.main()
