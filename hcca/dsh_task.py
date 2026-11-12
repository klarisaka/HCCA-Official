"""Run one worker for the DSH chat Root, without an internal HCCA Root."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

from .runner import GenericRun
from .agent_tools import AgentToolbox
from .llm import load_llm_from_env
from .request_budget import CallBudget


def run_task(request: dict, *, llm=None) -> dict:
    role = request.get("role", "analyst")
    if role not in {"executor", "reader", "analyst"}:
        raise ValueError("role must be executor, reader, or analyst")
    workspace = Path(request["workspace"]).resolve()
    output = Path(request["out"]).resolve()
    objective = request["objective"]
    wall = max(1, float(request.get("max_wall_seconds") or 10800))
    # Construct only the shared model/tool recorder. No scheduler, supervisor,
    # root planner, review, editor, or final-report model call is started.
    runner = GenericRun(
        out_dir=output, llm=llm if llm is not None else load_llm_from_env(), goal=objective,
        workspace=workspace, toolbox=AgentToolbox(workspace, allow_web=True),
        max_tool_rounds=0, readonly_analysts=True, retry_attempts=5,
        thinking_reasoning_effort="high", executor_reasoning_effort="low", reader_reasoning_effort="low",
        executor_context_budget_tokens=524288,
        enable_guidance=True,
        wall_budget_s=int(wall),
    )
    runner.executor.max_rounds = 0
    identifier = request.get("worker_agent_id") or {"analyst": "analyst_", "reader": "reading_", "executor": "execution_"}[role] + uuid4().hex
    runner.external_worker = {"external_root": True, "root_label": "DSH Root", "external_root_id": "dsh-root",
                              "worker_role": role, "worker_agent_id": identifier, "status": "RUNNING"}
    budget = CallBudget(None, runner.started + wall)
    result = {"status": "failed", "summary": "", "role": role}
    runner._write_status()
    runner.start_heartbeat()
    try:
        arguments = {"task": objective, "paths": request.get("paths", ["."])}
        if role == "executor":
            arguments.update(mode=request.get("mode", "fix"), acceptance=request.get("acceptance", ""))
            capture = runner.capture_workspace_diff if arguments["mode"] == "fix" and runner.capture_workspace_diff() is not None else None
            result = json.loads(runner.executor.run(arguments, requester_id="dsh-root", budget=budget,
                                                    capture_workspace_diff=capture, agent_id=identifier))
        elif role == "reader":
            result = json.loads(runner.reader.run(arguments, requester_id="dsh-root", budget=budget, agent_id=identifier))
        else:
            text = runner._call_with_tools(
                agent_id=identifier,
                system="你是 HCCA 调查 Agent。完成 DSH Root 下达的只读调查，按需委派阅读或只读命令，返回结果与未完成项。",
                user=objective, tag="analyst-task", max_rounds=0, budget=budget,
            )
            failed = bool(runner.calls) and all(call.get("error") for call in runner.calls)
            status = "failed" if failed or not text.strip() else "blocked" if "【交付未完成】" in text else "completed"
            result = {"status": status, "summary": text}
        runner._report = str(result.get("summary") or "")
        status = str(result.get("status") or "failed").upper()
        runner.external_worker["status"] = "INCOMPLETE" if status == "BLOCKED" else status
        if status in {"FAILED", "BLOCKED"}:
            runner.failed_reason = runner._report
        result.update(role=role, external_root=True)
        (output / "worker-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "report.md").write_text(runner._report, encoding="utf-8")
        return result
    except Exception as error:
        runner.failed_reason = f"{type(error).__name__}: {error}"
        runner.external_worker["status"] = "FAILED"
        raise
    finally:
        runner.stop_heartbeat()
        runner._write_status()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    result = run_task(json.loads(args.request.read_text(encoding="utf-8")))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
