from __future__ import annotations

import json
import os
import sys
import time
import tempfile
from typing import Optional

from .errors import BiliRewardError
from .models import RuntimePaths
from .accounts import DEFAULT_ACCOUNT, validate_account_id


def get_runtime_paths(
    base_dir: Optional[str] = None, account_id: str = DEFAULT_ACCOUNT
) -> RuntimePaths:
    validate_account_id(account_id)
    if base_dir is not None:
        root_dir = base_dir
    elif getattr(sys, "frozen", False):
        root_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(root_dir, "data")
    if account_id != DEFAULT_ACCOUNT:
        data_dir = os.path.join(data_dir, "accounts", account_id)
    logs_dir = os.path.join(data_dir, "logs")
    return RuntimePaths(
        data_dir=data_dir,
        logs_dir=logs_dir,
        cookie_file=os.path.join(data_dir, "bilibili_cookie.json"),
        log_file=os.path.join(logs_dir, "claim_live_reward.log"),
    )


def ensure_runtime_dirs(paths: RuntimePaths) -> None:
    os.makedirs(paths.data_dir, exist_ok=True)
    os.makedirs(paths.logs_dir, exist_ok=True)


def validate_cookies_dict(cookies: dict[str, str]) -> None:
    required_keys = ("SESSDATA", "bili_jct")
    missing_keys = [key for key in required_keys if not cookies.get(key)]
    if missing_keys:
        raise BiliRewardError(f"[Cookie] 缺少关键字段: {', '.join(missing_keys)}")


def save_cookie_file(cookie_file: str, cookies: dict[str, str]) -> None:
    validate_cookies_dict(cookies)
    payload = {
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cookies": cookies,
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=os.path.dirname(os.path.abspath(cookie_file)),
            delete=False,
        ) as file:
            temporary = file.name
            json.dump(payload, file, ensure_ascii=False, indent=2)
        os.replace(temporary, cookie_file)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.remove(temporary)


def load_cookie_file(cookie_file: str) -> Optional[dict[str, str]]:
    if not os.path.exists(cookie_file):
        return None

    try:
        with open(cookie_file, "r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, ValueError, UnicodeError):
        return None

    if not isinstance(payload, dict):
        return None
    cookies = payload.get("cookies")
    if not isinstance(cookies, dict):
        return None

    normalized: dict[str, str] = {}
    for key, value in cookies.items():
        if isinstance(key, str) and isinstance(value, str):
            normalized[key] = value

    try:
        validate_cookies_dict(normalized)
    except BiliRewardError:
        return None
    return normalized
