"""Generic HCCA multi-agent research run (used by hcca_delegate).

This runner accepts an objective from the command line or chat.
There is **no fixed team shape**: the Root model decides the mode (solo/team),
how many sub-Agents to create, and writes each sub-Agent's name and full prompt
itself. The runtime loop is a real research loop, not a one-shot split:

    Root plan   -> Root decides agents: [{name, prompt}]  (or solo)
    Analysts    -> research with tools, then a self-check pass that fills gaps
    Root review -> reads every answer, lists remaining gaps; may spawn another
                   wave of agents with new prompts, or declare the work done
    Verifier    -> only when Root asked for it (prompt written by Root)
    Report      -> Root writes the report with tools, then a final review pass

Every model call writes status.json (same hcca.status.v1 schema as the bench) so
the DSH HCCA tab can monitor the tree; a heartbeat keeps ``updated_at`` fresh,
publishes in-flight calls with live stream counters, and flags a stalled call
(keep-alives but no model output) so it cannot masquerade as "still thinking".
Each activation shares a request budget with its delegated workers. The pool
allows 8 parent/control requests plus one full reader job, and Root additionally
gets another reader job and one full executor job, within the applicable
wall-clock deadline.
Model requests leave output length unrestricted.

Usage:
    python -m hcca.runner --goal "..." --workspace /path/to/project --out runs/example
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PROJECT = Path(__file__).resolve().parents[1]

from hcca.agent_tools import AgentToolbox  # noqa: E402
from hcca.collab_runtime import CollabRuntime  # noqa: E402
from hcca.collaboration import CollaborationStore  # noqa: E402
from hcca.execution import ExecutionService, bounded_tool_messages  # noqa: E402
from hcca.execution_resume import load_execution_resume  # noqa: E402
from hcca.guidance import GuidanceQueue  # noqa: E402
from hcca.reading import ReaderService  # noqa: E402
from hcca.reporting import (  # noqa: E402
    REPORT_FINAL_INSTRUCTION,
    REPORT_TOOL_CHOICE,
    report_schema,
    submitted_report,
)
from hcca.llm import _cache_usage, load_llm_from_env  # noqa: E402
from hcca.request_budget import BudgetExceeded, CallBudget  # noqa: E402
from hcca.scheduler import Scheduler, SchedulerConfig  # noqa: E402
from hcca.supervisor import Supervisor, SupervisorConfig  # noqa: E402
from hcca.types import GoalSpec  # noqa: E402

ASPECTS = {
    "facts": "事实与时间线：关键事实、数字、时间顺序、可核实来源；区分事实/推断/不确定",
    "mechanism": "机制与因果：为什么会发生、传导链、必要条件、反例与边界",
    "context": "背景与比较：更广的经济/政策/国际环境、同类案例对比、长期影响",
}

_AGENT_SYSTEM = (
    "你是 HCCA Agent，按当前角色和任务工作。文件阅读与检索按完整问题交给 delegate_reading，"
    "依据带路径行号的短报告推理。"
    "命令、测试、修复、脚本计算和文件写入按完整子任务交给 delegate_execution，"
    "由执行者自主纠错并复验。"
    "只传目标、必要文件与验收条件。依据执行状态和摘要验收，"
    "需要执行证据时让阅读者按执行任务 ID 查阅日志。保留未完成项。"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _known_usage(meta: Mapping[str, Any], usage: Mapping[str, Any]) -> tuple[dict[str, int], bool]:
    """Return provider-reported counters without billing token estimates."""
    if (meta.get("stream_diagnostics") or {}).get("usage_parse_error"):
        return {}, True
    if usage:
        try:
            counters = _cache_usage(usage)
            if "completion_tokens" in usage:
                counters["completion_tokens"] = int(usage["completion_tokens"] or 0)
        except (TypeError, ValueError, OverflowError):
            return {}, True
        missing = not all(key in usage for key in ("prompt_tokens", "completion_tokens"))
    elif not meta.get("usage_missing") and not meta.get("usage_estimated"):
        counters = {
            key: int(meta[key] or 0)
            for key in (
                "prompt_tokens", "completion_tokens", "cache_hit_tokens", "cache_miss_tokens",
                "cache_created_tokens", "reasoning_tokens",
            )
            if key in meta
        }
        missing = not all(key in counters for key in ("prompt_tokens", "completion_tokens"))
    else:
        counters = {}
        missing = True
    return counters, missing


def _extract_json(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}


def _tool_preview(entry: Mapping[str, Any]) -> dict[str, Any]:
    """A status.json-sized copy of a tool log entry (no full file contents)."""
    preview = dict(entry)
    args = entry.get("args")
    if isinstance(args, Mapping):
        trimmed: dict[str, str] = {}
        for key, value in args.items():
            raw = str(value)
            trimmed[str(key)] = raw[:240] + ("…" if len(raw) > 240 else "")
        preview["args"] = trimmed
    else:
        raw = str(args or "")
        preview["args"] = raw[:240] + ("…" if len(raw) > 240 else "")
    return preview


class GenericRun:
    def __init__(
        self,
        *,
        out_dir: Path,
        llm: Any,
        max_output: int | None = None,
        goal: str,
        workspace: Path | str | None = None,
        toolbox: AgentToolbox | None = None,
        max_tool_rounds: int = 0,
        retry_attempts: int = 5,
        retry_delay: float = 2.0,
        max_review_rounds: int = 4,
        max_finish_rounds: int = 2,
        force_mode: str = "",
        readonly_analysts: bool = False,
        task_kind: str = "research",
        no_review_waves: bool = False,
        wall_budget_s: int = 0,
        analyst_reserve_frac: float = 0.0,
        unlimited_executor_rounds: bool = False,
        resume_execution: str | Path | None = None,
        thinking_reasoning_effort: str | None = None,
        executor_reasoning_effort: str | None = None,
        reader_reasoning_effort: str | None = None,
        executor_context_budget_tokens: int | None = None,
        resume_next_step_plan: bool = False,
        resume_planning_record: str | Path | None = None,
        plan_before_execution: bool = False,
        enable_guidance: bool = False,
    ) -> None:
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.guidance = GuidanceQueue(self.out_dir)
        self.enable_guidance = bool(enable_guidance)
        self.llm = llm
        self.max_output = max_output
        self.goal = goal
        self.workspace = Path(workspace).resolve() if workspace is not None else PROJECT
        self.toolbox = toolbox
        self.tool_log: list[dict[str, Any]] = []
        self.max_tool_rounds = max(0, int(max_tool_rounds))
        self.unlimited_executor_rounds = bool(unlimited_executor_rounds)
        self.resume_execution = Path(resume_execution).resolve() if resume_execution is not None else None
        self.resume_next_step_plan = bool(resume_next_step_plan)
        self.plan_before_execution = bool(plan_before_execution)
        self.resume_planning_record = Path(resume_planning_record).resolve() if resume_planning_record is not None else None
        if self.plan_before_execution and (
            not self.unlimited_executor_rounds or self.resume_execution is not None
            or self.resume_next_step_plan or self.resume_planning_record is not None
        ):
            raise ValueError("fresh planning requires explicit unlimited execution without continuation inputs")
        if self.resume_planning_record is not None and not self.resume_next_step_plan:
            raise ValueError("a paid planning-attempt record requires continuation planning")
        self._resume_planning: dict[str, Any] = {}
        if self.resume_next_step_plan and self.resume_execution is None:
            raise ValueError("resume next-step planning requires an execution continuation")
        if self.resume_execution is not None and not self.unlimited_executor_rounds:
            raise ValueError("execution continuation requires explicit unlimited_executor_rounds")
        self.role_reasoning_effort = {
            "thinking": thinking_reasoning_effort or ("high" if self.unlimited_executor_rounds else None),
            "executor": executor_reasoning_effort or ("low" if self.unlimited_executor_rounds else None),
            "reader": reader_reasoning_effort or ("low" if self.unlimited_executor_rounds else None),
        }
        if any(value not in (None, "low", "high") for value in self.role_reasoning_effort.values()):
            raise ValueError("role reasoning effort must be low, high, or None")
        self.retry_attempts = max(0, int(retry_attempts))
        self.retry_delay = max(0.0, float(retry_delay))
        #: 0 = Root keeps reviewing/adding waves until it declares the work done.
        self.max_review_rounds = max(0, int(max_review_rounds))
        #: Extra "finish the job" rounds an analyst gets after the completion
        #: audit says its delivery still has unfinished work (0 = old behaviour:
        #: submit whatever the agent says, even "I did not finish").
        self.max_finish_rounds = max(0, int(max_finish_rounds))
        #: Experiment control: force the Root planner into solo/team instead of
        #: letting it choose (empty = model decides).  Used by the SWE-bench
        #: architecture A/B, where the *mode* is the treatment variable.
        self.force_mode = str(force_mode or "").strip().lower()
        #: Read-only agents may request command/test jobs. Only Root may
        #: request workspace changes through the executor.
        self.readonly_analysts = bool(readonly_analysts)
        #: "research" (default) or "code": code swaps the Root/analyst/verifier
        #: system prompts from research to delegated implementation/validation.
        self.task_kind = str(task_kind or "research").strip().lower()
        if self.unlimited_executor_rounds and (
            self.task_kind != "code" or self.force_mode == "team" or self.toolbox is None
        ):
            raise ValueError("unlimited execution supports only the direct Git code route with tools")
        #: Experiment control: when true the review stage always ends after the
        #: first wave (no extra "continue" waves), so a fixed wall budget goes
        #: to the implement/verify stages instead of an unbounded review loop.
        self.no_review_waves = bool(no_review_waves)
        #: Hard wall budget for this stage (seconds; 0 = unknown/unbounded).
        #: Together with ``analyst_reserve_frac`` it protects the Root's own
        #: implementation window from being eaten by analyst/audit rounds.
        self.wall_budget_s = max(0, int(wall_budget_s))
        if self.unlimited_executor_rounds and self.wall_budget_s <= 0:
            raise ValueError("unlimited executor rounds require a positive wall budget")
        #: Fraction of the wall budget that MUST stay free for the Root's
        #: report/implementation phase.  0 = old behaviour (no reservation);
        #: e.g. 0.45 means "analysts + review must be over by 55% of the wall".
        self.analyst_reserve_frac = min(0.9, max(0.0, float(analyst_reserve_frac)))
        self._forced_report = False
        self._analyst_deadline_hit = False
        self._workspace_diff: dict[str, Any] | None = None
        self._audits: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.started = time.time()
        self.supervisor: Supervisor | None = None
        self.runtime: Any = None
        self._phases: dict[str, int] = {}
        self._report = ""
        self._llm_sem: Any = None
        limit = int(os.environ.get("HCCA_LLM_CONCURRENCY", "0") or 0)
        if limit > 0:
            self._llm_sem = threading.Semaphore(limit)
        self._status_lock = threading.RLock()
        self._answers: dict[str, str] = {}
        self._answer_order: list[str] = []
        self._notes: dict[str, str] = {}
        self._plan: dict[str, Any] | None = None
        self._submitted: dict[str, str] = {}
        self._root_stage: dict[str, str] = {}
        self._review_round = 0
        self._review_log: list[dict[str, Any]] = []
        self._verifier_prompt = ""
        self._pending: dict[str, dict[str, Any]] = {}
        self._pending_tools: dict[str, dict[str, Any]] = {}
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self.failed_reason = ""
        #: Whether the model client understands ``on_stream`` (our
        #: OpenAICompatLLM does; scripted/fake clients in tests may not).
        try:
            import inspect

            self._llm_accepts_on_stream = "on_stream" in inspect.signature(
                self.llm.complete
            ).parameters
            parameters = inspect.signature(self.llm.complete).parameters
            self._llm_accepts_call_timeout = "call_timeout" in parameters or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
            )
            self._llm_accepts_tool_choice = "tool_choice" in parameters or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
            )
        except (TypeError, ValueError):
            self._llm_accepts_on_stream = False
            self._llm_accepts_call_timeout = False
            self._llm_accepts_tool_choice = False
        self.executor = (
            ExecutionService(
                workspace=self.workspace,
                out_dir=self.out_dir,
                toolbox=self.toolbox,
                complete=self._complete,
                take_guidance=self._take_guidance,
                max_rounds=0 if self.unlimited_executor_rounds else self.max_tool_rounds or 16,
                context_budget_tokens=(executor_context_budget_tokens if executor_context_budget_tokens is not None
                                       else 262_144 if self.unlimited_executor_rounds else 65_536),
            )
            if self.toolbox is not None else None
        )
        self.reader = (
            ReaderService(
                workspace=self.workspace,
                out_dir=self.out_dir,
                toolbox=self.toolbox,
                complete=self._complete,
                read_execution_log=self.executor.read_log,
                take_guidance=self._take_guidance,
            )
            if self.toolbox is not None else None
        )

    # -- model call ----------------------------------------------------
    def _tools_enabled(self) -> bool:
        return self.toolbox is not None

    def _take_guidance(self, agent_id: str) -> list[dict[str, Any]]:
        return self.guidance.take(agent_id)

    @staticmethod
    def _guidance_schema() -> dict[str, Any]:
        return AgentToolbox._fn("send_guidance", "Send an updated subtask to one directly delegated active worker.", {
            "agent_id": {"type": "string"}, "text": {"type": "string"}, "message_id": {"type": "string"},
        }, ["agent_id", "text"])

    def _send_guidance(self, arguments: Mapping[str, Any], requester_id: str) -> str:
        target = str(arguments.get("agent_id") or "")
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            return "ERROR: text is required"
        jobs = [job for service in (self.executor, self.reader) if service is not None for job in service.jobs]
        direct = any(job["agent_id"] == target and job["requester_id"] == requester_id
                     and str(job["status"]).upper() in {"RUNNING", "QUEUED"} for job in jobs)
        handles = self.supervisor.handles if self.supervisor is not None else {}
        handle = handles.get(target)
        direct = direct or (handle is not None and getattr(handle, "parent_id", None) == requester_id
                            and str(getattr(handle, "status", "")).upper() in {"CREATED", "READY", "RUNNING", "WAITING", "QUEUED"})
        if not direct:
            return "ERROR: target must be your directly delegated active worker"
        try:
            result = self.guidance.publish(text, [target], str(arguments.get("message_id") or ""))
        except ValueError as error:
            return "ERROR: " + str(error)
        return json.dumps(result, ensure_ascii=False)

    # -- wall-budget guards (implementation-window protection) ----------
    def _analyst_deadline(self) -> float | None:
        """Absolute time by which the analyst/review phases must be over."""
        if self.wall_budget_s <= 0 or self.analyst_reserve_frac <= 0:
            return None
        return self.started + (1.0 - self.analyst_reserve_frac) * self.wall_budget_s

    def _past_analyst_deadline(self) -> bool:
        deadline = self._analyst_deadline()
        if deadline is None:
            return False
        if time.time() >= deadline:
            self._analyst_deadline_hit = True
            return True
        return False

    def _new_budget(self, agent_id: str, tag: str) -> CallBudget:
        analysis = agent_id != "root" or tag.startswith(("root-plan", "root-review"))
        deadline = self.started + self.wall_budget_s if self.wall_budget_s > 0 else None
        if analysis and self._analyst_deadline() is not None:
            deadline = self._analyst_deadline()
        if self.unlimited_executor_rounds:
            return CallBudget(None, deadline)
        # Worker requests use the same pool, so the parent's tool-round limit
        # must not truncate a complete delegated reading/execution chain.
        reader_calls = self.reader.max_rounds if self.reader is not None else 0
        max_calls = 8 + reader_calls
        if agent_id == "root":
            max_calls += reader_calls
            max_calls += self.executor.max_rounds if self.executor is not None else 8
        return CallBudget(max_calls, deadline)

    def capture_workspace_diff(self) -> dict[str, Any] | None:
        """Record the scoped Git patch and untracked workspace contents.

        A run that "completed" with an empty diff is the exact failure mode seen
        on SWE-bench (the Root experimented in a scratch copy and never wrote to
        the real checkout), so the status snapshot carries the evidence.
        """
        try:
            root = subprocess.run(
                ["git", "-C", str(self.workspace), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            if root.returncode != 0:
                return None
            pathspec = ["."]
            run_dir = self.out_dir.resolve()
            artifact_names = ("executions", "readings", "status.json", "report.md", "delegate.log")
            if self.resume_next_step_plan or self.plan_before_execution:
                artifact_names += ("resume_plan.json",)
            artifacts = [run_dir] if run_dir != self.workspace else [
                run_dir / name for name in artifact_names
            ]
            for artifact in artifacts:
                if artifact.is_relative_to(self.workspace):
                    pathspec.append(":(exclude,literal)" + artifact.relative_to(self.workspace).as_posix())
            diff = subprocess.run(
                ["git", "-C", str(self.workspace), "diff", "--binary", "HEAD", "--", *pathspec],
                capture_output=True, timeout=120,
            )
            status = subprocess.run(
                ["git", "-C", str(self.workspace), "status", "--porcelain", "-uall", "--", *pathspec],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
            )
            untracked = subprocess.run(
                ["git", "-C", str(self.workspace), "ls-files", "--others", "--exclude-standard", "-z", "--", *pathspec],
                capture_output=True, timeout=120,
            )
            if diff.returncode != 0 or status.returncode != 0 or untracked.returncode != 0:
                return None
            patch = diff.stdout or b""
            change_hash = hashlib.sha256()
            change_hash.update(len(patch).to_bytes(8, "big"))
            change_hash.update(patch)
            untracked_paths = sorted(path for path in untracked.stdout.split(b"\0") if path)
            for relative in untracked_paths:
                path = self.workspace / os.fsdecode(relative)
                if path.is_symlink():
                    kind = b"L"
                    content_hash = hashlib.sha256(os.fsencode(os.readlink(path))).digest()
                else:
                    kind = b"F"
                    with path.open("rb") as source:
                        content_hash = hashlib.file_digest(source, "sha256").digest()
                change_hash.update(len(relative).to_bytes(8, "big"))
                change_hash.update(relative)
                change_hash.update(kind)
                change_hash.update(content_hash)
            payload = {
                "diff_bytes": len(patch),
                "changed_files": len(
                    [line for line in (status.stdout or "").splitlines() if line.strip()]
                ),
                "empty": not patch.strip() and not untracked_paths,
                "diff_sha256": hashlib.sha256(patch).hexdigest(),
                "workspace_change_sha": change_hash.hexdigest(),
            }
            self._workspace_diff = payload
            return payload
        except Exception:  # noqa: BLE001 - status must never fail the run
            return None

    def _complete(
        self,
        *,
        agent_id: str,
        messages: list[dict[str, Any]],
        tag: str,
        tools: list[dict[str, Any]] | None,
        budget: CallBudget | None = None,
        max_output_tokens: int | None = None,
        call_timeout: float | None = None,
        tool_choice: str | None = None,
    ) -> Any:
        """One model call, recorded in ``self.calls``; returns the raw response.

        The call is published as a *pending* entry while it is in flight so the
        UI shows what the run is waiting on (instead of looking frozen during a
        long gateway stall) and the heartbeat keeps ``updated_at`` fresh.
        """
        budget = budget or self._new_budget(agent_id, tag)
        budget.check()
        context_budget = self.executor.context_budget_tokens if self.executor is not None and agent_id.startswith("execution_") else 65_536
        _, context_metadata = bounded_tool_messages(messages, [], tools, context_budget)
        context_metadata.pop("context_exchanges_dropped")
        # Accept legacy cap arguments while leaving the request unrestricted.
        output_limit = None
        role = "executor" if agent_id.startswith("execution_") else "reader" if agent_id.startswith("reading_") else "thinking"
        reasoning_effort = self.role_reasoning_effort[role]
        actual_tool_choice = tool_choice if self._llm_accepts_tool_choice else None
        timeout_limit = (
            call_timeout if call_timeout is not None and call_timeout > 0
            else float(getattr(self.llm, "call_timeout", 0.0) or 0.0)
        )
        if timeout_limit <= 0:
            timeout_limit = None
        t0 = time.time()
        started_at = datetime.fromtimestamp(t0, timezone.utc).isoformat(timespec="milliseconds")
        pending_key = f"{agent_id}|{tag}|{time.time_ns()}"
        self._pending[pending_key] = {
            "agent_id": agent_id,
            "tag": tag,
            "started_at": started_at,
            "t0": t0,
            "elapsed_s": 0.0,
            "attempt": 1,
            "last_progress_t": t0,
            "stream": {},
            **context_metadata,
        }
        self._write_status()

        def _on_stream(info: dict[str, Any]) -> None:
            """Publish live token counts so a slow call is visibly *alive*.

            Keep-alive bytes from the gateway are counted separately: a call
            with many keep-alives and zero content/reasoning chunks is a
            stalled call, not a thinking model.
            """
            entry = self._pending.get(pending_key)
            if entry is None:
                return
            now = time.time()
            # The parser knows when meaningful output last arrived. Empty SSE
            # deltas and gateway keep-alives must preserve that idle interval.
            try:
                idle_s = max(0.0, float(info.get("idle_s") or 0.0))
            except (TypeError, ValueError):
                idle_s = 0.0
            entry["last_progress_t"] = now - idle_s
            entry["idle_s"] = round(idle_s, 1)
            entry["stream"] = {
                "elapsed_s": info.get("elapsed_s"),
                "idle_s": info.get("idle_s"),
                "content_chars": int(info.get("content_chars") or 0),
                "reasoning_chars": int(info.get("reasoning_chars") or 0),
                "chunks": int(info.get("chunks") or 0),
                "keepalives": int(info.get("keepalives") or 0),
                **{
                    key: info[key]
                    for key in (
                        "reasoning_counter_kind", "nonstring_reasoning_fragments",
                        "stream_diagnostics", "usage", "usage_missing", "usage_estimated",
                        "usage_partial", "usage_complete", "cache_usage_complete", "cache_usage_source",
                    )
                    if key in info
                },
            }

        transport_attempts: list[dict[str, Any]] = []

        def _invoke() -> Any:
            """Record every transport invocation, including an empty retry."""
            request_start = time.time()
            entry = self._pending.get(pending_key)
            if entry is not None:
                entry.update({
                    "attempt": attempts,
                    "transport_attempt": len(transport_attempts) + 1,
                    "last_progress_t": request_start,
                    "idle_s": 0.0,
                    "stalled": False,
                    "stream": {},
                })
            self._write_status()
            timeout = timeout_limit
            remaining = budget.remaining_time
            if remaining is not None:
                if remaining <= 0:
                    raise BudgetExceeded("deadline")
                timeout = min(timeout, remaining) if timeout is not None else remaining
            budget.consume()
            request_start = time.time()
            attempt_meta: dict[str, Any] = {}
            usage: dict[str, Any] = {}
            attempt_error = ""
            try:
                result = self._llm_complete(
                    messages, tools, on_stream=_on_stream,
                    max_output_tokens=output_limit, call_timeout=timeout,
                    tool_choice=tool_choice,
                    reasoning_effort=reasoning_effort,
                )
                attempt_meta = dict(getattr(result, "meta", None) or {})
                usage = dict(attempt_meta.get("usage") or {})
                return result
            except Exception as exc:  # noqa: BLE001
                attempt_error = f"{type(exc).__name__}: {exc}"
                attempt_meta = dict(getattr(exc, "meta", None) or {})
                usage = dict(getattr(exc, "usage", None) or attempt_meta.get("usage") or {})
                raise
            finally:
                request_finish = time.time()
                stream = dict(entry.get("stream") or {}) if entry is not None else {}
                # A legacy client can report usage only through its callback.
                usage = usage or dict(stream.get("usage") or {})
                counters, usage_missing = _known_usage(attempt_meta, usage)
                usage_complete = bool(attempt_meta.get(
                    "usage_complete", not usage_missing and not attempt_error and not attempt_meta.get("usage_partial"),
                ))
                usage_partial = bool(attempt_meta.get("usage_partial", bool(usage or counters) and not usage_complete))
                cache_details = usage.get("prompt_tokens_details") or {}
                if not isinstance(cache_details, Mapping):
                    cache_details = {}
                cache_source = "missing"
                if "prompt_cache_hit_tokens" in usage or "prompt_cache_miss_tokens" in usage:
                    cache_source = "provider_top_level"
                elif "cached_tokens" in cache_details:
                    cache_source = "provider_details"
                cache_hit_known = "prompt_cache_hit_tokens" in usage or "cached_tokens" in cache_details
                cache_split_known = cache_hit_known and ("prompt_cache_miss_tokens" in usage or "prompt_tokens" in usage)
                cache_complete = bool(attempt_meta.get("cache_usage_complete", usage_complete and cache_split_known))
                transport_attempts.append({
                    "attempt": attempts,
                    "started_at": datetime.fromtimestamp(request_start, timezone.utc).isoformat(timespec="milliseconds"),
                    "finished_at": datetime.fromtimestamp(request_finish, timezone.utc).isoformat(timespec="milliseconds"),
                    "latency_s": round(request_finish - request_start, 2),
                    "max_output_tokens": output_limit,
                    "tool_choice": actual_tool_choice,
                    "requested_call_timeout": timeout,
                    "budget_max_calls": budget.max_calls,
                    "budget_remaining_after": None if budget.max_calls is None else budget.remaining,
                    "role_reasoning_effort": reasoning_effort,
                    "error": attempt_error,
                    "usage": usage,
                    "usage_missing": usage_missing,
                    "usage_partial": usage_partial,
                    "usage_complete": usage_complete,
                    "cache_usage_complete": cache_complete,
                    "cache_usage_source": str(attempt_meta.get("cache_usage_source") or cache_source),
                    **counters,
                    "meta": attempt_meta,
                    "stream": stream,
                    "stream_diagnostics": attempt_meta.get("stream_diagnostics") or stream.get("stream_diagnostics") or {},
                })

        def _with_model_slot() -> Any:
            budget.check()
            if self._llm_sem is None:
                return _invoke()
            remaining = budget.remaining_time
            acquired = self._llm_sem.acquire() if remaining is None else self._llm_sem.acquire(timeout=remaining)
            if not acquired:
                raise BudgetExceeded("deadline")
            try:
                return _invoke()
            finally:
                self._llm_sem.release()

        response: Any = None
        text = ""
        meta: dict[str, Any] = {}
        error = ""
        attempts = 0
        budget_error: BudgetExceeded | None = None
        try:
            while attempts <= self.retry_attempts:
                attempts += 1
                response = None
                try:
                    response = _with_model_slot()
                    text = (response.text or "").strip()
                    meta = dict(response.meta or {})
                    error = ""
                    break
                except BudgetExceeded as exc:
                    budget_error = exc
                    error = f"{type(exc).__name__}: {exc}"
                    break
                except Exception as exc:  # noqa: BLE001
                    error = f"{type(exc).__name__}: {exc}"
                    if attempts > self.retry_attempts:
                        break
                    delay = min(30.0, self.retry_delay * (2 ** (attempts - 1)))
                    if budget.remaining_time is not None:
                        delay = min(delay, budget.remaining_time)
                    if delay > 0:
                        time.sleep(delay)
        finally:
            self._pending.pop(pending_key, None)
        finished = time.time()
        finished_at = datetime.fromtimestamp(finished, timezone.utc).isoformat(timespec="milliseconds")
        usage_missing_attempts = sum(bool(attempt["usage_missing"]) for attempt in transport_attempts)
        usage_partial_attempts = sum(bool(attempt["usage_partial"]) for attempt in transport_attempts)
        usage_incomplete_attempts = sum(not attempt["usage_complete"] for attempt in transport_attempts)
        cache_incomplete_attempts = sum(not attempt["cache_usage_complete"] for attempt in transport_attempts)
        cache_sources = {attempt["cache_usage_source"] for attempt in transport_attempts} or {"missing"}
        usage_totals = {
            key: sum(int(attempt.get(key) or 0) for attempt in transport_attempts)
            for key in (
                "prompt_tokens", "completion_tokens", "cache_hit_tokens", "cache_miss_tokens",
                "cache_created_tokens", "reasoning_tokens",
            )
        }
        self.calls.append(
            {
                "agent_id": agent_id,
                "tag": tag,
                "started_at": started_at,
                "finished_at": finished_at,
                "latency_s": round(finished - t0, 2),
                **usage_totals,
                "usage_missing": bool(usage_missing_attempts),
                "usage_missing_attempts": usage_missing_attempts,
                "usage_partial_attempts": usage_partial_attempts,
                "usage_incomplete_attempts": usage_incomplete_attempts,
                "usage_complete": bool(transport_attempts) and not usage_incomplete_attempts,
                "cache_usage_complete": bool(transport_attempts) and not cache_incomplete_attempts,
                "cache_usage_incomplete_attempts": cache_incomplete_attempts,
                "cache_usage_source": next(iter(cache_sources)) if len(cache_sources) == 1 else "mixed",
                "transport_attempts": transport_attempts,
                "tool_choice": actual_tool_choice,
                "output_chars": len(text),
                "finish_reason": str(meta.get("finish_reason") or ""),
                "attempts": attempts,
                "error": error,
                **context_metadata,
            }
        )
        self._write_status()
        if budget_error is not None:
            raise budget_error
        return response

    # -- heartbeat -----------------------------------------------------
    def start_heartbeat(self, interval: float = 5.0) -> None:
        """Refresh status.json while model calls are in flight.

        Without it ``updated_at``/``elapsed_s`` only move when a call finishes,
        so the HCCA tab looks frozen during a slow (or stalled) gateway request.
        """
        if self._heartbeat_thread is not None:
            return

        def _beat() -> None:
            while not self._heartbeat_stop.wait(interval):
                try:
                    now = time.time()
                    idle_limit = float(
                        getattr(self.llm, "idle_timeout", 0.0)
                        or getattr(self.llm, "timeout", 0.0)
                        or 240.0
                    )
                    for entry in list(self._pending.values()):
                        entry["elapsed_s"] = round(now - float(entry.get("t0") or now), 1)
                        last = float(entry.get("last_progress_t") or entry.get("t0") or now)
                        idle_s = round(now - last, 1)
                        entry["idle_s"] = idle_s
                        entry["stalled"] = idle_s > idle_limit
                    for entry in list(self._pending_tools.values()):
                        entry["elapsed_s"] = round(now - float(entry.get("t0") or now), 1)
                    self._write_status()
                except Exception:  # noqa: BLE001 - a heartbeat must never kill the run
                    pass

        self._heartbeat_thread = threading.Thread(target=_beat, name="hcca-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._heartbeat_thread = None

    def _llm_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        on_stream: Any = None,
        max_output_tokens: int | None = None,
        call_timeout: float | None = None,
        tool_choice: str | None = None,
        reasoning_effort: str | None = None,
    ) -> Any:
        """One HTTP request to the model.

        There is deliberately **no global lock**: a stalled request must not block
        every other Agent (a hung gateway call used to freeze the whole run and
        look like a crash).  Set ``HCCA_LLM_CONCURRENCY=N`` if the gateway
        dislikes bursts; 0 (default) means no artificial cap.
        """
        kwargs = {
            "max_output_tokens": None,
            "temperature": None,
            "seed": None,
            "tools": tools or None,
        }
        if on_stream is not None and getattr(self, "_llm_accepts_on_stream", False):
            kwargs["on_stream"] = on_stream
        if call_timeout is not None and getattr(self, "_llm_accepts_call_timeout", False):
            kwargs["call_timeout"] = call_timeout
        if tool_choice is not None:
            if getattr(self, "_llm_accepts_tool_choice", False):
                kwargs["tool_choice"] = tool_choice
        client = self.llm
        if reasoning_effort is not None and hasattr(client, "reasoning_effort"):
            client = copy.copy(client)
            client.reasoning_effort = reasoning_effort
        return client.complete(messages, **kwargs)

    def _messages(self, instructions: str, user: str) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": _AGENT_SYSTEM},
            {"role": "user", "content": f"当前职责\n{instructions}\n\n当前任务\n{user}"},
        ]

    def _call(
        self, *, agent_id: str, system: str, user: str, tag: str,
        budget: CallBudget | None = None,
    ) -> str:
        messages = self._messages(system, user)
        messages.extend(self._take_guidance(agent_id))
        try:
            response = self._complete(agent_id=agent_id, messages=messages, tag=tag, tools=None, budget=budget)
        except BudgetExceeded:
            return ""
        return (getattr(response, "text", "") or "").strip()

    def _call_with_tools(
        self,
        *,
        agent_id: str,
        system: str,
        user: str,
        tag: str,
        max_rounds: int | None = None,
        budget: CallBudget | None = None,
    ) -> str:
        """Offer tools and accept a native report or a text answer.

        Tool results are appended as ``role=tool`` messages, so the stable system
        prefix (and the stable tool schemas) stay cacheable across rounds. The
        parent and its workers share one bounded request budget. A positive
        tool-round cap additionally requests a final report from existing evidence.
        """
        budget = budget or self._new_budget(agent_id, tag)
        try:
            budget.check()
        except BudgetExceeded:
            return ""
        if self.toolbox is None:
            return self._call(agent_id=agent_id, system=system, user=user, tag=tag, budget=budget)
        tools = [
            tool for tool in self.toolbox.schemas()
            if tool["function"]["name"] not in {
                "write_file", "run_command", "read_file", "list_files", "search_files",
                "read_execution_log",
            }
        ]
        tools.append(self.executor.schema())
        tools.append(self.reader.schema())
        if self.enable_guidance:
            tools.append(self._guidance_schema())
        tools.append(report_schema())
        allowed_tools = {tool["function"]["name"] for tool in tools}
        initial = self._messages(system, user)
        history: list[dict[str, Any]] = []
        last_text = ""
        evidence: list[str] = []
        cutoff = ""
        worker_budget = budget.child(reserve=1)
        rounds = self.max_tool_rounds if max_rounds is None else max(0, int(max_rounds))
        round_index = 0
        while True:
            if budget.remaining <= 1:
                break
            history.extend(self._take_guidance(agent_id))
            messages, _ = bounded_tool_messages(initial, history, tools)
            round_tag = tag if round_index == 0 else f"{tag}:tool{round_index}"
            try:
                response = self._complete(
                    agent_id=agent_id, messages=messages, tag=round_tag, tools=tools,
                    budget=budget,
                )
            except BudgetExceeded as exc:
                cutoff = str(exc)
                break
            response_text = str(getattr(response, "text", "") or "")
            text = response_text.strip()
            if text:
                last_text = text
            calls = list(getattr(response, "tool_calls", None) or ())
            try:
                report = submitted_report(calls)
            except ValueError as exc:
                return "\n\n".join(part for part in [last_text, *evidence[-8:], f"【交付未完成】{exc}"] if part)
            if report is not None:
                return report["summary"]
            if not calls:
                if text:
                    return text
                return "\n\n".join(part for part in [last_text, *evidence[-8:], "【交付未完成】模型未返回正文"] if part)
            if rounds > 0 and round_index >= rounds:
                # Tool budget spent: keep this batch unexecuted and request a report.
                break
            assistant = {
                "role": "assistant",
                "content": response_text,
                "tool_calls": [
                    {
                        "id": str(call.get("id") or f"call_{round_index}_{index}"),
                        "type": "function",
                        "function": {
                            "name": str(call.get("name") or ""),
                            "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                        },
                    }
                    for index, call in enumerate(calls)
                ],
            }
            reasoning = getattr(response, "reasoning_content", None)
            if isinstance(reasoning, str):
                assistant["reasoning_content"] = reasoning
            history.append(assistant)
            for index, call in enumerate(calls):
                try:
                    worker_budget.check()
                except BudgetExceeded as exc:
                    cutoff = str(exc)
                    for pending in assistant["tool_calls"][index:]:
                        history.append({
                            "role": "tool", "tool_call_id": pending["id"],
                            "content": "ERROR: Tool was not executed because " + cutoff,
                        })
                    break
                name = str(call.get("name") or "")
                arguments = call.get("arguments") or {}
                tool_started = time.monotonic()
                tool_key = f"{agent_id}|{name}|{time.time_ns()}"
                self._pending_tools[tool_key] = {
                    "agent_id": agent_id,
                    "tool": name,
                    "args": arguments,
                    "started_at": _now(),
                    "t0": time.time(),
                    "elapsed_s": 0.0,
                }
                self._write_status()
                try:
                    if name not in allowed_tools:
                        result = "ERROR: 文件阅读交给 delegate_reading，命令、测试、修复和写入交给 delegate_execution。"
                    elif name == "delegate_reading":
                        result = self.reader.run(arguments, requester_id=agent_id, budget=worker_budget)
                    elif name == "delegate_execution":
                        allow_write = agent_id == "root" or not (
                            self.readonly_analysts or self.task_kind == "code"
                        )
                        result = self.executor.run(
                            arguments, requester_id=agent_id, allow_write=allow_write,
                            budget=worker_budget,
                        )
                    elif name == "send_guidance":
                        result = self._send_guidance(arguments, agent_id)
                    else:
                        result = self.toolbox.call(name, arguments)
                finally:
                    self._pending_tools.pop(tool_key, None)
                self.tool_log.append({
                    "tool": name,
                    "agent_id": agent_id,
                    "tag": round_tag,
                    "args": {str(key): str(value)[:240] for key, value in arguments.items()},
                    "ok": (
                        _extract_json(result).get("status") == "completed"
                        if name in {"delegate_execution", "delegate_reading"} else not result.startswith("ERROR:")
                    ),
                    "chars": len(result),
                    "duration_ms": int((time.monotonic() - tool_started) * 1000),
                })
                evidence.append(f"[{call.get('name')}] {result[:1500]}")
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": assistant["tool_calls"][index]["id"],
                        "content": result,
                    }
                )
            if cutoff:
                break
            round_index += 1
        history.append({"role": "user", "content": REPORT_FINAL_INSTRUCTION})
        history.extend(self._take_guidance(agent_id))
        messages, _ = bounded_tool_messages(initial, history, tools)
        try:
            final = self._complete(
                agent_id=agent_id,
                messages=messages,
                tag=f"{tag}:final",
                tools=tools,
                tool_choice=REPORT_TOOL_CHOICE,
                budget=budget,
            )
            final_calls = list(getattr(final, "tool_calls", None) or ())
            report = submitted_report(final_calls)
            if report is not None:
                return report["summary"]
            final_text = (getattr(final, "text", "") or "").strip()
            if final_calls:
                cutoff = "收尾响应未提交 submit_report"
            elif final_text:
                return final_text
        except (BudgetExceeded, ValueError) as exc:
            cutoff = str(exc)
        if cutoff:
            if agent_id != "root":
                self._past_analyst_deadline()
            return "\n\n".join(part for part in [last_text, *evidence[-8:], f"【交付未完成】{cutoff}"] if part)
        return "\n\n".join(part for part in [last_text, *evidence[-8:], "【交付未完成】模型未返回正文"] if part)

    # -- prompts -------------------------------------------------------
    def _tool_hint(self) -> str:
        if self.toolbox is None:
            return ""
        reading_hint = (
            "已知可改文件范围时，可直接委派 delegate_execution fix，由执行者读取必要源码、修复并用项目现有 runner 复验。"
            "范围未知时，Root 的源码调查交给 delegate_reading，一单覆盖一个完整问题。"
            if self.task_kind == "code" else
            "文件阅读与检索交给 delegate_reading，一单覆盖一个完整问题。"
        )
        return (
            f"\n工作区 {self.workspace}，文件路径用相对路径。"
            + reading_hint
            + "委派 command/test/fix 分别用于命令、测试、修复或写入，fix 列明可改文件。"
            "只传必要任务信息，执行上下文独立。网页证据用 web_fetch 核实并附 URL。\n"
        )

    def _plan_task(self) -> dict[str, Any]:
        """Ask the model whether this task needs a team, and how to split it.

        The team shape is *not* a fixed taxonomy: Root decides names/objectives
        freely (it may spawn e.g. 「财务数据核查」「政策时间线」「反方论证」).
        ``force_mode`` / ``readonly_analysts`` are experiment controls: they
        override the model's mode choice and keep sub-Agents read-only so a
        parallel team cannot corrupt a single shared code checkout.
        """
        if self.force_mode == "solo":
            self._verifier_prompt = ""
            return {"mode": "solo", "agents": [], "verify": False, "verify_prompt": ""}
        system = (
            "你是 HCCA 的 Root 调度器。先判断任务类型，再决定怎么拆：\n"
            "- solo：由 Root 阅读、规划、验收即可完成的任务，执行工作按需委派给专用执行角色；\n"
            "- team：需要外部调研、多维度分析、对比、机制解释或长报告的任务，由你自由拆分子任务。\n"
            '只输出 JSON，不要解释：{"mode":"solo"|"team",'
            '"agents":[{"name":"简短角色名","prompt":"这个子 Agent 的完整指令"}],'
            '"verify":true|false 或 "verify":"复核者的完整任务说明"}\n'
            "team 时：拆几个、怎么拆完全由你决定；每个 agent 的 prompt 由你自由编写——写清它的角色、"
            "要查/要读什么、方法、交付格式和边界，就像给一个同事下任务。互相重叠或没必要拆的就少拆。\n"
            "verify 只在需要独立复核时设置；需要复核时请给出复核者要检查的具体点（证据、推导、反例、"
            "一致性），不要泛泛写“检查质量”。"
        )
        if self.task_kind == "code":
            system += "\n代码任务先调查和实现，由执行者随修复复验，verify 设为 false。"
        if self.force_mode == "team":
            system += (
                "\n\n【强制模式】本次运行由调度方强制使用 team 模式，"
                'mode 字段必须输出 "team"。'
                "必须给出恰好 2 个子 Agent；子 Agent 只做只读调查与验证，"
                "通过专用执行角色请求测试。实现修改由 Root 委派执行角色，职责互不重叠。"
            )
        budget = self._new_budget("root", "root-plan")
        text = self._call(agent_id="root", system=system, user=f"任务：{self.goal}", tag="root-plan", budget=budget)
        mode, agents, verify, verify_prompt = self._parse_plan(_extract_json(text))
        if self.force_mode == "team" and mode != "team":
            retry_system = system + "\n上一次输出不符合强制模式要求，请重新输出严格符合要求的 JSON。"
            retry_text = self._call(
                agent_id="root", system=retry_system, user=f"任务：{self.goal}", tag="root-plan:retry", budget=budget
            )
            mode, agents, verify, verify_prompt = self._parse_plan(_extract_json(retry_text))
        if mode not in ("solo", "team"):
            simple = any(word in self.goal for word in ("读取", "统计", "写入", "文件", "计算", "转换", "整理"))
            mode = "solo" if simple else "team"
        if self.force_mode == "team":
            mode = "team"
            if not agents:
                agents = [
                    {
                        "name": "定位与复现",
                        "prompt": f"只读调查并复现问题：{self.goal}\n给出相关文件、根因假设、验证命令。",
                    },
                    {
                        "name": "交叉核查",
                        "prompt": f"独立复核第一路调查并补充遗漏：{self.goal}",
                    },
                ]
            if len(agents) == 1:
                agents.append(
                    {"name": "交叉核查", "prompt": f"独立复核另一路调查并补充遗漏：{self.goal}"}
                )
            agents = agents[:2]
            if self.readonly_analysts:
                for item in agents:
                    item["prompt"] = (
                        str(item["prompt"]).rstrip()
                        + "\n\n【硬约束】你是只读调查者：禁止修改工作区任何文件，禁止 git commit/checkout/reset；"
                        "只允许读文件、搜索、委派只读命令与测试。实现修改由 Root 委派执行角色。"
                    )
        if mode == "team" and not agents:
            agents = [{"name": "research", "prompt": f"研究并回答：{self.goal}"}]
        if mode == "solo":
            agents = []
        self._verifier_prompt = verify_prompt if mode == "team" else ""
        return {
            "mode": mode,
            "agents": agents,
            "verify": verify and mode == "team",
            "verify_prompt": self._verifier_prompt,
        }

    def _parse_plan(
        self, data: Mapping[str, Any]
    ) -> tuple[str, list[dict[str, str]], bool, str]:
        """Normalise the Root planner JSON into (mode, agents, verify, verify_prompt)."""
        mode = str(data.get("mode") or "").strip().lower()
        agents = self._normalise_agents(data)
        verify_raw = data.get("verify")
        verify_prompt = ""
        if isinstance(verify_raw, str) and verify_raw.strip():
            verify_prompt = verify_raw.strip()
            verify = True
        else:
            verify = bool(verify_raw)
        if self.task_kind == "code":
            verify, verify_prompt = False, ""
        return mode, agents, verify, verify_prompt

    def _normalise_agents(self, data: Mapping[str, Any]) -> list[dict[str, str]]:
        agents: list[dict[str, str]] = []
        raw = data.get("agents")
        if isinstance(raw, list):
            for index, item in enumerate(raw):
                if not isinstance(item, Mapping):
                    continue
                prompt = str(item.get("prompt") or item.get("objective") or "").strip()
                if not prompt:
                    continue
                name = str(item.get("name") or item.get("role") or f"agent-{index + 1}").strip()
                agents.append({"name": name[:40] or f"agent-{index + 1}", "prompt": prompt})
        if agents:
            return agents
        # Backward compatibility: the older plan format used fixed dimensions.
        legacy = data.get("dimensions")
        if isinstance(legacy, list):
            for raw_dim in legacy:
                dim = str(raw_dim).strip()
                if not dim:
                    continue
                if dim in ASPECTS:
                    agents.append(
                        {"name": dim, "prompt": f"从「{dim}」维度分析：{self.goal}。{ASPECTS[dim]}"}
                    )
                else:
                    agents.append({"name": dim, "prompt": f"围绕「{dim}」完成子任务：{self.goal}"})
        return agents

    def _solo_system(self) -> str:
        if self.task_kind == "code":
            return (
                "你是 HCCA 的 Root，本次由你负责规划、定位与验收。\n"
                "按具体缺口委派工作，复用已有证据。"
                "在本次会话内依据执行证据验收差异与测试结果，具体失败交执行者修复复验。"
                "完成后给出结论（改了哪些文件 + 为什么 + 跑了哪些测试及结果），保留未完成项。\n"
                "不要修改测试文件；不要写 JSON。" + self._tool_hint()
            )
        return (
            "你是 HCCA 的 Root，本次由你负责阅读、分析与验收，按需委派执行工作。\n"
            "完成任务后给出结论（结论 + 依据 + 产出文件路径）。"
            "不要写 JSON，也不要为了简短而省略必要步骤。" + self._tool_hint()
        )

    def _plan_text(self) -> str:
        plan = self._plan or {}
        agents = plan.get("agents") or []
        if not agents:
            return "（无子 Agent）"
        lines = [f"- {item['name']}：{item['prompt']}" for item in agents]
        return "\n".join(lines)

    def _review_text(self) -> str:
        if not self._review_log:
            return "（无）"
        return "\n".join(
            f"- 第 {item['round']} 轮复盘：{item['decision']}"
            + (f"（{item['reason']}）" if item.get("reason") else "")
            + (f"，补派：{', '.join(item['agents'])}" if item.get("agents") else "")
            for item in self._review_log
        )

    def _review_decision(self) -> dict[str, Any]:
        """Root looks for gaps and may order another wave of sub-Agents."""
        if self.no_review_waves:
            return {"decision": "done", "reason": "review waves disabled for this run", "agents": []}
        if self.max_review_rounds and self._review_round >= self.max_review_rounds:
            return {"decision": "done", "reason": f"已达到复盘轮数上限 {self.max_review_rounds}", "agents": []}
        system = (
            "你是 HCCA Root 的验收阶段。阅读已收到的全部材料，判断研究目标是否真的已经完成。\n"
            "若还缺关键事实、推导步骤、反例、计算、文件产物或对某个子问题的回答，就继续派子 Agent 补齐："
            "每个子 Agent 的 name 和 prompt 都由你自由编写，写清目标、方法、要读写的文件、交付格式。\n"
            "特别注意：交付里标着「未完成/【未证】/尚未落盘/剩余命题/无法证明」或子 Agent 自己说某项没做的，"
            "一律视为该任务未完成，必须继续派 Agent 去补；只有它附上真实命令证据表明被外部条件"
            "（文件不存在/无权限/工具不可用）卡死时，才可以当作阻塞项。\n"
            "若不缺，就结束。不要为了凑数而派活，也不要因为材料不好看就草草结束。\n"
            "只输出 JSON：{\"decision\":\"continue\"|\"done\",\"reason\":\"一句话说明\","
            "\"agents\":[{\"name\":\"...\",\"prompt\":\"...\"}]}"
        )
        user = (
            f"研究目标：{self.goal}\n\nRoot 原计划：\n{self._plan_text()}\n\n"
            f"已收到的材料：\n{self._digest()}\n\n已补派记录：\n{self._review_text()}"
        )
        text = self._call(agent_id="root", system=system, user=user, tag="root-review")
        data = _extract_json(text)
        decision = str(data.get("decision") or "").strip().lower()
        agents = self._normalise_agents(data) if decision == "continue" else []
        if decision != "continue" or not agents:
            return {"decision": "done", "reason": str(data.get("reason") or ""), "agents": []}
        return {"decision": "continue", "reason": str(data.get("reason") or ""), "agents": agents}

    def _final_editor_system(self) -> str:
        return (
            "你是 HCCA 的终稿审阅者。对照研究目标审阅初稿：是否真正回答了问题、结论是否有证据、"
            "有没有把未核实的东西写成事实、有没有漏掉关键反方观点或必要步骤、是否完整。\n"
            "需要时用工具核对关键事实、补读文件或补算，然后输出最终交付物（Markdown 正文，"
            "不要写审阅过程）。篇幅按任务需要决定，不要为了短而删掉必要内容。"
            + self._tool_hint()
        )

    def _report_phase(
        self, handle: Any, *, solo: bool, workspace_before: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._root_stage[handle.agent_id] = "report"
        budget = self._new_budget(handle.agent_id, "root-report")
        status = "COMPLETED"
        if self.task_kind == "code" and self.executor is not None:
            workspace_before = workspace_before or self.capture_workspace_diff()
        if workspace_before is not None:
            arguments = {
                "task": self.goal, "mode": "fix", "paths": ["."],
                "acceptance": (
                    "自主定位、读取必要源码并完成修复，用项目现有 runner 验证相关行为，保持测试文件不变。"
                    "复用已有证据，实际失败在本任务内调整并复验，提交修改与验证结果及未完成项。\n"
                    "已有调查摘要（节选）\n" + self._digest()[:1200]
                ),
            }
            resume_plan = None
            if self.plan_before_execution:
                envelope = self.executor._envelope(arguments, True)
                resume_plan = self._plan_resume_next_step(budget, workspace_before, fresh_envelope=envelope)
            elif self.resume_next_step_plan and self.resume_execution is not None:
                resume_plan = self._plan_resume_next_step(budget, workspace_before)
            execution = _extract_json(self.executor.run(
                arguments,
                requester_id=handle.agent_id,
                allow_write=True,
                budget=budget,
                capture_workspace_diff=self.capture_workspace_diff,
                resume_from=self.resume_execution,
                resume_plan=resume_plan,
            ))
            self.resume_execution = None
            status = {"completed": "COMPLETED", "failed": "FAILED", "blocked": "BLOCKED"}.get(
                execution.get("status"), "FAILED",
            )
            report = str(execution.get("summary") or "执行者未返回有效结果。")
            if execution.get("log_path"):
                report += f"\n执行日志 {execution['log_path']}"
        else:
            if solo:
                system = self._solo_system()
                user = f"任务：{self.goal}"
                tag = "solo-task"
            else:
                system = self._root_system()
                user = (
                    f"研究目标：{self.goal}\n\nRoot 的任务拆解：\n{self._plan_text()}\n\n"
                    f"分析材料：\n{self._digest()}\n\n复核意见：\n{self._verdict_text()}\n\n"
                    f"补派/复盘记录：\n{self._review_text()}"
                )
                tag = "root-report"
            draft = self._call_with_tools(
                agent_id=handle.agent_id,
                system=system,
                user=user,
                tag=tag,
                budget=budget,
            )
            report = draft
            if self.task_kind != "code":
                final_user = (
                    f"任务：{self.goal}\n\n你的初稿：\n{draft}\n\n请审阅并给出最终版本。"
                    if solo else (
                        f"研究目标：{self.goal}\n\nRoot 的任务拆解：\n{self._plan_text()}\n\n"
                        f"分析材料（供核对，不要照抄）：\n{self._digest()}\n\n"
                        f"复核意见：\n{self._verdict_text()}\n\n你写的初稿：\n{draft}\n\n"
                        "请审阅初稿、补齐缺漏后输出最终报告。"
                    )
                )
                report = self._call_with_tools(
                    agent_id=handle.agent_id,
                    system=self._final_editor_system(),
                    user=final_user,
                    tag=f"{tag}:revise",
                    budget=budget,
                ) or draft
        self._report = (report or "").strip()
        return {
            "actions": [
                {
                    "type": "SUBMIT",
                    "result": {
                        "task_id": handle.task_id,
                        "status": status,
                        "summary": self._report[:400],
                        "payload": {"report": self._report},
                    },
                }
            ],
            "continuation": {},
            "usage": {
                "tokens": sum(int(c["prompt_tokens"]) + int(c["completion_tokens"]) for c in self.calls),
                "calls": len(self.calls),
            },
        }

    def _plan_resume_next_step(self, budget: CallBudget, workspace_snapshot: dict[str, Any], *, fresh_envelope: dict[str, Any] | None = None) -> dict[str, Any]:
        """Use the existing high controller once for fresh work or continuation."""
        if fresh_envelope is not None:
            if not self.plan_before_execution or self.resume_execution is not None:
                raise ValueError("fresh planning cannot use continuation history")
            state = {"envelope": fresh_envelope, "history": [], "resume_planning": self._resume_planning,
                     "provenance": {"source_sha256": None, "source_kind": "fresh_git",
                                    "goal_sha256": hashlib.sha256(self.goal.encode("utf-8")).hexdigest()}}
        else:
            state = load_execution_resume(self.resume_execution)
        expected = state.get("workspace_after")
        if state["envelope"]["task"] != self.goal.strip() or state["envelope"]["paths"] != ["."]:
            raise ValueError("continuation planner task or scope differs from the original execution")
        if expected is not None and any(
            expected.get(key) is not None and workspace_snapshot.get(key) != expected[key]
            for key in ("diff_sha256", "workspace_change_sha")
        ):
            raise ValueError("restored workspace differs before continuation planning")
        previous = state.get("resume_planning") or {}
        if previous.get("attempted") or previous.get("completed"):
            self._resume_planning = previous
            return previous
        if self.resume_planning_record is not None:
            return self._import_resume_planning_record(state, workspace_snapshot)
        if fresh_envelope is None and self.role_reasoning_effort["thinking"] != "high":
            raise ValueError("resume next-step planning requires high controller reasoning")
        budget.check()
        def git_timeout() -> float:
            budget.check()
            remaining = budget.remaining_time
            return min(120.0, remaining) if remaining is not None else 120.0
        pathspec = ["."]
        run_dir = self.out_dir.resolve()
        artifacts = [run_dir] if run_dir != self.workspace else [
            run_dir / name for name in ("executions", "readings", "status.json", "report.md", "delegate.log", "resume_plan.json")
        ]
        for artifact in artifacts:
            if artifact.is_relative_to(self.workspace):
                pathspec.append(":(exclude,literal)" + artifact.relative_to(self.workspace).as_posix())
        patch = subprocess.run(
            ["git", "-C", str(self.workspace), "diff", "--binary", "HEAD", "--", *pathspec],
            capture_output=True, timeout=git_timeout(),
        )
        if patch.returncode != 0 or hashlib.sha256(patch.stdout).hexdigest() != workspace_snapshot["diff_sha256"]:
            raise ValueError("workspace patch changed before continuation planning")
        with tempfile.TemporaryDirectory(prefix=".resume-plan-index-", dir=self.out_dir) as directory:
            environment = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))
            for arguments in (["read-tree", "HEAD"], ["add", "-A", "--", *pathspec]):
                result = subprocess.run(["git", "-C", str(self.workspace), *arguments], env=environment,
                                        capture_output=True, timeout=git_timeout())
                if result.returncode != 0:
                    raise ValueError("could not capture complete continuation patch in an isolated index")
            full_patch = subprocess.run(["git", "-C", str(self.workspace), "diff", "--cached", "--binary", "HEAD", "--", *pathspec],
                                        env=environment, capture_output=True, timeout=git_timeout())
            if full_patch.returncode != 0:
                raise ValueError("could not read complete continuation patch")
        if self.capture_workspace_diff() != workspace_snapshot:
            raise ValueError("workspace changed while capturing continuation planning evidence")
        names: dict[str, str] = {}
        evidence: list[dict[str, Any]] = []
        for message in state["history"]:
            for call in message.get("tool_calls") or []:
                names[str(call.get("id"))] = str((call.get("function") or {}).get("name", ""))
            if message.get("role") == "tool" and names.get(str(message.get("tool_call_id"))) != "submit_report":
                content = str(message.get("content", ""))
                evidence.append({
                    "tool": names.get(str(message.get("tool_call_id"))),
                    "content": content[:6000] + ("\n[Public tool evidence truncated after 6000 chars]" if len(content) > 6000 else ""),
                })
        planning_input = {
            "original_task": state["envelope"]["task"], "current_patch": full_patch.stdout.decode("utf-8"),
            "workspace_snapshot": workspace_snapshot, "recent_public_tool_evidence": evidence[-8:],
        }
        if fresh_envelope is not None:
            planning_input = {
                "public_environment": fresh_envelope["environment"],
                "current_patch": planning_input["current_patch"],
                "workspace_snapshot": planning_input["workspace_snapshot"],
                "recent_public_tool_evidence": planning_input["recent_public_tool_evidence"],
                "original_task": planning_input["original_task"],
            }
        messages = [
            {"role": "system", "content": "你是HCCA修复规划Agent，根据给定证据提出定位、修复和验证建议，将待验证假设交给执行者读取源码核实。"},
            {"role": "user", "content": "请给执行者下一步定位与验证建议。\n" + json.dumps(planning_input, ensure_ascii=False)},
        ]
        messages.extend(self._take_guidance("root"))
        response = None
        call_error = None
        try:
            response = self._complete(agent_id="root", messages=messages, tag="root-resume-plan", tools=None, budget=budget)
        except BudgetExceeded as exc:
            call_error = str(exc)
        raw_text = getattr(response, "text", None)
        raw_calls = getattr(response, "tool_calls", None)
        meta = getattr(response, "meta", None) or {}
        call_error = call_error or (self.calls[-1].get("error") if self.calls else None)
        plan = {"schema": "hcca.resume.plan.v1", "attempted": True, "completed": False,
                "advice_available": False, "text": "", "outcome": "received_uninterpreted",
                "agent_id": "root", "model": getattr(self.llm, "model", None),
                "tag": "root-resume-plan", "reasoning_effort": self.role_reasoning_effort["thinking"],
                "source_sha256": state["provenance"]["source_sha256"], "workspace_snapshot": workspace_snapshot,
                "logical_messages": messages, "raw_reply_available": response is not None,
                "raw_reply": {"text": raw_text, "reasoning_content": getattr(response, "reasoning_content", None),
                              "tool_calls": raw_calls, "meta": meta},
                "usage": meta.get("usage"), "transport_attempts": self.calls[-1].get("transport_attempts", []) if self.calls else [],
                "call_error": call_error}
        plan["complete_patch_sha256"] = hashlib.sha256(full_patch.stdout).hexdigest()
        if fresh_envelope is not None:
            plan.update(source_kind="fresh_git", goal_sha256=state["provenance"]["goal_sha256"])
        self._save_resume_plan(plan)  # Durable raw reply precedes all advice normalization.
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        suggestions = []
        sequence = raw_calls if isinstance(raw_calls, (list, tuple)) else []
        for call in sequence:
            if not isinstance(call, Mapping):
                continue
            function = call.get("function") or call
            if isinstance(function, Mapping) and isinstance(function.get("name"), str) and function["name"]:
                suggestions.append({"name": function["name"], "arguments": function.get("arguments")})
        if suggestions:
            advice = "规划模型提出的工具调用建议（尚未执行，需执行者核对后决定）：\n" + json.dumps(suggestions, ensure_ascii=False, default=str)
            text = text + "\n" + advice if text else advice
        plan.update(text=text, completed=bool(text), advice_available=bool(text),
                    client_visible_text_empty=not bool(raw_text.strip()) if isinstance(raw_text, str) else True,
                    tool_calls_executed=False, unknown_structure=bool(raw_calls) and (not sequence or len(suggestions) != len(sequence)),
                    outcome=("text_and_unexecuted_tool_advice" if suggestions and isinstance(raw_text, str) and raw_text.strip()
                             else "unexecuted_tool_call_advice" if suggestions else "text_advice" if text else "no_usable_advice"))
        return self._save_resume_plan(plan)

    def _save_resume_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        path = self.out_dir / "resume_plan.json"
        payload = json.dumps(plan, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        marker = {key: value for key, value in plan.items() if key not in ("logical_messages", "raw_reply", "transport_attempts")}
        marker.update(record_path=str(path), record_sha256=hashlib.sha256(payload).hexdigest())
        self._resume_planning = marker
        self._write_status()
        return marker

    def _import_resume_planning_record(self, state: dict[str, Any], workspace_snapshot: dict[str, Any]) -> dict[str, Any]:
        raw = self.resume_planning_record.read_bytes()
        plan = json.loads(raw)
        if (plan.get("schema") != "hcca.resume.plan.v1" or plan.get("attempted") is not True
            or plan.get("completed") is not False or plan.get("advice_available") is not False or plan.get("text") != ""
            or plan.get("client_visible_text_empty") is not True or plan.get("raw_reply_available") is not False
            or plan.get("unknown_structure") is not True or plan.get("raw_reply") is not None
            or plan.get("logical_messages") is not None
            or plan.get("source_sha256") != state["provenance"]["source_sha256"]
            or plan.get("workspace_snapshot") != workspace_snapshot
            or plan.get("agent_id") != "root" or plan.get("tag") != "root-resume-plan"
            or plan.get("reasoning_effort") != "high" or not isinstance(plan.get("model"), str) or not plan["model"]
            or plan.get("model") != getattr(self.llm, "model", None)):
            raise ValueError("paid planning-attempt record differs from this source, workspace, role, or model")
        historical = plan.get("historical_attempt") or {}
        if not isinstance(historical, Mapping) or not isinstance(historical.get("status_path"), str):
            raise ValueError("paid planning-attempt record has no verifiable status source")
        source = Path(historical["status_path"]).read_bytes()
        if hashlib.sha256(source).hexdigest() != historical.get("status_sha256"):
            raise ValueError("paid planning status source changed")
        status = json.loads(source)
        calls = status.get("calls") or []
        if len(calls) != 1:
            raise ValueError("paid planning import requires a planner-only attempt")
        call = calls[0]
        attempts = call.get("transport_attempts") or []
        if (status.get("model") != plan["model"] or str(status.get("goal", "")).strip() != self.goal.strip()
            or call.get("agent_id") != "root" or call.get("tag") != "root-resume-plan"
            or call.get("output_chars") != 0 or call.get("error") or len(attempts) != 1
            or attempts[0].get("usage_complete") is not True or attempts[0].get("error")
            or (attempts[0].get("role_reasoning_effort") or (attempts[0].get("meta") or {}).get("reasoning_effort")) != "high"
            or status.get("pending_calls") or status.get("executions")):
            raise ValueError("paid planning source does not prove one complete high request and zero execution")
        if historical.get("result_path") is not None:
            result = Path(historical["result_path"]).read_bytes()
            if hashlib.sha256(result).hexdigest() != historical.get("result_sha256"):
                raise ValueError("paid planning result source changed")
        plan.update(outcome="imported_paid_attempt_unknown_reply_structure", historical_usage=attempts[0].get("usage"),
                    import_record_path=str(self.resume_planning_record), import_record_sha256=hashlib.sha256(raw).hexdigest(),
                    logical_messages=None, raw_reply=None)
        return self._save_resume_plan(plan)

    def _analyst_system(self, label: str, prompt: str) -> str:
        if self.task_kind == "code":
            return (
                f"你是只读调查者「{label}」。交付根因假设、代码位置、证据与建议修法。"
                "在本次任务内补齐必要证据并报告缺项，需要时委派只读命令或测试，"
                "源码修改与剩余工作交给 Root 安排。输出中文正文。"
                + self._tool_hint()
            )
        return (
            f"你是分析者「{label}」。完成任务要求的阅读、推导与交付。"
            "输出中文正文，结论先行，附文件或 URL 证据、反例与边界，区分事实和未核实项。"
            + self._tool_hint()
        )

    def _analyst_selfcheck_system(self, label: str, prompt: str) -> str:
        return (
            f"你是「{label}」，进行自检补全。对照任务补齐证据、推导与遗漏，"
            "输出完整的最终版中文正文。"
            + self._tool_hint()
        )

    #: Self-declared incompleteness.  Used only when the audit model returns
    #: unparseable JSON; it is deliberately conservative (a delivery that says
    #: "未完成/尚未落盘/未证" is not accepted as done).
    _PARTIAL_MARKERS = (
        "未完成",
        "未能完成",
        "尚未完成",
        "尚未落盘",
        "未落盘",
        "未写入",
        "无法完成",
        "无法证明",
        "工具轮次耗尽",
        "时间不足",
        "未能给出",
    )

    def _audit_completion(
        self, *, agent_id: str, label: str, prompt: str, text: str,
        budget: CallBudget | None = None,
    ) -> dict[str, Any]:
        """Check whether an analyst delivery actually finished Root's task.

        Sub-Agents used to return "任务未完成（剩余命题……）" after the tool-round
        cap and the runtime happily recorded it as COMPLETED.  The audit is the
        gate that turns that into another work round instead of a submission.
        """
        if budget is not None:
            try:
                budget.check()
            except BudgetExceeded as exc:
                return {"status": "partial", "why": str(exc), "missing": [], "budget_exhausted": True}
        system = (
            "你是 HCCA 的交付验收员。判断子 Agent 的这份交付是否真正完成了 Root 下达的任务。\n"
            "完成的标准：任务点名要读的文件读过、要跑的命令/计算真的跑过（交付里有真实输出）、"
            "要产出的文件/证明/结论真的给出；如果交付里还写着「未完成/未证/尚未落盘/剩余命题/"
            "下一步」等，或明确说某项没做，就不算完成。\n"
            "只有确实被外部条件卡死（文件不存在、无权限、工具/环境不可用、资源不够）才算 blocked；"
            "「方法行不通、还需要再试别的构造」算 partial，不算 blocked。\n"
            "只输出 JSON：{\"status\":\"done\"|\"partial\"|\"blocked\","
            "\"missing\":[\"具体还没完成的工作\"],\"why\":\"一句话理由\"}"
        )
        user = (
            f"子 Agent：{label}\n\nRoot 的任务说明：\n{prompt}\n\n"
            f"子 Agent 的交付正文：\n{text[:24000]}"
        )
        try:
            reply = self._call(
                agent_id=agent_id, system=system, user=user, tag=f"analyst-{agent_id}:audit", budget=budget
            )
        except Exception as exc:  # noqa: BLE001 - audit must not kill the step
            reply = ""
            why = f"audit call failed: {type(exc).__name__}: {exc}"
        else:
            why = ""
        data = _extract_json(reply)
        status = str(data.get("status") or "").strip().lower()
        missing = [str(item).strip() for item in (data.get("missing") or []) if str(item).strip()][:12]
        if status not in {"done", "partial", "blocked"}:
            status = "partial" if any(marker in text for marker in self._PARTIAL_MARKERS) else "done"
        if status == "done" and missing:
            # It claims completion but lists unfinished work: trust the list.
            status = "partial"
        audit = {
            "agent_id": agent_id,
            "label": label,
            "status": status,
            "missing": missing,
            "why": why or str(data.get("why") or ""),
            "audited_at": _now(),
        }
        self._audits.append(audit)
        return audit

    def _analyst_continue_system(self, label: str, prompt: str, missing: list[str]) -> str:
        missing_text = "\n".join(f"- {item}" for item in missing) or "- 交付里明确标注了未完成项"
        return (
            f"你是「{label}」，继续完成以下缺项\n{missing_text}\n"
            "依据已有结果调整方法，必要时委派执行。输出完整交付物，"
            "外部条件阻塞时附实际尝试的证据。"
            + self._tool_hint()
        )

    def _finish_analyst(
        self, *, agent_id: str, label: str, prompt: str, draft: str,
        budget: CallBudget | None = None,
    ) -> tuple[str, dict[str, Any], int]:
        """Forward coding findings, or self-check and audit a research delivery."""
        budget = budget or self._new_budget(agent_id, f"analyst-{agent_id}")
        if self._past_analyst_deadline() or budget.remaining <= 0:
            # The Root still needs its own implementation window: do not spend
            # the remaining budget on extra self-check/audit rounds here.
            return draft, {
                "status": "partial",
                "why": "analyst budget reached; submitted current findings",
                "missing": [],
                "deadline_skipped": True,
            }, 0
        if self.task_kind == "code":
            incomplete = not draft.strip() or any(marker in draft for marker in self._PARTIAL_MARKERS)
            return draft, {
                "status": "partial" if incomplete else "done",
                "why": "Coding findings handed to Root for implementation and acceptance.",
                "missing": [],
            }, 0
        text = self._call_with_tools(
            agent_id=agent_id,
            system=self._analyst_selfcheck_system(label, prompt),
            user=(
                f"总目标：{self.goal}\n\n"
                f"你的任务说明（由 Root 下达）：\n{prompt}\n\n"
                f"你的第一版分析：\n{draft}\n\n"
                "请对照任务说明自检补全：证据不足就补查、步骤缺失就补推、任务点没覆盖就补上，"
                "然后输出最终版正文。"
            ),
            tag=f"analyst-{agent_id}:selfcheck",
            max_rounds=3,
            budget=budget,
        ) or draft
        audit = self._audit_completion(
            agent_id=agent_id, label=label, prompt=prompt, text=text, budget=budget
        )
        attempts = 0
        while (
            audit["status"] == "partial"
            and attempts < self.max_finish_rounds
            and not self._past_analyst_deadline()
            and budget.remaining > 0
        ):
            attempts += 1
            text = self._call_with_tools(
                agent_id=agent_id,
                system=self._analyst_continue_system(label, prompt, list(audit.get("missing") or [])),
                user=(
                    f"总目标：{self.goal}\n\n"
                    f"你的任务说明（由 Root 下达）：\n{prompt}\n\n"
                    f"你上一版交付（仅供对照，不要照抄）：\n{text}\n\n"
                    "继续把没完成的工作做完，然后输出完整最终版正文。"
                ),
                tag=f"analyst-{agent_id}:finish{attempts}",
                budget=budget,
            ) or text
            audit = self._audit_completion(
                agent_id=agent_id, label=label, prompt=prompt, text=text, budget=budget
            )
        return text, audit, attempts

    def _verifier_system(self, prompt: str = "") -> str:
        focus = (
            f"\nRoot 要求你重点核对：\n{prompt}\n"
            if prompt
            else ""
        )
        if self.task_kind == "code":
            return (
                "你是 HCCA 的独立复核者。通过 delegate_execution 委派查看当前工作区差异和相关测试"
                "（以及必要的回归测试），独立判断修复是否正确、完整以及是否有回归。"
                "若发现明确错误，把修复范围与验收标准交给 Root 委派处理。"
                + focus
                + "\n只输出中文，格式：\nverdict: PASS | PASS_WITH_ISSUES | REVISE\n"
                "issues: 逐条（高/中/低｜问题｜建议）\n证据: 你实际运行的命令与测试结果\n必须修正项: 按需要列出。"
                + self._tool_hint()
            )
        return (
            "你是 HCCA 的独立复核者。复核分析材料：事实一致性、是否覆盖了任务要求的各个部分、"
            "过度断言与逻辑跳跃、反方观点是否被处理、结论是否被证据支持。"
            + focus
            + "\n只输出中文，格式：\nverdict: PASS | PASS_WITH_ISSUES | REVISE\n"
            "issues: 逐条（高/中/低｜问题｜建议）\n必须修正项: 按需要列出，不要为了凑数删掉真问题。"
            + self._tool_hint()
            + "对可疑事实可用 web_search/web_fetch 抽查，也可以读文件或委派命令复核，并在 issues 里注明核实结果。"
        )

    def _root_system(self) -> str:
        if self.task_kind == "code":
            return (
                "你是 HCCA 团队的 Root（技术负责人）。吸收子 Agent 的只读调查结论并复用已有证据，"
                "按具体缺口委派工作，基于实际结果验收。\n"
                "在本次会话内验收差异与测试结果，具体失败交执行者修复复验，保留未完成项。\n"
                "交付：修改了哪些文件、为什么这样改、跑了哪些测试及结果。不要写研究报告，不要写 JSON。"
                + self._tool_hint()
            )
        return (
            "你是 HCCA 研究团队的 Root。根据材料写最终研究报告：它必须真正回答研究目标，"
            "并覆盖 Root 拆解里每一项任务的交付物。\n"
            "不要写 JSON，不要写过程说明。\n"
            "结构按任务需要组织（例如：一句话结论；关键事实/证据；机制与推导；反方观点与边界；不确定性），"
            "不要把材料里没有的东西写实；关键事实有疑问就用工具核对。篇幅按任务需要决定。"
            + self._tool_hint()
        )

    # -- step runner ---------------------------------------------------
    def _remember(self, messages: list[Any]) -> None:
        for message in messages:
            subject = str(getattr(message, "subject", "") or getattr(message, "sender", ""))
            body = str(getattr(message, "body", ""))
            if message.kind == "ANSWER":
                self._answers[message.message_id] = {"subject": subject, "body": body}
            elif message.kind == "NOTE":
                self._notes[message.message_id] = {"subject": subject, "body": body}

    def _spawn_actions(self, agents: list[dict[str, str]], *, round_index: int) -> list[dict[str, Any]]:
        spawns: list[dict[str, Any]] = []
        for index, agent in enumerate(agents):
            spawns.append(
                {
                    "type": "SPAWN",
                    "objective": agent["prompt"],
                    "task_kind": "analyst",
                    "acceptance": ["按 Root 的任务说明交付", "带依据的结论", "不确定性与反例"],
                    # Scheduler tokens are separate from the shared request cap.
                    "budget": {"tokens": 1_000_000_000},
                    "join_policy": "REQUIRED",
                    "metadata": {
                        "task_id": f"hcca.sub{round_index}_{index + 1}",
                        "dimension": agent["name"],
                        "prompt": agent["prompt"],
                        "objective": agent["prompt"],
                        "round": round_index,
                    },
                }
            )
        return spawns

    def step_runner(self, handle: Any, working_set: Any) -> dict[str, Any]:
        role = str(handle.role or "")
        messages = list(working_set.messages or ())
        self._remember(messages)
        if role == "root":
            stage = self._root_stage.setdefault(handle.agent_id, "plan")
            if stage == "plan":
                if self.task_kind == "code" and self.force_mode != "team" and self.executor is not None:
                    workspace_before = self.capture_workspace_diff()
                    if workspace_before is not None:
                        self._plan = {"mode": "solo", "agents": [], "verify": False, "verify_prompt": ""}
                        return self._report_phase(handle, solo=True, workspace_before=workspace_before)
                if self.unlimited_executor_rounds:
                    raise ValueError("unlimited execution requires a verifiable Git workspace")
                plan = self._plan_task()
                self._plan = plan
                if plan["mode"] == "solo":
                    self._root_stage[handle.agent_id] = "report"
                    return self._report_phase(handle, solo=True)
                self._root_stage[handle.agent_id] = "review"
                return {
                    "actions": self._spawn_actions(plan["agents"], round_index=0)
                    + [
                        {
                            "type": "SEND_MESSAGE",
                            "recipients": ["$children"],
                            "kind": "ASK",
                            "subject": "研究问题",
                            "body": self.goal,
                        },
                        {"type": "WAIT", "kind": "ALL_REQUIRED", "refs": ["$children"]},
                    ],
                    "continuation": {},
                    "usage": {"calls": 0, "tokens": 0},
                }
            if stage == "review":
                if self._past_analyst_deadline():
                    # Protect the implementation window: skip the review LLM call
                    # (and any further waves) and let the Root write/implement now.
                    self._forced_report = True
                    self._review_log.append(
                        {
                            "round": self._review_round + 1,
                            "decision": "forced_report",
                            "reason": "wall-clock analyst deadline reached; reserved budget for Root implementation",
                            "agents": [],
                        }
                    )
                    return self._report_phase(handle, solo=False)
                if self.task_kind == "code":
                    return self._report_phase(handle, solo=False)
                decision = self._review_decision()
                self._review_log.append(
                    {
                        "round": self._review_round + 1,
                        "decision": decision["decision"],
                        "reason": decision.get("reason", ""),
                        "agents": [agent["name"] for agent in decision["agents"]],
                    }
                )
                if decision["decision"] == "continue" and decision["agents"]:
                    self._review_round += 1
                    return {
                        "actions": self._spawn_actions(decision["agents"], round_index=self._review_round)
                        + [
                            {
                                "type": "SEND_MESSAGE",
                                "recipients": ["$new_children"],
                                "kind": "ASK",
                                "subject": "补派任务",
                                "body": (
                                    f"总目标：{self.goal}\n\n"
                                    f"你这一轮的任务说明（由 Root 下达）：\n"
                                    + "\n\n".join(
                                        f"【{agent['name']}】{agent['prompt']}" for agent in decision["agents"]
                                    )
                                ),
                            },
                            {"type": "WAIT", "kind": "ALL_REQUIRED", "refs": ["$new_children"]},
                        ],
                        "continuation": {},
                        "usage": {"calls": 0, "tokens": 0},
                    }
                self._root_stage[handle.agent_id] = "verify" if (self._plan or {}).get("verify") else "report"
                if self._root_stage[handle.agent_id] == "verify":
                    self._root_stage[handle.agent_id] = "report"
                    verify_prompt = (self._plan or {}).get("verify_prompt") or ""
                    objective = verify_prompt or (
                        f"独立复核关于「{self.goal}」的全部分析材料，指出过度断言、逻辑跳跃与遗漏。"
                    )
                    return {
                        "actions": [
                            {
                                "type": "SPAWN",
                                "objective": objective,
                                "task_kind": "verifier",
                                "acceptance": ["verdict", "issues", "必须修正项"],
                                "budget": {"tokens": 1_000_000_000},
                                "join_policy": "REQUIRED",
                                "metadata": {"task_id": "hcca.verify", "dimension": "verify"},
                            },
                            {
                                "type": "SEND_MESSAGE",
                                "recipients": ["$new_children"],
                                "kind": "NOTE",
                                "subject": "分析材料",
                                "body": f"{self._digest()}\n\nRoot 的计划：\n{self._plan_text()}",
                            },
                            {"type": "WAIT", "kind": "ALL_REQUIRED", "refs": ["$new_children"]},
                        ],
                        "continuation": {},
                        "usage": {"calls": 0, "tokens": 0},
                    }
                return self._report_phase(handle, solo=False)
            return self._report_phase(handle, solo=False)
        if role == "verifier":
            text = self._call_with_tools(
                agent_id=handle.agent_id,
                system=self._verifier_system(self._verifier_prompt),
                user=f"研究目标：{self.goal}\n\nRoot 的计划：\n{self._plan_text()}\n\n分析材料：\n{self._digest()}",
                tag="verify",
            )
            return {
                "actions": [
                    {
                        "type": "SEND_MESSAGE",
                        "recipients": ["root"],
                        "kind": "NOTE",
                        "subject": "复核结论",
                        "body": text,
                    },
                    {
                        "type": "SUBMIT",
                        "result": {
                            "task_id": handle.task_id,
                            "status": "COMPLETED",
                            "summary": text[:300],
                            "payload": {"verdict": {"raw": text}},
                        },
                    },
                ],
                "continuation": {},
                "usage": {"calls": 0, "tokens": 0},
            }
        # analyst: Root wrote this agent's instructions in the plan.
        metadata = dict((dict(handle.continuation or {}).get("metadata") or {}))
        label = str(metadata.get("dimension") or handle.agent_id)
        prompt = str(metadata.get("prompt") or metadata.get("objective") or self.goal)
        budget = self._new_budget(handle.agent_id, f"analyst-{handle.agent_id}")
        draft = self._call_with_tools(
            agent_id=handle.agent_id,
            system=self._analyst_system(label, prompt),
            user=(
                f"总目标：{self.goal}\n\n"
                f"你的任务说明（由 Root 下达）：\n{prompt}\n\n"
                "请完成你的任务并输出分析正文。"
            ),
            tag=f"analyst-{handle.agent_id}",
            budget=budget,
        )
        # Research deliveries retain self-check/audit rounds. Coding findings
        # and their remaining work go directly to Root's implementation phase.
        text, audit, finish_attempts = self._finish_analyst(
            agent_id=handle.agent_id, label=label, prompt=prompt, draft=draft, budget=budget
        )
        incomplete = audit["status"] != "done"
        if incomplete:
            header = (
                "【交付状态：未完成"
                + (f"，已自动补做 {finish_attempts} 轮仍未闭合" if finish_attempts else "")
                + f"】验收结论：{audit['status']}"
                + (f"（{audit['why']}）" if audit.get("why") else "")
            )
            if audit.get("missing"):
                header += "\n未完成项：\n" + "\n".join(f"- {item}" for item in audit["missing"])
            text = header + "\n\n" + text
        ask = next((message for message in messages if message.kind == "ASK"), None)
        answer: dict[str, Any] = {
            "type": "SEND_MESSAGE",
            "recipients": ["root"],
            "kind": "ANSWER",
            "body": text,
            "subject": f"{label} 分析" + ("（未完成）" if incomplete else ""),
        }
        if ask is not None:
            answer["reply_to"] = ask.message_id
        return {
            "actions": [
                answer,
                {
                    "type": "SUBMIT",
                    "result": {
                        "task_id": handle.task_id,
                        # Never report an unfinished delivery as COMPLETED; Root
                        # and the UI must be able to see that it still needs work.
                        "status": "BLOCKED" if audit["status"] == "blocked" else (
                            "FAILED" if incomplete else "COMPLETED"
                        ),
                        "summary": text[:300],
                        "payload": {
                            "analysis": text,
                            "dimension": label,
                            "round": metadata.get("round", 0),
                            "audit": audit,
                            "finish_attempts": finish_attempts,
                        },
                    },
                },
            ],
            "continuation": {"aspect": label},
            "usage": {"calls": 0, "tokens": 0},
        }

    def _digest(self) -> str:
        return (
            "\n\n".join(
                f"【{item['subject']}】\n{item['body']}" for item in self._answers.values()
            )
            or "（暂无分析）"
        )

    def _verdict_text(self) -> str:
        return (
            "\n\n".join(
                f"【{item['subject']}】\n{item['body']}" for item in self._notes.values()
            )
            or "（暂无复核意见）"
        )

    # -- status --------------------------------------------------------
    def write_status(self) -> None:
        self._write_status()

    def _tools_status(self) -> dict[str, Any]:
        if self.toolbox is None:
            return {"enabled": False}
        status = self.toolbox.stats()
        execution_log = list(self.executor.tool_log) if self.executor is not None else []
        reading_log = list(self.reader.tool_log) if self.reader is not None else []
        tool_log = list(self.tool_log) + execution_log + reading_log
        counts: dict[str, int] = {}
        for entry in tool_log:
            name = str(entry.get("tool") or "")
            counts[name] = counts.get(name, 0) + 1
        status["counts"] = counts
        status["calls"] = len(tool_log)
        per_agent: dict[str, dict[str, Any]] = {}
        recent: dict[str, list[dict[str, Any]]] = {}
        for entry in tool_log:
            agent_id = str(entry.get("agent_id") or "")
            if not agent_id:
                continue
            bucket = per_agent.setdefault(agent_id, {"calls": 0, "tools": {}})
            bucket["calls"] += 1
            tool = str(entry.get("tool") or "")
            bucket["tools"][tool] = int(bucket["tools"].get(tool, 0)) + 1
            recent.setdefault(agent_id, []).append(_tool_preview(entry))
        # ``log`` used to be the only per-agent source for the UI, so an Agent
        # whose tool calls fell out of the last-40 window looked idle even after
        # 20 real calls. Keep explicit per-Agent counts (and a short per-Agent
        # recent list) next to the global log.
        status["per_agent"] = per_agent
        status["recent_by_agent"] = {key: value[-6:] for key, value in recent.items()}
        status["log"] = [_tool_preview(entry) for entry in tool_log[-40:]]
        return status

    def _write_status(self) -> None:
        with self._status_lock:
            self._write_status_locked()

    def _write_status_locked(self) -> None:
        agents: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        handles = list((self.supervisor.handles if self.supervisor is not None else {}).items())
        results = dict(getattr(self.runtime, "_results", {}) or {}) if self.runtime is not None else {}
        calls = list(self.calls)
        for agent_id, handle in handles:
            contract = dict(getattr(handle, "continuation", None) or {})
            metadata = contract.get("metadata") or {}
            final = results.get(agent_id)
            result_status = str(getattr(final, "status", "") or "")
            waiting: dict[str, Any] | None = None
            if self.runtime is not None:
                try:
                    condition = self.runtime.scheduler.waiting_on(agent_id)
                    if condition is not None:
                        waiting = condition.to_dict()
                except Exception:  # noqa: BLE001 - status must never fail the run
                    waiting = None
            in_call = any(entry.get("agent_id") == agent_id for entry in self._pending.values())
            in_tool = any(entry.get("agent_id") == agent_id for entry in self._pending_tools.values())
            agents.append(
                {
                    "agent_id": agent_id,
                    "parent_id": getattr(handle, "parent_id", None),
                    "role": str(getattr(handle, "role", "agent")),
                    "dimension": str(metadata.get("dimension") or ""),
                    "task_id": str(getattr(handle, "task_id", "")),
                    "status": str(getattr(handle, "status", "CREATED")),
                    "result_status": result_status,
                    "depth": int(getattr(handle, "depth", 0) or 0),
                    "next_step": str(contract.get("next_step") or ""),
                    "objective": str(contract.get("objective") or ""),
                    "waiting_on": waiting,
                    "in_call": in_call,
                    "in_tool": in_tool,
                    "children": list(getattr(handle, "children", ()) or []),
                }
            )
            if getattr(handle, "parent_id", None):
                edges.append({"from": str(handle.parent_id), "to": agent_id, "kind": "supervises"})
        executions = [dict(job) for job in self.executor.jobs] if self.executor is not None else []
        readings = [dict(job) for job in self.reader.jobs] if self.reader is not None else []
        for job in executions + readings:
            agent_id = str(job["agent_id"])
            parent_id = str(job["requester_id"])
            state = str(job["status"]).upper()
            agents.append({
                "agent_id": agent_id,
                "parent_id": parent_id,
                "role": "reader" if job in readings else "executor",
                "dimension": str(job.get("mode", "reading")),
                "task_id": str(job["job_id"]),
                "status": state,
                "result_status": state if state in {"COMPLETED", "FAILED", "BLOCKED"} else "",
                "depth": next((int(a["depth"]) + 1 for a in agents if a["agent_id"] == parent_id), 1),
                "objective": str(job.get("task", ""))[:400],
                "in_call": any(entry.get("agent_id") == agent_id for entry in self._pending.values()),
                "in_tool": state == "RUNNING",
                "children": [],
            })
            edges.append({"from": parent_id, "to": agent_id, "kind": "reads" if job in readings else "executes"})
        hit = sum(c["cache_hit_tokens"] for c in self.calls)
        miss = sum(c["cache_miss_tokens"] for c in self.calls)
        out = sum(c["completion_tokens"] for c in self.calls)
        cache_sources = {c.get("cache_usage_source", "missing") for c in self.calls} or {"missing"}
        price = {"hit": 0.028e-6, "miss": 0.28e-6, "out": 0.42e-6}
        runtime_report: dict[str, Any] = {}
        if self.runtime is not None:
            try:
                runtime_report = self.runtime.report()
            except Exception:  # noqa: BLE001
                runtime_report = {}
        status = str(runtime_report.get("status") or "RUNNING")
        if self.failed_reason:
            status = "FAILED"
        snapshot = {
            "schema": "hcca.status.v1",
            "run_id": self.out_dir.name,
            "goal": self.goal,
            "status": status,
            "updated_at": _now(),
            "started_at": datetime.fromtimestamp(self.started, timezone.utc).isoformat(timespec="milliseconds"),
            "elapsed_s": round(time.time() - self.started, 1),
            "model": getattr(self.llm, "model", "unknown"),
            "mode": "generic",
            "execution_profile": {
                "unlimited_executor_rounds": self.unlimited_executor_rounds,
                "executor_max_rounds": self.executor.max_rounds if self.executor is not None else None,
                "executor_context_budget_tokens": self.executor.context_budget_tokens if self.executor is not None else None,
                "role_reasoning_effort": self.role_reasoning_effort,
                "resume_next_step_plan": self.resume_next_step_plan,
                "plan_before_execution": self.plan_before_execution,
                "resume_planning_record": str(self.resume_planning_record) if self.resume_planning_record is not None else None,
                "resume_planning": self._resume_planning,
            },
            "runner_pid": os.getpid(),
            "agents": agents,
            "edges": edges,
            "executions": executions,
            "readings": readings,
            "guidance": self.guidance.snapshot(),
            "messages": {
                "sent": runtime_report.get("messages_sent", 0),
                "processed": runtime_report.get("messages_processed", 0),
                "wakes": runtime_report.get("wakes", 0),
                "coalesced": runtime_report.get("coalesced", 0),
            },
            "tokens": {
                "input": hit + miss,
                "cached": hit,
                "cache_hit_rate": round(hit / (hit + miss), 4) if hit + miss else 0.0,
                "output": out,
                "reasoning": sum(c["reasoning_tokens"] for c in calls),
                "usage_missing_attempts": sum(c.get("usage_missing_attempts", 0) for c in self.calls),
                "usage_partial_attempts": sum(c.get("usage_partial_attempts", 0) for c in self.calls),
                "usage_incomplete_attempts": sum(c.get("usage_incomplete_attempts", 0) for c in self.calls),
                "usage_complete": bool(self.calls) and all(c.get("usage_complete", False) for c in self.calls),
                "cache_usage_complete": bool(self.calls) and all(c.get("cache_usage_complete", False) for c in self.calls),
                "cache_usage_incomplete_attempts": sum(c.get("cache_usage_incomplete_attempts", 0) for c in self.calls),
                "cache_usage_source": next(iter(cache_sources)) if len(cache_sources) == 1 else "mixed",
                "cost_estimate": round(hit * price["hit"] + miss * price["miss"] + out * price["out"], 4),
            },
            "calls": calls,
            "pending_calls": [
                {key: value for key, value in entry.items() if key not in ("t0", "last_progress_t")}
                for entry in sorted(self._pending.values(), key=lambda item: str(item.get("started_at")))
            ],
            "pending_tools": [
                {key: value for key, value in entry.items() if key != "t0"}
                for entry in sorted(self._pending_tools.values(), key=lambda item: str(item.get("started_at")))
            ],
            "workspace": str(self.workspace),
            "workspace_diff": self._workspace_diff,
            "wall": {
                "budget_s": self.wall_budget_s,
                "analyst_reserve_frac": self.analyst_reserve_frac,
                "analyst_deadline_hit": self._analyst_deadline_hit,
                "forced_report": self._forced_report,
            },
            "plan": self._plan,
            "root_stage": self._root_stage.get("root", ""),
            "reviews": self._review_log,
            "review_round": self._review_round,
            "audits": self._audits[-80:],
            "message_previews": [
                {
                    "kind": "answer",
                    "subject": str(item["subject"])[:120],
                    "chars": len(str(item["body"])),
                    "preview": str(item["body"])[:800],
                }
                for item in self._answers.values()
            ]
            + [
                {
                    "kind": "note",
                    "subject": str(item["subject"])[:120],
                    "chars": len(str(item["body"])),
                    "preview": str(item["body"])[:800],
                }
                for item in self._notes.values()
            ],
            "tools": self._tools_status(),
            "events": list(runtime_report.get("events") or [])[-80:],
            "wait_graph": runtime_report.get("wait_graph") or {},
            "checks": {},
            "judge": {},
            "report_chars": len(self._report),
            "report": self._report,
            "error": self.failed_reason
            or ("wall-clock limit reached" if getattr(self, "_timed_out", False) else ""),
        }
        external = getattr(self, "external_worker", None)
        if external:
            snapshot.update(external)
            snapshot["mode"] = "external-worker"
            if external["worker_role"] == "analyst":
                snapshot["agents"].insert(0, {
                    "agent_id": external["worker_agent_id"], "parent_id": "dsh-root", "role": "analyst",
                    "status": external["status"], "objective": self.goal, "children": [], "depth": 1,
                })
                snapshot["edges"].insert(0, {"from": "dsh-root", "to": external["worker_agent_id"], "kind": "supervises"})
        (self.out_dir / "status.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-output-tokens", type=int, default=None, help="legacy argument accepted for compatibility; output remains unrestricted")
    parser.add_argument("--max-steps", type=int, default=1_000_000_000)
    parser.add_argument(
        "--workspace",
        default=str(PROJECT),
        help="root directory for read_file / list_files / search_files / run_command",
    )
    parser.add_argument("--no-tools", action="store_true", help="disable all agent tools")
    parser.add_argument("--no-web", action="store_true", help="disable web_search / web_fetch")
    parser.add_argument("--no-run", action="store_true", help="disable run_command")
    parser.add_argument("--enable-guidance", action="store_true", help="allow parents to guide their active direct workers")
    parser.add_argument(
        "--max-tool-rounds",
        type=int,
        default=16,
        help="每个 Agent 每阶段最多几轮工具调用（0 = 不设上限，会一直查下去；默认 16，最后一轮强制收尾）",
    )
    parser.add_argument("--retry-attempts", type=int, default=5)
    parser.add_argument("--unlimited-executor-rounds", action="store_true", help="explicitly remove executor round and shared call ceilings; retain wall deadline")
    parser.add_argument("--resume-execution", help="same-case executor JSONL or durable checkpoint to continue without replaying completed tools")
    parser.add_argument("--resume-next-step-plan", action="store_true", help="one high-controller next-step plan for this continuation, sharing its wall budget")
    parser.add_argument("--plan-before-execution", action="store_true", help="one controller plan for a fresh direct Git code task before execution; no continuation inputs")
    parser.add_argument("--resume-planning-record", help="verified same-case paid planner-only attempt to reuse without another high request")
    parser.add_argument("--thinking-reasoning-effort", choices=["low", "high"], default=None)
    parser.add_argument("--executor-reasoning-effort", choices=["low", "high"], default=None)
    parser.add_argument("--reader-reasoning-effort", choices=["low", "high"], default=None)
    parser.add_argument("--executor-context-budget-tokens", type=int, default=None,
                        help="local soft context budget; defaults to 65536, or 262144 for explicit unlimited execution")
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument(
        "--max-review-rounds",
        type=int,
        default=4,
        help="Root 复盘后最多再补派几轮（0 = 由 Root 自己决定，不设上限）",
    )
    parser.add_argument(
        "--max-finish-rounds",
        type=int,
        default=int(os.environ.get("HCCA_MAX_FINISH_ROUNDS", "2")),
        help=(
            "子 Agent 交付被验收为「未完成」时，最多再自动补做几轮才允许提交"
            "（0 = 旧行为：交付什么就提交什么）"
        ),
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=int(os.environ.get("HCCA_MAX_WORKERS", "16")),
        help="同一时刻并行执行的 Agent 步数（默认 16，可用 HCCA_MAX_WORKERS 覆盖）",
    )
    parser.add_argument(
        "--max-wall-seconds",
        type=int,
        default=0,
        help="hard wall-clock budget for the whole run (a single hung call is bounded by HCCA_LLM_TIMEOUT)",
    )
    parser.add_argument(
        "--force-mode",
        choices=["", "solo", "team"],
        default=os.environ.get("HCCA_FORCE_MODE", ""),
        help="experiment control: force the planner into solo/team (empty = model decides)",
    )
    parser.add_argument(
        "--readonly-analysts",
        action="store_true",
        help="tell every planned sub-Agent to stay read-only (Root keeps write access); "
        "use with --force-mode team on a single shared code checkout",
    )
    parser.add_argument(
        "--task-kind",
        choices=["research", "code"],
        default=os.environ.get("HCCA_TASK_KIND", "research"),
        help="code swaps system prompts to 'edit the repo and run tests' instead of 'write a report'",
    )
    parser.add_argument(
        "--no-review-waves",
        action="store_true",
        help="experiment control: end the review stage after the first wave "
        "(keeps a fixed wall budget for implement/verify)",
    )
    parser.add_argument(
        "--analyst-reserve-frac",
        type=float,
        default=float(os.environ.get("HCCA_ANALYST_RESERVE_FRAC", "0") or 0),
        help=(
            "fraction of --max-wall-seconds reserved for the Root's own report/"
            "implementation phase; analysts + review must finish before the rest "
            "is used up (0 = off). Fixes the SWE-bench failure mode where analysts "
            "and audit rounds ate the whole budget and the Root never wrote code."
        ),
    )
    args = parser.parse_args()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    llm = load_llm_from_env(None)
    if hasattr(llm, "timeout"):
        try:
            llm.timeout = max(60.0, min(float(os.environ.get("HCCA_LLM_TIMEOUT", "240")), 900.0))
        except ValueError:
            llm.timeout = 240.0
    if hasattr(llm, "idle_timeout"):
        # Stalled stream = no real model output (reasoning/content/tool deltas)
        # for this long; keep-alive comments from the gateway do not count.
        try:
            llm.idle_timeout = max(30.0, min(float(os.environ.get("HCCA_LLM_IDLE_TIMEOUT", "240")), 3600.0))
        except ValueError:
            llm.idle_timeout = 240.0
    if hasattr(llm, "call_timeout"):
        # Absolute backstop for a stream that never stops (0 = no cap).
        try:
            raw_call_timeout = float(os.environ.get("HCCA_LLM_CALL_TIMEOUT", "0"))
        except ValueError:
            raw_call_timeout = 0.0
        llm.call_timeout = (
            0.0 if raw_call_timeout <= 0 else max(300.0, min(raw_call_timeout, 86400.0))
        )
    toolbox = None
    if not args.no_tools:
        toolbox = AgentToolbox(
            args.workspace,
            allow_web=not args.no_web,
            allow_run=not args.no_run,
        )
    runner = GenericRun(
        out_dir=out_dir,
        llm=llm,
        max_output=args.max_output_tokens,
        goal=args.goal,
        workspace=args.workspace,
        toolbox=toolbox,
        max_tool_rounds=args.max_tool_rounds,
        retry_attempts=max(5, args.retry_attempts),
        retry_delay=args.retry_delay,
        max_review_rounds=args.max_review_rounds,
        max_finish_rounds=args.max_finish_rounds,
        force_mode=args.force_mode,
        readonly_analysts=args.readonly_analysts,
        task_kind=args.task_kind,
        no_review_waves=args.no_review_waves,
        wall_budget_s=args.max_wall_seconds,
        analyst_reserve_frac=args.analyst_reserve_frac,
        unlimited_executor_rounds=args.unlimited_executor_rounds,
        resume_execution=args.resume_execution,
        thinking_reasoning_effort=args.thinking_reasoning_effort,
        executor_reasoning_effort=args.executor_reasoning_effort,
        reader_reasoning_effort=args.reader_reasoning_effort,
        executor_context_budget_tokens=args.executor_context_budget_tokens,
        resume_next_step_plan=args.resume_next_step_plan,
        plan_before_execution=args.plan_before_execution,
        resume_planning_record=args.resume_planning_record,
        enable_guidance=args.enable_guidance,
    )
    store = CollaborationStore(clock=_now)
    supervisor = Supervisor(
        SupervisorConfig(
            max_children_per_agent=1_000_000,
            max_depth=1_000_000,
            max_active=1_000_000,
            max_total_agents=1_000_000,
            default_budget={"tokens": 1_000_000_000, "calls": 1_000_000},
        ),
        store=store,
    )
    scheduler = Scheduler(SchedulerConfig(max_concurrent=1_000_000, max_steps=args.max_steps))
    runner.supervisor = supervisor
    runtime = CollabRuntime(runner.step_runner, store, supervisor, scheduler)
    runner.runtime = runtime
    goal = GoalSpec(goal_id=f"hcca-{uuid.uuid4().hex[:8]}", objective=args.goal)
    runner._write_status()
    runtime.start(goal)
    runner.start_heartbeat()
    deadline = time.time() + args.max_wall_seconds if args.max_wall_seconds > 0 else None
    try:
        while not runtime._finished:
            if deadline is not None and time.time() > deadline:
                runner._timed_out = True  # type: ignore[attr-defined]
                break
            # A large worker pool removes the old hidden "3 at a time" cap:
            # with it, a 4th sub-Agent stayed CREATED for minutes while Root
            # was waiting on ALL_REQUIRED (a real dead-looking run).
            if not runtime.run_parallel_step(max_workers=max(1, int(args.max_workers))):
                break
    finally:
        runner.stop_heartbeat()
    # All model calls failed (e.g. bad key / TLS / network): report honestly
    # instead of a misleading COMPLETED with an empty report.
    failed = [call["error"] for call in runner.calls if call.get("error")]
    if runner.calls and len(failed) == len(runner.calls):
        runner.failed_reason = f"LLM 调用全部失败：{failed[0]}"
    report = runner._report or (f"（未生成报告）\n\n{runner.failed_reason}" if runner.failed_reason else "（未生成报告）")
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    result = runtime.report()
    if runner.failed_reason:
        result["status"] = "FAILED"
    runner.capture_workspace_diff()
    runner._finish = True  # type: ignore[attr-defined]
    # Final status: use runtime status, keep report.
    runner._write_status()
    print(f"[generic] status={result.get('status')} steps={result.get('steps')} agents={result.get('agents')} "
          f"calls={len(runner.calls)} tools={len(runner.tool_log)} report_chars={len(report)}")
    if runner._workspace_diff is not None:
        print(f"[generic] workspace_diff={runner._workspace_diff}")
    if runner.failed_reason:
        print(f"[generic] error: {runner.failed_reason}")
    print(f"report: {out_dir / 'report.md'}")
    return 0 if result.get("status") == "COMPLETED" else 1


def main() -> int:
    """Run the job and, if it dies, leave a readable reason in status.json.

    A killed/crashed runner used to leave the last snapshot saying RUNNING and
    ``delegate.log`` empty, so the only clue was "进程已退出".  Merging the
    traceback into the existing snapshot makes the failure diagnosable from the
    HCCA tab and from the run directory.
    """
    import traceback

    try:
        return _run_main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - last-resort crash record
        traceback.print_exc()
        try:
            argv = sys.argv
            out_dir = Path(argv[argv.index("--out") + 1]).resolve()
            status_path = out_dir / "status.json"
            snapshot: dict[str, Any] = {}
            if status_path.exists():
                try:
                    snapshot = json.loads(status_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    snapshot = {}
            snapshot["status"] = "FAILED"
            snapshot["error"] = f"runner crashed: {type(exc).__name__}: {exc}"
            snapshot["updated_at"] = _now()
            status_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001 - never mask the original crash
            pass
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
