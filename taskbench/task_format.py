"""joy-feeder.task/v2 packet parsing and validation.

The published field table lives in ``docs/joy-feeder-task-v2.md``. This module
is the only place that interprets a packet. The watcher stores the dict this
module returns and does not re-parse fields elsewhere.

Packets on disk are YAML, using the subset in ``taskbench.yaml_subset``. JSON
is not accepted.

Assumptions where the spec is silent are marked in the README. In short:

* ``timeout_s`` above 7200, below 1, or not an integer is rejected (the packet
  is blocked). It is not clamped.
* A schema other than ``joy-feeder.task/v2`` (and the v1 shape below) is
  blocked with ``unknown schema '...'`` and is never run.
* v1 packets (schema ``joy-feeder.task/v1``, or a mapping ``from``) are stored
  as blocked v2 packets with reason ``v1 packet: set runner and project``.
  Their ``from`` label is ``(unconverted-v1)`` because v1 had no bot name.
* Unknown ``permissions`` values are stored as ``ask``.
* ``from`` is rejected when it is empty or equal, ignoring case and surrounding
  space, to the configured human owner (default ``Rich``).
* ``workdir``, when present, is a non-empty string. The watcher resolves it
  and decides whether that folder may be used. See ``docs/SPEC-ADDITIONS.md``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from taskbench.yaml_subset import YamlSubsetError, dump, load

SCHEMA_V2 = "joy-feeder.task/v2"
SCHEMA_V1 = "joy-feeder.task/v1"
RUNNERS = ("grok-build", "antigravity", "opencode")
PERMISSIONS = ("plan", "ask", "allow-list")
PRIORITIES = ("low", "normal", "high")
STATUSES = (
    "incoming",
    "claimed",
    "running",
    "needs-approval",
    "done",
    "failed",
    "blocked",
    "timed-out",
)
TERMINAL = ("done", "failed", "blocked", "timed-out")
DEFAULT_TIMEOUT_S = 900
MAX_TIMEOUT_S = 7200
V1_REASON = "v1 packet: set runner and project"
INVALID_FROM = "invalid from"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


@dataclass
class InterpretedPacket:
    """A packet the watcher can store. ``ok`` packets are safe to queue."""

    ok: bool
    packet: dict[str, Any]
    reason: str | None = None


def parse_yaml(text: str) -> Any:
    return load(text)


def emit_yaml(packet: dict[str, Any]) -> str:
    return dump(packet)


def interpret(data: Any, *, human_owner: str) -> InterpretedPacket:
    """Validate a parsed document. Invalid packets come back blocked, not raised.

    Raising is reserved for the caller when the file is not YAML at all. A
    packet that parsed but must not run is returned with ``ok`` false and
    ``status: blocked`` so the watcher can move it to the outbox.
    """
    if not isinstance(data, dict):
        return InterpretedPacket(
            False,
            _blocked_shell(reason="packet must be a mapping", packet_id=_fallback_id(data)),
            "packet must be a mapping",
        )
    if _is_v1(data):
        packet = _migrate_v1(data)
        return InterpretedPacket(False, packet, V1_REASON)
    schema = data.get("schema")
    if schema != SCHEMA_V2:
        shown = "" if schema is None else str(schema)
        reason = "missing schema" if schema is None else f"unknown schema '{shown}'"
        packet = _blocked_shell(
            reason=reason,
            packet_id=_safe_id(data.get("id")) or _fallback_id(data),
            goal=_as_text(data.get("goal")),
            project=_as_text(data.get("project")) or None,
            sender=_as_text(data.get("from")) or "(invalid)",
        )
        packet["schema"] = SCHEMA_V2
        return InterpretedPacket(False, packet, reason)

    reasons: list[str] = []
    packet_id = data.get("id")
    if not isinstance(packet_id, str) or not _ID_RE.fullmatch(packet_id) or ".." in packet_id:
        reasons.append("invalid id")
        packet_id = _fallback_id(data)

    created = data.get("created")
    if not isinstance(created, str) or _parse_offset(created) is None:
        reasons.append("invalid created")

    sender = data.get("from")
    from_reason = _check_from(sender, human_owner)
    if from_reason:
        reasons.append(from_reason)

    project = data.get("project")
    if not _valid_project(project):
        reasons.append("invalid project")

    workdir = data.get("workdir", None)
    if workdir is not None:
        if not isinstance(workdir, str) or not workdir.strip() or "\x00" in workdir:
            reasons.append("invalid workdir")

    runner = data.get("runner")
    requested = runner.get("requested") if isinstance(runner, dict) else None
    if requested not in RUNNERS:
        reasons.append("invalid runner")

    goal = data.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        reasons.append("invalid goal")

    status = data.get("status")
    if status != "incoming":
        reasons.append("invalid status")

    permissions = data.get("permissions")
    if permissions is None or permissions not in PERMISSIONS:
        permissions = "ask"
    allow = data.get("allow")
    if permissions == "allow-list":
        if not isinstance(allow, list) or not allow or not all(isinstance(item, str) and item for item in allow):
            reasons.append("allow-list requires allow")

    timeout, timeout_reason = _normalize_timeout(data.get("timeout_s"), present="timeout_s" in data)
    if timeout_reason:
        reasons.append(timeout_reason)

    files = data.get("files")
    if files is not None:
        if not isinstance(files, list) or not all(_valid_relative_file(item) for item in files):
            reasons.append("invalid files")

    review = data.get("review_by")
    if review is not None:
        if not isinstance(review, dict) or review.get("runner") not in RUNNERS:
            reasons.append("invalid review_by")
        elif review.get("permissions") not in (None, *PERMISSIONS):
            reasons.append("invalid review_by")

    priority = data.get("priority")
    if priority is not None and priority not in PRIORITIES:
        reasons.append("invalid priority")

    due = data.get("due", None)
    if due is not None and (not isinstance(due, str) or _parse_offset(due) is None):
        reasons.append("invalid due")

    if reasons:
        reason = reasons[0] if reasons == [INVALID_FROM] else "; ".join(reasons)
        if INVALID_FROM in reasons and len(reasons) == 1:
            reason = INVALID_FROM
        blocked = _copy_sender_fields(data)
        blocked["id"] = packet_id if isinstance(packet_id, str) else _fallback_id(data)
        blocked["schema"] = SCHEMA_V2
        blocked["status"] = "blocked"
        blocked["permissions"] = permissions
        blocked["timeout_s"] = timeout if timeout_reason is None else DEFAULT_TIMEOUT_S
        blocked["result"] = _result(reason)
        blocked["retro"] = None
        return InterpretedPacket(False, blocked, reason)

    packet = _copy_sender_fields(data)
    packet["schema"] = SCHEMA_V2
    packet["permissions"] = permissions
    packet["timeout_s"] = timeout
    packet["status"] = "incoming"
    packet.setdefault("approvals", [])
    if packet.get("retro") is None:
        packet["retro"] = None
    if packet.get("result") is None:
        packet["result"] = None
    runner_map = dict(packet.get("runner") or {})
    runner_map["requested"] = requested
    runner_map["actual"] = None
    packet["runner"] = runner_map
    model = packet.get("model")
    if not isinstance(model, dict):
        model = {"requested": None, "actual": None}
    else:
        model = dict(model)
        model["actual"] = None
    packet["model"] = model
    return InterpretedPacket(True, packet, None)


def check_from(value: Any, human_owner: str) -> str | None:
    return _check_from(value, human_owner)


def _check_from(value: Any, human_owner: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return INVALID_FROM
    if any(ord(char) < 32 or char in "/\\" for char in value):
        return INVALID_FROM
    folded = value.strip().casefold()
    owner = (human_owner or "Rich").strip().casefold()
    if folded == owner or folded == "rich":
        return INVALID_FROM
    return None


def _valid_project(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if value != value.strip():
        return False
    if ".." in value or "/" in value or "\\" in value:
        return False
    if re.match(r"^[A-Za-z]:", value):
        return False
    return _PROJECT_RE.fullmatch(value) is not None


def _valid_relative_file(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if value.startswith("/") or value.startswith("\\") or re.match(r"^[A-Za-z]:", value):
        return False
    parts = re.split(r"[\\/]", value)
    return all(part not in {"", ".", ".."} for part in parts)


def _normalize_timeout(value: Any, *, present: bool) -> tuple[int, str | None]:
    if not present or value is None:
        return DEFAULT_TIMEOUT_S, None
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_TIMEOUT_S, "invalid timeout_s"
    if value < 1 or value > MAX_TIMEOUT_S:
        return DEFAULT_TIMEOUT_S, "invalid timeout_s"
    return value, None


def _parse_offset(value: str) -> datetime | None:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _is_v1(data: dict[str, Any]) -> bool:
    schema = data.get("schema")
    if schema == SCHEMA_V1:
        return True
    if isinstance(data.get("from"), dict) and schema != SCHEMA_V2:
        return True
    return False


def _migrate_v1(data: dict[str, Any]) -> dict[str, Any]:
    origin: dict[str, Any] = {}
    sender = data.get("from")
    if isinstance(sender, dict):
        if "surface" in sender:
            origin["surface"] = sender.get("surface")
        if "thread" in sender:
            origin["thread"] = sender.get("thread")
        raw_project = sender.get("project")
    else:
        raw_project = data.get("project")
    if isinstance(data.get("origin"), dict):
        origin = {**data["origin"], **origin}
    project = raw_project if _valid_project(raw_project) else None
    packet_id = data.get("id")
    if not isinstance(packet_id, str) or not _ID_RE.fullmatch(packet_id):
        packet_id = _fallback_id(data)
    created = data.get("created") if isinstance(data.get("created"), str) else _now()
    goal = data.get("goal") if isinstance(data.get("goal"), str) else ""
    packet: dict[str, Any] = {
        "schema": SCHEMA_V2,
        "id": packet_id,
        "created": created,
        "from": "(unconverted-v1)",
        "origin": origin or None,
        "project": project,
        "runner": {"requested": None, "actual": None},
        "model": {"requested": None, "actual": None},
        "permissions": "ask",
        "timeout_s": DEFAULT_TIMEOUT_S,
        "goal": goal,
        "status": "blocked",
        "retro": None,
        "result": _result(V1_REASON),
        "approvals": [],
    }
    return packet


def _copy_sender_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Keep sender fields, including ones this version does not interpret yet."""
    return dict(data)


def _result(reason: str) -> dict[str, Any]:
    return {
        "summary": None,
        "artifacts": [],
        "blocked_reason": reason,
        "completed_at": _now(),
    }


def _blocked_shell(
    *,
    reason: str,
    packet_id: str,
    goal: str = "",
    project: str | None = None,
    sender: str = "(invalid)",
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_V2,
        "id": packet_id,
        "created": _now(),
        "from": sender or "(invalid)",
        "runner": {"requested": None, "actual": None},
        "model": {"requested": None, "actual": None},
        "project": project,
        "permissions": "ask",
        "timeout_s": DEFAULT_TIMEOUT_S,
        "goal": goal,
        "status": "blocked",
        "retro": None,
        "result": _result(reason),
        "approvals": [],
    }


def _safe_id(value: Any) -> str | None:
    if isinstance(value, str) and _ID_RE.fullmatch(value) and ".." not in value:
        return value
    return None


def _fallback_id(data: Any) -> str:
    raw = repr(data).encode("utf-8", errors="replace")
    return "invalid-" + hashlib.sha256(raw).hexdigest()[:8]


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _now() -> str:
    from datetime import timezone

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_packet_text(text: str, *, human_owner: str) -> InterpretedPacket:
    try:
        data = load(text)
    except YamlSubsetError as exc:
        packet = _blocked_shell(reason=f"invalid YAML: {exc}", packet_id=_fallback_id(text))
        return InterpretedPacket(False, packet, packet["result"]["blocked_reason"])
    return interpret(data, human_owner=human_owner)
