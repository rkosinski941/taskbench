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
