"""Loopback bind, Host allow-list, CSRF, and Tailscale-User-Login display."""

from __future__ import annotations

import http.client
import re
import threading
import unittest

from taskbench.ui.server import BindError, TaskbenchServer, ensure_loopback, host_allowed
from tests.support import TempApp, base_packet, write_packet


class BindAndHostTests(unittest.TestCase):
    def test_only_loopback_is_accepted(self) -> None:
        ensure_loopback("127.0.0.1")
        for host in ("0.0.0.0", "::", "::1", "localhost", "", "192.168.1.1"):
            with self.assertRaises(BindError):
                ensure_loopback(host)

    def test_host_allow_list_defaults_empty(self) -> None:
        self.assertTrue(host_allowed("127.0.0.1:8765", 8765, []))
        self.assertTrue(host_allowed("localhost", 8765, []))
        self.assertFalse(host_allowed("evil.example", 8765, []))
        self.assertFalse(host_allowed("127.0.0.1.evil.example", 8765, []))
        self.assertFalse(host_allowed("laptop.tailnet.ts.net", 8765, []))
        self.assertTrue(host_allowed("laptop.tailnet.ts.net:8765", 8765, ["laptop.tailnet.ts.net"]))
        self.assertFalse(host_allowed("other.tailnet.ts.net", 8765, ["laptop.tailnet.ts.net"]))
        self.assertFalse(host_allowed(None, 8765, []))


class PageTests(TempApp):
    def setUp(self) -> None:
        super().setUp()
        self.make()
        self.server = TaskbenchServer(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = int(self.server.server_address[1])

    def _get(self, path: str, headers: dict[str, str] | None = None) -> http.client.HTTPResponse:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        response.body = response.read()  # type: ignore[attr-defined]
        conn.close()
        return response

    def _token(self, response: http.client.HTTPResponse) -> str:
        match = re.search(br"name='csrf' value='([^']+)'", response.body)  # type: ignore[attr-defined]
        self.assertIsNotNone(match)
        return match.group(1).decode("ascii")

    def test_bind_address_is_loopback(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_evil_host_rejected(self) -> None:
        response = self._get("/", {"Host": "evil.example"})
        self.assertEqual(response.status, 400)

    def test_csrf_and_web_ui_from(self) -> None:
        page = self._get("/")
        token = self._token(page)
        cookie = page.getheader("Set-Cookie")
        self.assertIn("taskbench_csrf=", cookie)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "POST",
            "/tasks",
            body="goal=Hello&project=demo-canvas&runner=grok-build&from=Joy+Feeder",
        )
        denied = conn.getresponse()
        denied.read()
        conn.close()
        self.assertEqual(denied.status, 403)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        launch = re.search(br"name='launch' value='([^']+)'", page.body).group(1).decode("ascii")
        body = (
            f"csrf={token}&launch={launch}&goal=Hello+from+the+page"
            f"&project=demo-canvas&runner=grok-build&from=Joy+Feeder"
        )
        conn.request(
            "POST",
            "/tasks",
            body=body,
            headers={
                "Cookie": cookie.split(";")[0],
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": f"http://127.0.0.1:{self.port}",
            },
        )
        created = conn.getresponse()
        created.read()
        conn.close()
        self.assertEqual(created.status, 303)
        inbox = list(self.store.inbox.glob("*.yaml"))
        self.assertEqual(len(inbox), 1)
        text = inbox[0].read_text(encoding="utf-8")
        self.assertIn("from: web-ui", text)
        self.assertNotIn("Joy Feeder", text)

    def test_tailscale_header_is_display_only(self) -> None:
        missing = self._get("/")
        self.assertNotIn(b"data-tailscale-user", missing.body)  # type: ignore[attr-defined]
        shown = self._get("/", {"Tailscale-User-Login": "alice@example.com"})
        self.assertIn(b"data-tailscale-user='alice@example.com'", shown.body)  # type: ignore[attr-defined]
        self.assertIn(b"Tailscale user: alice@example.com", shown.body)  # type: ignore[attr-defined]
        token = self._token(shown)
        launch = re.search(br"name='launch' value='([^']+)'", shown.body).group(1).decode("ascii")
        cookie = shown.getheader("Set-Cookie").split(";")[0]
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "POST",
            "/tasks",
            body=f"csrf={token}&launch={launch}&goal=Hi&project=demo-canvas&runner=grok-build",
            headers={
                "Cookie": cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": f"http://127.0.0.1:{self.port}",
                "Tailscale-User-Login": "Rich",
            },
        )
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 303)
        text = list(self.store.inbox.glob("*.yaml"))[0].read_text(encoding="utf-8")
        self.assertIn("from: web-ui", text)
        self.assertNotIn("from: Rich", text)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "POST",
            "/tasks",
            body="goal=Hi&project=demo-canvas&runner=grok-build",
            headers={"Tailscale-User-Login": "alice@example.com"},
        )
        rejected = conn.getresponse()
        rejected.read()
        conn.close()
        self.assertEqual(rejected.status, 403)

    def test_bot_from_is_not_relabelled(self) -> None:
        write_packet(self.store.inbox, base_packet(id="bot-1"))
        self.watcher.tick()
        page = self._get("/").body.decode("utf-8")  # type: ignore[attr-defined]
        self.assertIn("data-from='Joy Feeder'", page)
        self.assertNotIn("data-from='Rich'", page)
        self.assertNotIn("data-from='web-ui'", page.split("data-human-label")[0] if False else page)
        card = page.split("data-id='bot-1'")[1].split("</article>")[0]
        self.assertIn("Joy Feeder", card)
        self.assertNotIn("web-ui", card)
        self.assertNotIn("Rich", card)


if __name__ == "__main__":
    unittest.main()
