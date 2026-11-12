"""HCCA sidecar for the DSH plugin (route A).

Serves the live collaboration snapshot and the dashboard UI over localhost.
The DSH host plugin spawns this process, points a `/api/hcca/*` proxy at it, and
the Web client's HCCA conversation-view tab fetches `/api/hcca/status`.

Usage:
    python -m hcca.dsh_worker --runs runs --port 8765
    python -m hcca.dsh_worker --run runs/example --port 8765

Endpoints:
    GET /                     dashboard HTML (serves a real run snapshot)
    GET /api/runs             [{"run_id", "status", "updated_at", "model"}]
    GET /api/status?run=ID    hcca.status.v1 snapshot
    GET /api/wait?run=ID      long-poll until the run is terminal (returns the
                              final snapshot incl. the report, or timed_out=true)
    GET /api/events?run=ID    SSE stream (1s) of snapshots, until terminal
    GET /healthz
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .guidance import GuidanceQueue

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS = PROJECT / "runs"
UI_FILE = Path(__file__).resolve().parent / "dsh_ui.html"

_PRICE = {"hit": 0.028e-6, "miss": 0.28e-6, "out": 0.42e-6}

#: Optional one-line GXWTF key file for the local DSH profile.
KEY_FILES = (Path.home() / ".dsh" / "gxwtf.key", Path.home() / ".dsh" / "gxwtf-key.txt")


def _read_key_file() -> str:
    for candidate in KEY_FILES:
        try:
            lines = candidate.read_text(encoding="utf-8").strip().splitlines()
        except OSError:
            continue
        if lines and lines[0].strip():
            return lines[0].strip()
    return ""


_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"'`，。；;、（）()\[\]<>|*?]*")
_POSIX_PATH_RE = re.compile(r"(?<![\w:/])/(?:[\w.\-]+/)+[\w.\-]+")


def _detect_workspace(objective: str) -> Path | None:
    """Find an existing directory named in the objective text.

    A real run once answered against the project root although the user's task
    lived in another folder: the model did not pass ``workspace`` and the sidecar
    silently fell back to the project.  When the objective itself names a real
    path, use it instead of the default workspace.
    """
    for match in _WIN_PATH_RE.finditer(objective):
        raw = match.group(0).rstrip("\\/ ")
        if not raw:
            continue
        # The regex may swallow a few CJK characters after the real directory
        # name; trim the tail until an existing directory is found.
        for _ in range(24):
            if not raw:
                break
            try:
                candidate = Path(raw).expanduser()
                if candidate.is_dir():
                    return candidate.resolve()
                if candidate.is_file():
                    return candidate.parent.resolve()
            except OSError:
                pass
            raw = raw[:-1].rstrip("\\/ ")
    for match in _POSIX_PATH_RE.finditer(objective):
        try:
            candidate = Path(match.group(0))
            if candidate.is_dir():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _workspace_of(body: Mapping[str, Any]) -> Path:
    """Use the requested workspace only when it is an existing directory."""
    raw = str(body.get("workspace") or "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
    detected = _detect_workspace(str(body.get("objective") or ""))
    if detected is not None:
        return detected
    return PROJECT


def _hydrate_llm_env() -> None:
    """Pick up a user-level GXWTF key when the Electron parent env lacks it."""
    if os.environ.get("HCCA_LLM_API_KEY") or os.environ.get("GXWTF_API_KEY"):
        return
    key = _read_key_file()
    if not key and sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
                key = str(winreg.QueryValueEx(handle, "GXWTF_API_KEY")[0]).strip()
        except OSError:
            key = ""
    if key:
        os.environ["GXWTF_API_KEY"] = key


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


_TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED", "INCOMPLETE"}
_ACTIVE_STATUSES = {"RUNNING", "WAITING", "READY", "CREATED", "QUEUED"}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) is NOT a liveness probe on Windows: any signal other
        # than CTRL_* terminates the process. Use OpenProcess instead.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:  # noqa: BLE001 - unknown platform behaviour
        return False
    return True


def _write_run_meta(run_dir: Path, meta: dict[str, Any]) -> None:
    try:
        (run_dir / "delegate.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def revive_stale_status(status: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """A killed runner cannot write its final status; do not show it as RUNNING."""
    if not status:
        return status
    current = str(status.get("status") or "").upper()
    if current in _TERMINAL_STATUSES:
        return status
    meta = _read_json(run_dir / "delegate.json")
    pid = int(status.get("runner_pid") or meta.get("pid") or 0)
    alive = _pid_alive(pid) if pid else False
    stale_by_time = False
    stamp = status.get("updated_at") or meta.get("started_at") or ""
    try:
        updated = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        stale_by_time = (datetime.now(timezone.utc) - updated).total_seconds() > 300
    except (ValueError, TypeError):
        pass
    if alive or not (pid or stale_by_time):
        return status
    status["status"] = "INCOMPLETE"
    status["error"] = "runner 进程已退出（没有写最终状态），此 run 已中断"
    status["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    for agent in status.get("agents") or []:
        if str(agent.get("status") or "").upper() in _ACTIVE_STATUSES:
            agent["status"] = "INTERRUPTED"
    try:
        (run_dir / "status.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
    return status


def sweep_stale_runs(runs_dir: Path) -> None:
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        status = _read_json(run_dir / "status.json")
        if status:
            revive_stale_status(status, run_dir)


def _derive_status(run_dir: Path) -> dict[str, Any]:
    """Build a snapshot from an old run.json + trace.jsonl (no status.json)."""

    run = _read_json(run_dir / "run.json")
    if not run:
        return {"schema": "hcca.status.v1", "run_id": run_dir.name, "status": "MISSING"}
    calls = list(run.get("calls") or [])
    agents: dict[str, dict[str, Any]] = {
        "root": {
            "agent_id": "root",
            "parent_id": None,
            "role": "root",
            "dimension": "",
            "task_id": "root",
            "status": "COMPLETED" if run.get("status") == "COMPLETED" else "FAILED",
            "depth": 0,
            "generation": 0,
            "waiting_on": None,
            "blocked_by": [],
            "next_step": "整合并提交最终报告",
            "objective": run.get("goal") or "Task",
            "children": [],
        }
    }
    dims = ["financial", "policy", "macro"]
    analysts = 0
    for call in calls:
        agent_id = str(call.get("agent_id") or "")
        if not agent_id or agent_id in agents or agent_id == "judge":
            continue
        tag = str(call.get("tag") or "")
        role = "verifier" if "verifier" in tag else ("analyst" if "analyst" in tag else "agent")
        dimension = ""
        if role == "analyst" and analysts < len(dims):
            dimension = dims[analysts]
            analysts += 1
        agents[agent_id] = {
            "agent_id": agent_id,
            "parent_id": "root",
            "role": role,
            "dimension": dimension,
            "task_id": f"case.{dimension or role}",
            "status": "COMPLETED",
            "depth": 1,
            "generation": 1,
            "waiting_on": None,
            "blocked_by": [],
            "next_step": "已完成",
            "objective": "",
            "children": [],
        }
        agents["root"]["children"].append(agent_id)
    hit = sum(int(c.get("cache_hit_tokens") or 0) for c in calls)
    miss = sum(int(c.get("cache_miss_tokens") or 0) for c in calls)
    out = sum(int(c.get("completion_tokens") or 0) for c in calls)
    events = []
    if isinstance(run.get("runtime"), Mapping):
        events = list(run["runtime"].get("events") or [])
    return {
        "schema": "hcca.status.v1",
        "run_id": run_dir.name,
        "goal": run.get("goal") or "Task",
        "goal_id": run.get("goal_id") or "",
        "status": run.get("status") or "UNKNOWN",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "started_at": run.get("started_at") or "",
        "elapsed_s": run.get("duration_s") or 0,
        "model": run.get("model") or "unknown",
        "mode": "tools",
        "agents": list(agents.values()),
        "edges": [
            {"from": "root", "to": agent_id, "kind": "supervises"}
            for agent_id in agents
            if agent_id != "root"
        ],
        "messages": {
            "sent": run.get("messages_sent", 0),
            "processed": run.get("messages_processed", 0),
            "wakes": run.get("wakes", 0),
            "coalesced": run.get("coalesced", 0),
        },
        "tokens": {
            "input": hit + miss,
            "cached": hit,
            "cache_hit_rate": round(hit / (hit + miss), 4) if hit + miss else 0.0,
            "output": out,
            "reasoning": run.get("total_reasoning_tokens", 0),
            "cost_estimate": round(
                hit * _PRICE["hit"] + miss * _PRICE["miss"] + out * _PRICE["out"], 4
            ),
        },
        "calls": calls,
        "events": events[-80:],
        "wait_graph": {},
        "checks": run.get("checks") or {},
        "judge": run.get("judge") or {},
        "report_chars": len(str(run.get("report") or "")),
        "report": str(run.get("report") or ""),
        "error": run.get("error") or "",
    }


def load_status(runs_dir: Path, run_id: str) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    status = _read_json(run_dir / "status.json")
    status = status or _derive_status(run_dir)
    status["guidance"] = GuidanceQueue(run_dir).snapshot()
    return status


def _session_slug(session: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(session))
    return keep.strip("-")[:60]


def find_session_run(runs_dir: Path, session: str) -> str:
    slug = _session_slug(session)
    if not slug:
        return ""
    for run_dir in sorted((p for p in runs_dir.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        if run_dir.name.startswith(slug + "-") or run_dir.name == slug:
            return run_dir.name
    return ""


def empty_status(session: str = "") -> dict[str, Any]:
    return {
        "schema": "hcca.status.v1",
        "run_id": "",
        "session": session,
        "goal": "",
        "status": "IDLE",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "started_at": "",
        "elapsed_s": 0,
        "model": "",
        "mode": "tools",
        "agents": [],
        "edges": [],
        "messages": {"sent": 0, "processed": 0, "wakes": 0, "coalesced": 0},
        "tokens": {"input": 0, "cached": 0, "cache_hit_rate": 0.0, "output": 0, "reasoning": 0, "cost_estimate": 0},
        "calls": [],
        "events": [],
        "wait_graph": {},
        "checks": {},
        "judge": {},
        "report_chars": 0,
        "report": "",
        "error": "",
    }


def session_worker_status(runs_dir: Path, session: str) -> dict[str, Any] | None:
    """Merge this DSH session's workers locally; DSH Root usage stays separate."""
    snapshots = []
    for directory in runs_dir.iterdir():
        if not directory.is_dir():
            continue
        metadata = _read_json(directory / "delegate.json")
        if metadata.get("session") != session or not metadata.get("external_root"):
            continue
        status = revive_stale_status(load_status(runs_dir, directory.name), directory)
        status.update(run_id=directory.name, external_root=True, worker_role=metadata.get("role", "analyst"))
        status.setdefault("status", "CREATED")
        status.setdefault("goal", metadata.get("objective", ""))
        snapshots.append(status)
    if not snapshots:
        return None
    snapshots.sort(key=lambda item: item["run_id"])
    merged = {**empty_status(session), **snapshots[-1], "session": session, "external_root": True,
              "external_root_id": "dsh-root", "root_label": "DSH Root", "mode": "external-worker-session",
              "usage_scope": "hcca_workers_only", "root_usage_included": False,
              "run_ids": [item["run_id"] for item in snapshots],
              "worker_runs": [{"run_id": item["run_id"], "role": item["worker_role"], "status": item["status"],
                               "goal": item.get("goal", "")} for item in snapshots]}
    if any(str(item["status"]).upper() not in _TERMINAL_STATUSES for item in snapshots):
        merged["status"] = "RUNNING"
    for field in ("agents", "edges", "calls", "executions", "readings", "pending_calls", "pending_tools", "guidance", "events", "message_previews"):
        merged[field] = [{**record, "run_id": item["run_id"]} for item in snapshots for record in item.get(field, [])]
    counters = ("input", "cached", "output", "reasoning", "cost_estimate", "usage_missing_attempts", "usage_partial_attempts",
                "usage_incomplete_attempts", "cache_usage_incomplete_attempts")
    merged["tokens"] = {key: sum((item.get("tokens") or {}).get(key, 0) or 0 for item in snapshots) for key in counters}
    merged["tokens"]["cache_hit_rate"] = merged["tokens"]["cached"] / merged["tokens"]["input"] if merged["tokens"]["input"] else 0.0
    for field in ("usage_complete", "cache_usage_complete"):
        merged["tokens"][field] = all((item.get("tokens") or {}).get(field, False) for item in snapshots)
    merged["messages"] = {key: sum((item.get("messages") or {}).get(key, 0) for item in snapshots)
                          for key in ("sent", "processed", "wakes", "coalesced")}
    tools = {"enabled": True, "calls": 0, "counts": {}, "per_agent": {}, "recent_by_agent": {}, "log": []}
    for item in snapshots:
        source = item.get("tools") or {}
        tools["calls"] += source.get("calls", 0)
        for key, count in source.get("counts", {}).items():
            tools["counts"][key] = tools["counts"].get(key, 0) + count
        for key in ("per_agent", "recent_by_agent"):
            tools[key].update(source.get(key, {}))
        tools["log"].extend({**entry, "run_id": item["run_id"]} for entry in source.get("log", []))
    merged["tools"] = tools
    merged["updated_at"] = max((item.get("updated_at") or "") for item in snapshots)
    return merged


def list_runs(runs_dir: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for run_dir in sorted(
        (p for p in runs_dir.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True
    ):
        status = _read_json(run_dir / "status.json")
        run = _read_json(run_dir / "run.json")
        if not status and not run:
            continue
        items.append(
            {
                "run_id": run_dir.name,
                "status": status.get("status") or run.get("status") or "UNKNOWN",
                "updated_at": status.get("updated_at") or "",
                "model": status.get("model") or run.get("model") or "",
                "agents": len(status.get("agents") or []) or run.get("agents") or 0,
            }
        )
    return items


class Handler(BaseHTTPRequestHandler):
    runs_dir: Path = DEFAULT_RUNS
    server_version = "hcca-sidecar/1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 - quiet
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        from urllib.parse import urlparse

        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}
        if not isinstance(body, dict):
            self._send(400, b'{"error":"JSON object is required"}', "application/json")
            return
        if parsed.path == "/api/delegate":
            result = self._delegate(body)
            code = 409 if result.get("status") == "workspace_busy" else 400 if result.get("error") else 200
            self._send(code, json.dumps(result, ensure_ascii=False).encode(), "application/json")
            return
        if parsed.path == "/api/guidance":
            code, result = self._guidance(body)
            self._send(code, json.dumps(result, ensure_ascii=False).encode(), "application/json")
            return
        if parsed.path == "/api/cancel":
            run_id = str(body.get("run_id") or "")
            proc = self.server.delegations.pop(run_id, None)  # type: ignore[attr-defined]
            if proc is not None and proc.poll() is None:
                proc.terminate()
                self._mark_cancelled(run_id)
                return self._send(200, json.dumps({"run_id": run_id, "status": "cancelled"}).encode(), "application/json")
            self._send(404, json.dumps({"error": "run not found or already finished"}).encode(), "application/json")
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def _guidance(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        lock = getattr(getattr(self, "server", None), "delegate_lock", None)
        if lock is None:
            return self._guidance_locked(body)
        with lock:
            return self._guidance_locked(body)

    def _guidance_locked(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        if not isinstance(body.get("text"), str) or not body["text"].strip():
            return 400, {"error": "text is required"}
        if not body.get("run_id"):
            return 400, {"error": "run_id is required; guidance is directed to one task, never broadcast"}
        return self._guidance_one(body)

    def _guidance_one(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return 400, {"error": "text is required"}
        session = str(body.get("session") or "")
        run_id = str(body.get("run_id") or "") or (find_session_run(self.runs_dir, session) if session else "")
        if not run_id:
            return 404, {"error": "no run for this session"}
        run_dir = (self.runs_dir / run_id).resolve()
        if run_dir.parent != self.runs_dir.resolve() or not run_dir.is_dir():
            return 404, {"error": "run not found"}
        metadata = _read_json(run_dir / "delegate.json")
        if session and metadata.get("session") != session:
            return 403, {"error": "run does not belong to this session"}
        status = revive_stale_status(load_status(self.runs_dir, run_id), run_dir)
        if str(status.get("status") or "").upper() in _TERMINAL_STATUSES:
            return 409, {"error": "run has already finished", "run_id": run_id}
        target = metadata.get("worker_agent_id") if metadata.get("external_root") else "root"
        if not target:
            return 409, {"error": "task has no top-level worker identity", "run_id": run_id}
        recipients = [target]
        try:
            result = GuidanceQueue(run_dir).publish(text, recipients, str(body.get("message_id") or ""))
        except ValueError as error:
            return 409, {"error": str(error), "run_id": run_id}
        return 200, {"run_id": run_id, **result}

    def _wait_for_run(self, run_id: str, timeout: float) -> dict[str, Any]:
        """Block until ``run_id`` reaches a terminal status (or timeout)."""
        deadline = time.time() + timeout
        status: dict[str, Any] = {}
        while True:
            status = revive_stale_status(load_status(self.runs_dir, run_id), self.runs_dir / run_id)
            if str(status.get("status") or "").upper() in _TERMINAL_STATUSES:
                return status
            if time.time() >= deadline:
                status["timed_out"] = True
                return status
            time.sleep(1.0)

    def _mark_cancelled(self, run_id: str) -> None:
        """The killed child cannot write its final status; record it for the UI."""
        status_path = self.runs_dir / run_id / "status.json"
        data = _read_json(status_path)
        if not data:
            return
        data["status"] = "CANCELLED"
        data["error"] = "cancelled by user"
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        try:
            status_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _active_run_for_workspace(self, workspace: str, *, except_run_id: str = "") -> str | None:
        """Find a live run that already owns ``workspace`` so callers can attach."""
        for run_id, process in getattr(getattr(self, "server", None), "delegations", {}).items():
            if run_id != except_run_id and process.poll() is None:
                metadata = _read_json(self.runs_dir / run_id / "delegate.json")
                if metadata.get("workspace") == workspace:
                    return run_id
        try:
            entries = sorted(
                (item for item in self.runs_dir.iterdir() if item.is_dir()),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return None
        for entry in entries[:30]:
            if entry.name == except_run_id:
                continue
            status = load_status(self.runs_dir, entry.name)
            if str(status.get("status") or "").upper() not in {"RUNNING", "WAITING", "CREATED"}:
                continue
            if str(status.get("workspace") or "") != workspace:
                continue
            try:
                pid = int(status.get("runner_pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            if pid and _pid_alive(pid):
                return entry.name
        return None

    def _delegate(self, body: Mapping[str, Any]) -> dict[str, Any]:
        lock = getattr(self.server, "delegate_lock", None)
        if lock is None:
            return self._delegate_locked(body)
        with lock:
            return self._delegate_locked(body)

    def _delegate_locked(self, body: Mapping[str, Any]) -> dict[str, Any]:
        objective = str(body.get("objective") or "").strip()
        if not objective:
            return {"error": "objective is required"}
        session = str(body.get("session") or "")
        external = body.get("external_root") is True
        role = str(body.get("role") or "analyst")
        worker_agent_id = {"reader": "reading_", "executor": "execution_", "analyst": "analyst_"}.get(role, "analyst_") + uuid4().hex if external else "root"
        if external and role not in {"reader", "executor", "analyst"}:
            return {"error": "role must be reader, executor, or analyst"}
        slug = _session_slug(session) or "dsh"
        run_id = str(body.get("run_id") or f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}")
        if body.get("dry_run"):
            return {"run_id": run_id, "status": "dry-run", "objective": objective}
        workspace = _workspace_of(body)
        run_dir = self.runs_dir / run_id
        if run_dir.resolve().parent != self.runs_dir.resolve():
            return {"error": "run_id must name a direct run directory"}
        if external and run_dir.exists():
            return {"error": "run_id already exists; the previous task was not overwritten"}
        if external and role == "executor":
            for directory in self.runs_dir.iterdir():
                if not directory.is_dir():
                    continue
                metadata = _read_json(directory / "delegate.json")
                if metadata.get("workspace") != str(workspace) or (metadata.get("external_root") and metadata.get("role") != "executor"):
                    continue
                status = _read_json(directory / "status.json")
                process = getattr(self.server, "delegations", {}).get(directory.name)
                live = process.poll() is None if process is not None else _pid_alive(int(metadata.get("pid") or status.get("runner_pid") or 0))
                if live and str(status.get("status") or "").upper() not in _TERMINAL_STATUSES:
                    return {"error": "workspace already has an active writer; this new task was not started",
                            "status": "workspace_busy", "active_run_id": directory.name, "workspace": str(workspace)}
        run_dir.mkdir(parents=True, exist_ok=True)
        if not external and not body.get("force"):
            active = self._active_run_for_workspace(str(workspace), except_run_id=run_id)
            if active:
                try:
                    run_dir.rmdir()
                except OSError:
                    pass
                return {
                    "run_id": active,
                    "status": "already_running",
                    "reused": True,
                    "workspace": str(workspace),
                    "message": f"工作区已有 run 在跑（{active}），本次不重复启动；等待它完成。",
                }
        env = dict(os.environ)
        # Explicit HCCA_LLM_* settings override the GXWTF integration defaults.
        if not env.get("HCCA_LLM_API_KEY"):
            env["HCCA_LLM_API_KEY"] = (
                env.get("GXWTF_API_KEY", "")
                or _read_key_file()
                or env.get("DEEPSEEK_API_KEY", "")
            )
        env.setdefault("HCCA_LLM_BASE_URL", "https://ai.gxwtf.cn/v1")
        env.setdefault("HCCA_LLM_MODEL", "deepseek-v4.1-flash")
        env.setdefault("HCCA_LLM_REASONING_EFFORT", "high")
        env.setdefault("HCCA_LLM_JSON_MODE", "0")
        env.setdefault("HCCA_LLM_TIMEOUT", "240")
        # Preserve the GXWTF integration compatibility default. Explicit TLS
        # verification settings take precedence.
        env.setdefault("HCCA_LLM_VERIFY_TLS", "0")
        cmd = [
            sys.executable,
            "-m",
            "hcca.runner",
            "--out",
            str(run_dir),
            "--goal",
            objective,
            "--workspace",
            str(workspace),
        ]
        if external:
            request = {"external_root": True, "role": role, "objective": objective, "workspace": str(workspace),
                       "out": str(run_dir), "mode": body.get("mode", "fix"), "paths": body.get("paths", ["."]),
                       "acceptance": body.get("acceptance", ""), "max_wall_seconds": body.get("max_wall_seconds", 0),
                       "worker_agent_id": worker_agent_id}
            request_path = run_dir / "task-request.json"
            request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            cmd = [sys.executable, "-m", "hcca.dsh_task", "--request", str(request_path)]
        else:
            cmd.append("--enable-guidance")
        if body.get("no_tools") and not external:
            cmd.append("--no-tools")
        log = (run_dir / "delegate.log").open("w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            log.close()
        except OSError:
            pass
        self.server.delegations[run_id] = proc  # type: ignore[attr-defined]
        _write_run_meta(
            run_dir,
            {
                "run_id": run_id,
                "pid": proc.pid,
                "objective": objective,
                "workspace": str(workspace),
                "session": session,
                "external_root": external,
                "role": role if external else "root",
                "worker_agent_id": worker_agent_id,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            },
        )
        return {
            "run_id": run_id,
            "status": "started",
            "objective": objective,
            "workspace": str(workspace),
            "session": session,
            "external_root": external,
            "role": role if external else "root",
            "worker_agent_id": worker_agent_id,
            "poll": f"/api/status?run={run_id}",
        }

    def do_GET(self) -> None:  # noqa: N802
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path in ("/", "/index.html", "/dashboard"):
            html = UI_FILE.read_text(encoding="utf-8") if UI_FILE.exists() else "<h1>HCCA UI missing</h1>"
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/healthz":
            self._send(200, b'{"ok": true, "version": "0.2.1"}', "application/json")
            return
        if parsed.path == "/api/runs":
            items = list_runs(self.runs_dir)
            session = (query.get("session") or [""])[0]
            if session:
                slug = _session_slug(session)
                items = [item for item in items if item["run_id"].startswith(slug + "-") or item["run_id"] == slug]
            self._send(200, json.dumps(items, ensure_ascii=False).encode(), "application/json")
            return
        if parsed.path == "/api/status":
            run_id = (query.get("run") or [""])[0]
            session = (query.get("session") or [""])[0]
            if not run_id and session:
                combined = session_worker_status(self.runs_dir, session)
                if combined is not None:
                    self._send(200, json.dumps(combined, ensure_ascii=False).encode(), "application/json")
                    return
            if not run_id and session:
                run_id = find_session_run(self.runs_dir, session)
            if not run_id and not session:
                runs = list_runs(self.runs_dir)
                run_id = runs[0]["run_id"] if runs else ""
            if not run_id:
                self._send(200, json.dumps(empty_status(session), ensure_ascii=False).encode(), "application/json")
                return
            status = revive_stale_status(load_status(self.runs_dir, run_id), self.runs_dir / run_id)
            if session:
                status["session"] = session
            self._send(200, json.dumps(status, ensure_ascii=False).encode(), "application/json")
            return
        if parsed.path == "/api/wait":
            # Long-poll: block until the run is terminal so the caller can return
            # the finished report instead of "started, poll later".
            run_id = (query.get("run") or [""])[0]
            if not run_id:
                self._send(400, b'{"error":"run is required"}', "application/json")
                return
            try:
                timeout = float((query.get("timeout") or ["1800"])[0])
            except ValueError:
                timeout = 1800.0
            status = self._wait_for_run(run_id, min(max(1.0, timeout), 6 * 3600))
            self._send(200, json.dumps(status, ensure_ascii=False).encode(), "application/json")
            return
        if parsed.path == "/api/events":
            run_id = (query.get("run") or [""])[0]
            if not run_id:
                runs = list_runs(self.runs_dir)
                run_id = runs[0]["run_id"] if runs else ""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = ""
            try:
                for _ in range(3600):
                    status = load_status(self.runs_dir, run_id)
                    body = json.dumps(status, ensure_ascii=False)
                    if body != last:
                        self.wfile.write(f"data: {body}\n\n".encode())
                        self.wfile.flush()
                        last = body
                    else:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                    if str(status.get("status")).upper() in {"COMPLETED", "FAILED", "INCOMPLETE"}:
                        break
                    time.sleep(1.0)
            except (BrokenPipeError, ConnectionResetError):
                return
            return
        self._send(404, b'{"error":"not found"}', "application/json")


class _Server(ThreadingHTTPServer):
    # Never share the port: a stale sidecar from an older app boot must not keep
    # serving old code alongside the freshly spawned one.
    allow_reuse_address = False
    daemon_threads = True


def _bind_with_retry(host: str, port: int, handler: Any, *, timeout: float = 20.0) -> _Server:
    """A killed predecessor may not release the socket instantly; wait for it.

    Exiting on the first ``EADDRINUSE`` looked like a Python crash (the host
    killed the stale sidecar and spawned ours a few ms later). Retry instead.
    """
    deadline = time.time() + timeout
    last_error: OSError | None = None
    while True:
        try:
            return _Server((host, port), handler)
        except OSError as exc:
            last_error = exc
            if time.time() >= deadline:
                raise
            time.sleep(0.4)
    raise last_error  # pragma: no cover - unreachable


def serve(runs_dir: Path, port: int, host: str = "127.0.0.1") -> None:  # pragma: no cover
    handler = type("HccaHandler", (Handler,), {"runs_dir": runs_dir})
    server = _bind_with_retry(host, port, handler)
    sweep_stale_runs(runs_dir)
    server.delegations = {}
    server.delegate_lock = threading.Lock()
    pid_file = Path.home() / ".dsh" / f"hcca-sidecar-{port}.pid"
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        atexit.register(lambda: pid_file.unlink(missing_ok=True))
    except OSError:
        pass
    print(f"hcca sidecar: http://{host}:{server.server_address[1]}/", flush=True)
    server.serve_forever()


def main() -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", default=str(DEFAULT_RUNS))
    parser.add_argument("--run", default="", help="single run id (default: newest)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    runs_dir = Path(args.runs)
    if args.run:
        run_dir = runs_dir / args.run
        runs_dir = run_dir.parent
    _hydrate_llm_env()
    try:
        serve(runs_dir, args.port, args.host)
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001 - write a traceback somewhere visible
        import traceback

        log = Path.home() / ".dsh" / "hcca-sidecar.log"
        try:
            with log.open("a", encoding="utf-8") as handle:
                handle.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} sidecar crashed ===\n")
                traceback.print_exc(file=handle)
        except OSError:
            pass
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
