"""Permission flags and the child-process environment.

Grok Build is pinned to ``--permission-mode dontAsk`` and ``--sandbox strict``.
Anything else that looks like a permission flag is rejected. The child does
not inherit the parent environment: only an explicit allow-list is copied,
then ``TASKBENCH_APPROVALS=1`` is set by taskbench itself.
"""

from __future__ import annotations

ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "SYSTEMDRIVE",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USER",
    "USERNAME",
    "LOGNAME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TZ",
    "TERM",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)

# Canonical flag -> allowed values. Comparison is case-insensitive.
ALLOWED_PERMISSION_VALUES = {
    "permission-mode": frozenset({"dontask"}),
    "sandbox": frozenset({"strict"}),
}

# Names that are permission flags even without a marker substring.
_EXTRA_PERMISSION_NAMES = frozenset(
    {
        "yolo",
        "allow",
        "deny",
        "sandbox",
        "auto-approve",
        "always-approve",
        "permission-mode",
    }
)

_MARKERS = ("permission", "approve", "allow", "sandbox", "yolo", "bypass")

# Short names and Claude-compatible aliases, folded onto one spelling.
_ALIASES = {
    "yolo": "always-approve",
    "allowedtools": "allow",
    "allowed-tools": "allow",
    "disallowedtools": "disallowed-tools",
    "disallowed-tools": "disallowed-tools",
    "dangerously-skip-permissions": "dangerously-skip-permissions",
    "bypasspermissions": "bypass-permissions",
    "autoapprove": "auto-approve",
    "alwaysapprove": "always-approve",
}

BANNED_CONFIG_KEYS = frozenset(
    {
        "yolo",
        "bypass",
        "allow",
        "deny",
        "always-approve",
        "auto-approve",
        "permission-mode",
        "bypass-permissions",
        "dangerously-skip-permissions",
        "approve-all",
        "auto-approve-all",
    }
)


class PermissionFlagError(ValueError):
    """A permission flag is banned or not on the allow-list."""


def scrub_environment(base: dict[str, str]) -> dict[str, str]:
    """Copy only allow-listed variables. Never pass ``PYTHONPATH`` or ``GROK_*``."""
    allowed = {name.upper() for name in ENV_ALLOWLIST}
    env: dict[str, str] = {}
    for key, value in base.items():
        if key.upper() in allowed:
            env[key] = value
    env["TASKBENCH_APPROVALS"] = "1"
    return env


def normalize_flag_name(token: str) -> str | None:
    if not token.startswith("-"):
        return None
    body = token[2:] if token.startswith("--") else token[1:]
    if not body:
        return None
    name = body.split("=", 1)[0]
    name = name.replace("_", "-").lower()
    return _ALIASES.get(name, name)


def is_permission_flag(name: str) -> bool:
    if name in _EXTRA_PERMISSION_NAMES or name in ALLOWED_PERMISSION_VALUES:
        return True
    return any(marker in name for marker in _MARKERS)


def iter_flags(args: list[str]) -> list[tuple[str, str | None]]:
    """Return ``(name, value)`` for each flag. A value may be absent."""
    found: list[tuple[str, str | None]] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            break
        name = normalize_flag_name(token)
        if name is None:
            index += 1
            continue
        if "=" in token:
            found.append((name, token.split("=", 1)[1]))
            index += 1
            continue
        value = None
        if index + 1 < len(args) and not args[index + 1].startswith("-"):
            value = args[index + 1]
            index += 2
        else:
            index += 1
        found.append((name, value))
    return found


def check_permission_args(args: list[str]) -> None:
    """Reject banned and unknown permission flags. Allow only the pinned pair."""
    for name, value in iter_flags(args):
        if not is_permission_flag(name):
            continue
        allowed = ALLOWED_PERMISSION_VALUES.get(name)
        if allowed is None:
            raise PermissionFlagError(f"refusing permission flag '--{name}'")
        if value is None or value.strip().lower().replace("-", "") not in allowed:
            shown = value if value is not None else "(missing)"
            raise PermissionFlagError(
                f"refusing permission flag '--{name}' value {shown}; "
                f"only {sorted(allowed)} is allowed"
            )


def ensure_pinned_args(args: list[str]) -> list[str]:
    """Validate flags, then append ``dontAsk`` and ``sandbox strict`` if absent."""
    check_permission_args(args)
    flags = iter_flags(args)
    has_mode = any(name == "permission-mode" for name, _value in flags)
    has_sandbox = any(name == "sandbox" for name, _value in flags)
    pinned = list(args)
    if not has_mode:
        pinned.extend(["--permission-mode", "dontAsk"])
    if not has_sandbox:
        pinned.extend(["--sandbox", "strict"])
    return pinned


def unexpected_permission(event: dict) -> str | None:
    """Return a failure reason when a runner reports a mode other than the pin.

    Events that do not mention a permission mode or sandbox are ignored.
    """
    kind = str(event.get("type") or "")
    mode = event.get("permission_mode", event.get("permissionMode"))
    if mode is None and kind in {"permission_mode", "permission-mode", "permissions"}:
        mode = event.get("mode")
    sandbox = event.get("sandbox")
    if kind in {"sandbox"} and sandbox is None:
        sandbox = event.get("mode")
    reports_mode = kind in {"permission_mode", "permission-mode", "permissions", "sandbox", "status"}
    if not reports_mode and mode is None and sandbox is None:
        return None
    if mode is None and sandbox is None:
        return None
    if mode is not None and str(mode).strip().lower().replace("-", "") != "dontask":
        return (
            f"unexpected permission mode: {mode} "
            "(expected dontAsk with --sandbox strict); refusing to continue"
        )
    if sandbox is not None and str(sandbox).strip().lower() != "strict":
        return (
            f"unexpected sandbox: {sandbox} "
            "(expected strict with --permission-mode dontAsk); refusing to continue"
        )
    return None
