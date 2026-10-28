import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.accounts import AccountStore
from core.errors import BiliRewardError
from core.service import RewardService
from core.storage import get_runtime_paths, load_cookie_file, save_cookie_file


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = get_runtime_paths(self.temp.name)
        self.store = AccountStore(self.paths.data_dir)

    def test_default_paths_and_saved_cookies_are_preserved(self):
        default = RewardService(self.temp.name)
        self.addCleanup(default.close)
        save_cookie_file(
            default.paths.cookie_file, {"SESSDATA": "original", "bili_jct": "original"}
        )
        added = self.store.add("账号二")
        second = RewardService(self.temp.name, account_id=added.account_id)
        self.addCleanup(second.close)
        self.assertNotEqual(default.paths.cookie_file, second.paths.cookie_file)
        self.assertNotEqual(default.paths.log_file, second.paths.log_file)
        self.assertIsNone(load_cookie_file(second.paths.cookie_file))
        save_cookie_file(
            second.paths.cookie_file, {"SESSDATA": "second", "bili_jct": "second"}
        )
        self.assertEqual(
            load_cookie_file(default.paths.cookie_file)["SESSDATA"], "original"
        )
        self.assertEqual(
            load_cookie_file(second.paths.cookie_file)["SESSDATA"], "second"
        )
        self.assertNotIn("SESSDATA", Path(self.store.path).read_text(encoding="utf-8"))
        self.assertEqual(
            [p.name for p in self.store.list_profiles()], ["默认账号", "账号二"]
        )

    def test_profiles_are_persistent_and_have_unique_ids(self):
        first, second = self.store.add("一"), self.store.add("二")
        self.assertNotEqual(first.account_id, second.account_id)
        self.assertEqual(
            AccountStore(self.paths.data_dir).list_profiles()[1:], [first, second]
        )

    def test_login_registers_default_identity_and_preserves_legacy_paths(self):
        self.store.bind_login("default", {"mid": 123, "uname": "测试昵称"})
        self.assertEqual(self.store.list_profiles()[0].name, "测试昵称 · 123")
        self.store.bind_login("default", {"mid": 123, "uname": "更新昵称"})
        self.assertEqual(len(self.store.list_profiles()), 1)
        self.assertEqual(self.store.list_profiles()[0].name, "更新昵称 · 123")
        self.store.add("账号二")
        saved = json.loads(Path(self.store.path).read_text(encoding="utf-8"))
        self.assertEqual(saved["identities"]["default"]["uid"], "123")
        self.assertEqual(self.store.list_profiles()[0].name, "更新昵称 · 123")
        self.assertEqual(get_runtime_paths(self.temp.name).cookie_file, self.paths.cookie_file)

    def test_unverified_identity_does_not_create_registry(self):
        for identity in ({}, {"mid": 0}, {"mid": "invalid"}):
            self.store.bind_login("default", identity)
        self.assertFalse(Path(self.store.path).exists())

    def test_bad_profile_id_cannot_escape_data_directory(self):
        for account_id in ("../other", "C:/other", "", "default/../other", "z" * 32):
            with self.assertRaises(ValueError):
                get_runtime_paths(self.temp.name, account_id)

    def test_duplicate_or_invalid_names_are_rejected(self):
        self.store.add("Test")
        for name in ("test", "默认账号", " ", "a" * 25, "a\nb"):
            with self.assertRaises(ValueError):
                self.store.add(name)

    def test_corrupt_registry_is_not_overwritten(self):
        Path(self.paths.data_dir).mkdir(parents=True)
        path = Path(self.store.path)
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(BiliRewardError):
            self.store.add("new")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [])

    def test_legacy_remark_and_unknown_metadata_survive_identity_refresh(self):
        legacy = self.store.add("旧账号备注")
        path = Path(self.store.path)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["extension"] = {"keep": True}
        path.write_text(json.dumps(data), encoding="utf-8")
        self.store.bind_login(legacy.account_id, {"mid": 123, "uname": "名" * 24})
        self.store.add("再加一个")
        profile = self.store.list_profiles()[1]
        self.assertEqual(profile.name, "名" * 24 + " · 123")
        self.assertEqual(profile.remark, "旧账号备注")
        persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["accounts"][0]["name"], "旧账号备注")
        self.assertEqual(persisted["extension"], {"keep": True})
        self.store.bind_login(legacy.account_id, {"mid": 123, "uname": "改名后"})
        self.assertEqual(self.store.list_profiles()[1].name, "改名后 · 123")

    def test_new_scans_reuse_default_then_create_and_deduplicate_by_uid(self):
        cookies = {"SESSDATA": "new", "bili_jct": "new"}
        first = self.store.save_login({"mid": 123, "uname": "同名"}, cookies, preferred_id="default")
        self.assertEqual(first.account_id, "default")
        second = self.store.save_login({"mid": 456, "uname": "同名"}, cookies, preferred_id="default")
        self.assertNotEqual(second.account_id, "default")
        self.assertEqual(second.remark, "")
        repeated = self.store.save_login({"mid": "456", "uname": "新昵称"}, cookies, preferred_id="default")
        self.assertEqual(repeated.account_id, second.account_id)
        self.assertEqual(repeated.name, "新昵称 · 456")
        self.assertEqual(len(self.store.list_profiles()), 2)

    def test_legacy_cookie_uid_reuses_profile_and_preserves_workspace(self):
        legacy = self.store.add("原备注")
        service = RewardService(self.temp.name, account_id=legacy.account_id)
        self.addCleanup(service.close)
        save_cookie_file(service.paths.cookie_file, {"SESSDATA": "old", "bili_jct": "old", "DedeUserID": "123"})
        workspace = Path(service.paths.data_dir) / "tasks.json"
        workspace.write_text("existing workspace", encoding="utf-8")
        result = self.store.save_login({"mid": 123, "uname": "真实昵称"},
                                      {"SESSDATA": "new", "bili_jct": "new"}, preferred_id="default")
        self.assertEqual(result.account_id, legacy.account_id)
        self.assertEqual(result.remark, "原备注")
        self.assertEqual(workspace.read_text(encoding="utf-8"), "existing workspace")
        self.assertEqual(load_cookie_file(service.paths.cookie_file)["SESSDATA"], "new")

    def test_original_default_cookie_without_registry_is_reused(self):
        service = RewardService(self.temp.name)
        self.addCleanup(service.close)
        save_cookie_file(service.paths.cookie_file, {"SESSDATA": "old", "bili_jct": "old", "DedeUserID": "123"})
        self.assertFalse(Path(self.store.path).exists())
        result = self.store.save_login({"mid": 123, "uname": "原账号"},
                                      {"SESSDATA": "new", "bili_jct": "new"}, preferred_id="default")
        self.assertEqual(result.account_id, "default")
        self.assertEqual(len(self.store.list_profiles()), 1)

    def test_unidentified_legacy_cookie_is_never_overwritten_by_guessing(self):
        service = RewardService(self.temp.name)
        self.addCleanup(service.close)
        save_cookie_file(service.paths.cookie_file, {"SESSDATA": "old", "bili_jct": "old"})
        result = self.store.save_login({"mid": 123, "uname": "账号"},
                                      {"SESSDATA": "new", "bili_jct": "new"}, preferred_id="default")
        self.assertNotEqual(result.account_id, "default")
        self.assertEqual(load_cookie_file(service.paths.cookie_file)["SESSDATA"], "old")

    def test_historical_duplicate_profiles_are_preserved_and_prefer_current(self):
        second = self.store.add("历史副本")
        for account_id in ("default", second.account_id):
            self.store.bind_login(account_id, {"mid": 123, "uname": "同一个账号"})
        result = self.store.save_login({"mid": 123, "uname": "同一个账号"},
                                      {"SESSDATA": "new", "bili_jct": "new"}, preferred_id=second.account_id)
        self.assertEqual(result.account_id, second.account_id)
        self.assertEqual(len(self.store.list_profiles()), 2)

    def test_failed_registry_write_restores_original_cookie_bytes(self):
        cookies = {"SESSDATA": "old", "bili_jct": "old"}
        self.store.save_login({"mid": 123, "uname": "原名"}, cookies, preferred_id="default")
        cookie_path = Path(self.paths.cookie_file)
        old_cookie = cookie_path.read_bytes()
        old_registry = Path(self.store.path).read_bytes()
        with patch.object(self.store, "_write", side_effect=OSError("cannot write")):
            with self.assertRaises(OSError):
                self.store.save_login({"mid": 123, "uname": "新名"},
                                      {"SESSDATA": "new", "bili_jct": "new"}, preferred_id="default")
        self.assertEqual(cookie_path.read_bytes(), old_cookie)
        self.assertEqual(Path(self.store.path).read_bytes(), old_registry)
        self.assertEqual(sorted(p.name for p in Path(self.paths.data_dir).iterdir()),
                         ["accounts.json", "bilibili_cookie.json"])

    def test_failed_first_save_does_not_leave_a_cookie_or_registry(self):
        with patch.object(self.store, "_write", side_effect=OSError("cannot write")):
            with self.assertRaises(OSError):
                self.store.save_login({"mid": 123, "uname": "名字"},
                                      {"SESSDATA": "new", "bili_jct": "new"}, preferred_id="default")
        self.assertFalse(Path(self.paths.cookie_file).exists())
        self.assertFalse(Path(self.store.path).exists())

    def test_wrong_identity_cannot_rebind_existing_profile(self):
        self.store.bind_login("default", {"mid": 123, "uname": "原名"})
        old_registry = Path(self.store.path).read_bytes()
        with self.assertRaisesRegex(BiliRewardError, "不一致"):
            self.store.bind_login("default", {"mid": 456, "uname": "其他账号"})
        self.assertEqual(Path(self.store.path).read_bytes(), old_registry)
