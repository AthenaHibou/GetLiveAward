import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtWidgets import QApplication

from core.client import BiliRewardClient
from core.constants import NAV_URL
from core.errors import BiliRateLimitError, BiliRewardCancelledError, BiliRewardError, BiliRewardTimeoutError
from core.models import AwardInfo, ClaimExecutionControl, RunSettings
from core.network import RequestLimiter, TaskRequestLimiter
from core.service import RewardService
from core.task_config import TaskDocument, SavedTask, load_document
from ui.main_window import MainWindow

AWARD = AwardInfo("a", "activity", "task", "award")
INELIGIBLE = {"code": 202032, "message": "无资格"}
SUCCESS = {"code": 0, "data": {}}


class AsyncDispatchTests(unittest.TestCase):
    def setUp(self):
        self.client = BiliRewardClient("bili_jct=fake", limiter=RequestLimiter(0, 2))
        self.addCleanup(self.client.close)

    def test_async_uses_dispatch_time_instead_of_response_time_for_next_tick(self):
        now, starts, logs = [0.0], [], []
        def response(*args, **kwargs):
            starts.append(now[0])
            now[0] += 0.03
            return INELIGIBLE if len(starts) == 1 else SUCCESS
        with (patch.object(self.client, "_signed_request", side_effect=response),
              patch("core.client.time.perf_counter", side_effect=lambda: now[0]),
              patch("core.client.time.monotonic", side_effect=lambda: now[0]),
              patch("core.client.time.sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay))):
            self.client.claim_award("task", award_info=AWARD, max_retry=2,
                                    retry_interval=0.04, async_dispatch=True, log_callback=logs.append)
        self.assertAlmostEqual(starts[1] - starts[0], 0.04)
        self.assertFalse(any("[重试-等待]" in line for line in logs))
        self.assertEqual(sum("[异步-派发]" in line for line in logs), 2)

    def test_async_overlaps_responses_without_sending_early(self):
        second_started = threading.Event()
        starts, finished = [], []
        lock = threading.Lock()
        def response(*args, **kwargs):
            with lock:
                index = len(starts)
                starts.append(time.perf_counter())
            if index == 0:
                if not second_started.wait(2):
                    raise AssertionError("second request waited for the first response")
            else:
                second_started.set()
            finished.append(time.perf_counter())
            return SUCCESS
        with patch.object(self.client, "_signed_request", side_effect=response):
            self.client.claim_award("task", award_info=AWARD, thread_count=2, max_retry=1,
                                    retry_interval=0.04, async_dispatch=True)
        self.assertEqual(len(starts), 2)
        self.assertGreaterEqual(starts[1] - starts[0], 0.039)
        self.assertLessEqual(starts[1], min(finished))

    def test_full_capacity_does_not_accumulate_missed_ticks(self):
        full, release = threading.Event(), threading.Event()
        starts, active, maximum = [], [0], [0]
        lock = threading.Lock()
        def response(*args, **kwargs):
            with lock:
                starts.append(time.perf_counter())
                number = len(starts)
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
                if number == 2:
                    full.set()
            try:
                if number <= 2 and not release.wait(2):
                    raise AssertionError("test did not release responses")
                return INELIGIBLE
            finally:
                with lock:
                    active[0] -= 1
        with (patch.object(self.client, "_signed_request", side_effect=response),
              ThreadPoolExecutor(max_workers=1) as executor):
            future = executor.submit(self.client.claim_award, "task", award_info=AWARD,
                                     thread_count=2, max_retry=2, retry_interval=0.04, async_dispatch=True)
            try:
                self.assertTrue(full.wait(2))
                time.sleep(0.12)
                self.assertEqual(len(starts), 2)
            finally:
                release.set()
            with self.assertRaises(BiliRewardError):
                future.result(2)
        self.assertEqual(maximum[0], 2)
        self.assertEqual(len(starts), 4)
        self.assertGreaterEqual(starts[3] - starts[2], 0.039)

    def test_risk_response_stops_future_ticks(self):
        def response(*args, **kwargs):
            time.sleep(0.02)
            self.client.limiter.block()
            raise BiliRateLimitError("code=-705")
        with patch.object(self.client, "_signed_request", side_effect=response) as request:
            with self.assertRaises(BiliRateLimitError):
                self.client.claim_award("task", award_info=AWARD, thread_count=2,
                                        max_retry=5, retry_interval=0.2, async_dispatch=True)
        self.assertEqual(request.call_count, 1)

    def test_async_still_obeys_global_inflight_limit_and_rechecks_stop(self):
        self.client.limiter = RequestLimiter(0, 1)
        started, release = threading.Event(), threading.Event()
        def response(*args, **kwargs):
            started.set()
            if not release.wait(2):
                raise AssertionError("test did not release response")
            return SUCCESS
        with (patch.object(self.client, "get_web_sign", return_value="test=1"),
              patch.object(self.client, "_perform_request", side_effect=response) as request,
              ThreadPoolExecutor(max_workers=1) as executor):
            future = executor.submit(self.client.claim_award, "task", award_info=AWARD,
                                     thread_count=2, max_retry=2, retry_interval=0.02, async_dispatch=True)
            try:
                self.assertTrue(started.wait(2))
                time.sleep(0.08)
                self.assertEqual(request.call_count, 1)
            finally:
                release.set()
            self.assertEqual(future.result(2), (AWARD, None))
        self.assertEqual(request.call_count, 1)




    def test_cancellation_and_duration_stop_pending_dispatch(self):
        for timed in (False, True):
            control = ClaimExecutionControl()
            def response(*args, **kwargs):
                if not timed:
                    control.cancel()
                return INELIGIBLE
            with patch.object(self.client, "_signed_request", side_effect=response) as request:
                with self.assertRaises(BiliRewardTimeoutError if timed else BiliRewardCancelledError):
                    self.client.claim_award("task", award_info=AWARD, claim_control=control,
                                            max_retry=None, duration_seconds=0.03 if timed else None,
                                            retry_interval=0.2, async_dispatch=True)
            self.assertEqual(request.call_count, 1)

    def test_successful_inflight_response_is_preserved_after_peer_risk(self):
        both_started = threading.Barrier(2)
        lock, calls = threading.Lock(), []
        def response(*args, **kwargs):
            with lock:
                index = len(calls)
                calls.append(index)
            both_started.wait(2)
            if index == 0:
                raise BiliRateLimitError("code=-705")
            return SUCCESS
        with patch.object(self.client, "_signed_request", side_effect=response):
            result = self.client.claim_award("task", award_info=AWARD, thread_count=2,
                                            max_retry=2, retry_interval=0.02, async_dispatch=True)
        self.assertEqual(result, (AWARD, None))
        self.assertEqual(len(calls), 2)

    def test_legacy_documents_default_to_response_wait_mode(self):
        payload = TaskDocument([SavedTask()]).payload()
        del payload["global"]["async_dispatch"]
        self.assertFalse(TaskDocument.parse(payload).global_settings.async_dispatch)
        settings = RunSettings(async_dispatch=True, request_workers=3)
        self.assertFalse(settings.concurrent)
        self.assertEqual(settings.active_requests, 2)
        self.assertEqual(settings.claim_options("task").thread_count, 3)
        self.assertTrue(settings.claim_options("task").async_dispatch)
