import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from PyQt5.QtGui import QWheelEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QScrollArea

from core.models import BatchClaimResult, SingleLinkClaimResult
from core.service import RewardService
from ui.main_window import MainWindow
from ui.settings_panel import SettingsPanel


class LogAndWheelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wheel(self, widget):
        pos = widget.rect().center()
        event = QWheelEvent(QPointF(pos), QPointF(widget.mapToGlobal(pos)),
                            QPoint(), QPoint(0, -120), Qt.NoButton, Qt.NoModifier,
                            Qt.NoScrollPhase, False)
        QApplication.sendEvent(widget, event)
        self.app.processEvents()

    def test_hover_scroll_preserves_numeric_time_and_combo_values(self):
        with tempfile.TemporaryDirectory() as root:
            panel = SettingsPanel(QSettings(root + "/ui.ini", QSettings.IniFormat))
            try:
                panel.mode.setCurrentIndex(panel.mode.findData("scheduled"))
                panel.after_schedule.setCurrentIndex(panel.after_schedule.findData("duration"))
                panel.resize(400, 500)
                panel.show()
                self.app.processEvents()
                scroll = panel.findChild(QScrollArea)
                cases = [(panel.interval_ms, panel.interval_ms.value),
                         (panel.duration, panel.duration.value),
                         (panel.schedule_time, panel.schedule_time.time),
                         (panel.mode, panel.mode.currentIndex)]
                for control, getter in cases:
                    with self.subTest(control=type(control).__name__):
                        scroll.verticalScrollBar().setValue(0)
                        control.setFocus(Qt.TabFocusReason)
                        before = getter()
                        self.wheel(control)
                        self.assertEqual(getter(), before)
                        self.assertGreater(scroll.verticalScrollBar().value(), 0)
            finally:
                panel.close()

    def test_clicked_editor_wheels_until_leave_or_focus_loss(self):
        with tempfile.TemporaryDirectory() as root:
            panel = SettingsPanel(QSettings(root + "/ui.ini", QSettings.IniFormat))
            try:
                panel.resize(400, 500)
                panel.show()
                self.app.processEvents()
                box = panel.interval_ms
                scroll = panel.findChild(QScrollArea)
                QTest.mouseClick(box.lineEdit(), Qt.LeftButton)
                self.wheel(box)
                self.assertEqual(box.value(), 900)
                self.assertEqual(scroll.verticalScrollBar().value(), 0)
                QApplication.sendEvent(box, QEvent(QEvent.Leave))
                self.wheel(box)
                self.assertEqual(box.value(), 900)
                self.assertGreater(scroll.verticalScrollBar().value(), 0)
                scroll.verticalScrollBar().setValue(0)
                QTest.mouseClick(box.lineEdit(), Qt.LeftButton)
                panel.mode.setFocus(Qt.TabFocusReason)
                self.wheel(box)
                self.assertEqual(box.value(), 900)
                self.assertGreater(scroll.verticalScrollBar().value(), 0)
            finally:
                panel.close()

    def test_log_outcomes_keep_colors_after_filter_rebuild_and_popup(self):
        cases = [("[链接1] [任务] 领取成功，兑换码=TEST😀", "#008000"),
                 ("[链接1] [任务] 执行失败: 到达时长限制", "#cc0000"),
                 ("[链接1] [领取-提交] 失败: code=-702, message=请求频率过高", "#9a741f"),
                 ("[链接1] [重试-等待] 原因=失败", "#000000"),
                 ("[批次] 每条任务独立处理失败", "#000000"),
                 ("[链接1] [任务] 取消: 已停止", "#000000")]
        with tempfile.TemporaryDirectory() as root:
            window = MainWindow(RewardService(root))
            try:
                for line, _ in cases:
                    window.append_ui_log(line)
                before = window.log_text.toPlainText()
                window._render_logs()
                window.log_highlighter.rehighlight()
                self.assertEqual(window.log_text.toPlainText(), before)
                block = window.log_text.document().firstBlock()
                for line, color in cases:
                    self.assertTrue(block.text().endswith(line))
                    formats = block.layout().formats()
                    self.assertEqual(len(formats), 1)
                    self.assertEqual(formats[0].format.foreground().color().name(), color)
                    self.assertEqual(formats[0].length, len(block.text().encode("utf-16-le")) // 2)
                    block = block.next()
                result = BatchClaimResult([
                    SingleLinkClaimResult(0, "a", "success"),
                    SingleLinkClaimResult(1, "b", "failed"),
                    SingleLinkClaimResult(2, "c", "cancelled"),
                ])
                with patch("ui.main_window.QMessageBox.information") as popup:
                    window.on_batch_completed(result)
                html = popup.call_args.args[2]
                self.assertIn('color: #008000">成功 1 条', html)
                self.assertIn('color: #CC0000">失败 1 条', html)
                self.assertIn('color: #000000">取消 1 条', html)
            finally:
                window.close()
