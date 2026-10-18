"""Per-task working directory.

``workdir`` is an optional absolute path on a joy-feeder.task/v2 packet.
When it is set, that folder is the project root for the run. Symlinks and
Windows junctions are resolved with ``os.path.realpath`` before any allow
or reject decision.

The registry of folders the owner has accepted lives under taskbench's
data directory (``workdir-registry.json``). It is not written into the
project repo. A bot may only use a folder that is already there. A task
from ``web-ui`` may introduce one new valid folder, which is then stored.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from taskbench.atomic import atomic_write_text

_REGISTRY_NAME = "workdir-registry.json"
_MAX_RECENT = 30

_POSIX_SYSTEM = (
    "/",
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/boot",
    "/proc",
    "/sys",
    "/dev",
    "/System",
    "/private",
)

_WINDOWS_SYSTEM = (
    r"C:\Windows",
    r"C:\Windows\System32",
    r"C:\Windows\SysWOW64",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
)


class WorkdirError(ValueError):
    """The path cannot be used as a task folder."""

    def __init__(self, message: str, resolved: str | None = None) -> None:
        super().__init__(message)
        self.resolved = resolved


class WorkdirTrustError(WorkdirError):
    """A valid folder that this sender has not been allowed to use."""


def validate_workdir(raw: str) -> Path:
    """Resolve ``raw`` and reject roots, the profile, and system folders."""
    if not isinstance(raw, str) or not raw.strip():
        raise WorkdirError("workdir is empty")
    if "\x00" in raw:
        raise WorkdirError("workdir contains NUL")
    text = raw.strip()
    if not _is_absolute(text, os.name):
        raise WorkdirError("workdir must be an absolute path")
    try:
        resolved = os.path.realpath(text, strict=True)
    except FileNotFoundError as exc:
        raise WorkdirError("workdir does not exist") from exc
    except OSError as exc:
        raise WorkdirError("workdir does not exist") from exc
    if not Path(resolved).is_dir():
        raise WorkdirError("workdir is not a directory", resolved)
    reason = rejection_reason(resolved)
    if reason:
        raise WorkdirError(reason, resolved)
    return Path(resolved)


def rejection_reason(
    resolved: str,
    *,
    platform: str | None = None,
    profile_roots: list[str] | None = None,
    system_roots: list[str] | None = None,
) -> str | None:
    """Reason to reject an already-resolved directory, or None.

    ``resolved`` is compared as a path string so Windows rules can be tested
    on other platforms. Callers pass the realpath result, not the original
    symlink or junction.
    """
    plat = platform or os.name
    if _is_drive_root(resolved, plat):
        return "workdir is a drive root"
    profiles = profile_roots if profile_roots is not None else _profile_roots()
    for profile in profiles:
        if _same(resolved, profile, plat):
            return "workdir is the user profile root"
    systems = system_roots if system_roots is not None else _system_roots(plat)
    for root in systems:
        if _under(resolved, root, plat):
            return f"workdir is inside a system folder: {root}"
    return None


def load_registry(data_dir: Path) -> list[str]:
    path = Path(data_dir) / _REGISTRY_NAME
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    folders = data.get("folders") if isinstance(data, dict) else None
    if not isinstance(folders, list):
        return []
    return [item for item in folders if isinstance(item, str) and item.strip()]


def register_workdir(data_dir: Path, folder: Path) -> None:
    """Remember ``folder``. Most recent first. Does not touch the repo."""
    chosen = str(folder)
    kept = [item for item in load_registry(data_dir) if not _same(item, chosen, os.name)]
    kept.insert(0, chosen)
    del kept[_MAX_RECENT:]
    path = Path(data_dir) / _REGISTRY_NAME
    atomic_write_text(path, json.dumps({"folders": kept}, indent=2) + "\n")


def is_registered(data_dir: Path, folder: Path) -> bool:
    chosen = str(folder)
    return any(_same(item, chosen, os.name) for item in load_registry(data_dir))


def display_folder(packet: dict, *, projects_dir: Path) -> str:
    """Resolved folder for the task list and the approval prompt."""
    raw = packet.get("workdir") if isinstance(packet, dict) else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    project = packet.get("project") if isinstance(packet, dict) else None
    if not isinstance(project, str) or not project.strip():
        return ""
    return str((projects_dir / project).resolve())


def _profile_roots() -> list[str]:
    found: list[str] = []
    for key in ("HOME", "USERPROFILE"):
        value = os.environ.get(key)
        if not value:
            continue
        try:
            found.append(os.path.realpath(value, strict=True))
        except OSError:
            found.append(value)
    return found


def _system_roots(platform: str) -> list[str]:
    if platform == "nt":
        roots = list(_WINDOWS_SYSTEM)
        windir = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
        if windir:
            roots.append(windir)
            roots.append(str(Path(windir) / "System32"))
            roots.append(str(Path(windir) / "SysWOW64"))
        for key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            value = os.environ.get(key)
            if value:
                roots.append(value)
        return roots
    return list(_POSIX_SYSTEM)


def _is_absolute(text: str, platform: str) -> bool:
    if platform == "nt":
        if re.match(r"^[A-Za-z]:[\\/]", text):
            return True
        return text.startswith("\\\\") or text.startswith("//")
    return text.startswith("/")


def _is_drive_root(text: str, platform: str) -> bool:
    if platform == "nt":
        normalized = text.replace("/", "\\")
        if re.fullmatch(r"[A-Za-z]:\\?", normalized):
            return True
        if normalized.startswith("\\\\"):
            parts = [part for part in normalized.split("\\") if part]
            return len(parts) <= 2
        return False
    return _norm(text, "posix") == "/"


def _norm(text: str, platform: str) -> str:
    """Normalize for comparison without using the host's ``os.path``.

    POSIX rules are string checks so they can be tested on Windows, where
    ``os.path.normpath('/System')`` is not a POSIX path.
    """
    if platform == "nt":
        return os.path.normcase(text.replace("/", "\\")).rstrip("\\")
    parts: list[str] = []
    absolute = text.startswith("/") or text.startswith("\\")
    for part in text.replace("\\", "/").split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    if absolute:
        return "/" + "/".join(parts)
    return "/".join(parts)


def _same(left: str, right: str, platform: str) -> bool:
    return _norm(left, platform) == _norm(right, platform)


def _under(path: str, root: str, platform: str) -> bool:
    path_n = _norm(path, platform)
    root_n = _norm(root, platform)
    if platform != "nt" and root_n == "/":
        return path_n == "/"
    return path_n == root_n or path_n.startswith(root_n + ("\\" if platform == "nt" else "/"))
