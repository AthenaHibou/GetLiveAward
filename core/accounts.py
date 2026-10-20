from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass

from .errors import BiliRewardError

DEFAULT_ACCOUNT = "default"


def validate_account_id(account_id: str) -> None:
    if account_id != DEFAULT_ACCOUNT and (
        len(account_id) != 32 or any(ch not in "0123456789abcdef" for ch in account_id)
    ):
        raise ValueError("账号标识无效")


@dataclass(frozen=True)
class AccountProfile:
    account_id: str
    name: str


class AccountStore:
    """Public account metadata lives here; cookies stay in isolated profiles."""

    def __init__(self, data_dir: str) -> None:
        self.path = os.path.join(data_dir, "accounts.json")
        self.data_dir = data_dir

    def list_profiles(self) -> list[AccountProfile]:
        profiles = [AccountProfile(DEFAULT_ACCOUNT, "默认账号")]
        if not os.path.exists(self.path):
            return profiles
        try:
            with open(self.path, encoding="utf-8") as stream:
                data = json.load(stream)
            if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
                raise ValueError("格式无效")
            default_name = data.get("default_name", "默认账号")
            if not isinstance(default_name, str) or not default_name.strip():
                raise ValueError("默认账号名称无效")
            profiles[0] = AccountProfile(DEFAULT_ACCOUNT, default_name)
            seen = {DEFAULT_ACCOUNT}
            for entry in data["accounts"]:
                account_id, name = entry["id"], entry["name"]
                validate_account_id(account_id)
                if (
                    account_id in seen
                    or not isinstance(name, str)
                    or not 1 <= len(name.strip()) <= 24
                ):
                    raise ValueError("账号记录无效")
                profiles.append(AccountProfile(account_id, name.strip()))
                seen.add(account_id)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise BiliRewardError(
                "账号列表读取失败；请检查 data/accounts.json，原数据未改动"
            ) from exc
        return profiles

    def add(self, name: str) -> AccountProfile:
        name = name.strip()
        if not 1 <= len(name) <= 24 or any(ord(ch) < 32 for ch in name):
            raise ValueError("账号备注请输入 1–24 个字符，不含换行")
        profiles = self.list_profiles()
        if any(profile.name.casefold() == name.casefold() for profile in profiles):
            raise ValueError("账号备注已存在，请换一个名称")
        added = AccountProfile(uuid.uuid4().hex, name)
        payload = {
            "version": 1,
            "default_name": profiles[0].name,
            "accounts": [
                {"id": item.account_id, "name": item.name}
                for item in profiles[1:] + [added]
            ],
        }
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as stream:
                previous = json.load(stream)
            if "identities" in previous:
                payload["identities"] = previous["identities"]
        self._write(payload)
        return added

    def bind_login(self, account_id: str, identity: dict) -> None:
        """Register a verified login using the existing NAV response, without requests."""
        profiles = self.list_profiles()
        if account_id not in {profile.account_id for profile in profiles}:
            raise ValueError("账号不存在")
        uid = str(identity.get("mid", ""))
        if not uid.isdecimal() or int(uid) <= 0:
            return
        nickname = str(identity.get("uname") or "B站账号").strip()[:24]
        payload = {"version": 1, "accounts": []}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as stream:
                payload = json.load(stream)
        identities = payload.setdefault("identities", {})
        if not isinstance(identities, dict):
            raise ValueError("账号身份记录格式无效")
        identities[account_id] = {"uid": uid, "nickname": nickname}
        if account_id == DEFAULT_ACCOUNT:
            payload["default_name"] = f"{nickname} · {uid}"
        self._write(payload)

    def _write(self, payload: dict) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.data_dir, delete=False
            ) as stream:
                temporary = stream.name
                json.dump(payload, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.remove(temporary)
