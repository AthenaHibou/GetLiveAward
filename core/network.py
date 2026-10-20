from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Callable

from .errors import BiliRateLimitError


class RequestLimiter:
    """Space API request starts across all threads in a batch."""

    def __init__(self, interval: float = 1.0, max_inflight: int = 1) -> None:
        self.interval = interval
        self._lock = threading.Lock()
        self._next_start = 0.0
        self._last_start: float | None = None
        self._blocked = False
        self._slots = threading.BoundedSemaphore(max_inflight)

    @contextmanager
    def request_slot(self, check: Callable[[], None] = lambda: None, interval: float = 0):
        while not self._slots.acquire(timeout=0.05):
            check()
        try:
            self.wait(check, interval)
            check()
            yield
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
