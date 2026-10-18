"""Adapter registry. Only Grok Build can launch in v1."""

from __future__ import annotations

from taskbench.adapters.antigravity import AntigravityAdapter
from taskbench.adapters.base import Adapter
from taskbench.adapters.grok_build import GrokBuildAdapter
from taskbench.adapters.opencode import OpenCodeAdapter
from taskbench.config import Config, RunnerConfig


def get_adapter(requested: str, config: Config) -> Adapter:
    if requested == "grok-build":
        runner = config.runners.get("grok-build")
        if runner is None or runner.adapter != "grok_build":
            return _Missing("grok-build")
        return GrokBuildAdapter(runner)
    if requested == "antigravity":
        return AntigravityAdapter()
    if requested == "opencode":
        return OpenCodeAdapter()
    return _Missing(requested)


class _Missing(Adapter):
    implemented = False
    can_ask = False

    def __init__(self, name: str) -> None:
        self.name = name

    def build_command(self, *, prompt_file, project_dir, timeout_s):  # type: ignore[no-untyped-def]
        raise NotImplementedError(f"runner not configured: {self.name}")


def configured_names(config: Config) -> list[str]:
    return [name for name, runner in config.runners.items() if isinstance(runner, RunnerConfig)]
