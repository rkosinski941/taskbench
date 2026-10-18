"""Load taskbench configuration.

There is no bypass, yolo, or auto-approve key. ``bind_host`` must be
``127.0.0.1``. ``allowed_hosts`` is empty unless the operator opts into a
tailnet hostname for the Host-header check. The page still listens only on
loopback; Tailscale Serve is what publishes it onto the tailnet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from taskbench.permissions import BANNED_CONFIG_KEYS, PermissionFlagError, check_permission_args

BANNED_KEYS = {
    "yolo",
    "bypass",
    "approve_all",
    "approve-all",
    "auto_approve",
    "auto-approve",
    "always_approve",
    "always-approve",
    "allow",
    "full_auto",
    "full-auto",
    "dangerously_skip_permissions",
    "dangerously-skip-permissions",
    "no_sandbox",
    "no-sandbox",
    "allow_all",
    "skip_approval",
    "skip-approval",
    "unrestricted",
    "funnel",
    "permission_mode",
    "permission-mode",
    "bypass_permissions",
    "bypass-permissions",
} | {key.replace("-", "_") for key in BANNED_CONFIG_KEYS}

BANNED_ARG_PARTS = (
    "yolo",
    "bypass",
    "full-auto",
    "full_auto",
    "dangerously",
    "approve-all",
    "approve_all",
    "no-sandbox",
    "no_sandbox",
    "skip-approval",
    "skip_approval",
)


class ConfigError(ValueError):
    pass


@dataclass
class RunnerConfig:
    name: str
    adapter: str
    executable: str
    args_template: list[str]
    can_ask: bool = False
    sandbox_only: bool = False


@dataclass
class Config:
    bind_host: str
    port: int
    allowed_hosts: list[str]
    human_owner: str
    watcher_name: str
    data_dir: Path
    projects_dir: Path
    default_timeout_s: int = 900
    max_timeout_s: int = 7200
    poll_interval_s: float = 0.2
    runners: dict[str, RunnerConfig] = field(default_factory=dict)
    config_path: Path | None = None
    remote_approvals: bool = False


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    _reject_banned(raw)
    base = path.resolve().parent
    bind_host = raw.get("bind_host", "127.0.0.1")
    if bind_host != "127.0.0.1":
        raise ConfigError("bind_host must be 127.0.0.1")
    port = raw.get("port", 8765)
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ConfigError("port must be an integer from 1 to 65535")
    allowed = raw.get("allowed_hosts", [])
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        raise ConfigError("allowed_hosts must be a list of strings")
    human = raw.get("human_owner", "Rich")
    if not isinstance(human, str) or not human.strip():
        raise ConfigError("human_owner must be a non-empty string")
    watcher = raw.get("watcher_name", "taskbench-local")
    if not isinstance(watcher, str) or not watcher.strip():
        raise ConfigError("watcher_name must be a non-empty string")
    data_dir = _resolve_dir(base, raw.get("data_dir", "var"), "data_dir")
    projects_dir = _resolve_dir(base, raw.get("projects_dir", "projects"), "projects_dir")
    runners = _load_runners(raw.get("runners", {}))
    remote = raw.get("remote_approvals", False)
    if remote is not True and remote is not False:
        raise ConfigError("remote_approvals must be a boolean")
    loaded = Config(
        bind_host=bind_host,
        port=port,
        allowed_hosts=list(allowed),
        human_owner=human.strip(),
        watcher_name=watcher.strip(),
        data_dir=data_dir,
        projects_dir=projects_dir,
        config_path=path.resolve(),
        runners=runners,
        remote_approvals=remote is True,
    )
    return loaded


def _resolve_dir(base: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a path string")
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path


def _load_runners(raw: object) -> dict[str, RunnerConfig]:
    if not isinstance(raw, dict):
        raise ConfigError("runners must be an object")
    runners: dict[str, RunnerConfig] = {}
    for name, body in raw.items():
        if not isinstance(body, dict):
            raise ConfigError(f"runner {name} must be an object")
        _reject_banned(body)
        adapter = body.get("adapter", "")
        executable = body.get("executable", "")
        args = body.get("args_template", [])
        if not isinstance(adapter, str) or not adapter:
            raise ConfigError(f"runner {name} needs an adapter")
        if not isinstance(executable, str) or not executable:
            raise ConfigError(f"runner {name} needs an executable")
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ConfigError(f"runner {name} args_template must be a list of strings")
        try:
            check_permission_args(list(args))
        except PermissionFlagError as exc:
            raise ConfigError(f"runner {name} args_template: {exc}") from exc
        for arg in args:
            lowered = arg.lower()
            if any(part in lowered for part in BANNED_ARG_PARTS):
                raise ConfigError(f"runner {name} args_template contains a banned flag")
        default_can_ask = False
        runners[name] = RunnerConfig(
            name=name,
            adapter=adapter,
            executable=executable,
            args_template=list(args),
            can_ask=bool(body.get("can_ask", default_can_ask)),
            sandbox_only=bool(body.get("sandbox_only", False)),
        )
    return runners


def _reject_banned(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower().replace("-", "_") in {item.replace("-", "_") for item in BANNED_KEYS}:
                raise ConfigError(f"config key '{key}' is not allowed")
            _reject_banned(item)
    elif isinstance(value, list):
        for item in value:
            _reject_banned(item)
