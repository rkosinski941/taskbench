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

On every OS the Grok process gets ``GROK_HOME`` pointed at an empty
taskbench directory recreated before each run. On Windows, ``HOME`` and
``USERPROFILE`` are pointed at that same directory (there is no OS sandbox).
On Linux and macOS those two stay as the real profile so ``--sandbox strict``
still applies. API-key-only is the default: ``auth.json`` is copied only when
``copy_auth_json`` is set. Deny-read rules in the pinned home name that
directory and the real profile's ``.grok`` and ``.claude``. The same TOML
turns ``[compat.claude]`` and ``[compat.cursor]`` hooks and MCP off, and the
Grok process gets those four scanner variables set to ``0``.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import sys
import tomllib
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


# Grok's Claude and Cursor scanners default to on. A repo can then start MCP
# servers, plugins, or hooks without a tool call for taskbench to deny.
# https://docs.x.ai/build/settings/reference lists these four variables.
GROK_SCANNER_DISABLES = {
    "GROK_CLAUDE_HOOKS_ENABLED": "0",
    "GROK_CURSOR_HOOKS_ENABLED": "0",
    "GROK_CLAUDE_MCPS_ENABLED": "0",
    "GROK_CURSOR_MCPS_ENABLED": "0",
}


def scrub_environment(base: dict[str, str]) -> dict[str, str]:
    """Copy only allow-listed variables. Never pass ``PYTHONPATH`` or ``GROK_*``.

    ``XAI_API_KEY`` is not on this list. The guarded Grok launch adds it to
    that one process. Companion dev servers stay on this allow-list.
    ``apply_grok_scanner_locks`` sets the four scanner switches after this
    scrub, and only on the Grok process. Parent ``GROK_*`` values are not copied.
    """
    from taskbench.redact import drop_from_env

    allowed = {name.upper() for name in ENV_ALLOWLIST}
    env: dict[str, str] = {}
    for key, value in base.items():
        if key.upper() in allowed:
            env[key] = value
    env["TASKBENCH_APPROVALS"] = "1"
    return drop_from_env(env)


def apply_grok_scanner_locks(env: dict[str, str]) -> dict[str, str]:
    """Force the Claude and Cursor hook and MCP scanners off.

    Call this on the filtered Grok environment, after ``scrub_environment``.
    A parent value of ``1`` does not survive the scrub, and this overwrites
    the four names so the child cannot inherit them.
    """
    locked = dict(env)
    locked.update(GROK_SCANNER_DISABLES)
    return locked


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


def banned_permission_in_text(text: str) -> str | None:
    """Refuse banned permission flags anywhere in a command a run wants to execute.

    The check tokenizes the whole string, including the payload of
    ``sh -c``, ``cmd /c``, and ``powershell -c``. A clean command is not
    approved by this function; callers still require an approval or an
    allow-list match. Caret escapes and encoded payloads are reported by
    ``command_warnings`` instead of being treated as a clean command.
    """
    if not text:
        return None
    try:
        check_permission_args(_flatten_command(text))
    except PermissionFlagError as exc:
        return str(exc)
    return None


def command_warnings(text: str) -> list[str]:
    """Warnings for a command that still needs a human approval.

    These are not a clean allow-list match. A plain banned flag is still a
    refusal from ``banned_permission_in_text``. A hidden one is named here
    so the approval prompt can show it.
    """
    if not text:
        return []
    return _command_warnings(text, depth=0)


def command_warning_text(text: str) -> str:
    """One sentence for the approval prompt, or an empty string."""
    return " ".join(command_warnings(text))


# GROK_* variables that change sandbox or tool approval. GROK_YOLO is included
# even though the public env table does not list it: an inline assignment
# would bypass the pins if it were honored.
_PERMISSION_ENV = re.compile(
    r"(?i)(?:^|[\s;&|`$])(?:\$env:)?GROK_"
    r"(?:YOLO|PERMISSION[A-Z0-9_]*|SANDBOX[A-Z0-9_]*|ALWAYS_APPROVE|AUTO_APPROVE|"
    r"BYPASS[A-Z0-9_]*|DEFAULT_SELECTED_PERMISSION|REMEMBER_TOOL_APPROVALS|"
    r"SANDBOX_AUTO_ALLOW_BASH)"
    r"\b"
)
_CARET_HIDDEN = re.compile(r"\^[^\s\r\n]")
_EXPANSION = re.compile(
    r"(?i)(?:\$[A-Za-z_{]|(?<![\w])@[A-Za-z_][A-Za-z0-9_]*|%[^%\s]+%|![^!\s]+!)"
)
_ENCODED = re.compile(
    r"(?i)(?:powershell|pwsh)(?:\.exe)?\b[\s\S]{0,240}?"
    r"-(?:encodedcommand|enc)\b(?:\s+|=|:)?[\"']?([A-Za-z0-9+/=]+)"
)


def _command_warnings(text: str, depth: int) -> list[str]:
    found: list[str] = []
    stripped = _strip_cmd_carets(text)
    if _CARET_HIDDEN.search(text):
        found.append("caret escape can hide the command or a permission flag")
        banned = banned_permission_in_text(stripped)
        if banned:
            found.append(f"after removing carets: {banned}")
    if depth == 0 and _ENCODED.search(text):
        found.append("PowerShell -EncodedCommand hides the command")
        for decoded in _decode_powershell_encoded(text):
            banned = banned_permission_in_text(decoded)
            if banned:
                found.append(f"decoded command: {banned}")
            found.extend(_command_warnings(decoded, depth + 1))
    if _EXPANSION.search(text):
        found.append(
            "variable expansion or @args splatting is unknown and needs approval"
        )
    if _PERMISSION_ENV.search(text) or _PERMISSION_ENV.search(stripped):
        found.append("inline GROK_* variable can change Grok permissions")
    if _grok_config_override(stripped):
        found.append("command points Grok at another config")
    unique: list[str] = []
    for item in found:
        if item not in unique:
            unique.append(item)
    return unique


def _strip_cmd_carets(text: str) -> str:
    """Remove cmd.exe ``^`` escapes. ``^^`` becomes one caret."""
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "^" and index + 1 < len(text) and text[index + 1] not in "\r\n":
            out.append(text[index + 1])
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _decode_powershell_encoded(text: str) -> list[str]:
    decoded: list[str] = []
    for match in _ENCODED.finditer(text):
        blob = match.group(1)
        try:
            raw = base64.b64decode(blob, validate=True)
            text_out = raw.decode("utf-16-le", errors="replace")
        except (ValueError, UnicodeError):
            continue
        if text_out and text_out not in decoded:
            decoded.append(text_out)
    return decoded


def _grok_config_override(text: str) -> bool:
    """True when a grok executable is passed ``-c`` or ``--config``."""
    flat = _flatten_command(text)
    names = {Path(part).name.lower() for part in flat}
    if not names.intersection({"grok", "grok.exe", "grok.cmd"}):
        return False
    return any(name in {"c", "config"} for name, _value in iter_flags(flat))


def _flatten_command(text: str, depth: int = 0) -> list[str]:
    """Split a shell command and the quoted payloads inside it."""
    if depth > 6:
        return text.split()
    try:
        parts = shlex.split(text, posix=True)
    except ValueError:
        parts = text.split()
    flat: list[str] = []
    for part in parts:
        if part != text and any(ch.isspace() for ch in part):
            flat.extend(_flatten_command(part, depth + 1))
        else:
            flat.append(part)
    # A payload that shlex kept as one word can still contain `--flag value`.
    if len(flat) == 1 and flat[0] == text and _FLAG_WORD.search(text):
        return text.split()
    return flat


_FLAG_WORD = re.compile(r"(?:^|\s)--?[A-Za-z]")


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
    """Drop permission-mode, sandbox, and output-format flags."""
    kept: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        name = normalize_flag_name(token)
        if name in {"permission-mode", "sandbox", "output-format"}:
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
    return [
        args[0],
        *script,
        "--permission-mode",
        "dontAsk",
        "--sandbox",
        "strict",
        "--output-format",
        "streaming-messages-json",
        *rest,
    ]


def unexpected_permission(event: dict) -> str | None:
    """Reject a structured event whose mode or sandbox is not the pin.

    The required check is the ``streaming-messages-json`` init event. The
    watcher reads ``permissionMode`` there and fails the run when it is
    missing or not ``dontAsk``. This helper covers a later structured event.
    Ordinary stdout is not parsed for a mode.
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


def seed_grok_credentials(
    home: Path,
    base_env: dict[str, str],
    *,
    copy_auth_json: bool = False,
) -> None:
    """Copy ``auth.json`` only when the owner opted in.

    API-key-only is the default on every OS. A session token in ``auth.json``
    outranks ``XAI_API_KEY``, so the copy is off unless ``copy_auth_json`` is
    true. Settings files are never copied. The file is created inside a home
    directory that is already owner-only.
    """
    if not copy_auth_json:
        return
    source = _credential_source(base_env)
    if source is None or not source.is_file():
        return
    target = Path(home) / "auth.json"
    _write_private(target, source.read_bytes())
    _remember_auth_file(target)


def _credential_source(base_env: dict[str, str]) -> Path | None:
    grok_home = (base_env.get("GROK_HOME") or "").strip()
    candidates: list[Path] = []
    if grok_home:
        candidates.append(Path(grok_home) / "auth.json")
    for key in ("USERPROFILE", "HOME"):
        value = (base_env.get(key) or "").strip()
        if value:
            candidates.append(Path(value) / ".grok" / "auth.json")
    for path in candidates:
        if path.is_file():
            return path
    return None


def lock_owner_only(path: Path) -> None:
    """Owner-only permissions. POSIX is ``0600``. Windows is an ACL for this user.

    POSIX creates the file with mode ``0600`` (there is no chmod-after-write
    step). Windows writes it only after the parent directory's DACL is already
    limited to the process token's SID, then sets the file ACE with ``icacls``
    from ``GetSystemDirectoryW`` to read, write, and delete for that SID.
    The SID comes from the process token, not from ``USERNAME``. The
    directory lock is the boundary the stdlib can apply before the file
    exists; a security descriptor on the create call is not available.
    """
    if os.name == "nt":
        _windows_owner_only(path)
        return
    if not path.exists():
        _write_private(path, b"")
        return
    os.chmod(path, 0o600)
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        raise PermissionFlagError(f"auth.json mode is {mode:o}, expected 600")


def _write_private(path: Path, data: bytes) -> None:
    """Create or replace ``path`` with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # The parent is locked before the bytes exist. The file ACE is applied
        # after the write; the stdlib has no create-time security descriptor.
        _windows_lock_directory(path.parent)
        path.write_bytes(data)
        _windows_owner_only(path)
        return
    if path.exists():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        raise PermissionFlagError(f"auth.json mode is {mode:o}, expected 600")


def _windows_owner_only(path: Path) -> None:
    import subprocess

    icacls = _icacls_path()
    sid = _current_user_sid()
    completed = subprocess.run(
        [icacls, str(path), "/inheritance:r", "/grant:r", f"*{sid}:(R,W,D)"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise PermissionFlagError("could not lock auth.json to the current user")


def _windows_lock_directory(directory: Path) -> None:
    """Limit a directory to the current user's SID before any file is created."""
    import subprocess

    icacls = _icacls_path()
    sid = _current_user_sid()
    completed = subprocess.run(
        [icacls, str(directory), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)(F)"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise PermissionFlagError("could not lock the grok home to the current user")


def _icacls_path() -> str:
    """Absolute ``icacls.exe`` from ``GetSystemDirectoryW``, not ``PATH``."""
    system = _system_directory()
    path = str(Path(system) / "icacls.exe")
    if not os.path.isfile(path):
        raise PermissionFlagError("icacls.exe was not found in the system directory")
    return path


def _system_directory() -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel32.GetSystemDirectoryW.restype = wintypes.UINT
    buf = ctypes.create_unicode_buffer(260)
    written = kernel32.GetSystemDirectoryW(buf, len(buf))
    if not written or written >= len(buf):
        raise PermissionFlagError("GetSystemDirectoryW failed")
    return buf.value


def _current_user_sid() -> str:
    """SID of the process token. Not ``USERNAME`` and not an environment variable."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    class _SidAndAttrs(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("User", _SidAndAttrs)]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise PermissionFlagError("cannot read the process token")
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buf = ctypes.create_string_buffer(max(needed.value, 1))
        if not advapi32.GetTokenInformation(token, 1, buf, len(buf), ctypes.byref(needed)):
            raise PermissionFlagError("cannot read the process token user")
        user = ctypes.cast(buf, ctypes.POINTER(_TokenUser)).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(user.User.Sid), ctypes.byref(sid_text)):
            raise PermissionFlagError("cannot convert the user SID")
        try:
            value = sid_text.value
            if not value:
                raise PermissionFlagError("cannot convert the user SID")
            return value
        finally:
            kernel32.LocalFree(ctypes.cast(sid_text, wintypes.HLOCAL))
    finally:
        kernel32.CloseHandle(token)


def _remember_auth_file(path: Path) -> None:
    from taskbench.redact import remember_json_secrets, remember_secret

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        try:
            remember_secret(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return
        return
    remember_json_secrets(payload)


def wipe_grok_credentials(data_dir: Path) -> None:
    """Delete a copied ``auth.json``. Missing files are fine."""
    auth = Path(data_dir) / "grok-home" / "auth.json"
    try:
        auth.unlink(missing_ok=True)
    except OSError:
        return


def prepare_grok_config_home(data_dir: Path) -> Path:
    """Recreate ``data_dir/grok-home`` and lock it to the current user.

    A symlink is removed and not followed. The directory is checked empty,
    then locked, before any file is written into it. POSIX uses mode
    ``0700``. Windows calls ``icacls`` from ``GetSystemDirectoryW`` and grants
    only the process token's SID. The stdlib cannot attach a security
    descriptor at ``mkdir`` time, so there is a short gap before ``icacls``.
    Credential files are created only after this lock.
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
    _lock_home_dir(home)
    return home


def _lock_home_dir(home: Path) -> None:
    if os.name == "nt":
        _windows_lock_directory(home)
        return
    os.chmod(home, 0o700)
    mode = home.stat().st_mode & 0o777
    if mode != 0o700:
        raise PermissionFlagError(f"grok home mode is {mode:o}, expected 700")


def write_grok_deny_rules(home: Path, base_env: dict[str, str]) -> None:
    """Write deny-read rules into the pinned home, not into the project.

    Grok's own read tools are not a taskbench approval. The rules name the
    pinned home and the real profile's ``.grok`` and ``.claude`` (from
    ``HOME`` and ``USERPROFILE`` before those variables are replaced). They
    do not set ``defaultMode`` and they do not copy the user's allow list.

    The TOML section is ``[permission]`` (singular), which is the key in
    https://docs.x.ai/build/settings/reference . That page also names
    ``hooks`` and ``mcps`` under ``[compat.claude]`` and ``[compat.cursor]``
    (default true). Both are set false here. The Claude-compatible JSON
    file keeps ``permissions``.
    """
    patterns = _read_deny_patterns(_deny_read_targets(home, base_env))
    if not patterns:
        return
    settings = json.dumps({"permissions": {"deny": patterns}}, indent=2).encode("utf-8") + b"\n"
    lines = ["[permission]", "deny = ["]
    lines.extend(f"  {json.dumps(item)}," for item in patterns)
    lines.append("]")
    lines.append("")
    lines.extend(
        [
            "[compat.claude]",
            "hooks = false",
            "mcps = false",
            "",
            "[compat.cursor]",
            "hooks = false",
            "mcps = false",
            "",
        ]
    )
    toml = "\n".join(lines).encode("utf-8")
    _write_private(Path(home) / ".claude" / "settings.json", settings)
    _write_private(Path(home) / ".grok" / "config.toml", toml)
    _write_private(Path(home) / "config.toml", toml)


def _deny_read_targets(home: Path, base_env: dict[str, str]) -> list[str]:
    targets = [str(home)]
    for key in ("HOME", "USERPROFILE"):
        value = (base_env.get(key) or "").strip()
        if not value:
            continue
        root = Path(value)
        targets.append(str(root / ".grok"))
        targets.append(str(root / ".claude"))
    seen: list[str] = []
    for item in targets:
        if item and item not in seen:
            seen.append(item)
    return seen


def _read_deny_patterns(targets: list[str], *, windows_case: bool | None = None) -> list[str]:
    """``Read`` rules for each path, plus a lowercased copy on Windows.

    Windows paths are matched case-insensitively by the filesystem, so the
    lowercased drive and path are written beside the original. Other
    platforms write the path as given.
    """
    if windows_case is None:
        windows_case = os.name == "nt"
    patterns: list[str] = []
    for raw in targets:
        forms = [raw]
        if "\\" in raw:
            forms.append(raw.replace("\\", "/"))
        if windows_case:
            extra = [form.lower() for form in list(forms)]
            for form in extra:
                if form not in forms:
                    forms.append(form)
        for form in forms:
            for pattern in (f"Read({form})", f"Read({form}/**)"):
                if pattern not in patterns:
                    patterns.append(pattern)
    return patterns


def apply_config_home(
    env: dict[str, str],
    home: Path,
    *,
    pin_profile_home: bool = True,
) -> dict[str, str]:
    """Point ``GROK_HOME`` at ``home``.

    ``pin_profile_home`` also replaces ``HOME`` and ``USERPROFILE``. Windows
    launches pass true. Linux and macOS pass false so the real home, and the
    OS sandbox that depends on it, stay in place. The default is true so a
    direct call still pins the profile variables.
    """
    pinned = str(home)
    updated = dict(env)
    updated["GROK_HOME"] = pinned
    if not pin_profile_home:
        return updated
    for name in ("HOME", "USERPROFILE"):
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
    (".claude", "mcp.json"),
    (".claude", "hooks.json"),
    (".cursor", "mcp.json"),
    (".cursor", "hooks.json"),
}

# Files that sit in the directory itself, not inside one of the folders above.
_DIRECT_SETTINGS = (".mcp.json", "mcp.json", "hooks.json")

# A file with this name defines that feature when its body is non-empty.
_FILE_EXEC = {
    "mcp.json": "mcp_servers",
    ".mcp.json": "mcp_servers",
    "hooks.json": "hooks",
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


def _snake_key(key: str) -> str:
    """Casefold a settings key, splitting camelCase without splitting ``YOLO``."""
    chars = str(key).strip().replace("-", "_").replace(" ", "_")
    out: list[str] = []
    for index, char in enumerate(chars):
        prev = chars[index - 1] if index else ""
        nxt = chars[index + 1] if index + 1 < len(chars) else ""
        if (
            char.isupper()
            and prev
            and prev != "_"
            and (prev.islower() or prev.isdigit() or nxt.islower())
        ):
            out.append("_")
        out.append(char.lower())
    return "".join(out)


def _canonical_rule_key(key: str) -> str | None:
    norm = _snake_key(key)
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


def _exec_canon(key: str) -> str | None:
    """MCP, plugin, and hook keys. Matched without case or separator."""
    norm = _snake_key(key)
    if norm in {"mcp_servers", "mcp_server", "mcps"}:
        return "mcp_servers"
    if norm in {"plugins", "plugin"}:
        return "plugins"
    if norm == "hooks":
        return "hooks"
    return None


def _defines_exec(value: object) -> bool:
    """True when a value actually names a server, plugin, or hook.

    ``false`` and empty lists do not. A nested table does: an MCP server
    entry counts even when its own table is empty. A ``disabled = []``
    plugins table does not. An empty ``{}`` does not.
    """
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return len(value) > 0
    if isinstance(value, dict):
        if not value:
            return False
        for item in value.values():
            if isinstance(item, dict):
                return True
            if _defines_exec(item):
                return True
        return False
    return False


def _file_defines(data: object) -> bool:
    """True when a whole ``mcp.json`` or ``hooks.json`` document is non-empty.

    An object whose known list is ``{}`` or ``[]`` does not count. A bare
    JSON array of hook or server entries does.
    """
    if isinstance(data, list):
        return len(data) > 0
    if isinstance(data, dict):
        return any(_defines_exec(item) for item in data.values())
    return False


def _key_is_problem(key: str, value: object) -> bool:
    canon = _canonical_rule_key(key)
    if canon is None:
        return False
    if canon == "allow":
        return _nonempty_allow(value)
    if canon in {"permission_mode", "defaultMode"}:
        return _mode_is_problem(value)
    return _truthy_switch(value)


def _remember(found: list[str], canon: str | None) -> None:
    if canon and canon not in found:
        found.append(canon)


def _json_problem_keys(data: object, found: list[str]) -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            if _key_is_problem(str(key), value):
                _remember(found, _canonical_rule_key(str(key)))
            if _defines_exec(value):
                _remember(found, _exec_canon(str(key)))
            if isinstance(value, (dict, list)):
                _json_problem_keys(value, found)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                _json_problem_keys(item, found)


def _problem_keys_from_data(data: object) -> list[str]:
    keys: list[str] = []
    _json_problem_keys(data, keys)
    return keys


def _read_settings(path: Path, filename: str) -> tuple[list[str] | None, str | None]:
    """Return problem keys, or an error token ``unreadable`` / ``unparsed``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, "unreadable"
    if filename.endswith(".json"):
        try:
            data = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            return None, "unparsed"
    else:
        try:
            data = tomllib.loads(text) if text.strip() else {}
        except tomllib.TOMLDecodeError:
            return None, "unparsed"
    keys = _problem_keys_from_data(data)
    extra = _FILE_EXEC.get(filename.casefold())
    if extra and extra not in keys and _file_defines(data):
        keys.append(extra)
    return keys, None


def _scan_directories(project: Path) -> list[Path]:
    """Workdir plus parents, stopping at the profile or before the drive root."""
    from taskbench.workdir import _is_drive_root, _profile_roots, _same

    profiles = []
    for raw in _profile_roots():
        try:
            profiles.append(str(Path(raw).resolve()))
        except OSError:
            profiles.append(raw)
    plat = "nt" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "posix")
    directories: list[Path] = []
    current = project
    while True:
        directories.append(current)
        if any(_same(str(current), profile, plat) for profile in profiles):
            break
        parent = current.parent
        if parent == current or _is_drive_root(str(parent), plat):
            break
        current = parent
    return directories


def _matching_child(directory: Path, name: str) -> Path | None:
    """Child whose name matches ``name``, ignoring case. Symlinks count."""
    direct = directory / name
    try:
        children = list(directory.iterdir())
    except OSError:
        if direct.is_symlink() or direct.exists():
            return direct
        return None
    folded = name.casefold()
    for child in children:
        if child.name.casefold() == folded:
            return child
    return None


def _record_settings_file(
    path: Path,
    filename: str,
    label: str,
    findings: list[tuple[str, tuple[str, ...]]],
    seen: set[str],
) -> None:
    if label in seen:
        return
    if not path.is_file():
        return
    keys, error = _read_settings(path, filename)
    if error:
        findings.append((label, (error,)))
        seen.add(label)
        return
    if keys:
        findings.append((label, tuple(keys)))
        seen.add(label)


def _consider_settings(
    directory: Path,
    label_prefix: str,
    findings: list[tuple[str, tuple[str, ...]]],
    seen: set[str],
) -> None:
    for folder_name, filename in sorted(_PROJECT_SETTINGS):
        label = f"{label_prefix}{folder_name}/{filename}"
        if label in seen:
            continue
        link = _matching_child(directory, folder_name)
        if link is None:
            continue
        try:
            target_dir = Path(os.path.realpath(link, strict=True))
        except OSError:
            findings.append((label, ("unreadable",)))
            seen.add(label)
            continue
        settings = _matching_child(target_dir, filename)
        if settings is None:
            continue
        try:
            path = Path(os.path.realpath(settings, strict=True))
        except OSError:
            findings.append((label, ("unreadable",)))
            seen.add(label)
            continue
        _record_settings_file(path, filename, label, findings, seen)
    for filename in _DIRECT_SETTINGS:
        label = f"{label_prefix}{filename}"
        if label in seen:
            continue
        link = _matching_child(directory, filename)
        if link is None:
            continue
        try:
            path = Path(os.path.realpath(link, strict=True))
        except OSError:
            findings.append((label, ("unreadable",)))
            seen.add(label)
            continue
        _record_settings_file(path, filename, label, findings, seen)


def scan_project_permission_rules(project: Path) -> list[tuple[str, tuple[str, ...]]]:
    """Find settings that would auto-approve tools or start side programs.

    Walks the workdir, then each parent up to the profile or the drive root,
    whichever comes first. The profile directory is included. The drive root
    is not. ``.grok``, ``.claude``, and ``.cursor`` symlinks and junctions are
    followed to their targets on every OS. Keys and mode values are matched
    case-insensitively. A file that defines MCP servers, plugins, or hooks is
    a finding, the same as an allow rule. Unreadable or unparsed files are
    findings (Needs you), not skips. Values are not returned.
    """
    root = Path(project)
    try:
        root = Path(os.path.realpath(root, strict=True))
    except OSError:
        return [("(unreadable)", ("unreadable",))]
    if not root.is_dir():
        return [("(unreadable)", ("unreadable",))]
    findings: list[tuple[str, tuple[str, ...]]] = []
    seen: set[str] = set()
    for dirpath, dirnames, _filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        relative = current.relative_to(root).as_posix()
        prefix = "" if relative == "." else relative + "/"
        _consider_settings(current, prefix, findings, seen)
    parents = _scan_directories(root)
    for directory in parents[1:]:
        prefix = str(directory).replace("\\", "/").rstrip("/") + "/"
        _consider_settings(directory, prefix, findings, seen)
    return findings


def needs_you_message(findings: list[tuple[str, tuple[str, ...]]]) -> str:
    """Name the files and rule keys. Do not include rule values."""
    parts = [f"{rel} [{', '.join(keys)}]" for rel, keys in findings]
    return (
        "Needs you: project settings could not be checked or contain "
        "auto-approve or allow rules, or they define MCP servers, plugins, or hooks; "
        "remove or repair them before this task can run: " + "; ".join(parts)
    )
