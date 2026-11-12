"""Scheduling, single-agent execution locks and event-driven wake-ups.

This module implements section 3.3 of ``docs/MULTI_AGENT_INTERFACES.md``.  The
scheduler never calls the model itself: it decides *which* agent may run, hands
out a generation-tagged :class:`Activation` token, and makes agents runnable
again when the events they wait for arrive.

Design notes
------------
* **One activation per agent.**  ``activate`` is idempotent while an activation
  is open, so a buggy caller can never run the same agent twice in parallel.
* **Stale commits are rejected.**  Every activation carries its generation and
  token; ``commit``/``abort`` verify both against the live activation.  A token
  from a previous generation (or from an activation that was already committed,
  aborted, or superseded by ``wait``) raises :class:`StaleActivation`.
* **Waiting is not running.**  ``wait`` releases the concurrency slot and
  records only the waiting condition.  So an entire hierarchy of parents may
  wait while their children still run - the classic deadlock the spec forbids.
* **Events coalesce.**  Several events that target an agent already woken from
  its current wait produce exactly one wake-up; the rest are counted as
  ``coalesced`` rather than triggering further model calls.
"""

from __future__ import annotations

import heapq
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from .collab_types import WaitCondition

__all__ = [
    "Activation",
    "CapacityError",
    "Scheduler",
    "SchedulerConfig",
    "StaleActivation",
    "StepLimitExceeded",
]

#: Statuses in which an agent no longer needs scheduling.
_TERMINAL = frozenset({"CLOSED", "FAILED"})


class StaleActivation(RuntimeError):
    """Raised when an activation token is not the current generation."""


class StepLimitExceeded(RuntimeError):
    """Raised when an agent used up its ``max_steps`` model calls."""


class CapacityError(RuntimeError):
    """Raised when every concurrency slot is occupied."""


@dataclass
class SchedulerConfig:
    """Scheduling limits: concurrency and per-agent call budget."""

    max_concurrent: int = 4
    max_steps: int = 200


@dataclass
class Activation:
    """A generation-tagged right to execute one agent step."""

    agent_id: str
    generation: int
    token: str
    scheduler: "Scheduler" = field(repr=False, compare=False)
    _state: str = field(default="open", repr=False)

    @property
    def committed(self) -> bool:
        return self._state == "committed"

    @property
    def aborted(self) -> bool:
        return self._state == "aborted"

    def commit(self) -> None:
        """Advance the generation and release the concurrency slot."""
        self.scheduler.commit(self)

    def abort(self, reason: str = "") -> None:
        """Roll the activation back (the agent returns to READY)."""
        self.scheduler.abort(self, reason)


@dataclass
class _Waiter:
    condition: WaitCondition
    since: float
    satisfied: set[str] = field(default_factory=set)


class Scheduler:
    """Priority ready-queue, activation locks and wait-graph bookkeeping."""

    def __init__(self, config: SchedulerConfig | None = None) -> None:
        self.config = config or SchedulerConfig()
        # Ready heap entries are ``(-priority, sequence, agent_id)``; entries
        # become stale when an agent is activated/removed (lazy deletion).
        self._heap: list[tuple[int, int, str]] = []
        self._queued: set[str] = set()
        self._priority: dict[str, int] = {}
        self._seq: dict[str, int] = {}
        self._status: dict[str, str] = {}
        self._active: dict[str, Activation] = {}
        self._generation: dict[str, int] = {}
        self._calls: dict[str, int] = {}
        self._waiters: dict[str, _Waiter] = {}
        #: agent -> set of agent/task/message ids it is waiting for.
        self._wait_edges: dict[str, set[str]] = {}
        self._woken: set[str] = set()
        #: Condition an agent was last woken from (coalescing window).
        self._last_waiter: dict[str, _Waiter] = {}
        self._wakes = 0
        self._coalesced = 0
        self._wait_total_s = 0.0
        self._token_seq = 0
        self._seq_counter = 0

    # ------------------------------------------------------------------
    # Registration / discovery
    # ------------------------------------------------------------------
    def add_agent(self, agent_id: str, priority: int = 50) -> str:
        """Insert ``agent_id`` into the ready queue (idempotent)."""
        agent_id = str(agent_id)
        if self._status.get(agent_id) in _TERMINAL:
            raise ValueError(f"agent {agent_id!r} is already {self._status[agent_id]}")
        if self._status.get(agent_id) == "WAITING":
            raise ValueError(f"agent {agent_id!r} is waiting and cannot be re-added")
        self._priority[agent_id] = int(priority)
        if agent_id not in self._seq:
            self._seq_counter += 1
            self._seq[agent_id] = self._seq_counter
        self._set_ready(agent_id)
        return agent_id

    def status(self, agent_id: str) -> str:
        return self._status.get(agent_id, "CREATED")

    def is_waiting(self, agent_id: str) -> bool:
        return self._status.get(agent_id) == "WAITING"

    def waiting_on(self, agent_id: str) -> WaitCondition | None:
        waiter = self._waiters.get(agent_id)
        return waiter.condition if waiter is not None else None

    def ready_agents(self) -> list[str]:
        """Ready agents in dispatch order (priority desc, FIFO within priority)."""
        entries = [
            (self._priority.get(agent_id, 50), self._seq.get(agent_id, 0), agent_id)
            for agent_id in self._queued
            if self.status(agent_id) == "READY"
        ]
        entries.sort(key=lambda item: (-item[0], item[1]))
        return [agent_id for _priority, _seq, agent_id in entries]

    @property
    def active(self) -> int:
        """Number of held concurrency slots (waiting does not hold one)."""
        return len(self._active)

    def active_activation(self, agent_id: str) -> Activation | None:
        return self._active.get(agent_id)

    # ------------------------------------------------------------------
    # Activation lifecycle
    # ------------------------------------------------------------------
    def activate(self, agent_id: str) -> Activation:
        """Return the open activation of ``agent_id`` or open a new one."""
        agent_id = str(agent_id)
        current = self._active.get(agent_id)
        if current is not None:
            return current
        status = self.status(agent_id)
        if status in _TERMINAL:
            raise ValueError(f"agent {agent_id!r} is already {status.lower()}")
        if status == "WAITING":
            raise ValueError(
                f"agent {agent_id!r} is waiting; wake it through notify() first"
            )
        used = int(self._calls.get(agent_id, 0))
        if used >= int(self.config.max_steps):
            raise StepLimitExceeded(
                f"agent {agent_id!r} used {used} steps (limit "
                f"{self.config.max_steps})"
            )
        if len(self._active) >= int(self.config.max_concurrent):
            raise CapacityError(
                f"concurrency limit reached ({len(self._active)}/"
                f"{self.config.max_concurrent})"
            )
        generation = int(self._generation.get(agent_id, 0)) + 1
        self._generation[agent_id] = generation
        self._token_seq += 1
        token = f"act_{agent_id}_{generation}_{self._token_seq}"
        activation = Activation(
            agent_id=agent_id,
            generation=generation,
            token=token,
            scheduler=self,
        )
        self._active[agent_id] = activation
        self._status[agent_id] = "RUNNING"
        self._queued.discard(agent_id)
        self._calls[agent_id] = used + 1
        self._woken.discard(agent_id)
        self._last_waiter.pop(agent_id, None)
        return activation

    def ready_step(self) -> Activation | None:
        """Activate the highest-priority ready agent, or ``None``."""
        if len(self._active) >= int(self.config.max_concurrent):
            return None
        while self._heap:
            _neg_priority, _seq, agent_id = heapq.heappop(self._heap)
            if agent_id in self._queued and self.status(agent_id) == "READY":
                return self.activate(agent_id)
        return None

    def commit(self, activation: Activation) -> None:
        """Validate and close ``activation``; the agent becomes READY again."""
        self._validate(activation)
        agent_id = activation.agent_id
        self._active.pop(agent_id, None)
        activation._state = "committed"
        self._generation[agent_id] = int(activation.generation) + 1
        if self.status(agent_id) == "RUNNING":
            self._set_ready(agent_id)
        if activation is not None:
            self._woken.discard(agent_id)

    def abort(self, activation: Activation, reason: str = "") -> None:
        """Roll back ``activation``; the agent becomes READY again."""
        self._validate(activation)
        agent_id = activation.agent_id
        self._active.pop(agent_id, None)
        activation._state = "aborted"
        self._generation[agent_id] = int(activation.generation) + 1
        if self.status(agent_id) not in _TERMINAL:
            self._set_ready(agent_id)

    def _validate(self, activation: Activation) -> None:
        if not isinstance(activation, Activation):
            raise StaleActivation("not an Activation instance")
        agent_id = activation.agent_id
        live = self._active.get(agent_id)
        if live is not activation:
            raise StaleActivation(
                f"activation for {agent_id!r} is no longer live "
                f"(generation {activation.generation})"
            )
        if activation._state != "open":
            raise StaleActivation(
                f"activation for {agent_id!r} was already {activation._state}"
            )
        if int(self._generation.get(agent_id, 0)) != int(activation.generation):
            raise StaleActivation(
                f"activation generation {activation.generation} is stale for "
                f"{agent_id!r} (current {self._generation.get(agent_id, 0)})"
            )
        expected = self._active.get(agent_id)
        if expected is None or expected.token != activation.token:
            raise StaleActivation(f"activation token mismatch for {agent_id!r}")

    # ------------------------------------------------------------------
    # Waiting
    # ------------------------------------------------------------------
    def wait(self, agent_id: str, condition: WaitCondition) -> None:
        """Park ``agent_id`` on ``condition`` and release its slot."""
        agent_id = str(agent_id)
        if self.status(agent_id) in _TERMINAL:
            raise ValueError(f"agent {agent_id!r} is already closed")
        activation = self._active.pop(agent_id, None)
        if activation is not None:
            # The activation is superseded by the wait; an old commit is stale.
            activation._state = "aborted"
            self._generation[agent_id] = int(activation.generation) + 1
        self._queued.discard(agent_id)
        waiter = _Waiter(condition=condition, since=time.monotonic())
        self._waiters[agent_id] = waiter
        self._status[agent_id] = "WAITING"
        refs = tuple(str(ref) for ref in (getattr(condition, "refs", ()) or ()))
        self._wait_edges[agent_id] = {
            ref for ref in refs if ref and ref != agent_id and self._known(ref)
        }
        return None

    def notify(self, event: Mapping[str, Any]) -> list[str]:
        """Apply one event; return the agents woken by it."""
        event = dict(event or {})
        event_kind = str(event.get("event_kind") or event.get("kind") or "")
        refs = _event_refs(event)
        target = event.get("agent_id") or event.get("target") or event.get("recipient")
        candidates = [str(target)] if target else list(self._waiters)
        woken: list[str] = []
        for agent_id in candidates:
            waiter = self._waiters.get(agent_id)
            if waiter is None:
                continue
            for ref in refs:
                waiter.satisfied.add(ref)
            if self._matches(agent_id, waiter, refs, event_kind, event):
                if self._wake(agent_id):
                    woken.append(agent_id)
        # Coalescing: an event that matches the condition an agent was just
        # woken from is folded into that same wake-up, never a new one.
        if target and not woken:
            target_id = str(target)
            last_waiter = self._last_waiter.get(target_id)
            if target_id in self._woken and last_waiter is not None:
                if self._matches(target_id, last_waiter, refs, event_kind, event):
                    self._coalesced += 1
        return woken

    def _matches(
        self,
        agent_id: str,
        waiter: _Waiter,
        refs: tuple[str, ...],
        event_kind: str,
        event: Mapping[str, Any],
    ) -> bool:
        condition = waiter.condition
        kind = str(getattr(condition, "kind", "UNTIL_EVENT") or "UNTIL_EVENT")
        expected_refs = tuple(str(ref) for ref in (getattr(condition, "refs", ()) or ()))
        if kind == "UNTIL_EVENT":
            kinds = tuple(
                str(item) for item in (getattr(condition, "event_kinds", ()) or ())
            )
            if kinds and event_kind not in kinds:
                return False
            if expected_refs:
                return any(ref in expected_refs for ref in refs)
            return True
        if kind == "ANY_ACCEPTED":
            if expected_refs:
                return any(ref in expected_refs for ref in refs)
            return bool(event.get("accepted", True))
        if kind == "ALL_REQUIRED":
            if not expected_refs:
                return True
            return set(expected_refs) <= waiter.satisfied
        return False

    def _wake(self, agent_id: str) -> bool:
        waiter = self._waiters.pop(agent_id, None)
        if waiter is None:
            return False
        elapsed = max(0.0, time.monotonic() - waiter.since)
        self._wait_total_s += elapsed
        self._wait_edges.pop(agent_id, None)
        self._woken.add(agent_id)
        self._last_waiter[agent_id] = waiter
        self._wakes += 1
        self._set_ready(agent_id)
        return True

    def _known(self, agent_id: str) -> bool:
        return agent_id in self._status

    # ------------------------------------------------------------------
    # Completion / removal
    # ------------------------------------------------------------------
    def complete(self, agent_id: str) -> None:
        """Remove ``agent_id`` from every queue (it has finished)."""
        agent_id = str(agent_id)
        activation = self._active.pop(agent_id, None)
        if activation is not None:
            activation._state = "aborted"
        self._status[agent_id] = "CLOSED"
        self._queued.discard(agent_id)
        self._waiters.pop(agent_id, None)
        self._wait_edges.pop(agent_id, None)
        self._woken.discard(agent_id)
        self._last_waiter.pop(agent_id, None)

    def cancel(self, agent_id: str) -> None:
        """Alias of :meth:`complete` used by cancellation paths."""
        self.complete(agent_id)

    def _set_ready(self, agent_id: str) -> None:
        self._status[agent_id] = "READY"
        if agent_id in self._queued:
            return
        self._queued.add(agent_id)
        self._seq_counter += 1
        seq = self._seq.get(agent_id)
        if seq is None:
            seq = self._seq_counter
            self._seq[agent_id] = seq
        heapq.heappush(
            self._heap,
            (-int(self._priority.get(agent_id, 50)), seq, agent_id),
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def step_cost(self, agent_id: str, cost: int = 1) -> int:
        """Record ``cost`` model calls for ``agent_id`` and return the total."""
        agent_id = str(agent_id)
        self._calls[agent_id] = int(self._calls.get(agent_id, 0)) + max(0, int(cost))
        return self._calls[agent_id]

    def steps_used(self, agent_id: str) -> int:
        return int(self._calls.get(agent_id, 0))

    def deadlock_check(self) -> list[tuple[str, ...]]:
        """Return the cycles of the wait-for graph (deterministic order)."""
        graph: dict[str, list[str]] = {}
        for agent_id in self._waiters:
            targets = sorted(
                ref
                for ref in self._wait_edges.get(agent_id, ())
                if ref in self._waiters
            )
            graph[agent_id] = targets
        cycles: list[tuple[str, ...]] = []
        seen_cycles: set[tuple[str, ...]] = set()
        color: dict[str, int] = {}
        stack: list[str] = []
        position: dict[str, int] = {}

        def canonical(cycle: list[str]) -> tuple[str, ...]:
            smallest = min(range(len(cycle)), key=lambda i: cycle[i])
            return tuple(cycle[smallest:] + cycle[:smallest])

        def visit(node: str) -> None:
            color[node] = 1
            position[node] = len(stack)
            stack.append(node)
            for neighbour in graph.get(node, ()):
                if color.get(neighbour, 0) == 0:
                    visit(neighbour)
                elif color.get(neighbour) == 1:
                    cycle = stack[position[neighbour] :]
                    key = canonical(cycle)
                    if key not in seen_cycles:
                        seen_cycles.add(key)
                        cycles.append(key)
            stack.pop()
            color[node] = 2

        for agent_id in sorted(graph):
            if color.get(agent_id, 0) == 0:
                visit(agent_id)
        cycles.sort()
        return cycles

    def stats(self) -> dict[str, Any]:
        """Scheduler telemetry: ready / running / waiting and wake accounting."""
        ready = sum(1 for agent_id in self._queued if self.status(agent_id) == "READY")
        waiting = len(self._waiters)
        running = len(self._active)
        return {
            "ready": ready,
            "running": running,
            "waiting": waiting,
            "active": running,
            "wakes": self._wakes,
            "coalesced": self._coalesced,
            "wait_total_s": round(self._wait_total_s, 6),
            "cycles": len(self.deadlock_check()),
            "capacity": int(self.config.max_concurrent),
            "agents": len(self._status),
        }

    def snapshot(self) -> dict[str, Any]:
        """Waiting-graph and queue view for debugging."""
        return {
            "statuses": dict(self._status),
            "ready": self.ready_agents(),
            "waiting": {
                agent_id: {
                    "kind": str(getattr(waiter.condition, "kind", "")),
                    "refs": list(getattr(waiter.condition, "refs", ()) or ()),
                    "event_kinds": list(
                        getattr(waiter.condition, "event_kinds", ()) or ()
                    ),
                }
                for agent_id, waiter in self._waiters.items()
            },
            "wait_edges": {key: sorted(values) for key, values in self._wait_edges.items()},
            "active": sorted(self._active),
            "cycles": self.deadlock_check(),
            "stats": self.stats(),
        }


def _event_refs(event: Mapping[str, Any]) -> tuple[str, ...]:
    """Every id an event can satisfy (message id, reply_to, task id, ...)."""
    refs: list[str] = []
    many = event.get("refs")
    if isinstance(many, (list, tuple, set)):
        refs.extend(str(item) for item in many if item)
    for key in ("ref", "message_id", "reply_to", "task_id", "child_id", "agent_ref"):
        value = event.get(key)
        if value:
            refs.append(str(value))
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            unique.append(ref)
    return tuple(unique)
