"""Independent, read-only workers that return concise source-grounded reports."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any
from uuid import uuid4

from .agent_tools import AgentToolbox
from .execution import ExecutionService, bounded_tool_messages
from .reporting import REPORT_FINAL_INSTRUCTION, REPORT_TOOL_CHOICE, report_schema, submitted_report
from .request_budget import BudgetExceeded, CallBudget

_READ_TOOLS = {"read_file", "list_files", "search_files", "read_execution_log"}
_BUNDLED_PROMPT = Path(__file__).with_name("prompts") / "reader.md"


def reader_prompt_path() -> Path:
    """Return the shared profile path without creating it."""
    configured = os.environ.get("HCCA_AGENT_HOME")
    agent_home = Path(configured).expanduser() if configured else Path.home() / ".hcca" / "agents"
    return agent_home / "reader.md"


class ReaderService:
    def __init__(
        self,
        workspace: Path,
        out_dir: Path,
        toolbox: AgentToolbox,
        complete: Callable[..., Any],
        max_rounds: int = 8,
        read_execution_log: Callable[[Mapping[str, Any]], str] | None = None,
        take_guidance: Callable[[str], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.out_dir = Path(out_dir).resolve()
        self.toolbox = toolbox
        self.complete = complete
        self.take_guidance = take_guidance
        self.max_rounds = max(1, int(max_rounds))
        self.read_execution_log = read_execution_log
        self.jobs: list[dict[str, Any]] = []
        self.tool_log: list[dict[str, Any]] = []

    @staticmethod
    def schema() -> dict[str, Any]:
        return AgentToolbox._fn(
            "delegate_reading",
            "Read workspace sources in a fresh context and return concise findings with file/line references and evidence gaps.",
            {
                "task": {"type": "string", "maxLength": 8000},
                "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            },
            ["task"],
        )

    def _path(self, value: str) -> Path:
        path = Path(value.replace("\\", "/"))
        if path.is_absolute() or PureWindowsPath(value).drive or PureWindowsPath(value).root:
            raise ValueError("paths must be relative to the workspace")
        candidate = (self.workspace / path).resolve()
        if not candidate.is_relative_to(self.workspace):
            raise ValueError("path escapes the workspace")
        return candidate

    def _envelope(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise ValueError("reading arguments must be an object")
        task = arguments.get("task")
        if not isinstance(task, str) or not task.strip() or len(task) > 8000:
            raise ValueError("task must be a nonempty string of at most 8000 characters")
        paths = arguments.get("paths", [])
        if not isinstance(paths, list) or len(paths) > 20:
            raise ValueError("paths must be an array of at most 20 relative paths")
        normalized: list[str] = []
        for value in paths:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("each path must be a nonempty relative path")
            normalized.append(self._path(value).relative_to(self.workspace).as_posix())
        return {"workspace": str(self.workspace), "paths": normalized, "task": task.strip()}

    def _arguments(self, name: str, arguments: Any) -> dict[str, Any]:
        if name not in _READ_TOOLS:
            raise ValueError("tool is unavailable for this read-only job")
        if not isinstance(arguments, Mapping):
            raise ValueError("tool arguments must be an object")
        args = dict(arguments)
        if name == "read_execution_log":
            return args
        path = args.get("path", "" if name == "read_file" else ".")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("a relative path is required")
        self._path(path)
        if name == "search_files" and (not isinstance(args.get("pattern"), str) or not args["pattern"].strip()):
            raise ValueError("a nonempty search pattern is required")
        if name == "list_files" and args.get("pattern"):
            pattern = args["pattern"]
            if not isinstance(pattern, str):
                raise ValueError("file pattern must be a string")
            glob_path = PureWindowsPath(pattern)
            if glob_path.drive or glob_path.root or ".." in glob_path.parts:
                raise ValueError("file pattern escapes the workspace")
        args["path"] = path
        if name == "read_file":
            args.setdefault("start_line", 1)
        return args

    def run(self, arguments: Mapping[str, Any], *, requester_id: str, budget: CallBudget | None = None,
            agent_id: str | None = None) -> str:
        job_id = uuid4().hex
        agent_id = agent_id or f"reading_{job_id}"
        log_path = self.out_dir / "readings" / f"{job_id}.jsonl"
        job: dict[str, Any] = {
            "job_id": job_id,
            "agent_id": agent_id,
            "requester_id": requester_id,
            "task": str(arguments.get("task", ""))[:8000] if isinstance(arguments, Mapping) else "",
            "status": "running",
            "log_path": str(log_path),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        self.jobs.append(job)
        started = time.monotonic()
        status, summary = "failed", "Reading failed. See the reading log."
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("w", encoding="utf-8") as log:
                def record(event: str, **data: Any) -> None:
                    log.write(
                        json.dumps({"event": event, **data}, ensure_ascii=False, default=str) + "\n"
                    )
                    log.flush()

                try:
                    envelope = self._envelope(arguments)
                except (ValueError, TypeError, OSError) as exc:
                    status, summary = "blocked", str(exc)
                    record("rejected", reason=summary)
                else:
                    job["paths"] = envelope["paths"]
                    record("job", agent_id=agent_id, requester_id=requester_id, **envelope)
                    try:
                        status, summary = self._work(envelope, agent_id, job_id, record, budget)
                    except Exception as exc:
                        record("worker_error", error=f"{type(exc).__name__}: {exc}")
                        status, summary = "failed", "Reader failed. See the reading log."
                record("final", status=status, summary=summary)
        except OSError:
            status, summary = "failed", "Could not write the reading log."
        summary = summary[:1200]
        job.update(
            status=status,
            summary=summary,
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration_s=round(time.monotonic() - started, 3),
        )
        return json.dumps(
            {"job_id": job_id, "status": status, "summary": summary, "log_path": str(log_path)},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _work(
        self,
        envelope: dict[str, Any],
        agent_id: str,
        job_id: str,
        record: Callable[..., None],
        budget: CallBudget | None = None,
    ) -> tuple[str, str]:
        profile = reader_prompt_path()
        source = profile if profile.is_file() else _BUNDLED_PROMPT
        prompt = source.read_text(encoding="utf-8").strip()
        record("configuration", prompt_path=str(source), prompt=prompt)
        initial = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(envelope, ensure_ascii=False)},
        ]
        worker = AgentToolbox(
            self.workspace,
            timeout=self.toolbox.timeout,
            max_chars=self.toolbox.max_chars,
            allow_web=False,
            allow_run=False,
        )
        tools = [tool for tool in worker.schemas() if tool["function"]["name"] in _READ_TOOLS]
        tools.append(ExecutionService.log_schema())
        tools.append(report_schema())
        history: list[dict[str, Any]] = []
        completed: list[dict[str, Any]] = []
        gaps: dict[str, str] = {}
        successes = 0

        def cutoff(reason: str) -> tuple[str, str]:
            evidence = [
                f"{item['tool']} {json.dumps(item['args'], ensure_ascii=False)}: {item['fact']}"
                for item in reversed(completed[-3:])
            ]
            evidence.extend(list(gaps.values())[-3:])
            summary = f"Reading budget ended: {reason}. " + (" | ".join(evidence) if evidence else "No source was read.")
            record("budget_cutoff", reason=reason, completed=completed, reading_gaps=list(gaps.values()))
            return "blocked", summary

        def failed(summary: str) -> tuple[str, str]:
            if budget is not None:
                try:
                    budget.check()
                except BudgetExceeded as exc:
                    return cutoff(str(exc))
            return "failed", summary

        def report_failure(reason: str) -> tuple[str, str]:
            evidence = [f"{item['tool']}: {item['fact']}" for item in reversed(completed[-3:])]
            evidence.extend(list(gaps.values())[-3:])
            record("report_rejected", reason=reason, completed=completed, reading_gaps=list(gaps.values()))
            return failed(reason + (" Evidence: " + " | ".join(evidence) if evidence else ""))

        for round_index in range(self.max_rounds):
            if budget is not None:
                try:
                    budget.check()
                except BudgetExceeded as exc:
                    return cutoff(str(exc))
            final_round = round_index == self.max_rounds - 1 or (budget is not None and budget.remaining <= 1)
            if self.take_guidance is not None:
                history.extend(self.take_guidance(agent_id))
            if final_round:
                history.append({"role": "user", "content": REPORT_FINAL_INSTRUCTION})
            messages, context_metadata = bounded_tool_messages(initial, history, tools)
            tag = f"reader:{job_id}:round{round_index + 1}"
            record("request", tag=tag, messages=messages, **context_metadata)
            try:
                options = {
                    "budget": budget,
                    "max_output_tokens": None,
                } if budget is not None else {}
                if budget is not None and final_round:
                    options["tool_choice"] = REPORT_TOOL_CHOICE
                response = self.complete(
                    agent_id=agent_id,
                    messages=messages,
                    tag=tag,
                    tools=tools,
                    **options,
                )
            except BudgetExceeded as exc:
                return cutoff(str(exc))
            except Exception as exc:
                record("model_error", error=f"{type(exc).__name__}: {exc}")
                return failed("Reader model call failed. See the reading log.")
            if response is None:
                record("model_error", error="Model returned no response")
                return failed("Reader model call failed. See the reading log.")
            response_text = str(getattr(response, "text", "") or "")
            text = response_text.strip()
            calls = list(getattr(response, "tool_calls", None) or [])
            record("response", tag=tag, text=text, tool_calls=calls)
            try:
                submission = submitted_report(calls)
            except ValueError as exc:
                return report_failure(f"Reader rejected submit_report: {exc}")
            if submission is not None:
                record("report_submission", report=submission)
                text = submission["summary"]
                calls = []
            if not calls:
                if not text:
                    return failed("Reader returned no report. See the reading log.")
                notes = list(gaps.values())[-3:]
                if len(gaps) > 3:
                    notes.append(f"另有 {len(gaps) - 3} 项阅读提示，见日志。")
                suffix = "\n阅读提示\n" + "\n".join(notes) if notes else ""
                summary = text[:max(0, 1200 - len(suffix))] + suffix
                if successes:
                    return "completed", summary
                return report_failure(summary) if submission is not None else failed(summary)
            if final_round:
                if budget is not None:
                    return cutoff("final report returned other tool calls")
                return failed(f"Reader reached its {self.max_rounds}-round limit without a report.")
            exchanges: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for index, call in enumerate(calls):
                if budget is not None:
                    try:
                        budget.check()
                    except BudgetExceeded as exc:
                        return cutoff(str(exc))
                name = str(call.get("name") or "")
                call_id = str(call.get("id") or f"reading_call_{round_index}_{index}")
                arguments = call.get("arguments") or {}
                tool_started = time.monotonic()
                before = len(worker.log)
                try:
                    args = self._arguments(name, arguments)
                    if name == "read_execution_log":
                        if self.read_execution_log is None:
                            raise ValueError("execution log reader unavailable")
                        result = self.read_execution_log(args)
                    else:
                        result = worker.call(name, args)
                except Exception as exc:
                    result = f"ERROR: {type(exc).__name__}: {exc}"
                ok = not result.startswith("ERROR:")
                if len(worker.log) > before:
                    ok = ok and worker.log[-1]["ok"]
                audit = {
                    "tool": name,
                    "ok": bool(ok),
                    "chars": len(result),
                    "duration_ms": int((time.monotonic() - tool_started) * 1000),
                    "args": {
                        str(key): str(value)[:120] for key, value in arguments.items()
                    } if isinstance(arguments, Mapping) else {},
                    "agent_id": agent_id,
                    "job_id": job_id,
                    "tag": tag,
                    "tool_call_id": call_id,
                }
                self.tool_log.append(audit)
                record("tool", **audit, arguments=arguments, result=result)
                identity = name + ":" + json.dumps(
                    arguments, ensure_ascii=False, sort_keys=True, default=str
                )
                target_key = "job_id" if name == "read_execution_log" else "path"
                target = str(audit["args"].get(target_key, name))
                content = result
                omitted = "已截断" in content or "[Output truncated]" in content
                if not ok:
                    gaps[identity] = f"未读 {target[:100]}，{result[:120]}"
                elif omitted:
                    gaps[identity] = f"截断 {target[:100]}，完整工具结果见阅读日志；部分内容尚未进入阅读上下文。"
                else:
                    gaps.pop(identity, None)
                if ok:
                    successes += 1
                    fact = content if len(content) <= 180 else content[:60] + " … " + content[-117:]
                    completed.append({"tool": name, "args": audit["args"], "fact": fact})
                    completed = completed[-6:]
                native = {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
                exchanges.append((
                    native,
                    {"role": "tool", "tool_call_id": call_id, "content": content},
                ))
            assistant = {"role": "assistant", "content": response_text, "tool_calls": [item[0] for item in exchanges]}
            reasoning = getattr(response, "reasoning_content", None)
            if isinstance(reasoning, str):
                assistant["reasoning_content"] = reasoning
            history.extend([assistant, *(item[1] for item in exchanges)])
        return "failed", "Reader exhausted its round budget without a report."
