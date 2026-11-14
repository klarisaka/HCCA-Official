"""Restore execution state without replaying completed workspace operations."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .reporting import REPORT_FINAL_INSTRUCTION

CONTINUE_INSTRUCTION = (
    "本次已授权继续本题。此前因轮数限制产生的立即提交报告、禁止工具的收尾指令已失效，"
    "旧受限终态不代表本次任务已完成。现在可继续使用工具修复源码并验证原任务；"
    "在剩余时间内完成实际工作后再提交报告。旧容器临时文件和进程未恢复。"
)
SHELL_RESTART_INSTRUCTION = (
    "The persistent shell has restarted in the workspace directory. Its previous "
    "working directory, environment variables, and activated environments were not "
    "restored. Restore any required directory and environment before running commands."
)
SHELL_RESTART_FEEDBACK = (
    "ERROR: Shell restarted; this tool was not executed; restore cwd/environment and retry."
)
TOOL_UNAVAILABLE_ERROR = "ERROR: ValueError: tool is unavailable for this job"
TOOL_SELECTION_ERRORS = {
    TOOL_UNAVAILABLE_ERROR,
    "ERROR: ValueError: tool arguments must be an object",
    "ERROR: ValueError: paths must be relative to the workspace",
    "ERROR: ValueError: path escapes the workspace",
    "ERROR: ValueError: write_file is restricted to the named fix scope",
    "ERROR: ValueError: write_file is disabled for this job",
    "ERROR: ValueError: edit_file is restricted to the named fix scope",
    "ERROR: ValueError: edit_file is disabled for this job",
}


def command_exit_code(result: str, command: str | None = None) -> int | None:
    """Read the status header without interpreting command text or stdout."""
    if command is not None:
        header = f"命令：{command.strip()}\n"
        if result.startswith(header):
            result = result[len(header):]
    status = re.match(r"\A退出码[：:][ \t]*(-?\d+)(?:\s|$)", result)
    return int(status.group(1)) if status else None


def update_tool_errors(
    outstanding: set[str], selection_errors: set[str], name: str, ok: bool,
    command_id: int | None, selection_error: bool,
) -> None:
    """Recover rejected tool selections after a real tool succeeds."""
    if ok:
        outstanding.discard(name)
        if name != "submit_report":
            outstanding.difference_update(selection_errors)
            selection_errors.clear()
    elif name != "run_command" or type(command_id) is not int:
        if not selection_error:
            selection_errors.discard(name)
        elif name not in outstanding:
            selection_errors.add(name)
        outstanding.add(name)


def _selection_error(event: dict[str, Any]) -> bool:
    return (event.get("event") == "tool" and not event.get("ok") and event.get("command_id") is None
            and (event.get("tool_selection_error") is True or event.get("result") in TOOL_SELECTION_ERRORS))


def _native_tool_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Read old checkpoint feedback, including a partially completed batch."""
    messages = list(state.get("history", []))
    exchanges = state.get("exchanges") or []
    if exchanges:
        messages.append({"role": "assistant", "tool_calls": [pair[0] for pair in exchanges]})
        messages.extend(pair[1] for pair in exchanges)
    calls: dict[str, dict[str, Any]] = {}
    records = []
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                calls[str(call.get("id"))] = call.get("function", {})
        elif message.get("role") == "tool":
            function = calls.get(str(message.get("tool_call_id")), {})
            name = str(function.get("name", ""))
            if not name or name == "submit_report":
                continue
            result = str(message.get("content", ""))
            if result == SHELL_RESTART_FEEDBACK:
                continue
            command = re.match(r"command_id=(\d+)\n", result)
            command_id = int(command.group(1)) if command else None
            if command:
                result = result[command.end():]
            ok = not result.startswith("ERROR:")
            if name == "run_command":
                arguments = function.get("arguments") or {}
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except (TypeError, ValueError):
                        arguments = {}
                source = arguments.get("command") if isinstance(arguments, dict) else None
                exit_code = command_exit_code(result, source if isinstance(source, str) else None)
                ok = ok and command_id is not None and exit_code == 0
            records.append({"event": "tool", "tool": name, "ok": ok,
                            "command_id": command_id, "result": result})
    return records


def _restore_tool_selection_errors(state: dict[str, Any], records: list[dict[str, Any]]) -> None:
    complete_records = (bool(records) and records[0].get("event") == "job" and not state.get("resume_lineage")
                        and not any(event.get("event") == "resume" for event in records))
    saved_pending = set(state.get("tool_selection_errors", []))
    records = [event for event in records if event.get("event") == "tool"] + _native_tool_records(state)
    # Native history may have lost an earlier real failure of the same tool.
    selection_names = {
        str(event.get("tool", "")) for event in records if _selection_error(event)
        and (complete_records or event.get("tool") in saved_pending or event.get("result") == TOOL_UNAVAILABLE_ERROR)
    }
    ledger = _ledger(records)
    pending = set(ledger["tool_selection_errors"]) & selection_names
    observed_errors = set(ledger["outstanding_errors"])
    outstanding = set(state["outstanding_errors"]) - (selection_names - observed_errors)
    saved_pending -= selection_names | (observed_errors - pending)
    state["outstanding_errors"] = sorted(outstanding)
    state["tool_selection_errors"] = sorted(outstanding & (saved_pending | pending))


def expire_final_control(state: dict[str, Any]) -> None:
    """Remove only the exact runtime control in the trailing user-control block."""
    history = state.get("history", [])
    expected = {"role": "user", "content": REPORT_FINAL_INSTRUCTION + " Include status and summary."}
    for index in range(len(history) - 1, -1, -1):
        if history[index].get("role") != "user":
            break
        if history[index] == expected:
            message = history.pop(index)
            encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            state.setdefault("expired_scheduler_controls", []).append({
                "original_history_index": index, "original_message": message,
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "reason": "exact runtime final-round control superseded by authorized continuation",
            })
            break


def native_assistant(response: dict[str, Any]) -> dict[str, Any]:
    calls = []
    for index, call in enumerate(response.get("tool_calls") or []):
        arguments = call.get("arguments") or {}
        calls.append({"id": str(call.get("id") or f"report_{index}"), "type": "function", "function": {
            "name": str(call.get("name") or ""),
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
        }})
    message = {"role": "assistant", "content": str(response.get("text") or "")}
    if calls:
        message["tool_calls"] = calls
    if isinstance(response.get("reasoning_content"), str):
        message["reasoning_content"] = response["reasoning_content"]
    return message


def _restart_minimal_shell(state: dict[str, Any]) -> None:
    """Return unfinished native calls to the model after shell state is lost."""
    response = state.get("pending_response")
    calls = (response or {}).get("tool_calls") or []
    if not calls or (len(calls) == 1 and calls[0].get("name") == "submit_report"):
        return
    next_index = state.get("next_tool_index", 0)
    exchanges = state.get("exchanges") or []
    if type(next_index) is not int or not 0 <= next_index <= len(calls) or len(exchanges) != next_index:
        raise ValueError("cannot resume: pending tool outcomes are incomplete")
    assistant = native_assistant(response)
    messages = []
    for index, native in enumerate(assistant["tool_calls"]):
        if index < next_index:
            saved_call, saved_result = exchanges[index]
            assistant["tool_calls"][index] = copy.deepcopy(saved_call)
            messages.append(copy.deepcopy(saved_result))
        else:
            messages.append({"role": "tool", "tool_call_id": native["id"], "content": SHELL_RESTART_FEEDBACK})
    state["history"].extend([assistant, *messages])
    state.update(pending_response=None, inflight_tool=None, exchanges=[], next_tool_index=0,
                 next_round_index=state["next_round_index"] + 1, phase="ready")


def _ledger(records: list[dict[str, Any]]) -> dict[str, Any]:
    completed: list[dict[str, Any]] = []
    observed: list[dict[str, Any]] = []
    outstanding: set[str] = set()
    selection_errors: set[str] = set()
    failed_commands: dict[str, int] = {}
    command_results: dict[str, bool] = {}
    successful_tools = 0
    last_command_ok: bool | None = None
    for event in records:
        if event.get("event") != "tool":
            continue
        name, ok = str(event.get("tool", "")), bool(event.get("ok"))
        args = event.get("arguments") or event.get("args") or {}
        command_id = event.get("command_id")
        if name == "run_command" and type(command_id) is int:
            command = str(args.get("command") or "").strip()
            command_results[str(command_id)] = ok
            last_command_ok = ok
            failed_commands.pop(command, None)
            if not ok:
                failed_commands[command] = command_id
        update_tool_errors(outstanding, selection_errors, name, ok, command_id, _selection_error(event))
        if ok:
            successful_tools += 1
        result = str(event.get("result", ""))
        fact = result if len(result) <= 180 else result[:60] + " … " + result[-117:]
        action = {"tool": name, "args": event.get("args") or {}, "fact": fact}
        if type(command_id) is int:
            action["command_id"] = command_id
        observed = [*observed, action][-3:]
        if ok:
            completed = [*completed, action][-6:]
    return {
        "completed": completed, "observed": observed,
        "outstanding_errors": sorted(outstanding), "failed_commands": failed_commands,
        "tool_selection_errors": sorted(selection_errors),
        "command_results": command_results, "last_command_ok": last_command_ok,
        "successful_tools": successful_tools,
    }


def load_execution_resume(path: str | Path) -> dict[str, Any]:
    """Load a durable checkpoint or a legacy, side-effect-free final boundary.

    Legacy response records did not save raw content/reasoning. Consequently a
    response after the last full request is excluded only when it executed no
    tools. A possibly executed operation is never silently replayed.
    """
    path = Path(path).resolve()
    raw = path.read_bytes()
    records: list[dict[str, Any]] = []
    checkpoint_records: list[dict[str, Any]] = []
    continuation_facts: list[str] = []
    if path.suffix == ".json":
        state = json.loads(raw)
        if state.get("schema") != "hcca.execution.checkpoint.v1":
            raise ValueError("unsupported execution checkpoint")
        state = copy.deepcopy(state)
    else:
        for line in raw.decode("utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        checkpoints = [(i, r["state"]) for i, r in enumerate(records)
                       if r.get("event") == "checkpoint" and isinstance(r.get("state"), dict)]
        state = copy.deepcopy(checkpoints[-1][1]) if checkpoints else {}
        if checkpoints:
            checkpoint_records = records[:checkpoints[-1][0]]
        if state and state.get("inflight_tool"):
            raise ValueError("cannot resume: the last tool's outcome was not durably recorded")
        # Terminal reports have no workspace side effects; continue from their
        # original input rather than pretending an unrecorded response is exact.
        terminal = next((r for r in reversed(records) if r.get("event") == "final"), None)
        if terminal and terminal.get("status") == "completed":
            raise ValueError("completed execution does not need continuation")
        if terminal and state.get("pending_response"):
            response = state["pending_response"]
            calls = response.get("tool_calls") or []
            if len(calls) == 1 and calls[0].get("name") == "submit_report":
                expire_final_control(state)
                assistant = native_assistant(response)
                response_position = max((i for i, r in enumerate(records) if r.get("event") == "response"), default=-1)
                rejected = next((r for r in reversed(records[response_position + 1:]) if r.get("event") == "report_rejected"), None)
                feedback = ("ERROR: " + str(rejected["reason"])) if rejected else json.dumps({
                    "recorded_report_status": terminal.get("status"), "report_recorded": True,
                }, ensure_ascii=False)
                state["history"].extend([assistant, {"role": "tool", "tool_call_id": assistant["tool_calls"][0]["id"], "content": feedback}])
                state["pending_response"] = None
                state["next_tool_index"] = 0
                state["exchanges"] = []
                state["next_round_index"] += 1
                state["phase"] = "ready"
                state["terminal_report_restoration"] = {
                    "raw_content_and_reasoning": "preserved", "native_feedback_source": "recorded runtime report outcome",
                    "report_rejected": rejected is not None,
                }
        if not state:
            requests = [(i, r) for i, r in enumerate(records) if r.get("event") == "request"]
            job = next((r for r in records if r.get("event") == "job"), None)
            if not requests or job is None:
                raise ValueError("execution log has no restorable request and job envelope")
            position, request = requests[-1]
            if any(r.get("event") in ("tool", "tool_started") for r in records[position + 1:]):
                raise ValueError("legacy tail executed tools without a complete native response checkpoint")
            messages = copy.deepcopy(request.get("messages"))
            if not isinstance(messages, list) or len(messages) < 2:
                raise ValueError("execution request has no complete initial messages")
            envelope = {k: job[k] for k in (
                "environment", "workspace", "mode", "paths", "allow_write", "task", "acceptance",
            ) if k in job}
            tag = str(request.get("tag", ""))
            try:
                next_round = int(tag.rsplit("round", 1)[1])
            except (IndexError, ValueError):
                raise ValueError("execution request has no round identifier") from None
            state = {
                "schema": "hcca.execution.checkpoint.v1", "phase": "ready",
                "envelope": envelope, "initial": messages[:2], "history": messages[2:],
                "next_round_index": next_round, "pending_response": None,
                "next_tool_index": 0, "exchanges": [], "inflight_tool": None,
                **_ledger(records[:position]),
            }
            drops = [r for r in records[:position + 1] if r.get("event") == "request" and r.get("context_exchanges_dropped", 0) > 0]
            visible_commands = {
                int(match.group(1)) for message in messages if message.get("role") == "tool"
                for match in [re.match(r"command_id=(\d+)\n", str(message.get("content", "")))] if match
            }
            missing_ids = sorted(set(state["failed_commands"].values()) - visible_commands)
            for event in records[:position]:
                if event.get("event") != "tool" or event.get("command_id") not in missing_ids:
                    continue
                args = event.get("arguments") or {}
                command = str(args.get("command", ""))
                result = str(event.get("result", ""))
                header = "命令：" + command + "\n"
                body = result[len(header):] if result.startswith(header) else result
                exit_code = command_exit_code(result, command)
                code = str(exit_code) if exit_code is not None else "未记录"
                if len(command) <= 240 and "\n" not in command and " -c " not in command and "<<" not in command:
                    fact = f"command_id={event['command_id']} 原命令：{command}；退出码{code}。"
                else:
                    environment = command.splitlines()[0].split(" -c", 1)[0][:180]
                    errors = re.findall(r"\b[A-Za-z_]+(?:Error|Exception):[^\n]+", body)
                    fact = f"command_id={event['command_id']} 命令摘要（非原文）：{environment} 后执行内联复现检查；退出码{code}。"
                    if errors:
                        fact += errors[-1][:200] + "。"
                    temporary = re.search(r"\bcd\s+(/tmp/[^\s;&]+)", command)
                    if temporary:
                        fact += f"旧临时目录{temporary.group(1)}未恢复。"
                continuation_facts.append(fact)
            state["legacy_restore_limits"] = {
                "restored_boundary": "last_complete_request",
                "excluded_tail_response": True,
                "excluded_tail_raw_content_and_reasoning": "not persisted by legacy logger",
                "external_scratch_files": "not restored by the execution log",
                "context_drop_request_count": len(drops),
                "context_exchanges_dropped_total": sum(r["context_exchanges_dropped"] for r in drops),
                "unresolved_command_ids_missing_from_request": missing_ids,
            }
            state["resume_command_facts"] = continuation_facts
        before = next((r.get("snapshot") for r in records if r.get("event") == "workspace_before"), None)
        after = next((r.get("snapshot") for r in reversed(records) if r.get("event") == "workspace_after"), None)
        state.setdefault("workspace_before", before)
        if after is not None:
            state["workspace_after"] = after
        state["previous_model_requests"] = sum(r.get("event") == "request" for r in records)
    if state.get("inflight_tool"):
        raise ValueError("cannot resume: the last tool's outcome was not durably recorded")
    if state.get("schema") != "hcca.execution.checkpoint.v1":
        raise ValueError("unsupported execution checkpoint")
    _restore_tool_selection_errors(state, checkpoint_records)
    expire_final_control(state)
    if state.get("tool_profile") == "minimal":
        _restart_minimal_shell(state)
    state["provenance"] = {
        "source_path": str(path), "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_request_count": state.get("previous_model_requests"),
        "context_restoration": {k: v for k, v in state.get("legacy_restore_limits", {}).items()
                                if k.startswith("context_") or k == "unresolved_command_ids_missing_from_request"},
        "expired_scheduler_controls": state.get("expired_scheduler_controls", []),
        "terminal_report_restoration": state.get("terminal_report_restoration"),
        "parent_lineage": state.get("resume_lineage"),
    }
    if state.get("pending_response") is None:
        content = CONTINUE_INSTRUCTION
        if state.get("tool_profile") == "minimal":
            content += "\n" + SHELL_RESTART_INSTRUCTION
        if continuation_facts:
            content += "\n原历史已裁掉但完成判定仍保留的失败事实（仅补此次续行缺项）：\n" + "\n".join(continuation_facts)
        state["history"].append({"role": "user", "content": content})
    elif state.get("tool_profile") == "minimal":
        state["history"].append({"role": "user", "content": SHELL_RESTART_INSTRUCTION})
    return state
