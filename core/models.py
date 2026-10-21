from __future__ import annotations

import threading
import math
import time
from typing import Any
from dataclasses import dataclass, field
from typing import Callable, Optional
from datetime import time as DayTime

from .errors import BiliRewardCancelledError, BiliRewardTimeoutError


@dataclass(slots=True)
class AwardInfo:
    act_id: str
    act_name: str
    task_name: str
    award_name: str


@dataclass(slots=True)
class ClaimOptions:
    task_id: str
    thread_count: int = 1
    max_retry: int | None = 60
    retry_interval: float = 1.0
    timeout: float = 10.0
    stop_sold_out: bool = True
    duration_seconds: float | None = None
    jitter_ms: int = 0
    async_dispatch: bool = False
    execution_key: object | None = field(default=None, repr=False, compare=False)
    priority: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.thread_count <= 4:
            raise ValueError("线程数必须在 1 到 4 之间")
        if (
            (self.max_retry is not None and self.max_retry < 1)
            or not math.isfinite(self.retry_interval)
            or self.retry_interval < 0
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
        ):
            raise ValueError("重试次数和超时必须为正数，重试间隔不能为负数")
        if self.duration_seconds is not None and (
            not math.isfinite(self.duration_seconds) or self.duration_seconds <= 0
        ):
            raise ValueError("执行时长必须是正数")
        if not 0 <= self.jitter_ms <= 60000:
            raise ValueError("抖动范围必须在 0 到 60000 ms 之间")


@dataclass(frozen=True, slots=True)
class RunSettings:
    global_interval_ms: int = 1000
    concurrent: bool = False
    request_workers: int = 2
    global_request_workers: int = 2
    parallel_links: bool = False
    link_workers: int = 2
    interval_ms: int = 1000
    retry_mode: str = "count"
    max_retry: int = 60
    duration_minutes: float = 5.0
    timeout: float = 10.0
    stop_sold_out: bool = True
    jitter_enabled: bool = False
    jitter_ms: int = 200
    time_sync_enabled: bool = False
    time_server: str = "ntp.aliyun.com"
    sync_interval_seconds: int = 60
    scheduled_time: str = "00:00:00"
    start_delay_ms: int = 20
    scheduled_retry_mode: str = "count"
    async_dispatch: bool = False

    def __post_init__(self) -> None:
        if not all(1 <= value <= 4 for value in (
            self.request_workers, self.link_workers, self.global_request_workers
        )):
            raise ValueError("并发数必须在 1 到 4 之间")
        if not 100 <= self.interval_ms <= 60000:
            raise ValueError("请求间隔必须在 100 到 60000 ms 之间")
        if not 100 <= self.global_interval_ms <= 60000:
            raise ValueError("全局最小间隔必须在 100 到 60000 ms 之间")
        if self.retry_mode not in ("count", "duration", "continuous", "scheduled"):
            raise ValueError("执行方式无效")
        if self.scheduled_retry_mode not in ("count", "duration", "continuous"):
            raise ValueError("定时后的执行方式无效")
        if not 0 <= self.jitter_ms <= 60000 or not 0 <= self.start_delay_ms <= 60000:
            raise ValueError("抖动和首发延迟必须在 0 到 60000 ms 之间")
        if not 5 <= self.sync_interval_seconds <= 3600:
            raise ValueError("校时间隔必须在 5 到 3600 秒之间")
        if (
            not self.time_server
            or len(self.time_server) > 253
            or any(
                ch
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"
                for ch in self.time_server
            )
        ):
            raise ValueError("时间服务器应为主机名或 IPv4 地址，不含协议和端口")
        if len(self.scheduled_time) != 8:
            raise ValueError("定时时间格式必须为 HH:mm:ss")
        DayTime.fromisoformat(self.scheduled_time)
        if (
            not math.isfinite(self.duration_minutes)
            or not 0.1 <= self.duration_minutes <= 1440
        ):
            raise ValueError("执行时长必须在 0.1 到 1440 分钟之间")
        if (
            not math.isfinite(self.timeout)
            or not 1 <= self.timeout <= 60
            or not 1 <= self.max_retry <= 100000
        ):
            raise ValueError("超时或重试次数超出范围")

    @property
    def active_requests(self) -> int:
        return self.global_request_workers if self.concurrent or self.parallel_links or self.async_dispatch else 1

    @property
    def task_request_workers(self) -> int:
        return self.request_workers if self.concurrent or self.async_dispatch else 1

    @property
    def active_links(self) -> int:
        return self.link_workers if self.parallel_links else 1

    @property
    def interval(self) -> float:
        return self.interval_ms / 1000

    @property
    def effective_retry_mode(self) -> str:
        return (
            self.scheduled_retry_mode
            if self.retry_mode == "scheduled"
            else self.retry_mode
        )

    @property
    def sync_required(self) -> bool:
        return self.time_sync_enabled or self.retry_mode == "scheduled"

    def claim_options(self, task_id: str) -> ClaimOptions:
        return ClaimOptions(
            task_id,
            self.task_request_workers,
            self.max_retry if self.effective_retry_mode == "count" else None,
            self.interval,
            self.timeout,
            self.stop_sold_out,
            self.duration_minutes * 60
            if self.effective_retry_mode == "duration"
            else None,
            self.jitter_ms if self.jitter_enabled else 0,
            self.async_dispatch,
        )


@dataclass(slots=True)
class ClaimResult:
    award_info: AwardInfo
    cdkey: str | None


@dataclass(slots=True)
class LinkTaskItem:
    index: int
    task_url: str
    task_id: str
    settings: RunSettings | None = None


@dataclass(slots=True)
class PreparedLinkTask:
    index: int
    task_url: str
    task_id: str
    options: "ClaimOptions"
    client: Any
    award_info: AwardInfo
    logger: Any


@dataclass(slots=True)
class SingleLinkClaimResult:
    index: int
    task_id: str
    status: str
    cdkey: str | None = None
    error: str | None = None


@dataclass(slots=True)
class BatchClaimResult:
    results: list[SingleLinkClaimResult]


@dataclass(slots=True)
class QrCodeData:
    login_url: str
    qrcode_key: str
    image_bytes: bytes


@dataclass(slots=True)
class RuntimePaths:
    data_dir: str
    logs_dir: str
    cookie_file: str
    log_file: str


class ClaimRetryBudget:
    """Keep each worker's attempt count across a cookie refresh."""

    def __init__(self, limit: int | None, workers: int = 1) -> None:
        self.limit = limit
        self.workers = workers
        self._counts: dict[int, int] = {}
        self._lock = threading.Lock()

    def reserve(self, worker_id: int) -> int | None:
        with self._lock:
            used = self._counts.get(worker_id, 0)
            if self.limit is not None and used >= self.limit:
                return None
            self._counts[worker_id] = used + 1
            return used + 1

    def has_remaining(self, worker_id: int | None = None) -> bool:
        with self._lock:
            workers = range(1, self.workers + 1) if worker_id is None else (worker_id,)
            return self.limit is None or any(self._counts.get(item, 0) < self.limit for item in workers)


class ClaimExecutionControl:
    def __init__(self) -> None:
        self._cancel_event = threading.Event()
        self._resume_event = threading.Event()
        self._resume_event.set()

    def pause(self) -> bool:
        if self._cancel_event.is_set() or not self._resume_event.is_set():
            return False
        self._resume_event.clear()
        return True

    def resume(self) -> bool:
        if self._cancel_event.is_set() or self._resume_event.is_set():
            return False
        self._resume_event.set()
        return True

    def cancel(self) -> bool:
        already_cancelled = self._cancel_event.is_set()
        self._cancel_event.set()
        self._resume_event.set()
        return not already_cancelled

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def is_paused(self) -> bool:
        return not self._resume_event.is_set() and not self._cancel_event.is_set()

    def wait_if_paused(
        self,
        poll_interval: float = 0.05,
        *,
        stop_event: threading.Event | None = None,
        deadline: float | None = None,
        check_callback: Callable[[], None] | None = None,
    ) -> None:
        while self.is_paused():
            if stop_event is not None and stop_event.is_set():
                return
            self.check_cancelled()
            if check_callback is not None:
                check_callback()
            if deadline is not None and time.monotonic() >= deadline:
                raise BiliRewardTimeoutError("[任务] 已达到执行时长，停止该链接")
            self._resume_event.wait(timeout=poll_interval)
        self.check_cancelled()

    def check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise BiliRewardCancelledError("[任务] 已取消抢奖")


LogCallback = Callable[[str], None]
QrCodeCallback = Callable[[QrCodeData], None]
CancelCheck = Callable[[], bool]

OptionalLogCallback = Optional[LogCallback]
OptionalQrCodeCallback = Optional[QrCodeCallback]
OptionalCancelCheck = Optional[CancelCheck]
