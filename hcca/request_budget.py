"""Shared request limits for a parent activation and its delegated workers."""

from __future__ import annotations

import threading
import time


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("request deadline reached" if reason == "deadline" else "request call budget exhausted")


class CallBudget:
    def __init__(self, max_calls: int | None, deadline: float | None = None) -> None:
        self.max_calls = None if max_calls is None else max(0, int(max_calls))
        self.deadline = deadline
        self._used = 0
        self._reserve = 0
        self._pool = self
        self._lock = threading.Lock()

    @property
    def remaining(self) -> int | float:
        with self._pool._lock:
            if self._pool.max_calls is None:
                return float("inf")
            return max(0, self._pool.max_calls - self._pool._used - self._reserve)

    @property
    def used(self) -> int:
        with self._pool._lock:
            return self._pool._used

    @property
    def remaining_time(self) -> float | None:
        return None if self.deadline is None else max(0.0, self.deadline - time.time())

    def _check_locked(self) -> None:
        if self.deadline is not None and time.time() >= self.deadline:
            raise BudgetExceeded("deadline")
        if self._pool.max_calls is not None and self._pool._used + self._reserve >= self._pool.max_calls:
            raise BudgetExceeded("calls")

    def check(self) -> None:
        with self._pool._lock:
            self._check_locked()

    def consume(self) -> None:
        with self._pool._lock:
            self._check_locked()
            self._pool._used += 1

    def child(self, reserve: int = 1) -> CallBudget:
        child = CallBudget(self.max_calls, self.deadline)
        child._pool = self._pool
        child._reserve = self._reserve + max(0, int(reserve))
        return child
