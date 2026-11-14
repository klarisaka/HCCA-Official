"""Fresh, bounded execution workers with one shared role configuration."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from .agent_tools import AgentToolbox
from .errors import ToolError
from .execution_environment import execution_environment
from .execution_resume import TOOL_SELECTION_ERRORS, command_exit_code, load_execution_resume, native_assistant, update_tool_errors
from .reporting import REPORT_FINAL_INSTRUCTION, REPORT_TOOL_CHOICE, report_schema, submitted_report
from .request_budget import BudgetExceeded, CallBudget
from .tokens import estimate_tokens

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_BUNDLED_PROMPT = Path(__file__).with_name("prompts") / "executor.md"
CONTEXT_BUDGET_TOKENS = 65_536


def bounded_tool_messages(
    initial: list[dict[str, Any]], history: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
    context_budget_tokens: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep complete native exchanges within a soft, locally estimated budget."""
    context_budget_tokens = CONTEXT_BUDGET_TOKENS if context_budget_tokens is None else context_budget_tokens
    dropped = 0
    while True:
        messages = [*initial, *history]
        tokens = estimate_tokens(json.dumps(
            {"messages": messages, "tools": tools or []}, ensure_ascii=False, separators=(",", ":"),
        ))
        next_exchange = next((
            index for index, message in enumerate(history[1:], 1) if message.get("role") == "assistant"
        ), None)
        if tokens <= context_budget_tokens or next_exchange is None:
            return messages, {
                "context_budget_tokens": context_budget_tokens,
                "context_tokens_estimated": tokens,
                "context_estimate_source": "local_estimator",
                "context_exchanges_dropped": dropped,
                "context_soft_budget_exceeded": tokens > context_budget_tokens,
            }
        del history[:next_exchange]
        dropped += 1


def executor_prompt_path() -> Path:
    """Return the shared user-editable configuration path without creating it."""
    agent_home = os.environ.get("HCCA_AGENT_HOME")
    home = Path(agent_home).expanduser() if agent_home else Path.home() / ".hcca" / "agents"
    return home / "executor.md"


class ExecutionService:
    """Run each delegation in a fresh context, serializing jobs per workspace."""

    def __init__(
        self,
        workspace: Path,
        out_dir: Path,
        toolbox: AgentToolbox,
        complete: Callable[..., Any],
        max_rounds: int = 16,
        context_budget_tokens: int | None = None,
        take_guidance: Callable[[str], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.out_dir = Path(out_dir).resolve()
        self.toolbox = toolbox
        self.complete = complete
        self.take_guidance = take_guidance
        self.max_rounds = max(0, int(max_rounds))
        self.context_budget_tokens = max(1, int(CONTEXT_BUDGET_TOKENS if context_budget_tokens is None else context_budget_tokens))
        self.jobs: list[dict[str, Any]] = []
        self.tool_log: list[dict[str, Any]] = []
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(os.path.normcase(str(self.workspace)), threading.Lock())

    @staticmethod
    def schema() -> dict[str, Any]:
        return AgentToolbox._fn(
            "delegate_execution",
            "Delegate commands, tests, or a narrowly scoped bugfix to a fresh execution worker. "
            "It returns a compact result and a full log path. Fix jobs require named files or existing workspace directories.",
            {
                "task": {"type": "string", "maxLength": 8000},
                "mode": {"type": "string", "enum": ["command", "test", "fix"]},
                "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                "acceptance": {"type": "string", "maxLength": 2000},
            },
            ["task", "mode"],
        )

    @staticmethod
    def log_schema() -> dict[str, Any]:
        return AgentToolbox._fn(
            "read_execution_log",
            "Read a known execution transcript or a saved command output file.",
            {
                "job_id": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
                "char_offset": {"type": "integer", "minimum": 0},
                "output_file": {"type": "string", "description": "Optional saved command output filename."},
            },
            ["job_id"],
        )

    def read_log(self, arguments: Mapping[str, Any]) -> str:
        job = next((item for item in self.jobs if item["job_id"] == arguments.get("job_id")), None)
        if job is None:
            return "ERROR: unknown execution job"
        start = arguments.get("start_line", 1)
        end = arguments.get("end_line", start + 19 if type(start) is int else 20)
        offset = arguments.get("char_offset", 0)
        if type(start) is not int or type(end) is not int or start < 1 or end < start:
            return "ERROR: invalid execution log line range"
        if type(offset) is not int or offset < 0:
            return "ERROR: invalid execution log character offset"
        lines: list[str] = []
        remaining = 5600
        last_line = start - 1
        next_line: int | None = None
        next_offset: int | None = None
        path = Path(job["log_path"])
        output_file = arguments.get("output_file")
        if output_file is not None:
            if not isinstance(output_file, str) or not re.fullmatch(r"[a-f0-9]{32}\.log", output_file):
                return "ERROR: invalid command output filename"
            log_root = path.parent.resolve()
            expected_directory = log_root / f"{job['job_id']}.commands"
            directory = expected_directory.resolve()
            if directory != expected_directory:
                return "ERROR: command output escapes its job"
            path = (directory / output_file).resolve()
            if not path.is_relative_to(directory.resolve()):
                return "ERROR: command output escapes its job"
        try:
            with path.open(encoding="utf-8") as log:
                for number, line in enumerate(log, 1):
                    if number < start:
                        continue
                    if number > end or remaining <= 0:
                        next_line, next_offset = number, 0
                        break
                    line_offset = offset if number == start else 0
                    if line_offset > len(line):
                        return "ERROR: character offset exceeds the selected log line"
                    segment = line[line_offset:]
                    chunk = segment[:remaining]
                    lines.append(chunk)
                    remaining -= len(chunk)
                    last_line = number
                    if len(chunk) < len(segment):
                        next_line, next_offset = number, line_offset + len(chunk)
                        break
        except OSError:
            return "ERROR: execution log unavailable"
        if not lines:
            return f"Execution log {job['job_id']} has no lines in range {start}-{end}."
        header = f"Execution log {job['job_id']} lines {start}-{last_line}\n"
        continuation = json.dumps({"next_start_line": next_line, "next_char_offset": next_offset})
        return (header + "Continuation " + continuation + "\n" + "".join(lines) + ("\n[Output truncated]" if remaining <= 0 else ""))[:6000]

    def _path(self, value: str) -> Path:
        path = Path(value.replace("\\", "/"))
        if path.is_absolute() or PureWindowsPath(value).drive or PureWindowsPath(value).root:
            raise ValueError("paths must be relative to the workspace")
        resolved = (self.workspace / path).resolve()
        if not resolved.is_relative_to(self.workspace):
            raise ValueError("path escapes the workspace")
        return resolved

    def _envelope(self, arguments: Mapping[str, Any], allow_write: bool) -> dict[str, Any]:
        task = arguments.get("task")
        if not isinstance(task, str) or not task.strip() or len(task) > 8000:
            raise ValueError("task must be a nonempty string of at most 8000 characters")
        mode = arguments.get("mode")
        if mode not in ("command", "test", "fix"):
            raise ValueError("mode must be command, test, or fix")
        acceptance = arguments.get("acceptance", "")
        if not isinstance(acceptance, str) or len(acceptance) > 2000:
            raise ValueError("acceptance must be a string of at most 2000 characters")
        paths = arguments.get("paths", [])
        if not isinstance(paths, list) or len(paths) > 20:
            raise ValueError("paths must be an array of at most 20 relative paths")
        normalized: list[str] = []
        for value in paths:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("each path must be a nonempty relative path")
            candidate = self._path(value)
            if mode == "fix" and value.endswith(("/", "\\")) and not candidate.is_dir():
                raise ValueError("fix directory scopes must already exist")
            normalized.append(candidate.relative_to(self.workspace).as_posix())
        if mode == "fix" and not allow_write:
            raise ValueError("fix jobs are disabled for a read-only requester")
        if mode == "fix" and not normalized:
            raise ValueError("fix jobs require at least one named file or existing directory")
        return {
            "environment": execution_environment(workspace=self.workspace, persistent_commands=True),
            "workspace": str(self.workspace),
            "mode": mode,
            "paths": normalized,
            "allow_write": bool(allow_write),
            "task": task.strip(),
            "acceptance": acceptance,
        }

    def run(
        self,
        arguments: Mapping[str, Any],
        *,
        requester_id: str,
        allow_write: bool = True,
        budget: CallBudget | None = None,
        capture_workspace_diff: Callable[[], dict[str, Any] | None] | None = None,
        resume_from: str | Path | None = None,
        resume_plan: dict[str, Any] | None = None,
        agent_id: str | None = None,
    ) -> str:
        resume_state = load_execution_resume(resume_from) if resume_from is not None else None
        if resume_plan is not None and not any(resume_plan.get(key) for key in ("attempted", "completed")):
            raise ValueError("execution planning has no recorded attempt")
        if resume_state is not None and resume_plan is not None and not any(
            resume_state.get("resume_planning", {}).get(key) for key in ("attempted", "completed")
        ):
            if not any(resume_plan.get(key) for key in ("attempted", "completed")):
                raise ValueError("resume planning has no recorded attempt")
            if str(resume_plan.get("text", "")).strip():
                resume_state["history"].append({"role": "user", "content": "本次主控的下一步执行建议：\n" + resume_plan["text"]})
            resume_state["resume_planning"] = resume_plan
        job_id = uuid4().hex
        agent_id = agent_id or f"execution_{job_id}"
        log_path = self.out_dir / "executions" / f"{job_id}.jsonl"
        job: dict[str, Any] = {
            "job_id": job_id,
            "agent_id": agent_id,
            "requester_id": requester_id,
            "status": "queued",
            "task": str(arguments.get("task", ""))[:8000],
            "mode": str(arguments.get("mode", "")),
            "log_path": str(log_path),
        }
        self.jobs.append(job)
        started = time.monotonic()
        status, summary = "failed", "Execution failed. See the execution log."
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("w", encoding="utf-8") as log:
                def record(event: str, **data: Any) -> None:
                    log.write(json.dumps({"event": event, **data}, ensure_ascii=False, default=str) + "\n")
                    log.flush()
                    if event in ("checkpoint", "response", "tool_started", "tool"):
                        os.fsync(log.fileno())

                acquired = False
                try:
                    if budget is not None:
                        budget.check()
                    wait = budget.remaining_time if budget is not None else None
                    acquired = self._lock.acquire() if wait is None else self._lock.acquire(timeout=max(0.0, wait))
                    if not acquired:
                        raise BudgetExceeded("deadline")
                    if budget is not None:
                        budget.check()
                    job.update(status="running", started_at=datetime.now(timezone.utc).isoformat())
                    try:
                        envelope = self._envelope(arguments, allow_write)
                        if resume_state is not None:
                            original = resume_state["envelope"]
                            if Path(original["workspace"]).resolve() != self.workspace:
                                raise ValueError("resume workspace differs from the original workspace")
                            if original.get("task") != envelope["task"] or original.get("paths") != envelope["paths"]:
                                raise ValueError("resume task or file scope differs from the original execution")
                            if original.get("mode") != envelope["mode"]:
                                raise ValueError("resume mode differs from the original execution")
                            if original.get("mode") == "fix" and not allow_write:
                                raise ValueError("fix jobs are disabled for a read-only requester")
                            envelope = original
                    except (ValueError, TypeError) as exc:
                        status, summary = "blocked", str(exc)
                        record("rejected", reason=summary)
                    else:
                        job["paths"] = envelope["paths"]
                        record("job", agent_id=agent_id, requester_id=requester_id, **envelope)
                        if resume_state is not None:
                            job["resumed_from"] = resume_state["provenance"]
                            record("resume", provenance=resume_state["provenance"],
                                   legacy_restore_limits=resume_state.get("legacy_restore_limits"))
                        try:
                            before = capture_workspace_diff() if capture_workspace_diff is not None else None
                            if resume_state is not None and before is not None:
                                expected = resume_state.get("workspace_after")
                                record("workspace_restored", snapshot=before, expected=expected)
                                if expected is not None and any(
                                    expected.get(key) is not None and before.get(key) != expected[key]
                                    for key in ("diff_sha256", "workspace_change_sha")
                                ):
                                    raise ValueError("restored workspace patch differs from the saved execution")
                                before = resume_state.get("workspace_before") or before
                            if capture_workspace_diff is not None:
                                job["workspace_before"] = before
                                record("workspace_before", snapshot=before)
                            if capture_workspace_diff is not None and before is None:
                                status, summary = "blocked", "Could not verify the workspace patch before repair."
                            else:
                                status, summary = self._work(
                                    envelope, agent_id, job_id, record, budget,
                                    resume_state=resume_state, workspace_before=before,
                                    planning=resume_plan,
                                )
                                if capture_workspace_diff is not None:
                                    after = capture_workspace_diff()
                                    job["workspace_after"] = after
                                    record("workspace_after", snapshot=after)
                                    if status == "completed":
                                        if after is None:
                                            status, summary = "blocked", "Could not verify the workspace patch after repair. " + summary
                                        elif (
                                            after.get("workspace_change_sha", after["diff_sha256"])
                                            == before.get("workspace_change_sha", before["diff_sha256"])
                                        ):
                                            status, summary = "failed", "Repair produced no new workspace change. " + summary
                        except Exception as exc:  # A worker failure must stay a failed delegation.
                            record("worker_error", error=f"{type(exc).__name__}: {exc}")
                            status, summary = "failed", "Executor failed. See the execution log."
                except BudgetExceeded as exc:
                    status, summary = "blocked", f"Execution budget ended before work started: {exc}."
                    record("budget_cutoff", reason=str(exc))
                finally:
                    if acquired:
                        self._lock.release()
                record("final", status=status, summary=summary)
        except OSError:
            status, summary = "failed", "Could not write the execution log."
        report = {"job_id": job_id, "status": status, "summary": summary[:600], "log_path": str(log_path)}
        encoded = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > 1000:
            report["summary"] = report["summary"][:max(0, len(report["summary"]) - (len(encoded) - 1000))]
            encoded = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
        job.update(
            status=status,
            summary=report["summary"],
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration_s=round(time.monotonic() - started, 3),
        )
        return encoded

    def _work(
        self,
        envelope: dict[str, Any],
        agent_id: str,
        job_id: str,
        record: Callable[..., None],
        budget: CallBudget | None = None,
        *,
        resume_state: dict[str, Any] | None = None,
        workspace_before: dict[str, Any] | None = None,
        planning: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        if self.max_rounds == 0 and (budget is None or budget.deadline is None):
            raise ValueError("unlimited execution requires an explicit wall deadline")
        profile = resume_state.get("tool_profile", "legacy") if resume_state is not None else "minimal"
        with AgentToolbox(
            self.workspace,
            timeout=self.toolbox.timeout,
            max_chars=self.toolbox.max_chars,
            allow_web=False,
            allow_run=self.toolbox.allow_run,
            persistent_commands=profile == "minimal",
            command_output_dir=self.out_dir / "executions" / f"{job_id}.commands",
        ) as worker:
            return self._work_with_tools(
                envelope, agent_id, job_id, record, budget, worker=worker, tool_profile=profile,
                resume_state=resume_state, workspace_before=workspace_before, planning=planning,
            )

    def _work_with_tools(
        self,
        envelope: dict[str, Any],
        agent_id: str,
        job_id: str,
        record: Callable[..., None],
        budget: CallBudget | None,
        *,
        worker: AgentToolbox,
        tool_profile: str,
        resume_state: dict[str, Any] | None,
        workspace_before: dict[str, Any] | None,
        planning: dict[str, Any] | None,
    ) -> tuple[str, str]:
        prompt_path = executor_prompt_path()
        source = prompt_path if prompt_path.is_file() else _BUNDLED_PROMPT
        prompt = source.read_text(encoding="utf-8").strip()
        record("configuration", prompt_path=str(source), prompt=prompt)
        initial = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(envelope, ensure_ascii=False)},
        ]
        write_allowed = envelope["allow_write"] and envelope["mode"] == "fix"
        tools = worker.schemas(minimal=tool_profile == "minimal", allow_write=write_allowed)
        if tool_profile == "legacy":
            tools = [tool for tool in tools if tool["function"]["name"] != "edit_file"]
        tools.append(report_schema(execution=True))
        if resume_state is not None and isinstance(resume_state.get("tool_schemas"), list):
            tools = resume_state["tool_schemas"]
        # Existing checkpoints and scripted callers can still use the native
        # workspace APIs. Fresh model requests receive the compact toolset.
        allowed_tools = {tool["function"]["name"] for tool in worker.schemas()} | {"submit_report"}
        write_paths = {self._path(path) for path in envelope["paths"]}
        write_directories = {path for path in write_paths if path.is_dir()}
        history: list[dict[str, Any]] = []
        completed: list[dict[str, Any]] = []
        observed: list[dict[str, Any]] = []
        outstanding_errors: set[str] = set()
        tool_selection_errors: set[str] = set()
        failed_commands: dict[str, int] = {}
        command_results: dict[int, bool] = {}
        last_command_ok: bool | None = None
        successful_tools = 0
        round_index = 0
        pending_response: dict[str, Any] | None = None
        next_tool_index = 0
        exchanges: list[tuple[dict[str, Any], dict[str, Any]]] = []
        if resume_state is not None:
            initial = resume_state["initial"]
            history = resume_state["history"]
            completed = resume_state["completed"]
            observed = resume_state["observed"]
            outstanding_errors = set(resume_state["outstanding_errors"])
            tool_selection_errors = set(resume_state["tool_selection_errors"]) | (outstanding_errors - allowed_tools)
            failed_commands = resume_state["failed_commands"]
            command_results = {int(k): bool(v) for k, v in resume_state["command_results"].items()}
            last_command_ok = resume_state["last_command_ok"]
            successful_tools = resume_state["successful_tools"]
            round_index = resume_state["next_round_index"]
            pending_response = resume_state.get("pending_response")
            next_tool_index = resume_state.get("next_tool_index", 0)
            exchanges = [tuple(pair) for pair in resume_state.get("exchanges", [])]
        planning_state = (resume_state or {}).get("resume_planning") or planning or {}
        if resume_state is None and str(planning_state.get("text", "")).strip():
            initial.append({"role": "user", "content": "本次主控的下一步执行建议：\n" + planning_state["text"]})

        def checkpoint(phase: str, inflight_tool: str | None = None) -> None:
            state = {
                "schema": "hcca.execution.checkpoint.v1", "phase": phase,
                "tool_profile": tool_profile, "tool_schemas": tools,
                "envelope": envelope, "initial": initial, "history": history,
                "next_round_index": round_index, "pending_response": pending_response,
                "next_tool_index": next_tool_index, "exchanges": exchanges,
                "inflight_tool": inflight_tool, "completed": completed, "observed": observed,
                "outstanding_errors": sorted(outstanding_errors), "failed_commands": failed_commands,
                "tool_selection_errors": sorted(tool_selection_errors),
                "command_results": command_results, "last_command_ok": last_command_ok,
                "successful_tools": successful_tools, "workspace_before": workspace_before,
                "budget": None if budget is None else {
                    "max_calls": budget.max_calls, "used": budget.used, "deadline": budget.deadline,
                },
                "legacy_restore_limits": (resume_state or {}).get("legacy_restore_limits", {}),
                "resume_lineage": (resume_state or {}).get("provenance"),
                "expired_scheduler_controls": (resume_state or {}).get("expired_scheduler_controls", []),
                "terminal_report_restoration": (resume_state or {}).get("terminal_report_restoration"),
                "resume_planning": planning_state,
            }
            record("checkpoint", state=state)
            destination = self.out_dir / "executions" / f"{job_id}.checkpoint.json"
            temporary = destination.with_suffix(".json.tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(destination)

        def cutoff(reason: str) -> tuple[str, str]:
            evidence = [
                f"{item['tool']} {json.dumps(item['args'], ensure_ascii=False)}: {item['fact']}"
                for item in reversed(observed)
            ]
            if failed_commands:
                evidence.append("Unresolved commands: " + ", ".join(list(failed_commands)[-3:]))
            if outstanding_errors:
                evidence.append("Unresolved tools: " + ", ".join(sorted(outstanding_errors)))
            summary = f"Execution budget ended: {reason}. " + (" | ".join(evidence) if evidence else "No tool completed.")
            record("budget_cutoff", reason=reason, completed=completed, evidence=observed, failed_commands=failed_commands, errors=sorted(outstanding_errors))
            return "blocked", summary

        def failed(summary: str) -> tuple[str, str]:
            if budget is not None:
                try:
                    budget.check()
                except BudgetExceeded as exc:
                    return cutoff(str(exc))
            return "failed", summary

        def report_failure(reason: str) -> tuple[str, str]:
            evidence = " | ".join(f"{item['tool']}: {item['fact']}" for item in reversed(observed))
            record("report_rejected", reason=reason, evidence=observed)
            return failed(reason + (" Evidence: " + evidence if evidence else ""))

        def report_feedback(reason: str) -> None:
            nonlocal pending_response, next_tool_index, exchanges, round_index
            record("report_rejected", reason=reason, evidence=observed)
            assistant = native_assistant(pending_response or {})
            history.append(assistant)
            feedback = "ERROR: " + reason
            if assistant.get("tool_calls"):
                for call in assistant["tool_calls"]:
                    history.append({"role": "tool", "tool_call_id": call["id"], "content": feedback})
            else:
                history.append({"role": "user", "content": feedback})
            record("report_feedback", result=feedback, continued=True)
            pending_response = None
            next_tool_index = 0
            exchanges = []
            round_index += 1
            checkpoint("ready")

        while self.max_rounds == 0 or round_index < self.max_rounds:
            if budget is not None:
                try:
                    budget.check()
                except BudgetExceeded as exc:
                    return cutoff(str(exc))
            final_round = (self.max_rounds > 0 and round_index == self.max_rounds - 1) or (budget is not None and budget.remaining <= 1)
            tag = f"executor:{job_id}:round{round_index + 1}"
            if pending_response is None:
                if self.take_guidance is not None:
                    history.extend(self.take_guidance(agent_id))
                if final_round:
                    history.append({"role": "user", "content": REPORT_FINAL_INSTRUCTION + " Include status and summary."})
                messages, context_metadata = bounded_tool_messages(initial, history, tools, self.context_budget_tokens)
                checkpoint("ready")
                record("request", tag=tag, messages=messages, **context_metadata)
                try:
                    options = {"budget": budget, "max_output_tokens": None} if budget is not None else {}
                    if budget is not None and final_round:
                        options["tool_choice"] = REPORT_TOOL_CHOICE
                    response = self.complete(agent_id=agent_id, messages=messages, tag=tag, tools=tools, **options)
                except BudgetExceeded as exc:
                    return cutoff(str(exc))
                except Exception as exc:
                    record("model_error", error=f"{type(exc).__name__}: {exc}")
                    return failed("Executor model call failed. See the execution log.")
                if response is None:
                    record("model_error", error="Model returned no response")
                    return failed("Executor model call failed. See the execution log.")
                pending_response = {
                    "text": str(getattr(response, "text", "") or ""),
                    "reasoning_content": getattr(response, "reasoning_content", None),
                    "tool_calls": list(getattr(response, "tool_calls", None) or []),
                }
                next_tool_index = 0
                exchanges = []
                record("response", tag=tag, text=pending_response["text"].strip(),
                       raw_text=pending_response["text"], reasoning_content=pending_response["reasoning_content"],
                       tool_calls=pending_response["tool_calls"])
                checkpoint("response")
            response = SimpleNamespace(**pending_response)
            response_text = str(getattr(response, "text", "") or "")
            text = response_text.strip()
            calls = list(getattr(response, "tool_calls", None) or [])
            try:
                submission = submitted_report(calls, execution=True)
            except ValueError as exc:
                if self.max_rounds == 0:
                    report_feedback(f"Executor rejected submit_report: {exc}; no tools in this batch were executed.")
                    continue
                return report_failure(f"Executor rejected submit_report: {exc}")
            if submission is not None:
                record("report_submission", report=submission)
                text = json.dumps(submission, ensure_ascii=False)
                calls = []
            reject_final = report_failure if submission is not None else failed
            if not calls:
                try:
                    fenced = re.findall(r"```json\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
                    candidate = fenced[-1] if fenced else text
                    start = candidate.find("{")
                    if start < 0:
                        raise ValueError("missing final JSON object")
                    final, _ = json.JSONDecoder().raw_decode(candidate[start:])
                    status, summary = final["status"], final["summary"]
                    if status not in ("completed", "failed", "blocked") or not isinstance(summary, str) or not summary.strip():
                        raise ValueError("unsupported status or empty summary")
                    resolutions = final.get("resolved_by") or {}
                    if not isinstance(resolutions, dict):
                        raise ValueError("resolved_by must map failed IDs to later successful IDs")
                    for failed_key, successful_values in resolutions.items():
                        failed_id = int(failed_key)
                        if str(failed_id) != failed_key or failed_id not in command_results:
                            raise ValueError("resolved_by must reference an executed command")
                        successful_ids = successful_values if isinstance(successful_values, list) else [successful_values]
                        if not successful_ids:
                            raise ValueError("resolved_by requires at least one later successful command")
                        for successful_id in successful_ids:
                            if isinstance(successful_id, str) and successful_id.isdecimal():
                                successful_id = int(successful_id)
                            if type(successful_id) is not int or command_results.get(successful_id) is not True or successful_id <= failed_id:
                                raise ValueError(f"resolved_by[{failed_key!r}] target {successful_id!r} must be a later successful command ID; use an integer or list of integers")
                except (ValueError, TypeError, KeyError) as exc:
                    if self.max_rounds == 0:
                        report_feedback(f"Executor returned an invalid final report: {exc}")
                        continue
                    return reject_final("Executor returned an invalid final report. See the execution log.")
                if status == "completed" and (not successful_tools or outstanding_errors):
                    if self.max_rounds == 0:
                        report_feedback("Executor reported completion with unresolved execution failures or no successful tool execution. "
                                        f"tool errors: {sorted(outstanding_errors)}.")
                        continue
                    return reject_final("Executor reported completion with unresolved execution failures or no successful tool execution.")
                if status == "completed" and (
                    last_command_ok is False
                    or (envelope["mode"] in ("command", "test") and last_command_ok is not True)
                ):
                    if self.max_rounds == 0:
                        report_feedback("Executor did not complete a successful command.")
                        continue
                    return reject_final("Executor did not complete a successful command.")
                return status, " ".join(summary.split())
            if final_round:
                if budget is not None:
                    return cutoff("final report returned other tool calls")
                return failed(f"Executor reached its {self.max_rounds}-round limit without a final result.")
            for index, call in enumerate(calls):
                if index < next_tool_index:
                    continue
                if budget is not None:
                    try:
                        budget.check()
                    except BudgetExceeded as exc:
                        return cutoff(str(exc))
                name = str(call.get("name") or "")
                call_id = str(call.get("id") or f"call_{round_index}_{index}")
                args = call.get("arguments") or {}
                tool_started = time.monotonic()
                previous_logs = len(worker.log)
                command_id: int | None = None
                tool_selection_error = name not in allowed_tools
                try:
                    if not isinstance(args, Mapping):
                        raise ValueError("tool arguments must be an object")
                    if name not in allowed_tools:
                        raise ValueError("tool is unavailable for this job")
                    if name in ("write_file", "edit_file") and not write_allowed:
                        raise ValueError(f"{name} is disabled for this job")
                    if name in ("write_file", "edit_file"):
                        try:
                            target = worker._resolve(str(args.get("path", "")))
                        except ToolError as exc:
                            raise ValueError("path escapes the workspace") from exc
                        if target not in write_paths and not any(target.is_relative_to(path) for path in write_directories):
                            raise ValueError(f"{name} is restricted to the named fix scope")
                    if name == "run_command":
                        remaining_time = budget.remaining_time if budget is not None else None
                        if remaining_time is not None:
                            if remaining_time < 1:
                                return cutoff("less than one second remains for a command")
                            args = dict(args)
                            try:
                                requested_timeout = int(args.get("timeout_seconds", worker.timeout))
                            except (TypeError, ValueError):
                                requested_timeout = int(worker.timeout)
                            args["timeout_seconds"] = max(1, min(requested_timeout, int(remaining_time)))
                        command_id = len(command_results) + 1
                    checkpoint("tools", inflight_tool=call_id)
                    record("tool_started", tag=tag, tool=name, tool_call_id=call_id)
                    result = worker.call(name, args)
                except Exception as exc:
                    result = f"ERROR: {type(exc).__name__}: {exc}"
                tool_selection_error = command_id is None and (tool_selection_error or result in TOOL_SELECTION_ERRORS)
                ok = not result.startswith("ERROR:")
                if len(worker.log) > previous_logs:
                    ok = ok and worker.log[-1]["ok"]
                if name == "run_command" and command_id is not None:
                    command = str(args.get("command") or "").strip() if isinstance(args, Mapping) else ""
                    ok = ok and command_exit_code(result, command) == 0
                    command_results[command_id] = bool(ok)
                    last_command_ok = bool(ok)
                    failed_commands.pop(command, None)
                    if not ok:
                        failed_commands[command] = command_id
                update_tool_errors(outstanding_errors, tool_selection_errors, name, ok,
                                   command_id, tool_selection_error)
                if ok:
                    successful_tools += 1
                audit = {
                    "tool": name,
                    "ok": bool(ok),
                    "chars": len(result),
                    "duration_ms": int((time.monotonic() - tool_started) * 1000),
                    "args": {str(key): str(value)[:120] for key, value in args.items()} if isinstance(args, Mapping) else {},
                    "agent_id": agent_id,
                    "tag": tag,
                    "job_id": job_id,
                    "tool_call_id": call_id,
                }
                if command_id is not None:
                    audit["command_id"] = command_id
                if tool_selection_error:
                    audit["tool_selection_error"] = True
                self.tool_log.append(audit)
                record("tool", **audit, arguments=args, result=result)
                content = result
                fact = content if len(content) <= 180 else content[:60] + " … " + content[-117:]
                action = {"tool": name, "args": audit["args"], "fact": fact}
                if command_id is not None:
                    action["command_id"] = command_id
                observed.append(action)
                observed = observed[-3:]
                if ok:
                    completed.append(action)
                    completed = completed[-6:]
                native_call = {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False)},
                }
                if command_id is not None:
                    content = f"command_id={command_id}\n" + content
                exchanges.append((native_call, {"role": "tool", "tool_call_id": call_id, "content": content}))
                next_tool_index = index + 1
                checkpoint("tools")
            assistant = {"role": "assistant", "content": response_text, "tool_calls": [item[0] for item in exchanges]}
            reasoning = getattr(response, "reasoning_content", None)
            if isinstance(reasoning, str):
                assistant["reasoning_content"] = reasoning
            history.extend([assistant, *(item[1] for item in exchanges)])
            pending_response = None
            exchanges = []
            next_tool_index = 0
            round_index += 1
            checkpoint("ready")
        return "failed", f"Executor reached its {self.max_rounds}-round limit before a valid final result."
