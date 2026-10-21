import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtWidgets import QApplication

from core.client import BiliRewardClient
from core.errors import BiliRewardCancelledError
from core.models import AwardInfo, LinkTaskItem, RunSettings
from core.network import RequestLimiter
from core.service import RewardService
from core.storage import save_cookie_file
from core.task_config import SavedTask, TaskDocument
from ui.main_window import MainWindow
from ui.worker import ClaimWorker


class DispatchControlTests(unittest.TestCase):
    def test_multi_task_and_single_task_concurrency_are_independent(self):
        multi = RunSettings(parallel_links=True, link_workers=3, global_request_workers=4)
        self.assertEqual((multi.active_links, multi.active_requests), (3, 4))
        self.assertEqual(multi.claim_options("a").thread_count, 1)
        single = RunSettings(concurrent=True, request_workers=3, global_request_workers=4)
        self.assertEqual((single.active_links, single.active_requests), (1, 4))
        self.assertEqual(single.claim_options("a").thread_count, 3)
        asynchronous = RunSettings(async_dispatch=True, request_workers=4)
        self.assertEqual(asynchronous.active_requests, 2)
        self.assertEqual(asynchronous.active_links, 1)
        self.assertEqual(asynchronous.claim_options("a").thread_count, 4)
        self.assertTrue(asynchronous.claim_options("a").async_dispatch)

    def test_new_account_cap_defaults_when_importing_old_configuration(self):
        payload = TaskDocument([SavedTask()]).payload()
        del payload["global"]["global_request_workers"]
        document = TaskDocument.parse(payload)
        self.assertEqual(document.global_settings.global_request_workers, 2)
        self.assertFalse(document.global_settings.parallel_links)

    def test_ready_requests_use_row_priority_instead_of_thread_arrival_order(self):
        shared = RequestLimiter(0, 1, max_tasks=1)
        ready = [threading.Event() for _ in range(3)]
        order = []
        def request(index):
            with shared.task_slot(object(), index, ready[index].set):
                order.append(index)
        with ThreadPoolExecutor(max_workers=3) as executor:
            with shared.task_slot(object(), -1, lambda: None):
                futures = [executor.submit(request, i) for i in (2, 1, 0)]
                for event in ready:
                    self.assertTrue(event.wait(1))
            for future in futures:
                future.result(1)
        self.assertEqual(order, [0, 1, 2])

    def test_cancelled_priority_waiter_does_not_block_remaining_tasks(self):
        shared = RequestLimiter(0, 1, max_tasks=1)
        ready, cancel = threading.Event(), threading.Event()
        def check():
            ready.set()
            if cancel.is_set():
                raise BiliRewardCancelledError("cancel only first")
        with ThreadPoolExecutor(max_workers=1) as executor:
            with shared.task_slot(object(), -1, lambda: None):
                def request():
                    with shared.task_slot(object(), 0, check):
                        self.fail("cancelled request was sent")
                future = executor.submit(request)
                self.assertTrue(ready.wait(1))
                cancel.set()
                with self.assertRaises(BiliRewardCancelledError):
                    future.result(1)
            with shared.task_slot(object(), 1, lambda: None):
                pass
        self.assertFalse(shared._pending)
        self.assertFalse(shared._active_tasks)

    def test_pausing_a_queued_priority_request_releases_other_tasks(self):
        shared = RequestLimiter(0, 1, max_tasks=1)
        queued, paused, resume = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def check():
            queued.set()
            if paused.is_set():
                self.assertTrue(resume.wait(2))
        with ThreadPoolExecutor(max_workers=2) as executor:
            with shared.task_slot(object(), -1, lambda: None):
                def request(priority, callback):
                    with shared.task_slot(object(), priority, callback):
                        calls.append(priority)
                first = executor.submit(request, 0, check)
                self.assertTrue(queued.wait(1))
                paused.set()
                second = executor.submit(request, 1, lambda: None)
            try:
                second.result(1)
                self.assertFalse(first.done())
            finally:
                resume.set()
            first.result(1)
        self.assertEqual(calls, [1, 0])

    def test_single_task_concurrency_does_not_enable_cross_task_overlap(self):
        shared = RequestLimiter(0, 3, max_tasks=1)
        first_key = object()
        second_ready = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with shared.task_slot(first_key, 0, lambda: None):
                with shared.task_slot(first_key, 0, lambda: None):
                    def other_task():
                        with shared.task_slot(object(), 1, second_ready.set):
                            return True
                    future = executor.submit(other_task)
                    self.assertTrue(second_ready.wait(1))
                    self.assertFalse(future.done())
                self.assertFalse(future.done())
            self.assertTrue(future.result(1))

    def test_multi_task_cap_and_global_cap_are_both_enforced(self):
        shared = RequestLimiter(0, 3, max_tasks=2)
        ready = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with shared.task_slot(object(), 0, lambda: None):
                with shared.task_slot(object(), 1, lambda: None):
                    def third():
                        with shared.task_slot(object(), 2, ready.set):
                            return True
                    future = executor.submit(third)
                    self.assertTrue(ready.wait(1))
                    self.assertFalse(future.done())
                self.assertTrue(future.result(1))


class DispatchUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.window = MainWindow(RewardService(self.root.name))
        self.addCleanup(self.window.close)

    def test_controls_persist_independently_and_explain_defaults(self):
        panel = self.window.settings_panel
        self.assertFalse(panel.parallel_links.isChecked())
        self.assertFalse(panel.concurrent.isChecked())
        self.assertFalse(panel.requests.isEnabled())
        panel.parallel_links.setChecked(True)
        self.assertTrue(panel.links.isEnabled())
        self.assertFalse(panel.requests.isEnabled())
        panel.global_requests.setValue(3)
        panel.async_dispatch.setChecked(True)
        self.assertEqual(panel.snapshot().claim_options("a").thread_count, 2)
        self.assertIn("默认关闭", panel.dispatch_note.text())
        self.assertIn("列表顺序", panel.dispatch_note.text())
        self.assertIn("多任务", panel.async_dispatch.toolTip())
        self.assertIn("请勿变动", panel.risk_note.text())
        self.assertEqual(panel.interval_label.text(), "执行间隔 / ms")
        self.assertEqual(panel.async_dispatch.text(), "异步派发")
        self.assertIn("多任务异步派发", panel.parallel_links.text())
        self.assertIn("<table width=", panel.async_dispatch.toolTip())
        self.assertIn("<br><br>", panel.async_dispatch.toolTip())
        self.assertTrue(self.window._autosave_workspace())
        self.window.close()
        reopened = MainWindow(RewardService(self.root.name))
        self.addCleanup(reopened.close)
        saved = reopened.settings_panel.snapshot()
        self.assertTrue(saved.parallel_links)
        self.assertFalse(saved.concurrent)
        self.assertTrue(saved.async_dispatch)
        self.assertEqual(saved.global_request_workers, 3)

    def test_single_task_async_sends_next_http_before_response_without_concurrent_toggle(self):
        service = self.window.service
        settings = RunSettings(async_dispatch=True, request_workers=2, global_request_workers=2,
                               interval_ms=100, global_interval_ms=100, max_retry=1)
        self.assertFalse(settings.concurrent)
        self.assertFalse(settings.parallel_links)
        service.configure(settings)
        save_cookie_file(service.paths.cookie_file, {"SESSDATA": "fake", "bili_jct": "fake"})
        second_started = threading.Event()
        calls, checks = [], []
        lock = threading.Lock()
        def respond(client, url, data, headers):
            with lock:
                calls.append(data)
                first = len(calls) == 1
            if first:
                checks.append(second_started.wait(2))
            else:
                second_started.set()
            return {"code": 0, "data": {}}
        with (patch.object(BiliRewardClient, "ensure_logged_in", return_value={}),
              patch.object(BiliRewardClient, "get_award_info", return_value=AwardInfo("a", "a", "t", "r")),
              patch.object(BiliRewardClient, "get_web_sign", return_value="test=1"),
              patch.object(BiliRewardClient, "_perform_request", respond)):
            service.run_claim(settings.claim_options("a"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(checks, [True])


    def test_multi_task_async_http_can_overlap_without_extra_task_workers(self):
        service = self.window.service
        save_cookie_file(service.paths.cookie_file, {"SESSDATA": "fake", "bili_jct": "fake"})
        worker = ClaimWorker(service, [LinkTaskItem(i, "", str(i)) for i in range(2)],
            RunSettings(parallel_links=True, global_request_workers=2,
                        global_interval_ms=100, interval_ms=100, max_retry=1))
        first, second = threading.Event(), threading.Event()
        active, maximum = {}, {}
        results, checks = [], []
        lock = threading.Lock()
        worker.batch_completed.connect(results.append)

        def respond(client, url, data, headers):
            if "nav" in url:
                return {"code": 0, "data": {"isLogin": True, "wbi_img": {
                    "img_url": "https://x/" + "a" * 32 + ".png",
                    "sub_url": "https://x/" + "b" * 32 + ".png"}}}
            if data is None:
                return {"code": 0, "data": {"act_id": "a", "act_name": "a", "task_name": "t",
                                             "reward_info": {"award_name": "award"}}}
            with lock:
                active[client] = active.get(client, 0) + 1
                maximum[client] = max(maximum.get(client, 0), active[client])
                is_first = not first.is_set()
                if is_first:
                    first.set()
                else:
                    second.set()
            if is_first:
                checks.append(second.wait(2))
            with lock:
                active[client] -= 1
            return {"code": 0, "data": {}}

        with patch.object(BiliRewardClient, "_perform_request", respond):
            worker.run()
        self.assertEqual(checks, [True])
        self.assertEqual(list(maximum.values()), [1, 1])
        self.assertEqual([r.status for r in results[0].results], ["success", "success"])
