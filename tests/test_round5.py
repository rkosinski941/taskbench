"""Round-5 review fixes. The smoke test launches ``python -m taskbench``."""

from __future__ import annotations

import base64
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from taskbench.cli import _install_signals, supervise
from taskbench.config import ConfigError, load_config
from taskbench.launch import spawn_runner
from taskbench.permissions import (
    apply_config_home,
    banned_permission_in_text,
    prepare_grok_config_home,
    seed_grok_credentials,
    write_grok_deny_rules,
)
from taskbench.process_tree import ProcessTreeError
from taskbench.redact import redact, remember_secret
from taskbench.safety import classify
from taskbench.ui.server import TaskbenchServer
from tests.support import FAKE, ROOT, TempApp, base_packet, write_packet


def _redact_url(text: str) -> str:
    return re.sub(r"token=[^\s&]+", "token=[redacted]", text)


class SignalAndHealthTests(TempApp):
    def test_signal_handlers_refuse_a_worker_thread(self) -> None:
        errors: list[str] = []

        def install() -> None:
            try:
                _install_signals(threading.Event())
            except RuntimeError as exc:
                errors.append(str(exc))

        thread = threading.Thread(target=install)
        thread.start()
        thread.join()
        self.assertEqual(errors, ["signal handlers are installed only on the main thread"])

    def test_dead_worker_exits_nonzero(self) -> None:
        _config, app, _watcher = self.make()
        stop = threading.Event()

        def boom() -> None:
            raise RuntimeError("watcher died")

        code = supervise(app, stop, [("watcher", boom)])
        self.assertEqual(code, 1)
        self.assertTrue(app.worker_down)
        self.assertFalse(app.watcher_healthy())

    def test_health_and_banner_when_the_watcher_is_down(self) -> None:
        import http.client

        _config, app, _watcher = self.make()
        app.note_worker_down("watcher thread died")
        server = TaskbenchServer(app, 0)
        self.server = server
        port = int(server.server_address[1])
        threading.Thread(target=server.serve_forever, daemon=True).start()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/health")
        health = conn.getresponse()
        body = health.read()
        status = health.status
        conn.close()
        self.assertEqual(status, 503)
        self.assertIn(b'"watcher":"down"', body)
        page = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        page.request("GET", "/")
        html = page.getresponse().read()
        page.close()
        self.assertIn(b"Watcher is down. New tasks will not run.", html)
        self.assertIn(b"data-watcher='down'", html)
        app.config.copy_auth_json = True
        warned = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        warned.request("GET", "/")
        warning = warned.getresponse().read()
        warned.close()
        self.assertIn(b"Copying auth.json is on.", warning)
        healthy = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        app.worker_down = False
        app.worker_down_reason = ""
        healthy.request("GET", "/health")
        up = healthy.getresponse()
        up_body = up.read()
        up_status = up.status
        healthy.close()
        self.assertEqual(up_status, 200)
        self.assertIn(b'"watcher":"up"', up_body)

    def test_login_link_expires(self) -> None:
        import http.client

        _config, app, _watcher = self.make()
        server = TaskbenchServer(app, 0)
        self.server = server
        port = int(server.server_address[1])
        threading.Thread(target=server.serve_forever, daemon=True).start()
        app.login_issued = time.monotonic() - app.config.login_ttl_s - 5
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/login?token=" + app.login_token)
        response = conn.getresponse()
        body = response.read()
        status = response.status
        conn.close()
        self.assertEqual(status, 403)
        self.assertIn(b"login token expired", body)
        self.assertFalse(app.login_consumed)


class CredentialAndRedactionTests(unittest.TestCase):
    def test_api_key_only_is_the_default_and_deny_rules_are_pinned(self) -> None:
        scratch = ROOT / "test-scratch"
        scratch.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source = root / "real"
        (source / ".grok").mkdir(parents=True)
        (source / ".grok" / "auth.json").write_text('{"token":"opt-in-token-value"}\n', encoding="utf-8")
        (source / ".grok" / "config.toml").write_text('allow = ["DO_NOT_COPY"]\n', encoding="utf-8")
        parent = {
            "HOME": str(source),
            "USERPROFILE": r"C:\Users\Owner",
            "PATH": "/usr/bin",
        }
        home = prepare_grok_config_home(root / "data")
        if os.name != "nt":
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        seed_grok_credentials(home, parent)
        self.assertFalse((home / "auth.json").exists())
        write_grok_deny_rules(home, parent)
        settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        deny = settings["permissions"]["deny"]
        self.assertTrue(any(item.startswith("Read(") and str(home) in item for item in deny))
        self.assertTrue(any(item.startswith("Read(") and ".grok" in item for item in deny))
        self.assertTrue(any(item.startswith("Read(") and ".claude" in item for item in deny))
        self.assertTrue(any("/" in item and ".grok" in item for item in deny))
        toml = (home / "config.toml").read_text(encoding="utf-8")
        grok_toml = (home / ".grok" / "config.toml").read_text(encoding="utf-8")
        self.assertIn("[permissions]", toml)
        self.assertIn("deny", toml)
        self.assertNotIn("defaultMode", toml)
        self.assertNotIn("DO_NOT_COPY", toml)
        self.assertNotIn("DO_NOT_COPY", grok_toml)
        self.assertNotIn("allow =", toml)
        if os.name != "nt":
            self.assertEqual((home / "config.toml").stat().st_mode & 0o777, 0o600)
        seed_grok_credentials(home, parent, copy_auth_json=True)
        self.assertEqual((home / "auth.json").read_text(encoding="utf-8"), '{"token":"opt-in-token-value"}\n')
        pinned = apply_config_home({"HOME": "/real/home", "PATH": "/usr/bin"}, home, pin_profile_home=False)
        self.assertEqual(pinned["GROK_HOME"], str(home))
        self.assertEqual(pinned["HOME"], "/real/home")
        replaced = apply_config_home({"HOME": "/real/home", "PATH": "/usr/bin"}, home)
        self.assertEqual(replaced["HOME"], str(home))

    def test_redaction_catches_case_zero_width_and_base64(self) -> None:
        secret = "round5-secret-value"
        remember_secret(secret)
        self.assertEqual(redact(secret), "[redacted]")
        self.assertEqual(redact(secret.upper()), "[redacted]")
        hidden = secret[:6] + "\u200b\u200d" + secret[6:]
        self.assertNotIn(secret, redact(hidden))
        self.assertNotIn("round5", redact(hidden))
        encoded = base64.b64encode(secret.encode("utf-8")).decode("ascii")
        url = base64.urlsafe_b64encode(secret.encode("utf-8")).decode("ascii")
        self.assertNotIn(encoded, redact("blob " + encoded))
        self.assertNotIn(url.rstrip("="), redact("blob " + url.rstrip("=")))
        self.assertEqual(redact("quota exceeded raw"), "quota exceeded raw")
        # Not a boundary: a split line and a backslash-u escape stay visible.
        split = secret[:6] + "\n" + secret[6:]
        self.assertIn(secret[:6], redact(split))
        escaped = "\\u0072ound5-secret-value"
        self.assertIn("ound5-secret-value", redact(escaped))

    def test_shell_payloads_are_scanned_and_still_need_approval(self) -> None:
        scratch = ROOT / "test-scratch"
        scratch.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)

        def decide(command: str) -> str:
            return classify(
                {"op": "shell", "command": command},
                permissions="ask",
                allow=[],
                sandbox_only=False,
                project_dir=folder,
            ).kind

        self.assertEqual(decide("sh -c 'grok --yolo'"), "refuse")
        self.assertEqual(decide('cmd /c "grok --always-approve"'), "refuse")
        self.assertEqual(
            decide('powershell -c "grok --permission-mode bypassPermissions"'),
            "refuse",
        )
        self.assertEqual(decide("echo hi"), "approve")
        self.assertIsNone(
            banned_permission_in_text("grok --permission-mode dontAsk --sandbox strict")
        )


class LaunchPinTests(TempApp):
    def test_spawn_pins_grok_home_without_copying_auth(self) -> None:
        import taskbench.launch as launch

        folder = self.root / "projects" / "demo-canvas"
        captured: list[dict[str, str]] = []
        real = launch.ManagedProcess

        class Capture:
            def __init__(self, argv: list[str], cwd: Path, env: dict[str, str]) -> None:
                captured.append(dict(env))
                raise ProcessTreeError("stop")

        launch.ManagedProcess = Capture  # type: ignore[misc, assignment]
        self.addCleanup(lambda: setattr(launch, "ManagedProcess", real))
        source = self.root / "profile"
        (source / ".grok").mkdir(parents=True)
        (source / ".grok" / "auth.json").write_text('{"token":"not-copied-token"}\n', encoding="utf-8")
        base = os.environ.copy()
        base["HOME"] = str(source)
        base["USERPROFILE"] = str(source)
        base.pop("XAI_API_KEY", None)
        _config, app, _watcher = self.make()
        self.assertFalse(app.config.copy_auth_json)
        with self.assertRaises(ProcessTreeError):
            spawn_runner(
                adapter=__import__("taskbench.adapters", fromlist=["get_adapter"]).get_adapter(
                    "grok-build", app.config
                ),
                folder=folder,
                prompt_file=folder / "INSTRUCTIONS.md",
                timeout_s=30,
                base_env=base,
                config=app.config,
            )
        home = self.root / "var" / "grok-home"
        self.assertFalse((home / "auth.json").exists())
        self.assertTrue((home / ".claude" / "settings.json").is_file())
        self.assertEqual(captured[0].get("GROK_HOME"), str(home))
        self.assertNotIn("XAI_API_KEY", captured[0])
        if os.name == "nt":
            self.assertEqual(captured[0].get("HOME"), str(home))
        else:
            self.assertEqual(captured[0].get("HOME"), str(source))
        note = (ROOT / "docs" / "grok-cli-home-and-auth.md").read_text(encoding="utf-8")
        self.assertIn("best-effort", note)
        self.assertIn("copy_auth_json", note)
        self.assertIn("GetSystemDirectoryW", note)


class LaptopPlanTests(unittest.TestCase):
    def test_probe_rules_and_acceptance_checklist(self) -> None:
        plan = (ROOT / "docs" / "laptop-test-plan.md").read_text(encoding="utf-8")
        self.assertIn(r"var\runtime\<id>.raw.log", plan)
        self.assertIn("--permission-mode dontAsk", plan)
        self.assertIn("--output-format streaming-messages-json", plan)
        self.assertIn("--sandbox strict", plan)
        self.assertIn("only the `.grok` rule", plan)
        self.assertIn("only the `.claude` rule", plan)
        self.assertIn("Sent to **Needs you** does not count as a pass.", plan)
        self.assertIn("Laptop acceptance checklist", plan)
        self.assertIn("A real task runs end to end.", plan)
        self.assertIn("the init line shows permissionMode dontAsk", plan)
        self.assertIn("With each planted rule alone", plan)
        self.assertIn("The Windows banner is the first output line.", plan)
        self.assertIn("No auth.json remains after done, timeout, and cancel", plan)
        self.assertIn("API-key-only mode is used with no copy", plan)
        self.assertIn("Renaming the workdir or a parent fails while a task runs.", plan)
        self.assertIn("A .cmd Grok shim starts correctly.", plan)
        self.assertIn("Bash(echo TASKBENCH-PROBE*)", plan)
        self.assertIn("echo TASKBENCH-PROBE-1", plan)
        self.assertIn("does not sit in the path", plan)
        self.assertNotIn('allow = ["TASKBENCH-PROBE"]', plan)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("login_ttl_s", readme)
        self.assertIn("copy_auth_json", readme)
        self.assertIn("GET /health", readme)
        self.assertIn("best-effort", readme)


class ConfigFlagTests(unittest.TestCase):
    def test_copy_flag_and_login_ttl_are_validated(self) -> None:
        scratch = ROOT / "test-scratch"
        scratch.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "bind_host": "127.0.0.1",
                    "port": 8765,
                    "data_dir": "var",
                    "projects_dir": "projects",
                    "copy_auth_json": "yes",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError) as copied:
            load_config(path)
        self.assertIn("copy_auth_json", str(copied.exception))
        path.write_text(
            json.dumps(
                {
                    "bind_host": "127.0.0.1",
                    "port": 8765,
                    "data_dir": "var",
                    "projects_dir": "projects",
                    "login_ttl_s": 0,
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError) as ttl:
            load_config(path)
        self.assertIn("login_ttl_s", str(ttl.exception))


class SmokeTests(unittest.TestCase):
    def test_python_m_taskbench_completes_a_packet(self) -> None:
        scratch = ROOT / "test-scratch"
        scratch.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        etc = root / "etc"
        var = root / "var"
        projects = root / "projects"
        etc.mkdir()
        projects.mkdir()
        demo = projects / "demo-canvas"
        import shutil

        shutil.copytree(ROOT / "examples" / "demo-canvas", demo)
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
        sock.close()
        config = {
            "bind_host": "127.0.0.1",
            "port": port,
            "data_dir": str(var),
            "projects_dir": str(projects),
            "copy_auth_json": False,
            "login_ttl_s": 600,
            "runners": {
                "grok-build": {
                    "adapter": "grok_build",
                    "executable": sys.executable,
                    "args_template": [
                        str(FAKE),
                        "--scenario",
                        "echo",
                        "--prompt-file",
                        "{prompt_file}",
                        "--project-dir",
                        "{project_dir}",
                    ],
                    "can_ask": False,
                    "sandbox_only": False,
                }
            },
        }
        cfg_path = etc / "config.json"
        cfg_path.write_text(json.dumps(config), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env.pop("XAI_API_KEY", None)
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            previous = signal.signal(signal.SIGBREAK, signal.SIG_IGN)
            self.addCleanup(lambda: signal.signal(signal.SIGBREAK, previous))
        proc = subprocess.Popen(
            [sys.executable, "-m", "taskbench", "--config", str(cfg_path)],
            cwd=str(root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        stdout: list[bytes] = []
        stderr: list[bytes] = []

        def drain(pipe: object, sink: list[bytes]) -> None:
            if pipe is None:
                return
            while True:
                line = pipe.readline()  # type: ignore[attr-defined]
                if not line:
                    break
                sink.append(line)

        threading.Thread(target=drain, args=(proc.stdout, stdout), daemon=True).start()
        threading.Thread(target=drain, args=(proc.stderr, stderr), daemon=True).start()

        def stop() -> None:
            if proc.poll() is None:
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()

        self.addCleanup(stop)

        def output() -> str:
            return _redact_url((b"".join(stdout) + b"".join(stderr)).decode("utf-8", "replace"))

        deadline = time.time() + 20
        started = False
        while time.time() < deadline:
            if f"taskbench 127.0.0.1:{port}" in b"".join(stdout).decode("utf-8", "replace"):
                started = True
                break
            if proc.poll() is not None:
                self.fail(f"exited {proc.returncode} before ready\n{output()}")
            time.sleep(0.05)
        self.assertTrue(started, output())

        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/health")
        health = conn.getresponse()
        health_body = health.read()
        health_status = health.status
        conn.close()
        self.assertEqual(health_status, 200, output())
        self.assertIn(b'"watcher":"up"', health_body)

        write_packet(var / "inbox", base_packet(id="smoke-1", permissions="plan", timeout_s=30))
        out = var / "outbox" / "smoke-1.json"
        deadline = time.time() + 40
        status = None
        while time.time() < deadline:
            if out.is_file():
                payload = json.loads(out.read_text(encoding="utf-8"))
                status = payload.get("status")
                if status == "done":
                    break
                if status in {"failed", "blocked", "cancelled", "timed-out"}:
                    self.fail(f"smoke packet {status}: {payload.get('result')}\n{output()}")
            if proc.poll() is not None:
                self.fail(f"process exited {proc.returncode} while waiting\n{output()}")
            time.sleep(0.1)
        else:
            self.fail(f"smoke packet status {status}\n{output()}")

        self.assertFalse((var / "grok-home" / "auth.json").exists())
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=15)
        self.assertEqual(code, 0, output())


if __name__ == "__main__":
    unittest.main()
