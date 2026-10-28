import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from core.accounts import AccountStore
from core.client import BiliRewardClient
from core.errors import BiliCookieInvalidError, BiliRewardCancelledError, BiliRewardError
from core.service import RewardService
from core.storage import load_cookie_file, save_cookie_file
from ui.main_window import MainWindow


class AccountLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = RewardService(self.temp.name)
        self.addCleanup(self.service.close)
        self.store = AccountStore(self.service.root_paths.data_dir)
        self.old = {"SESSDATA": "old", "bili_jct": "old", "DedeUserID": "123"}
        self.new = {"SESSDATA": "new", "bili_jct": "new", "DedeUserID": "456"}

    def test_new_scan_verifies_without_replacing_active_cookies_or_identity(self):
        save_cookie_file(self.service.paths.cookie_file, self.old)
        self.service.account_identity = {"mid": 123, "uname": "原账号"}
        with (patch("core.service.login_and_get_cookies", return_value=self.new) as scan,
              patch.object(BiliRewardClient, "ensure_logged_in", return_value={"mid": 456, "uname": "新账号"})):
            cookies, identity = self.service.scan_new_account()
        scan.assert_called_once()
        self.assertEqual(cookies, self.new)
        self.assertEqual(identity["mid"], 456)
        self.assertEqual(load_cookie_file(self.service.paths.cookie_file), self.old)
        self.assertEqual(self.service.account_identity["mid"], 123)
        self.assertFalse(Path(self.store.path).exists())

    def test_relogin_with_wrong_uid_never_saves_cookie_or_runs_task(self):
        save_cookie_file(self.service.paths.cookie_file, self.old)
        before = Path(self.service.paths.cookie_file).read_bytes()
        with (patch("core.service.login_and_get_cookies", return_value=self.new),
              patch.object(BiliRewardClient, "ensure_logged_in", side_effect=[
                  BiliCookieInvalidError("expired"), {"mid": 456, "uname": "扫错账号"}]),
              patch.object(BiliRewardClient, "get_award_info") as task):
            with self.assertRaisesRegex(BiliRewardError, "不一致"):
                self.service.ensure_login()
        task.assert_not_called()
        self.assertEqual(Path(self.service.paths.cookie_file).read_bytes(), before)
        self.assertFalse(self.service.account_identity)
        self.assertIsNone(self.service._cookie_text)

    def test_registered_identity_protects_profile_even_without_cookie_uid(self):
        self.store.bind_login("default", {"mid": 123, "uname": "原账号"})
        with (patch("core.service.login_and_get_cookies", return_value=self.new),
              patch.object(BiliRewardClient, "ensure_logged_in", return_value={"mid": 456})):
            with self.assertRaisesRegex(BiliRewardError, "不一致"):
                self.service.ensure_login()
        self.assertFalse(Path(self.service.paths.cookie_file).exists())

    def test_failed_nav_does_not_persist_scanned_cookies(self):
        with (patch("core.service.login_and_get_cookies", return_value=self.new),
              patch.object(BiliRewardClient, "ensure_logged_in", side_effect=BiliRewardError("network error"))):
            with self.assertRaises(BiliRewardError):
                self.service.ensure_login()
        self.assertFalse(Path(self.service.paths.cookie_file).exists())

    def test_same_uid_relogin_refreshes_cookie_after_verification(self):
        save_cookie_file(self.service.paths.cookie_file, self.old)
        refreshed = {**self.old, "SESSDATA": "refreshed"}
        with (patch("core.service.login_and_get_cookies", return_value=refreshed),
              patch.object(BiliRewardClient, "ensure_logged_in", side_effect=[
                  BiliCookieInvalidError("expired"), {"mid": 123, "uname": "原账号"}])):
            self.service.ensure_login()
        self.assertEqual(load_cookie_file(self.service.paths.cookie_file), refreshed)
        self.assertEqual(self.service.account_identity["mid"], 123)

    def test_new_scan_rejects_missing_uid_and_checks_cancel_after_nav(self):
        for identity in ({}, {"mid": 0}, {"mid": "invalid"}):
            with (patch("core.service.login_and_get_cookies", return_value=self.new),
                  patch.object(BiliRewardClient, "ensure_logged_in", return_value=identity)):
                with self.assertRaisesRegex(BiliRewardError, "UID"):
                    self.service.scan_new_account()
        cancelled = [False]

        def nav(**kwargs):
            cancelled[0] = True
            return {"mid": 456, "uname": "新账号"}

        with (patch("core.service.login_and_get_cookies", return_value=self.new),
              patch.object(BiliRewardClient, "ensure_logged_in", side_effect=nav)):
            with self.assertRaises(BiliRewardCancelledError):
                self.service.scan_new_account(cancel_check=lambda: cancelled[0])
        self.assertFalse(Path(self.service.paths.cookie_file).exists())
        self.assertFalse(Path(self.store.path).exists())


class AccountLoginUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        AccountLoginTests.setUp(self)
        save_cookie_file(self.service.paths.cookie_file, self.old)
        self.window = MainWindow(self.service)
        self.addCleanup(self.window.close)

    def finish_worker(self):
        deadline = time.monotonic() + 3
        while self.window.worker_thread is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertIsNone(self.window.worker_thread)

    def test_click_add_scans_and_only_switches_after_verification(self):
        self.window.link_rows[0].input.setText("https://www.bilibili.com/?task_id=original")

        def scan(**kwargs):
            self.assertEqual(self.window.service.account_id, "default")
            self.assertEqual(len(self.store.list_profiles()), 1)
            return self.new, {"mid": 456, "uname": "新账号"}

        with (patch.object(self.service, "scan_new_account", side_effect=scan) as login,
              patch.object(self.service, "prepare_link_task") as claim):
            self.window.add_account_btn.click()
            self.finish_worker()
        login.assert_called_once()
        claim.assert_not_called()
        self.assertEqual(self.window.account_combo.currentText(), "新账号 · 456")
        self.assertTrue(self.window._login_verified)
        new_id = self.window.service.account_id
        self.assertNotEqual(new_id, "default")
        self.assertEqual(load_cookie_file(self.service.paths.cookie_file), self.old)
        self.assertEqual(load_cookie_file(self.window.service.paths.cookie_file), self.new)
        self.assertEqual(self.window.link_rows[0].input.text(), "")
        self.window._activate_account("default")
        self.assertIn("task_id=original", self.window.link_rows[0].input.text())
        self.window._activate_account(new_id)
        self.window.close()
        reopened = MainWindow(RewardService(self.temp.name))
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.service.account_id, new_id)
        self.assertEqual(reopened.account_combo.currentText(), "新账号 · 456")

    def test_repeat_scan_reuses_legacy_account_and_preserves_remark_and_tasks(self):
        legacy = self.store.add("原备注")
        self.window._reload_account_profiles()
        self.window._activate_account(legacy.account_id)
        save_cookie_file(self.window.service.paths.cookie_file, self.new)
        self.window.link_rows[0].input.setText("https://www.bilibili.com/?task_id=kept")
        self.window._activate_account("default")
        with patch.object(self.window.service, "scan_new_account", return_value=(self.new, {"mid": 456, "uname": "新昵称"})):
            self.window.add_account()
            self.finish_worker()
        self.assertEqual(self.window.service.account_id, legacy.account_id)
        self.assertEqual(self.window.account_combo.count(), 2)
        self.assertEqual(self.window.account_combo.currentText(), "新昵称 · 456")
        self.assertIn("原备注", self.window.account_combo.currentData(Qt.ToolTipRole))
        self.assertIn("task_id=kept", self.window.link_rows[0].input.text())

    def test_cancel_or_fail_keeps_current_profile_and_verified_state(self):
        self.window._on_login_checked(True, "登录已验证")
        self.window.link_rows[0].input.setText("https://www.bilibili.com/?task_id=kept")
        for error in (BiliRewardCancelledError("cancelled"), BiliRewardError("failed")):
            with patch.object(self.service, "scan_new_account", side_effect=error):
                self.window.add_account()
                self.finish_worker()
            self.assertEqual(self.window.service.account_id, "default")
            self.assertEqual(self.window.account_combo.count(), 1)
            self.assertTrue(self.window._login_verified)
            self.assertEqual(self.window.login_status.text(), "登录已验证")
            self.assertIn("task_id=kept", self.window.link_rows[0].input.text())
            self.assertEqual(load_cookie_file(self.service.paths.cookie_file), self.old)
            self.assertFalse(Path(self.store.path).exists())

    def test_close_during_scan_does_not_add_profile(self):
        def scan(*, cancel_check, **kwargs):
            while not cancel_check():
                time.sleep(0.005)
            return self.new, {"mid": 456, "uname": "不保存"}

        with patch.object(self.service, "scan_new_account", side_effect=scan):
            self.window.add_account()
            self.window.close()
            self.finish_worker()
        self.assertFalse(Path(self.store.path).exists())
        self.assertEqual(load_cookie_file(self.service.paths.cookie_file), self.old)

    def test_registry_failure_keeps_current_workspace_and_account(self):
        self.window.link_rows[0].input.setText("https://www.bilibili.com/?task_id=kept")
        with (patch.object(self.service, "scan_new_account", return_value=(self.new, {"mid": 456, "uname": "新账号"})),
              patch.object(self.window.account_store, "_write", side_effect=OSError("cannot write")),
              patch("ui.main_window.QMessageBox.warning") as warning):
            self.window.add_account()
            self.finish_worker()
        warning.assert_called_once()
        self.assertEqual(self.window.service.account_id, "default")
        self.assertEqual(self.window.account_combo.count(), 1)
        self.assertIn("task_id=kept", self.window.link_rows[0].input.text())
        self.assertEqual(load_cookie_file(self.service.paths.cookie_file), self.old)
        self.assertFalse(Path(self.store.path).exists())

    def test_unsaved_current_workspace_prevents_starting_scan(self):
        with (patch("ui.task_workspace.save_document", side_effect=OSError("cannot write")),
              patch("ui.main_window.QMessageBox.warning"),
              patch.object(self.service, "scan_new_account") as scan):
            self.window.add_account()
        scan.assert_not_called()
        self.assertFalse(self.window._is_running)
        self.assertFalse(Path(self.store.path).exists())
