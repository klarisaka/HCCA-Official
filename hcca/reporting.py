"""Native final-report schema and extraction shared by parent and workers."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

REPORT_TOOL_NAME = "submit_report"
REPORT_TOOL_CHOICE = "auto"
REPORT_FINAL_INSTRUCTION = "Call submit_report now using only the evidence already available. Do not call other tools."


def report_schema(execution: bool = False) -> dict[str, Any]:
    properties: dict[str, Any] = {"summary": {"type": "string"}}
    required = ["summary"]
    if execution:
        properties = {
            "status": {"type": "string", "enum": ["completed", "failed", "blocked"]},
            **properties,
        }
        required = ["status", "summary"]
    return {
        "type": "function",
        "function": {
            "name": REPORT_TOOL_NAME,
            "description": "Submit the final evidence-based report.",
            "parameters": {
                "type": "object", "properties": properties,
                "required": required, "additionalProperties": False,
            },
        },
    }


def submitted_report(calls: Sequence[Mapping[str, Any]], execution: bool = False) -> dict[str, Any] | None:
    reports = [call for call in calls if call.get("name") == REPORT_TOOL_NAME]
    if not reports:
        return None
    if len(calls) != 1:
        raise ValueError("submit_report must be the only tool call")
    arguments = reports[0].get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ValueError("submit_report arguments must be an object") from exc
    if not isinstance(arguments, Mapping):
        raise ValueError("submit_report arguments must be an object")
    summary = arguments.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("submit_report requires a nonempty summary")
    allowed = {"summary"}
    if execution:
        allowed.update({"status", "resolved_by"})
        status = arguments.get("status")
        if not isinstance(status, str) or status not in {"completed", "failed", "blocked"}:
            raise ValueError("submit_report requires a valid status")
        if "resolved_by" in arguments and not isinstance(arguments["resolved_by"], Mapping):
            raise ValueError("submit_report resolved_by must be an object")
    if set(arguments) - allowed:
        raise ValueError("submit_report contains unsupported fields")
    return {**arguments, "summary": summary.strip()}
