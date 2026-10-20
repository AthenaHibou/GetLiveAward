from __future__ import annotations

import hashlib
import random
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Callable

import requests

from .constants import (
    DEFAULT_USER_AGENT,
    MISSION_INFO_RETRY_INTERVAL,
    MISSION_INFO_RETRY_SECONDS,
    MISSION_INFO_URL,
    MISSION_RECEIVE_URL,
    MIXIN_KEY_ENC_TAB,
    NAV_URL,
    WEB_LOCATION,
)
from .errors import (
    BiliCookieInvalidError,
    BiliRateLimitError,
    BiliRequestError,
    BiliRewardCancelledError,
    BiliRewardError,
    BiliRewardTimeoutError,
)
from .models import (
    AwardInfo,
    ClaimExecutionControl,
    ClaimOptions,
    ClaimRetryBudget,
    OptionalCancelCheck,
    OptionalLogCallback,
)
from .network import RequestLimiter


def format_api_failure(
    stage: str, code: object, message: object, *, detail: str = ""
) -> str:
    suffix = f", {detail}" if detail else ""
    return f"[{stage}] 失败: code={code}, message={message}{suffix}"


def format_cookies_text(cookies: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def is_cookie_invalid_error(error: BiliRewardError) -> bool:
    return any(
        marker in str(error)
        for marker in (
            "code=-101",
            "code=61000",
            "账号未登录",
            "请先登录",
            "登录态",
            "Cookie 中缺少 bili_jct",
        )
    )


def should_relogin_from_error(error: BiliRewardError) -> bool:
    return isinstance(error, BiliCookieInvalidError) or is_cookie_invalid_error(error)


class _ClaimStopped(BiliRewardError):
    pass


class _RetryExhausted(BiliRewardError):
    pass


class BiliRewardClient:
    WBI_CACHE_SECONDS = 3600.0

    def __init__(
        self,
        cookie_string: str,
        timeout: float = 10.0,
        *,
        limiter: RequestLimiter | None = None,
        wall_time: Callable[[], float] | None = None,
        request_interval: float = 0,
    ) -> None:
        self.timeout = timeout
        self.request_interval = request_interval
        self._wall_time = wall_time or time.time
        self.cookie_string = cookie_string
        pairs = (part.split("=", 1) for part in cookie_string.split(";") if "=" in part)
        self.cookies = {
            key.strip(): value.strip() for key, value in pairs if key.strip()
        }
        self.limiter = limiter or RequestLimiter()
        self._local = threading.local()
        self._sessions: set[requests.Session] = set()
        self._sessions_lock = threading.Lock()
        self._wbi_lock = threading.Lock()
        self._wbi_keys: tuple[str, str] | None = None
        self._wbi_expires = 0.0

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Referer": "https://www.bilibili.com/",
                }
            )
            for name, value in self.cookies.items():
                session.cookies.set(
                    name, value, domain=".bilibili.com", path="/", secure=True
                )
            self._local.session = session
            with self._sessions_lock:
                self._sessions.add(session)
        return self._local.session

    def _close_thread_session(self) -> None:
        session = getattr(self._local, "session", None)
        if session is not None:
            session.close()
            with self._sessions_lock:
                self._sessions.discard(session)
            del self._local.session

    def close(self) -> None:
        """Call after all tasks using this client have finished."""
        with self._sessions_lock:
            for session in self._sessions:
                session.close()
            self._sessions.clear()
        self._local = threading.local()

    def _check_request(self) -> None:
        check = getattr(self._local, "check", None)
        if check is not None:
            check()

    def _request_json(
        self,
        url: str,
        data: Optional[bytes] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> dict:
        with self.limiter.request_slot(self._check_request, self.request_interval):
            return self._perform_request(url, data, headers)

    def _perform_request(self, url, data, headers) -> dict:
        try:
            with self._session().request(
                "POST" if data is not None else "GET",
                url,
                data=data,
                headers=headers,
                timeout=(min(5.0, self.timeout), self.timeout),
            ) as response:
                if response.status_code in (412, 429):
                    self.limiter.block()
                    raise BiliRateLimitError(
                        f"[限流] HTTP {response.status_code}，停止本批次请求"
                    )
                response.raise_for_status()
                payload = response.json()
        except requests.exceptions.JSONDecodeError as exc:
            raise BiliRewardError("接口返回了无法解析的 JSON") from exc
        except requests.HTTPError as exc:
            status = exc.response.status_code
            error_type = BiliRequestError if status >= 500 else BiliRewardError
            raise error_type(f"HTTP {status} 请求失败") from exc
        except requests.RequestException as exc:
            raise BiliRequestError(f"网络请求失败: {type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise BiliRewardError("接口返回的 JSON 不是对象")
        message = str(payload.get("message") or "")
        details = payload.get("data")
        if isinstance(details, dict):
            message += " " + str(details.get("message") or "")
        normalized = message.upper().replace(" ", "")
        if payload.get("code") in (-352, -412, -509) or any(
            marker in normalized
            for marker in (
                "IP访问异常",
                "IP异常",
                "请求过于频繁",
                "访问过于频繁",
                "操作太频繁",
            )
        ):
            self.limiter.block()
            raise BiliRateLimitError(
                format_api_failure("限流", payload.get("code"), payload.get("message"))
            )
        return payload

    def _cache_wbi_keys(self, payload: dict) -> tuple[str, str]:
        wbi_img = (payload.get("data") or {}).get("wbi_img") or {}
        keys = tuple(
            str(wbi_img.get(name) or "").rsplit("/", 1)[-1].split(".", 1)[0]
            for name in ("img_url", "sub_url")
        )
        if len(keys[0]) != 32 or len(keys[1]) != 32:
            raise BiliRewardError("获取 WBI 密钥失败")
        self._wbi_keys = keys
        self._wbi_expires = time.monotonic() + self.WBI_CACHE_SECONDS
        return keys

    def _get_wbi_keys(self) -> tuple[str, str]:
        # A paused claimant must not hold the shared key lock indefinitely.
        # Key reads may finish during a pause; submission still checks pause again.
        self._local.fetching_keys = True
        acquired = False
        try:
            while not self._wbi_lock.acquire(timeout=0.05):
                self._check_request()
            acquired = True
            self._check_request()
            if self._wbi_keys is not None and time.monotonic() < self._wbi_expires:
                return self._wbi_keys
            return self._cache_wbi_keys(self._request_json(NAV_URL))
        finally:
            if acquired:
                self._wbi_lock.release()
            self._local.fetching_keys = False

    def ensure_logged_in(self, *, cancel_check: OptionalCancelCheck = None) -> dict:
        previous = getattr(self._local, "check", None)
        if cancel_check is not None:
            self._local.check = lambda: self._check_cancel(cancel_check)
        try:
            payload = self._request_json(NAV_URL)
            data = payload.get("data") or {}
            if not isinstance(data, dict):
                raise BiliRewardError("[Cookie] 登录验证返回内容格式无效")
            if not data.get("isLogin"):
                raise BiliCookieInvalidError("[Cookie] 当前登录态无效: NAV isLogin=false")
            self.get_csrf()
            with self._wbi_lock:
                self._cache_wbi_keys(payload)
            return data
        finally:
            self._local.check = previous

    def get_web_sign(self, params: Optional[dict[str, str]] = None) -> str:
        raw_params = dict(params or {})
        img_key, sub_key = self._get_wbi_keys()
        source = img_key + sub_key
        mixin_key = "".join(source[i] for i in MIXIN_KEY_ENC_TAB)[:32]
        raw_params["wts"] = str(int(self._wall_time()))
        sanitized = {
            key: "".join(ch for ch in value if ch not in "!'()*")
            for key, value in sorted(raw_params.items())
        }
        query = urllib.parse.urlencode(sanitized)
        sanitized["w_rid"] = hashlib.md5(
            (query + mixin_key).encode("utf-8")
        ).hexdigest()
        return urllib.parse.urlencode(sanitized)

    def _signed_request(
        self, url: str, params: dict[str, str] | None = None, data: bytes | None = None
    ) -> dict:
        for attempt in range(2):
            query = self.get_web_sign(params)
            payload = self._request_json(
                f"{url}?{query}",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
                if data is not None
                else None,
            )
            if payload.get("code") != -403 or attempt:
                return payload
            with self._wbi_lock:
                self._wbi_expires = 0.0
        raise AssertionError("unreachable")

    def get_csrf(self) -> str:
        if self.cookies.get("bili_jct"):
            return self.cookies["bili_jct"]
        raise BiliCookieInvalidError("Cookie 中缺少 bili_jct，无法提交领取请求")

    @staticmethod
    def _check_cancel(cancel_check: OptionalCancelCheck) -> None:
        if cancel_check is not None and cancel_check():
            raise BiliRewardCancelledError("[任务] 已取消")

    def get_award_info(
        self,
        task_id: str,
        *,
        log_callback: OptionalLogCallback = None,
        cancel_check: OptionalCancelCheck = None,
    ) -> AwardInfo:
        deadline = time.monotonic() + MISSION_INFO_RETRY_SECONDS
        self._local.check = lambda: self._check_cancel(cancel_check)
        try:
            while True:
                self._check_request()
                payload = self._signed_request(
                    MISSION_INFO_URL, {"task_id": task_id, "web_location": WEB_LOCATION}
                )
                code, message = payload.get("code", -1), payload.get("message", "")
                if code == 0:
                    data = payload.get("data") or {}
                    result = AwardInfo(
                        str(data.get("act_id") or ""),
                        str(data.get("act_name") or ""),
                        str(data.get("task_name") or ""),
                        str((data.get("reward_info") or {}).get("award_name") or ""),
                    )
                    if not all(
                        (
                            result.act_id,
                            result.act_name,
                            result.task_name,
                            result.award_name,
                        )
                    ):
                        raise BiliRewardError("奖励信息字段不完整，无法继续领取")
                    return result
                error = BiliRewardError(
                    format_api_failure("领取-任务信息", code, message)
                )
                if should_relogin_from_error(error):
                    raise BiliCookieInvalidError(str(error))
                if log_callback is not None:
                    log_callback(str(error))
                if code != -702 or time.monotonic() >= deadline:
                    raise error
                until = min(deadline, time.monotonic() + MISSION_INFO_RETRY_INTERVAL)
                while time.monotonic() < until:
                    self._check_request()
                    time.sleep(min(0.05, max(0.0, until - time.monotonic())))
                if time.monotonic() >= deadline:
                    raise error
        finally:
            self._local.check = None

    def claim_award(
        self,
        task_id: str,
        *,
        award_info: AwardInfo | None = None,
        thread_count: int = 1,
        max_retry: int | None = 60,
        retry_interval: float = 1.0,
        log_callback: OptionalLogCallback = None,
        claim_control: ClaimExecutionControl | None = None,
        stop_sold_out: bool = True,
        duration_seconds: float | None = None,
        jitter_ms: int = 0,
        retry_budget: ClaimRetryBudget | None = None,
        cancel_check: OptionalCancelCheck = None,
    ) -> tuple[AwardInfo, str | None]:
        ClaimOptions(
            task_id,
            thread_count,
            max_retry,
            retry_interval,
            self.timeout,
            stop_sold_out,
            duration_seconds,
            jitter_ms,
        )
        control = claim_control or ClaimExecutionControl()
        budget = retry_budget or ClaimRetryBudget(max_retry, thread_count)
        self._check_cancel(cancel_check)
        control.check_cancelled()
        award_info = award_info or self.get_award_info(
            task_id, log_callback=log_callback,
            cancel_check=lambda: control.is_cancelled() or (cancel_check is not None and cancel_check()),
        )
        body = urllib.parse.urlencode(
            {
                "task_id": task_id,
                "activity_id": award_info.act_id,
                "activity_name": award_info.act_name,
                "task_name": award_info.task_name,
                "reward_name": award_info.award_name,
                "gaia_vtoken": "",
                "receive_from": "missionPage",
                "csrf": self.get_csrf(),
            }
        ).encode("utf-8")
        stop = threading.Event()
        deadline = (
            None if duration_seconds is None else time.monotonic() + duration_seconds
        )

        def check_wait() -> None:
            self._check_cancel(cancel_check)
            self.limiter.check_blocked()

        def check() -> None:
            if stop.is_set():
                raise _ClaimStopped("任务已结束")
            check_wait()
            if deadline is not None and time.monotonic() >= deadline:
                raise BiliRewardTimeoutError("[任务] 已达到执行时长，停止该链接")
            if not getattr(self._local, "fetching_keys", False):
                control.wait_if_paused(stop_event=stop, deadline=deadline,
                                       check_callback=check_wait)
            if stop.is_set():
                raise _ClaimStopped("任务已结束")
            control.check_cancelled()

        def worker(worker_id: int) -> str | None:
            self._local.check = check
            last_error = ""
            network_failures = 0
            try:
                while True:
                    check()
                    attempt = budget.reserve(worker_id)
                    if attempt is None:
                        break
                    delay = retry_interval
                    try:
                        payload = self._signed_request(MISSION_RECEIVE_URL, data=body)
                        code, message = (
                            payload.get("code", -1),
                            payload.get("message", ""),
                        )
                        if code == 0:
                            stop.set()
                            data = payload.get("data")
                            extra = data.get("extra_info") if isinstance(data, dict) else None
                            cdkey = extra.get("cdkey_content") if isinstance(extra, dict) else None
                            # Some rewards are credited directly and never return a code.
                            # Successful API responses must stop retries in either case.
                            if isinstance(cdkey, str) and cdkey.strip():
                                return cdkey
                            if log_callback is not None:
                                field_state = (
                                    "未提供" if not isinstance(extra, dict) or "cdkey_content" not in extra
                                    else "空值" if cdkey is None or isinstance(cdkey, str) and not cdkey.strip()
                                    else "类型异常"
                                )
                                log_callback(
                                    "[领取-响应] code=0（成功）；"
                                    f"data类型={type(data).__name__}, "
                                    f"extra_info类型={type(extra).__name__}, "
                                    f"cdkey_content={field_state}；已停止重复领取"
                                )
                            return None
                        last_error = format_api_failure(
                            "领取-提交",
                            code,
                            message,
                            detail=f"worker={worker_id}, attempt={attempt}",
                        )
                        error = BiliRewardError(last_error)
                        if should_relogin_from_error(error):
                            raise BiliCookieInvalidError(last_error)
                        # 202032 can precede eligibility refresh at the opening time;
                        # let it follow the normal bounded retry path.
                        if code in (-400, -403, -404, -405):
                            raise error
                        if (
                            stop_sold_out
                            and code != 202032
                            and any(
                                marker in str(message)
                                for marker in (
                                    "库存不足",
                                    "库存已耗尽",
                                    "库存已用完",
                                    "库存已经使用完",
                                    "每日库存已达上限",
                                    "奖励已领完",
                                    "奖品已领完",
                                    "奖励已抢完",
                                    "奖品已抢完",
                                )
                            )
                        ):
                            raise BiliRewardError(f"[库存] 停止该链接: {last_error}")
                        network_failures = 0
                    except BiliRequestError as exc:
                        network_failures += 1
                        delay = max(
                            retry_interval, min(8.0, 2 ** min(network_failures - 1, 3))
                        ) + random.uniform(0, 0.2)
                        last_error = str(exc)
                    if log_callback is not None:
                        log_callback(last_error)
                    if budget.has_remaining(worker_id):
                        if jitter_ms:
                            delay += random.uniform(0, jitter_ms / 1000)
                        until = time.monotonic() + delay
                        while time.monotonic() < until:
                            check()
                            stop.wait(min(0.05, max(0.0, until - time.monotonic())))
                raise _RetryExhausted(f"领取超过最大重试次数；最后错误: {last_error}")
            except (_ClaimStopped, _RetryExhausted):
                raise
            except BiliRewardError:
                stop.set()
                raise
            finally:
                self._local.check = None
                self._close_thread_session()

        if thread_count == 1:
            # Reuse the preparing thread and its warm Session in serial mode.
            try:
                return award_info, worker(1)
            except BiliRewardError:
                raise
            except Exception as exc:
                raise BiliRewardError(f"领取异常: {type(exc).__name__}") from exc

        successes: list[str | None] = []
        failures: list[BiliRewardError] = []
        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(worker, i + 1) for i in range(thread_count)]
            try:
                for future in as_completed(futures):
                    try:
                        successes.append(future.result())
                    except _ClaimStopped:
                        pass
                    except BiliRewardError as exc:
                        failures.append(exc)
                    except Exception as exc:
                        stop.set()
                        failures.append(
                            BiliRewardError(f"领取异常: {type(exc).__name__}")
                        )
            finally:
                stop.set()
        # Drain in-flight responses: an earlier error must not discard a granted reward.
        if successes:
            return award_info, next((key for key in successes if key), None)
        for kind in (
            BiliRewardCancelledError,
            BiliRateLimitError,
            BiliCookieInvalidError,
        ):
            for error in failures:
                if isinstance(error, kind):
                    raise error
        control.check_cancelled()
        raise (
            failures[-1] if failures else BiliRewardError("领取失败，所有线程都未成功")
        )
