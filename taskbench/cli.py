"""Command line. ``python -m taskbench`` does not install anything.

Signal handlers are installed only on the main thread. The watcher and the
HTTP server run as workers and stop when the main thread sets a stop event.
If a worker dies, the process exits non-zero instead of serving a healthy page.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import traceback
from collections.abc import Callable
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
        return _serve(app)
    if args.command == "watch":
        return _watch(app)
    return _run(app)


def _install_signals(stop: threading.Event) -> None:
    """Own SIGINT, SIGTERM, and Windows Ctrl+Break on the main thread only."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("signal handlers are installed only on the main thread")

    def handle_stop(*_args: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, handle_stop)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, handle_stop)
        except (ValueError, OSError):
            pass
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, handle_stop)
        except (ValueError, OSError):
            pass


def _guarded(
    name: str,
    fn: Callable[[], None],
    app: App,
    stop: threading.Event,
) -> Callable[[], None]:
    def run() -> None:
        try:
            fn()
        except Exception:
            traceback.print_exc()
            app.note_worker_down(f"{name} thread died")
            stop.set()

    return run


def supervise(
    app: App,
    stop: threading.Event,
    workers: list[tuple[str, Callable[[], None]]],
    shutdown: Callable[[], None] | None = None,
) -> int:
    """Run workers until stop. A dead worker is a non-zero exit."""
    threads: list[threading.Thread] = []
    for name, fn in workers:
        thread = threading.Thread(
            target=_guarded(name, fn, app, stop),
            name=f"taskbench-{name}",
            daemon=True,
        )
        if name == "watcher":
            app.watcher_thread = thread
        thread.start()
        threads.append(thread)
    try:
        while not stop.is_set():
            if any(not thread.is_alive() for thread in threads):
                app.note_worker_down("worker thread died")
                stop.set()
                break
            stop.wait(0.05)
    finally:
        stop.set()
        if shutdown is not None:
            try:
                shutdown()
            except Exception:
                traceback.print_exc()
                app.note_worker_down("shutdown failed")
        for thread in threads:
            thread.join(timeout=5)
    return 1 if app.worker_down else 0


def _watch(app: App) -> int:
    """Tick on the main thread. This command does not start the page."""
    stop = threading.Event()
    _install_signals(stop)
    while not stop.is_set():
        try:
            app.watcher.tick()
        except Exception:
            traceback.print_exc()
            app.note_worker_down("watcher tick failed")
            stop.set()
            break
        stop.wait(app.config.poll_interval_s)
    app.watcher.shutdown()
    return 1 if app.worker_down else 0


def _serve(app: App) -> int:
    server = serve(app)
    port = int(server.server_address[1])
    _print_startup(app, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _print_startup(app: App, port: int) -> None:
    # stdout only. The one-time token is not written to the run log.
    from taskbench.ui.server import login_url

    print(login_url(app, port), flush=True)
    print(f"taskbench {app.config.bind_host}:{port}", flush=True)


def _run(app: App) -> int:
    """Page plus watcher. The main thread owns signals and shutdown."""
    stop = threading.Event()
    _install_signals(stop)
    server = serve(app)
    port = int(server.server_address[1])
    _print_startup(app, port)

    def watch() -> None:
        while not stop.is_set():
            try:
                app.watcher.tick()
            except Exception:
                traceback.print_exc()
            if stop.is_set():
                break
            stop.wait(app.config.poll_interval_s)

    def serve_loop() -> None:
        server.serve_forever(poll_interval=0.2)

    def shutdown() -> None:
        app.watcher.shutdown()
        server.shutdown()
        server.server_close()

    return supervise(
        app,
        stop,
        [("watcher", watch), ("http", serve_loop)],
        shutdown,
    )


if __name__ == "__main__":
    raise SystemExit(main())
