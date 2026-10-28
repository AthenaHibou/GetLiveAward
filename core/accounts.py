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
    remark: str = ""


class AccountStore:
    """Public account metadata lives here; cookies stay in isolated profiles."""

    def __init__(self, data_dir: str) -> None:
        self.path = os.path.join(data_dir, "accounts.json")
        self.data_dir = data_dir

    def _read(self) -> dict:
        if not os.path.exists(self.path):
            return {"version": 1, "accounts": []}
        try:
            with open(self.path, encoding="utf-8") as stream:
                data = json.load(stream)
            if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
                raise ValueError("格式无效")
            default_name = data.get("default_name", "默认账号")
            if not isinstance(default_name, str) or not default_name.strip():
                raise ValueError("默认账号名称无效")
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
                seen.add(account_id)
            identities = data.get("identities", {})
            if not isinstance(identities, dict):
                raise ValueError("账号身份记录格式无效")
            for identity in identities.values():
                if (not isinstance(identity, dict)
                        or not self.normalize_uid(identity.get("uid"))
                        or not isinstance(identity.get("nickname"), str)):
                    raise ValueError("账号身份记录格式无效")
        except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise BiliRewardError(
                "账号列表读取失败；请检查 data/accounts.json，原数据未改动"
            ) from exc
        return data

    @staticmethod
    def normalize_uid(value) -> str | None:
        value = str(value)
        if value.isascii() and value.isdecimal() and 0 < len(value) <= 20 and int(value) > 0:
            return str(int(value))
        return None

    @classmethod
    def _identity(cls, identity: dict) -> dict:
        uid = cls.normalize_uid(identity.get("mid"))
        if uid is None:
            raise ValueError("登录验证未返回有效 UID，请重新扫码")
        nickname = "".join(ch for ch in str(identity.get("uname") or "") if ord(ch) >= 32).strip()[:24]
        return {"uid": uid, "nickname": nickname or "B站账号"}

    @staticmethod
    def _profiles(data: dict) -> list[AccountProfile]:
        entries = [{"id": DEFAULT_ACCOUNT, "name": data.get("default_name", "默认账号"), "auto_name": True}]
        entries += data["accounts"]
        profiles = []
        for entry in entries:
            identity = data.get("identities", {}).get(entry["id"])
            name = entry["name"].strip()
            remark = "" if entry.get("auto_name") else name
            if identity:
                name = f"{identity['nickname']} · {identity['uid']}"
            profiles.append(AccountProfile(entry["id"], name, remark))
        return profiles

    def list_profiles(self) -> list[AccountProfile]:
        return self._profiles(self._read())

    def _cookie_file(self, account_id: str) -> str:
        validate_account_id(account_id)
        directory = self.data_dir if account_id == DEFAULT_ACCOUNT else os.path.join(self.data_dir, "accounts", account_id)
        return os.path.join(directory, "bilibili_cookie.json")

    def _uid(self, data: dict, account_id: str) -> str | None:
        # Old installations may have cookies but no identity registry yet.
        from .storage import load_cookie_file

        identity = data.get("identities", {}).get(account_id)
        if identity:
            return self.normalize_uid(identity["uid"])
        cookies = load_cookie_file(self._cookie_file(account_id)) or {}
        return self.normalize_uid(cookies.get("DedeUserID"))

    def uid_for(self, account_id: str) -> str | None:
        return self._uid(self._read(), account_id)

    def add(self, name: str) -> AccountProfile:
        name = name.strip()
        if not 1 <= len(name) <= 24 or any(ord(ch) < 32 for ch in name):
            raise ValueError("账号备注请输入 1–24 个字符，不含换行")
        payload = self._read()
        profiles = self._profiles(payload)
        if any(name.casefold() in (profile.name.casefold(), profile.remark.casefold()) for profile in profiles):
            raise ValueError("账号备注已存在，请换一个名称")
        added = AccountProfile(uuid.uuid4().hex, name, name)
        payload["accounts"].append({"id": added.account_id, "name": name})
        self._write(payload)
        return added

    def bind_login(self, account_id: str, identity: dict) -> None:
        """Register a verified login using the existing NAV response, without requests."""
        payload = self._read()
        profiles = self._profiles(payload)
        if account_id not in {profile.account_id for profile in profiles}:
            raise ValueError("账号不存在")
        if self.normalize_uid(identity.get("mid")) is None:
            return
        verified = self._identity(identity)
        expected = self._uid(payload, account_id)
        if expected and expected != verified["uid"]:
            raise BiliRewardError("扫码账号与当前档案不一致，请使用原账号重新扫码，或点击「添加账号」")
        self._bind(payload, account_id, verified)
        self._write(payload)

    @staticmethod
    def _bind(payload: dict, account_id: str, identity: dict) -> None:
        payload.setdefault("identities", {})[account_id] = identity
        if account_id == DEFAULT_ACCOUNT:
            payload["default_name"] = f"{identity['nickname']} · {identity['uid']}"

    def save_login(self, identity: dict, cookies: dict[str, str], *, preferred_id: str) -> AccountProfile:
        """Commit a verified scan, reusing a UID's existing profile and its files."""
        from .storage import save_cookie_file

        verified = self._identity(identity)
        payload = self._read()
        profiles = self._profiles(payload)
        matches = [p for p in profiles if self._uid(payload, p.account_id) == verified["uid"]]
        if matches:
            target = next((p for p in matches if p.account_id == preferred_id), matches[0])
            account_id = target.account_id
        elif (len(profiles) == 1 and not self._uid(payload, DEFAULT_ACCOUNT)
              and not os.path.exists(self._cookie_file(DEFAULT_ACCOUNT))):
            account_id = DEFAULT_ACCOUNT
        else:
            account_id = uuid.uuid4().hex
            payload["accounts"].append({"id": account_id, "name": verified["nickname"], "auto_name": True})
        self._bind(payload, account_id, verified)
        cookie_file = self._cookie_file(account_id)
        directory = os.path.dirname(cookie_file)
        os.makedirs(directory, exist_ok=True)
        backup = None
        try:
            if os.path.exists(cookie_file):
                with open(cookie_file, "rb") as original, tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
                    backup = stream.name
                    stream.write(original.read())
            save_cookie_file(cookie_file, cookies)
            try:
                self._write(payload)
            except Exception:
                # A failed registry write must not replace an existing login.
                if backup:
                    restore = backup
                    backup = None
                    # Keep the backup on disk if even rollback fails.
                    os.replace(restore, cookie_file)
                else:
                    os.remove(cookie_file)
                raise
        finally:
            if backup and os.path.exists(backup):
                os.remove(backup)
        return next(p for p in self._profiles(payload) if p.account_id == account_id)

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
