"""Collaboration store: scopes, threads, messages, mailboxes and state machines.

This module implements spec 3.1 of ``docs/MULTI_AGENT_INTERFACES.md``.  The
store is deliberately *not* a chat room: it is the persistence + access-control
layer the runtime consults.

Key properties enforced here (and tested in ``tests/test_collaboration.py``):

* **scope membership is the permission boundary** -- a sender and every
  recipient must belong to the scope, which is what keeps sibling branches from
  talking directly (a temporary ``THREAD`` scope is the explicit exception);
* **messages are events stored once** and projected into per-recipient
  mailboxes; per-mailbox status is the source of truth, ``Message.status`` is
  only an aggregate view;
* **duplicate delivery is safe** -- the same ``(sender, idempotency_key)`` pair
  returns the already stored message without a second side effect;
* **the message status machine only moves forward**
  ``STORED -> DELIVERED -> IN_CONTEXT -> PROCESSED``;
* **the context budget never silently drops mandatory messages** -- when not a
  single mandatory message fits, :class:`ContextBudgetError` is raised and all
  messages stay pending;
* **archive closes a room** (no new deliveries) but keeps the decisions and
  artifact references readable.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from .collab_types import (
    IMPORTANT_MESSAGE_KINDS,
    MESSAGE_ANSWER,
    MESSAGE_ASK,
    MESSAGE_DECISION,
    MESSAGE_INVALIDATE,
    MESSAGE_KINDS,
    MESSAGE_STATUS_IN_CONTEXT,
    MESSAGE_STATUS_ORDER,
    MESSAGE_STATUS_PROCESSED,
    MESSAGE_STATUS_STORED,
    ContextBudgetError,
    Message,
    MessageStatus,
    Scope,
    ScopeKind,
    ThreadState,
)

__all__ = ["CollaborationStore", "ContextBudgetError"]


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    """Default clock: ISO-8601 UTC seconds (tests inject a deterministic clock)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    """Order-preserving de-duplication of a string sequence."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def _normalise_ref(ref: str) -> str:
    """Normalise a ``file://`` / ``artifact://`` ref for prefix comparison."""
    return str(ref).strip().replace("\\", "/")


def _estimate_message_tokens(message: Message) -> int:
    """Default token estimator: character count of the rendered body.

    The spec signature writes ``estimator=len``; ``len(Message)`` is not
    callable, so the default is the natural reading ``len(message.body)``.
    Runtime callers can inject their own estimator.
    """
    return len(message.body or "")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class CollaborationStore:
    """In-memory, deterministic store for the v0.3 collaboration layer."""

    def __init__(self, clock: Callable[[], str] | None = None) -> None:
        self._clock: Callable[[], str] = clock or _utcnow
        self._scopes: dict[str, Scope] = {}
        self._threads: dict[str, ThreadState] = {}
        self._messages: dict[str, Message] = {}
        self._events: list[str] = []
        self._seq: dict[str, int] = {}
        self._mailboxes: dict[str, list[str]] = {}
        self._status: dict[tuple[str, str], MessageStatus] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._scope_seq = 0
        self._thread_seq = 0
        self._message_seq = 0

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------
    def scope(self, scope_id: str) -> Scope:
        """Return the scope or raise ``KeyError``."""
        return self._require_scope(scope_id)

    def thread(self, thread_id: str) -> ThreadState:
        """Return the thread or raise ``KeyError``."""
        return self._require_thread(thread_id)

    def get_message(self, message_id: str) -> Message:
        """Return a stored message or raise ``KeyError``."""
        message = self._messages.get(message_id)
        if message is None:
            raise KeyError(f"unknown message {message_id!r}")
        return message

    @property
    def events(self) -> tuple[Message, ...]:
        """The event log in storage order."""
        return tuple(self._messages[message_id] for message_id in self._events)

    @property
    def message_count(self) -> int:
        return len(self._events)

    # ------------------------------------------------------------------
    # Scopes
    # ------------------------------------------------------------------
    def create_scope(
        self,
        kind: ScopeKind,
        owner: str,
        members: Sequence[str] = (),
        parent_scope: str | None = None,
        allowed_artifacts: Sequence[str] = (),
        allowed_threads: Sequence[str] = (),
        close_condition: str = "",
    ) -> Scope:
        """Create a ``ROOM`` (parent + direct children) or a ``THREAD`` scope.

        The owner is always part of ``members`` so that the owner can post
        decisions and invalidations; this is a small, documented deviation from
        a literal reading of the spec (the owner would otherwise be locked out
        of its own scope).
        """
        if kind not in ("ROOM", "THREAD"):
            raise ValueError(f"unknown scope kind {kind!r}")
        if not owner or not owner.strip():
            raise ValueError("scope owner must be a non-empty string")
        if parent_scope is not None:
            parent = self._require_scope(parent_scope)
            if not parent.open:
                raise ValueError(f"parent scope {parent_scope!r} is closed")
        self._scope_seq += 1
        scope = Scope(
            scope_id=f"scope_{self._scope_seq}",
            kind=kind,
            owner=owner,
            members=_dedupe((owner, *members)),
            parent_scope=parent_scope,
            allowed_artifacts=tuple(str(item) for item in allowed_artifacts),
            allowed_threads=tuple(str(item) for item in allowed_threads),
            open=True,
            close_condition=close_condition,
            created_at=self._clock(),
        )
        self._scopes[scope.scope_id] = scope
        return scope

    def add_member(self, scope_id: str, agent_id: str) -> Scope:
        """Add a member to an open scope (idempotent)."""
        scope = self._require_scope(scope_id)
        if not scope.open:
            raise ValueError(f"scope {scope_id!r} is closed")
        if not agent_id or not agent_id.strip():
            raise ValueError("agent_id must be a non-empty string")
        if agent_id not in scope.members:
            scope.members = (*scope.members, agent_id)
        return scope

    def remove_member(self, scope_id: str, agent_id: str) -> Scope:
        """Remove a member from an open scope.

        Removing the owner is rejected: it would leave a room whose decisions
        can no longer be posted.
        """
        scope = self._require_scope(scope_id)
        if not scope.open:
            raise ValueError(f"scope {scope_id!r} is closed")
        if agent_id not in scope.members:
            raise KeyError(f"agent {agent_id!r} is not a member of scope {scope_id!r}")
        if agent_id == scope.owner:
            raise ValueError("cannot remove the scope owner")
        scope.members = tuple(member for member in scope.members if member != agent_id)
        return scope

    def archive_scope(self, scope_id: str) -> Scope:
        """Close a room: decisions and artifact references stay readable.

        No new messages are accepted afterwards (``send`` raises ``ValueError``),
        and the call is idempotent.
        """
        scope = self._require_scope(scope_id)
        scope.open = False
        return scope

    # ------------------------------------------------------------------
    # Threads (topics)
    # ------------------------------------------------------------------
    def open_thread(
        self,
        scope_id: str,
        question: str,
        owner: str,
        required_evidence: Sequence[str] = (),
        close_condition: str = "",
    ) -> ThreadState:
        """Open a topic inside a scope and make its owner a scope member."""
        scope = self._require_scope(scope_id)
        if not scope.open:
            raise ValueError(f"scope {scope_id!r} is closed")
        if not question or not question.strip():
            raise ValueError("thread question must be a non-empty string")
        if not owner or not owner.strip():
            raise ValueError("thread owner must be a non-empty string")
        # The thread owner must be able to post a DECISION into the scope.
        if owner not in scope.members:
            scope.members = (*scope.members, owner)
        self._thread_seq += 1
        thread = ThreadState(
            thread_id=f"thread_{self._thread_seq}",
            scope_id=scope_id,
            question=question,
            owner=owner,
            required_evidence=[str(item) for item in required_evidence],
            close_condition=close_condition,
        )
        self._threads[thread.thread_id] = thread
        return thread

    def close_thread(
        self, thread_id: str, decision: str, decided_by: str
    ) -> ThreadState:
        """Record the resolution of a thread; only its owner may decide."""
        thread = self._require_thread(thread_id)
        if not decision or not decision.strip():
            raise ValueError("decision must be a non-empty string")
        if not decided_by or decided_by != thread.owner:
            raise PermissionError(
                f"agent {decided_by!r} may not decide thread {thread_id!r} "
                f"(owner is {thread.owner!r})"
            )
        if thread.closed:
            if thread.decision == decision and thread.decided_by == decided_by:
                return thread  # idempotent re-close
            raise ValueError(f"thread {thread_id!r} is already closed")
        thread.decision = decision
        thread.decided_by = decided_by
        thread.closed = True
        return thread

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    def send(self, message: Message, *, sender: str) -> Message:
        """Store one message event and project it into every recipient mailbox.

        Validation order is deliberate: the idempotency lookup happens before
        the open-scope check so a retry of an already stored message stays safe
        even after the room has been archived.
        """
        if not isinstance(message, Message):
            raise TypeError("send() expects a Message instance")
        if not message.scope_id:
            raise ValueError("message.scope_id must be a non-empty string")
        scope = self._require_scope(message.scope_id)

        # 1) Idempotent replay: same sender + key -> same stored message.
        if message.idempotency_key:
            existing_id = self._idempotency.get((sender, message.idempotency_key))
            if existing_id is not None:
                return self._messages[existing_id]

        # 2) Structural validation.
        if message.kind not in MESSAGE_KINDS:
            raise ValueError(f"unknown message kind {message.kind!r}")
        if not scope.open:
            raise ValueError(f"scope {scope.scope_id!r} is closed; delivery refused")
        if not sender or sender not in scope.members:
            raise PermissionError(
                f"agent {sender!r} is not a member of scope {scope.scope_id!r}"
            )
        if message.sender and message.sender != sender:
            raise PermissionError(
                f"message.sender {message.sender!r} does not match authenticated "
                f"sender {sender!r}"
            )
        recipients = _dedupe(message.recipients)
        outsiders = [item for item in recipients if item not in scope.members]
        if outsiders:
            raise PermissionError(
                f"recipients {outsiders!r} are not members of scope "
                f"{scope.scope_id!r}"
            )

        # 3) Thread resolution (a message may only refer to a thread of its scope).
        thread: ThreadState | None = None
        if message.thread_id is not None:
            thread = self._require_thread(message.thread_id)
            if thread.scope_id != scope.scope_id:
                raise ValueError(
                    f"thread {thread.thread_id!r} belongs to scope "
                    f"{thread.scope_id!r}, not {scope.scope_id!r}"
                )

        # 4) Kind-specific permissions.
        if message.kind == MESSAGE_ANSWER:
            if not message.reply_to:
                raise ValueError("ANSWER requires reply_to")
            target = self._messages.get(message.reply_to)
            if target is None:
                raise ValueError(f"ANSWER reply_to {message.reply_to!r} does not exist")
            if target.kind != MESSAGE_ASK:
                raise ValueError(
                    f"ANSWER reply_to {message.reply_to!r} is a {target.kind}, not an ASK"
                )
        if message.kind == MESSAGE_DECISION:
            if thread is None:
                raise ValueError("DECISION requires a thread_id")
            if sender != thread.owner:
                raise PermissionError(
                    f"only thread owner {thread.owner!r} may send DECISION "
                    f"(sender {sender!r})"
                )
        if message.kind == MESSAGE_INVALIDATE:
            allowed = sender == scope.owner or (
                thread is not None and sender == thread.owner
            )
            if not allowed:
                raise PermissionError(
                    "INVALIDATE is limited to the scope owner or the thread owner"
                )

        # 5) Persist: assign id/seq, normalise, store, project, log.
        self._message_seq += 1
        seq = self._message_seq
        message_id = message.message_id or f"msg_{seq}"
        if message_id in self._messages:
            raise ValueError(f"duplicate message_id {message_id!r}")
        stored = replace(
            message,
            message_id=message_id,
            sender=sender,
            recipients=recipients,
            created_at=message.created_at or self._clock(),
            status=MESSAGE_STATUS_STORED,
        )
        self._messages[message_id] = stored
        self._events.append(message_id)
        self._seq[message_id] = seq
        for recipient in recipients:
            self._mailboxes.setdefault(recipient, []).append(message_id)
            self._status[(recipient, message_id)] = MESSAGE_STATUS_STORED
        if stored.idempotency_key:
            self._idempotency[(sender, stored.idempotency_key)] = message_id
        if thread is not None:
            thread.message_ids.append(message_id)
        return stored

    # ------------------------------------------------------------------
    # Mailboxes and status machine
    # ------------------------------------------------------------------
    def mailbox(self, agent_id: str) -> list[Message]:
        """All messages projected to ``agent_id``: priority desc, seq asc."""
        message_ids = self._mailboxes.get(agent_id, ())
        messages = [self._view(message_id, agent_id) for message_id in message_ids]
        messages.sort(key=lambda msg: (-int(msg.priority), self._seq.get(msg.message_id, 0)))
        return messages

    def pending(self, agent_id: str) -> list[Message]:
        """Messages whose per-recipient status has not reached ``PROCESSED``."""
        return [
            message
            for message in self.mailbox(agent_id)
            if message.status != MESSAGE_STATUS_PROCESSED
        ]

    def mark(self, agent_id: str, message_id: str, status: MessageStatus) -> MessageStatus:
        """Advance one recipient's view of a message through the state machine.

        Re-marking the current status is a no-op (idempotent); moving backwards
        raises ``ValueError``; an unknown message for this agent raises
        ``KeyError``.  Returns the (possibly unchanged) status.
        """
        if status not in MESSAGE_STATUS_ORDER:
            raise ValueError(f"unknown message status {status!r}")
        if message_id not in self._mailboxes.get(agent_id, ()):
            raise KeyError(
                f"message {message_id!r} is not in the mailbox of agent {agent_id!r}"
            )
        current = self._status[(agent_id, message_id)]
        if status == current:
            return current
        if MESSAGE_STATUS_ORDER[status] < MESSAGE_STATUS_ORDER[current]:
            raise ValueError(
                f"illegal status regression {current} -> {status} "
                f"(statuses only move forward)"
            )
        self._status[(agent_id, message_id)] = status
        self._refresh_aggregate(message_id)
        return status

    # ------------------------------------------------------------------
    # Context selection
    # ------------------------------------------------------------------
    def select_for_context(
        self,
        agent_id: str,
        *,
        budget_tokens: int,
        max_messages: int = 20,
        estimator: Callable[[Message], int] | None = None,
    ) -> tuple[list[Message], list[Message]]:
        """Pack pending messages into a token budget.

        Returns ``(selected, deferred)``.  Selected messages are marked
        ``IN_CONTEXT``; deferred messages keep their current status and stay in
        the pending set (never silently dropped).  Selection order is:
        mandatory messages first, then ASK/ANSWER/INVALIDATE/DECISION, then
        NOTE/PROPOSE/CHALLENGE; ties break on priority desc, then seq asc.

        Raises :class:`ContextBudgetError` when at least one mandatory message
        exists but not even one of them fits.
        """
        if budget_tokens < 0:
            raise ValueError("budget_tokens must be >= 0")
        if max_messages < 0:
            raise ValueError("max_messages must be >= 0")
        estimate = estimator or _estimate_message_tokens
        candidates = self.pending(agent_id)
        if not candidates:
            return [], []

        def sort_key(message: Message) -> tuple[int, int, int]:
            if message.is_mandatory:
                category = 0
            elif message.kind in IMPORTANT_MESSAGE_KINDS:
                category = 1
            else:
                category = 2
            return (category, -int(message.priority), self._seq.get(message.message_id, 0))

        ordered = sorted(candidates, key=sort_key)
        selected_ids: list[str] = []
        deferred: list[Message] = []
        consumed = 0
        mandatory_total = 0
        mandatory_selected = 0

        for message in ordered:
            mandatory = message.is_mandatory
            if mandatory:
                mandatory_total += 1
            if len(selected_ids) >= max_messages:
                deferred.append(message)
                continue
            try:
                cost = max(0, int(estimate(message)))
            except Exception:  # a broken estimator must not lose the message
                cost = max(0, _estimate_message_tokens(message))
            if consumed + cost <= budget_tokens:
                consumed += cost
                selected_ids.append(message.message_id)
                if mandatory:
                    mandatory_selected += 1
            else:
                deferred.append(message)

        if mandatory_total and mandatory_selected == 0:
            first = next(message for message in ordered if message.is_mandatory)
            cost = max(0, int(estimate(first)))
            raise ContextBudgetError(
                f"mandatory message {first.message_id!r} needs {cost} tokens but "
                f"budget is {budget_tokens}",
                message_id=first.message_id,
                required_tokens=cost,
                budget_tokens=budget_tokens,
            )

        for message_id in selected_ids:
            self.mark(agent_id, message_id, MESSAGE_STATUS_IN_CONTEXT)
        selected = [self._view(message_id, agent_id) for message_id in selected_ids]
        return selected, deferred

    # ------------------------------------------------------------------
    # Board / artifact access
    # ------------------------------------------------------------------
    def board(self, scope_id: str) -> dict[str, Any]:
        """A short directory of the room, without replaying full history.

        Contains: members, open threads, decided conventions (closed threads)
        and the artifact refs mentioned so far -- exactly the material a newly
        joining agent needs, not the message log.
        """
        scope = self._require_scope(scope_id)
        open_threads: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        for thread in self._threads.values():
            if thread.scope_id != scope_id:
                continue
            if thread.closed:
                decisions.append(
                    {
                        "thread_id": thread.thread_id,
                        "question": thread.question,
                        "decision": thread.decision,
                        "decided_by": thread.decided_by,
                    }
                )
            else:
                open_threads.append(
                    {
                        "thread_id": thread.thread_id,
                        "question": thread.question,
                        "owner": thread.owner,
                        "unresolved": list(thread.unresolved),
                        "required_evidence": list(thread.required_evidence),
                        "close_condition": thread.close_condition,
                        "message_count": len(thread.message_ids),
                    }
                )
        artifacts: set[str] = set(scope.allowed_artifacts)
        message_count = 0
        for message_id in self._events:
            message = self._messages[message_id]
            if message.scope_id != scope_id:
                continue
            message_count += 1
            for ref in message.evidence_refs:
                if "://" in ref:
                    artifacts.add(ref)
        return {
            "scope_id": scope.scope_id,
            "kind": scope.kind,
            "owner": scope.owner,
            "parent_scope": scope.parent_scope,
            "open": scope.open,
            "close_condition": scope.close_condition,
            "members": list(scope.members),
            "open_threads": open_threads,
            "decisions": decisions,
            "artifacts": sorted(artifacts),
            "allowed_artifacts": list(scope.allowed_artifacts),
            "allowed_threads": list(scope.allowed_threads),
            "message_count": message_count,
        }

    def check_artifact_access(self, agent_id: str, ref: str) -> bool:
        """True iff a scope the agent belongs to whitelists ``ref`` by prefix.

        ``file://`` and ``artifact://`` refs are normalised (slashes) and
        compared the same way; there is no scheme-specific privilege.  Members
        of an archived scope keep read access to the refs that scope retained.
        """
        if not ref or "://" not in ref:
            return False
        normalised = _normalise_ref(ref)
        for scope in self._scopes.values():
            if agent_id not in scope.members:
                continue
            for prefix in scope.allowed_artifacts:
                if not prefix or "://" not in prefix:
                    continue
                if normalised.startswith(_normalise_ref(prefix)):
                    return True
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _require_scope(self, scope_id: str) -> Scope:
        scope = self._scopes.get(scope_id)
        if scope is None:
            raise KeyError(f"unknown scope {scope_id!r}")
        return scope

    def _require_thread(self, thread_id: str) -> ThreadState:
        thread = self._threads.get(thread_id)
        if thread is None:
            raise KeyError(f"unknown thread {thread_id!r}")
        return thread

    def _view(self, message_id: str, agent_id: str) -> Message:
        """Project the canonical message with this recipient's own status."""
        message = self._messages[message_id]
        status = self._status.get((agent_id, message_id), message.status)
        return replace(message, status=status)

    def _refresh_aggregate(self, message_id: str) -> None:
        """Aggregate ``Message.status`` = least advanced recipient status."""
        stored = self._messages[message_id]
        statuses = [
            self._status[(recipient, message_id)]
            for recipient in stored.recipients
            if (recipient, message_id) in self._status
        ]
        if statuses:
            stored.status = min(
                statuses, key=lambda status: MESSAGE_STATUS_ORDER.get(status, 0)
            )
