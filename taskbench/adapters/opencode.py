"""v2 stub. opencode is not implemented in taskbench v1."""

from __future__ import annotations

from pathlib import Path

from taskbench.adapters.base import Adapter


class OpenCodeAdapter(Adapter):
    name = "opencode"
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
            "opencode is planned for v2 and is not implemented in taskbench v1."
        )
