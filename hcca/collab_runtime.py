"""End-to-end collaboration runtime (event-driven re-activation).

This module implements section 3.4 of ``docs/MULTI_AGENT_INTERFACES.md``: it
drives the supervision tree and the scheduler, turns one model call into one
``AgentStep`` execution, and persists continuation state between activations.

The runtime is the only component that translates model *actions* into side
effects.  Model output is never trusted to set identity, permissions or budget:
the runtime stamps those from the handle and asks the supervisor to adjudicate
spawns.

Message handling follows the spec's state machine: a message enters an
activation boundary as part of a fixed, read-only working set; messages that
arrive while a step executes are left pending for the next activation.  Every
activation boundary is also where the budget is charged and where the
continuation is saved, so a resumed agent never needs the conversation history.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence

from .collab_types import (
    AgentHandle,
    AgentStep,
    Message,
    SpawnRequest,
    StepAction,
    WaitCondition,
)
from .collaboration import ContextBudgetError
from .scheduler import Scheduler, StaleActivation
from .supervisor import Supervisor
from .types import GoalSpec, ResultEnvelope, STATUS_COMPLETED

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .collaboration import CollaborationStore

__all__ = [
    "CollabRuntime",
    "CollabWorkingSet",
    "WorkingSet",
]

#: Message kinds that wake a waiting parent by default.
_WAKING_KINDS = frozenset({"ANSWER", "DECISION", "INVALIDATE"})


@dataclass(frozen=True)
class CollabWorkingSet:
    """Read-only snapshot handed to the model for exactly one activation.

    The snapshot is immutable by construction: the selected messages, deferred
    messages, continuation and contract are copies.  Messages that arrive after
    the snapshot was built are *not* visible to this activation, which is what
    "消息在激活边界进入" means.
    """

    agent_id: str
    task_id: str
    objective: str
    contract: Mapping[str, Any]
    continuation: Mapping[str, Any]
    messages: tuple[Message, ...] = ()
    deferred: tuple[Message, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    scope_id: str | None = None
    generation: int = 0
    budget_tokens: int = 0

    def message_ids(self) -> tuple[str, ...]:
        return tuple(str(message.message_id) for message in self.messages)

    def render(self) -> str:
        """Render the volatile message tail of the working set (text view)."""
        lines: list[str] = []
        for message in self.messages:
            lines.append(
                f"[MSG kind={message.kind} from={message.sender} "
                f"id={message.message_id} subject={message.subject!r}]"
            )
            lines.append(str(message.body))
        if self.deferred:
            lines.append(
                "[DEFERRED message ids: "
                + ", ".join(str(m.message_id) for m in self.deferred)
                + "]"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "objective": self.objective,
            "contract": dict(self.contract),
            "continuation": dict(self.continuation),
            "message_ids": list(self.message_ids()),
            "deferred_ids": [str(m.message_id) for m in self.deferred],
            "evidence_refs": list(self.evidence_refs),
            "scope_id": self.scope_id,
            "generation": self.generation,
            "budget_tokens": self.budget_tokens,
            "statuses": [str(m.status) for m in self.messages],
        }


#: Spec name for the read-only working set.
WorkingSet = CollabWorkingSet


class CollabRuntime:
    """Drive supervisor + scheduler + store through one end-to-end run."""

    def __init__(
        self,
        step_runner: Callable[[AgentHandle, CollabWorkingSet], AgentStep],
        store: "CollaborationStore",
        supervisor: Supervisor,
        scheduler: Scheduler,
    ) -> None:
        if step_runner is None or store is None or supervisor is None or scheduler is None:
            raise ValueError("CollabRuntime needs step_runner, store, supervisor, scheduler")
        self.step_runner = step_runner
        self.store = store
        self.supervisor = supervisor
        self.scheduler = scheduler
        self.goal: GoalSpec | None = None
        self.root_id: str | None = None
        self.events: list[dict[str, Any]] = []
        self._contracts: dict[str, dict[str, Any]] = {}
        self._results: dict[str, ResultEnvelope] = {}
        #: Scope/thread id -> minimum snapshot version that is still valid.
        self._invalidated: dict[str, int] = {}
        self._spawn_seq = 0
        self._message_seq = 0
        self._steps = 0
        self._started = False
        self._finished = False
        self._stats: dict[str, Any] = {
            "denied_spawns": 0,
            "messages_sent": 0,
            "messages_processed": 0,
            "denied_messages": 0,
            "stale_answers": 0,
            "budget_errors": 0,
            "failed_agents": 0,
            "cancelled": 0,
            "yields": 0,
            "tokens_spent": 0,
        }
        self._report: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self, goal: GoalSpec | str) -> AgentHandle:
        """Register the root agent and make it the first ready agent."""
        if self._started:
            raise RuntimeError("CollabRuntime.start() may only be called once")
        goal_spec = goal if isinstance(goal, GoalSpec) else GoalSpec(
            goal_id="goal", objective=str(goal)
        )
        self.goal = goal_spec
        handle = AgentHandle(
            agent_id="root",
            task_id="root",
            role="root",
            parent_id=None,
            depth=0,
            status="READY",
            mailbox_id="mb_root",
            alloc_id="",
            waiting_on=None,
            continuation={
                "goal_id": goal_spec.goal_id,
                "objective": goal_spec.objective,
                "next_step": "plan",
            },
            children=(),
            generation=0,
            activation_token=None,
            closed_reason="",
        )
        self.supervisor.register_root(handle, self._root_granted())
        self._contracts["root"] = {
            "goal_id": goal_spec.goal_id,
            "objective": goal_spec.objective,
            "acceptance": list(goal_spec.acceptance),
            "constraints": list(goal_spec.constraints),
            "input_refs": [],
        }
        self.scheduler.add_agent("root", priority=100)
        self.root_id = "root"
        self._started = True
        self.events.append({"event": "root_started", "goal_id": goal_spec.goal_id})
        return handle

    def run(self, goal: GoalSpec | str | None = None) -> dict[str, Any]:
        """Drive activations until the root submits or nothing can progress."""
        if not self._started:
            if goal is None:
                raise ValueError("run() needs a goal on the first call")
            self.start(goal)
        max_steps = int(getattr(self.scheduler.config, "max_steps", 200))
        while not self._finished and self._steps < max_steps:
            if not self.run_one_step():
                break
        if not self._finished:
            self._finished = True
            if self.scheduler.stats()["waiting"]:
                self.events.append(
                    {"event": "run_stalled", "cycles": self.scheduler.deadlock_check()}
                )
        self._report = self._build_report()
        return self._report

    def run_one_step(self) -> bool:
        """Execute one activation; return ``False`` when nothing can run."""
        started = self._begin_activation()
        if started is None:
            return False
        activation, handle = started
        try:
            working_set = self._build_working_set(handle, activation)
        except ContextBudgetError as exc:
            self._stats["budget_errors"] += 1
            self._fail_agent(handle, f"context budget error: {exc}")
            return True
        try:
            step = self.step_runner(handle, working_set)
        except Exception as exc:  # pragma: no cover - defensive: runner failure
            self._abort_activation(activation, f"step_runner failed: {exc}")
            self._fail_agent(handle, f"step_runner failed: {exc}")
            return True
        return self._finish_activation(activation, handle, working_set, step)

    # -- parallel activations ------------------------------------------
    def run_parallel_step(self, max_workers: int = 4) -> bool:
        """Run up to ``max_workers`` independent ready activations.

        Model calls for distinct agents run concurrently; all state mutations
        (messages, budget, continuation, lifecycle) are committed sequentially
        afterwards, so the collaboration store keeps a single writer.
        """
        if not self._started:
            raise RuntimeError("call start(goal) before run_parallel_step()")
        if self._finished:
            return False
        capacity = int(getattr(self.scheduler.config, "max_concurrent", max_workers))
        limit = max(1, min(int(max_workers), capacity))
        batch: list[tuple[Any, AgentHandle]] = []
        while len(batch) < limit:
            started = self._begin_activation()
            if started is None:
                break
            batch.append(started)
        if not batch:
            return False
        prepared: list[tuple[Any, AgentHandle, Any]] = []
        for activation, handle in batch:
            try:
                working_set = self._build_working_set(handle, activation)
            except ContextBudgetError as exc:
                self._stats["budget_errors"] += 1
                self._abort_activation(activation, f"context budget error: {exc}")
                self._fail_agent(handle, f"context budget error: {exc}")
                continue
            prepared.append((activation, handle, working_set))
        if not prepared:
            return True

        def _call_model(item: tuple[Any, AgentHandle, Any]) -> Any:
            _activation, item_handle, item_working_set = item
            try:
                return self.step_runner(item_handle, item_working_set), None
            except Exception as exc:  # noqa: BLE001 - reported to the runtime
                return None, exc

        # Commit each finished activation as soon as it is done instead of
        # waiting for the whole batch: one stalled model call used to keep the
        # other Agents' completed (and already written) work out of the store,
        # so the run looked frozen even though five of six Agents had finished.
        pool = ThreadPoolExecutor(max_workers=len(prepared))
        try:
            futures = {
                pool.submit(_call_model, item): item for item in prepared
            }
            pending: set[Any] = set(futures)
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    activation, handle, working_set = futures[future]
                    try:
                        step, exc = future.result()
                    except BaseException as error:  # pragma: no cover - defensive
                        step, exc = None, error
                    if exc is not None:
                        self._abort_activation(activation, f"step_runner failed: {exc}")
                        self._fail_agent(handle, f"step_runner failed: {exc}")
                        continue
                    try:
                        self._finish_activation(activation, handle, working_set, step)
                    except Exception as error:  # noqa: BLE001 - one bad commit must not drop the rest
                        self._fail_agent(handle, f"step commit failed: {error}")
        finally:
            pool.shutdown(wait=True)
        return True

    def _begin_activation(self) -> tuple[Any, AgentHandle] | None:
        if not self._started:
            raise RuntimeError("call start(goal) before run_one_step()")
        if self._finished:
            return None
        if self._steps >= int(getattr(self.scheduler.config, "max_steps", 200)):
            self._finished = True
            return None
        activation = self.scheduler.ready_step()
        if activation is None:
            return None
        handle = self.supervisor.agent(activation.agent_id)
        self._steps += 1
        handle.waiting_on = None
        self.supervisor.start(handle.agent_id)
        self.events.append(
            {
                "event": "activate",
                "agent_id": handle.agent_id,
                "generation": activation.generation,
                "step": self._steps,
            }
        )
        return activation, handle

    def _finish_activation(
        self, activation: Any, handle: AgentHandle, working_set: Any, step: Any
    ) -> bool:
        step = self._coerce_step(step, handle)
        # Charge the budget before executing actions: a SUBMIT closes the agent
        # and settles the ledger, so the usage of this very step must be in it.
        self._charge(handle, step)
        self._execute_actions(handle, working_set, step)
        self._save_continuation(handle, step)
        self._mark_processed(handle, working_set)
        self._apply_yield(handle, working_set, step)
        self._settle_if_terminal(handle)
        if self._finished:
            return True
        if handle.status in ("CLOSED", "FAILED") or self.scheduler.is_waiting(
            handle.agent_id
        ):
            return True
        try:
            activation.commit()
        except StaleActivation:
            # An action already changed the lifecycle (e.g. cancellation raced).
            self._abort_activation(activation, "lifecycle changed during step")
        else:
            if handle.status == "RUNNING":
                handle.status = "READY"
        return True

    def result(self, agent_id: str = "root") -> ResultEnvelope | None:
        return self._results.get(agent_id)

    def report(self) -> dict[str, Any]:
        """Return the latest run statistics (builds them on demand)."""
        if self._report is not None:
            return dict(self._report)
        return self._build_report()

    def stats(self) -> dict[str, Any]:
        """Live counters plus the scheduler/supervisor snapshots."""
        report = self._build_report()
        return {
            **{key: report[key] for key in (
                "status",
                "steps",
                "wakes",
                "coalesced",
                "denied_spawns",
                "deadlock_cycles",
                "messages_processed",
                "messages_sent",
                "stale_answers",
                "budget_errors",
                "tokens_spent",
            )},
            "scheduler": self.scheduler.stats(),
            "agents": report["agents"],
        }

    # ------------------------------------------------------------------
    # Root / budgets
    # ------------------------------------------------------------------
    def _root_granted(self) -> dict[str, int]:
        """Give the root enough budget for the whole tree (config overridable)."""
        configured = getattr(self.supervisor.config, "root_budget", None)
        if isinstance(configured, Mapping):
            return {str(key): int(value) for key, value in configured.items()}
        default = dict(self.supervisor.config.default_budget or {})
        tokens = int(default.get("tokens", 2000) or 0) * max(
            1, int(self.supervisor.config.max_total_agents)
        )
        calls = int(default.get("calls", 4) or 0) * max(
            1, int(self.supervisor.config.max_total_agents)
        )
        return {"tokens": tokens, "calls": calls}

    # ------------------------------------------------------------------
    # Working-set construction (activation boundary)
    # ------------------------------------------------------------------
    def _build_working_set(
        self, handle: AgentHandle, activation: Any
    ) -> CollabWorkingSet:
        contract = dict(self._contracts.get(handle.agent_id) or {})
        budget_tokens = max(1, int(self.supervisor.remaining(handle.agent_id)))
        selected, deferred = self.store.select_for_context(
            handle.agent_id,
            budget_tokens=budget_tokens,
            max_messages=20,
        )
        scope_id = self.supervisor.room_of(handle.agent_id)
        if scope_id is None and handle.parent_id is not None:
            scope_id = self.supervisor.room_of(handle.parent_id)
        return CollabWorkingSet(
            agent_id=handle.agent_id,
            task_id=handle.task_id,
            objective=str(contract.get("objective", "")),
            contract=MappingProxyType(contract),
            continuation=MappingProxyType(dict(handle.continuation or {})),
            messages=tuple(selected),
            deferred=tuple(deferred),
            evidence_refs=tuple(
                str(ref) for ref in contract.get("input_refs", ()) or ()
            ),
            scope_id=scope_id,
            generation=int(getattr(activation, "generation", 0)),
            budget_tokens=budget_tokens,
        )

    # ------------------------------------------------------------------
    # Step coercion / continuation / budget
    # ------------------------------------------------------------------
    def _coerce_step(self, step: Any, handle: AgentHandle) -> AgentStep:
        if isinstance(step, AgentStep):
            return step
        if isinstance(step, Mapping):
            return AgentStep(
                actions=list(step.get("actions") or []),
                continuation=dict(step.get("continuation") or {}),
                yield_spec=step.get("yield_spec"),
                final_result=(
                    ResultEnvelope.from_dict(step["final_result"])
                    if isinstance(step.get("final_result"), Mapping)
                    else step.get("final_result")
                ),
                usage=dict(step.get("usage") or {}),
            )
        raise TypeError(
            f"step_runner for {handle.agent_id!r} returned {type(step).__name__!r}, "
            "expected AgentStep"
        )

    def _save_continuation(self, handle: AgentHandle, step: AgentStep) -> None:
        continuation = getattr(step, "continuation", None)
        if isinstance(continuation, Mapping):
            merged = dict(handle.continuation or {})
            merged.update({str(k): v for k, v in continuation.items()})
            handle.continuation = merged

    def _charge(self, handle: AgentHandle, step: AgentStep) -> None:
        usage = getattr(step, "usage", None)
        if not isinstance(usage, Mapping) or not usage:
            final = getattr(step, "final_result", None)
            usage = getattr(final, "usage", None) if final is not None else None
        if not isinstance(usage, Mapping) or not usage:
            return
        tokens = _tokens_of(usage)
        self.supervisor.charge(handle.agent_id, usage)
        self._stats["tokens_spent"] += tokens

    def _mark_processed(self, handle: AgentHandle, working_set: CollabWorkingSet) -> None:
        for message in working_set.messages:
            try:
                self.store.mark(handle.agent_id, message.message_id, "PROCESSED")
            except (KeyError, ValueError):
                continue
            self._stats["messages_processed"] += 1

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------
    def _execute_actions(
        self, handle: AgentHandle, working_set: CollabWorkingSet, step: AgentStep
    ) -> None:
        new_children: list[str] = []
        for action in list(getattr(step, "actions", None) or ()):
            action_type = _action_type(action).upper()
            data = _action_data(action)
            if action_type == "SEND_MESSAGE":
                self._do_send_message(handle, data, new_children)
            elif action_type == "SPAWN":
                child_id = self._do_spawn(handle, data)
                if child_id and child_id not in new_children:
                    new_children.append(child_id)
            elif action_type == "CANCEL":
                self._do_cancel(handle, data)
            elif action_type == "WAIT":
                self._do_wait(handle, data, new_children)
            elif action_type == "SUBMIT":
                self._do_submit(handle, step, data)
            elif action_type == "CLOSE_THREAD":
                self._do_close_thread(handle, data)
            elif action_type == "DECIDE":
                self._do_decide(handle, data)
            elif action_type == "INVALIDATE":
                self._do_invalidate(handle, data)
            else:
                self.events.append(
                    {
                        "event": "unknown_action",
                        "agent_id": handle.agent_id,
                        "action_type": action_type,
                    }
                )

    # -- SEND_MESSAGE -----------------------------------------------------
    def _do_send_message(
        self,
        handle: AgentHandle,
        data: Mapping[str, Any],
        new_children: Sequence[str] = (),
    ) -> None:
        kind = str(data.get("kind") or "NOTE").upper()
        recipients = self._expand_targets(
            handle, data.get("recipients") or data.get("to") or (), new_children
        )
        if not recipients:
            self.events.append(
                {"event": "send_skipped", "agent_id": handle.agent_id, "reason": "no recipients"}
            )
            return
        based_on = dict(data.get("based_on") or {})
        if kind in ("ANSWER", "DECISION"):
            stale, reason = self._is_stale(handle, data, based_on)
            if stale:
                self._stats["stale_answers"] += 1
                self.events.append(
                    {
                        "event": "stale_answer_rejected",
                        "agent_id": handle.agent_id,
                        "reason": reason,
                    }
                )
                return
        scope_id = data.get("scope_id") or self._scope_for(handle, recipients)
        idempotency_key = str(
            data.get("idempotency_key")
            or f"{handle.agent_id}.{self._next_message_seq()}.{kind}"
        )
        message = Message(
            message_id=str(data.get("message_id") or ""),
            kind=kind,
            sender=handle.agent_id,
            recipients=recipients,
            scope_id=str(scope_id or ""),
            thread_id=data.get("thread_id"),
            body=str(data.get("body") or ""),
            subject=str(data.get("subject") or ""),
            based_on=based_on,
            evidence_refs=tuple(
                str(ref) for ref in (data.get("evidence_refs") or ())
            ),
            reply_requirement=tuple(
                str(item) for item in (data.get("reply_requirement") or ())
            ),
            reply_to=data.get("reply_to"),
            idempotency_key=idempotency_key,
            created_at=str(data.get("created_at") or ""),
            status="STORED",
            priority=int(data.get("priority", 50) or 50),
            ttl_steps=data.get("ttl_steps"),
            metadata=dict(data.get("metadata") or {}),
        )
        try:
            stored = self.store.send(message, sender=handle.agent_id)
        except (PermissionError, ValueError) as exc:
            self._stats["denied_messages"] += 1
            self.events.append(
                {
                    "event": "send_denied",
                    "agent_id": handle.agent_id,
                    "kind": kind,
                    "reason": str(exc),
                }
            )
            return
        self._stats["messages_sent"] += 1
        self.events.append(
            {
                "event": "message_sent",
                "agent_id": handle.agent_id,
                "kind": kind,
                "message_id": stored.message_id,
                "recipients": list(stored.recipients),
            }
        )
        for recipient in stored.recipients:
            self.scheduler.notify(
                {
                    "kind": "message",
                    "event_kind": kind,
                    "agent_id": recipient,
                    "ref": stored.message_id,
                    "message_id": stored.message_id,
                    "reply_to": stored.reply_to,
                    "accepted": kind in _WAKING_KINDS,
                }
            )

    # -- SPAWN ------------------------------------------------------------
    def _do_spawn(self, handle: AgentHandle, data: Mapping[str, Any]) -> None:
        request_id = str(data.get("request_id") or self._next_spawn_id(handle))
        metadata = dict(data.get("metadata") or {})
        if data.get("task_id") and "task_id" not in metadata:
            metadata["task_id"] = str(data["task_id"])
        budget = {
            str(key): int(value)
            for key, value in dict(data.get("budget") or {}).items()
        }
        request = SpawnRequest(
            request_id=request_id,
            parent_agent=handle.agent_id,
            objective=str(data.get("objective") or ""),
            task_kind=str(data.get("task_kind") or "analyze"),
            input_refs=tuple(str(ref) for ref in (data.get("input_refs") or ())),
            acceptance=tuple(
                str(item) for item in (data.get("acceptance") or ())
            ),
            constraints=tuple(
                str(item) for item in (data.get("constraints") or ())
            ),
            capabilities=tuple(
                str(item) for item in (data.get("capabilities") or ())
            ),
            join_parent_room=bool(data.get("join_parent_room", True)),
            allowed_threads=tuple(
                str(item) for item in (data.get("allowed_threads") or ())
            ),
            alloc_ref=str(data.get("alloc_ref") or handle.alloc_id),
            budget=budget,
            max_children=int(data.get("max_children", 0) or 0),
            join_policy=str(data.get("join_policy") or "REQUIRED"),
            risk=str(data.get("risk") or "medium"),
            metadata=metadata,
        )
        decision = self.supervisor.spawn(request)
        created_child: str | None = None
        self.events.append(
            {
                "event": "spawn_decision",
                "agent_id": handle.agent_id,
                "request_id": request_id,
                "status": decision.status,
                "child_id": decision.agent_id,
                "reason": decision.reason,
            }
        )
        if decision.status in ("CREATED", "QUEUED"):
            created_child = decision.agent_id
            child = self.supervisor.agent(decision.agent_id)
            contract = dict(child.continuation or {})
            contract.setdefault("objective", request.objective)
            self._contracts[child.agent_id] = contract
            self.scheduler.add_agent(
                child.agent_id, priority=int(data.get("priority", 50) or 50)
            )
        elif decision.status == "REUSED" and decision.agent_id:
            created_child = decision.agent_id
            if decision.agent_id not in self._contracts:
                child = self.supervisor.agent(decision.agent_id)
                self._contracts[child.agent_id] = dict(child.continuation or {})
            if self.scheduler.status(decision.agent_id) == "CREATED":
                self.scheduler.add_agent(decision.agent_id)
        else:
            self._stats["denied_spawns"] += 1
        return created_child

    # -- CANCEL -----------------------------------------------------------
    def _do_cancel(self, handle: AgentHandle, data: Mapping[str, Any]) -> None:
        target = str(data.get("agent_id") or data.get("target") or "")
        if not target:
            return
        reason = str(data.get("reason") or "cancelled by parent")
        propagate = bool(data.get("propagate", True))
        try:
            cancelled = self.supervisor.cancel(target, reason, propagate=propagate)
        except KeyError:
            self.events.append(
                {"event": "cancel_unknown", "agent_id": handle.agent_id, "target": target}
            )
            return
        for agent_id in cancelled:
            self.scheduler.complete(agent_id)
        self._stats["cancelled"] += len(cancelled)
        self.events.append(
            {
                "event": "cancelled",
                "agent_id": handle.agent_id,
                "target": target,
                "cancelled": list(cancelled),
            }
        )

    # -- WAIT -------------------------------------------------------------
    def _do_wait(
        self,
        handle: AgentHandle,
        data: Mapping[str, Any],
        new_children: Sequence[str] = (),
    ) -> None:
        expanded = dict(data)
        expanded["refs"] = self._expand_targets(
            handle, data.get("refs") or (), new_children
        )
        condition = _condition_from(expanded)
        self._park(handle, condition)

    def _expand_targets(
        self,
        handle: AgentHandle,
        values: Iterable[Any],
        new_children: Sequence[str] = (),
    ) -> tuple[str, ...]:
        """Resolve ``$children`` / ``$new_children`` dynamic targeting aliases.

        A model can create children and address them in the same activation
        without knowing the runtime-generated ids.
        """
        out: list[str] = []
        for value in values or ():
            token = str(value)
            if token in ("$children", "$all_children", "$child"):
                candidates = tuple(handle.children or ())
            elif token in ("$new_children", "$new_child"):
                candidates = tuple(new_children or ())
            else:
                candidates = (token,)
            for candidate in candidates:
                if candidate and candidate not in out:
                    out.append(candidate)
        return tuple(out)

    def _park(self, handle: AgentHandle, condition: WaitCondition) -> None:
        self.scheduler.wait(handle.agent_id, condition)
        handle.status = "WAITING"
        handle.waiting_on = condition
        self._stats["yields"] += 1
        self.events.append(
            {
                "event": "wait",
                "agent_id": handle.agent_id,
                "kind": str(getattr(condition, "kind", "")),
                "refs": list(getattr(condition, "refs", ()) or ()),
            }
        )
        # If the wait depends on work that already finished before this
        # activation, replay those completions so the agent cannot sleep on an
        # event that happened earlier in the run.
        for ref in tuple(getattr(condition, "refs", ()) or ()):
            ref = str(ref)
            if ref in self._results or self._is_terminal(ref):
                self.scheduler.notify(
                    {
                        "kind": "child_done",
                        "event_kind": "CHILD_DONE",
                        "agent_id": handle.agent_id,
                        "ref": ref,
                        "refs": [ref],
                    }
                )

    def _apply_yield(
        self, handle: AgentHandle, working_set: CollabWorkingSet, step: AgentStep
    ) -> None:
        yield_spec = getattr(step, "yield_spec", None)
        if yield_spec is None or self.scheduler.is_waiting(handle.agent_id):
            return
        if handle.status in ("CLOSED", "FAILED"):
            return
        kind = str(getattr(yield_spec, "kind", "") or "")
        refs = tuple(str(ref) for ref in (getattr(yield_spec, "refs", ()) or ()))
        if kind == "WAIT_FOR_REPLY":
            self._park(
                handle,
                WaitCondition(
                    kind="UNTIL_EVENT",
                    refs=refs,
                    event_kinds=("ANSWER", "DECISION", "INVALIDATE"),
                    description=str(getattr(yield_spec, "note", "") or ""),
                ),
            )
        elif kind == "WAIT_FOR_CHILDREN":
            self._park(
                handle,
                WaitCondition(
                    kind="ALL_REQUIRED",
                    refs=refs,
                    description=str(getattr(yield_spec, "note", "") or ""),
                ),
            )
        elif kind == "WAIT_FOR_EVENT":
            self._park(
                handle,
                WaitCondition(
                    kind="UNTIL_EVENT",
                    refs=refs,
                    event_kinds=(),
                    description=str(getattr(yield_spec, "note", "") or ""),
                ),
            )
        elif kind == "DONE":
            if handle.agent_id not in self._results:
                self._results[handle.agent_id] = ResultEnvelope(
                    task_id=handle.task_id,
                    status=STATUS_COMPLETED,
                    summary=str(
                        dict(handle.continuation or {}).get("next_step", "done")
                    ),
                )
            self._close_agent(handle, "done")
        elif kind == "BLOCKED":
            self._fail_agent(handle, str(getattr(yield_spec, "note", "") or "blocked"))

    # -- SUBMIT -----------------------------------------------------------
    def _do_submit(
        self, handle: AgentHandle, step: AgentStep, data: Mapping[str, Any]
    ) -> None:
        final = getattr(step, "final_result", None)
        raw_result = data.get("result")
        if isinstance(raw_result, Mapping):
            final = ResultEnvelope.from_dict(raw_result)
        elif isinstance(final, Mapping):
            final = ResultEnvelope.from_dict(final)
        continuation = data.get("continuation")
        if isinstance(continuation, Mapping) and continuation:
            merged = dict(handle.continuation or {})
            merged.update({str(key): value for key, value in continuation.items()})
            handle.continuation = merged
        if not isinstance(final, ResultEnvelope):
            final = ResultEnvelope(
                task_id=handle.task_id,
                status=str(data.get("status") or STATUS_COMPLETED),
                summary=str(data.get("summary") or ""),
                payload={
                    str(key): value
                    for key, value in data.items()
                    if key not in ("status", "summary", "result", "continuation")
                },
            )
        self._results[handle.agent_id] = final
        self.events.append(
            {
                "event": "submitted",
                "agent_id": handle.agent_id,
                "status": final.status,
                "task_id": handle.task_id,
            }
        )
        self._close_agent(handle, "submitted")

    # -- threads ----------------------------------------------------------
    def _do_close_thread(self, handle: AgentHandle, data: Mapping[str, Any]) -> None:
        thread_id = str(data.get("thread_id") or "")
        if not thread_id:
            return
        decision = str(data.get("decision") or data.get("resolution") or "")
        self._close_thread(thread_id, decision, handle.agent_id)
        self.scheduler.notify(
            {
                "kind": "thread_update",
                "event_kind": "THREAD_UPDATE",
                "ref": thread_id,
                "thread_id": thread_id,
            }
        )

    def _do_decide(self, handle: AgentHandle, data: Mapping[str, Any]) -> None:
        thread_id = str(data.get("thread_id") or "")
        decision = str(data.get("decision") or "")
        if thread_id:
            self._close_thread(thread_id, decision, handle.agent_id)
        if data.get("recipients"):
            payload = dict(data)
            payload["kind"] = "DECISION"
            self._do_send_message(handle, payload)
        self.scheduler.notify(
            {
                "kind": "thread_update",
                "event_kind": "THREAD_UPDATE",
                "ref": thread_id or "",
                "thread_id": thread_id or "",
            }
        )

    def _close_thread(self, thread_id: str, decision: str, decided_by: str) -> None:
        try:
            self.store.close_thread(thread_id, decision, decided_by)
        except (KeyError, PermissionError, ValueError) as exc:
            self.events.append(
                {
                    "event": "thread_close_denied",
                    "thread_id": thread_id,
                    "reason": str(exc),
                }
            )
            return
        self.events.append(
            {
                "event": "thread_closed",
                "thread_id": thread_id,
                "decision": decision,
                "decided_by": decided_by,
            }
        )

    def _do_invalidate(self, handle: AgentHandle, data: Mapping[str, Any]) -> None:
        key = str(data.get("thread_id") or data.get("scope_id") or "")
        if not key:
            key = str(self._scope_for(handle, ()) or "")
        version = data.get("snapshot_version", data.get("version"))
        if key:
            if version is None:
                self._invalidated[key] = int(self._invalidated.get(key, 0)) + 1
            else:
                try:
                    version_int = int(version)
                except (TypeError, ValueError):
                    version_int = int(self._invalidated.get(key, 0)) + 1
                self._invalidated[key] = max(
                    int(self._invalidated.get(key, 0)), version_int
                )
        if data.get("recipients"):
            payload = dict(data)
            payload["kind"] = "INVALIDATE"
            self._do_send_message(handle, payload)
        self.events.append(
            {
                "event": "invalidated",
                "agent_id": handle.agent_id,
                "key": key,
                "version": int(self._invalidated.get(key, 0)),
            }
        )
        self.scheduler.notify(
            {
                "kind": "thread_update",
                "event_kind": "INVALIDATE",
                "agent_id": None,
                "ref": key,
            }
        )

    # ------------------------------------------------------------------
    # Staleness / scopes
    # ------------------------------------------------------------------
    def _is_stale(
        self, handle: AgentHandle, data: Mapping[str, Any], based_on: Mapping[str, Any]
    ) -> tuple[bool, str]:
        thread_id = str(data.get("thread_id") or "")
        scope_id = str(
            data.get("scope_id") or self._scope_for(handle, ()) or ""
        )
        thresholds = [
            self._invalidated.get(key, 0)
            for key in {thread_id, scope_id}
            if key
        ]
        threshold = max(thresholds) if thresholds else 0
        if not threshold:
            return False, ""
        version = _based_on_version(based_on)
        if version is None:
            return False, ""
        if version < threshold:
            return (
                True,
                f"based_on version {version} < invalidated version {threshold}",
            )
        return False, ""

    def _scope_for(
        self, handle: AgentHandle, recipients: Iterable[str]
    ) -> str | None:
        """Pick the local room in which a message is legal (default local)."""
        recipients = tuple(str(item) for item in recipients)
        children = set(self.supervisor.children_of(handle.agent_id))
        if recipients and all(recipient in children for recipient in recipients):
            return self.supervisor.ensure_room(handle.agent_id)
        parent_id = handle.parent_id
        if parent_id is not None:
            family = set(self.supervisor.children_of(parent_id))
            family.add(parent_id)
            if all(recipient in family for recipient in recipients):
                return self.supervisor.ensure_room(parent_id)
        return self.supervisor.ensure_room(handle.agent_id)

    # ------------------------------------------------------------------
    # Terminal transitions
    # ------------------------------------------------------------------
    def _close_agent(self, handle: AgentHandle, reason: str) -> None:
        agent_id = handle.agent_id
        handle.status = "CLOSED"
        handle.closed_reason = reason
        handle.waiting_on = None
        self.scheduler.complete(agent_id)
        self.supervisor.finish(agent_id)
        self.supervisor.settle(agent_id, {})
        if agent_id == self.root_id:
            self._finished = True
            self.events.append({"event": "root_submitted", "agent_id": agent_id})
            return
        self._notify_parent(handle)

    def _fail_agent(self, handle: AgentHandle, reason: str) -> None:
        agent_id = handle.agent_id
        handle.status = "FAILED"
        handle.closed_reason = f"failed: {reason}"
        handle.waiting_on = None
        self.scheduler.complete(agent_id)
        self.supervisor.finish(agent_id)
        self.supervisor.settle(agent_id, {})
        self._stats["failed_agents"] += 1
        self.events.append(
            {"event": "agent_failed", "agent_id": agent_id, "reason": reason}
        )
        if agent_id == self.root_id:
            self._finished = True
            return
        self._notify_parent(handle)

    def _settle_if_terminal(self, handle: AgentHandle) -> None:
        if handle.status in ("CLOSED", "FAILED"):
            self.supervisor.settle(handle.agent_id, {})

    def _notify_parent(self, handle: AgentHandle) -> None:
        parent_id = handle.parent_id
        if not parent_id:
            return
        self.scheduler.notify(
            {
                "kind": "child_done",
                "event_kind": "CHILD_DONE",
                "agent_id": parent_id,
                "ref": handle.agent_id,
                "refs": [handle.agent_id, handle.task_id],
                "child_id": handle.agent_id,
                "task_id": handle.task_id,
            }
        )

    def _is_terminal(self, agent_id: str) -> bool:
        return self.supervisor.handles.get(agent_id) is not None and (
            self.supervisor.handles[agent_id].status in ("CLOSED", "FAILED")
        )

    def _abort_activation(self, activation: Any, reason: str) -> None:
        try:
            activation.abort(reason)
        except StaleActivation:
            pass

    def _next_message_seq(self) -> int:
        self._message_seq += 1
        return self._message_seq

    def _next_spawn_id(self, handle: AgentHandle) -> str:
        self._spawn_seq += 1
        return f"spawn.{handle.agent_id}.{self._spawn_seq}"

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    def _build_report(self) -> dict[str, Any]:
        scheduler_stats = self.scheduler.stats()
        cycles = self.scheduler.deadlock_check()
        root_result = self._results.get(self.root_id or "root")
        if root_result is not None:
            status = "COMPLETED" if root_result.ok else str(root_result.status)
        elif cycles:
            status = "DEADLOCK"
        elif self.scheduler.stats()["waiting"]:
            status = "WAITING"
        elif self._finished:
            status = "INCOMPLETE"
        else:
            status = "RUNNING"
        closed = sum(
            1
            for handle in self.supervisor.handles.values()
            if handle.status in ("CLOSED", "FAILED")
        )
        tokens_spent = sum(
            int(allocation.spent) for allocation in self.supervisor.allocations.values()
        )
        return {
            "status": status,
            "steps": self._steps,
            "wakes": scheduler_stats["wakes"],
            "coalesced": scheduler_stats["coalesced"],
            "denied_spawns": self._stats["denied_spawns"],
            "deadlock_cycles": [list(cycle) for cycle in cycles],
            "messages_processed": self._stats["messages_processed"],
            "messages_sent": self._stats["messages_sent"],
            "stale_answers": self._stats["stale_answers"],
            "budget_errors": self._stats["budget_errors"],
            "failed_agents": self._stats["failed_agents"],
            "cancelled": self._stats["cancelled"],
            "yields": self._stats["yields"],
            "tokens_spent": tokens_spent,
            "agents": len(self.supervisor.handles),
            "closed_agents": closed,
            "root_status": root_result.status if root_result is not None else None,
            "result": root_result.to_dict() if root_result is not None else None,
            "results": {
                agent_id: result.to_dict() for agent_id, result in self._results.items()
            },
            "scheduler": scheduler_stats,
            "wait_graph": {
                key: sorted(values)
                for key, values in self.scheduler.snapshot()["wait_edges"].items()
            },
            "events": list(self.events),
        }


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _tokens_of(usage: Mapping[str, Any]) -> int:
    if "tokens" in usage:
        try:
            return max(0, int(usage.get("tokens") or 0))
        except (TypeError, ValueError):
            return 0
    total = 0
    for key in ("input", "output", "input_tokens", "output_tokens"):
        try:
            total += max(0, int(usage.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return total


def _action_type(action: Any) -> str:
    if isinstance(action, Mapping):
        return str(action.get("type") or "")
    return str(getattr(action, "type", "") or "")


def _action_data(action: Any) -> dict[str, Any]:
    if isinstance(action, Mapping):
        data = action.get("data")
        if isinstance(data, Mapping):
            return dict(data)
        return {
            str(key): value
            for key, value in action.items()
            if key not in ("type", "data")
        }
    data = getattr(action, "data", None)
    return dict(data) if isinstance(data, Mapping) else {}


def _condition_from(data: Mapping[str, Any]) -> WaitCondition:
    kind = str(data.get("kind") or data.get("wait_kind") or "UNTIL_EVENT").upper()
    refs = tuple(str(ref) for ref in (data.get("refs") or ()))
    event_kinds = tuple(str(item) for item in (data.get("event_kinds") or ()))
    deadline = data.get("deadline_s")
    return WaitCondition(
        kind=kind,
        refs=refs,
        event_kinds=event_kinds,
        deadline_s=float(deadline) if deadline is not None else None,
        description=str(data.get("description") or ""),
    )


def _based_on_version(based_on: Mapping[str, Any]) -> int | None:
    for key in ("snapshot_version", "contract_version", "version"):
        value = based_on.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    snapshot = based_on.get("snapshot")
    if isinstance(snapshot, Mapping):
        value = snapshot.get("version")
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None
