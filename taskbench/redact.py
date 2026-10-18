"""Redact secrets before they are written where a bot can read them.

Exact values (an ``auth.json`` field, ``XAI_API_KEY``, the one-time login
token, the session secret, the CSRF token) are replaced when they are at
least six characters. Token-shaped strings are replaced as well. The raw
runner stream stays under ``runtime/`` and is still passed through this
filter so a secret is not left on disk.
"""

from __future__ import annotations

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


def redact(text: str) -> str:
    """Replace known secrets and token-shaped strings with ``[redacted]``."""
    if not text:
        return text
    redacted = text
    for secret in sorted(_EXACT, key=len, reverse=True):
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "[redacted]")
    return _TOKEN_RE.sub("[redacted]", redacted)


def drop_from_env(env: dict[str, str]) -> dict[str, str]:
    """Drop any variable whose name or value contains a remembered secret."""
    cleaned: dict[str, str] = {}
    for key, value in env.items():
        if any(secret in key or secret in value for secret in _EXACT):
            continue
        cleaned[key] = value
    return cleaned
