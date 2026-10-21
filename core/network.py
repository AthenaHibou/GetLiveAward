from __future__ import annotations

import threading
import time
import itertools
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable

from .errors import BiliRateLimitError


@dataclass(frozen=True)
class RequestWait:
    slot_seconds: float
    interval_seconds: float


class RequestLimiter:
    """Space API request starts across all threads in a batch."""

    def __init__(
        self, interval: float = 1.0, max_inflight: int = 1, *, max_tasks: int | None = None,
    ) -> None:
        self.interval = interval
        self.max_inflight = max_inflight
        self._lock = threading.Lock()
        self._next_start = 0.0
        self._last_start: float | None = None
        self._blocked = False
        self._slots = threading.BoundedSemaphore(max_inflight)
        self._condition = threading.Condition(self._lock)
        self._max_tasks = max_tasks
        self._active_tasks: dict[object, int] = {}
        self._pending: list[tuple[int, int, object]] = []
        self._checking: set[tuple[int, int, object]] = set()
        self._sequence = itertools.count()

    @contextmanager
    def task_slot(self, task: object, priority: int, check: Callable[[], None]):
        """Admit ready requests by row priority without reserving future ticks."""
        queued = time.perf_counter()
        ticket = (priority, next(self._sequence), task)
        admitted = False
        interval_wait = 0.0
        with self._condition:
            self._pending.append(ticket)
        try:
            while True:
                # Call outside the condition: cancellation may inspect this limiter.
                # A pause can block this callback; that ticket must stop competing
                # for priority while other tasks continue to request capacity.
                with self._condition:
                    self._checking.add(ticket)
                check()
                with self._condition:
                    self._checking.discard(ticket)
                    if self._blocked:
                        raise BiliRateLimitError("[限流] 请求已停止")
                    eligible = [item for item in self._pending if item not in self._checking and (
                        item[2] in self._active_tasks
                        or self._max_tasks is None
                        or len(self._active_tasks) < self._max_tasks
                    )]
                    # Serial tasks use row priority; concurrent tasks share
                    # capacity in arrival order so one row cannot monopolize it.
                    key = (lambda item: item[:2]) if self._max_tasks == 1 else (lambda item: (item[1],))
                    first = bool(eligible) and min(eligible, key=key) == ticket
                    remaining = self._next_start - time.monotonic()
                    if first and remaining <= 0 and self._slots.acquire(blocking=False):
                        self._pending.remove(ticket)
                        self._active_tasks[task] = self._active_tasks.get(task, 0) + 1
                        self._last_start = time.monotonic()
                        self._next_start = self._last_start + self.interval
                        admitted = True
                        break
                    before = time.perf_counter()
                    self._condition.wait(min(0.05, remaining) if first and remaining > 0 else 0.05)
                    if first and remaining > 0:
                        interval_wait += time.perf_counter() - before
            elapsed = time.perf_counter() - queued
            yield RequestWait(max(0.0, elapsed - interval_wait), interval_wait)
        finally:
            with self._condition:
                self._checking.discard(ticket)
                if admitted:
                    self._active_tasks[task] -= 1
                    if not self._active_tasks[task]:
                        del self._active_tasks[task]
                    self._slots.release()
                else:
                    self._pending.remove(ticket)
                self._condition.notify_all()

    @contextmanager
    def request_slot(self, check: Callable[[], None] = lambda: None, interval: float = 0):
        queued = time.perf_counter()
        while not self._slots.acquire(timeout=0.05):
            check()
        acquired = time.perf_counter()
        try:
            self.wait(check, interval)
            check()
            yield RequestWait(acquired - queued, time.perf_counter() - acquired)
        finally:
            self._slots.release()

    def block(self) -> None:
        with self._lock:
            self._blocked = True

    def check_blocked(self) -> None:
        with self._lock:
            if self._blocked:
                raise BiliRateLimitError("[限流] 本批次已停止请求，请稍后手动重试")

    def wait(self, check: Callable[[], None] = lambda: None, interval: float = 0) -> None:
        while True:
            check()
            with self._lock:
                if self._blocked:
                    raise BiliRateLimitError("[限流] 本批次已停止请求，请稍后手动重试")
                earliest = self._next_start
                if self._last_start is not None:
                    earliest = max(earliest, self._last_start + interval)
                remaining = earliest - time.monotonic()
                if remaining <= 0:
                    self._last_start = time.monotonic()
                    self._next_start = self._last_start + max(self.interval, interval)
                    return
            time.sleep(min(remaining, 0.05))


class TaskRequestLimiter:
    """Keep task failure local while sharing account pacing and HTTP slots."""

    def __init__(
        self, shared: RequestLimiter, *, task_key: object | None = None, priority: int = 0,
    ) -> None:
        self.shared = shared
        self.task_key = task_key if task_key is not None else object()
        self.priority = priority
        self._blocked = threading.Event()
        self._dispatch_lock = threading.Lock()
        self._next_start = 0.0

    def block(self) -> None:
        self._blocked.set()

    def check_blocked(self) -> None:
        if self._blocked.is_set():
            raise BiliRateLimitError("[限流] 当前任务已停止请求，其他任务继续运行")
        self.shared.check_blocked()

    @contextmanager
    def request_slot(self, check: Callable[[], None] = lambda: None, interval: float = 0):
        def check_task():
            check()
            self.check_blocked()

        check_task()
        queued = time.perf_counter()
        while not self._dispatch_lock.acquire(timeout=0.05):
            check_task()
        locked = True
        acquired = time.perf_counter()
        try:
            while True:
                check_task()
                remaining = self._next_start - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 0.05))
            local_wait = time.perf_counter() - acquired
            # A task's interval must not become the next task's interval, or
            # occupy an account HTTP slot while waiting for its own cadence.
            with self.shared.task_slot(self.task_key, self.priority, check_task) as wait:
                self._next_start = time.monotonic() + interval
                self._dispatch_lock.release()
                locked = False
                yield RequestWait(
                    acquired - queued + wait.slot_seconds,
                    local_wait + wait.interval_seconds,
                )
        finally:
            if locked:
                self._dispatch_lock.release()
