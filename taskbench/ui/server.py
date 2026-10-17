"""Loopback web page for taskbench.

The server refuses every bind address except 127.0.0.1. DNS rebinding is
rejected by an allow-list of Host values: loopback names, plus
``allowed_hosts`` from config (empty by default, meant for a tailnet
hostname when Tailscale Serve is in front). CSRF is a per-session cookie
checked against a form field.

``Tailscale-User-Login`` is shown when present and is never used for
authorization or to relabel a task. Trusting it depends on the listener
staying on loopback. If the process were bound to any other address, that
header could be forged and the display would be wrong.
"""

from __future__ import annotations

import html
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from taskbench.atomic import atomic_write_text
from taskbench.config import Config
from taskbench.projects import now_iso
from taskbench.safety import approval_title
from taskbench.state import StateStore
from taskbench.task_format import emit_yaml
from taskbench.watcher import Watcher, queue_view

COOKIE = "taskbench_csrf"
MAX_BODY = 2_000_000


class BindError(ValueError):
    """Raised when a caller asks taskbench to listen on anything but loopback."""


def ensure_loopback(host: str) -> None:
    if host != "127.0.0.1":
        raise BindError(f"refusing to bind {host!r}; taskbench listens on 127.0.0.1 only")


def host_allowed(host_header: str | None, port: int, allowed_hosts: list[str]) -> bool:
    if not host_header:
        return False
    text = host_header.strip()
    if not text or " " in text or "/" in text:
        return False
    permitted = {f"127.0.0.1:{port}", "127.0.0.1", f"localhost:{port}", "localhost"}
    for extra in allowed_hosts:
        name = extra.strip()
        if not name or "/" in name or " " in name:
            continue
        permitted.add(name)
        permitted.add(f"{name}:{port}")
    folded = {item.casefold() for item in permitted}
    return text.casefold() in folded


class TaskbenchServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, app: App, port: int) -> None:
        ensure_loopback(app.config.bind_host)
        super().__init__((app.config.bind_host, port), Handler)
        bound_host = self.server_address[0]
        if bound_host != "127.0.0.1":
            self.server_close()
            raise BindError(f"socket bound to {bound_host!r}, expected 127.0.0.1")
        self.app = app

    def server_bind(self) -> None:
        super().server_bind()
        host = self.server_address[0]
        if host != "127.0.0.1":
            raise BindError(f"socket bound to {host!r}, expected 127.0.0.1")


class App:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = StateStore(config.data_dir)
        self.watcher = Watcher(config, self.store)


class Handler(BaseHTTPRequestHandler):
    server: TaskbenchServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if not self._check_host():
            return
        token, fresh = self._session_token()
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            body = render_index(self.server.app, token, self._tailscale_user()).encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body, token if fresh else None)
            return
        if path.startswith("/task/"):
            packet_id = path[len("/task/") :]
            self._task_page(packet_id, token, fresh)
            return
        if path.startswith("/outbox/"):
            self._outbox_file(path[len("/outbox/") :])
            return
        if path.startswith("/artifact/"):
            self._artifact(path[len("/artifact/") :])
            return
        self._send(404, "text/html; charset=utf-8", _not_found(), None)

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_host():
            return
        if not self._check_origin():
            return
        length = int(self.headers.get("Content-Length") or "0")
        if length < 0 or length > MAX_BODY:
            self._send(413, "text/plain; charset=utf-8", b"body too large", None)
            return
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        form = {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}
        token, _fresh = self._session_token()
        cookie = _cookie(self.headers.get("Cookie"), COOKIE)
        supplied = form.get("csrf", "")
        if not cookie or not secrets.compare_digest(cookie, supplied):
            self._send(403, "text/plain; charset=utf-8", b"CSRF check failed", None)
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/tasks":
            self._create_task(form)
            return
        if path.startswith("/task/") and path.endswith("/approve"):
            packet_id = path[len("/task/") : -len("/approve")].strip("/")
            self._decide(packet_id, "approved")
            return
        if path.startswith("/task/") and path.endswith("/deny"):
            packet_id = path[len("/task/") : -len("/deny")].strip("/")
            self._decide(packet_id, "denied")
            return
        if path.startswith("/task/") and path.endswith("/cancel"):
            packet_id = path[len("/task/") : -len("/cancel")].strip("/")
            self.server.app.watcher.request_cancel(packet_id)
            self._redirect()
            return
        self._send(404, "text/html; charset=utf-8", _not_found(), None)

    def _create_task(self, form: dict[str, str]) -> None:
        # Posted `from` is ignored. The page is the local human, labeled web-ui.
        goal = form.get("goal", "").strip()
        project = form.get("project", "").strip()
        runner = form.get("runner", "").strip()
        permissions = form.get("permissions", "ask").strip() or "ask"
        if permissions not in {"plan", "ask", "allow-list"}:
            permissions = "ask"
        allow_text = form.get("allow", "")
        timeout_raw = form.get("timeout_s", "").strip()
        error = _validate_form(
            goal, project, runner, permissions, allow_text, timeout_raw, self.server.app.config
        )
        if error:
            body = f"<p>{html.escape(error)}</p><p><a href='/'>Back</a></p>".encode("utf-8")
            self._send(400, "text/html; charset=utf-8", body, None)
            return
        timeout_s = int(timeout_raw) if timeout_raw else 900
        packet: dict[str, Any] = {
            "schema": "joy-feeder.task/v2",
            "id": "web-" + secrets.token_hex(6),
            "created": now_iso(),
            "from": "web-ui",
            "runner": {"requested": runner, "actual": None},
            "model": {"requested": None, "actual": None},
            "project": project,
            "permissions": permissions,
            "timeout_s": timeout_s,
            "goal": goal,
            "status": "incoming",
            "retro": None,
            "result": None,
        }
        if permissions == "allow-list":
            packet["allow"] = [line.strip() for line in allow_text.splitlines() if line.strip()]
        path = self.server.app.store.inbox / f"{packet['id']}.yaml"
        atomic_write_text(path, emit_yaml(packet))
        self._redirect()

    def _decide(self, packet_id: str, decision: str) -> None:
        if not _safe_id(packet_id):
            self._send(404, "text/html; charset=utf-8", _not_found(), None)
            return
        # The command is the one already stored. A posted command is ignored.
        self.server.app.watcher.request_decision(packet_id, decision)
        self._redirect()

    def _task_page(self, packet_id: str, token: str, fresh: bool) -> None:
        if not _safe_id(packet_id):
            self._send(404, "text/html; charset=utf-8", _not_found(), None)
            return
        found = self.server.app.store.find(packet_id)
        if found is None:
            self._send(404, "text/html; charset=utf-8", _not_found(), None)
            return
        _location, packet = found
        body = render_task(self.server.app, packet, token, self._tailscale_user()).encode("utf-8")
        self._send(200, "text/html; charset=utf-8", body, token if fresh else None)

    def _outbox_file(self, name: str) -> None:
        packet_id, suffix = _split_outbox_name(name)
        if packet_id is None or suffix is None:
            self._send(404, "text/plain; charset=utf-8", b"not found", None)
            return
        path = self.server.app.store.outbox / f"{packet_id}{suffix}"
        if not path.is_file():
            self._send(404, "text/plain; charset=utf-8", b"not found", None)
            return
        data = path.read_bytes()
        self._send(200, "text/plain; charset=utf-8", data, None)

    def _artifact(self, remainder: str) -> None:
        if "/" not in remainder:
            self._send(404, "text/plain; charset=utf-8", b"not found", None)
            return
        packet_id, relative = remainder.split("/", 1)
        found = self.server.app.store.find(packet_id) if _safe_id(packet_id) else None
        if found is None:
            self._send(404, "text/html; charset=utf-8", _not_found(), None)
            return
        _location, packet = found
        project = packet.get("project")
        if not isinstance(project, str):
            self._send(404, "text/plain; charset=utf-8", b"not found", None)
            return
        from taskbench.safety import PathEscape, resolve_inside_project

        folder = self.server.app.config.projects_dir / project
        try:
            path = resolve_inside_project(folder, relative)
        except PathEscape:
            self._send(400, "text/plain; charset=utf-8", b"path escapes project", None)
            return
        if not path.is_file():
            self._send(404, "text/plain; charset=utf-8", b"not found", None)
            return
        self._send(200, "text/plain; charset=utf-8", path.read_bytes(), None)

    def _check_host(self) -> bool:
        port = int(self.server.server_address[1])
        if host_allowed(self.headers.get("Host"), port, self.server.app.config.allowed_hosts):
            return True
        self._send(400, "text/plain; charset=utf-8", b"rejected host", None)
        return False

    def _check_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = int(self.server.server_address[1])
        allowed = {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }
        for extra in self.server.app.config.allowed_hosts:
            name = extra.strip()
            if name:
                allowed.add(f"http://{name}:{port}")
        if origin.rstrip("/") in {item.rstrip("/") for item in allowed} or origin in allowed:
            return True
        self._send(403, "text/plain; charset=utf-8", b"rejected origin", None)
        return False

    def _tailscale_user(self) -> str | None:
        value = self.headers.get("Tailscale-User-Login")
        if not value:
            return None
        text = value.strip()
        if not text or len(text) > 200:
            return None
        return text

    def _session_token(self) -> tuple[str, bool]:
        existing = _cookie(self.headers.get("Cookie"), COOKIE)
        if existing and len(existing) >= 16:
            return existing, False
        return secrets.token_urlsafe(32), True

    def _redirect(self) -> None:
        body = b"See other"
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, status: int, content_type: str, body: bytes, set_cookie: str | None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if set_cookie:
            self.send_header(
                "Set-Cookie",
                f"{COOKIE}={set_cookie}; HttpOnly; SameSite=Strict; Path=/",
            )
        self.end_headers()
        self.wfile.write(body)


def render_index(app: App, token: str, tailscale_user: str | None) -> str:
    packets = app.store.iter_packets()
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in _STATUS_ORDER}
    for _location, packet in packets:
        status = str(packet.get("status") or "")
        grouped.setdefault(status, []).append(packet)
    queue = {item["id"]: item for item in queue_view(app.store)}
    parts = [_page_start(tailscale_user), _composer(app, token), ""]
    for status in _STATUS_ORDER:
        parts.append(f"<section class='group' id='{status}'><h2>{html.escape(status)}</h2>")
        items = grouped.get(status) or []
        if not items:
            parts.append("<p class='empty'>none</p>")
        for packet in items:
            parts.append(_card(app, packet, token, queue.get(packet.get("id"))))
        parts.append("</section>")
    parts.append(_page_end())
    return "".join(parts)


def render_task(app: App, packet: dict[str, Any], token: str, tailscale_user: str | None) -> str:
    parts = [
        _page_start(tailscale_user),
        f"<p><a href='/'>All tasks</a></p>",
        _card(app, packet, token, None),
        _page_end(),
    ]
    return "".join(parts)


def _composer(app: App, token: str) -> str:
    projects = _projects(app.config)
    runners = list(app.config.runners)
    project_options = "".join(
        f"<option value='{html.escape(name)}'>{html.escape(name)}</option>" for name in projects
    )
    runner_options = "".join(
        f"<option value='{html.escape(name)}'>{html.escape(name)}</option>" for name in runners
    )
    return (
        "<form method='post' action='/tasks'>"
        f"<input type='hidden' name='csrf' value='{html.escape(token)}'>"
        "<p>New task as <strong data-human-label='web-ui'>web-ui</strong></p>"
        f"<label>Project <select name='project'>{project_options}</select></label> "
        f"<label>Runner <select name='runner'>{runner_options}</select></label> "
        "<label>Permissions <select name='permissions'>"
        "<option value='ask' selected>ask</option>"
        "<option value='plan'>plan</option>"
        "<option value='allow-list'>allow-list</option>"
        "</select></label> "
        "<label>Timeout seconds <input name='timeout_s' value='900'></label>"
        "<p><label>Allow patterns (allow-list only)<br>"
        "<textarea name='allow' rows='3'></textarea></label></p>"
        "<p><label>Task<br><textarea name='goal' rows='8' required></textarea></label></p>"
        "<button type='submit'>Queue task</button></form>"
    )


def _card(app: App, packet: dict[str, Any], token: str, queued: dict[str, Any] | None) -> str:
    packet_id = str(packet.get("id") or "")
    sender = str(packet.get("from") or "")
    project = str(packet.get("project") or "")
    status = str(packet.get("status") or "")
    requested = _nested(packet, "model", "requested")
    actual = _nested(packet, "model", "actual")
    mismatch = bool(requested) and isinstance(actual, str) and actual != requested
    parts = [
        "<article class='card'",
        f" data-id='{html.escape(packet_id)}'",
        f" data-from='{html.escape(sender)}'",
        f" data-project='{html.escape(project)}'",
        f" data-status='{html.escape(status)}'>",
        f"<h3>{html.escape(packet_id)}</h3>",
        f"<p class='from'>From: {html.escape(sender)}</p>",
        f"<p>Project: {html.escape(project)}</p>",
        f"<p>Status: {html.escape(status)}</p>",
        "<p class='model'>requested <span data-model-requested='",
        html.escape("" if requested is None else str(requested)),
        "'>",
        html.escape("" if requested is None else str(requested)),
        "</span> actual <span data-model-actual='",
        html.escape(_actual_label(actual)),
        "'>",
        html.escape(_actual_label(actual)),
        "</span>",
    ]
    if mismatch:
        parts.append(" <span class='mismatch'>mismatch</span>")
    parts.append("</p>")
    if queued:
        parts.append(
            f"<p class='queue'>position {queued['position']}, waiting {queued['wait_s']}s</p>"
        )
    goal = packet.get("goal") or ""
    parts.append(f"<pre class='goal'>{html.escape(str(goal)[:2000])}</pre>")
    reason = _nested_result(packet, "blocked_reason")
    if reason:
        parts.append(f"<p class='reason'>{html.escape(str(reason))}</p>")
    if status == "running":
        parts.append(
            "<form method='post' action='/task/"
            + html.escape(packet_id)
            + "/cancel'>"
            f"<input type='hidden' name='csrf' value='{html.escape(token)}'>"
            "<button type='submit'>Cancel</button></form>"
        )
    if status == "needs-approval":
        meta = app.store.read_meta(packet_id)
        command = str(meta.get("pending_command") or _pending_command(packet))
        diff = str(meta.get("pending_diff") or "")
        title = approval_title(command)
        parts.append(f"<h3 class='approval-title'>{html.escape(title)}</h3>")
        parts.append(f"<pre class='command'>{html.escape(command)}</pre>")
        if diff:
            parts.append(f"<pre class='diff'>{html.escape(diff)}</pre>")
        for action, label in (("approve", "Approve"), ("deny", "Deny")):
            parts.append(
                "<form method='post' action='/task/"
                + html.escape(packet_id)
                + f"/{action}'>"
                f"<input type='hidden' name='csrf' value='{html.escape(token)}'>"
                f"<button type='submit'>{label}</button></form>"
            )
    parts.append("<ul class='files'>")
    for suffix, label in (
        (".yaml", "packet"),
        (".prompt.md", "prompt"),
        (".output.log", "output"),
    ):
        file_path = app.store.outbox / f"{packet_id}{suffix}"
        if file_path.is_file():
            href = f"/outbox/{packet_id}{suffix}"
            parts.append(f"<li><a href='{html.escape(href)}'>{label}</a></li>")
    artifacts = _nested_result(packet, "artifacts") or []
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if isinstance(artifact, str):
                href = f"/artifact/{packet_id}/{artifact}"
                parts.append(f"<li><a href='{html.escape(href)}'>{html.escape(artifact)}</a></li>")
    parts.append("</ul>")
    preview = app.store.read_meta(packet_id).get("preview_url")
    if isinstance(preview, str) and preview.startswith("http://127.0.0.1:"):
        parts.append(f"<p class='preview-link'><a href='{html.escape(preview)}'>{html.escape(preview)}</a></p>")
    parts.append(f"<p><a href='/task/{html.escape(packet_id)}'>task link</a></p>")
    parts.append("</article>")
    return "".join(parts)


def _page_start(tailscale_user: str | None) -> str:
    who = ""
    if tailscale_user:
        who = (
            "<p class='tailscale' data-tailscale-user='"
            + html.escape(tailscale_user)
            + "'>Tailscale user: "
            + html.escape(tailscale_user)
            + "</p>"
        )
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<title>taskbench</title><style>"
        "body{font-family:sans-serif;margin:1.5rem;max-width:52rem}"
        "textarea{width:100%;} .card{border:1px solid #ccc;padding:.75rem;margin:.5rem 0}"
        "pre.command,pre.diff{max-height:12rem;overflow:auto;white-space:pre-wrap;"
        "word-break:break-word;background:#f6f6f6;padding:.5rem}"
        "</style></head><body><h1>taskbench</h1>"
        "<p>Local runner. Artifacts are files. There is no preview pane.</p>"
        + who
    )


def _page_end() -> str:
    return "</body></html>"


def _not_found() -> bytes:
    return b"<!DOCTYPE html><html><body><h1>not found</h1></body></html>"


def _projects(config: Config) -> list[str]:
    root = config.projects_dir
    if not root.is_dir():
        return []
    names = []
    for path in sorted(root.iterdir()):
        if path.is_dir() and not path.name.startswith("."):
            names.append(path.name)
    return names


def _validate_form(
    goal: str,
    project: str,
    runner: str,
    permissions: str,
    allow_text: str,
    timeout_raw: str,
    config: Config,
) -> str | None:
    if not goal:
        return "goal is required"
    if project not in _projects(config):
        return "unknown project"
    if runner not in config.runners:
        return "unknown runner"
    if permissions == "allow-list" and not any(line.strip() for line in allow_text.splitlines()):
        return "allow-list requires allow"
    if timeout_raw:
        if not timeout_raw.isdigit() or not (1 <= int(timeout_raw) <= 7200):
            return "invalid timeout_s"
    return None


def _actual_label(actual: object) -> str:
    if actual is None:
        return "not reported yet"
    return str(actual)


def _nested(packet: dict[str, Any], key: str, child: str) -> object:
    value = packet.get(key)
    if not isinstance(value, dict):
        return None
    return value.get(child)


def _nested_result(packet: dict[str, Any], child: str) -> object:
    return _nested(packet, "result", child)


def _pending_command(packet: dict[str, Any]) -> str:
    approvals = packet.get("approvals") or []
    if isinstance(approvals, list):
        for entry in reversed(approvals):
            if isinstance(entry, dict) and entry.get("decision") == "pending":
                return str(entry.get("command") or "")
    return ""


def _safe_id(packet_id: str) -> bool:
    import re

    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,140}", packet_id) is not None


def _split_outbox_name(name: str) -> tuple[str | None, str | None]:
    for suffix in (".prompt.md", ".output.log", ".yaml"):
        if name.endswith(suffix):
            packet_id = name[: -len(suffix)]
            if _safe_id(packet_id):
                return packet_id, suffix
    return None, None


def _cookie(header: str | None, name: str) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip() == name:
            return value.strip()
    return None


_STATUS_ORDER = (
    "incoming",
    "claimed",
    "running",
    "needs-approval",
    "done",
    "failed",
    "blocked",
    "timed-out",
)


def serve(app: App, port: int | None = None) -> TaskbenchServer:
    chosen = app.config.port if port is None else port
    return TaskbenchServer(app, chosen)
