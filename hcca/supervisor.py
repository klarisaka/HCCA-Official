"""Supervision tree: spawn admission, budget allocation and cancellation.

This module implements section 3.2 of ``docs/MULTI_AGENT_INTERFACES.md``.  The
supervisor is the only component allowed to create agents, grant budget and
propagate cancellation.  It deliberately knows nothing about the model: a spawn
is admitted here, executed by the :mod:`hcca.scheduler` / :mod:`hcca.collab_runtime`
pair.

Design notes
------------
* **Admission is centralised.**  Every check (idempotency, contract shape,
  capability subset, budget, fan-out/depth/global caps) lives in :meth:`spawn`
  in the order required by the spec, and each denial carries a concrete reason.
* **The budget ledger is the :class:`BudgetAllocation` object** from
  ``hcca.collab_types``.  ``granted`` is a per-resource mapping, while
  ``reserved``/``spent`` are denominated in the primary unit (tokens).  The
  conservation invariant is ``spent + reserved <= granted["tokens"]``; the
  uncommitted remainder is ``remaining``.
* **The collaboration store is optional.**  When one is supplied the supervisor
  also materialises the default local room (parent + direct children) so the
  runtime has somewhere to send messages.  Without a store the supervisor is
  fully usable and testable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

from .collab_types import AgentHandle, BudgetAllocation, SpawnDecision, SpawnRequest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .collaboration import CollaborationStore

__all__ = [
    "ACTIVE_STATUSES",
    "Supervisor",
    "SupervisorConfig",
    "SpawnDenied",
]

#: Every status in which an agent still occupies a supervision slot.
ACTIVE_STATUSES = frozenset({"CREATED", "QUEUED", "READY", "RUNNING", "WAITING"})
#: Statuses that count towards ``max_active`` (queued agents wait for a slot).
CAPACITY_STATUSES = frozenset({"CREATED", "READY", "RUNNING", "WAITING"})
#: Statuses that end an agent forever.
TERMINAL_STATUSES = frozenset({"CLOSED", "FAILED"})

#: Primary resource unit of the budget ledger.
BUDGET_UNIT = "tokens"


class SpawnDenied(Exception):
    """Raised when a spawn request cannot be admitted (carries the reason)."""

    def __init__(self, reason: str, *, request_id: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.request_id = request_id


@dataclass
class SupervisorConfig:
    """Static limits of one supervision tree.

    ``delegable_capabilities`` maps a parent agent id (or role) to the set of
    capabilities it may delegate.  ``None`` means "no capability filtering",
    which is the default for a trusted in-process runtime.
    """

    max_children_per_agent: int = 4
    max_depth: int = 3
    max_active: int = 8
    max_total_agents: int = 32
    retry_budget: int = 8
    default_budget: Mapping[str, int] = field(
        default_factory=lambda: {"tokens": 2000, "calls": 4}
    )
    reserve_ratio: float = 0.1
    delegable_capabilities: Mapping[str, tuple[str, ...]] | None = None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _budget_tokens(budget: Mapping[str, Any] | None) -> int:
    """Convert an input/output/budget mapping into a primary-unit cost."""
    budget = dict(budget or {})
    if "tokens" in budget:
        return max(0, int(budget.get("tokens") or 0))
    if "input" in budget or "output" in budget:
        return max(0, int(budget.get("input") or 0)) + max(
            0, int(budget.get("output") or 0)
        )
    return 0


def _as_dict(value: Any) -> dict[str, Any]:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        return dict(result) if isinstance(result, Mapping) else {}
    if isinstance(value, Mapping):
        return dict(value)
    return {}


class Supervisor:
    """Admission and budget controller for one collaboration run."""

    def __init__(
        self,
        config: SupervisorConfig | None = None,
        *,
        store: "CollaborationStore | None" = None,
    ) -> None:
        self.config = config or SupervisorConfig()
        self.store = store
        #: Live agent handles keyed by agent id (insertion ordered).
        self.handles: dict[str, AgentHandle] = {}
        #: Budget ledger keyed by allocation id.
        self.allocations: dict[str, BudgetAllocation] = {}
        #: Every spawn decision ever made, keyed by request id (idempotency).
        self.decisions: dict[str, SpawnDecision] = {}
        self._agent_alloc: dict[str, str] = {}
        self._children: dict[str, list[str]] = {}
        self._child_caps: dict[str, int] = {}
        self._join_policies: dict[str, str] = {}
        self._rooms: dict[str, str] = {}
        self._alloc_seq = 0
        self._agent_seq = 0
        self._req_seq = 0
        self._queued: list[str] = []
        #: ``calls`` spent per allocation (``spent`` tracks tokens only).
        self._calls_spent: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Lookup / convenience accessors
    # ------------------------------------------------------------------
    @property
    def agents(self) -> dict[str, AgentHandle]:
        """Alias of :attr:`handles` (the spec calls them agent handles)."""
        return self.handles

    def agent(self, agent_id: str) -> AgentHandle:
        try:
            return self.handles[agent_id]
        except KeyError:
            raise KeyError(f"unknown agent {agent_id!r}") from None

    def allocation(self, agent_id: str) -> BudgetAllocation:
        try:
            return self.allocations[self._agent_alloc[agent_id]]
        except KeyError:
            raise KeyError(f"unknown agent {agent_id!r}") from None

    def alloc_id_of(self, agent_id: str) -> str:
        try:
            return self._agent_alloc[agent_id]
        except KeyError:
            raise KeyError(f"unknown agent {agent_id!r}") from None

    def children_of(self, agent_id: str) -> list[str]:
        self.agent(agent_id)
        return list(self._children.get(agent_id, ()))

    def parent_of(self, agent_id: str) -> str | None:
        return self.agent(agent_id).parent_id

    def active_count(self) -> int:
        return sum(
            1 for handle in self.handles.values() if handle.status in CAPACITY_STATUSES
        )

    def remaining(self, agent_id: str) -> int:
        """Uncommitted primary-unit budget still available to ``agent_id``."""
        alloc = self.allocation(agent_id)
        return self._remaining(alloc)

    def _remaining(self, alloc: BudgetAllocation) -> int:
        granted = int(dict(alloc.granted or {}).get(BUDGET_UNIT, 0))
        return max(0, granted - int(alloc.spent) - int(alloc.reserved))

    def _known(self, agent_id: str) -> bool:
        return agent_id in self.handles

    # ------------------------------------------------------------------
    # Root registration
    # ------------------------------------------------------------------
    def register_root(
        self, handle: AgentHandle, granted: Mapping[str, int]
    ) -> str:
        """Register the single root agent and open its budget account.

        Returns the new ``alloc_id``.  The handle is mutated in place (its
        ``alloc_id`` field is filled in) because ``AgentHandle`` is the runtime's
        mutable identity object.
        """
        if handle.parent_id is not None:
            raise ValueError("root agent must not have a parent")
        if handle.depth != 0:
            raise ValueError("root agent must be at depth 0")
        if handle.agent_id in self.handles:
            raise ValueError(f"duplicate root agent id {handle.agent_id!r}")
        alloc = self._new_allocation(parent=None, granted=granted)
        handle.alloc_id = alloc.alloc_id
        if handle.status in ("", "CREATED"):
            handle.status = "READY"
        self.handles[handle.agent_id] = handle
        self._agent_alloc[handle.agent_id] = alloc.alloc_id
        self._children[handle.agent_id] = []
        self._child_caps[handle.agent_id] = int(self.config.max_children_per_agent)
        self._join_policies[handle.agent_id] = "REQUIRED"
        # Root room: the default local collaboration space (owner + direct kids).
        self.ensure_room(handle.agent_id)
        return alloc.alloc_id

    # ------------------------------------------------------------------
    # Spawn adjudication
    # ------------------------------------------------------------------
    def spawn(self, request: SpawnRequest) -> SpawnDecision:
        """Adjudicate one spawn request; never raises for a denial."""
        request_id = str(getattr(request, "request_id", "") or "")
        if request_id and request_id in self.decisions:
            return self._reuse_decision(request_id)
        decision = self._decide(request)
        if request_id:
            self.decisions[request_id] = decision
        return decision

    def _reuse_decision(self, request_id: str) -> SpawnDecision:
        original = self.decisions[request_id]
        if original.status in ("CREATED", "QUEUED"):
            return SpawnDecision(
                request_id=request_id,
                status="REUSED",
                agent_id=original.agent_id,
                reason=(
                    f"duplicate request_id {request_id!r}: already "
                    f"{original.status.lower()}"
                ),
                allocation=dict(original.allocation or {}),
                reused=True,
            )
        return original

    def _decide(self, request: SpawnRequest) -> SpawnDecision:
        request_id = str(getattr(request, "request_id", "") or "")
        parent_id = str(getattr(request, "parent_agent", "") or "")

        def deny(reason: str) -> SpawnDecision:
            return SpawnDecision(
                request_id=request_id,
                status="DENIED",
                agent_id=None,
                reason=reason,
                allocation={},
                reused=False,
            )

        # 1. Idempotency was handled by the caller (spawn()).
        # 2. Contract completeness.
        objective = str(getattr(request, "objective", "") or "").strip()
        if not objective:
            return deny("contract incomplete: objective must be non-empty")
        input_refs = tuple(str(ref) for ref in (getattr(request, "input_refs", ()) or ()))
        for ref in input_refs:
            if "://" not in ref:
                return deny(f"contract incomplete: malformed input ref {ref!r}")
        acceptance = tuple(getattr(request, "acceptance", ()) or ())
        if not acceptance:
            return deny("contract incomplete: at least one acceptance criterion required")
        alloc_ref = str(getattr(request, "alloc_ref", "") or "")
        if not alloc_ref:
            return deny("contract incomplete: alloc_ref is required")
        if alloc_ref not in self.allocations:
            return deny(f"contract incomplete: unknown alloc_ref {alloc_ref!r}")

        # Parent must be a live, registered agent.
        if not self._known(parent_id):
            return deny(f"unknown parent agent {parent_id!r}")
        parent = self.handles[parent_id]
        if parent.status in TERMINAL_STATUSES:
            return deny(f"parent agent {parent_id!r} is already {parent.status.lower()}")
        if alloc_ref != self._agent_alloc[parent_id]:
            return deny(
                f"alloc_ref {alloc_ref!r} does not belong to parent {parent_id!r}"
            )

        # 3. Capability subset.
        requested_caps = tuple(
            str(cap) for cap in (getattr(request, "capabilities", ()) or ())
        )
        allowed = self._delegable_capabilities(parent)
        if allowed is not None:
            extra = [cap for cap in requested_caps if cap not in allowed]
            if extra:
                return deny(
                    "capability escalation: "
                    + ", ".join(sorted(extra))
                    + " not delegable by "
                    + parent_id
                )

        # 4. Budget: the child grant is carved out of the parent's remaining
        #    budget while the parent keeps ``reserve_ratio`` for integration and
        #    verification work.
        grant = self._grant_for(request)
        needed = int(grant.get(BUDGET_UNIT, 0))
        alloc = self.allocation(parent_id)
        available = self._remaining(alloc)
        keep = int(available * max(0.0, min(1.0, float(self.config.reserve_ratio))))
        spendable = available - keep
        if needed > spendable:
            return deny(
                f"parent budget insufficient: requested {needed} tokens > "
                f"spendable {spendable} (remaining {available}, reserve {keep})"
            )
        parent_calls = dict(alloc.granted or {}).get("calls")
        if parent_calls is not None:
            used_calls = int(self._calls_spent.get(alloc.alloc_id, 0))
            requested_calls = int(grant.get("calls", 0))
            if requested_calls > max(0, int(parent_calls) - used_calls):
                return deny(
                    "parent budget insufficient: requested "
                    f"{requested_calls} calls > remaining "
                    f"{max(0, int(parent_calls) - used_calls)}"
                )

        # 5. Structural caps.
        child_depth = int(parent.depth) + 1
        if child_depth > int(self.config.max_depth):
            return deny(
                f"max_depth exceeded: child depth {child_depth} > "
                f"limit {self.config.max_depth}"
            )
        cap = self._child_cap(parent_id)
        live_children = [
            child
            for child in self._children.get(parent_id, ())
            if self.handles[child].status not in TERMINAL_STATUSES
        ]
        if len(live_children) >= cap:
            return deny(
                f"max_children exceeded: {len(live_children)} live children >= "
                f"cap {cap} for agent {parent_id!r}"
            )
        total = len(self.handles)
        if total >= int(self.config.max_total_agents):
            return deny(
                f"max_total_agents exceeded: {total} >= limit "
                f"{self.config.max_total_agents}"
            )

        # 6. Admit: atomically reserve the grant and create the handle.
        status = "CREATED"
        if self.active_count() >= int(self.config.max_active):
            status = "QUEUED"
        child_id = self._next_agent_id()
        child_alloc = self._new_allocation(parent=alloc.alloc_id, granted=grant)
        alloc.reserved = int(alloc.reserved) + needed
        alloc.children = tuple(alloc.children or ()) + (child_alloc.alloc_id,)
        handle = AgentHandle(
            agent_id=child_id,
            task_id=str(
                (getattr(request, "metadata", {}) or {}).get("task_id")
                or f"task.{child_id}"
            ),
            role=str(getattr(request, "task_kind", "analyze") or "analyze"),
            parent_id=parent_id,
            depth=child_depth,
            status=status,
            mailbox_id=f"mb_{child_id}",
            alloc_id=child_alloc.alloc_id,
            waiting_on=None,
            continuation=self._contract_snapshot(request),
            children=(),
            generation=0,
            activation_token=None,
            closed_reason="",
        )
        self.handles[child_id] = handle
        self._agent_alloc[child_id] = child_alloc.alloc_id
        self._children[child_id] = []
        self._children[parent_id].append(child_id)
        parent.children = tuple(parent.children or ()) + (child_id,)
        self._join_policies[child_id] = str(
            getattr(request, "join_policy", "REQUIRED") or "REQUIRED"
        )
        max_children = int(getattr(request, "max_children", 0) or 0)
        self._child_caps[child_id] = (
            min(int(self.config.max_children_per_agent), max_children)
            if max_children > 0
            else int(self.config.max_children_per_agent)
        )
        if status == "QUEUED":
            self._queued.append(child_id)

        # Collaboration scope: children join their parent's default room.
        parent_room = self.ensure_room(parent_id)
        if parent_room and bool(getattr(request, "join_parent_room", True)):
            self._add_member(parent_room, child_id)

        return SpawnDecision(
            request_id=request_id,
            status=status,
            agent_id=child_id,
            reason="admitted",
            allocation=dict(grant),
            reused=False,
        )

    # -- admission helpers ------------------------------------------------
    def _grant_for(self, request: SpawnRequest) -> dict[str, int]:
        budget = dict(getattr(request, "budget", {}) or {})
        if not budget:
            budget = dict(self.config.default_budget or {})
        grant: dict[str, int] = {}
        tokens = _budget_tokens(budget)
        if tokens <= 0:
            tokens = _budget_tokens(self.config.default_budget)
        grant[BUDGET_UNIT] = max(0, int(tokens))
        default_calls = int(dict(self.config.default_budget or {}).get("calls", 0) or 0)
        grant["calls"] = max(0, int(budget.get("calls", default_calls) or 0))
        return grant

    def _delegable_capabilities(
        self, parent: AgentHandle
    ) -> tuple[str, ...] | None:
        """Delegable capabilities of ``parent``; ``None`` = no filtering."""
        mapping = self.config.delegable_capabilities
        if mapping is None:
            return None
        for key in (parent.agent_id, str(parent.role)):
            values = mapping.get(key) if isinstance(mapping, Mapping) else None
            if values is not None:
                return tuple(str(v) for v in values)
        return ()

    def _child_cap(self, agent_id: str) -> int:
        return min(
            int(self.config.max_children_per_agent),
            int(self._child_caps.get(agent_id, self.config.max_children_per_agent)),
        )

    def _contract_snapshot(self, request: SpawnRequest) -> dict[str, Any]:
        """Initial continuation payload: the immutable part of the contract."""
        return {
            "objective": str(getattr(request, "objective", "") or ""),
            "input_refs": list(getattr(request, "input_refs", ()) or ()),
            "acceptance": list(getattr(request, "acceptance", ()) or ()),
            "constraints": list(getattr(request, "constraints", ()) or ()),
            "capabilities": list(getattr(request, "capabilities", ()) or ()),
            "task_kind": str(getattr(request, "task_kind", "analyze") or "analyze"),
            "risk": str(getattr(request, "risk", "medium") or "medium"),
            "metadata": dict(getattr(request, "metadata", {}) or {}),
        }

    def _new_allocation(
        self, *, parent: str | None, granted: Mapping[str, int]
    ) -> BudgetAllocation:
        alloc_id = f"alloc_{self._alloc_seq:04d}"
        self._alloc_seq += 1
        alloc = BudgetAllocation(
            alloc_id=alloc_id,
            parent=parent,
            granted=dict(granted or {}),
            reserved=0,
            spent=0,
            children=(),
        )
        self.allocations[alloc_id] = alloc
        return alloc

    def _next_agent_id(self) -> str:
        agent_id = f"agent_{self._agent_seq:04d}"
        self._agent_seq += 1
        return agent_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self, agent_id: str) -> bool:
        """Mark ``agent_id`` RUNNING if a concurrency slot is free.

        Returns ``True`` when the agent actually started and ``False`` when it
        stays QUEUED because ``max_active`` is already saturated.
        """
        handle = self.agent(agent_id)
        if handle.status in TERMINAL_STATUSES:
            return False
        if handle.status in ("RUNNING", "WAITING"):
            # WAITING -> RUNNING: the agent was woken and is scheduled again.
            handle.status = "RUNNING"
            return True
        if handle.status == "QUEUED" and self.active_count() >= int(
            self.config.max_active
        ):
            return False
        handle.status = "RUNNING"
        if agent_id in self._queued:
            self._queued.remove(agent_id)
        return True

    def mark_ready(self, agent_id: str) -> None:
        handle = self.agent(agent_id)
        if handle.status not in TERMINAL_STATUSES:
            handle.status = "READY"

    def mark_wait(self, agent_id: str) -> None:
        handle = self.agent(agent_id)
        if handle.status not in TERMINAL_STATUSES:
            handle.status = "WAITING"

    def finish(self, agent_id: str) -> None:
        """Mark a submitting agent CLOSED and free its supervision slot."""
        handle = self.agent(agent_id)
        if handle.status in TERMINAL_STATUSES:
            return
        handle.status = "CLOSED"
        handle.closed_reason = handle.closed_reason or "finished"
        if agent_id in self._queued:
            self._queued.remove(agent_id)

    def cancel(
        self, agent_id: str, reason: str, propagate: bool = True
    ) -> list[str]:
        """Cancel ``agent_id`` and (optionally) every unfinished descendant.

        Completed delegations are never destroyed: only agents that are still in
        an active status are closed.  Returns the ids actually cancelled.
        """
        if not self._known(agent_id):
            raise KeyError(f"unknown agent {agent_id!r}")
        cancelled: list[str] = []
        if propagate:
            self._cancel_one(agent_id, reason, cancelled)
        else:
            handle = self.agent(agent_id)
            if handle.status not in TERMINAL_STATUSES:
                handle.status = "CLOSED"
                handle.closed_reason = f"cancelled: {reason}"
                if agent_id in self._queued:
                    self._queued.remove(agent_id)
                self._release(agent_id)
                cancelled.append(agent_id)
        return cancelled

    def _cancel_one(self, agent_id: str, reason: str, out: list[str]) -> None:
        handle = self.handles.get(agent_id)
        if handle is None or handle.status in TERMINAL_STATUSES:
            return
        handle.status = "CLOSED"
        handle.closed_reason = f"cancelled: {reason}"
        if agent_id in self._queued:
            self._queued.remove(agent_id)
        self._release(agent_id)
        out.append(agent_id)
        # Cancellation propagates down the whole subtree (spec 3.2).
        for child in list(self._children.get(agent_id, ())):
            self._cancel_one(child, f"ancestor {agent_id} cancelled: {reason}", out)

    def _release(self, agent_id: str) -> int:
        """Return an agent's unspent grant to its parent and zero its reserve."""
        alloc = self.allocation(agent_id)
        outstanding = self._outstanding(alloc)
        parent_alloc = self.allocations.get(alloc.parent) if alloc.parent else None
        if parent_alloc is None:
            # The root has no parent to return budget to; keep the ledger valid.
            return outstanding
        parent_alloc.reserved = max(0, int(parent_alloc.reserved) - outstanding)
        alloc.reserved = int(alloc.reserved) + outstanding
        return outstanding

    @staticmethod
    def _outstanding(alloc: BudgetAllocation) -> int:
        """Granted budget neither spent nor already committed to children."""
        granted = int(dict(alloc.granted or {}).get(BUDGET_UNIT, 0))
        return max(0, granted - int(alloc.spent) - int(alloc.reserved))

    def settle(self, agent_id: str, used: Mapping[str, int]) -> int:
        """Record actual usage and return the unspent remainder to the parent.

        Idempotent for an already-settled agent (the second call returns ``0``).
        ``used`` is a resource mapping; only ``tokens`` move ``spent`` while
        ``calls`` update the secondary counter.
        """
        alloc = self.allocation(agent_id)
        usage = dict(used or {})
        tokens = _budget_tokens(usage)
        if tokens:
            alloc.spent = int(alloc.spent) + tokens
        calls = int(usage.get("calls", 0) or 0)
        if calls:
            self._calls_spent[alloc.alloc_id] = (
                int(self._calls_spent.get(alloc.alloc_id, 0)) + calls
            )
        handle = self.agent(agent_id)
        if handle.status in TERMINAL_STATUSES:
            return self._release(agent_id)
        return 0

    def charge(self, agent_id: str, used: Mapping[str, int]) -> None:
        """Record in-flight usage without releasing the reservation."""
        if not self._known(agent_id):
            return
        usage = dict(used or {})
        alloc = self.allocation(agent_id)
        tokens = _budget_tokens(usage)
        if tokens:
            alloc.spent = int(alloc.spent) + tokens
        calls = int(usage.get("calls", 0) or 0)
        if calls:
            self._calls_spent[alloc.alloc_id] = (
                int(self._calls_spent.get(alloc.alloc_id, 0)) + calls
            )

    # ------------------------------------------------------------------
    # Closing and hand-over
    # ------------------------------------------------------------------
    def request_close(self, agent_id: str) -> tuple[bool, str]:
        """Check whether ``agent_id`` may close (spec: no open required work)."""
        self.agent(agent_id)
        for child in self._children.get(agent_id, ()):
            handle = self.handles[child]
            policy = self._join_policies.get(child, "REQUIRED")
            if policy == "REQUIRED" and handle.status in ACTIVE_STATUSES:
                return (
                    False,
                    f"required child {child!r} is still {handle.status.lower()}",
                )
        for scope_id in self._owned_scopes(agent_id):
            open_threads = self._open_threads(scope_id)
            if open_threads:
                return (
                    False,
                    f"scope {scope_id!r} still has open required thread(s): "
                    + ", ".join(open_threads),
                )
        return True, "all required work is finished"

    def reassign(self, agent_id: str, new_parent: str) -> bool:
        """Hand an unfinished agent (and its subtree) to a different parent."""
        handle = self.agent(agent_id)
        new_parent_handle = self.agent(new_parent)
        if agent_id == new_parent or handle.parent_id == new_parent:
            return False
        if new_parent_handle.status in TERMINAL_STATUSES:
            raise ValueError(f"new parent {new_parent!r} is already closed")
        # No cycles: the new parent must not be inside the moved subtree.
        cursor: str | None = new_parent
        while cursor is not None:
            if cursor == agent_id:
                raise ValueError(
                    f"reassign would create a cycle: {new_parent!r} is a descendant "
                    f"of {agent_id!r}"
                )
            cursor = self.handles[cursor].parent_id
        # Depth consistency of the whole subtree must hold after the move.
        delta = (int(new_parent_handle.depth) + 1) - int(handle.depth)
        deepest = max(self._subtree_depths(agent_id))
        if deepest + delta > int(self.config.max_depth):
            raise ValueError(
                f"reassign would exceed max_depth {self.config.max_depth}: "
                f"deepest descendant becomes {deepest + delta}"
            )
        old_parent = handle.parent_id
        if old_parent is not None:
            siblings = self._children.get(old_parent, [])
            if agent_id in siblings:
                siblings.remove(agent_id)
        self._children.setdefault(new_parent, []).append(agent_id)
        # Move the outstanding budget commitment to the new parent.
        alloc = self.allocation(agent_id)
        outstanding = self._outstanding(alloc)
        old_alloc = self.allocations.get(alloc.parent) if alloc.parent else None
        new_alloc = self.allocation(new_parent)
        if old_alloc is not None:
            old_alloc.reserved = max(0, int(old_alloc.reserved) - outstanding)
            old_children = tuple(old_alloc.children or ())
            old_alloc.children = tuple(
                child for child in old_children if child != alloc.alloc_id
            )
        new_alloc.reserved = int(new_alloc.reserved) + outstanding
        new_alloc.children = tuple(new_alloc.children or ()) + (alloc.alloc_id,)
        alloc.parent = new_alloc.alloc_id
        handle.parent_id = new_parent
        # Shift depths for the subtree and transfer room membership.
        for descendant in self._subtree(agent_id):
            self.handles[descendant].depth = int(self.handles[descendant].depth) + delta
        old_room = self._rooms.get(old_parent) if old_parent else None
        new_room = self.ensure_room(new_parent)
        if old_room and self.store is not None:
            self._remove_member(old_room, agent_id)
        if new_room:
            self._add_member(new_room, agent_id)
        return True

    def _subtree(self, agent_id: str) -> list[str]:
        out = [agent_id]
        for child in self._children.get(agent_id, ()):
            out.extend(self._subtree(child))
        return out

    def _subtree_depths(self, agent_id: str) -> list[int]:
        return [int(self.handles[agent].depth) for agent in self._subtree(agent_id)]

    # ------------------------------------------------------------------
    # Collaboration rooms (optional store integration)
    # ------------------------------------------------------------------
    def ensure_room(self, agent_id: str) -> str | None:
        """Return (creating on demand) the default local room of an agent."""
        if self.store is None:
            return None
        existing = self._rooms.get(agent_id)
        if existing:
            return existing
        handle = self.agent(agent_id)
        parent_scope = None
        if handle.parent_id is not None:
            parent_scope = self._rooms.get(handle.parent_id)
        scope = self.store.create_scope(
            kind="ROOM",
            owner=agent_id,
            members=(agent_id,),
            parent_scope=parent_scope,
        )
        self._rooms[agent_id] = scope.scope_id
        return scope.scope_id

    def room_of(self, agent_id: str) -> str | None:
        return self._rooms.get(agent_id)

    def _add_member(self, scope_id: str, agent_id: str) -> None:
        if self.store is None:
            return
        try:
            self.store.add_member(scope_id, agent_id)
        except (KeyError, ValueError, PermissionError):
            pass

    def _remove_member(self, scope_id: str, agent_id: str) -> None:
        if self.store is None:
            return
        try:
            self.store.remove_member(scope_id, agent_id)
        except (KeyError, ValueError, PermissionError):
            pass

    def _owned_scopes(self, agent_id: str) -> list[str]:
        scopes: list[str] = []
        room = self._rooms.get(agent_id)
        if room:
            scopes.append(room)
        # Child rooms created later are discovered lazily.
        return scopes

    def _open_threads(self, scope_id: str) -> list[str]:
        if self.store is None:
            return []
        try:
            board = self.store.board(scope_id)
        except (KeyError, ValueError, PermissionError):
            return []
        if not isinstance(board, Mapping):
            return []
        for key in ("open_threads", "threads", "open"):
            values = board.get(key)
            if isinstance(values, (list, tuple)):
                open_threads: list[str] = []
                for value in values:
                    if isinstance(value, Mapping):
                        if value.get("closed") or value.get("decision"):
                            continue
                        thread_id = value.get("thread_id") or value.get("id")
                        open_threads.append(str(thread_id or "?"))
                    elif not isinstance(value, bool):
                        open_threads.append(str(value))
                return open_threads
        return []

    # ------------------------------------------------------------------
    # Invariants / telemetry
    # ------------------------------------------------------------------
    def assert_invariants(self) -> None:
        """Assert budget conservation, acyclic parenting and depth consistency."""
        for agent_id, handle in self.handles.items():
            if handle.agent_id != agent_id:
                raise AssertionError(f"handle key {agent_id!r} != handle id")
            if handle.alloc_id not in self.allocations:
                raise AssertionError(
                    f"agent {agent_id!r} has unknown allocation {handle.alloc_id!r}"
                )
            alloc = self.allocations[handle.alloc_id]
            if self._agent_alloc[agent_id] != alloc.alloc_id:
                raise AssertionError(f"agent {agent_id!r} allocation mapping drifted")
            # Depth consistency with the parent handle.
            if handle.parent_id is None:
                if handle.depth != 0:
                    raise AssertionError(f"root agent {agent_id!r} must be depth 0")
            else:
                parent = self.handles.get(handle.parent_id)
                if parent is None:
                    raise AssertionError(
                        f"agent {agent_id!r} has unknown parent {handle.parent_id!r}"
                    )
                if int(handle.depth) != int(parent.depth) + 1:
                    raise AssertionError(
                        f"agent {agent_id!r} depth {handle.depth} != parent depth + 1"
                    )
                if agent_id not in self._children.get(handle.parent_id, ()):
                    raise AssertionError(
                        f"parent {handle.parent_id!r} does not list child {agent_id!r}"
                    )
            for child in self._children.get(agent_id, ()):
                if self.handles[child].parent_id != agent_id:
                    raise AssertionError(
                        f"child {child!r} parent_id drifted from {agent_id!r}"
                    )
            # No cycles in the parent chain.
            cursor: str | None = agent_id
            seen: set[str] = set()
            while cursor is not None:
                if cursor in seen:
                    raise AssertionError(f"parent chain cycle at {cursor!r}")
                seen.add(cursor)
                cursor = self.handles[cursor].parent_id
        for alloc_id, alloc in self.allocations.items():
            granted = int(dict(alloc.granted or {}).get(BUDGET_UNIT, 0))
            if granted < 0:
                raise AssertionError(f"allocation {alloc_id!r} has negative grant")
            spent = int(alloc.spent)
            reserved = int(alloc.reserved)
            if spent < 0 or reserved < 0:
                raise AssertionError(
                    f"allocation {alloc_id!r} has negative spent/reserved"
                )
            if spent + reserved > granted:
                raise AssertionError(
                    f"budget conservation violated at {alloc_id!r}: "
                    f"spent {spent} + reserved {reserved} > granted {granted}"
                )
            if alloc.parent is not None and alloc.parent not in self.allocations:
                raise AssertionError(
                    f"allocation {alloc_id!r} has unknown parent {alloc.parent!r}"
                )
            granted_calls = dict(alloc.granted or {}).get("calls")
            if granted_calls is not None:
                calls = int(self._calls_spent.get(alloc_id, 0))
                if calls > int(granted_calls):
                    raise AssertionError(
                        f"call budget exceeded at {alloc_id!r}: {calls} > "
                        f"{granted_calls}"
                    )

    def snapshot(self) -> dict[str, Any]:
        """Full supervision state for telemetry / debugging."""
        return {
            "agents": {
                agent_id: _as_dict(handle) for agent_id, handle in self.handles.items()
            },
            "allocations": {
                alloc_id: _as_dict(alloc) for alloc_id, alloc in self.allocations.items()
            },
            "decisions": {
                request_id: _as_dict(decision)
                for request_id, decision in self.decisions.items()
            },
            "rooms": dict(self._rooms),
            "queued": list(self._queued),
            "active": self.active_count(),
            "total_agents": len(self.handles),
            "limits": {
                "max_children_per_agent": self.config.max_children_per_agent,
                "max_depth": self.config.max_depth,
                "max_active": self.config.max_active,
                "max_total_agents": self.config.max_total_agents,
            },
        }
