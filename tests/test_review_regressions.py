import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QEvent
from PyQt5.QtGui import QFocusEvent

from core.client import BiliRewardClient
from core.errors import BiliRewardCancelledError, BiliRewardError, BiliRateLimitError
from core.models import AwardInfo, ClaimOptions, ClaimExecutionControl, LinkTaskItem, PreparedLinkTask, RunSettings
from core.network import RequestLimiter
from core.service import RewardService
from core.storage import save_cookie_file
from core.task_config import load_document
from ui.main_window import MainWindow
from ui.worker import ClaimWorker, LoginWorker

AWARD = AwardInfo("a", "activity", "task", "award")
INELIGIBLE = {"code": 202032, "message": "无资格领取奖励"}
EXPIRED = {"code": -101, "message": "账号未登录"}


class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = RewardService(self.temp.name)
        self.addCleanup(self.service.close)

    def test_login_only_is_not_blocked_by_unavailable_ntp(self):
        worker = LoginWorker(self.service, RunSettings(retry_mode="scheduled"))
        checked = []
        worker.login_checked.connect(lambda success, message: checked.append(success))
        with (patch.object(worker, "_sync_clock", side_effect=BiliRewardError("NTP unavailable")) as sync,
              patch.object(self.service, "ensure_login") as login):
            worker.run()
        login.assert_called_once()
        sync.assert_not_called()
        self.assertEqual(checked, [True])

    def test_pending_task_edits_are_saved_before_login(self):
        window = MainWindow(self.service)
        self.addCleanup(window.close)
        window.link_rows[0].name_edit.setText("尚未等待自动保存")
        window.link_rows[0].input.setText("https://x/?task_id=a")
        with patch.object(window, "_start_worker"):
            window.start_login()
        document = load_document(window._workspace_path())
        self.assertIsNotNone(document)
        self.assertEqual(document.tasks[0].name, "尚未等待自动保存")
        self.assertEqual(document.tasks[0].url, "https://x/?task_id=a")
        window._set_running(False)

    def test_focus_does_not_open_recursive_dialogs_for_invalid_settings(self):
        window = MainWindow(self.service)
        self.addCleanup(window.close)
        first = window.link_rows[0]
        window.add_link_row()
        second = window.link_rows[1]
        window.select_task(first)
        window.settings_panel.server.setText("https://invalid/")
        with patch("ui.main_window.QMessageBox.warning") as warning:
            self.app.sendEvent(second.input, QFocusEvent(QEvent.FocusIn))
        warning.assert_not_called()
        self.assertIs(window._selected_row, first)
        window.settings_panel.server.setText("ntp.aliyun.com")
        self.app.sendEvent(second.input, QFocusEvent(QEvent.FocusIn))
        self.assertIs(window._selected_row, second)

    def test_log_filter_follows_task_identity_after_inserting_a_row(self):
        window = MainWindow(self.service)
        self.addCleanup(window.close)
        first = window.link_rows[0]
        window.add_link_row()
        second = window.link_rows[1]
        window.append_log("[链接1] FIRST-OLD")
        window.append_log("[链接2] SECOND-OLD")
        window.add_link_row(after_row=first)
        window.select_task(second)
        window.log_filter.setCurrentIndex(1)
        self.assertIn("SECOND-OLD", window.log_text.toPlainText())
        self.assertNotIn("FIRST-OLD", window.log_text.toPlainText())
        window.select_task(window.link_rows[1])
        self.assertNotIn("SECOND-OLD", window.log_text.toPlainText())

    def test_scan_confirmation_is_not_displayed_as_verified_login(self):
        window = MainWindow(self.service)
        self.addCleanup(window.close)
        window.append_log("[登录] 扫码登录成功")
        self.assertNotEqual(window.login_status.text(), "登录已验证")
        window.append_log("[Cookie] 登录状态验证通过")
        self.assertEqual(window.login_status.text(), "登录已验证")

    def test_login_cancels_while_waiting_for_nav_request_slot(self):
        save_cookie_file(self.service.paths.cookie_file, {"SESSDATA": "fake", "bili_jct": "fake"})
        cancel = threading.Event()
        finished = threading.Event()
        failures = []
        self.service._limiter._next_start = time.monotonic() + 30
        def login():
            try:
                self.service.ensure_login(cancel_check=cancel.is_set)
            except BiliRewardError as exc:
                failures.append(exc)
            finally:
                finished.set()
        with patch.object(BiliRewardClient, "_perform_request", return_value={"data": {"isLogin": True}}) as request:
            thread = threading.Thread(target=login)
            thread.start()
            time.sleep(0.03)
            cancel.set()
            cancelled_promptly = finished.wait(0.3)
            self.service._limiter._next_start = 0
            thread.join(2)
            self.assertTrue(cancelled_promptly)
            request.assert_not_called()
        self.assertTrue(any(isinstance(exc, BiliRewardCancelledError) for exc in failures))

    def test_retry_count_is_not_reset_after_cookie_expiry(self):
        old = BiliRewardClient("bili_jct=fake", limiter=RequestLimiter(0))
        new = BiliRewardClient("bili_jct=fake", limiter=RequestLimiter(0))
        self.addCleanup(old.close)
        self.addCleanup(new.close)
        old._signed_request = Mock(side_effect=[INELIGIBLE, EXPIRED])
        new._signed_request = Mock(return_value=INELIGIBLE)
        new.get_award_info = Mock(return_value=AWARD)
        task = PreparedLinkTask(0, "", "task", ClaimOptions("task", max_retry=3, retry_interval=0), old, AWARD, Mock())
        with patch.object(self.service, "_client", return_value=new):
            with self.assertRaisesRegex(BiliRewardError, "最大重试"):
                self.service.claim_prepared_task(task)
        self.assertEqual(old._signed_request.call_count + new._signed_request.call_count, 3)

    def test_exhausted_retry_count_does_not_start_another_qr_login(self):
        client = BiliRewardClient("bili_jct=fake", limiter=RequestLimiter(0))
        self.addCleanup(client.close)
        client._signed_request = Mock(return_value=EXPIRED)
        task = PreparedLinkTask(0, "", "task", ClaimOptions("task", max_retry=1, retry_interval=0), client, AWARD, Mock())
        with patch.object(self.service, "_client") as refresh:
            with self.assertRaises(BiliRewardError):
                self.service.claim_prepared_task(task)
        refresh.assert_not_called()

    def test_risk_block_stops_another_tasks_scheduled_wait(self):
        worker = ClaimWorker(self.service, [LinkTaskItem(0, "", "a"), LinkTaskItem(1, "", "b")])
        self.service._limiter.block()
        self.service.clock = Mock(now=Mock(return_value=100), age=Mock(return_value=0))
        with patch.object(worker._cancel_event, "wait", side_effect=RuntimeError("still waiting")):
            with self.assertRaises(BiliRateLimitError):
                worker._wait_scheduled(1000, {0: Mock()})

    def test_real_local_http_risk_responses_block_following_requests(self):
        class Handler(BaseHTTPRequestHandler):
            calls = 0
            def do_GET(self):
                type(self).calls += 1
                self.send_response(int(self.path.strip("/")))
                body = b'{"code":-1}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for status in (412, 429):
                client = BiliRewardClient("", limiter=RequestLimiter(0))
                client._session().trust_env = False
                previous = Handler.calls
                try:
                    for _ in range(2):
                        with self.assertRaises(BiliRateLimitError):
                            client._request_json(f"http://127.0.0.1:{server.server_port}/{status}")
                    self.assertEqual(Handler.calls - previous, 1)
                finally:
                    client.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_paused_claim_stops_on_global_risk_or_external_cancellation(self):
        for risk in (True, False):
            limiter = RequestLimiter(0)
            client = BiliRewardClient("bili_jct=fake", limiter=limiter)
            control = ClaimExecutionControl()
            control.pause()
            cancelled, done = threading.Event(), threading.Event()
            failures = []
            def claim():
                try:
                    client.claim_award("task", award_info=AWARD, claim_control=control,
                                       max_retry=None, cancel_check=cancelled.is_set)
                except BiliRewardError as exc:
                    failures.append(exc)
                finally:
                    done.set()
            thread = threading.Thread(target=claim)
            with patch.object(client, "_signed_request") as request:
                thread.start()
                time.sleep(0.03)
                limiter.block() if risk else cancelled.set()
                stopped = done.wait(0.4)
                control.cancel()
                thread.join(2)
                request.assert_not_called()
            client.close()
            self.assertTrue(stopped)
            self.assertIsInstance(failures[0], BiliRateLimitError if risk else BiliRewardCancelledError)

    def test_real_qt_worker_runs_independent_tasks_with_mocked_http(self):
        save_cookie_file(self.service.paths.cookie_file, {"SESSDATA": "fake", "bili_jct": "fake"})
        window = MainWindow(self.service)
        self.addCleanup(window.close)
        window.link_rows[0].input.setText("https://www.bilibili.com/?task_id=a")
        window.settings_panel.apply(RunSettings(interval_ms=100, global_interval_ms=100, max_retry=3))
        window.add_link_row(text="https://www.bilibili.com/?task_id=b")
        window.settings_panel.apply(RunSettings(interval_ms=200, global_interval_ms=100, max_retry=2))
        receives = []
        def respond(client, url, data, headers):
            if "nav" in url:
                return {"code": 0, "data": {"isLogin": True, "mid": 123, "uname": "测试用户",
                        "wbi_img": {"img_url": "https://x/" + "a" * 32 + ".png",
                                    "sub_url": "https://x/" + "b" * 32 + ".png"}}}
            if data is None:
                return {"code": 0, "data": {"act_id": "a", "act_name": "activity", "task_name": "task",
                                             "reward_info": {"award_name": "award"}}}
            receives.append(data)
            if len(receives) == 1:
                return INELIGIBLE
            return {"code": 0, "data": {"extra_info": {"cdkey_content": "TEST-KEY"}}}
        with (patch.object(BiliRewardClient, "_perform_request", respond),
              patch("ui.main_window.QMessageBox.information")):
            window.start_claim()
            deadline = time.monotonic() + 4
            while window.worker_thread is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
            if window.worker_thread is not None:
                window.stop_all()
                while window.worker_thread is not None:
                    self.app.processEvents()
                    time.sleep(0.005)
                self.fail("Qt worker did not finish within 4 seconds")
        self.assertEqual([row.status_label.text() for row in window.link_rows], ["成功", "成功"])
        self.assertEqual(len(receives), 3)
        self.assertIn(b"task_id=a", receives[0])
        self.assertIn(b"task_id=a", receives[1])
        self.assertIn(b"task_id=b", receives[2])
        self.assertFalse(window._is_running)
        self.assertTrue(window.start_btn.isEnabled())
        self.assertIn("测试用户", window.account_combo.currentText())
