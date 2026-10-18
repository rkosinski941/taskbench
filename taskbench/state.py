"""Packet files, atomic moves, and the run log.

Directories:

* ``inbox/`` — ``incoming``
* ``claimed/`` — queued behind the current run
* ``running/`` — the runner process is live
* ``needs-approval/`` — paused on a prompt; the timeout clock keeps running
* ``outbox/`` — terminal packets (``done``, ``failed``, ``blocked``, ``timed-out``)
  plus ``<id>.prompt.md`` and ``<id>.output.log``

A transition writes the destination file, fsyncs it, then deletes the source.
``recover`` drops ``.tmp`` files and, if a crash left the same id in two
places, keeps the further state.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from taskbench.atomic import atomic_write_text
from taskbench.projects import now_iso
from taskbench.redact import redact
from taskbench.task_format import TERMINAL, emit_stored, parse_stored

ACTIVE = ("inbox", "claimed", "running", "needs-approval")
RANK = {
    "inbox": 1,
    "claimed": 2,
    "running": 3,
    "needs-approval": 4,
    "outbox": 5,
}


class StateStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.inbox = root / "inbox"
        self.claimed = root / "claimed"
        self.running = root / "running"
        self.needs_approval = root / "needs-approval"
        self.outbox = root / "outbox"
        self.runtime = root / "runtime"
        self.log_path = root / "run.log"
        self.lock = threading.RLock()
        for directory in (
            self.inbox,
            self.claimed,
            self.running,
            self.needs_approval,
            self.outbox,
            self.runtime,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def location_dir(self, location: str) -> Path:
        return {
            "inbox": self.inbox,
            "claimed": self.claimed,
            "running": self.running,
            "needs-approval": self.needs_approval,
            "outbox": self.outbox,
        }[location]

    def log(self, message: str) -> None:
        line = f"{now_iso()} {redact(message)}\n"
        with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)

    def recover(self) -> list[str]:
        notes: list[str] = []
        for directory in (
            self.inbox,
            self.claimed,
            self.running,
            self.needs_approval,
            self.outbox,
            self.runtime,
        ):
            for tmp in directory.glob("*.tmp"):
                tmp.unlink(missing_ok=True)
                notes.append(f"dropped incomplete {tmp.name}")
        found: dict[str, list[str]] = {}
        for location in ("inbox", "claimed", "running", "needs-approval", "outbox"):
            directory = self.location_dir(location)
            for path in _loose_packet_files(directory):
                stem = _packet_stem(path)
                found.setdefault(stem, []).append(location)
            _drop_yaml_shadows(directory)
        for packet_id, locations in found.items():
            unique = list(dict.fromkeys(locations))
            if len(unique) < 2:
                continue
            keep = max(unique, key=lambda item: RANK[item])
            for location in unique:
                if location == keep:
                    continue
                self.remove_packet(location, packet_id)
                notes.append(f"recover {packet_id}: kept {keep}, removed {location}")
        for note in notes:
            self.log(note)
        return notes

    def iter_packets(self) -> list[tuple[str, dict[str, Any]]]:
        items: list[tuple[str, dict[str, Any]]] = []
        for location in ("inbox", "claimed", "running", "needs-approval", "outbox"):
            for path in self.packet_paths(self.location_dir(location)):
                try:
                    data = parse_stored(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(data, dict):
                    items.append((location, data))
        return items

    def find(self, packet_id: str) -> tuple[str, dict[str, Any]] | None:
        for location, packet in self.iter_packets():
            if packet.get("id") == packet_id:
                return location, packet
        return None

    def id_exists_elsewhere(self, packet_id: str, ignore: Path) -> bool:
        """True when some packet file other than ``ignore`` already uses this id."""
        ignore_resolved = ignore.resolve()
        for location in ("inbox", "claimed", "running", "needs-approval", "outbox"):
            for path in self.packet_paths(self.location_dir(location)):
                if path.resolve() == ignore_resolved:
                    continue
                try:
                    data = parse_stored(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("id") == packet_id:
                    return True
        return False

    def packet_paths(self, directory: Path) -> list[Path]:
        """Packet files in ``directory``. JSON wins when a YAML copy is also there."""
        chosen: dict[str, Path] = {}
        rank = {".yml": 1, ".yaml": 2, ".json": 3}
        if not directory.is_dir():
            return []
        for path in directory.iterdir():
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix not in rank:
                continue
            stem = _packet_stem(path)
            current = chosen.get(stem)
            if current is None or rank[suffix] > rank.get(current.suffix.lower(), 0):
                chosen[stem] = path
        return sorted(chosen.values(), key=lambda item: item.name)

    def inbox_files(self) -> list[Path]:
        return self.packet_paths(self.inbox)

    def write_packet(self, location: str, packet: dict[str, Any]) -> None:
        """Store a watcher-owned packet as JSON. Model text is not re-parsed as YAML."""
        path = self.location_dir(location) / f"{packet['id']}.json"
        atomic_write_text(path, emit_stored(_redact_model_fields(packet)))

    def remove_packet(self, location: str, packet_id: str) -> None:
        directory = self.location_dir(location)
        for suffix in (".json", ".yaml", ".yml"):
            (directory / f"{packet_id}{suffix}").unlink(missing_ok=True)

    def move_packet(self, src: str, dest: str, packet: dict[str, Any], note: str) -> None:
        """Write ``dest`` then delete ``src``. ``recover`` heals a crash between them."""
        previous = packet.get("status")
        self.write_packet(dest, packet)
        if src != dest:
            self.remove_packet(src, str(packet["id"]))
        self.log(f"{packet['id']} {note}")
        if previous and previous != packet.get("status"):
            self.log(f"{packet['id']} {previous} -> {packet['status']}")

    def read_meta(self, packet_id: str) -> dict[str, Any]:
        path = self.runtime / f"{packet_id}.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def write_meta(self, packet_id: str, meta: dict[str, Any]) -> None:
        atomic_write_text(
            self.runtime / f"{packet_id}.json",
            json.dumps(meta, indent=2, sort_keys=True) + "\n",
        )

    def read_output(self, packet_id: str) -> str:
        path = self.runtime / f"{packet_id}.output.log"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def append_output(self, packet_id: str, text: str) -> None:
        path = self.runtime / f"{packet_id}.output.log"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(redact(text))

    def append_raw(self, packet_id: str, text: str) -> None:
        """Private runner stream. Not copied to the outbox."""
        path = self.runtime / f"{packet_id}.raw.log"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(redact(text))

    def write_prompt(self, packet_id: str, prompt: str) -> None:
        atomic_write_text(self.runtime / f"{packet_id}.prompt.md", prompt)
        atomic_write_text(self.outbox / f"{packet_id}.prompt.md", redact(prompt))

    def publish_terminal_files(self, packet: dict[str, Any], output: str) -> None:
        packet_id = str(packet["id"])
        stored = _redact_model_fields(packet)
        model = {
            "summary": (stored.get("result") or {}).get("summary")
            if isinstance(stored.get("result"), dict)
            else None,
            "retro": stored.get("retro"),
        }
        atomic_write_text(
            self.runtime / f"{packet_id}.model.json",
            json.dumps(model, ensure_ascii=True) + "\n",
        )
        atomic_write_text(self.outbox / f"{packet_id}.json", emit_stored(stored))
        prompt_path = self.runtime / f"{packet_id}.prompt.md"
        if prompt_path.is_file():
            atomic_write_text(
                self.outbox / f"{packet_id}.prompt.md",
                redact(prompt_path.read_text(encoding="utf-8")),
            )
        elif not (self.outbox / f"{packet_id}.prompt.md").is_file():
            atomic_write_text(
                self.outbox / f"{packet_id}.prompt.md",
                "(runner was not started)\n",
            )
        atomic_write_text(self.outbox / f"{packet_id}.output.log", redact(output))
        for location in ("inbox", "claimed", "running", "needs-approval"):
            self.remove_packet(location, packet_id)

    def is_terminal_status(self, status: str) -> bool:
        return status in TERMINAL


def _packet_stem(path: Path) -> str:
    name = path.name
    for suffix in (".json", ".yaml", ".yml"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _loose_packet_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in {".json", ".yaml", ".yml"}
    ]


def _drop_yaml_shadows(directory: Path) -> None:
    """When the JSON copy exists, the YAML copy is not packet state."""
    json_stems = {
        _packet_stem(path)
        for path in _loose_packet_files(directory)
        if path.suffix.lower() == ".json"
    }
    for path in _loose_packet_files(directory):
        if path.suffix.lower() in {".yaml", ".yml"} and _packet_stem(path) in json_stems:
            path.unlink(missing_ok=True)


def _redact_model_fields(packet: dict[str, Any]) -> dict[str, Any]:
    result = packet.get("result")
    if isinstance(result, dict) and isinstance(result.get("summary"), str):
        result["summary"] = redact(result["summary"])
    retro = packet.get("retro")
    if isinstance(retro, dict):
        for key in ("effort", "unclear", "would_change"):
            if isinstance(retro.get(key), str):
                retro[key] = redact(retro[key])
    return packet
