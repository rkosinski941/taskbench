"""v2 stub. Antigravity is not implemented in taskbench v1."""

from __future__ import annotations

from pathlib import Path

from taskbench.adapters.base import Adapter


class AntigravityAdapter(Adapter):
    name = "antigravity"
    implemented = False
    can_ask = False

    def build_command(
        self,
        *,
        prompt_file: Path,
        project_dir: Path,
        timeout_s: int,
    ) -> list[str]:
        raise NotImplementedError(
            "Antigravity is planned for v2 and is not implemented in taskbench v1."
        )
