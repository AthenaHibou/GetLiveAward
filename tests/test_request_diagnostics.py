import os
import re
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.client import BiliRewardClient
from core.constants import MISSION_RECEIVE_URL, MISSION_INFO_URL
from core.errors import BiliRateLimitError, BiliRequestError, BiliRetryableResponseError
from core.logging_utils import format_log_line
from core.models import AwardInfo, LinkTaskItem, RunSettings
from core.network import RequestLimiter, RequestWait
from core.service import RewardService
from ui.worker import ClaimWorker


AWARD = AwardInfo("a", "activity", "task", "award")


class RequestDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.client = BiliRewardClient("bili_jct=COOKIE_SECRET", limiter=RequestLimiter(0))
        self.addCleanup(self.client.close)

    def test_request_and_response_share_id_with_separate_wait_and_call_times(self):
        logs, now = [], [10.0]
        self.client._local.log_callback = logs.append

        @contextmanager
        def admit(*args):
            now[0] += 0.125
            yield RequestWait(0.025, 0.1)

        def perform(*args):
            now[0] += 0.25
            return {"code": 0, "data": {"cdkey_content": "REWARD_SECRET"}}

        with (patch("core.client.time.perf_counter", side_effect=lambda: now[0]),
              patch.object(self.client.limiter, "request_slot", side_effect=admit),
              patch.object(self.client, "_perform_request", side_effect=perform)):
            self.client._request_json(MISSION_RECEIVE_URL + "?w_rid=SIGNATURE_SECRET", b"csrf=BODY_SECRET")
        self.assertEqual(len(logs), 2)
        self.assertEqual(re.search(r"request=(\w+)", logs[0]).group(1),
                         re.search(r"request=(\w+)", logs[1]).group(1))
        self.assertIn("接口=领取提交", logs[0])
        self.assertIn("并发槽等待=25.000 ms", logs[0])
        self.assertIn("间隔限流等待=100.000 ms", logs[0])
        self.assertIn("HTTP调用耗时=250.000 ms", logs[1])
        self.assertIn("code=0", logs[1])
        for secret in ("COOKIE_SECRET", "REWARD_SECRET", "SIGNATURE_SECRET", "BODY_SECRET"):
            self.assertNotIn(secret, "\n".join(logs))

    def test_frequency_response_is_logged_without_blocking_requests(self):
        logs = []
        response = Mock(status_code=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.json.return_value = {"code": -705, "message": "请求过于频繁，请稍后再试"}
        session = Mock()
        session.request.return_value = response
        with (patch.object(self.client, "_session", return_value=session),
              patch.object(self.client, "get_web_sign", return_value="w_rid=SECRET"),
              patch("core.client.MISSION_INFO_RETRY_SECONDS", 0)):
            with self.assertRaises(BiliRetryableResponseError):
                self.client.get_award_info("task", log_callback=logs.append)
        self.assertIn("接口=任务信息", logs[0])
        self.assertTrue(any("HTTP=200, code=-705" in line and "BiliRetryableResponseError" in line for line in logs))
        self.client.limiter.check_blocked()
        self.assertIsNone(self.client._local.log_callback)

    def test_queue_rejection_is_not_reported_as_a_sent_request(self):
        logs = []
        self.client._local.log_callback = logs.append
        self.client.limiter.block()
        with patch.object(self.client, "_perform_request") as perform:
            with self.assertRaises(BiliRateLimitError):
                self.client._request_json(MISSION_RECEIVE_URL)
        perform.assert_not_called()
        self.assertEqual(len(logs), 1)
        self.assertIn("[请求-未发送]", logs[0])

    def test_transport_error_logs_timing_without_exception_secrets(self):
        import requests
        logs = []
        self.client._local.log_callback = logs.append
        session = Mock()
        session.request.side_effect = requests.Timeout("URL_OR_TOKEN_SECRET")
        with patch.object(self.client, "_session", return_value=session):
            with self.assertRaises(BiliRequestError):
                self.client._request_json(MISSION_INFO_URL)
        self.assertTrue(any("HTTP调用耗时=" in line and "BiliRequestError" in line for line in logs))
        self.assertNotIn("URL_OR_TOKEN_SECRET", "\n".join(logs))

    def test_log_timestamp_includes_milliseconds(self):
        self.assertRegex(format_log_line("test"), r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\] test$")

    def test_serial_retry_wait_is_after_the_previous_response(self):
        logs, starts, now = [], [], [0.0]
        def respond(*args, **kwargs):
            starts.append(now[0])
            now[0] += 0.12
            return {"code": 202032} if len(starts) == 1 else {"code": 0, "data": {}}
        with (patch.object(self.client, "_signed_request", side_effect=respond),
              patch("core.client.time.monotonic", side_effect=lambda: now[0]),
              patch("core.client.threading.Event.wait", side_effect=lambda delay: now.__setitem__(0, now[0] + delay))):
            self.client.claim_award("task", award_info=AWARD, max_retry=2,
                                    retry_interval=0.05, log_callback=logs.append)
        self.assertAlmostEqual(starts[1] - starts[0], 0.17)
        self.assertTrue(any("响应处理后等待=50.000 ms" in line for line in logs))

    def test_existing_concurrency_can_send_before_the_first_response(self):
        both_started = threading.Barrier(2)
        active, maximum = [0], [0]
        lock = threading.Lock()
        def respond(*args, **kwargs):
            with lock:
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
            both_started.wait(2)
            with lock:
                active[0] -= 1
            return {"code": 0, "data": {}}
        with patch.object(self.client, "_signed_request", side_effect=respond):
            self.client.claim_award("task", award_info=AWARD, thread_count=2, max_retry=1)
        self.assertEqual(maximum[0], 2)

    def test_individual_task_batch_settings_and_failure_are_persisted(self):
        with tempfile.TemporaryDirectory() as root:
            service = RewardService(root)
            self.addCleanup(service.close)
            worker = ClaimWorker(service, [LinkTaskItem(0, "", "task", RunSettings())])
            with patch.object(service, "prepare_link_task", side_effect=BiliRateLimitError("[限流] code=-705")):
                worker.run()
            logs = Path(service.paths.log_file).read_text(encoding="utf-8")
            self.assertIn("启用请求并发=False", logs)
            self.assertIn("请求并发上限=1", logs)
            self.assertIn("[链接1] [任务] 失败", logs)
            self.assertIn("code=-705", logs)
            self.assertIn("全部链接处理完成", logs)
