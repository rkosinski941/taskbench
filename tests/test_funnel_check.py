"""Funnel detection without a Tailscale install."""

from __future__ import annotations

import unittest

from taskbench.funnel_check import funnel_is_on


class FunnelCheckTests(unittest.TestCase):
    def test_tailnet_only_text_is_off(self) -> None:
        text = "https://laptop.tailnet.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8765\n"
        self.assertFalse(funnel_is_on(serve_text=text, serve_json="{}", funnel_text="No serve config"))

    def test_funnel_on_text(self) -> None:
        self.assertTrue(funnel_is_on(serve_text="https://laptop.tailnet.ts.net (Funnel on)\n"))
        self.assertTrue(funnel_is_on(funnel_text="Available on the internet\n"))

    def test_allow_funnel_json(self) -> None:
        self.assertFalse(funnel_is_on(serve_json='{"AllowFunnel": {}}'))
        self.assertFalse(funnel_is_on(serve_json='{"Funnel": false}'))
        self.assertTrue(
            funnel_is_on(serve_json='{"AllowFunnel": {"laptop.tailnet.ts.net:443": true}}')
        )

    def test_unreadable_json_fails_closed(self) -> None:
        self.assertTrue(funnel_is_on(serve_json="not-json"))


if __name__ == "__main__":
    unittest.main()
