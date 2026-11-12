"""Data model for the HCCA v0.3 multi-agent collaboration layer.

This module is the single source of truth for the collaboration wire format
(``docs/MULTI_AGENT_INTERFACES.md`` section 2).  Every type here is a plain
:class:`dataclasses.dataclass` with ``to_dict()`` / ``from_dict()`` so it can be
persisted as JSON and replayed in tests without a database.

Design notes:

* enums follow the :mod:`hcca.types` style: a ``Literal`` alias plus ``str``
  constants (and a small ``frozenset`` for membership checks);
* all dataclass fields carry defaults, which keeps the field order identical to
  the spec and lets callers construct objects with keyword arguments in any
  order (the store validates the required combinations);
* tuple fields serialise to JSON lists; mapping payloads are normalised with a
  best-effort ``_jsonable`` pass so a ``ContextBuildSpec`` / telemetry dump is
  always JSON-safe.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping

from .errors import HCCAError
from .types import ResultEnvelope

# ---------------------------------------------------------------------------
# Literal aliases (kept dependency-free, mirroring hcca.types)
# ---------------------------------------------------------------------------

MessageKind = Literal[
    "NOTE", "ASK", "ANSWER", "PROPOSE", "CHALLENGE", "DECISION", "INVALIDATE"
]
MessageStatus = Literal["STORED", "DELIVERED", "IN_CONTEXT", "PROCESSED"]
ScopeKind = Literal["ROOM", "THREAD"]
AgentStatus = Literal[
    "CREATED", "QUEUED", "READY", "RUNNING", "WAITING", "CLOSED", "FAILED"
]
WaitKind = Literal["ALL_REQUIRED", "ANY_ACCEPTED", "UNTIL_EVENT"]
SpawnStatus = Literal["CREATED", "QUEUED", "REUSED", "DENIED"]

StepActionType = Literal[
    "SEND_MESSAGE",
    "SPAWN",
    "CANCEL",
    "WAIT",
    "SUBMIT",
    "CLOSE_THREAD",
    "DECIDE",
    "INVALIDATE",
]
YieldKind = Literal[
    "WAIT_FOR_REPLY", "WAIT_FOR_CHILDREN", "WAIT_FOR_EVENT", "DONE", "BLOCKED"
]
JoinPolicy = Literal["REQUIRED", "OPTIONAL"]

# ---- str constants (same spirit as hcca.types.STATUS_*) -------------------

MESSAGE_NOTE: MessageKind = "NOTE"
MESSAGE_ASK: MessageKind = "ASK"
MESSAGE_ANSWER: MessageKind = "ANSWER"
MESSAGE_PROPOSE: MessageKind = "PROPOSE"
MESSAGE_CHALLENGE: MessageKind = "CHALLENGE"
MESSAGE_DECISION: MessageKind = "DECISION"
MESSAGE_INVALIDATE: MessageKind = "INVALIDATE"

MESSAGE_STATUS_STORED: MessageStatus = "STORED"
MESSAGE_STATUS_DELIVERED: MessageStatus = "DELIVERED"
MESSAGE_STATUS_IN_CONTEXT: MessageStatus = "IN_CONTEXT"
MESSAGE_STATUS_PROCESSED: MessageStatus = "PROCESSED"

SCOPE_ROOM: ScopeKind = "ROOM"
SCOPE_THREAD: ScopeKind = "THREAD"

AGENT_CREATED: AgentStatus = "CREATED"
AGENT_QUEUED: AgentStatus = "QUEUED"
AGENT_READY: AgentStatus = "READY"
AGENT_RUNNING: AgentStatus = "RUNNING"
AGENT_WAITING: AgentStatus = "WAITING"
AGENT_CLOSED: AgentStatus = "CLOSED"
AGENT_FAILED: AgentStatus = "FAILED"

WAIT_ALL_REQUIRED: WaitKind = "ALL_REQUIRED"
WAIT_ANY_ACCEPTED: WaitKind = "ANY_ACCEPTED"
WAIT_UNTIL_EVENT: WaitKind = "UNTIL_EVENT"

SPAWN_CREATED: SpawnStatus = "CREATED"
SPAWN_QUEUED: SpawnStatus = "QUEUED"
SPAWN_REUSED: SpawnStatus = "REUSED"
SPAWN_DENIED: SpawnStatus = "DENIED"

# ---- orderings / sets used by the store and the runtime -------------------

#: Message statuses in their legal forward order (state machine).
MESSAGE_STATUS_ORDER: dict[MessageStatus, int] = {
    MESSAGE_STATUS_STORED: 0,
    MESSAGE_STATUS_DELIVERED: 1,
    MESSAGE_STATUS_IN_CONTEXT: 2,
    MESSAGE_STATUS_PROCESSED: 3,
}

#: Kinds that must be considered before NOTE / PROPOSE / CHALLENGE when the
#: context budget is packed (spec 3.1 ``select_for_context``).
IMPORTANT_MESSAGE_KINDS: frozenset[MessageKind] = frozenset(
    {MESSAGE_ASK, MESSAGE_ANSWER, MESSAGE_INVALIDATE, MESSAGE_DECISION}
)

MESSAGE_KINDS: frozenset[MessageKind] = frozenset(
    {
        MESSAGE_NOTE,
        MESSAGE_ASK,
        MESSAGE_ANSWER,
        MESSAGE_PROPOSE,
        MESSAGE_CHALLENGE,
        MESSAGE_DECISION,
        MESSAGE_INVALIDATE,
    }
)
MESSAGE_STATUSES: frozenset[MessageStatus] = frozenset(MESSAGE_STATUS_ORDER)
SCOPE_KINDS: frozenset[ScopeKind] = frozenset({SCOPE_ROOM, SCOPE_THREAD})
AGENT_STATUSES: frozenset[AgentStatus] = frozenset(
    {
        AGENT_CREATED,
        AGENT_QUEUED,
        AGENT_READY,
        AGENT_RUNNING,
        AGENT_WAITING,
        AGENT_CLOSED,
        AGENT_FAILED,
    }
)
WAIT_KINDS: frozenset[WaitKind] = frozenset(
    {WAIT_ALL_REQUIRED, WAIT_ANY_ACCEPTED, WAIT_UNTIL_EVENT}
)
SPAWN_STATUSES: frozenset[SpawnStatus] = frozenset(
    {SPAWN_CREATED, SPAWN_QUEUED, SPAWN_REUSED, SPAWN_DENIED}
)
STEP_ACTION_TYPES: frozenset[StepActionType] = frozenset(
    {
        "SEND_MESSAGE",
        "SPAWN",
        "CANCEL",
        "WAIT",
        "SUBMIT",
        "CLOSE_THREAD",
        "DECIDE",
        "INVALIDATE",
    }
)
YIELD_KINDS: frozenset[YieldKind] = frozenset(
    {"WAIT_FOR_REPLY", "WAIT_FOR_CHILDREN", "WAIT_FOR_EVENT", "DONE", "BLOCKED"}
)

#: Terminal agent statuses (an agent in one of these cannot run any more).
TERMINAL_AGENT_STATUSES: frozenset[AgentStatus] = frozenset({AGENT_CLOSED, AGENT_FAILED})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ContextBudgetError(HCCAError):
    """Raised when even one mandatory message cannot fit the context budget.

    The error is defined here (and re-exported by :mod:`hcca.collaboration`) so
    both modules are valid import sites for the runtime.  The offending message
    is *not* dropped: it is still pending in the mailbox when this is raised
    (acceptance test #8).
    """

    def __init__(
        self,
        message: str,
        *,
        message_id: str = "",
        required_tokens: int = 0,
        budget_tokens: int = 0,
    ) -> None:
        super().__init__(message)
        self.message_id = message_id
        self.required_tokens = int(required_tokens)
        self.budget_tokens = int(budget_tokens)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _as_tuple(value: Any) -> tuple[Any, ...]:
    """Coerce a scalar/list/set/None value into a tuple."""
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, (list, set, frozenset)):
        return tuple(value)
    return (value,)


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in _as_tuple(value))


def _as_list_of_str(value: Any) -> list[str]:
    return [str(item) for item in _as_tuple(value)]


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int_map(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _as_int(item) for key, item in value.items()}


def _jsonable(value: Any) -> Any:
    """Best-effort conversion to JSON-serialisable Python objects."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return str(value)


# ---------------------------------------------------------------------------
# Message: the persistent event (stored exactly once)
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """One collaboration message.

    A message is a *persistent event*: it is stored once and then projected
    into each recipient's mailbox.  ``status`` on this object is an aggregate
    view; the authoritative per-mailbox status lives in ``CollaborationStore``
    (acceptance test #9) and is what ``to_dict`` records for a stored message.
    """

    message_id: str = ""
    kind: MessageKind = MESSAGE_NOTE
    sender: str = ""
    recipients: tuple[str, ...] = ()
    scope_id: str = ""
    thread_id: str | None = None
    body: str = ""
    subject: str = ""
    based_on: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    reply_requirement: tuple[str, ...] = ()
    reply_to: str | None = None
    idempotency_key: str = ""
    created_at: str = ""
    status: MessageStatus = MESSAGE_STATUS_STORED
    priority: int = 50
    ttl_steps: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_mandatory(self) -> bool:
        """A message the context selector must not silently drop."""
        return bool(self.reply_requirement) or self.kind == MESSAGE_INVALIDATE

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "kind": self.kind,
            "sender": self.sender,
            "recipients": list(self.recipients),
            "scope_id": self.scope_id,
            "thread_id": self.thread_id,
            "body": self.body,
            "subject": self.subject,
            "based_on": _jsonable(self.based_on),
            "evidence_refs": list(self.evidence_refs),
            "reply_requirement": list(self.reply_requirement),
            "reply_to": self.reply_to,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
            "status": self.status,
            "priority": int(self.priority),
            "ttl_steps": self.ttl_steps,
            "metadata": _jsonable(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "Message":
        data = dict(data or {})
        return cls(
            message_id=str(data.get("message_id", "")),
            kind=data.get("kind") or MESSAGE_NOTE,
            sender=str(data.get("sender", "")),
            recipients=_as_str_tuple(data.get("recipients")),
            scope_id=str(data.get("scope_id", "")),
            thread_id=(str(data["thread_id"]) if data.get("thread_id") is not None else None),
            body=str(data.get("body", "")),
            subject=str(data.get("subject", "")),
            based_on=_as_mapping(data.get("based_on")),
            evidence_refs=_as_str_tuple(data.get("evidence_refs")),
            reply_requirement=_as_str_tuple(data.get("reply_requirement")),
            reply_to=(str(data["reply_to"]) if data.get("reply_to") is not None else None),
            idempotency_key=str(data.get("idempotency_key", "")),
            created_at=str(data.get("created_at", "")),
            status=data.get("status") or MESSAGE_STATUS_STORED,
            priority=_as_int(data.get("priority"), 50),
            ttl_steps=(
                _as_int(data["ttl_steps"]) if data.get("ttl_steps") is not None else None
            ),
            metadata=_as_mapping(data.get("metadata")),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Scope and thread state
# ---------------------------------------------------------------------------


@dataclass
class Scope:
    """A collaboration room (``ROOM``) or an authorised topic room (``THREAD``)."""

    scope_id: str = ""
    kind: ScopeKind = SCOPE_ROOM
    owner: str = ""
    members: tuple[str, ...] = ()
    parent_scope: str | None = None
    allowed_artifacts: tuple[str, ...] = ()
    allowed_threads: tuple[str, ...] = ()
    open: bool = True
    close_condition: str = ""
    created_at: str = ""

    def is_member(self, agent_id: str) -> bool:
        return agent_id in self.members

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "kind": self.kind,
            "owner": self.owner,
            "members": list(self.members),
            "parent_scope": self.parent_scope,
            "allowed_artifacts": list(self.allowed_artifacts),
            "allowed_threads": list(self.allowed_threads),
            "open": bool(self.open),
            "close_condition": self.close_condition,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "Scope":
        data = dict(data or {})
        return cls(
            scope_id=str(data.get("scope_id", "")),
            kind=data.get("kind") or SCOPE_ROOM,
            owner=str(data.get("owner", "")),
            members=_as_str_tuple(data.get("members")),
            parent_scope=(
                str(data["parent_scope"]) if data.get("parent_scope") is not None else None
            ),
            allowed_artifacts=_as_str_tuple(data.get("allowed_artifacts")),
            allowed_threads=_as_str_tuple(data.get("allowed_threads")),
            open=bool(data.get("open", True)),
            close_condition=str(data.get("close_condition", "")),
            created_at=str(data.get("created_at", "")),
        )


@dataclass
class ThreadState:
    """One open question (topic) and its resolution state."""

    thread_id: str = ""
    scope_id: str = ""
    question: str = ""
    owner: str = ""
    candidates: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    close_condition: str = ""
    decision: str | None = None
    decided_by: str | None = None
    closed: bool = False
    message_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "scope_id": self.scope_id,
            "question": self.question,
            "owner": self.owner,
            "candidates": list(self.candidates),
            "unresolved": list(self.unresolved),
            "required_evidence": list(self.required_evidence),
            "close_condition": self.close_condition,
            "decision": self.decision,
            "decided_by": self.decided_by,
            "closed": bool(self.closed),
            "message_ids": list(self.message_ids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ThreadState":
        data = dict(data or {})
        return cls(
            thread_id=str(data.get("thread_id", "")),
            scope_id=str(data.get("scope_id", "")),
            question=str(data.get("question", "")),
            owner=str(data.get("owner", "")),
            candidates=_as_list_of_str(data.get("candidates")),
            unresolved=_as_list_of_str(data.get("unresolved")),
            required_evidence=_as_list_of_str(data.get("required_evidence")),
            close_condition=str(data.get("close_condition", "")),
            decision=(str(data["decision"]) if data.get("decision") is not None else None),
            decided_by=(
                str(data["decided_by"]) if data.get("decided_by") is not None else None
            ),
            closed=bool(data.get("closed", False)),
            message_ids=_as_list_of_str(data.get("message_ids")),
        )


# ---------------------------------------------------------------------------
# Runtime handles and spawn protocol
# ---------------------------------------------------------------------------


@dataclass
class AgentHandle:
    """Stable identity of an agent (it is *not* a live model session)."""

    agent_id: str = ""
    task_id: str = ""
    role: str = ""
    parent_id: str | None = None
    depth: int = 0
    status: AgentStatus = AGENT_CREATED
    mailbox_id: str = ""
    alloc_id: str = ""
    waiting_on: "WaitCondition | None" = None
    continuation: Mapping[str, Any] = field(default_factory=dict)
    children: tuple[str, ...] = ()
    generation: int = 0
    activation_token: str | None = None
    closed_reason: str = ""

    def is_active(self) -> bool:
        """True while the agent can still run or receive work.

        ``CLOSED`` and ``FAILED`` are terminal; every other status (including
        ``CREATED`` / ``QUEUED``, which are waiting in the supervisor queue) is
        considered active.
        """
        return self.status not in TERMINAL_AGENT_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "role": self.role,
            "parent_id": self.parent_id,
            "depth": int(self.depth),
            "status": self.status,
            "mailbox_id": self.mailbox_id,
            "alloc_id": self.alloc_id,
            "waiting_on": (
                self.waiting_on.to_dict() if self.waiting_on is not None else None
            ),
            "continuation": _jsonable(self.continuation),
            "children": list(self.children),
            "generation": int(self.generation),
            "activation_token": self.activation_token,
            "closed_reason": self.closed_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "AgentHandle":
        data = dict(data or {})
        waiting = data.get("waiting_on")
        if isinstance(waiting, WaitCondition):
            waiting_condition: WaitCondition | None = waiting
        elif isinstance(waiting, Mapping):
            waiting_condition = WaitCondition.from_dict(waiting)
        else:
            waiting_condition = None
        return cls(
            agent_id=str(data.get("agent_id", "")),
            task_id=str(data.get("task_id", "")),
            role=str(data.get("role", "")),
            parent_id=(
                str(data["parent_id"]) if data.get("parent_id") is not None else None
            ),
            depth=_as_int(data.get("depth")),
            status=data.get("status") or AGENT_CREATED,
            mailbox_id=str(data.get("mailbox_id", "")),
            alloc_id=str(data.get("alloc_id", "")),
            waiting_on=waiting_condition,
            continuation=_as_mapping(data.get("continuation")),
            children=_as_str_tuple(data.get("children")),
            generation=_as_int(data.get("generation")),
            activation_token=(
                str(data["activation_token"])
                if data.get("activation_token") is not None
                else None
            ),
            closed_reason=str(data.get("closed_reason", "")),
        )


@dataclass
class SpawnRequest:
    """A model's request to create a child agent; the supervisor decides."""

    request_id: str = ""
    parent_agent: str = ""
    objective: str = ""
    task_kind: str = "analyze"
    input_refs: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    join_parent_room: bool = True
    allowed_threads: tuple[str, ...] = ()
    alloc_ref: str = ""
    budget: Mapping[str, int] = field(default_factory=dict)
    max_children: int = 0
    join_policy: JoinPolicy = "REQUIRED"
    risk: str = "medium"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "parent_agent": self.parent_agent,
            "objective": self.objective,
            "task_kind": self.task_kind,
            "input_refs": list(self.input_refs),
            "acceptance": list(self.acceptance),
            "constraints": list(self.constraints),
            "capabilities": list(self.capabilities),
            "join_parent_room": bool(self.join_parent_room),
            "allowed_threads": list(self.allowed_threads),
            "alloc_ref": self.alloc_ref,
            "budget": {str(k): _as_int(v) for k, v in self.budget.items()},
            "max_children": int(self.max_children),
            "join_policy": self.join_policy,
            "risk": self.risk,
            "metadata": _jsonable(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "SpawnRequest":
        data = dict(data or {})
        return cls(
            request_id=str(data.get("request_id", "")),
            parent_agent=str(data.get("parent_agent", "")),
            objective=str(data.get("objective", "")),
            task_kind=str(data.get("task_kind", "analyze")),
            input_refs=_as_str_tuple(data.get("input_refs")),
            acceptance=_as_str_tuple(data.get("acceptance")),
            constraints=_as_str_tuple(data.get("constraints")),
            capabilities=_as_str_tuple(data.get("capabilities")),
            join_parent_room=bool(data.get("join_parent_room", True)),
            allowed_threads=_as_str_tuple(data.get("allowed_threads")),
            alloc_ref=str(data.get("alloc_ref", "")),
            budget=_as_int_map(data.get("budget")),
            max_children=_as_int(data.get("max_children")),
            join_policy=data.get("join_policy") or "REQUIRED",
            risk=str(data.get("risk", "medium")),
            metadata=_as_mapping(data.get("metadata")),
        )


@dataclass
class SpawnDecision:
    """The supervisor's verdict on one :class:`SpawnRequest`."""

    request_id: str = ""
    status: SpawnStatus = SPAWN_CREATED
    agent_id: str | None = None
    reason: str = ""
    allocation: Mapping[str, int] = field(default_factory=dict)
    reused: bool = False

    @property
    def approved(self) -> bool:
        return self.status in (SPAWN_CREATED, SPAWN_QUEUED, SPAWN_REUSED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "agent_id": self.agent_id,
            "reason": self.reason,
            "allocation": {str(k): _as_int(v) for k, v in self.allocation.items()},
            "reused": bool(self.reused),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "SpawnDecision":
        data = dict(data or {})
        return cls(
            request_id=str(data.get("request_id", "")),
            status=data.get("status") or SPAWN_DENIED,
            agent_id=(str(data["agent_id"]) if data.get("agent_id") is not None else None),
            reason=str(data.get("reason", "")),
            allocation=_as_int_map(data.get("allocation")),
            reused=bool(data.get("reused", False)),
        )


@dataclass
class WaitCondition:
    """What an agent is waiting for (agent ids / task ids / message ids)."""

    kind: WaitKind = WAIT_UNTIL_EVENT
    refs: tuple[str, ...] = ()
    event_kinds: tuple[str, ...] = ()
    deadline_s: float | None = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "refs": list(self.refs),
            "event_kinds": list(self.event_kinds),
            "deadline_s": self.deadline_s,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "WaitCondition":
        data = dict(data or {})
        deadline = data.get("deadline_s")
        return cls(
            kind=data.get("kind") or WAIT_UNTIL_EVENT,
            refs=_as_str_tuple(data.get("refs")),
            event_kinds=_as_str_tuple(data.get("event_kinds")),
            deadline_s=(_as_float(deadline) if deadline is not None else None),
            description=str(data.get("description", "")),
        )


@dataclass
class BudgetAllocation:
    """One node of the supervisor's budget ledger.

    ``granted`` holds the per-resource caps (``input``/``output``/``tokens``/
    ``calls``); ``spent`` and ``reserved`` are measured in **tokens** because
    tokens are the common currency of the ledger (spec 2).  The invariant is
    ``spent + reserved + remaining == granted["tokens"]`` and a child's
    allocation must never exceed the parent's ``remaining``.
    """

    alloc_id: str = ""
    parent: str | None = None
    granted: Mapping[str, int] = field(default_factory=dict)
    reserved: int = 0
    spent: int = 0
    children: tuple[str, ...] = ()

    @property
    def tokens_granted(self) -> int:
        return _as_int(self.granted.get("tokens"), 0)

    @property
    def remaining(self) -> int:
        """Unreserved, unspent tokens on this node."""
        return self.tokens_granted - int(self.spent) - int(self.reserved)

    def remaining_by_resource(self) -> dict[str, int]:
        """The granted caps with the token budget adjusted for spend/reserve."""
        out = {str(key): _as_int(value) for key, value in self.granted.items()}
        out["tokens"] = self.remaining
        return out

    def validate(self) -> None:
        """Check the local ledger invariant; raise ``ValueError`` on violation."""
        if int(self.spent) < 0:
            raise ValueError(f"alloc {self.alloc_id}: spent must be >= 0")
        if int(self.reserved) < 0:
            raise ValueError(f"alloc {self.alloc_id}: reserved must be >= 0")
        if int(self.spent) + int(self.reserved) > self.tokens_granted:
            raise ValueError(
                f"alloc {self.alloc_id}: spent({self.spent}) + reserved({self.reserved}) "
                f"exceeds granted tokens({self.tokens_granted})"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "alloc_id": self.alloc_id,
            "parent": self.parent,
            "granted": {str(k): _as_int(v) for k, v in self.granted.items()},
            "reserved": int(self.reserved),
            "spent": int(self.spent),
            "children": list(self.children),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "BudgetAllocation":
        data = dict(data or {})
        return cls(
            alloc_id=str(data.get("alloc_id", "")),
            parent=(str(data["parent"]) if data.get("parent") is not None else None),
            granted=_as_int_map(data.get("granted")),
            reserved=_as_int(data.get("reserved")),
            spent=_as_int(data.get("spent")),
            children=_as_str_tuple(data.get("children")),
        )


# ---------------------------------------------------------------------------
# One model-call output: actions + continuation + yield + final result
# ---------------------------------------------------------------------------


@dataclass
class StepAction:
    """A single action proposed by the model.

    The ``type`` discriminator selects the payload schema; the payload itself is
    kept in ``data`` (a mapping) instead of using inheritance, which keeps the
    runtime's validation table small and the wire format stable.  The named
    constructors below are the only place the runtime needs to know the schema.
    """

    type: StepActionType = "SEND_MESSAGE"
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": _jsonable(self.data)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "StepAction":
        data = dict(data or {})
        action_type = data.get("type") or "SEND_MESSAGE"
        payload = data.get("data")
        if payload is None:  # tolerate a flat ``{"type": ..., ...}`` dict
            payload = {key: value for key, value in data.items() if key != "type"}
        return cls(type=action_type, data=_jsonable(payload))

    @classmethod
    def of(cls, action_type: StepActionType, **data: Any) -> "StepAction":
        """Build an action of ``action_type`` with a free-form payload."""
        return cls(type=action_type, data=_jsonable(data))

    # -- named constructors (spec 2) -------------------------------------
    @classmethod
    def send_message(
        cls,
        recipients: str | Iterable[str] = (),
        body: str = "",
        *,
        kind: MessageKind = MESSAGE_NOTE,
        scope_id: str = "",
        thread_id: str | None = None,
        subject: str = "",
        based_on: Mapping[str, Any] | None = None,
        evidence_refs: Iterable[str] = (),
        reply_requirement: Iterable[str] = (),
        reply_to: str | None = None,
        idempotency_key: str = "",
        priority: int = 50,
        metadata: Mapping[str, Any] | None = None,
    ) -> "StepAction":
        if isinstance(recipients, str):
            recipients = (recipients,)
        data: dict[str, Any] = {
            "recipients": [str(item) for item in recipients],
            "body": str(body),
            "kind": kind,
            "scope_id": str(scope_id),
            "thread_id": thread_id,
            "subject": str(subject),
            "based_on": _jsonable(based_on or {}),
            "evidence_refs": [str(item) for item in evidence_refs],
            "reply_requirement": [str(item) for item in reply_requirement],
            "reply_to": reply_to,
            "idempotency_key": str(idempotency_key),
            "priority": int(priority),
            "metadata": _jsonable(metadata or {}),
        }
        return cls(type="SEND_MESSAGE", data=data)

    @classmethod
    def spawn(
        cls,
        objective: str,
        *,
        task_kind: str = "analyze",
        input_refs: Iterable[str] = (),
        acceptance: Iterable[str] = (),
        constraints: Iterable[str] = (),
        capabilities: Iterable[str] = (),
        join_parent_room: bool = True,
        allowed_threads: Iterable[str] = (),
        budget: Mapping[str, int] | None = None,
        max_children: int = 0,
        join_policy: JoinPolicy = "REQUIRED",
        risk: str = "medium",
        metadata: Mapping[str, Any] | None = None,
    ) -> "StepAction":
        data: dict[str, Any] = {
            "objective": str(objective),
            "task_kind": str(task_kind),
            "input_refs": [str(item) for item in input_refs],
            "acceptance": [str(item) for item in acceptance],
            "constraints": [str(item) for item in constraints],
            "capabilities": [str(item) for item in capabilities],
            "join_parent_room": bool(join_parent_room),
            "allowed_threads": [str(item) for item in allowed_threads],
            "max_children": int(max_children),
            "join_policy": join_policy,
            "risk": str(risk),
            "metadata": _jsonable(metadata or {}),
        }
        if budget is not None:
            data["budget"] = {str(k): _as_int(v) for k, v in budget.items()}
        return cls(type="SPAWN", data=data)

    @classmethod
    def wait(
        cls,
        *,
        kind: WaitKind = WAIT_UNTIL_EVENT,
        refs: Iterable[str] = (),
        event_kinds: Iterable[str] = (),
        deadline_s: float | None = None,
        description: str = "",
    ) -> "StepAction":
        return cls(
            type="WAIT",
            data={
                "kind": kind,
                "refs": [str(item) for item in refs],
                "event_kinds": [str(item) for item in event_kinds],
                "deadline_s": deadline_s,
                "description": str(description),
            },
        )

    @classmethod
    def submit(
        cls,
        result: ResultEnvelope | Mapping[str, Any],
        *,
        continuation: Mapping[str, Any] | None = None,
    ) -> "StepAction":
        if isinstance(result, ResultEnvelope):
            payload = result.to_dict()
        elif isinstance(result, Mapping):
            payload = _jsonable(result)
        else:
            raise TypeError("submit() expects a ResultEnvelope or a mapping")
        data: dict[str, Any] = {"result": payload}
        if continuation is not None:
            data["continuation"] = _jsonable(continuation)
        return cls(type="SUBMIT", data=data)


@dataclass
class YieldSpec:
    """How the current activation should end (wait / done / blocked)."""

    kind: YieldKind = "DONE"
    refs: tuple[str, ...] = ()
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "refs": list(self.refs), "note": self.note}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "YieldSpec":
        data = dict(data or {})
        return cls(
            kind=data.get("kind") or "DONE",
            refs=_as_str_tuple(data.get("refs")),
            note=str(data.get("note", "")),
        )


@dataclass
class AgentStep:
    """Everything a single model call returns to the runtime."""

    actions: list[StepAction] = field(default_factory=list)
    continuation: Mapping[str, Any] = field(default_factory=dict)
    yield_spec: YieldSpec | None = None
    final_result: ResultEnvelope | None = None
    usage: Mapping[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": [action.to_dict() for action in self.actions],
            "continuation": _jsonable(self.continuation),
            "yield_spec": (
                self.yield_spec.to_dict() if self.yield_spec is not None else None
            ),
            "final_result": (
                self.final_result.to_dict() if self.final_result is not None else None
            ),
            "usage": {str(k): _as_int(v) for k, v in self.usage.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "AgentStep":
        data = dict(data or {})
        raw_actions = data.get("actions") or []
        actions: list[StepAction] = []
        for raw in raw_actions:
            if isinstance(raw, StepAction):
                actions.append(raw)
            elif isinstance(raw, Mapping):
                actions.append(StepAction.from_dict(raw))
        yield_raw = data.get("yield_spec")
        if isinstance(yield_raw, YieldSpec):
            yield_spec: YieldSpec | None = yield_raw
        elif isinstance(yield_raw, Mapping):
            yield_spec = YieldSpec.from_dict(yield_raw)
        else:
            yield_spec = None
        result_raw = data.get("final_result")
        if isinstance(result_raw, ResultEnvelope):
            final_result: ResultEnvelope | None = result_raw
        elif isinstance(result_raw, Mapping):
            final_result = ResultEnvelope.from_dict(result_raw)
        else:
            final_result = None
        return cls(
            actions=actions,
            continuation=_as_mapping(data.get("continuation")),
            yield_spec=yield_spec,
            final_result=final_result,
            usage=_as_int_map(data.get("usage")),
        )


# ---------------------------------------------------------------------------
# Introspection helpers (used by reports / tests)
# ---------------------------------------------------------------------------


def collab_model_classes() -> tuple[type, ...]:
    """Every serialisable collaboration dataclass defined in this module."""
    return (
        Message,
        Scope,
        ThreadState,
        AgentHandle,
        SpawnRequest,
        SpawnDecision,
        WaitCondition,
        BudgetAllocation,
        StepAction,
        YieldSpec,
        AgentStep,
    )


def dataclass_field_names(cls: type) -> tuple[str, ...]:
    """Field names of a collaboration dataclass (telemetry / schema checks)."""
    return tuple(field.name for field in dataclasses.fields(cls))


__all__ = [
    "AGENT_CLOSED",
    "AGENT_CREATED",
    "AGENT_FAILED",
    "AGENT_QUEUED",
    "AGENT_READY",
    "AGENT_RUNNING",
    "AGENT_STATUSES",
    "AGENT_WAITING",
    "AgentHandle",
    "AgentStatus",
    "AgentStep",
    "BudgetAllocation",
    "ContextBudgetError",
    "IMPORTANT_MESSAGE_KINDS",
    "JoinPolicy",
    "MESSAGE_ANSWER",
    "MESSAGE_ASK",
    "MESSAGE_CHALLENGE",
    "MESSAGE_DECISION",
    "MESSAGE_INVALIDATE",
    "MESSAGE_KINDS",
    "MESSAGE_NOTE",
    "MESSAGE_PROPOSE",
    "MESSAGE_STATUSES",
    "MESSAGE_STATUS_DELIVERED",
    "MESSAGE_STATUS_IN_CONTEXT",
    "MESSAGE_STATUS_ORDER",
    "MESSAGE_STATUS_PROCESSED",
    "MESSAGE_STATUS_STORED",
    "Message",
    "MessageKind",
    "MessageStatus",
    "ResultEnvelope",
    "SCOPE_KINDS",
    "SCOPE_ROOM",
    "SCOPE_THREAD",
    "SPAWN_CREATED",
    "SPAWN_DENIED",
    "SPAWN_QUEUED",
    "SPAWN_REUSED",
    "SPAWN_STATUSES",
    "STEP_ACTION_TYPES",
    "Scope",
    "ScopeKind",
    "SpawnDecision",
    "SpawnRequest",
    "SpawnStatus",
    "StepAction",
    "StepActionType",
    "TERMINAL_AGENT_STATUSES",
    "ThreadState",
    "WAIT_ALL_REQUIRED",
    "WAIT_ANY_ACCEPTED",
    "WAIT_KINDS",
    "WAIT_UNTIL_EVENT",
    "WaitCondition",
    "WaitKind",
    "YIELD_KINDS",
    "YieldKind",
    "YieldSpec",
    "collab_model_classes",
    "dataclass_field_names",
]
