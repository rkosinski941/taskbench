"""Permission flags and the child-process environment.

Grok Build is pinned to ``--permission-mode dontAsk`` and ``--sandbox strict``.
For ``grok`` those two flags are the first arguments, so a later argument
cannot override them. When the executable is a Python interpreter and the
next token is a script path, that path stays between the interpreter and
the pins (CPython would otherwise treat the pins as its own options).
``--`` (end of options) is rejected. Anything
else that looks like a permission flag is rejected. The child does not
inherit the parent environment: only an explicit allow-list is copied, then
``TASKBENCH_APPROVALS=1`` is set by taskbench itself.

On Windows there is no OS sandbox. The Grok process is still started.
``HOME``, ``USERPROFILE``, and ``GROK_HOME`` are pointed at an empty
taskbench directory recreated before each run. The review names ``HOME`` and
``USERPROFILE`` as the variables that cause ``~/.grok`` and ``~/.claude`` to
be read; ``GROK_HOME`` is pinned to that same directory so the documented
grok-home override cannot point anywhere else.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

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
    """Reject banned and unknown permission flags. Allow only the pinned pair.

    ``--`` is rejected wherever it appears. Flag parsing stops at that token,
    so pins placed after it would be ignored.
    """
    if any(token == "--" for token in args):
        raise PermissionFlagError(
            "refusing end-of-options '--' in args; it would hide later flags"
        )
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


def _without_pin_flags(args: list[str]) -> list[str]:
    """Drop permission-mode and sandbox flags. Their values were already checked."""
    kept: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        name = normalize_flag_name(token)
        if name in {"permission-mode", "sandbox"}:
            if "=" in token:
                index += 1
                continue
            if index + 1 < len(args) and not args[index + 1].startswith("-"):
                index += 2
            else:
                index += 1
            continue
        kept.append(token)
        index += 1
    return kept


def _interpreter_script(executable: str, rest: list[str]) -> list[str]:
    """Keep a script path in front of the pins when argv[0] is Python.

    ``grok`` receives the pins as its first arguments. A Python interpreter
    would treat those same flags as its own and never start the stub, so the
    script path stays between the interpreter and the pins. It is not a flag.
    Every permission flag is still removed and the canonical pins are placed
    before the remaining arguments.
    """
    name = Path(executable).name.lower()
    if name not in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}:
        return []
    if not rest or rest[0].startswith("-"):
        return []
    return [rest.pop(0)]


def ensure_pinned_args(args: list[str]) -> list[str]:
    """Validate flags, then put the pins before every other flag.

    Later copies of the same flags are removed so nothing after the pins can
    override them. ``--`` anywhere in the list is an error. For ``grok`` the
    pins are the first arguments. See ``_interpreter_script`` for the Python
    stub used in tests.
    """
    if not args or not str(args[0]).strip():
        raise PermissionFlagError("refusing empty argv")
    check_permission_args(args)
    rest = list(args[1:])
    script = _interpreter_script(args[0], rest)
    rest = _without_pin_flags(rest)
    return [args[0], *script, "--permission-mode", "dontAsk", "--sandbox", "strict", *rest]


def unexpected_permission(event: dict) -> str | None:
    """Best-effort check of one structured event.

    The real Grok CLI does not print ``TASKBENCH`` permission lines. The
    review mentioned an init ``permissionMode`` and did not include a
    captured line, so unstructured stdout is not parsed. A run that never
    emits one of these events is not proof that the process is in
    ``dontAsk``. When a structured event does report a mode or sandbox,
    anything other than ``dontAsk`` and ``strict`` is fatal.
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


WINDOWS_SANDBOX_BANNER = "No OS sandbox on Windows; only taskbench approvals protect you"

# The review names HOME and USERPROFILE. GROK_HOME is the documented grok
# directory override and is pinned to the same empty directory.
_CONFIG_HOME_VARS = ("HOME", "USERPROFILE", "GROK_HOME")


def windows_sandbox_banner(platform: str | None = None) -> str:
    """The Windows banner, or an empty string on every other platform."""
    plat = os.name if platform is None else platform
    if plat == "nt":
        return WINDOWS_SANDBOX_BANNER
    return ""


def prefix_output(text: str, platform: str | None = None) -> str:
    """Put the Windows sandbox banner on the first line of a run's output."""
    banner = windows_sandbox_banner(platform)
    if not banner:
        return text
    if text.startswith(banner):
        return text
    return banner + "\n" + text


def prepare_grok_config_home(data_dir: Path) -> Path:
    """Recreate ``data_dir/grok-home`` as an empty real directory.

    A symlink is removed and not followed. The directory is checked empty
    before it is handed to the child.
    """
    home = Path(data_dir) / "grok-home"
    home.parent.mkdir(parents=True, exist_ok=True)
    if home.is_symlink():
        home.unlink()
    elif home.exists():
        if home.is_dir():
            shutil.rmtree(home)
        else:
            home.unlink()
    home.mkdir(parents=False, exist_ok=False)
    if any(home.iterdir()):
        raise PermissionFlagError("grok config home was not empty after recreate")
    return home


def apply_config_home(env: dict[str, str], home: Path) -> dict[str, str]:
    """Point config-dir variables at ``home``. Other allow-listed values stay."""
    pinned = str(home)
    updated = dict(env)
    for name in _CONFIG_HOME_VARS:
        updated[name] = pinned
    drive, tail = os.path.splitdrive(pinned)
    if drive:
        updated["HOMEDRIVE"] = drive
        updated["HOMEPATH"] = tail or "\\"
    return updated


# Project files the review names. User-level copies live under HOME, which
# Windows runs replace with the empty config home above.
_PROJECT_SETTINGS = {
    (".grok", "config.toml"),
    (".claude", "settings.json"),
    (".claude", "settings.local.json"),
}

_RULE_CANONICAL = {
    "allow": "allow",
    "yolo": "yolo",
    "permission_mode": "permission_mode",
    "default_mode": "defaultMode",
    "defaultmode": "defaultMode",
    "always_approve": "always-approve",
    "auto_approve": "auto-approve",
    "bypass_permissions": "bypassPermissions",
    "bypasspermissions": "bypassPermissions",
    "dangerously_skip_permissions": "dangerously-skip-permissions",
}


def _canonical_rule_key(key: str) -> str | None:
    norm = str(key).strip().lower().replace("-", "_")
    if norm == "defaultmode":
        norm = "default_mode"
    return _RULE_CANONICAL.get(norm)


def _nonempty_allow(value: object) -> bool:
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return bool(value)


def _truthy_switch(value: object) -> bool:
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"", "false", "off", "no", "0", "none"}
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return bool(value)


def _mode_is_problem(value: object) -> bool:
    if value is None:
        return False
    text = str(value).strip().lower().replace("-", "").replace("_", "")
    if not text:
        return False
    return text != "dontask"


def _key_is_problem(key: str, value: object) -> bool:
    canon = _canonical_rule_key(key)
    if canon is None:
        return False
    if canon == "allow":
        return _nonempty_allow(value)
    if canon in {"permission_mode", "defaultMode"}:
        return _mode_is_problem(value)
    return _truthy_switch(value)


def _json_problem_keys(data: object, found: list[str]) -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            if _key_is_problem(str(key), value):
                canon = _canonical_rule_key(str(key))
                if canon and canon not in found:
                    found.append(canon)
            if isinstance(value, (dict, list)):
                _json_problem_keys(value, found)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                _json_problem_keys(item, found)


def _strip_toml_comment(line: str) -> str:
    out: list[str] = []
    quote = ""
    for char in line:
        if quote:
            out.append(char)
            if char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
            out.append(char)
            continue
        if char == "#":
            break
        out.append(char)
    return "".join(out)


_ASSIGN = re.compile(r"^([A-Za-z0-9_.-]+)\s*=\s*(.*)$")
_UNPARSED = object()


def _parse_loose_value(value: str) -> object:
    text = value.strip().rstrip(",").strip()
    if not text:
        return _UNPARSED
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [inner]
    if text.startswith("{") and text.endswith("}"):
        inner = text[1:-1].strip()
        return {} if not inner else {"_": inner}
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return _UNPARSED


def _toml_problem_keys(text: str) -> list[str] | None:
    """Problem keys, or None when a relevant assignment cannot be read."""
    found: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        code = _strip_toml_comment(lines[index]).strip()
        index += 1
        if not code or code.startswith("["):
            continue
        match = _ASSIGN.match(code)
        if not match:
            continue
        key = match.group(1).split(".")[-1]
        value_text = match.group(2).strip()
        if value_text.startswith("[") and value_text.count("[") > value_text.count("]"):
            while index < len(lines) and value_text.count("[") > value_text.count("]"):
                value_text += _strip_toml_comment(lines[index]).strip()
                index += 1
        if _canonical_rule_key(key) is None:
            continue
        parsed = _parse_loose_value(value_text)
        if parsed is _UNPARSED:
            return None
        if _key_is_problem(key, parsed):
            canon = _canonical_rule_key(key)
            if canon and canon not in found:
                found.append(canon)
    return found


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def scan_project_permission_rules(project: Path) -> list[tuple[str, tuple[str, ...]]]:
    """Find project settings that would auto-approve tools.

    Returns ``(relative path, rule keys)``. Values are not returned. A
    symlink that resolves outside the project is named and not read.
    Unreadable or unparsed settings files fail closed. Deny-only files and
    empty allow lists are not findings.
    """
    root = Path(project).resolve()
    if not root.is_dir():
        return []
    findings: list[tuple[str, tuple[str, ...]]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        # Do not descend through a directory symlink.
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for filename in filenames:
            if (current.name, filename) not in _PROJECT_SETTINGS:
                continue
            path = current / filename
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                try:
                    target = path.resolve()
                except OSError:
                    findings.append((relative, ("unreadable",)))
                    continue
                if not _inside(target, root):
                    findings.append((relative, ("symlink-outside-project",)))
                    continue
                path = target
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                findings.append((relative, ("unreadable",)))
                continue
            if filename.endswith(".json"):
                try:
                    data = json.loads(text) if text.strip() else {}
                except json.JSONDecodeError:
                    findings.append((relative, ("unparsed",)))
                    continue
                keys: list[str] = []
                _json_problem_keys(data, keys)
            else:
                keys_or_none = _toml_problem_keys(text)
                if keys_or_none is None:
                    findings.append((relative, ("unparsed",)))
                    continue
                keys = keys_or_none
            if keys:
                findings.append((relative, tuple(keys)))
    return findings


def needs_you_message(findings: list[tuple[str, tuple[str, ...]]]) -> str:
    """Name the files and rule keys. Do not include rule values."""
    parts = [f"{rel} [{', '.join(keys)}]" for rel, keys in findings]
    return (
        "Needs you: project settings contain auto-approve or allow rules; "
        "remove them before this task can run: " + "; ".join(parts)
    )
