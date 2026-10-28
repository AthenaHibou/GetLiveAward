from __future__ import annotations

import threading
import time
import os
from contextlib import contextmanager
from dataclasses import replace

from .auth import login_and_get_cookies
from .clock_sync import NetworkClock
from .accounts import AccountStore, DEFAULT_ACCOUNT
from .client import BiliRewardClient, format_cookies_text
from .errors import BiliCookieInvalidError, BiliRewardError, BiliRewardTimeoutError
from .logging_utils import EventLogger, ScopedLogger
from .models import (
    AwardInfo,
    ClaimExecutionControl,
    ClaimOptions,
    ClaimRetryBudget,
    ClaimResult,
    LinkTaskItem,
    OptionalCancelCheck,
    OptionalQrCodeCallback,
    PreparedLinkTask,
    RunSettings,
)
from .network import RequestLimiter, TaskRequestLimiter
from .storage import (
    ensure_runtime_dirs,
    get_runtime_paths,
    load_cookie_file,
    save_cookie_file,
)


class RewardService:
    def __init__(
        self, base_dir: str | None = None, *, account_id: str = DEFAULT_ACCOUNT
    ) -> None:
        self.root_paths = get_runtime_paths(base_dir)
        self.base_dir = os.path.dirname(self.root_paths.data_dir)
        self.account_id = account_id
        self.account_identity: dict = {}
        self.paths = get_runtime_paths(base_dir, account_id)
        ensure_runtime_dirs(self.paths)
        self._login_lock = threading.Lock()
        self._clients: dict[tuple[object, float, float], BiliRewardClient] = {}
        self._all_clients: list[BiliRewardClient] = []
        self._cookie_text: str | None = None
        self._limiter = RequestLimiter()
        self._loggers: list[EventLogger] = []
        self.clock = NetworkClock()

    def close(self) -> None:
        """Release batch resources after all workers have stopped."""
        for client in self._all_clients:
            client.close()
        self._all_clients.clear()
        self._clients.clear()
        self._cookie_text = None
        self._limiter = RequestLimiter()
        for logger in self._loggers:
            logger.close()
        self._loggers.clear()

    def configure(self, settings: RunSettings) -> None:
        """Apply an immutable settings snapshot before starting a batch."""
        if self._all_clients:
            raise RuntimeError("任务运行时不能修改请求设置")
        self._limiter = RequestLimiter(
            settings.interval, settings.active_requests, max_tasks=settings.active_links,
        )
        self.clock = NetworkClock()

    def create_logger(self, log_callback=None) -> EventLogger:
        logger = EventLogger(self.paths.log_file, log_callback=log_callback)
        self._loggers.append(logger)
        return logger

    def ensure_login(
        self,
        *,
        log_callback=None,
        qr_callback=None,
        cancel_check=None,
        timeout: float = 10.0,
        ignore_risk_control: bool = False,
    ) -> None:
        """Authenticate without querying a task or submitting a reward claim."""
        logger = self.create_logger(log_callback)
        self._client(
            ClaimOptions(task_id="", timeout=timeout, ignore_risk_control=ignore_risk_control),
            logger, qr_callback, cancel_check
        )
        logger.log("[Cookie] 登录状态验证通过，可以等待奖励开放后开始领取")

    def scan_new_account(self, *, log_callback=None, qr_callback=None, cancel_check=None,
                         timeout: float = 10.0, ignore_risk_control: bool = False) -> tuple[dict, dict]:
        """Verify a fresh QR login in memory, without touching the active profile."""
        logger = self.create_logger(log_callback)
        cookies = login_and_get_cookies(log_callback=logger.log, qr_callback=qr_callback, cancel_check=cancel_check)
        BiliRewardClient._check_cancel(cancel_check)
        client = BiliRewardClient(format_cookies_text(cookies), timeout=timeout,
                                  ignore_risk_control=ignore_risk_control)
        try:
            identity = client.ensure_logged_in(cancel_check=cancel_check, log_callback=logger.log)
            if not isinstance(identity, dict) or not AccountStore.normalize_uid(identity.get("mid")):
                raise BiliRewardError("登录验证未返回有效 UID，请重新扫码")
            BiliRewardClient._check_cancel(cancel_check)
            return cookies, identity
        finally:
            client.close()

    def _check_account_identity(self, identity) -> dict:
        identity = identity if isinstance(identity, dict) else {}
        try:
            expected = AccountStore(self.root_paths.data_dir).uid_for(self.account_id)
        except BiliRewardError:
            # Keep the legacy default login usable if the account index is damaged.
            cookies = load_cookie_file(self.paths.cookie_file) or {}
            expected = AccountStore.normalize_uid(cookies.get("DedeUserID"))
        expected = expected or AccountStore.normalize_uid(self.account_identity.get("mid"))
        if expected and AccountStore.normalize_uid(identity.get("mid")) != expected:
            raise BiliRewardError("扫码账号与当前档案不一致，请使用原账号重新扫码，或点击「添加账号」；原登录和配置未覆盖")
        return identity

    @contextmanager
    def _login_guard(self, cancel_check):
        while not self._login_lock.acquire(timeout=0.05):
            BiliRewardClient._check_cancel(cancel_check)
        try:
            BiliRewardClient._check_cancel(cancel_check)
            yield
        finally:
            self._login_lock.release()

    def _client(self, options, logger, qr_callback, cancel_check, *, stale=None):
        with self._login_guard(cancel_check):
            client_key = (options.execution_key or options.task_id, options.timeout, options.retry_interval)
            # A concurrent task may already have replaced this stale login generation.
            refresh = stale is not None and stale in self._clients.values()
            if refresh:
                self._clients.clear()
                self._cookie_text = None
            if client_key in self._clients:
                return self._clients[client_key]
            if not refresh and self._cookie_text is None:
                cookies = load_cookie_file(self.paths.cookie_file)
                self._cookie_text = format_cookies_text(cookies) if cookies else None
            if self._cookie_text is not None:
                client = self._new_client(options)
                try:
                    self.account_identity = self._check_account_identity(
                        client.ensure_logged_in(cancel_check=cancel_check, log_callback=logger.log))
                    logger.log("[Cookie] 登录状态验证通过")
                    self._clients[client_key] = client
                    return client
                except BiliCookieInvalidError:
                    self._cookie_text = None
                    self._clients.clear()
            logger.log("[Cookie] 正在扫码登录（同批次共享登录结果）")
            cookies = login_and_get_cookies(
                log_callback=logger.log,
                qr_callback=qr_callback,
                cancel_check=cancel_check,
            )
            BiliRewardClient._check_cancel(cancel_check)
            cookie_text = format_cookies_text(cookies)
            client = self._new_client(options, cookie_text=cookie_text)
            identity = self._check_account_identity(
                client.ensure_logged_in(cancel_check=cancel_check, log_callback=logger.log))
            BiliRewardClient._check_cancel(cancel_check)
            save_cookie_file(self.paths.cookie_file, cookies)
            self._cookie_text = cookie_text
            self.account_identity = identity
            self._clients[client_key] = client
            return client

    def _new_client(self, options, *, cookie_text=None):
        client = BiliRewardClient(
            cookie_text if cookie_text is not None else self._cookie_text or "",
            timeout=options.timeout,
            limiter=TaskRequestLimiter(
                self._limiter, task_key=options.execution_key, priority=options.priority,
            ),
            wall_time=self.clock.now,
            request_interval=options.retry_interval,
            ignore_risk_control=options.ignore_risk_control,
        )
        self._all_clients.append(client)
        return client

    def _get_client_and_award_info(
        self, options: ClaimOptions, *, logger, qr_callback=None, cancel_check=None
    ) -> tuple[BiliRewardClient, AwardInfo]:
        client = self._client(options, logger, qr_callback, cancel_check)
        try:
            return client, client.get_award_info(
                options.task_id, log_callback=logger.log, cancel_check=cancel_check
            )

        except BiliCookieInvalidError:
            client = self._client(
                options, logger, qr_callback, cancel_check, stale=client
            )
            return client, client.get_award_info(
                options.task_id, log_callback=logger.log, cancel_check=cancel_check
            )

    def refresh_prepared_task(
        self, task: PreparedLinkTask, *, qr_callback=None, cancel_check=None
    ) -> None:
        BiliRewardClient._check_cancel(cancel_check)
        try:
            self.account_identity = self._check_account_identity(
                task.client.ensure_logged_in(cancel_check=cancel_check, log_callback=task.logger.log))
        except BiliCookieInvalidError:
            task.client = self._client(
                task.options, task.logger, qr_callback, cancel_check, stale=task.client
            )
        task.award_info = task.client.get_award_info(
            task.task_id, log_callback=task.logger.log, cancel_check=cancel_check
        )

    def prepare_link_task(
        self,
        link_task: LinkTaskItem,
        *,
        options: ClaimOptions | None = None,
        log_callback=None,
        qr_callback: OptionalQrCodeCallback = None,
        cancel_check: OptionalCancelCheck = None,
        log_scope: str | None = None,
    ) -> PreparedLinkTask:
        base_logger = self.create_logger(log_callback)
        logger = ScopedLogger(base_logger, log_scope) if log_scope else base_logger
        logger.log(f"[任务] 开始处理 task_id={link_task.task_id}")
        options = options or ClaimOptions(task_id=link_task.task_id)
        if options.task_id != link_task.task_id:
            raise ValueError("领取配置与链接的 task_id 不一致")
        # Even duplicate links represent independent executions. Keep this key
        # across login refreshes, but never reuse another row's blocked client.
        options = replace(options, execution_key=object(), priority=link_task.index)
        logger.log(
            f"[运行参数] 领取线程={options.thread_count}, 全局在途上限={self._limiter.max_inflight}, "
            f"任务间隔={options.retry_interval * 1000:g} ms, 全局最小间隔={self._limiter.interval * 1000:g} ms, "
            f"每线程最多尝试={options.max_retry}, 时长限制={options.duration_seconds}秒, "
            f"派发模式={'异步派发' if options.async_dispatch else '响应后等待'}, "
            f"无视风控继续尝试={options.ignore_risk_control}"
        )
        client, award_info = self._get_client_and_award_info(
            options,
            logger=logger,
            qr_callback=qr_callback,
            cancel_check=cancel_check,
        )
        return PreparedLinkTask(
            link_task.index,
            link_task.task_url,
            link_task.task_id,
            options,
            client,
            award_info,
            logger,
        )

    def claim_prepared_task(
        self,
        prepared_task: PreparedLinkTask,
        *,
        claim_control: ClaimExecutionControl | None = None,
        claim_stage_callback=None,
        qr_callback: OptionalQrCodeCallback = None,
        cancel_check: OptionalCancelCheck = None,
    ) -> ClaimResult:
        duration = prepared_task.options.duration_seconds
        deadline = None if duration is None else time.monotonic() + duration
        retry_budget = ClaimRetryBudget(prepared_task.options.max_retry, prepared_task.options.thread_count)

        def cancelled():
            if deadline is not None and time.monotonic() >= deadline:
                raise BiliRewardTimeoutError("[任务] 已达到执行时长，停止该链接")
            return (claim_control is not None and claim_control.is_cancelled()) or (
                cancel_check is not None and cancel_check()
            )

        for attempt in range(2):
            BiliRewardClient._check_cancel(cancelled)
            if claim_stage_callback is not None:
                claim_stage_callback(True)
            try:
                award_info, cdkey = prepared_task.client.claim_award(
                    prepared_task.task_id,
                    award_info=prepared_task.award_info,
                    thread_count=prepared_task.options.thread_count,
                    max_retry=prepared_task.options.max_retry,
                    retry_interval=prepared_task.options.retry_interval,
                    log_callback=prepared_task.logger.log,
                    claim_control=claim_control,
                    stop_sold_out=prepared_task.options.stop_sold_out,
                    duration_seconds=None
                    if deadline is None
                    else max(0.001, deadline - time.monotonic()),
                    jitter_ms=prepared_task.options.jitter_ms,
                    async_dispatch=prepared_task.options.async_dispatch,
                    retry_budget=retry_budget,
                    cancel_check=cancelled,
                )
                if cdkey:
                    prepared_task.logger.log(f"[任务] 领取成功，兑换码={cdkey}")
                else:
                    prepared_task.logger.log(
                        "[任务] 领取成功（接口 code=0）；本次未返回兑换码，"
                        "奖励可能直接发放至账号，请到活动页或游戏内核对到账"
                    )
                return ClaimResult(award_info, cdkey)
            except BiliCookieInvalidError as exc:
                if not retry_budget.has_remaining():
                    raise BiliRewardError(f"领取超过最大重试次数；最后错误: {exc}") from exc
                if attempt:
                    raise BiliRewardError(
                        f"[Cookie] 重新登录后仍然失效: {exc}"
                    ) from exc
            finally:
                if claim_stage_callback is not None:
                    claim_stage_callback(False)
            prepared_task.logger.log("[Cookie] 领取时登录失效，正在重新登录")
            BiliRewardClient._check_cancel(cancelled)
            prepared_task.client = self._client(
                prepared_task.options,
                prepared_task.logger,
                qr_callback,
                cancelled,
                stale=prepared_task.client,
            )
            prepared_task.award_info = prepared_task.client.get_award_info(
                prepared_task.task_id,
                log_callback=prepared_task.logger.log,
                cancel_check=cancelled,
            )
        raise AssertionError("unreachable")

    def run_claim(
        self,
        options: ClaimOptions,
        *,
        log_callback=None,
        qr_callback=None,
        cancel_check=None,
        claim_control=None,
        claim_stage_callback=None,
    ) -> ClaimResult:
        def cancelled():
            return (claim_control is not None and claim_control.is_cancelled()) or (
                cancel_check is not None and cancel_check()
            )

        prepared = self.prepare_link_task(
            LinkTaskItem(0, "", options.task_id),
            options=options,
            log_callback=log_callback,
            qr_callback=qr_callback,
            cancel_check=cancelled,
        )
        return self.claim_prepared_task(
            prepared,
            claim_control=claim_control,
            claim_stage_callback=claim_stage_callback,
            qr_callback=qr_callback,
            cancel_check=cancelled,
        )
