"""Command line. ``python -m taskbench`` does not install anything."""

from __future__ import annotations

import argparse
import sys
import threading
import traceback
from pathlib import Path

from taskbench import __version__
from taskbench.config import ConfigError, load_config
from taskbench.ui.server import App, serve


def require_python(version: tuple[int, int] | None = None) -> None:
    """tomllib, used for Grok's TOML settings, needs Python 3.11 or newer."""
    current = version if version is not None else sys.version_info[:2]
    if current < (3, 11):
        raise SystemExit(f"taskbench requires Python 3.11 or newer (this is {current[0]}.{current[1]})")


def main(argv: list[str] | None = None) -> int:
    require_python()
    parser = argparse.ArgumentParser(prog="taskbench", description="Local task runner")
    parser.add_argument("--config", default="config.json", help="path to config.json")
    parser.add_argument("--version", action="store_true")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "watch", "serve", "check"),
    )
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    path = Path(args.config)
    if not path.is_file():
        print(f"config not found: {path}", file=sys.stderr)
        return 2
    try:
        config = load_config(path)
    except (OSError, ConfigError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.command == "check":
        print(f"bind {config.bind_host}:{config.port}")
        print(f"projects {config.projects_dir}")
        print("runners " + ", ".join(config.runners) if config.runners else "runners (none)")
        return 0
    app = App(config)
    if args.command == "serve":
        _serve(app)
        return 0
    if args.command == "watch":
        _watch(app)
        return 0
    _run(app)
    return 0


def _watch(app: App) -> None:
    stop = threading.Event()

    def handle_stop(*_args: object) -> None:
        stop.set()
        app.watcher.shutdown()

    import signal

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    while not stop.is_set():
        try:
            app.watcher.tick()
        except Exception:
            traceback.print_exc()
        stop.wait(app.config.poll_interval_s)


def _serve(app: App) -> None:
    server = serve(app)
    print(f"taskbench {app.config.bind_host}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _run(app: App) -> None:
    thread = threading.Thread(target=_watch, args=(app,), name="taskbench-watcher", daemon=True)
    thread.start()
    _serve(app)
    app.watcher.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
