import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

from core.models import RunSettings
from core.service import RewardService
from core.task_config import SavedTask, TaskDocument, with_globals
from ui.settings_panel import SettingsPanel


class RiskControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_option_defaults_and_persists_through_ui_and_task_document(self):
        self.assertFalse(RunSettings().ignore_risk_control)
        with tempfile.TemporaryDirectory() as root:
            settings = QSettings(os.path.join(root, "ui.ini"), QSettings.IniFormat)
            panel = SettingsPanel(settings)
            self.addCleanup(panel.close)
            self.assertFalse(panel.ignore_risk_control.isChecked())
            panel.ignore_risk_control.setChecked(True)
            panel.save()
            panel.apply(RunSettings())
            panel.load_preferences(settings)
            self.assertTrue(panel.snapshot().ignore_risk_control)
            doc = TaskDocument.parse(TaskDocument(
                tasks=[SavedTask()], global_settings=panel.snapshot()).payload())
            # Global option also propagates to per-task snapshots and clients.
            effective = with_globals(RunSettings(), doc.global_settings)
            service = RewardService(root)
            try:
                client = service._new_client(effective.claim_options("task"))
                self.assertTrue(client.ignore_risk_control)
            finally:
                service.close()
            panel.apply(RunSettings())
            self.assertFalse(panel.snapshot().ignore_risk_control)
