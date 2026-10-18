"""Decide whether Tailscale Funnel is on.

The PowerShell helper in ``scripts/install.ps1`` uses the same rules. Funnel
publishes a port to the public internet. taskbench must refuse that. When the
status cannot be read, the result is "on" so the helper fails closed.

Text is checked for ``Funnel on`` and ``Available on the internet``. JSON is
checked for a non-empty ``Funnel`` value, ``AllowFunnel`` entries that are
true, or any string ``(Funnel on)``. Empty objects, ``false``, and ``No serve
config`` mean Funnel is off.
"""

from __future__ import annotations

import json
import re
from typing import Any


def proxies_port(text: str, port: int) -> bool:
    """True when ``text`` names ``127.0.0.1:<port>`` and not a longer port."""
    return re.search(rf"127\.0\.0\.1:{int(port)}(?!\d)", text) is not None


_NOT_LOGGED_IN = re.compile(
    r"logged out|not logged in|needs login|needslogin|"
    r"tailscale is stopped|tailscaled is not running|"
    r"failed to connect to local tailscale|no state file",
    re.IGNORECASE,
)


def funnel_plan(
    serve_text: str = "",
    serve_json: str = "",
    funnel_text: str = "",
    port: int = 8765,
) -> dict[str, object]:
    """Decide what the installer may do about Funnel.

    ``off`` means Funnel is not on. ``on-scoped`` means a single Funnel
    marker is tied to ``127.0.0.1:<port>``; the only command is
    ``tailscale funnel --https=443 off``. ``unrelated`` means Funnel is on
    for something else, or more than once, and must be left alone.
    ``not-logged-in`` and ``unreadable`` fail closed without calling Funnel on.
    """
    combined = "\n".join(part for part in (serve_text, funnel_text, serve_json) if part)
    if _NOT_LOGGED_IN.search(combined):
        return {"kind": "not-logged-in", "command": None}
    data: object | None = None
    if serve_json and serve_json.strip():
        try:
            data = json.loads(serve_json)
        except json.JSONDecodeError:
            return {"kind": "unreadable", "command": None}
    markers = len(re.findall(r"funnel on", combined, re.IGNORECASE))
    json_count = _json_funnel_count(data) if data is not None else 0
    count = max(markers, json_count)
    on = count > 0 or funnel_is_on(
        serve_text=serve_text,
        serve_json="" if data is None else serve_json,
        funnel_text=funnel_text,
    )
    if not on:
        return {"kind": "off", "command": None}
    if count > 1 or not _funnel_targets_port(serve_text, funnel_text, data, port):
        return {"kind": "unrelated", "command": None}
    return {
        "kind": "on-scoped",
        "command": ["tailscale", "funnel", "--https=443", "off"],
    }


def _funnel_targets_port(
    serve_text: str,
    funnel_text: str,
    data: object | None,
    port: int,
) -> bool:
    lines = f"{serve_text}\n{funnel_text}".splitlines()
    for index, line in enumerate(lines):
        if not re.search(r"funnel", line, re.IGNORECASE):
            continue
        window = "\n".join(lines[max(0, index - 1) : index + 2])
        if proxies_port(window, port):
            return True
    return _json_targets_port(data, port)


def _json_funnel_count(data: object) -> int:
    return _walk_funnel(data, count_only=True)


def _json_targets_port(data: object | None, port: int) -> bool:
    if data is None:
        return False
    return _walk_funnel(data, count_only=False, port=port) > 0


def _walk_funnel(data: object, *, count_only: bool, port: int = 0) -> int:
    total = 0
    if isinstance(data, dict):
        for key, value in data.items():
            key_text = str(key)
            interesting = "funnel" in key_text.lower() or key_text in {"AllowFunnel", "Funnel"}
            if interesting and _truthy_funnel(value):
                if count_only:
                    total += _count_truthy(value)
                elif proxies_port(key_text, port) or proxies_port(_flatten_text(value), port):
                    total += 1
            if isinstance(value, (dict, list)):
                total += _walk_funnel(value, count_only=count_only, port=port)
    elif isinstance(data, list):
        for item in data:
            total += _walk_funnel(item, count_only=count_only, port=port)
    return total


def _count_truthy(value: object) -> int:
    if isinstance(value, dict):
        if not value:
            return 0
        return sum(_count_truthy(item) for item in value.values()) or 1
    if isinstance(value, list):
        return sum(_count_truthy(item) for item in value) or (1 if value else 0)
    return 1 if _truthy_funnel(value) else 0


def _flatten_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(f"{key} {_flatten_text(item)}" for key, item in value.items())
    if isinstance(value, list):
        return " ".join(_flatten_text(item) for item in value)
    return ""


def funnel_is_on(serve_text: str = "", serve_json: str = "", funnel_text: str = "") -> bool:
    combined = "\n".join(part for part in (serve_text, funnel_text) if part)
    if re.search(r"funnel on|available on the internet", combined, re.IGNORECASE):
        return True
    if serve_json and serve_json.strip():
        try:
            data = json.loads(serve_json)
        except json.JSONDecodeError:
            return True
        if _json_funnel(data):
            return True
    return False


def _json_funnel(data: Any) -> bool:
    if isinstance(data, dict):
        if "AllowFunnel" in data and _truthy_funnel(data.get("AllowFunnel")):
            return True
        if "Funnel" in data and _truthy_funnel(data.get("Funnel")):
            return True
        for key, value in data.items():
            if "funnel" in str(key).lower() and _truthy_funnel(value):
                return True
            if isinstance(value, (dict, list)) and _json_funnel(value):
                return True
    elif isinstance(data, list):
        return any(_json_funnel(item) for item in data)
    return False


def _truthy_funnel(value: Any) -> bool:
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"", "false", "off", "none"}
    if isinstance(value, dict):
        if not value:
            return False
        return any(_truthy_funnel(item) for item in value.values())
    if isinstance(value, list):
        return any(_truthy_funnel(item) for item in value)
    return bool(value)
