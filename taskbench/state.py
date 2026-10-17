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
from taskbench.task_format import TERMINAL, emit_yaml, parse_yaml

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
        line = f"{now_iso()} {message}\n"
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
            for path in self.location_dir(location).glob("*.yaml"):
                found.setdefault(path.stem, []).append(location)
        for packet_id, locations in found.items():
            if len(locations) < 2:
                continue
            keep = max(locations, key=lambda item: RANK[item])
            for location in locations:
                if location == keep:
                    continue
                path = self.location_dir(location) / f"{packet_id}.yaml"
                path.unlink(missing_ok=True)
                notes.append(f"recover {packet_id}: kept {keep}, removed {location}")
        for note in notes:
            self.log(note)
        return notes

    def iter_packets(self) -> list[tuple[str, dict[str, Any]]]:
        items: list[tuple[str, dict[str, Any]]] = []
        for location in ("inbox", "claimed", "running", "needs-approval", "outbox"):
            for path in sorted(self.location_dir(location).glob("*.yaml")):
                try:
                    data = parse_yaml(path.read_text(encoding="utf-8"))
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
            for path in self.location_dir(location).glob("*.yaml"):
                if path.resolve() == ignore_resolved:
                    continue
                try:
                    data = parse_yaml(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("id") == packet_id:
                    return True
        return False

    def inbox_files(self) -> list[Path]:
        files = list(self.inbox.glob("*.yaml")) + list(self.inbox.glob("*.yml"))
        return sorted(files, key=lambda path: path.name)

    def write_packet(self, location: str, packet: dict[str, Any]) -> None:
        path = self.location_dir(location) / f"{packet['id']}.yaml"
        atomic_write_text(path, emit_yaml(packet))

    def remove_packet(self, location: str, packet_id: str) -> None:
        path = self.location_dir(location) / f"{packet_id}.yaml"
        path.unlink(missing_ok=True)

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
            handle.write(text)

    def write_prompt(self, packet_id: str, prompt: str) -> None:
        atomic_write_text(self.runtime / f"{packet_id}.prompt.md", prompt)
        atomic_write_text(self.outbox / f"{packet_id}.prompt.md", prompt)

    def publish_terminal_files(self, packet: dict[str, Any], output: str) -> None:
        packet_id = str(packet["id"])
        atomic_write_text(self.outbox / f"{packet_id}.yaml", emit_yaml(packet))
        prompt_path = self.runtime / f"{packet_id}.prompt.md"
        if prompt_path.is_file():
            atomic_write_text(
                self.outbox / f"{packet_id}.prompt.md",
                prompt_path.read_text(encoding="utf-8"),
            )
        elif not (self.outbox / f"{packet_id}.prompt.md").is_file():
            atomic_write_text(
                self.outbox / f"{packet_id}.prompt.md",
                "(runner was not started)\n",
            )
        atomic_write_text(self.outbox / f"{packet_id}.output.log", output)
        for location in ("inbox", "claimed", "running", "needs-approval"):
            self.remove_packet(location, packet_id)

    def is_terminal_status(self, status: str) -> bool:
        return status in TERMINAL
