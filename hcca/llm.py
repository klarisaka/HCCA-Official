"""Model boundary: a deterministic scripted model plus an optional real adapter.

The prototype must run offline and reproducibly.  ``ScriptedLLM`` simulates a
competent but strictly context-bound assistant:

* it only reasons over evidence that is visible in the delivered context;
* it knows generic repair patterns (``ModelWorld.issue_fixes``) and canonical
  fact statements, supplied by the evaluation fixture;
* when it lacks necessary evidence it returns ``NEED_CONTEXT`` instead of
  guessing.

That makes the interesting variable the *context architecture*, not the model.
``OpenAICompatLLM`` is a thin optional adapter so the same orchestration can be
pointed at a real chat-completions endpoint.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from .errors import ModelError
from .tokens import estimate_tokens
from .world import ModelWorld

_CONTEXT_RE = re.compile(r"<CONTEXT>(.*?)</CONTEXT>", re.DOTALL)
_REQUEST_RE = re.compile(r"<REQUEST>\s*(\{.*\})\s*</REQUEST>", re.DOTALL)
_HEADER_RE = re.compile(
    r"^\[CTX kind=(?P<kind>\w+)(?: ref=(?P<ref>[^\s\]]+))? "
    r"priority=(?P<priority>\d+) tokens=(?P<tokens>\d+)\]$"
)
_FACT_IDS_RE = re.compile(r"FACT_IDS:\s*([^\n]*)")
_ISSUE_CODES_RE = re.compile(r"ISSUE_CODES:\s*([^\n]*)")
_UNCERTAINTIES_RE = re.compile(r"UNCERTAINTIES:\n((?:\s+- .*\n?)+)")
_CONDITION_RE = re.compile(
    r"^\s+(requires|not_established|depends_on):\s*(.+)$", re.MULTILINE
)
_COVERAGE_RE = re.compile(r"NOT_SEARCHED=([^|\n]+)")
_CONTINUATION_FIELDS = (
    "ESTABLISHED",
    "UNRESOLVED",
    "REJECTED",
    "REQUIRED_CHECKS",
    "REFERENCES",
)


def _http_error_metadata(exc: Exception, api_key: str) -> dict[str, Any]:
    """Persist the response error, without request headers or credentials."""
    if not isinstance(exc, urllib.error.HTTPError):
        return {}
    metadata: dict[str, Any] = {"http_status": exc.code}
    try:
        raw = exc.read()
    except Exception as read_error:  # diagnostics must not hide the HTTP failure
        metadata["http_error_body_read_error"] = type(read_error).__name__
        return metadata
    text = raw.decode("utf-8", "replace")
    redacted = bool(api_key and api_key in text)
    if redacted:
        text = text.replace(api_key, "[REDACTED_API_KEY]")
    metadata.update({
        "http_error_body": text,
        "http_error_body_bytes": len(raw),
        "http_error_body_sha256": hashlib.sha256(raw).hexdigest(),
        "http_error_body_redacted": redacted,
    })
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return metadata
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        metadata["http_error_fields"] = {
            name: error[name] for name in ("message", "type", "param", "code") if name in error
        }
    return metadata


def _wire_messages(messages: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Omit only an empty optional operation list; keep native history intact."""
    wire = [dict(message) for message in messages]
    changes = []
    for index, message in enumerate(wire):
        if message.get("role") != "assistant" or message.get("tool_calls") != []:
            continue
        original = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        del message["tool_calls"]
        canonical = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        changes.append({
            "message_index": index, "omitted_field": "tool_calls", "original_value": [],
            "original_message_sha256": hashlib.sha256(original).hexdigest(),
            "wire_message_sha256": hashlib.sha256(canonical).hexdigest(),
            "reason": "an assistant message without tool calls omits the optional operation list",
        })
    return wire, {"request_message_normalization_count": len(changes), "request_message_normalizations": changes}


def _collect_conditions(text: str) -> dict[str, list[str]]:
    collected: dict[str, list[str]] = {
        "requires": [],
        "not_established": [],
        "depends_on": [],
    }
    for key, raw in _CONDITION_RE.findall(text):
        for item in re.split(r"[;]", raw):
            value = item.strip().rstrip(",")
            if value and value.lower() not in {"(none)", "none"} and value not in collected[key]:
                collected[key].append(value)
    return collected


def _collect_coverage_gaps(text: str) -> list[str]:
    gaps: list[str] = []
    for raw in _COVERAGE_RE.findall(text):
        for item in raw.split(","):
            value = item.strip()
            if value and value.lower() not in {"(none)", "none"} and value not in gaps:
                gaps.append(value)
    return gaps


def _parse_continuation_text(text: str) -> dict[str, Any]:
    """Parse the resumable checkpoint rendered by ``ContinuationState``."""
    result: dict[str, Any] = {
        "goal": "",
        "next_step": "",
        "established": [],
        "unresolved": [],
        "rejected": [],
        "required_checks": [],
        "references": [],
    }
    current: str | None = None
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        upper = line.upper()
        if upper.startswith("GOAL:"):
            result["goal"] = line[5:].strip()
            current = None
            continue
        if upper.startswith("NEXT_STEP:"):
            result["next_step"] = line[10:].strip()
            current = None
            continue
        matched = False
        for field in _CONTINUATION_FIELDS:
            if upper == field + ":":
                current = field.lower()
                matched = True
                break
        if matched:
            continue
        if line.startswith("- ") and current:
            value = line[2:].strip()
            if value and value.lower() not in {"(none)", "none"}:
                result[current].append(value)
        elif line and not line.startswith("- "):
            current = None
    return result


def _cache_usage(usage: Mapping[str, Any]) -> dict[str, int]:
    """Normalize prompt-cache counters across OpenAI-compatible gateways.

    Official DeepSeek exposes ``prompt_cache_hit_tokens``/``..._miss_tokens`` at
    the top level, while vLLM-style gateways (gxwtf) put the hit count in
    ``prompt_tokens_details.cached_tokens`` and the cache population in
    ``prompt_tokens_details.created_cache_tokens``.
    """
    details = usage.get("prompt_tokens_details") or {}
    if not isinstance(details, Mapping):
        details = {}
    completion_details = usage.get("completion_tokens_details") or {}
    if not isinstance(completion_details, Mapping):
        completion_details = {}
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    hit = int(
        usage.get("prompt_cache_hit_tokens", details.get("cached_tokens", 0)) or 0
    )
    miss = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
    if not miss and prompt:
        miss = max(prompt - hit, 0)
    return {
        "prompt_tokens": prompt,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "cache_created_tokens": int(details.get("created_cache_tokens", 0) or 0),
        "reasoning_tokens": int(completion_details.get("reasoning_tokens", 0) or 0),
    }


def _set_response_read_timeout(response: Any, timeout: float) -> None:
    """Bound the socket behind urllib's HTTPResponse before a blocking read."""
    raw = getattr(getattr(response, "fp", None), "raw", None)
    connection = getattr(raw, "_sock", None)
    if connection is not None:
        connection.settimeout(timeout)


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    meta: dict[str, Any] = field(default_factory=dict)
    #: Normalised tool invocations: ``{"id", "name", "arguments": {...}}``.
    #: Native tool calling avoids asking the model to hand-write a JSON envelope.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    #: Opaque provider continuation required by native thinking-mode tool calls.
    reasoning_content: str | None = field(default=None, repr=False)


def _native_reasoning_content(message: Mapping[str, Any]) -> str | None:
    empty = None
    for field_name in ("reasoning_content", "reasoning"):
        value = message.get(field_name)
        if isinstance(value, str):
            if value:
                return value
            empty = ""
    return empty


def _stream_choice(choices: list[Any]) -> Any:
    if not choices:
        return {}
    first = choices[0] or {}
    if not isinstance(first, dict) or "index" not in first:
        return first
    for choice in choices:
        if isinstance(choice, dict) and choice.get("index") == 0:
            return choice
    return {}


class LLMClient(Protocol):
    name: str

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        seed: int | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse: ...


@dataclass(frozen=True)
class VisibleSection:
    kind: str
    ref: str | None
    text: str
    priority: int = 0

    def mentions(self, pattern: str) -> bool:
        return pattern.lower() in self.text.lower()


def split_prompt(user_text: str) -> tuple[str, dict[str, Any]]:
    """Split the gateway prompt into visible context text and request JSON."""
    context_match = _CONTEXT_RE.search(user_text)
    request_match = _REQUEST_RE.search(user_text)
    context_text = context_match.group(1) if context_match else user_text
    if not request_match:
        raise ModelError("prompt has no <REQUEST> block")
    try:
        request = json.loads(request_match.group(1))
    except json.JSONDecodeError as exc:
        raise ModelError(f"request JSON is invalid: {exc}") from exc
    if not isinstance(request, dict):
        raise ModelError("request must be a JSON object")
    return context_text, request


def parse_sections(context_text: str) -> list[VisibleSection]:
    """Parse the ``[CTX ...]`` headed sections back out of the rendered context."""
    sections: list[VisibleSection] = []
    current_kind: str | None = None
    current_ref: str | None = None
    current_priority = 0
    buffer: list[str] = []

    def flush() -> None:
        if current_kind is not None:
            sections.append(
                VisibleSection(
                    kind=current_kind,
                    ref=current_ref,
                    text="\n".join(buffer).strip(),
                    priority=current_priority,
                )
            )

    for line in context_text.split("\n"):
        match = _HEADER_RE.match(line.strip())
        if match:
            flush()
            current_kind = match.group("kind")
            current_ref = match.group("ref")
            current_priority = int(match.group("priority"))
            buffer = []
        else:
            buffer.append(line)
    flush()
    return sections


def _json_ids(raw: str) -> list[str]:
    return [
        token.strip().strip(",;")
        for token in re.split(r"[,\s]+", raw.strip())
        if token.strip().strip(",;")
    ]


class ScriptedLLM:
    """Deterministic, context-bound stand-in for a chat model."""

    name = "scripted"

    def __init__(self, world: ModelWorld | None = None, *, seed: int = 0) -> None:
        self.world = world
        self.seed = seed

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        seed: int | None = None,
    ) -> LLMResponse:
        prompt_text = "\n".join(str(m.get("content", "")) for m in messages)
        user_text = str(messages[-1].get("content", "")) if messages else prompt_text
        try:
            context_text, request = split_prompt(user_text)
            sections = parse_sections(context_text)
            envelope = self._dispatch(request, sections, context_text)
        except ModelError as exc:
            envelope = self._envelope("FAILED", summary="", error=str(exc))
        envelope.setdefault("task_id", "")
        text = json.dumps(envelope, ensure_ascii=False, indent=2)
        return LLMResponse(
            text=text,
            input_tokens=estimate_tokens(prompt_text),
            output_tokens=estimate_tokens(text),
            meta={
                "model": self.name,
                "capability": envelope.get("payload", {}).get("capability", ""),
            },
        )

    # -- capability dispatch ----------------------------------------------
    def _dispatch(
        self,
        request: Mapping[str, Any],
        sections: list[VisibleSection],
        context_text: str,
    ) -> dict[str, Any]:
        capability = str(request.get("capability", "analyze"))
        task_id = str(request.get("task_id", ""))
        handlers = {
            "plan": self._plan,
            "decompose": self._decompose,
            "analyze": self._analyze,
            "draft_patch": self._draft_patch,
            "verify": self._verify,
            "integrate": self._integrate,
            "resume": self._resume,
        }
        handler = handlers.get(capability)
        if handler is None:
            return self._envelope(
                "FAILED",
                summary=f"unsupported capability {capability!r}",
                error="unsupported capability",
            )
        try:
            envelope = handler(request, sections, context_text)
        except Exception as exc:  # the model must never crash the run loop
            return self._envelope(
                "FAILED",
                summary=f"scripted model error: {exc}",
                error=repr(exc),
            )
        envelope["task_id"] = task_id
        envelope.setdefault("payload", {})["capability"] = capability
        return envelope

    # -- helpers -----------------------------------------------------------
    def _envelope(
        self,
        status: str,
        *,
        summary: str = "",
        findings: Sequence[Mapping[str, Any]] = (),
        artifacts: Sequence[str] = (),
        uncertainties: Sequence[str] = (),
        requested_context: Sequence[str] = (),
        issue_codes: Sequence[str] = (),
        fact_ids: Sequence[str] = (),
        payload: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "task_id": "",
            "status": status,
            "summary": summary,
            "findings": [dict(f) for f in findings],
            "artifacts": list(artifacts),
            "uncertainties": list(uncertainties),
            "requested_context": list(requested_context),
            "issue_codes": list(issue_codes),
            "fact_ids": list(fact_ids),
            "payload": dict(payload or {}),
            "error": error,
        }

    @staticmethod
    def _visible_refs(sections: Sequence[VisibleSection]) -> set[str]:
        return {s.ref for s in sections if s.ref}

    def _match_refs(
        self, patterns: Sequence[str], sections: Sequence[VisibleSection]
    ) -> list[str]:
        refs: list[str] = []
        for pattern in patterns:
            for section in sections:
                if section.mentions(pattern):
                    if section.ref and section.ref not in refs:
                        refs.append(section.ref)
        return refs

    def _matched(
        self, patterns: Sequence[str], sections: Sequence[VisibleSection]
    ) -> bool:
        return any(section.mentions(p) for p in patterns for section in sections)

    # -- capabilities ------------------------------------------------------
    def _plan(
        self,
        request: Mapping[str, Any],
        sections: list[VisibleSection],
        context_text: str,
    ) -> dict[str, Any]:
        payload = dict(request.get("payload") or {})
        objective = str(request.get("objective", ""))
        candidates = [str(d) for d in (payload.get("candidate_domains") or [])]
        keywords = payload.get("domain_keywords") or {}
        complexity = str(payload.get("complexity", "complex"))
        matched: list[str] = []
        for domain in candidates:
            domain_words = [str(k) for k in (keywords.get(domain) or [])]
            if any(word.lower() in objective.lower() for word in domain_words):
                matched.append(domain)
        if complexity == "simple":
            split = False
            reason = "task classified as simple"
        elif len(matched) < 2:
            split = False
            reason = "fewer than two domains are implicated by the objective"
        else:
            split = True
            reason = f"objective implicates domains {matched}"
        domains = matched if split else (matched or candidates[:1])
        return self._envelope(
            "COMPLETED",
            summary=f"decomposition decision: split={split} ({reason})",
            payload={"split": split, "domains": domains, "reason": reason},
        )

    def _decompose(
        self,
        request: Mapping[str, Any],
        sections: list[VisibleSection],
        context_text: str,
    ) -> dict[str, Any]:
        payload = dict(request.get("payload") or {})
        context_tokens = int(payload.get("context_tokens", 0) or 0)
        budget = int(payload.get("budget", 1) or 1)
        independent = bool(payload.get("independent", False))
        split = independent and context_tokens > 0.7 * budget
        reason = (
            f"context {context_tokens} tokens vs budget {budget}, independent={independent}"
        )
        return self._envelope(
            "COMPLETED",
            summary=f"decompose decision: split={split}",
            payload={"split": split, "reason": reason},
        )

    def _analyze(
        self,
        request: Mapping[str, Any],
        sections: list[VisibleSection],
        context_text: str,
    ) -> dict[str, Any]:
        payload = dict(request.get("payload") or {})
        domain = str(request.get("domain") or payload.get("domain") or "")
        visible = self._visible_refs(sections)
        findings: list[dict[str, Any]] = []
        issue_codes: list[str] = []
        fact_ids: list[str] = []
        world = self.world
        if world is not None:
            for fix in world.fixes_for_domain(domain):
                matched_refs = self._match_refs(fix.trigger_patterns, sections)
                if not matched_refs:
                    continue
                if world.faults.get("omit_issue") == fix.issue_code:
                    continue  # simulated worker miss; verification can still catch it
                refs = () if world.faults.get("drop_evidence_refs") else tuple(matched_refs)
                findings.append(
                    {
                        "claim": fix.summary,
                        "evidence_refs": list(refs),
                        "fact_ids": list(fix.fact_ids),
                        "confidence": 0.85,
                        "requires": list(fix.requires),
                        "not_established": list(fix.not_established),
                        "depends_on": list(fix.depends_on),
                        "details": {"issue_code": fix.issue_code, "domain": fix.domain},
                    }
                )
                issue_codes.append(fix.issue_code)
                for fact_id in fix.fact_ids:
                    if fact_id not in fact_ids:
                        fact_ids.append(fact_id)
            for rule in world.evidence_rules:
                if rule.domain and rule.domain != domain:
                    continue
                matched_refs = self._match_refs(rule.contains, sections)
                if not matched_refs:
                    continue
                findings.append(
                    {
                        "claim": rule.claim,
                        "evidence_refs": list(matched_refs),
                        "fact_ids": list(rule.fact_ids),
                        "confidence": 0.7,
                        "details": {"rule_id": rule.rule_id},
                    }
                )
                for fact_id in rule.fact_ids:
                    if fact_id not in fact_ids:
                        fact_ids.append(fact_id)

            if not findings:
                fixes = world.fixes_for_domain(domain)
                missing: list[str] = []
                probe_ref = payload.get("probe_ref")
                if probe_ref and probe_ref not in visible:
                    missing.append(str(probe_ref))
                for fix in fixes:
                    missing.extend(r for r in fix.required_refs if r not in visible)
                missing = list(dict.fromkeys(missing))
                if missing:
                    return self._envelope(
                        "NEED_CONTEXT",
                        summary=f"{domain}: evidence is not visible; requesting context",
                        requested_context=missing,
                        uncertainties=["insufficient visible evidence"],
                        payload={"issue_codes": [], "facts": [], "domain": domain},
                    )
                return self._envelope(
                    "COMPLETED",
                    summary=f"{domain}: no confirmed issue in visible evidence",
                    uncertainties=["no trigger matched visible evidence"],
                    payload={"issue_codes": [], "facts": [], "domain": domain},
                )

        summary = f"{domain}: confirmed {len(findings)} issue(s)"
        if issue_codes:
            summary += ": " + ", ".join(issue_codes)
        return self._envelope(
            "COMPLETED",
            summary=summary,
            findings=findings,
            issue_codes=issue_codes,
            fact_ids=fact_ids,
            payload={"issue_codes": issue_codes, "facts": fact_ids, "domain": domain},
        )

    def _draft_patch(
        self,
        request: Mapping[str, Any],
        sections: list[VisibleSection],
        context_text: str,
    ) -> dict[str, Any]:
        payload = dict(request.get("payload") or {})
        codes = [str(c) for c in (payload.get("issue_codes") or [])]
        domain = str(request.get("domain") or payload.get("domain") or "")
        visible = self._visible_refs(sections)
        world = self.world
        if not codes:
            return self._envelope(
                "BLOCKED",
                summary="no issue codes supplied; cannot draft a repair",
                uncertainties=["upstream analysis produced no issue codes"],
                payload={"patches": [], "issue_codes": []},
            )
        if world is None:
            return self._envelope(
                "BLOCKED",
                summary="model has no repair knowledge for these codes",
                payload={"patches": [], "issue_codes": []},
            )
        patches: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        found: list[str] = []
        fact_ids: list[str] = []
        missing_refs: list[str] = []
        unknown: list[str] = []
        for code in codes:
            fix = world.issue_fixes.get(code)
            if fix is None:
                unknown.append(code)
                continue
            missing = [r for r in fix.required_refs if r not in visible]
            if missing:
                missing_refs.extend(missing)
                continue
            patches.extend(dict(p) for p in fix.patch)
            found.append(code)
            for fact_id in fix.fact_ids:
                if fact_id not in fact_ids:
                    fact_ids.append(fact_id)
            findings.append(
                {
                    "claim": f"repair for {code}: {fix.summary}",
                    "evidence_refs": list(fix.required_refs),
                    "fact_ids": list(fix.fact_ids),
                    "confidence": 0.8,
                    "details": {"issue_code": code},
                }
            )
        if not patches:
            if missing_refs:
                return self._envelope(
                    "NEED_CONTEXT",
                    summary=f"cannot draft repair without source files ({domain})",
                    requested_context=list(dict.fromkeys(missing_refs)),
                    uncertainties=["repair requires uncited source"],
                    payload={"patches": [], "issue_codes": []},
                )
            return self._envelope(
                "BLOCKED",
                summary=f"no repairable issue codes among {codes}",
                uncertainties=[f"unknown codes: {unknown}"] if unknown else [],
                payload={"patches": [], "issue_codes": []},
            )
        return self._envelope(
            "COMPLETED",
            summary=f"drafted {len(patches)} replacement patch(es) for {', '.join(found)}",
            findings=findings,
            issue_codes=found,
            fact_ids=fact_ids,
            payload={"patches": patches, "issue_codes": found},
        )

    def _verify(
        self,
        request: Mapping[str, Any],
        sections: list[VisibleSection],
        context_text: str,
    ) -> dict[str, Any]:
        payload = dict(request.get("payload") or {})
        claims = [dict(c) for c in (payload.get("claims") or [])]
        domains = [str(d) for d in (payload.get("domains") or [])]
        visible = self._visible_refs(sections)
        claimed_codes: set[str] = set()
        for claim in claims:
            claimed_codes.update(str(c) for c in (claim.get("issue_codes") or []))
        expected: list[str] = []
        missing: list[str] = []
        world = self.world
        if world is not None:
            for fix in world.issue_fixes.values():
                if domains and fix.domain not in domains:
                    continue
                if self._matched(fix.trigger_patterns, sections):
                    expected.append(fix.issue_code)
                    if fix.issue_code not in claimed_codes:
                        missing.append(fix.issue_code)
        verified: list[str] = []
        failed: list[str] = []
        for claim in claims:
            refs = [str(r) for r in (claim.get("evidence_refs") or [])]
            fact_ids = [str(f) for f in (claim.get("fact_ids") or [])]
            if refs and all(ref in visible for ref in refs):
                verified.extend(fid for fid in fact_ids if fid not in verified)
            else:
                failed.extend(fid for fid in fact_ids if fid not in failed)
        status = "CONFLICT" if missing else "COMPLETED"
        summary = (
            f"verification: {len(expected)} expected issue(s), {len(missing)} missing"
            if not missing
            else f"verification found {len(missing)} unreported issue(s): {', '.join(missing)}"
        )
        return self._envelope(
            status,
            summary=summary,
            findings=[
                {
                    "claim": summary,
                    "evidence_refs": sorted(visible)[:8],
                    "fact_ids": [],
                    "confidence": 0.9,
                }
            ],
            payload={
                "expected_issues": expected,
                "missing_issues": missing,
                "verified_fact_ids": verified,
                "failed_fact_ids": failed,
                "domains": domains,
            },
        )

    def _integrate(
        self,
        request: Mapping[str, Any],
        sections: list[VisibleSection],
        context_text: str,
    ) -> dict[str, Any]:
        payload = dict(request.get("payload") or {})
        question = str(payload.get("question") or request.get("objective") or "")
        visible_text = context_text
        fact_ids: list[str] = []
        for match in _FACT_IDS_RE.findall(visible_text):
            for fact_id in _json_ids(match):
                if fact_id not in fact_ids:
                    fact_ids.append(fact_id)
        issue_codes: list[str] = []
        for match in _ISSUE_CODES_RE.findall(visible_text):
            for code in _json_ids(match):
                if code not in issue_codes:
                    issue_codes.append(code)
        uncertainties: list[str] = []
        for match in _UNCERTAINTIES_RE.findall(visible_text):
            for line in match.split("\n"):
                line = line.strip().lstrip("-").strip()
                if line:
                    uncertainties.append(line)
        world = self.world
        omitted = str((world.faults.get("omit_fact") if world else "") or "")
        if omitted:
            fact_ids = [f for f in fact_ids if f != omitted]

        lines = ["# Final report", "", f"**Question:** {question}", "", "## Confirmed findings"]
        if fact_ids:
            for fact_id in fact_ids:
                statement = (
                    world.fact_statement(fact_id)
                    if world is not None
                    else f"<fact {fact_id}>"
                )
                lines.append(f"- [{fact_id}] {statement}")
        else:
            lines.append("- (no verified fact survived the information chain)")
        if issue_codes:
            lines.append("")
            lines.append("## Issue codes")
            lines.append("- " + ", ".join(issue_codes))
        conditions = _collect_conditions(visible_text)
        coverage_gaps = _collect_coverage_gaps(visible_text)
        if conditions["requires"]:
            lines += ["", "## Conditions to check before relying on these conclusions"]
            lines += [f"- {item}" for item in conditions["requires"]]
        if conditions["not_established"]:
            lines += ["", "## Not established"]
            lines += [f"- {item}" for item in conditions["not_established"]]
        if conditions["depends_on"]:
            lines += ["", "## Depends on (invalidate if the source version changes)"]
            lines += [f"- {item}" for item in conditions["depends_on"]]
        if coverage_gaps:
            lines += ["", "## Coverage limitations (what was NOT checked)"]
            lines += [f"- {item}" for item in coverage_gaps]
        evidence_refs = sorted(
            {s.ref for s in sections if s.ref and s.ref.startswith(("artifact://", "file://"))}
        )
        if evidence_refs:
            lines.append("")
            lines.append("## Evidence references")
            lines.extend(f"- {ref}" for ref in evidence_refs[:12])
        lines.append("")
        lines.append("## Open uncertainties")
        if uncertainties:
            lines.extend(f"- {item}" for item in dict.fromkeys(uncertainties))
        else:
            lines.append("- none recorded")
        report = "\n".join(lines)
        return self._envelope(
            "COMPLETED",
            summary=report,
            findings=[
                {
                    "claim": f"integrated report covering {len(fact_ids)} fact(s)",
                    "evidence_refs": evidence_refs[:8],
                    "fact_ids": list(fact_ids),
                    "confidence": 0.9,
                }
            ],
            issue_codes=issue_codes,
            fact_ids=list(fact_ids),
            uncertainties=list(dict.fromkeys(uncertainties)),
            payload={
                "answer": report,
                "fact_ids": list(fact_ids),
                "issues": issue_codes,
                "conditions": conditions,
                "coverage_gaps": coverage_gaps,
            },
        )

    def _resume(
        self,
        request: Mapping[str, Any],
        sections: list[VisibleSection],
        context_text: str,
    ) -> dict[str, Any]:
        """Cold-start executor: continue from a checkpoint, not from raw history."""
        payload = dict(request.get("payload") or {})
        continuation = _parse_continuation_text(context_text)
        if not continuation["next_step"]:
            return self._envelope(
                "NEED_CONTEXT",
                summary="no resumable continuation state is visible; cannot continue safely",
                uncertainties=["checkpoint missing next_step"],
                payload={
                    "next_action": "",
                    "required_checks": [],
                    "gaps": continuation["unresolved"],
                    "cited_refs": [],
                },
            )
        visible_refs = sorted({s.ref for s in sections if s.ref})
        gaps = list(continuation["unresolved"])
        next_action = continuation["next_step"]
        checks = list(continuation["required_checks"])
        summary = (
            f"cold-start resume: next action is {next_action!r}; "
            f"{len(checks)} required check(s), {len(gaps)} open gap(s)"
        )
        return self._envelope(
            "COMPLETED",
            summary=summary,
            findings=[
                {
                    "claim": f"resumed from checkpoint: {next_action}",
                    "evidence_refs": visible_refs[:8],
                    "fact_ids": [],
                    "confidence": 0.8,
                }
            ],
            uncertainties=list(dict.fromkeys(gaps)),
            payload={
                "next_action": next_action,
                "required_checks": checks,
                "gaps": list(dict.fromkeys(gaps)),
                "cited_refs": list(continuation["references"]) or visible_refs[:8],
                "established": continuation["established"],
                "rejected": continuation["rejected"],
                "goal": continuation["goal"],
            },
        )


class OpenAICompatLLM:
    """Optional adapter for an OpenAI-compatible ``/chat/completions`` endpoint."""

    name = "openai-compat"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 300.0,
        reasoning_effort: str | None = None,
        verify_tls: bool = True,
        json_mode: bool = True,
        stream: bool = True,
        idle_timeout: float | None = None,
        call_timeout: float | None = None,
    ) -> None:
        if reasoning_effort is not None and reasoning_effort not in ("low", "high"):
            raise ValueError("reasoning_effort must be 'low', 'high', or None")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.reasoning_effort = reasoning_effort
        self.verify_tls = verify_tls
        self.json_mode = json_mode
        self.stream = stream
        #: Optional observer for streaming progress: callback(dict).
        self.stream_callback = None
        #: Abort a stream whose *model output* goes silent for this long.
        #: Gateways often keep the HTTP connection alive with SSE comments while
        #: the upstream model has already died, so ``timeout`` (the socket read
        #: timeout) never fires; this is the guard that turns such a hang into a
        #: retryable ``ModelError``.
        self.idle_timeout = float(idle_timeout) if idle_timeout is not None else float(timeout)
        #: Absolute cap for one model call (0 = no cap). ``idle_timeout`` is the
        #: primary guard; this only bounds a stream that keeps producing output
        #: forever (e.g. a reasoning loop that would otherwise run for hours).
        self.call_timeout = float(call_timeout) if call_timeout is not None else 0.0

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        seed: int | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | None = None,
        on_stream: Any = None,
        call_timeout: float | None = None,
    ) -> LLMResponse:
        """Use provider output limits while accepting legacy token-cap arguments.

        The active output policy leaves token ceilings to the service.
        ``max_output_tokens`` remains an accepted compatibility argument.
        """
        wire_messages, request_meta = _wire_messages(messages)
        body = {
            "model": self.model,
            "messages": wire_messages,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if seed is not None:
            body["seed"] = seed
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if tools:
            body["tools"] = [dict(tool) for tool in tools]
            if tool_choice:
                body["tool_choice"] = tool_choice
        elif self.json_mode:
            # vLLM / OpenAI-compatible servers use this to constrain decoding to
            # a single JSON object, which removes an entire class of parse
            # failures for the structured-result protocol.
            body["response_format"] = {"type": "json_object"}
        if self.stream:
            body["stream"] = True
            return self._complete_streaming(
                body, messages, on_stream=on_stream, call_timeout=call_timeout, request_meta=request_meta,
            )
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            call_limit = float(self.call_timeout if call_timeout is None else call_timeout)
            started = time.monotonic()
            request_timeout = min(self.timeout, call_limit) if call_limit > 0 else self.timeout
            context = None
            if not self.verify_tls:
                import ssl

                # Test endpoints sometimes ship an expired certificate. This is
                # an explicit opt-in (HCCA_LLM_VERIFY_TLS=0) and never the
                # default; do not use it for production traffic.
                context = ssl._create_unverified_context()
            with urllib.request.urlopen(
                request, timeout=request_timeout, context=context
            ) as response:
                if call_limit > 0:
                    remaining = call_limit - (time.monotonic() - started)
                    if remaining <= 0:
                        raise ModelError("LLM request exceeded call timeout")
                    _set_response_read_timeout(response, min(self.timeout, remaining))
                data = json.loads(response.read().decode("utf-8"))
                if call_limit > 0 and time.monotonic() - started >= call_limit:
                    raise ModelError("LLM request exceeded call timeout")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            error = ModelError(f"LLM request failed: {exc}")
            error.meta = {**request_meta, **_http_error_metadata(exc, self.api_key)}
            raise error from exc
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content")
            finish_reason = choice.get("finish_reason", "")
        except (KeyError, IndexError, TypeError) as exc:
            error = ModelError(f"unexpected LLM response: {data!r}")
            error.meta = dict(request_meta)
            raise error from exc
        usage = data.get("usage") or {}
        cache = _cache_usage(usage)
        tool_calls = _normalise_tool_calls(choice["message"].get("tool_calls"))
        return LLMResponse(
            text=str(text or ""),
            tool_calls=tool_calls,
            reasoning_content=_native_reasoning_content(choice["message"]),
            input_tokens=int(usage.get("prompt_tokens", estimate_tokens(json.dumps(messages)))),
            output_tokens=int(usage.get("completion_tokens", estimate_tokens(text or ""))),
            meta={
                **request_meta,
                "model": self.model,
                "finish_reason": finish_reason,
                "reasoning_effort": self.reasoning_effort or "",
                "max_output_tokens": None,
                **cache,
            },
        )

    def _complete_streaming(
        self,
        body: dict[str, Any],
        messages: Sequence[Mapping[str, str]],
        *,
        on_stream: Any = None,
        call_timeout: float | None = None,
        request_meta: Mapping[str, Any] | None = None,
    ) -> LLMResponse:
        """Stream SSE responses.

        Some gateways put a hard timeout on non-streaming completions; streaming
        keeps the connection alive and also lets the bench show partial progress.
        Reasoning characters count received string fragments. Repetition and
        growing-prefix observations are diagnostic candidates only.
        """
        import ssl

        body = dict(body)
        body.pop("max_tokens", None)
        body.pop("max_completion_tokens", None)
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        context = None if self.verify_tls else ssl._create_unverified_context()
        content: list[str] = []
        reasoning: list[str] = []
        reasoning_seen = False
        reasoning_chars = 0
        tool_parts: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        finish_reason = ""
        started = time.time()
        started_monotonic = time.monotonic()
        last_notify = 0.0
        with_chunks = 0
        keepalives = 0
        try:
            idle_limit = float(getattr(self, "idle_timeout", 0.0) or 0.0)
        except (TypeError, ValueError):
            idle_limit = 0.0
        if idle_limit <= 0:
            idle_limit = float(self.timeout)
        try:
            configured_limit = getattr(self, "call_timeout", 0.0) if call_timeout is None else call_timeout
            call_limit = float(configured_limit or 0.0)
        except (TypeError, ValueError):
            call_limit = 0.0
        last_progress = started
        callback = on_stream if on_stream is not None else self.stream_callback
        diagnostics: dict[str, Any] = {
            "frame_count": 0,
            "malformed_frame_shapes": 0,
            "nonstring_content_fragments": 0,
            "message_snapshot_conflicts": 0,
            "field_counts": {},
            "last_nonempty_fields": {},
            "reasoning_fragment_count": 0,
            "nonstring_reasoning_fragments": 0,
            "adjacent_duplicate_reasoning_fragments": 0,
            "cumulative_reasoning_snapshot_candidates": 0,
            "json_parse_errors": 0,
            "termination": "in_progress",
            "completion_id": "",
            "event_id": "",
            "effective_call_timeout": call_limit,
            "effective_socket_timeout": min(self.timeout, call_limit) if call_limit > 0 else self.timeout,
        }
        sample_head: list[dict[str, Any]] = []
        sample_tail: list[dict[str, Any]] = []
        frame_head: list[dict[str, Any]] = []
        frame_tail: list[dict[str, Any]] = []
        previous_reasoning: str | None = None

        def preview(value: str, byte_limit: int, *, suffix: bool = False) -> str:
            result = value[-byte_limit:] if suffix else value[:byte_limit]
            result = result.encode("utf-8", "replace").decode("utf-8")
            while len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > byte_limit:
                result = result[len(result) // 2:] if suffix else result[:len(result) // 2]
            return result

        def record_frame(payload: str, chunk: Any, status: str, data_lines: int) -> None:
            diagnostics["frame_count"] += 1
            fields = {}
            choice = {}
            delta = {}
            message = {}
            if isinstance(chunk, dict):
                choices = chunk.get("choices")
                if isinstance(choices, list):
                    selected = _stream_choice(choices)
                    if isinstance(selected, dict):
                        choice = selected
                delta = choice.get("delta")
                message = choice.get("message")
                for container_name, container in (("choice", choice), ("delta", delta), ("message", message)):
                    if not isinstance(container, dict):
                        continue
                    for field_name in ("content", "function_call", "tool_calls"):
                        if field_name not in container:
                            continue
                        value = container[field_name]
                        is_text = isinstance(value, str)
                        encoded_value = value if is_text else json.dumps(
                            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        )
                        fields[f"{container_name}.{field_name}"] = {
                            "type": type(value).__name__,
                            "chars": len(encoded_value),
                            "sha256": hashlib.sha256(encoded_value.encode("utf-8", "surrogatepass")).hexdigest(),
                            "prefix": preview(encoded_value, 64),
                            "encoding": "text" if is_text else "json_canonical",
                        }
                        path = f"{container_name}.{field_name}"
                        diagnostics["field_counts"][path] = diagnostics["field_counts"].get(path, 0) + 1
                        if value:
                            diagnostics["last_nonempty_fields"][path] = dict(fields[path])
            sample = {
                "chars": len(payload),
                "sha256": hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest(),
                "prefix": preview(payload, 128),
                "suffix": preview(payload, 128, suffix=True),
                "parse_status": status,
                "data_lines": data_lines,
                "type": type(chunk).__name__,
                "keys": [preview(key, 32) for key in list(chunk)[:16]] if isinstance(chunk, dict) else [],
                "choice_keys": [preview(key, 32) for key in list(choice)[:16]],
                "delta_keys": [preview(key, 32) for key in list(delta)[:16]] if isinstance(delta, dict) else [],
                "message_keys": [preview(key, 32) for key in list(message)[:16]] if isinstance(message, dict) else [],
                "key_counts": {
                    "chunk": len(chunk) if isinstance(chunk, dict) else 0,
                    "choice": len(choice),
                    "delta": len(delta) if isinstance(delta, dict) else 0,
                    "message": len(message) if isinstance(message, dict) else 0,
                },
                "fields": fields,
                "completion_id": preview(str(chunk.get("id") or ""), 64)
                if isinstance(chunk, dict) else "",
                "event_id": preview(diagnostics["event_id"], 64),
            }
            if len(json.dumps(sample, ensure_ascii=False).encode("utf-8")) > 4096:
                sample["sample_truncated"] = True
                sample["prefix"] = preview(sample["prefix"], 32)
                sample["suffix"] = preview(sample["suffix"], 32, suffix=True)
                for key in ("keys", "choice_keys", "delta_keys", "message_keys"):
                    sample[key] = [preview(value, 32) for value in sample[key][:8]]
                for field_summary in fields.values():
                    field_summary["prefix"] = preview(field_summary["prefix"], 16)
            if len(frame_head) < 2:
                frame_head.append(sample)
            frame_tail.append(sample)
            del frame_tail[:-2]

        def record_reasoning(value: Any, field_name: str) -> bool:
            nonlocal previous_reasoning
            is_text = isinstance(value, str)
            sample = {
                "field": field_name,
                "type": type(value).__name__,
                "chars": len(value) if is_text else None,
                "sha256": hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()
                if is_text else None,
                "prefix": value[:128] if is_text else "",
                "completion_id": diagnostics["completion_id"],
                "event_id": diagnostics["event_id"],
            }
            if len(sample_head) < 2:
                sample_head.append(sample)
            sample_tail.append(sample)
            del sample_tail[:-2]
            if not is_text:
                diagnostics["nonstring_reasoning_fragments"] += 1
                return False
            diagnostics["reasoning_fragment_count"] += 1
            if previous_reasoning is not None:
                if value == previous_reasoning:
                    diagnostics["adjacent_duplicate_reasoning_fragments"] += 1
                elif len(value) > len(previous_reasoning) and value.startswith(previous_reasoning):
                    diagnostics["cumulative_reasoning_snapshot_candidates"] += 1
            previous_reasoning = value
            return True

        def read_reasoning(container: Mapping[str, Any]) -> str | None:
            empty = None
            for field_name in ("reasoning_content", "reasoning"):
                value = container.get(field_name)
                if value is None:
                    continue
                if isinstance(value, str) and not value:
                    empty = ""
                    continue
                if record_reasoning(value, field_name):
                    return value
            return empty

        def stream_meta() -> dict[str, Any]:
            now = time.time()
            snapshot = {
                **deepcopy(diagnostics),
                "reasoning_samples": {
                    "head": [dict(sample) for sample in sample_head],
                    "tail": [dict(sample) for sample in sample_tail],
                },
                "frame_samples": {
                    "head": deepcopy(frame_head),
                    "tail": deepcopy(frame_tail),
                },
            }
            try:
                cache = _cache_usage(usage)
                completion = (
                    {"completion_tokens": int(usage["completion_tokens"])}
                    if "completion_tokens" in usage else {}
                )
            except (TypeError, ValueError, OverflowError):
                cache, completion = {}, {}
                snapshot["usage_parse_error"] = True
            usage_complete = (
                bool(usage) and not snapshot.get("usage_parse_error")
                and "prompt_tokens" in usage and "completion_tokens" in usage
                and diagnostics["termination"] in {"done", "eof"}
            )
            details = usage.get("prompt_tokens_details") or {}
            if not isinstance(details, Mapping):
                details = {}
            cache_source = "missing"
            if "prompt_cache_hit_tokens" in usage or "prompt_cache_miss_tokens" in usage:
                cache_source = "provider_top_level"
            elif "cached_tokens" in details:
                cache_source = "provider_details"
            known_hit = "prompt_cache_hit_tokens" in usage or "cached_tokens" in details
            return {
                **(request_meta or {}),
                "model": self.model,
                "finish_reason": finish_reason,
                "reasoning_effort": self.reasoning_effort or "",
                "max_output_tokens": None,
                "streamed": True,
                "reasoning_counter_kind": "received_fragments",
                "reasoning_chars": reasoning_chars,
                "content_chars": sum(len(part) for part in content),
                "nonstring_reasoning_fragments": diagnostics["nonstring_reasoning_fragments"],
                "stream_chunks": with_chunks,
                "stream_keepalives": keepalives,
                "call_timeout_s": call_limit,
                "effective_call_timeout": call_limit,
                "effective_socket_timeout": diagnostics["effective_socket_timeout"],
                "elapsed_s": now - started,
                "idle_s": now - last_progress,
                "usage": deepcopy(usage),
                "usage_missing": not bool(usage) or bool(snapshot.get("usage_parse_error")),
                "usage_complete": bool(usage_complete),
                "usage_partial": bool(usage) and not usage_complete,
                "cache_usage_source": cache_source,
                "cache_usage_complete": bool(
                    usage_complete and known_hit
                    and ("prompt_cache_miss_tokens" in usage or "prompt_tokens" in usage)
                ),
                "usage_estimated": (
                    "prompt_tokens" not in usage or "completion_tokens" not in usage
                    or bool(snapshot.get("usage_parse_error"))
                ),
                "stream_diagnostics": snapshot,
                **cache,
                **completion,
            }

        def stream_error(exc: Exception) -> ModelError:
            diagnostics["termination"] = "error"
            error = exc if isinstance(exc, ModelError) else ModelError(
                f"LLM streaming request failed: {exc}"
            )
            error.meta = {**stream_meta(), **_http_error_metadata(exc, self.api_key)}
            error.usage = deepcopy(usage)
            return error

        def read_timeout() -> float:
            if call_limit <= 0:
                return self.timeout
            remaining = call_limit - (time.monotonic() - started_monotonic)
            if remaining <= 0:
                raise ModelError(
                    "LLM stream exceeded call timeout "
                    f"({call_limit:.0f}s; chunks={with_chunks}, keepalives={keepalives})"
                )
            return min(self.timeout, remaining)

        def payloads(response: Any):
            """Frame SSE data events, including gateways omitting blank separators."""
            nonlocal keepalives
            pending: list[str] = []
            json_body: list[str] = []
            json_response = "json" in diagnostics.get("content_type", "").lower()
            lines = iter(response)
            try:
                while True:
                    diagnostics["effective_socket_timeout"] = read_timeout()
                    _set_response_read_timeout(response, diagnostics["effective_socket_timeout"])
                    try:
                        raw_line = next(lines)
                    except StopIteration:
                        break
                    now = time.time()
                    read_timeout()
                    if now - last_progress > idle_limit:
                        raise ModelError(
                            "LLM stream stalled: no model output for "
                            f"{now - last_progress:.0f}s (limit {idle_limit:.0f}s; "
                            f"chunks={with_chunks}, keepalives={keepalives})"
                        )
                    line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("data:"):
                        json_response = False
                    if json_response:
                        json_body.append(raw_line.decode("utf-8", "replace"))
                        continue
                    if not line:
                        if pending:
                            yield "\n".join(pending), len(pending)
                            pending.clear()
                        continue
                    if line == "data" or line.startswith("data:"):
                        piece = line[5:] if line.startswith("data:") else ""
                        if piece.startswith(" "):
                            piece = piece[1:]
                        if pending:
                            previous = "\n".join(pending)
                            try:
                                json.loads(previous)
                            except json.JSONDecodeError:
                                pass
                            else:
                                yield previous, len(pending)
                                pending.clear()
                        if piece.strip() == "[DONE]":
                            if pending:
                                yield "\n".join(pending), len(pending)
                                pending.clear()
                            yield "[DONE]", 1
                            return
                        pending.append(piece)
                        continue
                    if line.startswith("id:"):
                        diagnostics["event_id"] = line[3:].strip()[:64]
                    keepalives += 1
            except (ModelError, OSError, urllib.error.URLError):
                if pending:
                    yield "\n".join(pending), len(pending)
                if json_body:
                    yield "".join(json_body), 0
                raise
            if pending:
                yield "\n".join(pending), len(pending)
            if json_body:
                yield "".join(json_body), 0

        try:
            diagnostics["effective_socket_timeout"] = read_timeout()
            require_stream_done = False
            with urllib.request.urlopen(
                request, timeout=diagnostics["effective_socket_timeout"], context=context
            ) as response:
                headers = getattr(response, "headers", None)
                if headers is not None:
                    diagnostics["response_request_id"] = str(
                        headers.get("x-request-id") or headers.get("request-id") or ""
                    )[:64]
                    diagnostics["content_type"] = str(headers.get("Content-Type") or "")[:128]
                    require_stream_done = (
                        str(headers.get("X-HCCA-Require-Stream-Done") or "").strip() == "1"
                        and diagnostics["content_type"].split(";", 1)[0].strip().lower() == "text/event-stream"
                    )
                for payload, data_lines in payloads(response):
                    now = time.time()
                    if not payload.strip():
                        continue
                    if payload.strip() == "[DONE]":
                        record_frame(payload, None, "done", data_lines)
                        diagnostics["termination"] = "done"
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        diagnostics["json_parse_errors"] += 1
                        record_frame(payload, None, "invalid_json", data_lines)
                        continue
                    record_frame(payload, chunk, "parsed" if isinstance(chunk, dict) else "non_object", data_lines)
                    if not isinstance(chunk, dict):
                        diagnostics["malformed_frame_shapes"] += 1
                        continue
                    if chunk.get("id") is not None:
                        diagnostics["completion_id"] = str(chunk["id"])[:64]
                    progressed = False
                    if isinstance(chunk.get("usage"), dict):
                        for key, value in chunk["usage"].items():
                            if key in {"prompt_tokens_details", "completion_tokens_details"} and isinstance(value, dict):
                                previous = usage.get(key)
                                usage[key] = {**(previous if isinstance(previous, dict) else {}), **value}
                            else:
                                usage[key] = value
                        progressed = True
                    choices = chunk.get("choices") or []
                    if not isinstance(choices, list):
                        diagnostics["malformed_frame_shapes"] += 1
                        continue
                    if not choices:
                        if progressed:
                            last_progress = now
                        continue
                    choice = _stream_choice(choices)
                    if not isinstance(choice, dict):
                        diagnostics["malformed_frame_shapes"] += 1
                        continue
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        diagnostics["malformed_frame_shapes"] += 1
                        delta = {}
                    text_delta = delta.get("content")
                    if isinstance(text_delta, str) and text_delta:
                        content.append(text_delta)
                        progressed = True
                    elif text_delta is not None and not isinstance(text_delta, str):
                        diagnostics["nonstring_content_fragments"] += 1
                    thinking_delta = read_reasoning(delta)
                    if thinking_delta is not None:
                        reasoning_seen = True
                        reasoning.append(thinking_delta)
                        reasoning_chars += len(thinking_delta)
                        progressed = progressed or bool(thinking_delta)
                    if delta.get("tool_calls"):
                        _accumulate_tool_delta(tool_parts, delta.get("tool_calls"))
                        progressed = True
                    elif isinstance(delta.get("function_call"), dict):
                        _accumulate_tool_delta(tool_parts, [{
                            "index": 0, "id": "legacy_function_call", "function": delta["function_call"],
                        }])
                        progressed = True
                    message = choice.get("message") or {}
                    if not isinstance(message, dict):
                        diagnostics["malformed_frame_shapes"] += 1
                        message = {}
                    message_content = message.get("content")
                    if isinstance(message_content, str) and message_content:
                        assembled = "".join(content)
                        if message_content.startswith(assembled):
                            suffix = message_content[len(assembled):]
                            if suffix:
                                content.append(suffix)
                                progressed = True
                        elif not assembled.startswith(message_content):
                            diagnostics["message_snapshot_conflicts"] += 1
                            content[:] = [message_content]
                            progressed = True
                    elif message_content is not None and not isinstance(message_content, str):
                        diagnostics["nonstring_content_fragments"] += 1
                    thinking_snapshot = read_reasoning(message)
                    if thinking_snapshot is not None:
                        reasoning_seen = True
                        reasoning[:] = [thinking_snapshot]
                        reasoning_chars += len(thinking_snapshot)
                        progressed = progressed or bool(thinking_snapshot)
                    if message.get("tool_calls"):
                        _apply_tool_snapshot(tool_parts, message["tool_calls"])
                        progressed = True
                    elif isinstance(message.get("function_call"), dict):
                        _apply_tool_snapshot(tool_parts, [{
                            "id": "legacy_function_call", "function": message["function_call"],
                        }])
                        progressed = True
                    if choice.get("finish_reason"):
                        finish_reason = str(choice["finish_reason"])
                        progressed = True
                    if progressed:
                        last_progress = now
                    with_chunks += 1
                    if callback is not None and now - last_notify >= 2.0:
                        last_notify = now
                        try:
                            callback(
                                {
                                    **stream_meta(),
                                    "elapsed_s": round(now - started, 1),
                                    "idle_s": round(now - last_progress, 1),
                                    "content_chars": sum(len(part) for part in content),
                                    "reasoning_chars": reasoning_chars,
                                    "chunks": with_chunks,
                                    "keepalives": keepalives,
                                    "finish_reason": finish_reason,
                                }
                            )
                        except Exception:
                            pass
            if diagnostics["termination"] == "in_progress":
                if require_stream_done:
                    raise ModelError("LLM event stream ended before [DONE]")
                diagnostics["termination"] = "eof"
        except ModelError as exc:
            stream_error(exc)
            raise
        except (urllib.error.URLError, OSError, ValueError, TypeError, AttributeError) as exc:
            raise stream_error(exc) from exc
        text = "".join(content)
        guessed_output = estimate_tokens(text) + estimate_tokens("".join(reasoning))
        tool_calls = []
        for index in sorted(tool_parts):
            item = tool_parts[index]
            arguments = item.get("arguments") or "{}"
            try:
                parsed_args = json.loads(arguments)
            except json.JSONDecodeError:
                parsed_args = {"_raw": arguments}
            tool_calls.append(
                {"id": item.get("id") or f"call_{index}", "name": item.get("name") or "", "arguments": parsed_args}
            )
        try:
            return LLMResponse(
                text=text,
                tool_calls=tool_calls,
                reasoning_content="".join(reasoning) if reasoning_seen else None,
                input_tokens=int(usage.get("prompt_tokens", estimate_tokens(json.dumps(messages)))),
                output_tokens=int(usage.get("completion_tokens", guessed_output)),
                meta=stream_meta(),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise stream_error(exc) from exc


def _accumulate_tool_delta(
    parts: dict[int, dict[str, Any]], deltas: Any
) -> None:
    if not deltas:
        return
    for raw in deltas or ():
        if not isinstance(raw, Mapping):
            continue
        try:
            index = int(raw.get("index", 0))
        except (TypeError, ValueError):
            index = 0
        function = raw.get("function") or {}
        if not isinstance(function, Mapping):
            continue
        item = parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if raw.get("id"):
            item["id"] = str(raw["id"])
        if function.get("name"):
            item["name"] = str(function["name"])
        if function.get("arguments"):
            item["arguments"] = str(item["arguments"]) + str(function["arguments"])


def _apply_tool_snapshot(parts: dict[int, dict[str, Any]], raw_calls: Any) -> None:
    """Apply complete message tool calls without appending their full arguments."""
    if not isinstance(raw_calls, list):
        return
    for index, raw in enumerate(raw_calls):
        if not isinstance(raw, Mapping):
            continue
        function = raw.get("function") or {}
        if not isinstance(function, Mapping):
            continue
        matching = next(
            (key for key, part in parts.items() if raw.get("id") and part.get("id") == raw["id"]),
            index,
        )
        item = parts.setdefault(matching, {"id": "", "name": "", "arguments": ""})
        if raw.get("id"):
            item["id"] = str(raw["id"])
        if function.get("name"):
            item["name"] = str(function["name"])
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            item["arguments"] = arguments
        elif isinstance(arguments, (dict, list)):
            item["arguments"] = json.dumps(arguments)


def _normalise_tool_calls(raw_calls: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_calls or []):
        if not isinstance(raw, Mapping):
            continue
        function = raw.get("function") or {}
        arguments = function.get("arguments") or "{}"
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError:
            parsed = {"_raw": arguments}
        calls.append(
            {
                "id": raw.get("id") or f"call_{index}",
                "name": function.get("name") or "",
                "arguments": parsed,
            }
        )
    return calls


def load_llm_from_env(
    world: ModelWorld | None = None, *, seed: int = 0
) -> LLMClient:
    """Use a real endpoint only when explicitly configured; else scripted.

    Real-model runs are opt-in through environment variables so the default
    evaluation stays deterministic and offline:

    ``HCCA_LLM_BASE_URL`` (e.g. https://host/v1), ``HCCA_LLM_API_KEY``,
    ``HCCA_LLM_MODEL``, optional ``HCCA_LLM_TIMEOUT`` (socket read timeout),
    ``HCCA_LLM_IDLE_TIMEOUT`` (abort a stream with no real model output for this
    long; defaults to ``HCCA_LLM_TIMEOUT``), ``HCCA_LLM_CALL_TIMEOUT`` (absolute
    cap for one call, 0 = none), ``HCCA_LLM_REASONING_EFFORT`` and
    ``HCCA_LLM_VERIFY_TLS`` (default 1; set 0 only for test endpoints with a
    broken certificate).
    """
    import os

    base_url = os.environ.get("HCCA_LLM_BASE_URL", "").strip()
    api_key = os.environ.get("HCCA_LLM_API_KEY", "").strip()
    model = os.environ.get("HCCA_LLM_MODEL", "deepseek-chat").strip()
    verify_tls = os.environ.get("HCCA_LLM_VERIFY_TLS", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    json_mode = os.environ.get("HCCA_LLM_JSON_MODE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    stream = os.environ.get("HCCA_LLM_STREAM", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if base_url and api_key:
        try:
            timeout = float(os.environ.get("HCCA_LLM_TIMEOUT", "300"))
        except ValueError:
            timeout = 300.0
        effort = os.environ.get("HCCA_LLM_REASONING_EFFORT", "high").strip() or "high"

        def _env_float(name: str, fallback: float | None) -> float | None:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return fallback
            try:
                return float(raw)
            except ValueError:
                return fallback

        return OpenAICompatLLM(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=timeout,
            reasoning_effort=effort,
            verify_tls=verify_tls,
            json_mode=json_mode,
            stream=stream,
            # A stalled stream must not hang the caller forever: keep-alive
            # comments can defeat the socket read timeout (see
            # ``_complete_streaming``), so the caller gets an explicit guard.
            idle_timeout=_env_float("HCCA_LLM_IDLE_TIMEOUT", timeout),
            call_timeout=_env_float("HCCA_LLM_CALL_TIMEOUT", 0.0),
        )
    return ScriptedLLM(world=world, seed=seed)


__all__ = [
    "LLMClient",
    "LLMResponse",
    "OpenAICompatLLM",
    "ScriptedLLM",
    "VisibleSection",
    "load_llm_from_env",
    "parse_sections",
    "split_prompt",
]
