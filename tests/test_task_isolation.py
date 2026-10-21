import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtWidgets import QApplication

from core.client import BiliRewardClient
from core.errors import BiliRateLimitError, BiliRewardCancelledError, BiliRewardError
from core.models import AwardInfo, ClaimOptions, LinkTaskItem, PreparedLinkTask, RunSettings
from core.network import RequestLimiter, TaskRequestLimiter
from core.service import RewardService
from core.storage import save_cookie_file
from ui.worker import ClaimWorker


AWARD = AwardInfo("act", "activity", "task", "award")
NAV = {"code": 0, "data": {"isLogin": True, "wbi_img": {
    "img_url": "https://x/" + "a" * 32 + ".png",
    "sub_url": "https://x/" + "b" * 32 + ".png"}}}
INFO = {"code": 0, "data": {"act_id": "act", "act_name": "activity",
    "task_name": "task", "reward_info": {"award_name": "award"}}}


class TaskIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.service = RewardService(root.name)
        self.addCleanup(self.service.close)

    def ready(self, task, **kwargs):
        return PreparedLinkTask(task.index, "", task.task_id, kwargs["options"],
                                Mock(), AWARD, Mock())

    def test_retrying_task_cannot_hold_up_another_task(self):
        worker = ClaimWorker(self.service, [LinkTaskItem(i, "", str(i)) for i in range(2)])
        first_started, second_finished = threading.Event(), threading.Event()
        outcomes, results = [], []
        worker.batch_completed.connect(results.append)

        def claim(task, **kwargs):
            if task.index == 0:
                first_started.set()
                outcomes.append(second_finished.wait(1))
                raise BiliRewardError("task A failed after B finished")
            outcomes.append(first_started.wait(1))
            second_finished.set()
            return SimpleNamespace(cdkey="OK")

        with (patch.object(self.service, "prepare_link_task", side_effect=self.ready),
              patch.object(self.service, "claim_prepared_task", side_effect=claim)):
            worker.run()
        self.assertEqual(outcomes, [True, True])
        self.assertEqual([r.status for r in results[0].results], ["failed", "success"])

    def test_paused_task_can_be_cancelled_without_stopping_other_task(self):
        worker = ClaimWorker(self.service, [LinkTaskItem(i, "", str(i)) for i in range(2)])
        worker.pause_claim(0)
        results, submitted = [], []
        worker.batch_completed.connect(results.append)

        def ready(task, **kwargs):
            client = self.service._new_client(ClaimOptions(task.task_id))
            def submit(*args, **kw):
                submitted.append(task.index)
                worker.cancel_claim(0)
                return {"code": 0, "data": {}}
            client._signed_request = submit
            client.get_csrf = lambda: "fake"
            return PreparedLinkTask(task.index, "", task.task_id, kwargs["options"],
                                    client, AWARD, Mock())

        with patch.object(self.service, "prepare_link_task", side_effect=ready):
            worker.run()
        self.assertEqual(submitted, [1])
        self.assertEqual([r.status for r in results[0].results], ["cancelled", "success"])

    def test_future_scheduled_task_does_not_block_due_task_or_its_result(self):
        tasks = [LinkTaskItem(i, "", str(i), RunSettings(
            retry_mode="scheduled", scheduled_time="01:00:00")) for i in range(2)]
        worker = ClaimWorker(self.service, tasks)
        waiting, completed = threading.Event(), threading.Event()
        checks, results = [], []
        worker.batch_completed.connect(results.append)

        def wait(target, prepared, settings):
            index = next(iter(prepared))
            if index == 0:
                waiting.set()
                checks.append(completed.wait(1))
                raise BiliRewardCancelledError("cancel only future task")
            checks.append(waiting.wait(1))

        def claim(task, **kwargs):
            completed.set()
            return SimpleNamespace(cdkey="OK")

        with (patch.object(worker, "_sync_clock"),
              patch.object(worker, "_wait_scheduled", side_effect=wait),
              patch.object(self.service, "prepare_link_task", side_effect=self.ready),
              patch.object(self.service, "claim_prepared_task", side_effect=claim)):
            worker.run()
        self.assertEqual(checks, [True, True])
        self.assertEqual([r.status for r in results[0].results], ["cancelled", "success"])

    def test_real_response_risk_is_local_even_with_identical_links_and_options(self):
        save_cookie_file(self.service.paths.cookie_file, {"SESSDATA": "fake", "bili_jct": "fake"})
        # Exercise real parsing/blocking and account slots, not a mocked exception.
        for status, payload in ((200, {"code": -705, "message": "请求过于频繁"}),
                                (200, {"code": -702, "message": "请求频率过高"}),
                                (429, {}), (412, {})):
            with self.subTest(status=status, payload=payload):
                worker = ClaimWorker(self.service, [LinkTaskItem(i, "", "same") for i in range(2)],
                                     RunSettings(interval_ms=100, global_interval_ms=100, max_retry=1))
                results, submitted = [], []
                worker.batch_completed.connect(results.append)
                lock = threading.Lock()

                def session(client):
                    fake = Mock()
                    def request(method, url, **kwargs):
                        response = Mock(status_code=200)
                        body = NAV if "nav" in url else INFO
                        if method == "POST":
                            with lock:
                                submitted.append(client)
                                first = len(submitted) == 1
                            response.status_code = status if first else 200
                            body = payload if first else {"code": 0, "data": {}}
                        response.json.return_value = body
                        response.__enter__ = Mock(return_value=response)
                        response.__exit__ = Mock(return_value=False)
                        return response
                    fake.request.side_effect = request
                    return fake

                with patch.object(BiliRewardClient, "_session", session):
                    worker.run()
                self.assertEqual(len(submitted), 2)
                self.assertIsNot(submitted[0], submitted[1])
                self.assertCountEqual([r.status for r in results[0].results], ["failed", "success"])

    def test_task_cadence_does_not_reserve_account_slot_or_delay_other_task(self):
        shared = RequestLimiter(0)
        first, second = TaskRequestLimiter(shared), TaskRequestLimiter(shared)
        with first.request_slot(interval=60):
            pass
        waiting = threading.Event()
        failures = []
        def wait_first():
            try:
                with first.request_slot(check=waiting.set, interval=60):
                    self.fail("task A should still be waiting")
            except BiliRateLimitError:
                failures.append(True)
        thread = threading.Thread(target=wait_first)
        thread.start()
        try:
            self.assertTrue(waiting.wait(1))
            self.assertTrue(shared._slots.acquire(timeout=0.2))
            shared._slots.release()
            with second.request_slot():
                pass
        finally:
            first.block()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [True])

    def test_separate_tasks_still_obey_account_spacing_and_inflight_limit(self):
        shared = RequestLimiter(0.03, 1)
        clients = [TaskRequestLimiter(shared) for _ in range(3)]
        barrier = threading.Barrier(3)
        starts, active, peak = [], 0, 0
        lock = threading.Lock()
        def request(client):
            nonlocal active, peak
            barrier.wait(1)
            with client.request_slot():
                with lock:
                    starts.append(time.monotonic())
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.01)
                with lock:
                    active -= 1
        threads = [threading.Thread(target=request, args=(client,)) for client in clients]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertEqual(len(starts), 3)
        self.assertEqual(peak, 1)
        self.assertTrue(all(b - a >= 0.025 for a, b in zip(starts, starts[1:])))
