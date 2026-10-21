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

    def test_terminal_response_stops_queued_peer_before_delayed_response_log_finishes(self):
        responses = [
            ({"code": 0, "data": {"extra_info": {"cdkey_content": key}}}, key)
            for key in (None, "TEST-KEY")
        ] + [({"code": 75255, "message": "库存已经使用完"}, None)] + [
            ({"code": code, "message": "terminal failure"}, None) for code in (-400, -404, -405)
        ]
        for scoped in (False, True):
            for asynchronous in (False, True):
                for payload, key in responses:
                    with self.subTest(scoped=scoped, async_dispatch=asynchronous, response=payload):
                        shared = RequestLimiter(0, 1, max_tasks=1)
                        self.client.limiter = TaskRequestLimiter(shared) if scoped else shared
                        peer_entered, peer_processed = threading.Event(), threading.Event()
                        signed_calls, submissions, checks = [], [], []
                        first_context = []
                        lock = threading.Lock()
                        original_signed = self.client._signed_request

                        def signed(*args, **kwargs):
                            with lock:
                                signed_calls.append(True)
                                if len(signed_calls) == 2:
                                    peer_entered.set()
                            return original_signed(*args, **kwargs)

                        def perform(*args):
                            with lock:
                                submissions.append(True)
                                first = len(submissions) == 1
                            if first:
                                first_context.append(self.client._local.claim_context)
                                checks.append(peer_entered.wait(1))
                            else:
                                peer_processed.set()
                            return payload

                        def log(message):
                            if "[请求-未发送]" in message:
                                peer_processed.set()
                            if ("[请求-响应]" in message and first_context
                                    and first_context[0] in message):
                                # Keep the successful worker inside logging until
                                # its peer has either stopped or incorrectly sent.
                                checks.append(peer_processed.wait(1))

                        with (patch.object(self.client, "_signed_request", side_effect=signed),
                              patch.object(self.client, "get_web_sign", return_value="test=1"),
                              patch.object(self.client, "_perform_request", side_effect=perform)):
                            options = dict(award_info=AWARD, thread_count=2, max_retry=1,
                                           retry_interval=0.01, async_dispatch=asynchronous, log_callback=log)
                            if payload["code"] == 0:
                                result = self.client.claim_award("task", **options)
                            else:
                                with self.assertRaises(BiliRewardError):
                                    self.client.claim_award("task", **options)
                        self.assertEqual(checks, [True, True])
                        self.assertEqual(len(submissions), 1)
                        if payload["code"] == 0:
                            self.assertEqual(result, (AWARD, key))

    def test_success_stops_peer_admitted_but_still_inside_send_log(self):
        shared = RequestLimiter(0, 2, max_tasks=1)
        self.client.limiter = TaskRequestLimiter(shared)
        second_logging, response_logged = threading.Event(), threading.Event()
        sends, submissions, checks = [], [], []
        lock = threading.Lock()

        def log(message):
            if "[请求-发送]" in message:
                with lock:
                    sends.append(True)
                    second = len(sends) == 2
                if second:
                    second_logging.set()
                    checks.append(response_logged.wait(1))
            elif "[请求-响应]" in message:
                response_logged.set()

        def perform(*args):
            submissions.append(True)
            checks.append(second_logging.wait(1))
            return SUCCESS

        with (patch.object(self.client, "get_web_sign", return_value="test=1"),
              patch.object(self.client, "_perform_request", side_effect=perform)):
            result = self.client.claim_award(
                "task", award_info=AWARD, thread_count=2, max_retry=1,
                retry_interval=0.01, async_dispatch=True, log_callback=log,
            )
        self.assertEqual(checks, [True, True])
        self.assertEqual(len(submissions), 1)
        self.assertEqual(result, (AWARD, None))

    def test_nav_success_and_retryable_claim_responses_do_not_stop_claim(self):
        for failure, stop_stock in (
            ({"code": 75255, "message": "库存已经使用完"}, False),
            ({"code": 202032, "message": "库存不足，无资格"}, True),
            ({"code": -403, "message": "signature expired"}, True),
        ):
            with self.subTest(response=failure, stop_sold_out=stop_stock):
                self.client.limiter = TaskRequestLimiter(RequestLimiter(0))
                claims, navs = [], []
                def sign(*args):
                    self.client._request_json(NAV_URL)
                    return "test=1"
                def perform(url, *args):
                    if url == NAV_URL:
                        navs.append(True)
                        return SUCCESS
                    claims.append(True)
                    return failure if len(claims) == 1 else SUCCESS
                with (patch.object(self.client, "get_web_sign", side_effect=sign),
                      patch.object(self.client, "_perform_request", side_effect=perform)):
                    result = self.client.claim_award(
                        "task", award_info=AWARD, thread_count=1, max_retry=2,
                        retry_interval=0, stop_sold_out=stop_stock,
                    )
                self.assertEqual(result, (AWARD, None))
                self.assertEqual(len(claims), 2)
                self.assertEqual(len(navs), 2)
                self.assertIsNone(self.client._local.claim_stop)
                self.assertIsNone(self.client._local.claim_terminal_response)

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


class AsyncSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_checkbox_works_independently_and_persists_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            window = MainWindow(RewardService(root))
            try:
                self.assertFalse(window.settings_panel.async_dispatch.isChecked())
                window.settings_panel.async_dispatch.setChecked(True)
                self.assertFalse(window.settings_panel.concurrent.isChecked())
                self.assertTrue(window.settings_panel.requests.isEnabled())
                window.settings_panel.parallel_links.setChecked(True)
                window.settings_panel.global_requests.setValue(3)
                self.assertEqual(window.settings_panel.snapshot().active_requests, 3)
                self.assertEqual(window.settings_panel.snapshot().claim_options("task").thread_count, 2)
                self.assertTrue(window._autosave_workspace())
                saved = load_document(window._workspace_path())
                self.assertTrue(saved.global_settings.async_dispatch)
                self.assertNotIn("async_dispatch", saved.payload()["tasks"][0]["settings"])
            finally:
                window.close()
            reopened = MainWindow(RewardService(root))
            try:
                self.assertTrue(reopened.settings_panel.async_dispatch.isChecked())
                self.assertTrue(reopened.settings_panel.parallel_links.isChecked())
                self.assertFalse(reopened.settings_panel.concurrent.isChecked())
                self.assertEqual(reopened.settings_panel.global_requests.value(), 3)
                reopened.settings_panel.apply(RunSettings())
                self.assertFalse(reopened.settings_panel.async_dispatch.isChecked())
            finally:
                reopened.close()
