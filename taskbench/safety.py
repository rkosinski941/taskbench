"""Permission decisions and project path checks.

Default permission is ``ask``. ``plan`` (and a runner with ``sandbox_only``)
may list, read, and diff inside the project. Anything else is refused, not
prompted. ``ask`` prompts for writes and commands. ``allow-list`` runs a
command only when a glob matches the full command line. A glob star does not
match shell separators (``;``, ``&``, ``|``, and command substitution) unless
the allow pattern itself contains that separator.

Starting a listener or a background process always prompts, even on an
allow-list, unless an allow entry matches that full command. An allowed
server still has to name ``127.0.0.1`` or ``localhost`` and must not name
another address.

Paths are resolved and must stay inside the project. ``..``, absolute paths,
UNC paths, drive-relative paths, and symlinks that land outside are rejected.
Writes under ``<project>/.taskbench`` are refused so a runner cannot rewrite
the watcher handoff.
"""

from __future__ import annotations

import difflib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

READ_ONLY_OPS = {"list", "read", "diff"}
_META = ("&&", "||", ";", "|", "`", "$(", "\n", "&")
_SERVER_RE = re.compile(
    r"(http\.server|uvicorn|gunicorn|flask\s+run|next\s+dev|npm\s+start|"
    r"npx\s+serve|php\s+-S|\blisten\b|--listen\b|--bind\b|0\.0\.0\.0)",
    re.IGNORECASE,
)
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


class PathEscape(Exception):
    """The path is outside the project or is not a safe relative path."""


@dataclass
class Decision:
    kind: str  # allow, approve, refuse
    reason: str


def full_command(action: dict) -> str:
    command = action.get("command")
    if isinstance(command, str) and command:
        return command
    op = str(action.get("op") or "action")
    if op == "write":
        path = action.get("path", "")
        content = action.get("content", "")
        return f"write {path} <<'EOF'\n{content}\nEOF"
    path = action.get("path", ".")
    return f"{op} {path}"


def approval_title(command: str) -> str:
    return command[:120]


def classify(
    action: dict,
    *,
    permissions: str,
    allow: list[str] | None,
    sandbox_only: bool,
    project_dir: Path,
    denied_dirs: list[Path] | None = None,
) -> Decision:
    op = str(action.get("op") or "")
    command = full_command(action)
    patterns = list(allow or [])
    server = is_long_lived(action, command)
    plan_like = sandbox_only or permissions == "plan"

    if _hits_denied(project_dir, action, denied_dirs or []):
        return Decision("refuse", "path is inside a denied directory")

    if plan_like:
        if op in READ_ONLY_OPS and not server and _path_inside(project_dir, action):
            return Decision("allow", "read-only")
        return Decision("refuse", "plan mode refuses this action")

    if server:
        if (
            permissions == "allow-list"
            and _allow_match(command, patterns)
            and binds_loopback_only(command)
        ):
            return Decision("allow", "allow-list")
        if permissions == "allow-list" and _allow_match(command, patterns):
            return Decision("refuse", "server must bind 127.0.0.1")
        return Decision("approve", "server or background process needs approval")

    if op in READ_ONLY_OPS:
        if _path_inside(project_dir, action):
            return Decision("allow", "read-only")
        return Decision("approve", "path is outside the project")

    if op == "write":
        if _targets_audit(project_dir, action):
            return Decision("refuse", "writes to .taskbench are reserved for the watcher")
        if permissions == "allow-list" and _allow_match(command, patterns) and _path_inside(project_dir, action):
            return Decision("allow", "allow-list")
        return Decision("approve", "write needs approval")

    if permissions == "allow-list" and _allow_match(command, patterns):
        return Decision("allow", "allow-list")
    return Decision("approve", "command needs approval")


def is_long_lived(action: dict, command: str) -> bool:
    op = str(action.get("op") or "")
    if op in {"serve", "listen", "background", "daemon"}:
        return True
    stripped = command.rstrip()
    if stripped.endswith("&"):
        return True
    if re.search(r"(^|[\s;&|])nohup\b", command, re.IGNORECASE):
        return True
    return _SERVER_RE.search(command) is not None


def binds_loopback_only(command: str) -> bool:
    lowered = command.lower()
    if "0.0.0.0" in lowered or "[::]" in lowered or "://::" in lowered:
        return False
    for ip in _IP_RE.findall(command):
        if ip != "127.0.0.1":
            return False
    return "127.0.0.1" in lowered or "localhost" in lowered


def resolve_inside_project(project_dir: Path, raw: str) -> Path:
    if not isinstance(raw, str) or raw.strip() == "":
        raise PathEscape("empty path")
    if "\x00" in raw:
        raise PathEscape("NUL in path")
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise PathEscape("UNC path")
    if re.match(r"^[A-Za-z]:", raw):
        raise PathEscape("drive path")
    root = project_dir.resolve()
    candidate = Path(raw)
    target = candidate if candidate.is_absolute() else root / candidate
    resolved = target.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PathEscape(f"path escapes project: {raw}") from exc
    return resolved


def preview_url(command: str) -> str | None:
    if not is_long_lived({"op": "shell", "command": command}, command):
        return None
    if not binds_loopback_only(command):
        return None
    patterns = (
        r"http\.server\s+(\d{2,5})",
        r"(?:127\.0\.0\.1|localhost):(\d{2,5})",
        r"--port(?:=|\s+)(\d{2,5})",
    )
    for pattern in patterns:
        match = re.search(pattern, command, re.IGNORECASE)
        if not match:
            continue
        port = int(match.group(1))
        if 1 <= port <= 65535:
            return f"http://127.0.0.1:{port}/"
    return None


def render_diff(project_dir: Path, action: dict) -> str:
    supplied = action.get("diff")
    if isinstance(supplied, str) and supplied:
        return supplied
    if str(action.get("op") or "") != "write":
        return ""
    raw = action.get("path")
    if not isinstance(raw, str):
        return ""
    try:
        path = resolve_inside_project(project_dir, raw)
    except PathEscape:
        return f"(path outside project) {raw}"
    old = ""
    if path.is_file():
        old = path.read_text(encoding="utf-8", errors="replace")
    new = action.get("content")
    if not isinstance(new, str):
        new = ""
    lines = difflib.unified_diff(
        old.splitlines(True),
        new.splitlines(True),
        fromfile=raw,
        tofile=raw,
    )
    return "".join(lines)


def execute_readonly(project_dir: Path, action: dict) -> str:
    op = str(action.get("op") or "")
    raw = action.get("path", ".")
    if not isinstance(raw, str):
        raise PathEscape("path must be a string")
    path = resolve_inside_project(project_dir, raw)
    if op == "list":
        if not path.exists():
            return ""
        if path.is_file():
            return path.name
        names = sorted(child.name for child in path.iterdir())
        return "\n".join(names)
    if op == "read":
        if not path.is_file():
            return ""
        return _read_limited(path)
    if op == "diff":
        against = action.get("against")
        current = _read_limited(path) if path.is_file() else ""
        if isinstance(against, str) and against:
            other = resolve_inside_project(project_dir, against)
            previous = _read_limited(other) if other.is_file() else ""
            return "".join(
                difflib.unified_diff(
                    previous.splitlines(True),
                    current.splitlines(True),
                    fromfile=against,
                    tofile=raw,
                )
            )
        return "".join(
            difflib.unified_diff([], current.splitlines(True), fromfile="/dev/null", tofile=raw)
        )
    raise PathEscape(f"not a read-only op: {op}")


def execute_write(project_dir: Path, action: dict) -> str:
    raw = action.get("path")
    if not isinstance(raw, str):
        raise PathEscape("write path must be a string")
    path = resolve_inside_project(project_dir, raw)
    root = project_dir.resolve()
    if path == root or _under_audit(root, path):
        raise PathEscape("refusing this write")
    content = action.get("content")
    if not isinstance(content, str):
        raise PathEscape("write content must be a string")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return f"wrote {raw}"


def shell_argv(command: str) -> list[str]:
    """Run an approved command string without adding a bypass flag."""
    import os

    if os.name == "nt":
        return ["cmd.exe", "/c", command]
    return ["/bin/sh", "-c", command]


def _hits_denied(project_dir: Path, action: dict, denied_dirs: list[Path]) -> bool:
    """True when a path resolves inside the pinned Grok home or another denied dir."""
    if not denied_dirs:
        return False
    raw_paths = [action.get("path"), action.get("against")]
    for raw in raw_paths:
        if not isinstance(raw, str) or not raw.strip():
            continue
        candidate = Path(raw)
        target = candidate if candidate.is_absolute() else project_dir / candidate
        try:
            resolved = target.resolve(strict=False)
        except OSError:
            continue
        for denied in denied_dirs:
            try:
                root = Path(denied).resolve(strict=False)
            except OSError:
                continue
            if resolved == root or _is_under(resolved, root):
                return True
    return False


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _path_inside(project_dir: Path, action: dict) -> bool:
    raw = action.get("path", ".")
    if not isinstance(raw, str):
        return False
    try:
        resolve_inside_project(project_dir, raw)
    except PathEscape:
        return False
    against = action.get("against")
    if isinstance(against, str) and against:
        try:
            resolve_inside_project(project_dir, against)
        except PathEscape:
            return False
    return True


def _targets_audit(project_dir: Path, action: dict) -> bool:
    raw = action.get("path")
    if not isinstance(raw, str):
        return False
    try:
        path = resolve_inside_project(project_dir, raw)
    except PathEscape:
        return False
    return _under_audit(project_dir.resolve(), path)


def _under_audit(root: Path, path: Path) -> bool:
    audit = root / ".taskbench"
    try:
        path.relative_to(audit)
        return True
    except ValueError:
        return False


def _allow_match(command: str, patterns: list[str]) -> bool:
    if _meta_requires_prompt(command, patterns):
        return False
    return any(glob_fullmatch(pattern, command) for pattern in patterns)


def _meta_requires_prompt(command: str, patterns: list[str]) -> bool:
    if not any(token in command for token in _META):
        return False
    for pattern in patterns:
        if glob_fullmatch(pattern, command) and any(token in pattern for token in _META):
            return False
    return True


def glob_fullmatch(pattern: str, text: str) -> bool:
    parts: list[str] = []
    for char in pattern:
        if char == "*":
            parts.append(".*")
        elif char == "?":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    return re.fullmatch("".join(parts), text, flags=re.DOTALL) is not None


def _read_limited(path: Path) -> str:
    data = path.read_bytes()[: 1_000_000]
    return data.decode("utf-8", errors="replace")


def split_command_display(command: str) -> str:
    """Return the command unchanged. Present so callers do not re-quote it."""
    return command


def posix_quote(command: str) -> str:
    return shlex.quote(command)
