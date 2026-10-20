import json
import tempfile
import unittest
from pathlib import Path

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
