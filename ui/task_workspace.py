from __future__ import annotations

import os
from collections import deque
from datetime import datetime

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QFileDialog, QHBoxLayout, QInputDialog, QMessageBox, QPushButton

from core.clock_sync import BEIJING, next_scheduled_time
from core.task_config import SavedTask, TaskDocument, load_document, save_document, with_globals
from ui.controls import SoftComboBox


class TaskWorkspaceMixin:
    """Task selection, per-account persistence and credential-free import/export."""

    def _init_workspace(self):
        self._selected_row = None
        self._loading_workspace = True
        self._workspace_error = False
        self._templates = {}
        self._log_history = deque(maxlen=2000)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._autosave_workspace)

    def _build_workspace_tools(self):
        toolbar = QHBoxLayout()
        self.new_task_btn = QPushButton("＋ 添加任务")
        self.new_task_btn.clicked.connect(lambda: self.add_link_row())
        self.save_tasks_btn = QPushButton("保存配置")
        self.save_tasks_btn.clicked.connect(self._manual_save_workspace)
        self.import_tasks_btn = QPushButton("导入")
        self.import_tasks_btn.clicked.connect(self._import_tasks)
        self.export_tasks_btn = QPushButton("导出")
        self.export_tasks_btn.clicked.connect(self._export_tasks)
        for button in (self.new_task_btn, self.save_tasks_btn, self.import_tasks_btn, self.export_tasks_btn):
            toolbar.addWidget(button)
        toolbar.addStretch()
        self.template_combo = SoftComboBox()
        self.template_combo.addItem("套用参数模板…", None)
        self.template_combo.setToolTip("只复制当前任务的参数；不会替换链接或全局设置")
        self.template_combo.currentIndexChanged.connect(self._apply_template)
        self.save_template_btn = QPushButton("存模板")
        self.save_template_btn.setToolTip("将当前任务参数另存为模板，不含链接与全局设置")
        self.save_template_btn.setStyleSheet("padding: 6px 9px;")
        self.save_template_btn.clicked.connect(self._save_template)
        template_tools = QHBoxLayout()
        template_tools.addWidget(self.template_combo, 1)
        template_tools.addWidget(self.save_template_btn)
        self.settings_panel.layout().insertLayout(1, template_tools)
        self.log_filter = SoftComboBox()
        self.log_filter.addItems(["全部日志", "当前任务"])
        self.log_filter.currentIndexChanged.connect(self._render_logs)
        self.settings_panel.changed.connect(self._task_settings_changed)
        return toolbar

    def _workspace_path(self):
        return os.path.join(self.service.paths.data_dir, "tasks.json")

    def _load_workspace(self):
        self._save_timer.stop()
        self._loading_workspace = True
        self._workspace_error = False
        defaults = self.settings_panel.snapshot()
        try:
            document = load_document(self._workspace_path())
        except (OSError, ValueError, UnicodeError) as exc:
            self._workspace_error = True
            document = None
            self.append_ui_log(f"[配置] 读取失败，原文件保留；自动保存已暂停：{exc}")
        if document is None:
            document = TaskDocument([SavedTask(name="任务 1", settings=defaults)], global_settings=defaults)
        self._global_settings = document.global_settings
        self._templates = document.templates
        self._selected_row = None
        for row in self.link_rows:
            self.rows_wrap.removeWidget(row.frame)
            row.frame.deleteLater()
        self.link_rows.clear()
        for task in document.tasks:
            self.add_link_row(config=task)
        self._reload_templates()
        self._loading_workspace = False
        self.select_task(self.link_rows[0], commit=False)

    def _commit_selected_task(self):
        if self._selected_row is not None and not self._loading_workspace:
            settings = self.settings_panel.snapshot()
            self._global_settings = settings
            self._selected_row.config.settings = settings
            self._update_task_summary(self._selected_row)

    def select_task(self, row, *, commit=True, quiet=False):
        if self._loading_workspace or row not in self.link_rows:
            return
        if row is not self._selected_row:
            if commit:
                try:
                    self._commit_selected_task()
                except ValueError as exc:
                    if not quiet:
                        QMessageBox.warning(self, "请先修正当前设置", str(exc))
                    return
            self._selected_row = row
            self._loading_workspace = True
            self.settings_panel.apply(with_globals(row.config.settings, self._global_settings))
            self._loading_workspace = False
        self.settings_panel.title.setText(f"任务设置 · {row.config.name[:10]}")
        self.settings_panel.title.setToolTip(row.config.name)
        for item in self.link_rows:
            item.frame.setProperty("selectedTask", item is row)
            item.frame.style().unpolish(item.frame)
            item.frame.style().polish(item.frame)
        self.rows_wrap.activate()
        self.link_scroll.ensureWidgetVisible(row.frame, 0, 6)
        QTimer.singleShot(0, self._reveal_selected_task)
        self._render_logs()

    def _reveal_selected_task(self):
        if self._selected_row in self.link_rows:
            self.link_scroll.ensureWidgetVisible(self._selected_row.frame, 0, 6)

    def _task_settings_changed(self):
        if self._loading_workspace or self._selected_row is None:
            return
        try:
            self._commit_selected_task()
        except ValueError:
            return  # Allow incomplete edits; do not overwrite the last valid document.
        self._queue_workspace_save()

    def _task_text_changed(self, row):
        row.config.name = row.name_edit.text().strip() or "未命名任务"
        row.config.url = row.input.text().strip()
        row.config.enabled = row.enabled.isChecked()
        self._update_task_summary(row)
        if row in self.link_rows:
            self._sync_row_action_buttons(self.link_rows.index(row))
        if row is self._selected_row:
            self.settings_panel.title.setText(f"任务设置 · {row.config.name[:10]}")
        self._queue_workspace_save()

    def _update_task_summary(self, row):
        settings = row.config.settings
        mode = settings.effective_retry_mode
        limit = f"{settings.max_retry} 次" if mode == "count" else f"{settings.duration_minutes:g} 分钟" if mode == "duration" else "持续尝试"
        timing = f" · {settings.scheduled_time}" if settings.retry_mode == "scheduled" else ""
        effective_interval = max(settings.interval_ms, settings.global_interval_ms)
        interval_summary = f"{effective_interval} ms"
        if settings.interval_ms != effective_interval:
            interval_summary += f"（任务 {settings.interval_ms}）"
        row.summary.setText(f"{interval_summary} · {limit}{timing}")
        detail = (
            f"实际最小请求间隔 {effective_interval} ms；任务间隔 {settings.interval_ms} ms；"
            f"全局最小间隔 {settings.global_interval_ms} ms；{limit}；"
            f"抖动{'开启' if settings.jitter_enabled else '关闭'}"
        )
        if settings.retry_mode == "scheduled":
            target = next_scheduled_time(datetime.now(BEIJING).timestamp(), settings.scheduled_time, settings.start_delay_ms)
            detail += "；下次目标（北京时间）" + datetime.fromtimestamp(target, BEIJING).strftime("%m-%d %H:%M:%S.%f")[:-3]
        row.summary.setToolTip(detail + "。实际请求还受全局最小间隔约束。")

    def _document(self):
        self._commit_selected_task()
        return TaskDocument([row.config for row in self.link_rows], dict(self._templates), self._global_settings)

    def _queue_workspace_save(self):
        if not self._loading_workspace and not self._is_running and not self._workspace_error:
            self._save_timer.start(600)

    def _autosave_workspace(self):
        if self._loading_workspace or self._workspace_error:
            return False
        try:
            save_document(self._workspace_path(), self._document())
            return True
        except (ValueError, OSError, UnicodeError) as exc:
            self.append_ui_log(f"[配置] 未保存：{exc}")
            return False

    def _manual_save_workspace(self):
        if self._workspace_error:
            QMessageBox.warning(self, "配置未保存", "原配置读取失败，已保留原文件。请修复文件，或导入有效配置。")
        elif self._autosave_workspace():
            self.append_ui_log("[配置] 当前账号的任务、设置和模板已保存；下次启动自动恢复，不自动领取")

    def _reload_templates(self):
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        self.template_combo.addItem("套用参数模板…", None)
        for name in self._templates:
            self.template_combo.addItem(name, name)
        self.template_combo.blockSignals(False)

    def save_template_named(self, name):
        name = name.strip()
        if not 1 <= len(name) <= 24 or any(ord(ch) < 32 for ch in name):
            raise ValueError("模板名称请输入 1–24 字，不含换行")
        if name in self._templates:
            raise ValueError("模板名称已存在，请换一个名称")
        if len(self._templates) >= 100:
            raise ValueError("最多保存 100 个模板")
        self._templates[name] = self.settings_panel.snapshot()
        self._reload_templates()
        self._queue_workspace_save()

    def _save_template(self):
        name, accepted = QInputDialog.getText(self, "保存参数模板", "模板名称（只保存任务参数，不含链接与账号）：")
        if accepted:
            try:
                self.save_template_named(name)
            except ValueError as exc:
                QMessageBox.warning(self, "模板未保存", str(exc))

    def _apply_template(self, index):
        name = self.template_combo.itemData(index)
        if name not in self._templates:
            return
        if self._is_running:
            self.append_ui_log(f"[模板] 运行期间无法套用「{name}」")
        else:
            self.settings_panel.apply(with_globals(self._templates[name], self._global_settings))
            self.append_ui_log(f"[模板] 已套用「{name}」到当前任务")
        # Treat the selector as an action menu: return to its prompt so the same
        # template can be selected again, and make the action result visible in logs.
        self.template_combo.setCurrentIndex(0)

    def import_tasks_from(self, path):
        if self._is_running:
            raise ValueError("运行期间不能导入配置")
        document = load_document(path)
        if document is None:
            raise ValueError("配置文件不存在")
        # Validate and atomically write before replacing any visible task.
        save_document(self._workspace_path(), document)
        self._load_workspace()
        self.append_ui_log("[配置] 已导入当前账号；未启动任务，请核对全局设置与定时时间")

    def _import_tasks(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入任务配置", "", "任务配置 (*.json)")
        if not path:
            return
        answer = QMessageBox.question(self, "导入配置", "导入将替换当前账号的任务、模板及全局设置。是否继续？")
        if answer == QMessageBox.Yes:
            try:
                self.import_tasks_from(path)
            except (OSError, ValueError, UnicodeError) as exc:
                QMessageBox.warning(self, "导入失败", str(exc))

    def _export_tasks(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出任务配置", "领取任务.json", "任务配置 (*.json)")
        if path:
            try:
                save_document(path, self._document())
                self.append_ui_log("[配置] 已导出任务与参数；不包含 Cookie、登录身份或日志")
            except (OSError, ValueError, UnicodeError) as exc:
                QMessageBox.warning(self, "导出失败", str(exc))

    def _render_logs(self, *args):
        if not hasattr(self, "log_filter"):
            return
        records = list(self._log_history)
        if self.log_filter.currentIndex() == 1 and self._selected_row in self.link_rows:
            key = self._selected_row.config.key
            records = [(task_key, line) for task_key, line in records if task_key is None or task_key == key]
        lines = [line for _, line in records]
        self.log_text.setPlainText("\n".join(lines))
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())
