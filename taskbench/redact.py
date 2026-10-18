"""Redact secrets before they are written where a bot can read them.

Exact values (an ``auth.json`` field, ``XAI_API_KEY``, the one-time login
token, the session secret, the CSRF token) are replaced when they are at
least six characters. The match ignores case and zero-width characters, and
it also removes base64 (and url-safe base64) of those known values. Token-shaped
strings are replaced as well.

This is best-effort defense in depth, not a boundary. A backslash-u escape,
a secret split across lines, or a token taskbench never saw (including one
Grok refreshes) can still pass. API-key-only mode and the deny-read rules
are the boundary. The raw runner stream stays under ``runtime/`` and is
still passed through this filter.
"""

from __future__ import annotations

import base64
import re

_EXACT: list[str] = []

# Long enough that ordinary words and short test fixtures are left alone.
# Exact secrets of length >= 6 are removed by ``remember_secret`` instead.
_TOKEN_RE = re.compile(
    r"(?:"
    r"xai-[A-Za-z0-9_-]{16,}"
    r"|sk-(?:ant-)?[A-Za-z0-9_-]{16,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")"
)


def remember_secret(value: str) -> None:
    """Remember an exact secret so later writes can remove it."""
    if not isinstance(value, str):
        return
    text = value.strip()
    if len(text) < 6 or text in _EXACT:
        return
    _EXACT.append(text)


def remember_json_secrets(value: object) -> None:
    """Remember string leaves from a credential document."""
    if isinstance(value, str):
        remember_secret(value)
        return
    if isinstance(value, dict):
        for item in value.values():
            remember_json_secrets(item)
        return
    if isinstance(value, list):
        for item in value:
            remember_json_secrets(item)


# Characters used to hide a secret inside an otherwise exact copy.
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u180e\u200e\u200f"), None)
_MARK = "\ue000REDACTED\ue001"


def redact(text: str) -> str:
    """Replace known secrets and token-shaped strings with ``[redacted]``.

    Best-effort only. See the module docstring for what this does not catch.
    """
    if not text:
        return text
    redacted = text.translate(_ZERO_WIDTH)
    for secret in sorted(_EXACT, key=len, reverse=True):
        if len(secret) < 6:
            continue
        redacted = _replace_ci(redacted, secret)
        for encoded in _b64_forms(secret):
            redacted = _replace_ci(redacted, encoded)
    redacted = _TOKEN_RE.sub(_MARK, redacted)
    return redacted.replace(_MARK, "[redacted]")


def _replace_ci(text: str, needle: str) -> str:
    if len(needle) < 6:
        return text
    return re.sub(re.escape(needle), _MARK, text, flags=re.IGNORECASE)


def _b64_forms(secret: str) -> list[str]:
    """Base64 spellings of a known secret. Shorter than 8 characters is ignored."""
    raw = secret.encode("utf-8")
    forms: list[str] = []
    for encoded in (
        base64.b64encode(raw).decode("ascii"),
        base64.urlsafe_b64encode(raw).decode("ascii"),
    ):
        for item in (encoded, encoded.rstrip("=")):
            if len(item) >= 8 and item not in forms:
                forms.append(item)
    return forms


def drop_from_env(env: dict[str, str]) -> dict[str, str]:
    """Drop any variable whose name or value contains a remembered secret."""
    cleaned: dict[str, str] = {}
    for key, value in env.items():
        if any(secret in key or secret in value for secret in _EXACT):
            continue
        cleaned[key] = value
    return cleaned
