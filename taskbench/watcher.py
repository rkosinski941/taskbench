"""Claim inbox packets and run one of them at a time.

The watcher is the only component that changes ``status``. Closing the web
page does not call this code. Cancel, timeout, a normal exit, a failure, and
``shutdown`` all kill the runner's process tree. The timeout clock starts when
the run starts and keeps running through ``needs-approval``.

A packet left in ``running`` after a restart is marked ``failed`` (it is not
run a second time). A packet left in ``needs-approval`` stays there so the
human can still answer; the dead runner group is killed first.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from taskbench.adapters import get_adapter
from taskbench.adapters.base import Adapter, ProtocolError
from taskbench.config import Config
from taskbench.launch import LaunchRefused, spawn_runner
from taskbench.permissions import (
    needs_you_message,
    prefix_output,
    scan_project_permission_rules,
    scrub_environment,
    unexpected_permission,
    windows_sandbox_banner,
    wipe_grok_credentials,
)
from taskbench.process_tree import (
    ManagedProcess,
    ProcessTreeError,
    kill_process_group,
    listening_ports,
    pid_alive,
    read_cmdline,
)
from taskbench.projects import (
    compose_prompt,
    last_agent_path,
    load_instructions,
    now_iso,
    project_dir,
    read_last_agent,
    retro_followup_prompt,
    write_last_agent,
)
from taskbench.workdir import (
    WorkdirError,
    WorkdirLease,
    WorkdirTrustError,
    is_registered,
    same_canonical,
    validate_workdir,
)
from taskbench.safety import (
    classify,
    execute_readonly,
    execute_write,
    full_command,
    preview_url,
    render_diff,
    shell_argv,
)
from taskbench.state import StateStore
from taskbench.task_format import load_packet_text

RETRO_TIMEOUT_S = 30


class Watcher:
    def __init__(self, config: Config, store: StateStore | None = None) -> None:
        self.config = config
        self.store = store or StateStore(config.data_dir)
        self.proc: ManagedProcess | None = None
        self.shell_proc: ManagedProcess | None = None
        self.adapter: Adapter | None = None
        self.run_state: dict[str, Any] = {}
        self.deadline: float | None = None
        self._stop = False
        self._recovered = False
        self._lease: WorkdirLease | None = None
        wipe_grok_credentials(config.data_dir)

    def shutdown(self) -> None:
        """Kill the live tree. The web server does not call this when a browser disconnects."""
        self._stop = True
        with self.store.lock:
            packet = self._live_packet()
            if packet is not None and self.proc is not None:
                self._kill_and_finish(packet, "blocked", "cancelled", killed=True)
            elif self.proc is not None:
                self.proc.kill_tree()
                self.proc = None
            self._close_lease()
            wipe_grok_credentials(self.config.data_dir)

    def request_cancel(self, packet_id: str) -> None:
        with self.store.lock:
            meta = self.store.read_meta(packet_id)
            meta["cancel"] = True
            self.store.write_meta(packet_id, meta)
            self.store.log(f"{packet_id} cancel requested")

    def request_decision(self, packet_id: str, decision: str) -> None:
        if decision not in {"approved", "denied"}:
            raise ValueError("decision must be approved or denied")
        with self.store.lock:
            meta = self.store.read_meta(packet_id)
            meta["decision"] = decision
            self.store.write_meta(packet_id, meta)

    def tick(self) -> str:
        with self.store.lock:
            if self._stop:
                return "stopped"
            self.store.recover()
            if not self._recovered:
                self._recover_orphans()
                self._recovered = True
            if self.proc is not None:
                return self._pump()
            waiting = self._packet_in("needs-approval")
            if waiting is not None:
                return self._service_detached_approval(waiting)
            self._claim_available()
            if self._packet_in("running") or self._packet_in("needs-approval"):
                return "busy"
            nxt = self._next_claimed()
            if nxt is None:
                return "idle"
            return self._start(nxt)

    # Recovery --------------------------------------------------------------

    def _recover_orphans(self) -> None:
        running = self._packet_in("running")
        if running is not None:
            self._kill_saved_group(str(running["id"]))
            self._finish_without_process(
                running,
                "failed",
                "watcher restarted during run",
                killed=True,
            )
        waiting = self._packet_in("needs-approval")
        if waiting is not None:
            self._kill_saved_group(str(waiting["id"]))
            if self._clock_expired(str(waiting["id"]), waiting):
                self._finish_without_process(
                    waiting,
                    "timed-out",
                    self._timeout_reason(waiting),
                    killed=True,
                )

    def _kill_saved_group(self, packet_id: str) -> None:
        meta = self.store.read_meta(packet_id)
        pgid = meta.get("pgid")
        if isinstance(pgid, int):
            kill_process_group(pgid)

    # Inbox -----------------------------------------------------------------

    def _claim_available(self) -> None:
        for path in self.store.inbox_files():
            text = path.read_text(encoding="utf-8")
            interpreted = load_packet_text(text, human_owner=self.config.human_owner)
            packet = interpreted.packet
            if not interpreted.ok:
                # Outbox first, then drop the inbox file, so a crash cannot lose it.
                self._publish(
                    packet,
                    self.store.read_output(str(packet["id"])) or f"{interpreted.reason}\n",
                )
                path.unlink(missing_ok=True)
                self.store.log(f"{packet['id']} -> blocked ({interpreted.reason})")
                self._write_handoff(packet, interpreted.reason or "blocked")
                continue
            packet_id = str(packet["id"])
            if self.store.id_exists_elsewhere(packet_id, path):
                packet = dict(packet)
                packet["id"] = self._duplicate_id(packet_id)
                packet["status"] = "blocked"
                packet["result"] = {
                    "summary": None,
                    "artifacts": [],
                    "blocked_reason": f"duplicate id {packet_id}",
                    "completed_at": now_iso(),
                }
                self._publish(packet, f"duplicate id {packet_id}\n")
                path.unlink(missing_ok=True)
                self.store.log(f"{packet['id']} -> blocked (duplicate id {packet_id})")
                self._write_handoff(packet, f"duplicate id {packet_id}")
                continue
            packet["status"] = "claimed"
            packet["claimed_by"] = self.config.watcher_name
            packet["claimed_at"] = now_iso()
            self.store.write_packet("claimed", packet)
            path.unlink(missing_ok=True)
            self.store.log(f"{packet_id} incoming -> claimed")

    def _duplicate_id(self, packet_id: str) -> str:
        for number in range(1, 100):
            candidate = f"{packet_id}-dup-{number}"
            if self.store.find(candidate) is None:
                return candidate
        return f"{packet_id}-dup-x"

    def _publish_blocked(self, packet: dict[str, Any], reason: str) -> None:
        packet["status"] = "blocked"
        result = dict(packet.get("result") or {})
        result["blocked_reason"] = reason
        result.setdefault("summary", None)
        result.setdefault("artifacts", [])
        result["completed_at"] = now_iso()
        packet["result"] = result
        packet["retro"] = None
        self._publish(packet, self.store.read_output(str(packet["id"])) or f"{reason}\n")
        self.store.log(f"{packet['id']} -> blocked ({reason})")
        self._write_handoff(packet, reason)

    def _publish(self, packet: dict[str, Any], output: str) -> None:
        self.store.publish_terminal_files(packet, prefix_output(output))

    # Start -----------------------------------------------------------------

    def _start(self, packet: dict[str, Any]) -> str:
        packet_id = str(packet["id"])
        try:
            folder = self._resolve_run_folder(packet)
        except WorkdirTrustError as exc:
            self._enter_workdir_approval(packet, str(exc.resolved or packet.get("workdir") or ""))
            return "needs-approval"
        except WorkdirError as exc:
            reason = "invalid workdir" if _has_workdir(packet) else str(exc)
            self._publish_blocked(packet, reason)
            return "blocked"
        requested = str((packet.get("runner") or {}).get("requested") or "")
        adapter = get_adapter(requested, self.config)
        permissions = str(packet.get("permissions") or "ask")
        if not adapter.implemented:
            self._publish_blocked(packet, f"runner not available in v1: {adapter.name}")
            return "blocked"
        if permissions in {"ask", "allow-list"} and not adapter.can_ask:
            self._publish_blocked(packet, "runner cannot ask")
            return "blocked"
        findings = scan_project_permission_rules(folder)
        if findings:
            self._publish_blocked(packet, needs_you_message(findings))
            return "blocked"
        self.adapter = adapter
        previous = read_last_agent(folder)
        handoff = adapter.handoff(
            previous,
            requested_runner=requested,
            requested_model=_model_requested(packet),
            actual_runner=None,
            actual_model=None,
        )
        if handoff is not None:
            packet["handoff"] = handoff
        instructions = load_instructions(folder)
        prompt = compose_prompt(
            instructions=instructions,
            packet=packet,
            previous=previous,
            taking_over=bool(handoff and handoff.get("taking_over")),
        )
        self.store.write_prompt(packet_id, prompt)
        timeout_s = int(packet.get("timeout_s") or 900)
        try:
            folder = self._confirm_launch_folder(folder)
        except (NotImplementedError, ValueError, WorkdirError) as exc:
            self._publish_blocked(packet, str(exc))
            return "blocked"
        packet["status"] = "running"
        packet["worker_thread"] = "run-" + os.urandom(2).hex()
        self.store.move_packet("claimed", "running", packet, f"{packet_id} claimed -> running")
        self.run_state = {
            "id": packet_id,
            "project": folder,
            "reported_model": None,
            "reported_runner": None,
            "texts": [],
            "summary": None,
            "artifacts": [],
            "retro": None,
            "pids": set(),
            "ports_seen": set(),
            "pending": None,
            "cmdline_checked": False,
        }
        self._lease = WorkdirLease(folder)
        try:
            self._lease.acquire()
        except OSError as exc:
            self._close_lease()
            self._publish_blocked(packet, f"could not hold workdir: {exc}")
            return "blocked"
        try:
            self.proc, _home = spawn_runner(
                adapter=adapter,
                folder=folder,
                prompt_file=self.store.runtime / f"{packet_id}.prompt.md",
                timeout_s=timeout_s,
                base_env=os.environ.copy(),
                config=self.config,
            )
        except LaunchRefused as exc:
            self.proc = None
            self.deadline = None
            self._close_lease()
            wipe_grok_credentials(self.config.data_dir)
            reason = str(exc)
            if reason.startswith("Needs you") or reason == "workdir changed before launch":
                self._publish_blocked(packet, reason)
                return "blocked"
            return self._kill_and_finish(packet, "failed", reason, killed=True)
        except (ProcessTreeError, OSError) as exc:
            self.proc = None
            self.deadline = None
            return self._kill_and_finish(
                packet,
                "failed",
                f"refusing to run without process confinement: {exc}",
                killed=True,
            )
        previous_meta = self.store.read_meta(packet_id)
        carry_clock = previous_meta.get("pending_kind") == "workdir" and isinstance(
            previous_meta.get("started_epoch"), (int, float)
        )
        started = float(previous_meta["started_epoch"]) if carry_clock else time.time()
        remaining = timeout_s - (time.time() - started) if carry_clock else float(timeout_s)
        self.deadline = time.monotonic() + max(0.1, remaining)
        meta = {
            "started_epoch": started,
            "timeout_s": timeout_s,
            "pid": self.proc.pid,
            "pgid": self.proc.group_id,
            "cancel": False,
            "decision": None,
        }
        self.store.write_meta(packet_id, meta)
        banner = windows_sandbox_banner()
        if banner:
            self.store.append_output(packet_id, banner + "\n")
        self._note_pids()
        self.store.log(f"{packet_id} process {self.proc.pid} group {self.proc.group_id}")
        return self._pump()

    # Pump ------------------------------------------------------------------

    def _pump(self) -> str:
        packet = self._packet_in("running") or self._packet_in("needs-approval")
        if packet is None or self.proc is None:
            return "idle"
        packet_id = str(packet["id"])
        if self._cancelled(packet_id):
            return self._kill_and_finish(packet, "blocked", "cancelled", killed=True)
        if self._timed_out_live(packet):
            return self._kill_and_finish(packet, "timed-out", self._timeout_reason(packet), killed=True)
        if packet.get("status") == "needs-approval":
            decision = self.store.read_meta(packet_id).get("decision")
            if decision == "approved":
                self._apply_decision(packet, approved=True)
                if self.proc is None:
                    return str(packet.get("status") or "failed")
                packet = self._packet_in("running") or packet
            elif decision == "denied":
                self._mark_decision(packet, "denied")
                return self._kill_and_finish(packet, "blocked", "refused approval", killed=True)
            else:
                return "needs-approval"
        assert self.proc is not None
        observed = self._observed_command_problem()
        if observed:
            return self._kill_and_finish(packet, "failed", observed, killed=True)
        while True:
            item = self.proc.poll_line()
            if item is None:
                break
            stream, line = item
            self.store.append_raw(packet_id, line if stream == "stdout" else f"[stderr] {line}")
            if stream != "stdout":
                self.store.append_output(packet_id, line)
                continue
            try:
                event = self.adapter.parse_line(line) if self.adapter else None
            except ProtocolError as exc:
                return self._kill_and_finish(packet, "failed", str(exc), killed=True)
            if event is None:
                self.run_state["texts"].append(line.rstrip("\n"))
                if not line.lstrip().startswith("{") and not line.lstrip().startswith("TASKBENCH"):
                    self.store.append_output(packet_id, line)
                continue
            if event.get("type") == "ignore":
                continue
            outcome = self._handle_event(packet, event)
            if outcome == "pause":
                return "needs-approval"
            if outcome == "terminal":
                return str(packet.get("status"))
            packet = self._packet_in("running") or self._packet_in("needs-approval") or packet
        code = self.proc.exit_code()
        if code is None:
            return "running"
        if not self.proc.output_closed():
            self.proc.reap_group()
            return "running"
        return self._finish_exit(packet, code)

    def _observed_command_problem(self) -> str | None:
        # Linux /proc only. This checks the argv taskbench launched, not a
        # mode the CLI applied. read_cmdline returns None off Linux, so it
        # is not a Windows backstop. Absence of a later report is not proof
        # of dontAsk; see unexpected_permission.
        if self.run_state.get("cmdline_checked") or self.proc is None:
            return None
        self.run_state["cmdline_checked"] = True
        argv = read_cmdline(self.proc.pid)
        if not argv:
            return None
        from taskbench.permissions import PermissionFlagError, check_permission_args, iter_flags

        try:
            check_permission_args(argv)
        except PermissionFlagError as exc:
            return f"observed command line: {exc}"
        flags = iter_flags(argv)
        if not any(name == "permission-mode" for name, _value in flags):
            return "observed command line is missing --permission-mode dontAsk"
        flags = iter_flags(argv)
        if not any(name == "sandbox" for name, _value in flags):
            return "observed command line is missing --sandbox strict"
        return None

    def _handle_event(self, packet: dict[str, Any], event: dict[str, Any]) -> str:
        if not self.run_state.get("init_ok"):
            reason = init_failure(event)
            if reason:
                self._kill_and_finish(packet, "failed", reason, killed=True)
                return "terminal"
            self.run_state["init_ok"] = True
            return "continue"
        reason = unexpected_permission(event)
        if reason:
            self._kill_and_finish(packet, "failed", reason, killed=True)
            return "terminal"
        kind = str(event.get("type") or "")
        if kind == "model":
            model = event.get("model")
            self.run_state["reported_model"] = model if isinstance(model, str) else ""
            return "continue"
        if kind == "runner":
            runner = event.get("runner")
            self.run_state["reported_runner"] = runner if isinstance(runner, str) else ""
            return "continue"
        if kind == "text":
            text = event.get("text")
            if isinstance(text, str):
                self.run_state["texts"].append(text)
                self.store.append_output(str(packet["id"]), text if text.endswith("\n") else text + "\n")
            return "continue"
        if kind == "result":
            summary = event.get("summary")
            if isinstance(summary, str):
                self.run_state["summary"] = summary
                self.store.append_output(
                    str(packet["id"]),
                    summary if summary.endswith("\n") else summary + "\n",
                )
            artifacts = event.get("artifacts")
            if isinstance(artifacts, list):
                self.run_state["artifacts"] = [item for item in artifacts if isinstance(item, str)]
            return "continue"
        if kind == "retro":
            retro = _normalize_retro(event)
            if retro is not None:
                self.run_state["retro"] = retro
            return "continue"
        if kind == "done":
            return "continue"
        if kind == "usage_limit":
            self._usage_block(packet, event)
            return "terminal"
        if kind == "action":
            action = event.get("action")
            if not isinstance(action, dict):
                return "continue"
            return self._handle_action(packet, action)
        return "continue"

    def _handle_action(self, packet: dict[str, Any], action: dict[str, Any]) -> str:
        folder: Path = self.run_state["project"]
        runner_cfg = self.config.runners.get(self.adapter.name if self.adapter else "", None)
        sandbox_only = bool(runner_cfg and runner_cfg.sandbox_only)
        decision = classify(
            action,
            permissions=str(packet.get("permissions") or "ask"),
            allow=list(packet.get("allow") or []),
            sandbox_only=sandbox_only,
            project_dir=folder,
            denied_dirs=[self.config.data_dir / "grok-home"],
        )
        self.store.append_output(str(packet["id"]), f"tool: {action.get('op') or 'tool'}\n")
        command = full_command(action)
        if decision.kind == "allow":
            self.store.log(f"{packet['id']} allow-match {command}")
            self._execute_action(packet, action, command)
            fatal = self._fail_if_unconfined(packet)
            if fatal:
                return fatal
            self._reply({"ok": True})
            return "continue"
        if decision.kind == "refuse":
            self.store.log(f"{packet['id']} refuse {decision.reason}: {command}")
            self.store.append_output(str(packet["id"]), f"refused: {decision.reason}\n")
            self._reply({"ok": False, "refused": decision.reason})
            return "continue"
        self._enter_needs_approval(packet, action, command)
        return "pause"

    def _enter_needs_approval(self, packet: dict[str, Any], action: dict[str, Any], command: str) -> None:
        packet_id = str(packet["id"])
        approvals = list(packet.get("approvals") or [])
        approvals.append({"at": now_iso(), "command": command, "decision": "pending"})
        packet["approvals"] = approvals
        packet["status"] = "needs-approval"
        diff = render_diff(self.run_state["project"], action)
        meta = self.store.read_meta(packet_id)
        meta["decision"] = None
        meta["pending_command"] = command
        meta["pending_action"] = action
        meta["pending_diff"] = diff
        self.store.write_meta(packet_id, meta)
        self.run_state["pending"] = action
        self.store.move_packet("running", "needs-approval", packet, f"{packet_id} running -> needs-approval")

    def _apply_decision(self, packet: dict[str, Any], *, approved: bool) -> None:
        packet_id = str(packet["id"])
        meta = self.store.read_meta(packet_id)
        action = meta.get("pending_action") or self.run_state.get("pending") or {}
        command = str(meta.get("pending_command") or full_command(action if isinstance(action, dict) else {}))
        self._mark_decision(packet, "approved" if approved else "denied")
        if not approved:
            return
        self.store.log(f"{packet_id} approval approved {command}")
        if isinstance(action, dict):
            self._execute_action(packet, action, command)
            if self.run_state.get("fatal"):
                self._kill_and_finish(packet, "failed", str(self.run_state["fatal"]), killed=True)
                return
        self._reply({"ok": True, "approved": True})
        packet["status"] = "running"
        meta = self.store.read_meta(packet_id)
        meta["decision"] = None
        meta["pending_action"] = None
        self.store.write_meta(packet_id, meta)
        self.store.move_packet("needs-approval", "running", packet, f"{packet_id} needs-approval -> running")

    def _mark_decision(self, packet: dict[str, Any], decision: str) -> None:
        approvals = list(packet.get("approvals") or [])
        for entry in reversed(approvals):
            if isinstance(entry, dict) and entry.get("decision") == "pending":
                entry["decision"] = decision
                break
        packet["approvals"] = approvals

    def _execute_action(self, packet: dict[str, Any], action: dict[str, Any], command: str) -> None:
        op = str(action.get("op") or "")
        folder: Path = self.run_state["project"]
        packet_id = str(packet["id"])
        try:
            if op in {"list", "read", "diff"}:
                result = execute_readonly(folder, action)
                self.store.append_raw(packet_id, result + ("\n" if result and not result.endswith("\n") else ""))
                return
            if op == "write":
                result = execute_write(folder, action)
                self.store.append_raw(packet_id, result + "\n")
                return
        except Exception as exc:  # path escapes are refusals, not crashes
            self.store.append_output(packet_id, f"action error: {exc}\n")
            self.store.log(f"{packet_id} action error {exc}")
            return
        if _is_server(action, command):
            url = preview_url(command)
            if url is None or not _loopback_command(command):
                self.store.append_output(packet_id, "refused: server must bind 127.0.0.1\n")
                self.store.log(f"{packet_id} refuse server bind {command}")
                return
            try:
                if self.proc is None:
                    env = self.adapter.environment(os.environ.copy()) if self.adapter else os.environ.copy()
                    self.proc = ManagedProcess(shell_argv(command), folder, env)
                else:
                    # Same allow-list as the runner. Do not pass the grok
                    # config home and do not inherit the parent environment.
                    self.proc.spawn_companion(
                        shell_argv(command),
                        folder,
                        scrub_environment(os.environ.copy()),
                    )
            except ProcessTreeError as exc:
                self.run_state["fatal"] = (
                    f"refusing sidecar outside the process tree: {exc}"
                )
                return
            except Exception as exc:
                self.store.append_output(packet_id, f"server error: {exc}\n")
                return
            meta = self.store.read_meta(packet_id)
            meta["preview_url"] = url
            self.store.write_meta(packet_id, meta)
            self.store.append_output(packet_id, f"preview {url}\n")
            self._note_pids()
            return
        self._run_foreground_command(packet, command)

    def _fail_if_unconfined(self, packet: dict[str, Any]) -> str | None:
        reason = self.run_state.get("fatal")
        if not reason:
            return None
        self._kill_and_finish(packet, "failed", str(reason), killed=True)
        return "terminal"

    def _run_foreground_command(self, packet: dict[str, Any], command: str) -> None:
        folder: Path = self.run_state["project"]
        remaining = self._remaining()
        env = self.adapter.environment(os.environ.copy()) if self.adapter else os.environ.copy()
        try:
            proc = ManagedProcess(shell_argv(command), folder, env)
        except (ProcessTreeError, OSError) as exc:
            self.run_state["fatal"] = f"refusing command without process confinement: {exc}"
            return
        self.shell_proc = proc
        chunks: list[str] = []
        deadline = time.monotonic() + remaining
        code: int | None = None
        while time.monotonic() < deadline:
            item = proc.poll_line()
            if item is not None:
                _stream, line = item
                chunks.append(line)
            code = proc.exit_code()
            if code is not None and not proc.output_closed():
                proc.reap_group()
            if code is not None and proc.output_closed():
                break
            if item is None:
                time.sleep(0.02)
        else:
            proc.kill_tree()
            self.store.append_output(str(packet["id"]), "".join(chunks))
            self.store.append_output(str(packet["id"]), "command timed out\n")
            self.shell_proc = None
            return
        proc.kill_tree()
        self.shell_proc = None
        self.store.append_output(str(packet["id"]), "".join(chunks))
        self.store.append_output(str(packet["id"]), f"command exit {code}\n")

    def _reply(self, payload: dict[str, Any]) -> None:
        if self.proc is not None:
            self.proc.send_line(json.dumps(payload))

    def _usage_block(self, packet: dict[str, Any], event: dict[str, Any]) -> None:
        runner = event.get("runner")
        if not isinstance(runner, str) or not runner.strip():
            runner = self.adapter.name if self.adapter else "unknown"
        message = event.get("message") if isinstance(event.get("message"), str) else ""
        reset_at = event.get("reset_at") if isinstance(event.get("reset_at"), str) else ""
        reason = f"usage limit: runner {runner}: {message}"
        if reset_at:
            reason = f"{reason}; reset {reset_at}"
        self._kill_and_finish(packet, "blocked", reason, killed=True)

    def _finish_exit(self, packet: dict[str, Any], code: int) -> str:
        summary = self.run_state.get("summary")
        texts = [item for item in self.run_state.get("texts") or [] if item]
        if not self.run_state.get("init_ok"):
            tail = _last_lines(self.store.read_output(str(packet["id"])), 20)
            reason = "missing permissionMode init event"
            if code != 0:
                reason = f"{reason}; exit {code}"
            if tail:
                reason = f"{reason}: {tail}"
            return self._kill_and_finish(packet, "failed", reason, killed=False)
        if code != 0:
            tail = _last_lines(self.store.read_output(str(packet["id"])), 20)
            reason = f"exit {code}"
            if tail:
                reason = f"{reason}: {tail}"
            return self._kill_and_finish(packet, "failed", reason, killed=False)
        if not summary and not texts:
            tail = _last_lines(self.store.read_output(str(packet["id"])), 20)
            reason = "exit 0: no result"
            if tail:
                reason = f"{reason}: {tail}"
            return self._kill_and_finish(packet, "failed", reason, killed=False)
        if not summary:
            self.run_state["summary"] = texts[-1][:500]
        if self.run_state.get("retro") is None:
            try:
                self._retro_followup(packet)
            except LaunchRefused as exc:
                return self._kill_and_finish(packet, "failed", str(exc), killed=True)
        return self._kill_and_finish(packet, "done", None, killed=False)

    def _retro_followup(self, packet: dict[str, Any]) -> None:
        if self.adapter is None or not self.adapter.implemented:
            return
        folder: Path = self.run_state["project"]
        prompt = retro_followup_prompt(packet)
        path = self.store.runtime / f"{packet['id']}.retro-prompt.md"
        from taskbench.atomic import atomic_write_text

        atomic_write_text(path, prompt)
        try:
            proc, _home = spawn_runner(
                adapter=self.adapter,
                folder=folder,
                prompt_file=path,
                timeout_s=RETRO_TIMEOUT_S,
                base_env=os.environ.copy(),
                config=self.config,
            )
        except LaunchRefused:
            raise
        except (ProcessTreeError, OSError) as exc:
            raise LaunchRefused(f"refusing to run without process confinement: {exc}") from exc
        deadline = time.monotonic() + min(RETRO_TIMEOUT_S, max(self._remaining(), 1))
        init_ok = False
        try:
            while time.monotonic() < deadline:
                item = proc.poll_line()
                if item is not None:
                    stream, line = item
                    self.store.append_raw(str(packet["id"]), line if stream == "stdout" else f"[stderr] {line}")
                    if stream == "stdout":
                        try:
                            event = self.adapter.parse_line(line)
                        except ProtocolError:
                            event = None
                        if event and event.get("type") != "ignore":
                            if not init_ok:
                                reason = init_failure(event)
                                if reason:
                                    raise LaunchRefused(reason)
                                init_ok = True
                            elif event.get("type") == "retro":
                                retro = _normalize_retro(event)
                                if retro is not None:
                                    self.run_state["retro"] = retro
                                break
                if proc.exit_code() is not None and not proc.output_closed():
                    proc.reap_group()
                if proc.exit_code() is not None and proc.output_closed():
                    break
                if item is None:
                    time.sleep(0.02)
            if not init_ok:
                raise LaunchRefused("missing permissionMode init event")
        finally:
            proc.kill_tree()

    def _kill_and_finish(
        self,
        packet: dict[str, Any],
        status: str,
        reason: str | None,
        *,
        killed: bool,
    ) -> str:
        pids = set(self.run_state.get("pids") or set())
        if self.proc is not None:
            pids.update(self.proc.snapshot_pids())
            ports_during = listening_ports(pids)
            self.run_state.setdefault("ports_seen", set()).update(ports_during)
            self.proc.kill_tree()
        if self.shell_proc is not None:
            pids.update(self.shell_proc.snapshot_pids())
            self.shell_proc.kill_tree()
            self.shell_proc = None
        survivors = sorted(pid for pid in pids if pid_alive(pid))
        still_open = listening_ports(set(pids))
        opened = sorted(self.run_state.get("ports_seen") or [])
        self.store.log(f"{packet['id']} ports opened: {opened or 'none'}")
        self.store.log(f"{packet['id']} ports still open: {still_open or 'none'}")
        if survivors:
            self.store.log(f"{packet['id']} survivors: {survivors}")
            if self.proc is not None:
                self.proc.kill_tree()
        self.proc = None
        if killed:
            self.run_state["retro"] = None
        self._assign_actuals(packet, started=True)
        self._refresh_handoff(packet)
        if killed:
            packet["retro"] = None
        else:
            packet["retro"] = self.run_state.get("retro")
        summary = self.run_state.get("summary") or reason or status
        artifacts = [
            item
            for item in (self.run_state.get("artifacts") or [])
            if isinstance(item, str) and ".." not in item and not item.startswith("/")
        ]
        result = {
            "summary": summary if status == "done" else (self.run_state.get("summary") or None),
            "artifacts": artifacts,
            "blocked_reason": None if status == "done" else (reason or status),
            "completed_at": now_iso(),
        }
        if status == "done" and not result["summary"]:
            result["summary"] = summary
        packet["result"] = result
        packet["status"] = status
        output = self.store.read_output(str(packet["id"]))
        if reason and status != "done":
            output += f"\n{reason}\n"
        output += f"status: {status}\n"
        self._publish(packet, output)
        self.store.log(f"{packet['id']} -> {status}" + (f" ({reason})" if reason else ""))
        self._write_handoff(packet, result["summary"] or reason or status)
        if status == "done":
            self._maybe_review(packet)
        self.run_state = {}
        self.deadline = None
        self.adapter = None
        self._close_lease()
        wipe_grok_credentials(self.config.data_dir)
        return status

    def _finish_without_process(
        self,
        packet: dict[str, Any],
        status: str,
        reason: str,
        *,
        killed: bool,
    ) -> None:
        self.run_state = {"id": packet["id"], "retro": None, "summary": None, "artifacts": [], "pids": set(), "ports_seen": set()}
        self.proc = None
        packet["retro"] = None
        result = {
            "summary": None,
            "artifacts": [],
            "blocked_reason": reason,
            "completed_at": now_iso(),
        }
        packet["result"] = result
        packet["status"] = status
        output = self.store.read_output(str(packet["id"]))
        output += f"\n{reason}\nstatus: {status}\n"
        self._publish(packet, output)
        self.store.log(f"{packet['id']} -> {status} ({reason})")
        self._write_handoff(packet, reason)
        self._close_lease()
        wipe_grok_credentials(self.config.data_dir)

    def _service_detached_approval(self, packet: dict[str, Any]) -> str:
        packet_id = str(packet["id"])
        if self._cancelled(packet_id) or self._clock_expired(packet_id, packet):
            status = "blocked" if self._cancelled(packet_id) else "timed-out"
            reason = "cancelled" if status == "blocked" else self._timeout_reason(packet)
            self._finish_without_process(packet, status, reason, killed=True)
            return status
        if self.store.read_meta(packet_id).get("pending_kind") == "workdir":
            return self._service_workdir_approval(packet)
        decision = self.store.read_meta(packet_id).get("decision")
        if decision is None:
            return "needs-approval"
        if decision == "denied":
            self._mark_decision(packet, "denied")
            self._finish_without_process(packet, "blocked", "refused approval", killed=True)
            return "blocked"
        meta = self.store.read_meta(packet_id)
        action = meta.get("pending_action") if isinstance(meta.get("pending_action"), dict) else {}
        command = str(meta.get("pending_command") or "")
        try:
            folder = self._resolve_run_folder(packet)
        except WorkdirError as exc:
            self._finish_without_process(packet, "blocked", str(exc), killed=True)
            return "blocked"
        self.run_state = {
            "id": packet_id,
            "project": folder,
            "reported_model": None,
            "reported_runner": None,
            "texts": [],
            "summary": f"approved: {command[:120]}",
            "artifacts": [],
            "retro": None,
            "pids": set(),
            "ports_seen": set(),
        }
        self.adapter = get_adapter(str((packet.get("runner") or {}).get("requested") or ""), self.config)
        self._mark_decision(packet, "approved")
        self.store.log(f"{packet_id} approval approved {command}")
        self._execute_action(packet, action, command)
        self._kill_and_finish(packet, "done", None, killed=True)
        return "done"

    # Helpers ---------------------------------------------------------------

    def _assign_actuals(self, packet: dict[str, Any], *, started: bool) -> None:
        if not started or self.adapter is None:
            return
        model = packet.get("model")
        if not isinstance(model, dict):
            model = {"requested": None, "actual": None}
        model = dict(model)
        model["actual"] = self.adapter.report_model(self.run_state.get("reported_model"))
        packet["model"] = model
        runner = packet.get("runner")
        if not isinstance(runner, dict):
            runner = {"requested": None, "actual": None}
        runner = dict(runner)
        reported = self.run_state.get("reported_runner")
        runner["actual"] = reported or self.adapter.name
        packet["runner"] = runner

    def _refresh_handoff(self, packet: dict[str, Any]) -> None:
        folder = self.run_state.get("project")
        if not isinstance(folder, Path) or self.adapter is None:
            return
        previous = read_last_agent(folder)
        # The file still describes the previous run until we overwrite it below.
        packet["handoff"] = self.adapter.handoff(
            previous,
            requested_runner=str((packet.get("runner") or {}).get("requested") or ""),
            requested_model=_model_requested(packet),
            actual_runner=str((packet.get("runner") or {}).get("actual") or "") or None,
            actual_model=str((packet.get("model") or {}).get("actual") or "") or None,
        )

    def _resolve_run_folder(self, packet: dict[str, Any]) -> Path:
        """Project root for this packet.

        A ``workdir`` is resolved before it is accepted. ``from`` is never
        used to decide trust. An unregistered folder, including one on a
        ``web-ui`` packet, is ``needs-approval`` (the page shows Needs you).
        Only the local UI register endpoint adds a folder. A rejected path
        is ``invalid workdir``.
        """
        raw = packet.get("workdir")
        if isinstance(raw, str) and raw.strip():
            try:
                folder = self._validate_folder(raw)
            except WorkdirError as exc:
                if exc.resolved:
                    packet["workdir"] = exc.resolved
                raise WorkdirError("invalid workdir", exc.resolved) from exc
            packet["workdir"] = str(folder)
            if not is_registered(self.config.data_dir, folder):
                raise WorkdirTrustError(
                    "Needs you: workdir is not registered: " + str(folder),
                    str(folder),
                )
            return folder
        folder = project_dir(self.config.projects_dir, str(packet.get("project") or ""))
        if not folder.is_dir():
            raise WorkdirError("unknown project")
        return folder

    def _enter_workdir_approval(self, packet: dict[str, Any], folder: str) -> None:
        """Park a bot folder until the owner accepts it. Do not register it."""
        packet_id = str(packet["id"])
        packet["workdir"] = folder
        packet["status"] = "needs-approval"
        approvals = list(packet.get("approvals") or [])
        approvals.append({"at": now_iso(), "command": folder, "decision": "pending"})
        packet["approvals"] = approvals
        timeout_s = int(packet.get("timeout_s") or 900)
        self.store.write_meta(
            packet_id,
            {
                "started_epoch": time.time(),
                "timeout_s": timeout_s,
                "cancel": False,
                "decision": None,
                "pending_kind": "workdir",
                "pending_command": folder,
                "pending_workdir": folder,
            },
        )
        self.store.move_packet(
            "claimed",
            "needs-approval",
            packet,
            f"{packet_id} claimed -> needs-approval (workdir)",
        )

    def _service_workdir_approval(self, packet: dict[str, Any]) -> str:
        packet_id = str(packet["id"])
        decision = self.store.read_meta(packet_id).get("decision")
        folder_text = str(
            self.store.read_meta(packet_id).get("pending_workdir") or packet.get("workdir") or ""
        )
        if decision == "denied":
            self._mark_decision(packet, "denied")
            self._finish_without_process(packet, "blocked", "refused workdir", killed=True)
            return "blocked"
        try:
            folder = self._validate_folder(folder_text)
        except WorkdirError:
            self._finish_without_process(packet, "blocked", "invalid workdir", killed=True)
            return "blocked"
        if not is_registered(self.config.data_dir, folder):
            return "needs-approval"
        packet["workdir"] = str(folder)
        packet["status"] = "claimed"
        self.store.move_packet(
            "needs-approval",
            "claimed",
            packet,
            f"{packet_id} workdir registered",
        )
        return self._start(packet)

    def _validate_folder(self, raw: str) -> Path:
        config_dir = getattr(self.config, "config_path", None)
        parent = Path(config_dir).resolve().parent if config_dir else None
        return validate_workdir(raw, state_dir=self.config.data_dir, config_dir=parent)

    def _confirm_launch_folder(self, folder: Path) -> Path:
        """Re-resolve immediately before launch and refuse a swapped path."""
        confirmed = self._validate_folder(str(folder))
        if not same_canonical(confirmed, folder):
            raise WorkdirError("workdir changed before launch", str(confirmed))
        return confirmed

    def _handoff_folder(self, packet: dict[str, Any]) -> Path | None:
        raw = packet.get("workdir")
        if isinstance(raw, str) and raw.strip():
            try:
                folder = self._validate_folder(raw)
            except WorkdirError:
                return None
            if not is_registered(self.config.data_dir, folder):
                return None
            return folder if folder.is_dir() else None
        project = packet.get("project")
        if not isinstance(project, str) or not project:
            return None
        folder = project_dir(self.config.projects_dir, project)
        return folder if folder.is_dir() else None

    def _write_handoff(self, packet: dict[str, Any], summary: str | None) -> None:
        folder = self._handoff_folder(packet)
        if folder is None:
            return
        runner = str((packet.get("runner") or {}).get("actual") or (packet.get("runner") or {}).get("requested") or "unknown")
        model = (packet.get("model") or {}).get("actual")
        if not isinstance(model, str) or not model:
            model = "unknown"
        write_last_agent(
            folder,
            runner=runner,
            model=model,
            finished_at=str((packet.get("result") or {}).get("completed_at") or now_iso()),
            task_id=str(packet.get("id")),
            sender=str(packet.get("from") or ""),
            status=str(packet.get("status") or ""),
            summary=summary or str(packet.get("status") or ""),
        )

    def _maybe_review(self, packet: dict[str, Any]) -> None:
        review = packet.get("review_by")
        if not isinstance(review, dict):
            return
        child_id = f"{packet['id']}-review"
        if self.store.find(child_id) is not None:
            return
        permissions = review.get("permissions") or "plan"
        if permissions not in {"plan", "ask", "allow-list"}:
            permissions = "plan"
        summary = (packet.get("result") or {}).get("summary") or ""
        child: dict[str, Any] = {
            "schema": "joy-feeder.task/v2",
            "id": child_id,
            "created": now_iso(),
            "from": "taskbench",
            "runner": {"requested": review.get("runner"), "actual": None},
            "model": {"requested": review.get("model"), "actual": None},
            "project": packet.get("project"),
            "permissions": permissions,
            "timeout_s": int(packet.get("timeout_s") or 900),
            "goal": f"Review the changes made by {packet['id']}\n\nParent result:\n{summary}",
            "status": "incoming",
            "retro": None,
            "result": None,
        }
        if isinstance(packet.get("workdir"), str) and packet["workdir"].strip():
            child["workdir"] = packet["workdir"]
        self.store.write_packet("inbox", child)
        result = dict(packet.get("result") or {})
        result["review_task"] = child_id
        packet["result"] = result
        self.store.write_packet("outbox", packet)
        self.store.log(f"{packet['id']} queued review {child_id}")

    def _note_pids(self) -> None:
        if self.proc is None:
            return
        self.run_state.setdefault("pids", set()).update(self.proc.snapshot_pids())
        ports = listening_ports(set(self.run_state["pids"]))
        self.run_state.setdefault("ports_seen", set()).update(ports)

    def _close_lease(self) -> None:
        lease = self._lease
        self._lease = None
        if lease is not None:
            lease.close()

    def _packet_in(self, location: str) -> dict[str, Any] | None:
        from taskbench.task_format import parse_stored

        directory = self.store.location_dir(location)
        files = self.store.packet_paths(directory)
        if not files:
            return None
        try:
            raw = parse_stored(files[0].read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        return raw

    def _live_packet(self) -> dict[str, Any] | None:
        return self._packet_in("running") or self._packet_in("needs-approval")

    def _next_claimed(self) -> dict[str, Any] | None:
        items: list[dict[str, Any]] = []
        for path in self.store.packet_paths(self.store.claimed):
            from taskbench.task_format import parse_stored

            raw = parse_stored(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                items.append(raw)
        if not items:
            return None
        items.sort(key=lambda packet: str(packet.get("claimed_at") or ""))
        return items[0]

    def _cancelled(self, packet_id: str) -> bool:
        return bool(self.store.read_meta(packet_id).get("cancel"))

    def _timed_out_live(self, packet: dict[str, Any]) -> bool:
        if self.deadline is None:
            return self._clock_expired(str(packet["id"]), packet)
        return time.monotonic() >= self.deadline

    def _clock_expired(self, packet_id: str, packet: dict[str, Any]) -> bool:
        meta = self.store.read_meta(packet_id)
        started = meta.get("started_epoch")
        timeout_s = meta.get("timeout_s") or packet.get("timeout_s") or 900
        if not isinstance(started, (int, float)):
            return False
        return (time.time() - float(started)) >= float(timeout_s)

    def _timeout_reason(self, packet: dict[str, Any]) -> str:
        timeout_s = packet.get("timeout_s") or self.store.read_meta(str(packet["id"])).get("timeout_s") or 900
        return f"timed out after {timeout_s}s"

    def _remaining(self) -> float:
        if self.deadline is None:
            return 30
        return max(0.1, self.deadline - time.monotonic())


def _has_workdir(packet: dict[str, Any]) -> bool:
    raw = packet.get("workdir")
    return isinstance(raw, str) and bool(raw.strip())


def _model_requested(packet: dict[str, Any]) -> str | None:
    model = packet.get("model")
    if not isinstance(model, dict):
        return None
    requested = model.get("requested")
    return requested if isinstance(requested, str) and requested else None


def _normalize_retro(event: dict[str, Any]) -> dict[str, Any] | None:
    try:
        score = int(event.get("fit_1_5"))
    except (TypeError, ValueError):
        return None
    if score < 1 or score > 5:
        return None
    return {
        "effort": str(event.get("effort") or ""),
        "unclear": str(event.get("unclear") or ""),
        "would_change": str(event.get("would_change") or ""),
        "fit_1_5": score,
    }


def init_failure(event: dict[str, Any]) -> str | None:
    """Why this event is not a ``dontAsk`` init, or None when it is.

    ``default`` is not success. A missing field fails closed. The real
    ``grok`` binary has to emit this event; the test stub is not that check.
    """
    if not event.get("init"):
        return "missing permissionMode init event"
    mode = event.get("permissionMode", event.get("permission_mode", event.get("mode")))
    if mode is None or not str(mode).strip():
        return "missing permissionMode on the streaming-messages init event"
    folded = str(mode).strip().lower().replace("-", "").replace("_", "")
    if folded != "dontask":
        return (
            "unexpected permission mode: "
            f"{mode} (expected dontAsk from the streaming-messages init event); "
            "refusing to continue"
        )
    return None


def _last_lines(text: str, count: int) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-count:])


def _is_server(action: dict[str, Any], command: str) -> bool:
    from taskbench.safety import is_long_lived

    return is_long_lived(action, command)


def _loopback_command(command: str) -> bool:
    from taskbench.safety import binds_loopback_only

    return binds_loopback_only(command)


def queue_view(store: StateStore) -> list[dict[str, Any]]:
    """Claimed tasks with position (running counts as position 1) and wait seconds."""
    running = store.packet_paths(store.running) + store.packet_paths(store.needs_approval)
    ahead = 1 if running else 0
    claimed: list[dict[str, Any]] = []
    from taskbench.task_format import parse_stored

    for path in store.packet_paths(store.claimed):
        raw = parse_stored(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            claimed.append(raw)
    claimed.sort(key=lambda packet: str(packet.get("claimed_at") or ""))
    now = time.time()
    view = []
    for index, packet in enumerate(claimed, start=1):
        claimed_at = packet.get("claimed_at")
        wait = 0
        if isinstance(claimed_at, str):
            try:
                from datetime import datetime

                parsed = datetime.fromisoformat(claimed_at)
                wait = max(0, int(now - parsed.timestamp()))
            except ValueError:
                wait = 0
        view.append(
            {
                "id": packet.get("id"),
                "position": ahead + index,
                "wait_s": wait,
                "project": packet.get("project"),
                "from": packet.get("from"),
            }
        )
    return view
