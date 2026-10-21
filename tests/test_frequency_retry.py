import unittest
import tempfile
from unittest.mock import Mock, patch

from core.client import BiliRewardClient
from core.constants import NAV_URL
from core.errors import (BiliRateLimitError, BiliRewardError, BiliRewardCancelledError,
                         BiliRewardTimeoutError, BiliRetryableResponseError)
from core.models import AwardInfo, ClaimExecutionControl, RunSettings
from core.network import RequestLimiter
from core.service import RewardService
from ui.worker import LoginWorker

AWARD = AwardInfo("activity", "activity", "task", "reward")
SUCCESS = {"code": 0, "data": {"extra_info": {"cdkey_content": "TEST-KEY"}}}


def response(payload, status=200):
    result = Mock(status_code=status)
    result.json.return_value = payload
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


class FrequencyRetryTests(unittest.TestCase):
    def client(self, ignore=False):
        client = BiliRewardClient("bili_jct=fake", limiter=RequestLimiter(0),
                                  ignore_risk_control=ignore)
        self.addCleanup(client.close)
        client.get_web_sign = Mock(return_value="test=1")
        client._session = Mock(return_value=Mock())
        return client

    def test_frequency_responses_retry_without_opt_in_in_both_dispatch_modes(self):
        for asynchronous in (False, True):
            for payload in ({"code": -702}, {"code": -705},
                            {"code": 999, "message": "请求频率过高，请稍后再试"}):
                with self.subTest(async_dispatch=asynchronous, payload=payload):
                    client = self.client()
                    logs = []
                    client._session().request.side_effect = [response(payload), response(SUCCESS)]
                    _, key = client.claim_award("task", award_info=AWARD, max_retry=2,
                                                retry_interval=0, async_dispatch=asynchronous,
                                                log_callback=logs.append)
                    self.assertEqual(key, "TEST-KEY")
                    self.assertEqual(client._session().request.call_count, 2)
                    client.limiter.check_blocked()
                    self.assertTrue(any(line.startswith("[领取-提交] 失败:") for line in logs))
                    self.assertFalse(any("频率限制-可重试" in line or "BiliRetryableResponseError" in line for line in logs))
                    if not asynchronous:
                        self.assertTrue(any("[重试-等待]" in line and "原因=[领取-提交] 失败:" in line for line in logs))

    def test_explicit_risk_defaults_to_stop_and_opt_in_retries(self):
        cases = [(412, {}), (429, {}),
                 *[(200, {"code": code}) for code in (-352, -412, -509)],
                 (200, {"code": 999, "message": "IP 访问异常"}),
                 (200, {"code": -702, "message": "IP异常"})]
        for status, payload in cases:
            for ignore in (False, True):
                with self.subTest(status=status, payload=payload, ignore=ignore):
                    client = self.client(ignore)
                    logs = []
                    client._session().request.side_effect = [response(payload, status), response(SUCCESS)]
                    if ignore:
                        _, key = client.claim_award("task", award_info=AWARD, max_retry=2,
                                                    retry_interval=0, log_callback=logs.append)
                        self.assertEqual(key, "TEST-KEY")
                        self.assertEqual(client._session().request.call_count, 2)
                        self.assertTrue(any("可能导致风控加剧" in line for line in logs))
                        client.limiter.check_blocked()
                    else:
                        with self.assertRaises(BiliRateLimitError):
                            client.claim_award("task", award_info=AWARD, max_retry=2, retry_interval=0)
                        self.assertEqual(client._session().request.call_count, 1)
                        with self.assertRaises(BiliRateLimitError):
                            client.limiter.check_blocked()

    def test_frequency_and_ignored_risk_respect_attempt_budget(self):
        for ignore, code in ((False, -705), (True, -412)):
            client = self.client(ignore)
            client._session().request.return_value = response({"code": code})
            with self.assertRaisesRegex(BiliRewardError, "最大重试次数"):
                client.claim_award("task", award_info=AWARD, max_retry=3, retry_interval=0)
            self.assertEqual(client._session().request.call_count, 3)

    def test_cancellation_interrupts_frequency_retry_wait(self):
        client = self.client()
        control = ClaimExecutionControl()
        client._session().request.return_value = response({"code": -705})
        def log(line):
            if "[重试-等待]" in line:
                control.cancel()
        with self.assertRaises(BiliRewardCancelledError):
            client.claim_award("task", award_info=AWARD, max_retry=10, retry_interval=30,
                               claim_control=control, log_callback=log)
        self.assertEqual(client._session().request.call_count, 1)

    def test_duration_interrupts_frequency_and_ignored_risk_retry_wait(self):
        for ignore, code in ((False, -705), (True, -412)):
            with self.subTest(ignore=ignore, code=code):
                client = self.client(ignore)
                client._session().request.return_value = response({"code": code})
                with self.assertRaises(BiliRewardTimeoutError):
                    client.claim_award("task", award_info=AWARD, max_retry=None,
                                       duration_seconds=0.03, retry_interval=30)
                self.assertEqual(client._session().request.call_count, 1)

    def test_setup_requests_retry_and_exhaust_bounded_window(self):
        for ignore, code in ((False, -702), (False, -705), (True, -412)):
            with self.subTest(ignore=ignore, code=code):
                client = self.client(ignore)
                client._session().request.side_effect = [response({"code": code}), response({"code": 0})]
                with patch("core.client.MISSION_INFO_RETRY_INTERVAL", 0.001):
                    self.assertEqual(client._request_json(NAV_URL), {"code": 0})
                self.assertEqual(client._session().request.call_count, 2)
                client._session().request.side_effect = None
                client._session().request.return_value = response({"code": code})
                with (patch("core.client.MISSION_INFO_RETRY_SECONDS", 0),
                      self.assertRaises(BiliRetryableResponseError)):
                    client._request_json(NAV_URL)

    def test_login_worker_forwards_risk_option_to_client_creation(self):
        for ignore in (False, True):
            with self.subTest(ignore=ignore), tempfile.TemporaryDirectory() as root:
                service = RewardService(root)
                try:
                    with patch.object(service, "_client") as create:
                        LoginWorker(service, RunSettings(ignore_risk_control=ignore)).run()
                    self.assertEqual(create.call_args.args[0].ignore_risk_control, ignore)
                finally:
                    service.close()
