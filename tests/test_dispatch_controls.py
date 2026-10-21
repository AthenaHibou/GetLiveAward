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
