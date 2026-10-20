from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from PyQt5.QtWidgets import QApplication

from core import auth
from core.clock_sync import NetworkClock, TimeSample
from core.client import BiliRewardClient
from core.constants import NAV_URL, MISSION_INFO_URL
from core.errors import (
    BiliCookieInvalidError,
    BiliRateLimitError,
    BiliRewardCancelledError,
    BiliRewardError,
    BiliRewardTimeoutError,
)
from core.models import (
    AwardInfo,
    ClaimExecutionControl,
    ClaimOptions,
    LinkTaskItem,
    PreparedLinkTask,
    RunSettings,
)
from core.network import RequestLimiter
from core.service import RewardService
from core.storage import load_cookie_file, save_cookie_file
from ui.main_window import MainWindow
from ui.worker import ClaimWorker

AWARD = AwardInfo("1", "activity", "task", "award")
SUCCESS = {"code": 0, "data": {"extra_info": {"cdkey_content": "TEST-KEY"}}}
NAV = {
    "code": 0,
    "data": {
        "isLogin": True,
        "wbi_img": {
            "img_url": "https://x/" + "a" * 32 + ".png",
            "sub_url": "https://x/" + "b" * 32 + ".png",
        },
    },
}


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = BiliRewardClient("bili_jct=fake", limiter=RequestLimiter(0))
        self.addCleanup(self.client.close)

    def test_parallel_signing_fetches_keys_once(self):
        self.client._request_json = Mock(return_value=NAV)
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: self.client.get_web_sign(), range(24)))
        self.assertEqual(self.client._request_json.call_count, 1)

    def test_login_nav_populates_keys_and_ttl_refreshes(self):
        self.client._request_json = Mock(return_value=NAV)
        self.assertEqual(self.client.ensure_logged_in(), NAV["data"])
        self.client.get_web_sign()
        self.assertEqual(self.client._request_json.call_count, 1)
        self.client._wbi_expires = 0
        self.client.get_web_sign()
        self.assertEqual(self.client._request_json.call_count, 2)

    def test_signature_rejection_refreshes_only_once(self):
        self.client._request_json = Mock(
            side_effect=[NAV, {"code": -403}, NAV, {"code": -403}]
        )
        self.assertEqual(
            self.client._signed_request("https://example.com/")["code"], -403
        )
        self.assertEqual(self.client._request_json.call_count, 4)

    def test_success_releases_paused_peer(self):
        control = ClaimExecutionControl()
        barrier = threading.Barrier(2)
        paused = threading.Event()
        lock = threading.Lock()
        calls = []

        def response(*args, **kwargs):
            with lock:
                index = len(calls)
                calls.append(index)
            barrier.wait(2)
            if index == 0:
                paused.wait(2)
                time.sleep(0.05)
                return SUCCESS
            control.pause()
            paused.set()
            return {"code": -702}

        self.client._signed_request = response
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self.client.claim_award,
                "task",
                award_info=AWARD,
                claim_control=control,
                thread_count=2,
                max_retry=2,
                retry_interval=0,
            )
            try:
                result = future.result(timeout=2)
            finally:
                control.cancel()
        self.assertEqual(result[1], "TEST-KEY")

    def test_success_wins_over_earlier_login_error(self):
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        calls = []

        def response(*args, **kwargs):
            with lock:
                index = len(calls)
                calls.append(index)
            barrier.wait(2)
            if index == 0:
                return {"code": -101, "message": "expired"}
            time.sleep(0.05)
            return SUCCESS

        self.client._signed_request = response
        self.assertEqual(
            self.client.claim_award(
                "task", award_info=AWARD, thread_count=2, max_retry=1
            )[1],
            "TEST-KEY",
        )

    def test_ineligible_claim_retries_until_eligibility_refreshes(self):
        self.client._signed_request = Mock(
            side_effect=[
                {"code": 202032, "message": "无资格领取奖励"},
                {"code": 202032, "message": "无资格领取奖励"},
                SUCCESS,
            ]
        )
        result = self.client.claim_award(
            "task", award_info=AWARD, thread_count=1, max_retry=3, retry_interval=0.01
        )
        self.assertEqual(result[1], "TEST-KEY")
        self.assertEqual(self.client._signed_request.call_count, 3)

    def test_ineligible_claim_still_respects_retry_budget(self):
        self.client._signed_request = Mock(
            return_value={"code": 202032, "message": "无资格领取奖励"}
        )
        with self.assertRaisesRegex(BiliRewardError, "202032"):
            self.client.claim_award(
                "task", award_info=AWARD, thread_count=1, max_retry=3, retry_interval=0
            )
        self.assertEqual(self.client._signed_request.call_count, 3)

    def test_optional_jitter_only_applies_between_attempts(self):
        starts = []
        responses = iter([{"code": 202032}, SUCCESS])

        def response(*args, **kwargs):
            starts.append(time.monotonic())
            return next(responses)

        self.client._signed_request = response
        with patch("core.client.random.uniform", return_value=0.03) as jitter:
            self.client.claim_award(
                "task", award_info=AWARD, max_retry=2, retry_interval=0.01, jitter_ms=50
            )
            jitter.assert_called_once_with(0, 0.05)
        self.assertGreaterEqual(starts[1] - starts[0], 0.035)
        self.client._signed_request = Mock(return_value=SUCCESS)
        with patch("core.client.random.uniform") as jitter:
            self.client.claim_award("task", award_info=AWARD, jitter_ms=200)
            jitter.assert_not_called()

    def test_wbi_sign_uses_corrected_clock(self):
        self.client._request_json = Mock(return_value=NAV)
        self.client._wall_time = lambda: 1700000000.75
        self.assertIn("wts=1700000000", self.client.get_web_sign())

    def test_continuous_mode_survives_eligibility_delay(self):
        self.client._signed_request = Mock(
            side_effect=[{"code": 202032}, {"code": 202032}, SUCCESS]
        )
        self.assertEqual(
            self.client.claim_award(
                "task", award_info=AWARD, max_retry=None, retry_interval=0
            )[1],
            "TEST-KEY",
        )
        self.assertEqual(self.client._signed_request.call_count, 3)

    def test_duration_limit_interrupts_retry_wait(self):
        self.client._signed_request = Mock(return_value={"code": 202032})
        started = time.monotonic()
        with self.assertRaises(BiliRewardTimeoutError):
            self.client.claim_award(
                "task",
                award_info=AWARD,
                max_retry=None,
                duration_seconds=0.06,
                retry_interval=30,
            )
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.client._signed_request.call_count, 1)

    def test_duration_limit_expires_while_paused(self):
        control = ClaimExecutionControl()

        def response(*a, **kw):
            control.pause()
            return {"code": 202032}

        self.client._signed_request = response
        with self.assertRaises(BiliRewardTimeoutError):
            self.client.claim_award(
                "task",
                award_info=AWARD,
                max_retry=None,
                duration_seconds=0.06,
                claim_control=control,
            )

    def test_stock_exhaustion_can_stop_or_be_retried(self):
        self.client._signed_request = Mock(
            return_value={"code": 123, "message": "奖品库存不足"}
        )
        with self.assertRaisesRegex(BiliRewardError, "库存"):
            self.client.claim_award("task", award_info=AWARD, max_retry=None)
        self.assertEqual(self.client._signed_request.call_count, 1)
        self.client._signed_request = Mock(
            side_effect=[{"code": 123, "message": "奖品库存不足"}, SUCCESS]
        )
        self.assertEqual(
            self.client.claim_award(
                "task",
                award_info=AWARD,
                max_retry=2,
                retry_interval=0,
                stop_sold_out=False,
            )[1],
            "TEST-KEY",
        )

    def test_ip_anomaly_text_blocks_batch_requests(self):
        response = Mock(status_code=200)
        response.json.return_value = {"code": 999, "message": "IP访问异常"}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        session = Mock()
        session.request.return_value = response
        with patch.object(self.client, "_session", return_value=session):
            for _ in range(2):
                with self.assertRaises(BiliRateLimitError):
                    self.client._request_json("https://example.com/")
        self.assertEqual(session.request.call_count, 1)

    def test_success_without_key_does_not_repeat_claim(self):
        self.client._signed_request = Mock(return_value={"code": 0, "data": {}})
        with self.assertRaisesRegex(BiliRewardError, "人工核对"):
            self.client.claim_award(
                "task", award_info=AWARD, thread_count=1, max_retry=10
            )
        self.assertEqual(self.client._signed_request.call_count, 1)

    def test_retry_budget_is_respected_per_worker(self):
        self.client._signed_request = Mock(return_value={"code": -702})
        with self.assertRaises(BiliRewardError):
            self.client.claim_award(
                "task", award_info=AWARD, thread_count=2, max_retry=3, retry_interval=0
            )
        self.assertEqual(self.client._signed_request.call_count, 6)

    def test_cancel_interrupts_long_retry_wait(self):
        control = ClaimExecutionControl()
        requested = threading.Event()
        self.client._signed_request = lambda *a, **k: (
            requested.set() or {"code": 202032}
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self.client.claim_award,
                "task",
                award_info=AWARD,
                thread_count=1,
                claim_control=control,
                retry_interval=30,
            )
            self.assertTrue(requested.wait(1))
            control.cancel()
            with self.assertRaises(BiliRewardCancelledError):
                future.result(timeout=1)

    def test_info_cancellation_stops_retry(self):
        control = ClaimExecutionControl()

        def response(*a, **k):
            control.cancel()
            return {"code": -702}

        self.client._signed_request = response
        with self.assertRaises(BiliRewardCancelledError):
            self.client.get_award_info("task", cancel_check=control.is_cancelled)

    def test_rate_response_blocks_following_requests(self):
        response = Mock(status_code=429)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        session = Mock()
        session.request.return_value = response
        with patch.object(self.client, "_session", return_value=session):
            for _ in range(2):
                with self.assertRaises(BiliRateLimitError):
                    self.client._request_json("https://example.com/")
        self.assertEqual(session.request.call_count, 1)

    def test_json_shape_error_is_reported(self):
        response = Mock(status_code=200)
        response.json.return_value = []
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(
            self.client,
            "_session",
            return_value=SimpleNamespace(request=lambda *a, **k: response),
        ):
            with self.assertRaisesRegex(BiliRewardError, "不是对象"):
                self.client._request_json("https://example.com/")


class TransportTests(unittest.TestCase):
    def test_global_request_limit_covers_inflight_responses(self):
        for cap in (1, 2):
            limiter = RequestLimiter(0, max_inflight=cap)
            clients = [BiliRewardClient("", limiter=limiter) for _ in range(2)]
            lock = threading.Lock()
            active = peak = 0

            def request(*a, **kw):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.03)
                with lock:
                    active -= 1
                return {"code": 0}

            try:
                with (
                    patch.object(clients[0], "_perform_request", side_effect=request),
                    patch.object(clients[1], "_perform_request", side_effect=request),
                    ThreadPoolExecutor(max_workers=4) as executor,
                ):
                    list(
                        executor.map(
                            lambda index: clients[index % 2]._request_json(
                                "https://example.com/"
                            ),
                            range(4),
                        )
                    )
                self.assertEqual(peak, cap)
            finally:
                for client in clients:
                    client.close()

    def test_http_connections_are_reused(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            connections = 0

            def setup(self):
                type(self).connections += 1
                super().setup()

            def do_GET(self):
                body = b'{"code":0}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = BiliRewardClient("", limiter=RequestLimiter(0))
        client._session().trust_env = False
        try:
            for _ in range(5):
                client._request_json(f"http://127.0.0.1:{server.server_port}/")
            self.assertEqual(Handler.connections, 1)
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_limiter_spaces_parallel_requests(self):
        limiter = RequestLimiter(0.04)
        timestamps = []

        def request():
            limiter.wait()
            timestamps.append(time.monotonic())

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: request(), range(4)))
        timestamps.sort()
        self.assertTrue(all(b - a >= 0.035 for a, b in zip(timestamps, timestamps[1:])))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = RewardService(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.service.close)

    def test_run_claim_preserves_options(self):
        options = ClaimOptions(
            "task", thread_count=3, max_retry=2, retry_interval=0.2, timeout=0.4
        )
        with (
            patch.object(
                self.service, "prepare_link_task", return_value=Mock()
            ) as prepare,
            patch.object(self.service, "claim_prepared_task", return_value="ok"),
        ):
            self.assertEqual(self.service.run_claim(options), "ok")
            self.assertIs(prepare.call_args.kwargs["options"], options)

    def test_settings_default_to_serial_and_original_interval(self):
        settings = RunSettings()
        self.assertEqual(
            (settings.active_requests, settings.active_links, settings.interval_ms),
            (1, 1, 1000),
        )
        options = settings.claim_options("task")
        self.assertEqual(
            (options.thread_count, options.max_retry, options.retry_interval),
            (1, 120, 1.0),
        )
        disabled = RunSettings(parallel_links=True, request_workers=4, link_workers=4)
        self.assertEqual((disabled.active_links, disabled.active_requests), (1, 1))

    def test_duration_settings_convert_minutes_and_ms(self):
        options = RunSettings(
            interval_ms=750, retry_mode="duration", duration_minutes=2.5
        ).claim_options("task")
        self.assertIsNone(options.max_retry)
        self.assertEqual(options.duration_seconds, 150)
        self.assertEqual(options.retry_interval, 0.75)

    def test_time_budget_does_not_restart_after_login_expiry(self):
        clock = [0.0]
        client = Mock()

        def claim(*a, **kw):
            clock[0] = 2.0
            raise BiliCookieInvalidError("expired")

        client.claim_award.side_effect = claim
        task = PreparedLinkTask(
            0,
            "",
            "task",
            ClaimOptions("task", max_retry=None, duration_seconds=1),
            client,
            AWARD,
            Mock(),
        )
        with (
            patch("core.service.time.monotonic", side_effect=lambda: clock[0]),
            patch.object(self.service, "_client") as refresh,
        ):
            with self.assertRaises(BiliRewardTimeoutError):
                self.service.claim_prepared_task(task)
            refresh.assert_not_called()

    def test_login_only_checks_nav_without_task_or_claim(self):
        save_cookie_file(
            self.service.paths.cookie_file, {"SESSDATA": "fake", "bili_jct": "fake"}
        )
        with patch.object(
            BiliRewardClient, "_request_json", return_value=NAV
        ) as request:
            self.service.ensure_login()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args, (NAV_URL,))

    def test_concurrent_expiry_shares_one_relogin(self):
        save_cookie_file(
            self.service.paths.cookie_file, {"SESSDATA": "old", "bili_jct": "fake"}
        )
        logger = self.service.create_logger()
        options = ClaimOptions("task", max_retry=1)
        barrier = threading.Barrier(2)

        def claim(client, *a, **kw):
            if "old" in client.cookie_string:
                barrier.wait(2)
                raise BiliCookieInvalidError("expired")
            return AWARD, "TEST-KEY"

        with (
            patch.object(BiliRewardClient, "ensure_logged_in"),
            patch.object(BiliRewardClient, "get_award_info", return_value=AWARD),
            patch.object(BiliRewardClient, "claim_award", claim),
            patch(
                "core.service.login_and_get_cookies",
                return_value={"SESSDATA": "new", "bili_jct": "fake"},
            ) as login,
        ):
            client = self.service._client(options, logger, None, None)
            tasks = [
                PreparedLinkTask(i, "", "task", options, client, AWARD, logger)
                for i in range(2)
            ]
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(self.service.claim_prepared_task, tasks))
            self.assertEqual(
                [result.cdkey for result in results], ["TEST-KEY", "TEST-KEY"]
            )
            self.assertEqual(login.call_count, 1)

    def test_cookie_wrong_shape_returns_none(self):
        path = Path(self.service.paths.cookie_file)
        for value in ([], None, 42, "bad"):
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertIsNone(load_cookie_file(str(path)))

    def test_atomic_cookie_save_leaves_no_temporary_file(self):
        save_cookie_file(
            self.service.paths.cookie_file, {"SESSDATA": "fake", "bili_jct": "fake"}
        )
        self.assertEqual(
            load_cookie_file(self.service.paths.cookie_file)["SESSDATA"], "fake"
        )
        self.assertEqual(
            sorted(p.name for p in Path(self.service.paths.data_dir).iterdir()),
            ["bilibili_cookie.json", "logs"],
        )

    def test_expired_qr_without_logger_fails_immediately(self):
        with patch.object(
            auth, "_get_json", return_value={"code": 0, "data": {"code": 86038}}
        ) as get:
            with self.assertRaisesRegex(BiliRewardError, "过期"):
                auth.poll_login("fake", Mock())
            self.assertEqual(get.call_count, 1)

    def test_login_transport_failure_closes_session(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError("fake")
        with patch.object(auth.requests, "Session", return_value=session):
            with self.assertRaises(BiliRewardError):
                auth.login_and_get_cookies()
        session.close.assert_called_once()

    def test_mock_login_creates_png_and_valid_cookies(self):
        session = Mock()
        session.cookies = [
            SimpleNamespace(name="SESSDATA", value="fake"),
            SimpleNamespace(name="bili_jct", value="fake"),
        ]
        qr_codes = []
        responses = [
            {
                "code": 0,
                "data": {"url": "https://example.com/login", "qrcode_key": "fake"},
            },
            {"code": 0, "data": {"code": 0}},
        ]
        with (
            patch.object(auth.requests, "Session", return_value=session),
            patch.object(auth, "_get_json", side_effect=responses),
        ):
            cookies = auth.login_and_get_cookies(qr_callback=qr_codes.append)
        self.assertEqual(cookies["SESSDATA"], "fake")
        self.assertTrue(qr_codes[0].image_bytes.startswith(b"\x89PNG"))
        session.close.assert_called_once()


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = RewardService(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.service.close)

    def test_batch_preparation_failure_is_isolated(self):
        worker = ClaimWorker(
            self.service, [LinkTaskItem(0, "", "a"), LinkTaskItem(1, "", "b")]
        )
        batches = []
        worker.batch_completed.connect(batches.append)
        calls = []

        def prepare(task, **kwargs):
            calls.append(task.index)
            raise requests.ConnectionError("fake")

        with patch.object(self.service, "prepare_link_task", side_effect=prepare):
            worker.run()
        self.assertEqual(sorted(calls), [0, 1])
        self.assertEqual(
            [result.status for result in batches[0].results], ["failed", "failed"]
        )

    def test_ready_link_claims_before_slow_preparation_finishes(self):
        worker = ClaimWorker(
            self.service, [LinkTaskItem(0, "", "fast"), LinkTaskItem(1, "", "slow")]
        )
        claimed = threading.Event()
        logger = SimpleNamespace(log=lambda *a: None)

        def prepare(task, **kwargs):
            if task.index == 1:
                self.assertTrue(claimed.wait(2))
            return PreparedLinkTask(
                task.index,
                "",
                task.task_id,
                ClaimOptions(task.task_id),
                Mock(),
                AWARD,
                logger,
            )

        def claim(*args, **kwargs):
            claimed.set()
            return SimpleNamespace(cdkey="TEST-KEY")

        with (
            patch.object(self.service, "prepare_link_task", side_effect=prepare),
            patch.object(self.service, "claim_prepared_task", side_effect=claim),
        ):
            worker.run()
        self.assertTrue(claimed.is_set())

    def test_close_waits_for_worker_and_retains_no_running_thread(self):
        window = MainWindow(self.service)
        window.link_rows[0].input.setText("https://www.bilibili.com/?task_id=fake")

        def prepare(*args, cancel_check=None, **kwargs):
            while not cancel_check():
                time.sleep(0.01)
            raise BiliRewardCancelledError("cancelled")

        with (
            patch.object(self.service, "prepare_link_task", side_effect=prepare),
            patch("ui.main_window.QMessageBox.information") as message,
        ):
            window.show()
            window.start_claim()
            window.close()
            self.assertTrue(window._closing)
            deadline = time.monotonic() + 3
            while window.worker_thread is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertIsNone(window.worker_thread)
            self.assertFalse(window.isVisible())
            message.assert_not_called()

    def test_ui_log_has_bounded_history(self):
        window = MainWindow(self.service)
        for _ in range(2100):
            window.append_log("retry")
        self.assertEqual(window.log_text.document().blockCount(), 2000)
        window.close()

    def test_verified_login_automatically_registers_default_account(self):
        window = MainWindow(self.service)
        self.service.account_identity = {"mid": 123, "uname": "测试用户"}
        original_path = self.service.paths.cookie_file
        with patch.object(self.service, "prepare_link_task") as claim:
            window._register_logged_in_account()
        self.assertEqual(window.account_combo.currentText(), "测试用户 · 123")
        self.assertEqual(self.service.paths.cookie_file, original_path)
        self.assertEqual(window.account_combo.count(), 1)
        claim.assert_not_called()
        window.close()
        reopened = MainWindow(RewardService(self.temp.name))
        self.assertEqual(reopened.account_combo.currentText(), "测试用户 · 123")
        reopened.close()

    def test_add_account_isolates_cookie_and_starts_login_only(self):
        save_cookie_file(
            self.service.paths.cookie_file,
            {"SESSDATA": "original", "bili_jct": "original"},
        )
        window = MainWindow(self.service)
        with (
            patch.object(window, "start_login") as login,
            patch.object(self.service, "prepare_link_task") as claim,
        ):
            window.add_account_named("账号二")
        login.assert_called_once()
        claim.assert_not_called()
        self.assertNotEqual(window.service.account_id, "default")
        self.assertEqual(window.account_combo.currentText(), "账号二（待登录）")
        self.assertEqual(window.login_status.text(), "账号待登录")
        self.assertIsNone(load_cookie_file(window.service.paths.cookie_file))
        self.assertEqual(
            load_cookie_file(self.service.paths.cookie_file)["SESSDATA"], "original"
        )
        window.close()

    def test_account_switch_restores_individual_settings_and_clears_ui_log(self):
        window = MainWindow(self.service)
        window.settings_panel.apply(RunSettings(interval_ms=750))
        window.append_ui_log("DEFAULT-ONLY-LOG")
        with patch.object(window, "start_login"):
            window.add_account_named("账号二")
        second_id = window.service.account_id
        self.assertNotIn("DEFAULT-ONLY-LOG", window.log_text.toPlainText())
        self.assertEqual(window.settings_panel.snapshot().interval_ms, 1000)
        window.settings_panel.apply(RunSettings(interval_ms=2000))
        window.account_combo.setCurrentIndex(window.account_combo.findData("default"))
        self.assertEqual(window.service.account_id, "default")
        self.assertEqual(window.settings_panel.snapshot().interval_ms, 750)
        window.account_combo.setCurrentIndex(window.account_combo.findData(second_id))
        self.assertEqual(window.settings_panel.snapshot().interval_ms, 2000)
        window.close()
        reopened = MainWindow(RewardService(self.temp.name))
        self.assertEqual(reopened.service.account_id, second_id)
        self.assertEqual(reopened.settings_panel.snapshot().interval_ms, 2000)
        reopened.close()

    def test_running_login_blocks_account_addition_and_switch(self):
        window = MainWindow(self.service)
        with patch.object(window, "start_login"):
            window.add_account_named("账号二")
        second_id = window.service.account_id
        window.account_combo.setCurrentIndex(window.account_combo.findData("default"))

        def login(*, cancel_check=None, **kwargs):
            while not cancel_check():
                time.sleep(0.01)
            raise BiliRewardCancelledError("cancelled")

        with patch.object(window.service, "ensure_login", side_effect=login):
            window.show()
            window.start_login()
            self.assertFalse(window.account_combo.isEnabled())
            self.assertFalse(window.add_account_btn.isEnabled())
            window.account_combo.setCurrentIndex(
                window.account_combo.findData(second_id)
            )
            self.assertEqual(window.service.account_id, "default")
            with self.assertRaises(ValueError):
                window.add_account_named("账号三")
            window.close()
            deadline = time.monotonic() + 2
            while window.worker_thread is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
        self.assertIsNone(window.worker_thread)

    def test_corrupt_account_index_keeps_default_account_usable(self):
        from pathlib import Path

        path = Path(self.service.paths.data_dir) / "accounts.json"
        path.write_text("[]", encoding="utf-8")
        window = MainWindow(self.service)
        self.assertFalse(window.account_combo.isEnabled())
        self.assertFalse(window.add_account_btn.isEnabled())
        self.assertTrue(window.login_btn.isEnabled())
        self.assertEqual(window.service.account_id, "default")
        window.close()
        self.assertEqual(path.read_text(encoding="utf-8"), "[]")

    def test_settings_persist_and_restore_defaults(self):
        window = MainWindow(self.service)
        desired = RunSettings(
            concurrent=True,
            parallel_links=True,
            request_workers=3,
            link_workers=2,
            interval_ms=750,
            retry_mode="duration",
            duration_minutes=2.5,
        )
        window.settings_panel.apply(desired)
        window.close()
        reopened = MainWindow(self.service)
        self.assertEqual(reopened.settings_panel.snapshot(), desired)
        reopened.settings_panel.apply(RunSettings())
        self.assertEqual(reopened.settings_panel.snapshot().active_requests, 1)
        self.assertFalse(reopened.settings_panel.requests.isEnabled())
        reopened.close()

    def test_schedule_settings_force_sync_and_persist(self):
        window = MainWindow(self.service)
        settings = RunSettings(
            retry_mode="scheduled",
            time_sync_enabled=True,
            jitter_enabled=True,
            jitter_ms=300,
            start_delay_ms=150,
            scheduled_time="00:00:00",
            scheduled_retry_mode="duration",
        )
        window.settings_panel.apply(settings)
        self.assertTrue(window.settings_panel.sync.isChecked())
        self.assertFalse(window.settings_panel.sync.isEnabled())
        window.close()
        reopened = MainWindow(self.service)
        self.assertEqual(reopened.settings_panel.snapshot(), settings)
        reopened.close()

    def test_invalid_ntp_setting_does_not_lock_ui_or_start_worker(self):
        window = MainWindow(self.service)
        window.settings_panel.server.setText("https://ntp.aliyun.com/")
        window.link_rows[0].input.setText("https://www.bilibili.com/?task_id=test")
        with patch("ui.main_window.QMessageBox.warning") as warning:
            window.start_claim()
            window.start_login()
            self.assertEqual(warning.call_count, 2)
        self.assertFalse(window._is_running)
        self.assertIsNone(window.worker_thread)
        self.assertTrue(window.start_btn.isEnabled())
        window.close()

    def test_scheduled_batch_prepares_then_waits_before_claiming(self):
        clock = NetworkClock()
        original_configure = self.service.configure

        def configure(settings):
            original_configure(settings)
            self.service.clock = clock

        target = time.time() + 0.12
        starts = []
        prepared_at = []

        def prepare(task, **kwargs):
            prepared_at.append(clock.now())
            return PreparedLinkTask(
                task.index, "", task.task_id, kwargs["options"], Mock(), AWARD, Mock()
            )

        def claim(*args, **kwargs):
            starts.append(clock.now())
            return SimpleNamespace(cdkey="TEST-KEY")

        worker = ClaimWorker(
            self.service,
            [LinkTaskItem(0, "", "task")],
            RunSettings(retry_mode="scheduled"),
        )
        sample = TimeSample(time.time(), time.monotonic(), 0, 0.01)
        with (
            patch.object(self.service, "configure", side_effect=configure),
            patch("core.clock_sync.query_ntp", return_value=sample),
            patch("ui.worker.next_scheduled_time", return_value=target),
            patch.object(self.service, "prepare_link_task", side_effect=prepare),
            patch.object(self.service, "claim_prepared_task", side_effect=claim),
        ):
            worker.run()
        self.assertLess(prepared_at[0], target)
        self.assertGreaterEqual(starts[0], target)
        self.assertEqual(len(starts), 1)

    def test_scheduled_sync_failure_never_prepares_or_claims(self):
        worker = ClaimWorker(
            self.service,
            [LinkTaskItem(0, "", "task")],
            RunSettings(retry_mode="scheduled"),
        )
        results = []
        worker.batch_completed.connect(results.append)
        with (
            patch(
                "core.clock_sync.query_ntp",
                side_effect=BiliRewardError("NTP unavailable"),
            ),
            patch.object(self.service, "prepare_link_task") as prepare,
            patch.object(self.service, "claim_prepared_task") as claim,
        ):
            worker.run()
        prepare.assert_not_called()
        claim.assert_not_called()
        self.assertEqual(results[0].results[0].status, "failed")

    def test_scheduled_wait_syncs_periodically_and_can_be_cancelled(self):
        worker = ClaimWorker(
            self.service,
            [LinkTaskItem(0, "", "task")],
            RunSettings(retry_mode="scheduled", sync_interval_seconds=5),
        )
        elapsed = [0.0]
        self.service.clock = SimpleNamespace(
            now=lambda: 1700000000 + elapsed[0], age=lambda: 0
        )

        def advance(*args):
            elapsed[0] += 5

        task = PreparedLinkTask(
            0, "", "task", ClaimOptions("task"), Mock(), AWARD, Mock()
        )
        with (
            patch("ui.worker.time.monotonic", side_effect=lambda: elapsed[0]),
            patch.object(worker, "_sync_clock") as sync,
            patch.object(worker._cancel_event, "wait", side_effect=advance),
        ):
            worker._wait_scheduled(1700000020, {0: task})
        self.assertGreaterEqual(sync.call_count, 2)
        worker.cancel_all()
        with self.assertRaises(BiliRewardCancelledError):
            worker._wait_scheduled(1700000030, {0: task})

    def test_long_login_preparation_resyncs_before_waiting(self):
        worker = ClaimWorker(
            self.service,
            [LinkTaskItem(0, "", "task")],
            RunSettings(retry_mode="scheduled"),
        )
        elapsed = [0.0]
        age = [180.0]
        self.service.clock = SimpleNamespace(
            now=lambda: 1700000000 + elapsed[0], age=lambda: age[0]
        )

        def sync(*args, **kwargs):
            age[0] = 0

        def advance(*args):
            elapsed[0] += 5

        with (
            patch("ui.worker.time.monotonic", side_effect=lambda: elapsed[0]),
            patch.object(worker, "_sync_clock", side_effect=sync) as synchronize,
            patch.object(worker._cancel_event, "wait", side_effect=advance),
        ):
            worker._wait_scheduled(1700000010, {})
        synchronize.assert_called_once()

    def test_default_batch_is_serial_and_cancelled_queue_is_skipped(self):
        tasks = [LinkTaskItem(i, "", str(i)) for i in range(3)]
        worker = ClaimWorker(self.service, tasks)
        worker.cancel_claim(1)
        calls = []
        batches = []
        worker.batch_completed.connect(batches.append)

        def prepare(task, **kwargs):
            calls.append(task.index)
            self.assertEqual(kwargs["options"].thread_count, 1)
            raise BiliRewardError("fake")

        with patch.object(self.service, "prepare_link_task", side_effect=prepare):
            worker.run()
        self.assertEqual(calls, [0, 2])
        self.assertEqual(
            [r.status for r in batches[0].results], ["failed", "cancelled", "failed"]
        )

    def test_tutorial_navigation_and_saved_opt_out(self):
        window = MainWindow(self.service)
        window.show()
        self.app.processEvents()
        with (
            patch.object(self.service, "ensure_login") as login,
            patch.object(self.service, "prepare_link_task") as claim,
        ):
            window.show_tutorial()
            self.assertTrue(window.guide.isVisible())
            self.assertEqual(window.guide.steps[0][0], window.link_rows[0].input)
            window.guide.next_step()
            self.assertEqual(
                window.guide.steps[window.guide.step][0], window.account_combo
            )
            window.guide.previous_step()
            self.assertEqual(window.guide.step, 0)
            window.guide.hide_next_time.setChecked(True)
            window.guide.finish()
            self.assertFalse(window.guide.isVisible())
            login.assert_not_called()
            claim.assert_not_called()
        window.close()
        reopened = MainWindow(self.service)
        reopened.show()
        self.app.processEvents()
        reopened._maybe_show_tutorial()
        self.assertIsNone(reopened.guide)
        reopened.help_btn.click()
        self.assertTrue(reopened.guide.isVisible())
        reopened.close()

    def test_login_button_works_without_link_and_restores_controls(self):
        window = MainWindow(self.service)
        window.show()
        with (
            patch.object(self.service, "ensure_login") as login,
            patch.object(self.service, "prepare_link_task") as claim,
        ):
            window.login_btn.click()
            self.assertFalse(window.start_btn.isEnabled())
            deadline = time.monotonic() + 2
            while window.worker_thread is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertIsNone(window.worker_thread)
            self.assertEqual(window.login_status.text(), "登录已验证")
            self.assertEqual(window.login_btn.text(), "检查登录")
            self.assertEqual(window.batch_status.text(), window.STATUS_IDLE)
            self.assertIn("账号已就绪", window.flow_hint.text())
            self.assertTrue(window.start_btn.isEnabled())
            login.assert_called_once()
            claim.assert_not_called()
        window.close()

    def test_login_ui_copy_tracks_current_account_verification(self):
        window = MainWindow(self.service)
        self.addCleanup(window.close)
        self.assertEqual(window.login_btn.text(), "登录当前账号")
        self.assertIn("登录当前账号后", window.product_hint.text())
        window._on_login_checked(True, "登录已验证")
        self.assertEqual(window.login_btn.text(), "检查登录")
        self.assertIn("等待奖励开放", window.product_hint.text())
        self.assertIn("账号已就绪", window.flow_hint.text())
        window._on_login_checked(False, "登录已取消")
        self.assertEqual(window.login_btn.text(), "登录当前账号")
        self.assertIn("登录当前账号", window.flow_hint.text())

    def test_tutorial_account_step_matches_verified_state(self):
        window = MainWindow(self.service)
        self.addCleanup(window.close)
        window._on_login_checked(True, "登录已验证")
        window.show_tutorial()
        step = window.guide.steps[1]
        self.assertEqual(step[1], "确认当前账号")
        self.assertIn("当前账号已验证", step[2])

    def test_window_close_cancels_login_without_leaving_thread(self):
        window = MainWindow(self.service)

        def login(*, cancel_check=None, **kwargs):
            while not cancel_check():
                time.sleep(0.01)
            raise BiliRewardCancelledError("cancelled")

        with patch.object(self.service, "ensure_login", side_effect=login):
            window.show()
            window.start_login()
            window.close()
            deadline = time.monotonic() + 2
            while window.worker_thread is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertIsNone(window.worker_thread)
            self.assertFalse(window.isVisible())

    def test_window_reads_current_theme_scale(self):
        with patch("ui.theme.SCALE", 1.5):
            window = MainWindow(self.service)
            self.assertEqual(window.link_rows[0].index_label.width(), 42)
            window.close()

    def test_batch_concurrency_is_bounded(self):
        tasks = [LinkTaskItem(i, "", str(i)) for i in range(9)]
        worker = ClaimWorker(
            self.service,
            tasks,
            RunSettings(concurrent=True, parallel_links=True, link_workers=4),
        )
        lock = threading.Lock()
        active = peak = 0
        batches = []
        worker.batch_completed.connect(batches.append)

        def prepare(task, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            raise BiliRewardError("fake")

        with patch.object(self.service, "prepare_link_task", side_effect=prepare):
            worker.run()
        self.assertEqual(len(batches[0].results), 9)
        self.assertEqual(peak, 4)

    def test_mock_batch_end_to_end_reuses_login_nav(self):
        save_cookie_file(
            self.service.paths.cookie_file, {"SESSDATA": "fake", "bili_jct": "fake"}
        )
        worker = ClaimWorker(
            self.service, [LinkTaskItem(0, "", "a"), LinkTaskItem(1, "", "b")]
        )
        batches = []
        worker.batch_completed.connect(batches.append)
        counts = {"nav": 0, "info": 0}

        def request(client, url, **kwargs):
            if url == NAV_URL:
                counts["nav"] += 1
                return NAV
            if url.startswith(MISSION_INFO_URL):
                counts["info"] += 1
                return {
                    "code": 0,
                    "data": {
                        "act_id": "1",
                        "act_name": "activity",
                        "task_name": "task",
                        "reward_info": {"award_name": "award"},
                    },
                }
            return SUCCESS

        with patch.object(BiliRewardClient, "_request_json", request):
            worker.run()
        self.assertEqual([r.status for r in batches[0].results], ["success", "success"])
        self.assertEqual(counts, {"nav": 1, "info": 2})


if __name__ == "__main__":
    unittest.main()
