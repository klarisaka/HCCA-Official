"""Core data types shared by every layer of the HCCA prototype.

The types in this module are deliberately small and serialisable: they are the
wire format between the control plane (hierarchy / DAG / verifier) and the
information plane (artifact store / router / memory).  Everything an agent
receives or returns is one of these objects, which keeps the prototype honest
about what crosses an agent boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping

# ---------------------------------------------------------------------------
# Small enums expressed as Literal aliases (kept dependency-free)
# ---------------------------------------------------------------------------

Status = Literal[
    "PENDING", "RUNNING", "COMPLETED", "NEED_CONTEXT", "BLOCKED", "CONFLICT", "FAILED"
]
Risk = Literal["low", "medium", "high"]
Role = Literal["root", "domain", "worker", "verifier"]
SemanticKind = Literal[
    "system", "invariants", "contract", "memory", "retrieved", "history",
    "dependency", "evidence", "tool",
]
Placement = Literal["PIN", "KEEP", "COMPRESS", "EXTERNALIZE", "DISCARD"]

STATUS_PENDING: Status = "PENDING"
STATUS_RUNNING: Status = "RUNNING"
STATUS_COMPLETED: Status = "COMPLETED"
STATUS_NEED_CONTEXT: Status = "NEED_CONTEXT"
STATUS_BLOCKED: Status = "BLOCKED"
STATUS_CONFLICT: Status = "CONFLICT"
STATUS_FAILED: Status = "FAILED"

FINISHED_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_BLOCKED})
RETRYABLE_STATUSES = frozenset({STATUS_NEED_CONTEXT})

#: Statuses a completed task may carry when it still reports work to do.
_NON_SUCCESS = frozenset(
    {STATUS_FAILED, STATUS_BLOCKED, STATUS_NEED_CONTEXT, STATUS_CONFLICT}
)


def _as_tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, (list, set)):
        return tuple(value)
    return (value,)


def _jsonable(value: Any) -> Any:
    """Best-effort conversion so result payloads can always be logged as JSON."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


# ---------------------------------------------------------------------------
# Budgets and contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Budget:
    """A token budget for one agent invocation.

    ``input_budget`` is the maximum size of the assembled context (C_i),
    ``output_budget`` the maximum generation length (O_i) and ``context_window``
    the hard model window (W_i).  The invariant enforced here is
    ``C_i + O_i <= W_i``, with a small safety margin optional but not required.
    """

    input_budget: int = 8000
    output_budget: int = 1024
    context_window: int = 32768

    def __post_init__(self) -> None:
        if self.input_budget <= 0:
            raise ValueError("input_budget must be positive")
        if self.output_budget < 0:
            raise ValueError("output_budget must be non-negative")
        if self.input_budget + self.output_budget > self.context_window:
            raise ValueError(
                "input_budget + output_budget must not exceed context_window "
                f"({self.input_budget} + {self.output_budget} > {self.context_window})"
            )

    @property
    def work_budget(self) -> int:
        """B_i in the design note: context budget plus reserved output."""
        return self.input_budget + self.output_budget

    def fits(self, tokens: int) -> bool:
        return tokens <= self.input_budget

    def only_input(self) -> "Budget":
        return Budget(self.input_budget, 0, self.context_window)

    def to_dict(self) -> dict[str, int]:
        return {
            "input_budget": self.input_budget,
            "output_budget": self.output_budget,
            "context_window": self.context_window,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "Budget":
        data = dict(data or {})
        return cls(
            input_budget=int(data.get("input_budget", 8000)),
            output_budget=int(data.get("output_budget", 1024)),
            context_window=int(data.get("context_window", 32768)),
        )


@dataclass(frozen=True)
class Scope:
    """Applicability scope for invariants.

    An empty field means "matches anything"; populated fields must all match
    (logical AND), which makes it cheap to keep a global invariant store while
    still scoping entries to a domain or a task.
    """

    domains: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    min_depth: int | None = None
    max_depth: int | None = None

    def matches(
        self,
        *,
        domain: str = "",
        task_id: str = "",
        text: str = "",
        depth: int | None = None,
    ) -> bool:
        if self.domains and domain not in self.domains:
            return False
        if self.task_ids and task_id not in self.task_ids:
            return False
        if self.keywords:
            haystack = text.lower()
            if not any(k.lower() in haystack for k in self.keywords):
                return False
        if self.min_depth is not None and (depth is None or depth < self.min_depth):
            return False
        if self.max_depth is not None and (depth is None or depth > self.max_depth):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "domains": list(self.domains),
            "task_ids": list(self.task_ids),
            "keywords": list(self.keywords),
            "min_depth": self.min_depth,
            "max_depth": self.max_depth,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "Scope":
        data = dict(data or {})
        return cls(
            domains=_as_tuple(data.get("domains")),
            task_ids=_as_tuple(data.get("task_ids")),
            keywords=_as_tuple(data.get("keywords")),
            min_depth=data.get("min_depth"),
            max_depth=data.get("max_depth"),
        )


@dataclass(frozen=True)
class TaskContract:
    """The standardised unit of delegation between two HCCA agents.

    A contract is the only way a parent tells a child what to do.  It names the
    objective, the exact context refs the child may consult, the constraints
    that must not be broken, the acceptance criteria, the dependency set and a
    token budget.  Workers answer with a :class:`ResultEnvelope`, never with
    free-form chat.
    """

    task_id: str
    objective: str
    kind: str = "analyze"
    domain: str = "general"
    depth: int = 0
    parent_id: str | None = None
    context_refs: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    budget: Budget = field(default_factory=Budget)
    depends_on: tuple[str, ...] = ()
    risk: Risk = "medium"
    artifacts_out: tuple[str, ...] = ()
    required_facts: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.task_id or not self.task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if not self.objective or not self.objective.strip():
            raise ValueError(f"task {self.task_id}: objective must be non-empty")
        if self.depth < 0:
            raise ValueError(f"task {self.task_id}: depth must be >= 0")
        if self.task_id in self.depends_on:
            raise ValueError(f"task {self.task_id} cannot depend on itself")
        for ref in self.context_refs:
            if "://" not in ref:
                raise ValueError(f"task {self.task_id}: malformed context ref {ref!r}")

    def render(self, *, max_field_chars: int = 900) -> str:
        """Render the contract as the text injected into the agent context."""

        def _bullets(items: Iterable[str]) -> str:
            items = [str(i) for i in items]
            return "\n".join(f"  - {i[:max_field_chars]}" for i in items) if items else "  (none)"

        return (
            f"TASK {self.task_id} | kind={self.kind} domain={self.domain} "
            f"depth={self.depth} risk={self.risk}\n"
            f"OBJECTIVE: {self.objective}\n"
            "CONTEXT_REFS:\n" + _bullets(self.context_refs) + "\n"
            "CONSTRAINTS:\n" + _bullets(self.constraints) + "\n"
            "ACCEPTANCE:\n" + _bullets(self.acceptance) + "\n"
            "DEPENDS_ON: " + (", ".join(self.depends_on) or "(none)") + "\n"
            "QUESTIONS_TO_ANSWER:\n" + _bullets(self.questions) + "\n"
            f"BUDGET: input<={self.budget.input_budget} output<={self.budget.output_budget} "
            f"window={self.budget.context_window}\n"
            "OUTPUT_FORMAT: return a ResultEnvelope JSON object with keys: "
            "status, summary, findings[{claim, evidence_refs, fact_ids, confidence}], "
            "artifacts, uncertainties, requested_context, issue_codes, fact_ids, payload."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "kind": self.kind,
            "domain": self.domain,
            "depth": self.depth,
            "parent_id": self.parent_id,
            "context_refs": list(self.context_refs),
            "constraints": list(self.constraints),
            "acceptance": list(self.acceptance),
            "budget": self.budget.to_dict(),
            "depends_on": list(self.depends_on),
            "risk": self.risk,
            "artifacts_out": list(self.artifacts_out),
            "required_facts": list(self.required_facts),
            "questions": list(self.questions),
            "metadata": _jsonable(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskContract":
        return cls(
            task_id=str(data["task_id"]),
            objective=str(data["objective"]),
            kind=str(data.get("kind", "analyze")),
            domain=str(data.get("domain", "general")),
            depth=int(data.get("depth", 0)),
            parent_id=data.get("parent_id"),
            context_refs=tuple(str(r) for r in _as_tuple(data.get("context_refs"))),
            constraints=tuple(str(c) for c in _as_tuple(data.get("constraints"))),
            acceptance=tuple(str(a) for a in _as_tuple(data.get("acceptance"))),
            budget=Budget.from_dict(data.get("budget")),
            depends_on=tuple(str(d) for d in _as_tuple(data.get("depends_on"))),
            risk=data.get("risk", "medium"),
            artifacts_out=tuple(str(a) for a in _as_tuple(data.get("artifacts_out"))),
            required_facts=tuple(str(f) for f in _as_tuple(data.get("required_facts"))),
            questions=tuple(str(q) for q in _as_tuple(data.get("questions"))),
            metadata=dict(data.get("metadata") or {}),
        )


# ---------------------------------------------------------------------------
# Results: what crosses an agent boundary
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One claim produced by an agent, anchored to evidence refs."""

    claim: str
    evidence_refs: tuple[str, ...] = ()
    fact_ids: tuple[str, ...] = ()
    confidence: float = 0.5
    requires: tuple[str, ...] = ()
    not_established: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "evidence_refs": list(self.evidence_refs),
            "fact_ids": list(self.fact_ids),
            "confidence": self.confidence,
            "requires": list(self.requires),
            "not_established": list(self.not_established),
            "depends_on": list(self.depends_on),
            "details": _jsonable(self.details),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Finding":
        try:
            confidence = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        return cls(
            claim=str(data.get("claim", "")),
            evidence_refs=tuple(str(r) for r in _as_tuple(data.get("evidence_refs"))),
            fact_ids=tuple(str(f) for f in _as_tuple(data.get("fact_ids"))),
            confidence=max(0.0, min(1.0, confidence)),
            requires=tuple(str(r) for r in _as_tuple(data.get("requires"))),
            not_established=tuple(
                str(n) for n in _as_tuple(data.get("not_established"))
            ),
            depends_on=tuple(str(d) for d in _as_tuple(data.get("depends_on"))),
            details=dict(data.get("details") or {}),
        )


@dataclass
class ResultEnvelope:
    """Compact, verifiable answer passed from a child to its parent.

    The envelope intentionally does not contain large artefacts: it carries a
    summary, claims with evidence refs, artifact refs and open uncertainties.
    The parent can always walk the refs back to the artifact store.
    """

    task_id: str
    status: Status = STATUS_COMPLETED
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    requested_context: list[str] = field(default_factory=list)
    issue_codes: list[str] = field(default_factory=list)
    fact_ids: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    # v0.2: resumable hand-off state and explicit coverage limitations.
    continuation: dict[str, Any] | None = None
    coverage: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status not in _NON_SUCCESS

    @property
    def needs_context(self) -> bool:
        return self.status == STATUS_NEED_CONTEXT

    def add_finding(
        self,
        claim: str,
        *,
        evidence_refs: Iterable[str] = (),
        fact_ids: Iterable[str] = (),
        confidence: float = 0.5,
        details: Mapping[str, Any] | None = None,
    ) -> Finding:
        finding = Finding(
            claim=claim,
            evidence_refs=tuple(evidence_refs),
            fact_ids=tuple(fact_ids),
            confidence=confidence,
            details=dict(details or {}),
        )
        self.findings.append(finding)
        for fact_id in finding.fact_ids:
            if fact_id not in self.fact_ids:
                self.fact_ids.append(fact_id)
        return finding

    def text_for_parent(self) -> str:
        """The compact view a parent agent receives (not the raw evidence)."""
        lines = [
            f"RESULT task_id={self.task_id} status={self.status}",
            f"SUMMARY: {self.summary}",
        ]
        if self.findings:
            lines.append("FINDINGS:")
            for f in self.findings:
                refs = ", ".join(f.evidence_refs) or "(no evidence refs)"
                lines.append(f"  - claim: {f.claim} | evidence: {refs} | confidence: {f.confidence:.2f}")
                if f.requires:
                    lines.append("    requires: " + "; ".join(f.requires))
                if f.not_established:
                    lines.append("    not_established: " + "; ".join(f.not_established))
                if f.depends_on:
                    lines.append("    depends_on: " + ", ".join(f.depends_on))
        if self.continuation:
            continuation = self.continuation
            lines.append("CONTINUATION:")
            lines.append(f"  goal: {continuation.get('goal', '')}")
            lines.append(f"  next_step: {continuation.get('next_step', '')}")
            for key in (
                "established",
                "unresolved",
                "rejected",
                "required_checks",
                "references",
            ):
                values = continuation.get(key) or []
                lines.append(f"  {key}: " + ("; ".join(str(v) for v in values) if values else "(none)"))
        if self.coverage:
            lines.append("COVERAGE:")
            for note in self.coverage:
                scope = note.get("scope", "")
                searched = note.get("searched") or []
                not_searched = note.get("not_searched") or []
                lines.append(
                    f"  - scope={scope} | searched={len(searched)} | "
                    f"NOT_SEARCHED={', '.join(str(v) for v in not_searched) or '(none)'}"
                )
        if self.artifacts:
            lines.append("ARTIFACTS:")
            lines.append("\n".join(f"  - {a}" for a in self.artifacts))
        if self.uncertainties:
            lines.append("UNCERTAINTIES:")
            lines.append("\n".join(f"  - {u}" for u in self.uncertainties))
        if self.requested_context:
            lines.append("REQUESTED_CONTEXT:")
            lines.append("\n".join(f"  - {r}" for r in self.requested_context))
        if self.issue_codes:
            lines.append("ISSUE_CODES: " + ", ".join(self.issue_codes))
        if self.fact_ids:
            lines.append("FACT_IDS: " + ", ".join(self.fact_ids))
        if self.error:
            lines.append(f"ERROR: {self.error}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
            "artifacts": list(self.artifacts),
            "uncertainties": list(self.uncertainties),
            "requested_context": list(self.requested_context),
            "issue_codes": list(self.issue_codes),
            "fact_ids": list(self.fact_ids),
            "payload": _jsonable(self.payload),
            "error": self.error,
            "usage": dict(self.usage),
            "continuation": _jsonable(self.continuation),
            "coverage": _jsonable(self.coverage),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResultEnvelope":
        return cls(
            task_id=str(data.get("task_id", "")),
            status=data.get("status", STATUS_COMPLETED),
            summary=str(data.get("summary", "")),
            findings=[Finding.from_dict(f) for f in data.get("findings") or []],
            artifacts=[str(a) for a in _as_tuple(data.get("artifacts"))],
            uncertainties=[str(u) for u in _as_tuple(data.get("uncertainties"))],
            requested_context=[str(r) for r in _as_tuple(data.get("requested_context"))],
            issue_codes=[str(i) for i in _as_tuple(data.get("issue_codes"))],
            fact_ids=[str(f) for f in _as_tuple(data.get("fact_ids"))],
            payload=dict(data.get("payload") or {}),
            error=data.get("error"),
            usage={str(k): int(v) for k, v in (data.get("usage") or {}).items()},
            continuation=(
                dict(data["continuation"])
                if isinstance(data.get("continuation"), Mapping)
                else None
            ),
            coverage=[dict(note) for note in (data.get("coverage") or [])],
        )


# ---------------------------------------------------------------------------
# Context bundles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextSection:
    """One candidate fragment for an agent context, before budget packing."""

    section_id: str
    kind: SemanticKind
    text: str
    tokens: int
    ref: str | None = None
    priority: int = 50
    mandatory: bool = False
    tags: tuple[str, ...] = ()

    def with_priority(self, priority: int) -> "ContextSection":
        return ContextSection(
            section_id=self.section_id,
            kind=self.kind,
            text=self.text,
            tokens=self.tokens,
            ref=self.ref,
            priority=priority,
            mandatory=self.mandatory,
            tags=self.tags,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "kind": self.kind,
            "tokens": self.tokens,
            "ref": self.ref,
            "priority": self.priority,
            "mandatory": self.mandatory,
            "tags": list(self.tags),
        }


@dataclass
class BundleEntry:
    section: ContextSection
    included: bool = True
    reason: str = "selected"
    score: float = 0.0
    rendered_text: str | None = None

    @property
    def tokens(self) -> int:
        return self.section.tokens


@dataclass
class ContextBundle:
    """The exact context handed to one agent, plus the packing receipt."""

    agent_id: str
    task_id: str
    entries: list[BundleEntry] = field(default_factory=list)
    budget: int = 0

    @property
    def total_tokens(self) -> int:
        return sum(e.tokens for e in self.entries if e.included)

    @property
    def included_entries(self) -> list[BundleEntry]:
        return [e for e in self.entries if e.included]

    @property
    def omitted_entries(self) -> list[BundleEntry]:
        return [e for e in self.entries if not e.included]

    @property
    def fits(self) -> bool:
        return self.total_tokens <= self.budget

    def refs(self) -> list[str]:
        return [e.section.ref for e in self.included_entries if e.section.ref]

    def text(self) -> str:
        """Render with ref headers so a model can cite exactly what it saw."""
        chunks: list[str] = []
        for entry in self.included_entries:
            section = entry.section
            ref = f" ref={section.ref}" if section.ref else ""
            header = (
                f"[CTX kind={section.kind}{ref} priority={section.priority} "
                f"tokens={section.tokens}]"
            )
            chunks.append(header + "\n" + section.text.strip())
        return "\n\n".join(chunks)

    def cache_text(self) -> str:
        """Render in prefix-cache-friendly order (stable sections first).

        DeepSeek-compatible endpoints cache a *prefix* of the prompt.  The
        normal context order puts the task contract and role-specific system
        section first, so two calls in the same task almost never share a
        cacheable prefix.  This variant keeps the same sections but moves
        stable material (invariants, repository index, source refs) before the
        volatile contract/continuation and the role-specific system block.
        """

        def rank(entry: BundleEntry) -> tuple[int, int]:
            section = entry.section
            section_id = str(section.section_id)
            if section.kind == "invariants":
                # Base invariants are stable; action-triggered ones are volatile
                # and must come after the cacheable repository/source prefix.
                if section_id.startswith("triggered-invariants"):
                    return 4, 0
                return 0, 0
            if section.kind == "evidence":
                ref = section.ref or ""
                # Full exact refs (the run-wide prefix) are stable.  Retrieval
                # activation chunks ("hit:repo/index:...") are volatile and
                # must NOT sit inside the shared prefix.
                if section_id.startswith("cache:"):
                    return 1, 0
                if section_id.startswith("ref:") and "repo/index" in ref:
                    return 1, 0
                if section_id.startswith("ref:"):
                    return 2, 0
                return 5, 0
            if section.kind == "contract":
                return 3, 0
            if section.kind == "continuation":
                return 6, 0
            if section.kind == "dependency":
                return 7, 0
            if section.kind == "memory":
                return 8, 0
            if section.kind == "system":
                return 10, 0
            return 9, 0

        ordered = sorted(
            enumerate(self.included_entries),
            key=lambda pair: (rank(pair[1]), pair[0]),
        )
        chunks: list[str] = []
        for _index, entry in ordered:
            section = entry.section
            ref = f" ref={section.ref}" if section.ref else ""
            header = (
                f"[CTX kind={section.kind}{ref} priority={section.priority} "
                f"tokens={section.tokens}]"
            )
            chunks.append(header + "\n" + section.text.strip())
        return "\n\n".join(chunks)

    def coverage(self, required_refs: Iterable[str]) -> float:
        required = [r for r in required_refs if r]
        if not required:
            return 1.0
        present = set(self.refs())
        hits = sum(1 for r in required if r in present)
        return hits / len(required)

    def report(self) -> dict[str, Any]:
        included = [
            {
                "section_id": e.section.section_id,
                "kind": e.section.kind,
                "ref": e.section.ref,
                "tokens": e.tokens,
                "priority": e.section.priority,
                "reason": e.reason,
            }
            for e in self.included_entries
        ]
        omitted = [
            {
                "section_id": e.section.section_id,
                "kind": e.section.kind,
                "ref": e.section.ref,
                "tokens": e.tokens,
                "reason": e.reason,
            }
            for e in self.omitted_entries
        ]
        return {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "budget": self.budget,
            "total_tokens": self.total_tokens,
            "fits": self.fits,
            "included": included,
            "omitted": omitted,
        }


# ---------------------------------------------------------------------------
# Agents, goals and run results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSpec:
    """Identity and budget of one agent instance in the hierarchy."""

    agent_id: str
    role: Role
    depth: int
    budget: Budget
    parent_id: str | None = None
    domain: str = "general"
    model: str = "scripted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "depth": self.depth,
            "budget": self.budget.to_dict(),
            "parent_id": self.parent_id,
            "domain": self.domain,
            "model": self.model,
        }


@dataclass(frozen=True)
class GoalSpec:
    """The user request entering the system at L0."""

    goal_id: str
    objective: str
    constraints: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    workspace: str | None = None
    risk: Risk = "medium"
    family: str = "generic"
    required_facts: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "objective": self.objective,
            "constraints": list(self.constraints),
            "acceptance": list(self.acceptance),
            "workspace": self.workspace,
            "risk": self.risk,
            "family": self.family,
            "required_facts": list(self.required_facts),
            "metadata": _jsonable(self.metadata),
        }


@dataclass
class RunResult:
    """Uniform result of one system run (HCCA or a baseline)."""

    run_id: str
    system: str
    goal_id: str
    status: Status
    report: str = ""
    artifacts: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    telemetry: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    started_at: str = ""
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "system": self.system,
            "goal_id": self.goal_id,
            "status": self.status,
            "report": self.report,
            "artifacts": list(self.artifacts),
            "metrics": _jsonable(self.metrics),
            "telemetry": _jsonable(self.telemetry),
            "error": self.error,
            "started_at": self.started_at,
            "duration_s": self.duration_s,
        }


__all__ = [
    "AgentSpec",
    "Budget",
    "BundleEntry",
    "ContextBundle",
    "ContextSection",
    "FINISHED_STATUSES",
    "Finding",
    "GoalSpec",
    "Placement",
    "RETRYABLE_STATUSES",
    "ResultEnvelope",
    "Risk",
    "Role",
    "RunResult",
    "STATUS_BLOCKED",
    "STATUS_COMPLETED",
    "STATUS_CONFLICT",
    "STATUS_FAILED",
    "STATUS_NEED_CONTEXT",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "Scope",
    "SemanticKind",
    "Status",
    "TaskContract",
]
