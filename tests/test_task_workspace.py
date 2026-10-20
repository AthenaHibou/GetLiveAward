import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest

from core.errors import BiliRewardCancelledError
from core.models import AwardInfo, LinkTaskItem, PreparedLinkTask, RunSettings
from core.network import RequestLimiter
from core.service import RewardService
from core.task_config import SavedTask, TaskDocument, load_document, save_document
from ui.main_window import MainWindow
from ui.worker import ClaimWorker


class TaskConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "tasks.json")

    def test_roundtrip_restores_individual_options_and_templates(self):
        settings = RunSettings(interval_ms=700, retry_mode="scheduled", start_delay_ms=150)
        document = TaskDocument([SavedTask("零点", "https://x/?task_id=a", False, settings)],
                                {"零点模板": settings}, RunSettings(global_interval_ms=800))
        save_document(self.path, document)
        restored = load_document(self.path)
        self.assertEqual(restored.tasks[0].settings, settings)
        self.assertFalse(restored.tasks[0].enabled)
        self.assertEqual(restored.templates["零点模板"], settings)
        self.assertEqual(restored.global_settings.global_interval_ms, 800)
        for secret in ("SESSDATA", "bili_jct", "cookie", "identities"):
            self.assertNotIn(secret, Path(self.path).read_text(encoding="utf-8"))

    def test_invalid_or_credential_fields_are_rejected_before_replacement(self):
        original = TaskDocument([SavedTask("original")])
        save_document(self.path, original)
        for field, value in (("interval_ms", "100"), ("max_retry", 0),
                             ("jitter_enabled", 1), ("SESSDATA", "secret")):
            payload = original.payload()
            payload["tasks"][0]["settings"][field] = value
            with self.assertRaises(ValueError):
                TaskDocument.parse(payload)
        invalid = TaskDocument([SavedTask("invalid", settings=RunSettings())] * 2)
        with self.assertRaises(ValueError):
            save_document(self.path, invalid)
        self.assertEqual(load_document(self.path).tasks[0].name, "original")


class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = RewardService(self.temp.name)
        self.window = MainWindow(self.service)
        self.addCleanup(self.window.close)

    def fill(self, row, name, task_id):
        row.name_edit.setText(name)
        row.input.setText(f"https://www.bilibili.com/?task_id={task_id}")

    def test_task_switch_restores_independent_options_and_shared_global_limit(self):
        window = self.window
        first = window.link_rows[0]
        self.fill(first, "任务一", "a")
        window.settings_panel.apply(RunSettings(interval_ms=700, max_retry=11, global_interval_ms=800))
        window.add_link_row()
        second = window.link_rows[1]
        self.fill(second, "任务二", "b")
        window.settings_panel.apply(RunSettings(interval_ms=1500, max_retry=22, global_interval_ms=900))
        window.select_task(first)
        self.assertEqual(window.settings_panel.snapshot().interval_ms, 700)
        self.assertEqual(window.settings_panel.snapshot().max_retry, 11)
        self.assertEqual(window.settings_panel.snapshot().global_interval_ms, 900)
        self.assertTrue(window._autosave_workspace())
        window.close()
        reopened = MainWindow(RewardService(self.temp.name))
        self.addCleanup(reopened.close)
        self.assertEqual([row.config.name for row in reopened.link_rows], ["任务一", "任务二"])
        self.assertEqual([task.settings.max_retry for task in reopened._collect_link_tasks()], [11, 22])
        self.assertIsNone(reopened.worker_thread)

    def test_single_run_ignores_other_invalid_tasks_and_does_not_enable_parallelism(self):
        window = self.window
        first = window.link_rows[0]
        first.input.setText("invalid")
        window.add_link_row()
        second = window.link_rows[1]
        self.fill(second, "单跑", "b")
        second.enabled.setChecked(False)
        window.settings_panel.max_retry.setValue(3)
        with patch.object(window, "_start_worker") as start:
            window.start_claim(only_row=second)
        worker = start.call_args.args[0]
        self.assertEqual([task.index for task in worker.link_tasks], [1])
        self.assertEqual(worker.link_tasks[0].settings.max_retry, 3)
        self.assertEqual(worker.settings.active_links, 1)
        self.assertFalse(first.run_btn.isEnabled())
        window._set_running(False)

    def test_all_start_filters_unchecked_tasks(self):
        first = self.window.link_rows[0]
        self.fill(first, "一", "a")
        self.window.add_link_row(text="invalid")
        self.window.link_rows[1].enabled.setChecked(False)
        self.assertEqual([task.task_id for task in self.window._collect_link_tasks()], ["a"])

    def test_task_summary_shows_effective_interval_and_scheduled_help_covers_single_run(self):
        window = self.window
        row = window.link_rows[0]
        window.settings_panel.apply(RunSettings(interval_ms=1000, global_interval_ms=1200))
        self.assertIn("1200 ms", row.summary.text())
        self.assertIn("任务 1000", row.summary.text())
        self.assertIn("实际最小请求间隔 1200 ms", row.summary.toolTip())
        self.assertIn("单条运行", window.settings_panel.schedule_note.text())
        self.assertIn("全部开始", window.settings_panel.schedule_note.text())

    def test_template_is_copied_without_url_or_global_settings(self):
        window = self.window
        self.fill(window.link_rows[0], "一", "a")
        window.settings_panel.apply(RunSettings(max_retry=9, concurrent=True, request_workers=3))
        window.save_template_named("九次")
        window.add_link_row(text="https://www.bilibili.com/?task_id=b")
        window.settings_panel.apply(RunSettings(max_retry=20, global_interval_ms=1500))
        window.template_combo.showPopup()
        self.app.processEvents()
        view = window.template_combo.view()
        item_rect = view.visualRect(view.model().index(1, 0))
        QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=item_rect.center())
        self.app.processEvents()
        current = window.settings_panel.snapshot()
        self.assertEqual(current.max_retry, 9)
        self.assertFalse(current.concurrent)
        self.assertEqual(current.global_interval_ms, 1500)
        self.assertIn("task_id=b", window.link_rows[1].input.text())
        self.assertEqual(window.template_combo.currentIndex(), 0)
        self.assertTrue(any("已套用「九次」" in line for _, line in window._log_history))
        window.settings_panel.max_retry.setValue(15)
        self.assertEqual(window._templates["九次"].max_retry, 9)

    def test_account_switch_has_independent_task_documents(self):
        window = self.window
        self.fill(window.link_rows[0], "原账号任务", "a")
        with patch.object(window, "start_login"):
            window.add_account_named("另一个账号")
        self.assertEqual(window.link_rows[0].input.text(), "")
        self.fill(window.link_rows[0], "新账号任务", "b")
        window.account_combo.setCurrentIndex(window.account_combo.findData("default"))
        self.assertEqual(window.link_rows[0].config.name, "原账号任务")
        self.assertIn("task_id=a", window.link_rows[0].input.text())

    def test_corrupt_config_is_preserved_and_bad_import_does_not_clear_tasks(self):
        path = self.window._workspace_path()
        Path(path).write_text("bad-json", encoding="utf-8")
        self.window._load_workspace()
        self.assertFalse(self.window._autosave_workspace())
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "bad-json")
        imported = str(Path(self.temp.name) / "invalid.json")
        Path(imported).write_text(json.dumps({"version": 99}), encoding="utf-8")
        original = self.window.link_rows[0]
        with self.assertRaises(ValueError):
            self.window.import_tasks_from(imported)
        self.assertIs(self.window.link_rows[0], original)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "bad-json")

    def test_log_filter_retains_history_for_other_tasks(self):
        window = self.window
        window.add_link_row()
        window.append_log("[链接1] FIRST")
        window.append_log("[链接2] SECOND")
        window.append_log("[批次] GLOBAL")
        window.log_filter.setCurrentIndex(1)
        self.assertNotIn("FIRST", window.log_text.toPlainText())
        self.assertIn("SECOND", window.log_text.toPlainText())
        self.assertIn("GLOBAL", window.log_text.toPlainText())
        window.select_task(window.link_rows[0])
        self.assertIn("FIRST", window.log_text.toPlainText())
        self.assertNotIn("SECOND", window.log_text.toPlainText())
        window.log_filter.setCurrentIndex(0)
        self.assertIn("SECOND", window.log_text.toPlainText())

    def test_valid_import_restores_tasks_without_starting_login_or_claim(self):
        imported = str(Path(self.temp.name) / "import.json")
        document = TaskDocument([SavedTask("导入任务", "https://x/?task_id=a", False,
                                          RunSettings(max_retry=17))])
        save_document(imported, document)
        with (patch.object(self.window, "start_login") as login,
              patch.object(self.window, "_start_worker") as worker):
            self.window.import_tasks_from(imported)
        self.assertEqual(self.window.link_rows[0].config.name, "导入任务")
        self.assertEqual(self.window.settings_panel.snapshot().max_retry, 17)
        self.assertFalse(self.window.link_rows[0].enabled.isChecked())
        login.assert_not_called()
        worker.assert_not_called()


class TaskSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = RewardService(self.temp.name)
        self.addCleanup(self.service.close)

    def test_task_options_and_global_serial_limit_reach_service(self):
        tasks = [LinkTaskItem(0, "", "a", RunSettings(max_retry=3, interval_ms=700)),
                 LinkTaskItem(1, "", "b", RunSettings(max_retry=7, interval_ms=1500, concurrent=True))]
        worker = ClaimWorker(self.service, tasks, RunSettings(global_interval_ms=900))
        prepared = []
        def prepare(task, **kwargs):
            prepared.append((task.index, kwargs["options"]))
            self.assertEqual(self.service._limiter.interval, 0.9)
            return PreparedLinkTask(task.index, "", task.task_id, kwargs["options"], Mock(),
                                    AwardInfo("a", "a", "t", "r"), Mock())
        with (patch.object(self.service, "prepare_link_task", side_effect=prepare),
              patch.object(self.service, "claim_prepared_task", return_value=SimpleNamespace(cdkey="TEST"))):
            worker.run()
        self.assertEqual([options.max_retry for _, options in prepared], [3, 7])
        self.assertEqual([options.retry_interval for _, options in prepared], [0.7, 1.5])
        self.assertEqual([options.thread_count for _, options in prepared], [1, 1])

    def test_late_queued_schedule_keeps_batch_target_instead_of_next_day(self):
        scheduled = RunSettings(retry_mode="scheduled", scheduled_time="01:00:00")
        tasks = [LinkTaskItem(0, "", "later", scheduled),
                 LinkTaskItem(1, "", "now", RunSettings())]
        worker = ClaimWorker(self.service, tasks)
        order, targets = [], []
        def prepare(task, **kwargs):
            order.append(task.index)
            return PreparedLinkTask(task.index, "", task.task_id, kwargs["options"], Mock(),
                                    AwardInfo("a", "a", "t", "r"), Mock())
        with (patch.object(worker, "_sync_clock"),
              patch("ui.worker.next_scheduled_time", return_value=12345) as next_time,
              patch.object(worker, "_wait_scheduled", side_effect=lambda target, *args: targets.append(target)),
              patch.object(self.service, "prepare_link_task", side_effect=prepare),
              patch.object(self.service, "claim_prepared_task", return_value=SimpleNamespace(cdkey="TEST"))):
            # Fix the batch clock so scheduled time is after the immediate task.
            with patch.object(self.service.clock, "now", return_value=12300):
                worker._run_task_queue()
        self.assertEqual(order, [1, 0])
        self.assertEqual(targets, [12345])
        next_time.assert_called_once()

    def test_cancelling_one_scheduled_wait_does_not_wait_for_other_queue_items(self):
        tasks = [LinkTaskItem(i, "", str(i), RunSettings()) for i in range(2)]
        worker = ClaimWorker(self.service, tasks)
        worker.cancel_claim(0)
        with self.assertRaises(BiliRewardCancelledError):
            worker._wait_scheduled(time.time() + 100, {0: Mock()})

    def test_global_limiter_respects_both_adjacent_task_intervals(self):
        limiter = RequestLimiter(0.1)
        now = [10.0]
        with (patch("core.network.time.monotonic", side_effect=lambda: now[0]),
              patch("core.network.time.sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay))):
            limiter.wait(interval=0.7)
            limiter.wait(interval=0.2)
            self.assertAlmostEqual(now[0], 10.7)
            limiter.wait(interval=1.5)
            self.assertAlmostEqual(now[0], 12.2)
