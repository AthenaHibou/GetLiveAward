from __future__ import annotations

import threading
import time
from datetime import datetime
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

from core.errors import BiliRewardCancelledError, BiliRewardError, LoginCancelledError
from core.logging_utils import format_log_line
from core.models import (
    BatchClaimResult,
    ClaimExecutionControl,
    LinkTaskItem,
    PreparedLinkTask,
    SingleLinkClaimResult,
    RunSettings,
)
from core.service import RewardService
from core.clock_sync import BEIJING, next_scheduled_time
from core.task_config import with_globals


class ClaimWorker(QObject):
    finished = pyqtSignal()
    log_message = pyqtSignal(str)
    status_changed = pyqtSignal(str)
    qr_code_ready = pyqtSignal(int, object)
    link_status_changed = pyqtSignal(int, str)
    link_claim_stage_changed = pyqtSignal(int, bool)
    batch_completed = pyqtSignal(object)
    clock_status_changed = pyqtSignal(str)

    def __init__(
        self,
        service: RewardService,
        link_tasks: list[LinkTaskItem],
        settings: RunSettings | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.link_tasks = link_tasks
        self.settings = settings or RunSettings()
        self._login_cancel_event = threading.Event()
        self._cancel_event = threading.Event()
        self._claim_controls = {
            item.index: ClaimExecutionControl() for item in link_tasks
        }
        self._prepared_loggers: dict[int, Any] = {}
        self._batch_logger = None
        self._clock_sync_lock = threading.Lock()

    def _check_wait_cancelled(self) -> None:
        if self._is_login_cancelled() or (
            self._claim_controls
            and all(control.is_cancelled() for control in self._claim_controls.values())
        ):
            raise BiliRewardCancelledError("[定时] 已取消等待")

    def _sync_clock(self, timeout: float = 2.0) -> None:
        while not self._clock_sync_lock.acquire(timeout=0.05):
            self._check_wait_cancelled()
        try:
            self._check_wait_cancelled()
            # Tasks share a clock sample; simultaneous waiters need one refresh.
            if self.service.clock.age() < 1.0:
                return
            sample = self.service.clock.sync(self.settings.time_server, timeout)
            message = f"已校时，偏差 {sample.offset * 1000:+.1f} ms，往返 {sample.round_trip * 1000:.1f} ms"
            self.clock_status_changed.emit(message)
            self._emit_batch_log(f"[校时] {self.settings.time_server}: {message}")
        finally:
            self._clock_sync_lock.release()

    def _wait_scheduled(
        self, target: float, prepared: dict[int, PreparedLinkTask],
        task_settings: RunSettings | None = None,
    ) -> None:
        timing = task_settings or self.settings

        def log(message):
            if prepared:
                for index in prepared:
                    self._emit_link_log(index, message)
            else:
                self._emit_batch_log(message)

        formatted = datetime.fromtimestamp(target, BEIJING).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:-3]
        log(
            f"[定时] 首发目标 {formatted}（北京时间）；后续执行方式 {timing.effective_retry_mode}"
        )
        self.status_changed.emit("定时等待中")
        for index in prepared:
            self.link_status_changed.emit(index, "等待定时")
        last_sync = time.monotonic() - self.service.clock.age()
        prepared_at = time.monotonic()
        warmed = False
        last_remaining = None
        lead = max(
            20.0, timing.timeout * 2 + timing.interval * len(prepared)
        )
        while True:
            self._check_wait_cancelled()
            if prepared and all(self._claim_controls[index].is_cancelled() for index in prepared):
                raise BiliRewardCancelledError("[定时] 已取消该任务等待")
            remaining = target - self.service.clock.now()
            if self.service.clock.age() > max(
                120, self.settings.sync_interval_seconds * 2 + 2
            ):
                if remaining > 2.5:
                    try:
                        self._sync_clock()
                        last_sync = time.monotonic()
                        continue
                    except BiliRewardError as exc:
                        log(f"[校时] 过期样本刷新失败: {exc}")
                raise BiliRewardError("[校时] 样本过期，停止定时任务，请检查时间服务器")
            if remaining <= 0:
                log(
                    f"[定时] 等待结束，唤醒偏差 {-remaining * 1000:+.3f} ms"
                    "（不含请求准备、限流等待与网络耗时）"
                )
                if remaining < -0.1:
                    log(
                        f"[定时] 目标已过 {-remaining:.3f}s，立即开始（准备、网络或系统调度可能造成延迟）"
                    )
                return
            if not warmed and remaining <= lead:
                # Refresh login and WBI before the final window, never at first submit.
                if time.monotonic() - prepared_at > 5:
                    log("[定时] 临近目标，正在预热登录和签名密钥")
                    for index, task in prepared.items():
                        if self._claim_controls[index].is_cancelled():
                            continue
                        try:
                            self.service.refresh_prepared_task(
                                task,
                                qr_callback=lambda qr, idx=index: (
                                    self.qr_code_ready.emit(idx, qr)
                                ),
                                cancel_check=lambda idx=index: (
                                    self._is_login_cancelled()
                                    or self._claim_controls[idx].is_cancelled()
                                ),
                            )
                        except BiliRewardCancelledError:
                            if (
                                not self._is_login_cancelled()
                                and self._claim_controls[index].is_cancelled()
                            ):
                                continue
                            raise
                warmed = True
                continue
            if (
                time.monotonic() - last_sync >= self.settings.sync_interval_seconds
                and remaining > 2.5
            ):
                try:
                    self._sync_clock(timeout=min(2.0, remaining - 0.5))
                except BiliRewardError as exc:
                    log(f"[校时] 本次失败，暂用上次样本: {exc}")
                last_sync = time.monotonic()
                continue
            seconds = int(remaining)
            if seconds != last_remaining and (remaining <= 10 or seconds % 30 == 0):
                log(f"[定时] 距首发约 {remaining:.1f}s")
                last_remaining = seconds
            if remaining > 60:
                self._cancel_event.wait(min(0.25, remaining))
            elif remaining > 10:
                self._cancel_event.wait(min(0.05, remaining))
            elif remaining > 1:
                self._cancel_event.wait(min(0.01, remaining))
            else:
                # Event.wait uses a millisecond-based lock timeout on Windows.
                # Python 3.11+ sleep uses a waitable timer there, so reserve it
                # for the final second. Recheck cancellation on every tick and
                # never spin or change the system-wide timer resolution.
                time.sleep(min(0.001, remaining))

    def cancel_login(self) -> None:
        self._login_cancel_event.set()

    def cancel_all(self) -> None:
        self._cancel_event.set()
        self.cancel_login()
        for control in self._claim_controls.values():
            control.cancel()

    def pause_claim(self, link_index: int) -> bool:
        control = self._claim_controls.get(link_index)
        if control is None:
            return False
        changed = control.pause()
        if changed:
            self._log_for_link(link_index, "[任务] 已暂停抢奖")
            self.link_status_changed.emit(link_index, "已暂停")
        return changed

    def resume_claim(self, link_index: int) -> bool:
        control = self._claim_controls.get(link_index)
        if control is None:
            return False
        changed = control.resume()
        if changed:
            self._log_for_link(link_index, "[任务] 已继续抢奖")
            self.link_status_changed.emit(link_index, "抢奖中")
        return changed

    def cancel_claim(self, link_index: int) -> bool:
        control = self._claim_controls.get(link_index)
        if control is None:
            return False
        changed = control.cancel()
        if changed:
            self.link_status_changed.emit(link_index, "已取消")
        return changed

    def _is_login_cancelled(self) -> bool:
        return self._login_cancel_event.is_set() or self._cancel_event.is_set()

    def _emit_batch_log(self, message: str) -> None:
        if self._batch_logger is not None:
            self._batch_logger.log(f"[批次] {message}")
        else:
            self.log_message.emit(format_log_line(f"[批次] {message}"))

    def _emit_link_log(self, link_index: int, message: str) -> None:
        logger = self._prepared_loggers.get(link_index)
        if logger is not None:
            logger.log(message)
        elif self._batch_logger is not None:
            self._batch_logger.log(f"[链接{link_index + 1}] {message}")
        else:
            self.log_message.emit(format_log_line(f"[链接{link_index + 1}] {message}"))

    def _log_for_link(self, link_index: int, message: str) -> None:
        logger = self._prepared_loggers.get(link_index)
        if logger is not None:
            logger.log(message)
        else:
            self._emit_link_log(link_index, message)

    def _prepare_link_task(self, link_task: LinkTaskItem) -> PreparedLinkTask:
        self.link_status_changed.emit(link_task.index, "预处理中")
        self.link_claim_stage_changed.emit(link_task.index, False)
        self._emit_link_log(
            link_task.index, f"[任务] 开始处理第 {link_task.index + 1} 条链接"
        )
        prepared = self.service.prepare_link_task(
            link_task,
            options=with_globals(link_task.settings or self.settings, self.settings).claim_options(link_task.task_id),
            log_callback=self.log_message.emit,
            qr_callback=lambda qr_data, idx=link_task.index: self.qr_code_ready.emit(
                idx, qr_data
            ),
            cancel_check=lambda: (
                self._is_login_cancelled()
                or self._claim_controls[link_task.index].is_cancelled()
            ),
            log_scope=f"[链接{link_task.index + 1}]",
        )
        self._prepared_loggers[link_task.index] = prepared.logger
        self.link_status_changed.emit(link_task.index, "待抢奖")
        return prepared

    def _claim_prepared_task(
        self, prepared_task: PreparedLinkTask
    ) -> SingleLinkClaimResult:
        link_index = prepared_task.index
        try:
            self.link_status_changed.emit(link_index, "抢奖中")
            result = self.service.claim_prepared_task(
                prepared_task,
                claim_control=self._claim_controls[link_index],
                claim_stage_callback=lambda active, idx=link_index: (
                    self.link_claim_stage_changed.emit(idx, active)
                ),
                qr_callback=lambda qr: self.qr_code_ready.emit(link_index, qr),
                cancel_check=self._is_login_cancelled,
            )
            self.link_status_changed.emit(link_index, "成功")
            return SingleLinkClaimResult(
                index=link_index,
                task_id=prepared_task.task_id,
                status="success",
                cdkey=result.cdkey,
            )
        except (BiliRewardCancelledError, LoginCancelledError) as exc:
            self.link_status_changed.emit(link_index, "已取消")
            return SingleLinkClaimResult(
                index=link_index,
                task_id=prepared_task.task_id,
                status="cancelled",
                error=str(exc),
            )
        except BiliRewardError as exc:
            prepared_task.logger.log(f"[任务] 执行失败: {exc}")
            self.link_status_changed.emit(link_index, "失败")
            return SingleLinkClaimResult(
                index=link_index,
                task_id=prepared_task.task_id,
                status="failed",
                error=str(exc),
            )
        except Exception as exc:  # pragma: no cover
            prepared_task.logger.log(f"[任务] 执行失败: {type(exc).__name__}: {exc}")
            self.link_status_changed.emit(link_index, "失败")
            return SingleLinkClaimResult(
                index=link_index,
                task_id=prepared_task.task_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            self.link_claim_stage_changed.emit(link_index, False)

    def _resolve_batch_status(self, results: list[SingleLinkClaimResult]) -> str:
        if any(result.status == "success" for result in results):
            return "成功"
        if any(result.status == "failed" for result in results):
            return "失败"
        if results and all(result.status == "cancelled" for result in results):
            return "已取消"
        return "失败"

    def _run_task_queue(self) -> list[SingleLinkClaimResult]:
        """One execution per task; only authentication and HTTP pacing are shared."""
        settings = {task.index: with_globals(task.settings or self.settings, self.settings)
                    for task in self.link_tasks}
        sync_error = None
        if any(item.sync_required for item in settings.values()):
            try:
                self._sync_clock()
            except BiliRewardError as exc:
                sync_error = exc
                self._emit_batch_log(f"[校时] 失败，仅影响需要校时的任务: {exc}")
        now = self.service.clock.now()
        targets = {task.index: next_scheduled_time(now, settings[task.index].scheduled_time,
                                                  settings[task.index].start_delay_ms)
                   for task in self.link_tasks if settings[task.index].retry_mode == "scheduled"}
        tasks = sorted(self.link_tasks, key=lambda task: targets.get(task.index, now))
        for task in tasks:
            self.link_status_changed.emit(task.index, "排队中")
            if task.index in targets:
                target_text = datetime.fromtimestamp(targets[task.index], BEIJING).strftime("%m-%d %H:%M:%S.%f")[:-3]
                self._emit_link_log(task.index, f"[定时] 本次目标 {target_text}（北京时间）")
        self._emit_batch_log("每条任务独立等待、执行和处理失败；共用全局请求并发上限及最小间隔，单条失败不停止其他任务。")

        def execute(task):
            try:
                self._check_wait_cancelled()
                self._claim_controls[task.index].check_cancelled()
                if sync_error is not None and settings[task.index].sync_required:
                    self._sync_clock()
                    if task.index in targets:
                        targets[task.index] = next_scheduled_time(
                            self.service.clock.now(), settings[task.index].scheduled_time,
                            settings[task.index].start_delay_ms,
                        )
                ready = self._prepare_link_task(task)
                target = targets.get(task.index)
                if target is not None:
                    self._wait_scheduled(target, {task.index: ready}, settings[task.index])
                self._claim_controls[task.index].check_cancelled()
                return self._claim_prepared_task(ready)
            except Exception as exc:
                cancelled = isinstance(exc, (BiliRewardCancelledError, LoginCancelledError))
                status = "cancelled" if cancelled else "failed"
                self._emit_link_log(task.index, f"[任务] {'取消' if cancelled else '失败'}: {exc}")
                self.link_status_changed.emit(task.index, "已取消" if cancelled else "失败")
                return SingleLinkClaimResult(task.index, task.task_id, status, error=str(exc))

        with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            futures = [executor.submit(execute, task) for task in tasks]
            return [future.result() for future in as_completed(futures)]

    @pyqtSlot()
    def run(self) -> None:
        self.status_changed.emit("运行中")
        results: list[SingleLinkClaimResult] = []
        try:
            self._batch_logger = self.service.create_logger(self.log_message.emit)
            self._emit_batch_log(
                f"共 {len(self.link_tasks)} 条链接，开始处理；启用请求并发={self.settings.concurrent}, "
                f"异步派发={self.settings.async_dispatch}, "
                f"多任务异步派发={self.settings.parallel_links}, "
                f"每任务领取线程={self.settings.task_request_workers}, "
                f"请求并发上限={self.settings.active_requests}, 同时在途任务上限={self.settings.active_links}, "
                f"独立任务数={len(self.link_tasks)}, "
                f"全局最小间隔={self.settings.global_interval_ms} ms"
            )
            self.service.configure(replace(self.settings, interval_ms=self.settings.global_interval_ms))
            if self.link_tasks:
                results.extend(self._run_task_queue())
            else:
                self._emit_batch_log("没有可执行的链接")
        except Exception as exc:
            cancelled = isinstance(exc, (BiliRewardCancelledError, LoginCancelledError))
            self._emit_batch_log(f"执行中止: {exc}")
            completed = {result.index for result in results}
            for task in self.link_tasks:
                if task.index not in completed:
                    results.append(
                        SingleLinkClaimResult(
                            task.index,
                            task.task_id,
                            "cancelled" if cancelled else "failed",
                            error=str(exc),
                        )
                    )
                    self.link_status_changed.emit(
                        task.index, "已取消" if cancelled else "失败"
                    )
        finally:
            sorted_results = sorted(results, key=lambda item: item.index)
            self._emit_batch_log("全部链接处理完成")
            try:
                self.service.close()
            except Exception as exc:
                self.log_message.emit(format_log_line(f"[批次] 资源清理异常: {type(exc).__name__}"))
            self._batch_logger = None
            self.batch_completed.emit(BatchClaimResult(results=sorted_results))
            self.status_changed.emit(self._resolve_batch_status(sorted_results))
            self.finished.emit()


class LoginWorker(ClaimWorker):
    login_checked = pyqtSignal(bool, str)

    def __init__(
        self, service: RewardService, settings: RunSettings | None = None, *, add_account: bool = False
    ) -> None:
        super().__init__(service, [], settings)
        self.add_account = add_account
        self.new_account_login: tuple[dict, dict] | None = None

    @pyqtSlot()
    def run(self) -> None:
        self.status_changed.emit("检查登录中")
        try:
            # Authentication must work even when the scheduled task's NTP server is down.
            self.service.configure(replace(self.settings, interval_ms=self.settings.global_interval_ms,
                                           retry_mode="count", time_sync_enabled=False))
            login = self.service.scan_new_account if self.add_account else self.service.ensure_login
            result = login(
                log_callback=self.log_message.emit,
                qr_callback=lambda qr: self.qr_code_ready.emit(-1, qr),
                cancel_check=self._is_login_cancelled,
                timeout=self.settings.timeout,
                ignore_risk_control=self.settings.ignore_risk_control,
            )
            self._check_wait_cancelled()
            if self.add_account:
                self.new_account_login = result
            self.login_checked.emit(True, "登录已验证")
            self.status_changed.emit("登录就绪")
        except (BiliRewardCancelledError, LoginCancelledError):
            self.login_checked.emit(False, "登录已取消")
            self.status_changed.emit("已取消")
        except Exception as exc:
            self.log_message.emit(format_log_line(f"[登录] 检查失败: {exc}"))
            self.login_checked.emit(False, "登录检查失败")
            self.status_changed.emit("失败")
        finally:
            try:
                self.service.close()
            except Exception as exc:
                self.log_message.emit(
                    format_log_line(f"[登录] 资源清理异常: {type(exc).__name__}")
                )
            finally:
                self.finished.emit()
