"""The only place that starts the Grok / runner process.

Every launch re-checks the workdir, scans allow rules, pins the command,
and on Windows points Grok at an empty config home outside the workdir.
``XAI_API_KEY`` is added only for that Grok process. A context flag is set
for the duration of the spawn so a runner started any other way is refused.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from collections.abc import Iterator
from pathlib import Path

from taskbench.adapters.base import Adapter
from taskbench.config import Config
from taskbench.permissions import (
    PermissionFlagError,
    needs_you_message,
    prepare_grok_config_home,
    scan_project_permission_rules,
    seed_grok_credentials,
)
from taskbench.process_tree import ManagedProcess
from taskbench.redact import drop_from_env, remember_secret
from taskbench.workdir import WorkdirError, same_canonical, validate_workdir

_GUARD: contextvars.ContextVar[bool] = contextvars.ContextVar("taskbench_runner_guard", default=False)


class LaunchRefused(Exception):
    """The guarded launch refused to start a process."""


def runner_guard_active() -> bool:
    """True only while ``spawn_runner`` is inside its spawn call."""
    return bool(_GUARD.get())


@contextlib.contextmanager
def runner_guard() -> Iterator[None]:
    """Mark the current context as the guarded spawn.

    Production code sets this only from ``spawn_runner``, after the workdir
    re-check and the allow-rule scan. Process-confinement tests may set it
    when they start the runner fixture to prove the tree kill.
    """
    token = _GUARD.set(True)
    try:
        yield
    finally:
        _GUARD.reset(token)


def argv_is_runner(argv: list[str]) -> bool:
    """True when ``argv`` is the Grok executable or the taskbench test runner."""
    for part in argv:
        name = Path(str(part)).name.lower()
        if name in {"grok", "grok.exe", "grok.cmd"}:
            return True
        if "fake_runner" in name:
            return True
    return False


def spawn_runner(
    *,
    adapter: Adapter,
    folder: Path,
    prompt_file: Path,
    timeout_s: int,
    base_env: dict[str, str],
    config: Config,
) -> tuple[ManagedProcess, Path | None]:
    """Start the runner. This is the only supported spawn path."""
    config_dir = None
    if config.config_path is not None:
        config_dir = config.config_path.parent
    try:
        confirmed = validate_workdir(
            str(folder),
            state_dir=config.data_dir,
            config_dir=config_dir,
        )
    except WorkdirError as exc:
        raise LaunchRefused(str(exc)) from exc
    if not same_canonical(confirmed, folder):
        raise LaunchRefused("workdir changed before launch")
    findings = scan_project_permission_rules(confirmed)
    if findings:
        raise LaunchRefused(needs_you_message(findings))
    try:
        argv = adapter.build_command(
            prompt_file=prompt_file,
            project_dir=confirmed,
            timeout_s=timeout_s,
        )
    except (NotImplementedError, ValueError) as exc:
        raise LaunchRefused(str(exc)) from exc
    config_home: Path | None = None
    env = adapter.environment(base_env)
    if adapter.name == "grok-build":
        home = config.data_dir / "grok-home"
        if os.name == "nt":
            try:
                config_home = prepare_grok_config_home(config.data_dir)
            except PermissionFlagError as exc:
                raise LaunchRefused(str(exc)) from exc
            home = config_home
            _assert_home_outside(home, confirmed)
            seed_grok_credentials(config_home, base_env)
            env = adapter.environment(base_env, config_home=config_home)
        else:
            _assert_home_outside(home, confirmed)
        key = (base_env.get("XAI_API_KEY") or "").strip()
        if key:
            remember_secret(key)
    env = drop_from_env(env)
    if adapter.name == "grok-build":
        key = (base_env.get("XAI_API_KEY") or "").strip()
        if key:
            # Added after the scrub so the key reaches Grok and nobody else.
            env["XAI_API_KEY"] = key
    with runner_guard():
        proc = ManagedProcess(argv, confirmed, env)
    return proc, config_home


def _assert_home_outside(home: Path, workdir: Path) -> None:
    """The pinned Grok home must not overlap the task folder."""
    try:
        home_real = Path(os.path.realpath(home))
        work_real = Path(os.path.realpath(workdir))
    except OSError as exc:
        raise LaunchRefused("grok config home overlaps the workdir") from exc
    if home_real == work_real or _nested(home_real, work_real) or _nested(work_real, home_real):
        raise LaunchRefused("grok config home overlaps the workdir")


def _nested(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return child.resolve() != parent.resolve()
