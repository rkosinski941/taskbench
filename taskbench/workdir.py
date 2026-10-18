"""Per-task working directory.

``workdir`` is an optional absolute path on a joy-feeder.task/v2 packet.
When it is set, that folder is the project root for the run. Symlinks and
Windows junctions are resolved with ``os.path.realpath`` before any allow
or reject decision. On Windows the result is also passed through
``GetFinalPathNameByHandleW`` and ``GetLongPathNameW`` so ``\\\\?\\``, UNC,
and 8.3 short names become the real long path.

The registry of folders the owner has accepted lives under taskbench's
data directory (``workdir-registry.json``). It is not written into the
project repo. Nothing in a packet, including ``from: web-ui``, can add a
folder. Only the local UI's register endpoint can.

A chosen folder may contain ``.grok`` and ``.claude`` (the allow-rule scan
reports those), plus ``.git``, ``.github``, and ``.taskbench``. Any other
hidden directory under it is rejected. The folder may not be the profile,
a personal folder (Documents, Desktop, Downloads, OneDrive), anything
inside AppData or a dot-folder under the profile, or the taskbench
install, config, or state directory. A folder that is an ancestor of one
of those is rejected too.
"""

from __future__ import annotations

import json
import os
import re
import sys
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

_DOT_NAMES = (
    ".ssh",
    ".grok",
    ".claude",
    ".aws",
    ".config",
    ".gnupg",
    ".azure",
    ".kube",
    ".docker",
)

_PERSONAL_NAMES = ("documents", "desktop", "downloads", "onedrive")


class WorkdirError(ValueError):
    """The path cannot be used as a task folder."""

    def __init__(self, message: str, resolved: str | None = None) -> None:
        super().__init__(message)
        self.resolved = resolved


class WorkdirTrustError(WorkdirError):
    """A valid folder that this sender has not been allowed to use."""


def validate_workdir(
    raw: str,
    *,
    state_dir: Path | str | None = None,
    config_dir: Path | str | None = None,
    short_names: dict[str, str] | None = None,
) -> Path:
    """Resolve ``raw`` and reject roots, personal folders, and system folders."""
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
    if os.name == "nt":
        resolved = windows_canonical_path(resolved, short_names=short_names)
    elif short_names:
        mapped = short_names.get(resolved)
        if mapped:
            resolved = mapped
    if not Path(resolved).is_dir():
        raise WorkdirError("workdir is not a directory", resolved)
    protected = _protected_roots(state_dir=state_dir, config_dir=config_dir)
    reason = rejection_reason(resolved, protected_roots=protected)
    if reason:
        raise WorkdirError(reason, resolved)
    if _contains_denied_dir(Path(resolved)):
        raise WorkdirError("workdir contains a denied path", resolved)
    return Path(resolved)


def same_canonical(left: str | Path, right: str | Path, *, platform: str | None = None) -> bool:
    """True when two already-canonical paths are the same folder."""
    plat = platform or ("nt" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "posix"))
    return _same(str(left), str(right), plat)


def rejection_reason(
    resolved: str,
    *,
    platform: str | None = None,
    profile_roots: list[str] | None = None,
    system_roots: list[str] | None = None,
    protected_roots: list[str] | None = None,
) -> str | None:
    """Reason to reject an already-resolved directory, or None.

    ``resolved`` is compared as a path string so Windows rules can be tested
    on other platforms. Callers pass the canonical path, not the original
    symlink or junction. Comparison is case-insensitive on Windows and macOS.
    """
    plat = platform or ("nt" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "posix"))
    if _is_drive_root(resolved, plat):
        return "workdir is a drive root"
    profiles = profile_roots if profile_roots is not None else _profile_roots()
    for profile in profiles:
        if _same(resolved, profile, plat):
            return "workdir is the user profile root"
        if _is_personal_exact(resolved, profile, plat):
            return "workdir is a personal folder"
    systems = system_roots if system_roots is not None else _system_roots(plat)
    for root in systems:
        if _under(resolved, root, plat):
            return f"workdir is inside a system folder: {root}"
    for profile in profiles:
        if _inside_appdata(resolved, profile, plat):
            return "workdir is inside AppData"
        if _inside_dot_folder(resolved, profile, plat):
            return "workdir is inside a dot-folder"
    protected = protected_roots or []
    for root in protected:
        if _under(resolved, root, plat):
            return "workdir is inside a taskbench folder"
    denied = _denied_targets(profiles, protected, plat)
    for target in denied:
        if _strict_ancestor(resolved, target, plat):
            return "workdir contains a denied path"
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


def _fold(text: str, platform: str) -> str:
    norm = _norm(text, "nt" if platform == "nt" else "posix")
    if platform in {"nt", "darwin"}:
        return norm.casefold()
    return norm


def _same(left: str, right: str, platform: str) -> bool:
    return _fold(left, platform) == _fold(right, platform)


def _under(path: str, root: str, platform: str) -> bool:
    path_n = _fold(path, platform)
    root_n = _fold(root, platform)
    if platform != "nt" and root_n == "/":
        return path_n == "/"
    sep = "\\" if platform == "nt" else "/"
    return path_n == root_n or path_n.startswith(root_n + sep)


def _strict_ancestor(parent: str, child: str, platform: str) -> bool:
    return _under(child, parent, platform) and not _same(parent, child, platform)


def _parent_name(path: str, platform: str) -> tuple[str, str]:
    if platform == "nt":
        text = path.replace("/", "\\").rstrip("\\")
        if "\\" not in text:
            return text, ""
        parent, name = text.rsplit("\\", 1)
        return parent, name
    text = path.rstrip("/") or "/"
    if text == "/":
        return "/", ""
    parent, name = text.rsplit("/", 1)
    return parent or "/", name


def _is_personal_exact(path: str, profile: str, platform: str) -> bool:
    parent, name = _parent_name(path, platform)
    if not name or not _same(parent, profile, platform):
        return False
    folded = name.casefold()
    if folded in _PERSONAL_NAMES:
        return True
    return folded.startswith("onedrive - ")


def _inside_appdata(path: str, profile: str, platform: str) -> bool:
    appdata = _join(profile, "AppData", platform)
    return _under(path, appdata, platform)


def _inside_dot_folder(path: str, profile: str, platform: str) -> bool:
    if _same(path, profile, platform) or not _under(path, profile, platform):
        return False
    rest = _fold(path, platform)[len(_fold(profile, platform)) :].strip("\\/")
    sep = "\\" if platform == "nt" else "/"
    return any(part.startswith(".") and part not in {".", ".."} for part in rest.split(sep))


def _join(root: str, name: str, platform: str) -> str:
    sep = "\\" if platform == "nt" else "/"
    return root.rstrip("\\/") + sep + name


def _denied_targets(profiles: list[str], protected: list[str], platform: str) -> list[str]:
    targets: list[str] = []
    for profile in profiles:
        targets.append(profile)
        for name in ("Documents", "Desktop", "Downloads", "OneDrive", "AppData"):
            targets.append(_join(profile, name, platform))
        names = set(_DOT_NAMES)
        try:
            for child in Path(profile).iterdir():
                if child.name.startswith(".") and child.name not in {".", ".."}:
                    names.add(child.name)
        except OSError:
            pass
        for name in names:
            targets.append(_join(profile, name, platform))
    targets.extend(protected)
    return targets


def _protected_roots(*, state_dir: Path | str | None, config_dir: Path | str | None) -> list[str]:
    roots = [str(Path(__file__).resolve().parent)]
    for extra in (state_dir, config_dir):
        if extra:
            roots.append(str(Path(extra)))
    return roots


# Project settings are scanned (Needs you) instead of rejected here.
# ``.git`` is source control. ``.github`` is workflow metadata.
# ``.taskbench`` is the watcher's own log directory, created after launch.
_ALLOWED_DOT_DIRS = {".git", ".github", ".taskbench", ".grok", ".claude"}


def _contains_denied_dir(folder: Path) -> bool:
    """True when ``folder`` contains a hidden directory other than the allowed set.

    Profile credential directories (``.ssh``, ``.aws``, and any other ``.*``)
    make the folder an ancestor of a denied path. ``.grok`` and ``.claude``
    stay allowed so the settings scan can send the task to Needs you.
    An unreadable child fails closed.
    """
    failed = False

    def onerror(_err: OSError) -> None:
        nonlocal failed
        failed = True

    for dirpath, dirnames, _filenames in os.walk(folder, followlinks=False, onerror=onerror):
        current = Path(dirpath)
        kept: list[str] = []
        for name in dirnames:
            folded = name.casefold()
            child = current / name
            if folded in _ALLOWED_DOT_DIRS:
                continue
            if folded.startswith(".") and folded not in {".", ".."}:
                return True
            if child.is_symlink():
                continue
            kept.append(name)
        dirnames[:] = kept
        if failed:
            return True
    return failed


def windows_canonical_path(path: str, *, short_names: dict[str, str] | None = None) -> str:
    """Map ``\\\\?\\``, UNC, and 8.3 names to a long DOS path.

    On Windows this calls ``GetFinalPathNameByHandleW`` and
    ``GetLongPathNameW``. Tests pass ``short_names`` to exercise the 8.3
    step without those APIs.
    """
    text = path
    if os.name == "nt" and short_names is None:
        text = _windows_final_path(path)
    text = _strip_extended_prefix(text)
    if short_names and text in short_names:
        text = short_names[text]
    elif os.name == "nt" and short_names is None:
        text = _windows_long_path(text)
    return text


def _strip_extended_prefix(path: str) -> str:
    text = path.replace("/", "\\")
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[len("\\\\?\\UNC\\") :]
    if text.startswith("\\\\?\\"):
        return text[4:]
    return path


def _windows_final_path(path: str) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        path,
        0,
        0x1 | 0x2 | 0x4,
        None,
        3,
        0x02000000,
        None,
    )
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid or handle is None:
        raise WorkdirError("workdir does not exist", path)
    try:
        size = 4096
        buf = ctypes.create_unicode_buffer(size)
        kernel32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        written = kernel32.GetFinalPathNameByHandleW(handle, buf, size, 0)
        if written == 0 or written >= size:
            raise WorkdirError("workdir does not exist", path)
        return buf.value
    finally:
        kernel32.CloseHandle(handle)


def _windows_long_path(path: str) -> str:
    import ctypes
    from ctypes import wintypes

    size = 4096
    buf = ctypes.create_unicode_buffer(size)
    kernel32 = ctypes.windll.kernel32
    kernel32.GetLongPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel32.GetLongPathNameW.restype = wintypes.DWORD
    written = kernel32.GetLongPathNameW(path, buf, size)
    if written == 0 or written >= size:
        raise WorkdirError("workdir does not exist", path)
    return buf.value
