from __future__ import annotations

from dataclasses import asdict

from PyQt5.QtCore import QSettings, Qt, QTime, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QLineEdit,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from core.models import RunSettings
from ui import theme
from ui.controls import SoftComboBox


class SettingsPanel(QWidget):
    """Persist user choices, then snapshot them at batch start."""

    changed = pyqtSignal()

    def __init__(self, preferences: QSettings, parent=None) -> None:
        super().__init__(parent)
        self.preferences = preferences
        self._applying = False
        self.setFixedWidth(round(270 * theme.SCALE))
        self.title = title = QLabel("当前任务设置")
        title.setProperty("cssClass", "sectionTitle")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        form = QFormLayout(content)
        form.setContentsMargins(10, 8, 10, 8)
        form.setSpacing(round(8 * theme.SCALE))
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.interval_ms = QSpinBox()
        self.interval_ms.setRange(100, 60000)
        self.interval_ms.setSingleStep(100)
        self.interval_ms.setToolTip(
            "本任务的请求间隔与失败重试等待；实际还受账号全局最小间隔约束。1000 ms = 1 秒。"
        )
        form.addRow("间隔 / ms", self.interval_ms)
        self.mode = SoftComboBox()
        self.mode.addItem("按尝试次数", "count")
        self.mode.addItem("按执行时长", "duration")
        self.mode.addItem("持续运行", "continuous")
        self.mode.addItem("定时执行", "scheduled")
        form.addRow("执行方式", self.mode)
        self.schedule_group = QWidget()
        schedule_form = QFormLayout(self.schedule_group)
        schedule_form.setContentsMargins(0, 0, 0, 0)
        schedule_form.setSpacing(round(8 * theme.SCALE))
        self.schedule_time = QTimeEdit()
        self.schedule_time.setDisplayFormat("HH:mm:ss")
        self.schedule_label = QLabel("北京时间")
        schedule_form.addRow(self.schedule_label, self.schedule_time)
        self.start_delay = QSpinBox()
        self.start_delay.setRange(0, 60000)
        self.delay_label = QLabel("首发延迟 / ms")
        schedule_form.addRow(self.delay_label, self.start_delay)
        self.after_schedule = SoftComboBox()
        for choice_title, value in (
            ("按尝试次数", "count"),
            ("按执行时长", "duration"),
            ("持续运行", "continuous"),
        ):
            self.after_schedule.addItem(choice_title, value)
        self.after_label = QLabel("到点后")
        schedule_form.addRow(self.after_label, self.after_schedule)
        self.schedule_note = QLabel(
            "按北京时间计算下一次时刻。点击「单条运行」或「全部开始」提前准备，并保持电脑唤醒。等待时不提交领取，首发不加随机抖动。"
        )
        self.schedule_note.setWordWrap(True)
        schedule_form.addRow(self.schedule_note)
        form.addRow(self.schedule_group)
        self.max_retry = QSpinBox()
        self.max_retry.setRange(1, 100000)
        self.max_retry.setToolTip(
            "包含第一次提交。开启并发后，每个领取线程分别计算次数。"
        )
        self.count_label = QLabel("尝试次数")
        form.addRow(self.count_label, self.max_retry)
        self.duration = QDoubleSpinBox()
        self.duration.setRange(0.1, 1440)
        self.duration.setDecimals(1)
        self.duration.setSingleStep(1)
        self.duration.setToolTip(
            "每条链接从领取阶段开始计时；含暂停、重试和重新登录等待，初次扫码与任务准备不计时。到时不再发请求，在途请求等待返回。"
        )
        self.duration_label = QLabel("时长 / 分钟")
        form.addRow(self.duration_label, self.duration)
        self.duration_note = QLabel(
            "每条领取单独计时，暂停和重新登录也计时；到时停止新请求，等待在途请求返回。"
        )
        self.duration_note.setWordWrap(True)
        self.duration_note.setStyleSheet(f"color: {theme.TEXT_SUB};")
        form.addRow(self.duration_note)
        self.timeout_ms = QSpinBox()
        self.timeout_ms.setRange(1000, 60000)
        self.timeout_ms.setSingleStep(1000)
        form.addRow("超时 / ms", self.timeout_ms)
        self.sold_out = QCheckBox("明确库存耗尽时停止")
        self.sold_out.setToolTip(
            "根据领取接口明确的库存耗尽提示停止；202032 无资格始终继续尝试。"
        )
        form.addRow(self.sold_out)
        self.jitter = QCheckBox("随机间隔抖动")
        form.addRow(self.jitter)
        self.jitter_ms = QSpinBox()
        self.jitter_ms.setRange(0, 60000)
        self.jitter_ms.setSingleStep(50)
        self.jitter_ms.setToolTip(
            "每次领取失败后额外随机等待 0 到此值 ms；不会缩短基础间隔，不作用于定时首发。"
        )
        self.jitter_label = QLabel("额外抖动 / ms")
        form.addRow(self.jitter_label, self.jitter_ms)
        self.sync = QCheckBox("启用时间同步")
        global_title = QLabel("账号全局设置")
        global_title.setProperty("cssClass", "sectionTitle")
        form.addRow(global_title)
        global_note = QLabel("所有任务共用；实际间隔取任务与全局设置中较大值。")
        global_note.setWordWrap(True)
        global_note.setStyleSheet(f"color: {theme.TEXT_SUB};")
        form.addRow(global_note)
        self.global_interval_ms = QSpinBox()
        self.global_interval_ms.setRange(100, 60000)
        self.global_interval_ms.setSingleStep(100)
        form.addRow("最小间隔 / ms", self.global_interval_ms)
        form.addRow(self.sync)
        self.sync_group = QWidget()
        sync_form = QFormLayout(self.sync_group)
        sync_form.setContentsMargins(0, 0, 0, 0)
        sync_form.setSpacing(round(8 * theme.SCALE))
        self.server = QLineEdit()
        self.server.setPlaceholderText("ntp.aliyun.com")
        self.server_label = QLabel("NTP 服务器")
        sync_form.addRow(self.server_label, self.server)
        self.sync_interval = QSpinBox()
        self.sync_interval.setRange(5, 3600)
        self.sync_interval_label = QLabel("校时间隔 / 秒")
        sync_form.addRow(self.sync_interval_label, self.sync_interval)
        self.sync_status = QLabel("未校时；只校准程序时间")
        self.sync_status.setWordWrap(True)
        self.sync_status.setStyleSheet(f"color: {theme.TEXT_SUB};")
        sync_form.addRow(self.sync_status)
        form.addRow(self.sync_group)
        self.concurrent = QCheckBox("启用请求并发（实验）")
        form.addRow(self.concurrent)
        self.requests = QSpinBox()
        self.requests.setRange(1, 4)
        form.addRow("并发请求上限", self.requests)
        self.parallel_links = QCheckBox("同时处理多条链接")
        form.addRow(self.parallel_links)
        self.links = QSpinBox()
        self.links.setRange(1, 4)
        form.addRow("同时链接数", self.links)
        self.warning = QLabel()
        self.warning.setWordWrap(True)
        form.addRow(self.warning)
        note = QLabel(
            "无资格继续尝试；前一条结束或取消后才处理下一条。\n\nIP 异常 / 风控停止本批次，此保护始终开启。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_SUB};")
        form.addRow(note)
        scroll.setWidget(content)
        reset = QPushButton("恢复默认设置")
        reset.setCursor(Qt.PointingHandCursor)
        reset.clicked.connect(lambda: self.apply(RunSettings()))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(title)
        layout.addWidget(scroll, 1)
        layout.addWidget(reset)
        self._restore()
        for control in (
            self.mode,
            self.concurrent,
            self.parallel_links,
            self.interval_ms,
            self.after_schedule,
            self.jitter,
            self.sync,
        ):
            signal = (
                control.currentIndexChanged
                if isinstance(control, QComboBox)
                else control.valueChanged
                if isinstance(control, QSpinBox)
                else control.toggled
            )
            signal.connect(self._update_controls)
        self._update_controls()
        for control in (self.interval_ms, self.mode, self.after_schedule, self.schedule_time,
                        self.start_delay, self.max_retry, self.duration, self.timeout_ms,
                        self.sold_out, self.jitter, self.jitter_ms, self.sync, self.server,
                        self.sync_interval, self.concurrent, self.requests, self.parallel_links,
                        self.links, self.global_interval_ms):
            signal = (control.currentIndexChanged if isinstance(control, QComboBox)
                      else control.toggled if isinstance(control, QCheckBox)
                      else control.textChanged if isinstance(control, QLineEdit)
                      else control.timeChanged if isinstance(control, QTimeEdit)
                      else control.valueChanged)
            signal.connect(self._emit_changed)

    def _emit_changed(self, *args) -> None:
        if not self._applying:
            self.changed.emit()

    def snapshot(self) -> RunSettings:
        return RunSettings(
            global_interval_ms=self.global_interval_ms.value(),
            concurrent=self.concurrent.isChecked(),
            request_workers=self.requests.value(),
            parallel_links=self.parallel_links.isChecked(),
            link_workers=self.links.value(),
            interval_ms=self.interval_ms.value(),
            retry_mode=self.mode.currentData(),
            max_retry=self.max_retry.value(),
            duration_minutes=self.duration.value(),
            timeout=self.timeout_ms.value() / 1000,
            stop_sold_out=self.sold_out.isChecked(),
            jitter_enabled=self.jitter.isChecked(),
            jitter_ms=self.jitter_ms.value(),
            time_sync_enabled=self.sync.isChecked(),
            time_server=self.server.text().strip(),
            sync_interval_seconds=self.sync_interval.value(),
            scheduled_time=self.schedule_time.time().toString("HH:mm:ss"),
            start_delay_ms=self.start_delay.value(),
            scheduled_retry_mode=self.after_schedule.currentData(),
        )

    def save(self) -> None:
        for name, value in asdict(self.snapshot()).items():
            self.preferences.setValue(f"run/{name}", value)
        self.preferences.sync()

    def load_preferences(self, preferences: QSettings) -> None:
        self.preferences = preferences
        self._restore()

    def _restore(self) -> None:
        defaults = RunSettings()
        try:
            values = {
                name: self.preferences.value(f"run/{name}", value, type=type(value))
                for name, value in asdict(defaults).items()
            }
            self.apply(RunSettings(**values))
        except (ValueError, TypeError, OverflowError):
            self.apply(defaults)

    def apply(self, settings: RunSettings) -> None:
        self._applying = True
        self.global_interval_ms.setValue(settings.global_interval_ms)
        self.interval_ms.setValue(settings.interval_ms)
        self.mode.setCurrentIndex(self.mode.findData(settings.retry_mode))
        self.max_retry.setValue(settings.max_retry)
        self.duration.setValue(settings.duration_minutes)
        self.timeout_ms.setValue(round(settings.timeout * 1000))
        self.sold_out.setChecked(settings.stop_sold_out)
        self.jitter.setChecked(settings.jitter_enabled)
        self.jitter_ms.setValue(settings.jitter_ms)
        self.sync.setChecked(settings.time_sync_enabled)
        self.server.setText(settings.time_server)
        self.sync_interval.setValue(settings.sync_interval_seconds)
        self.schedule_time.setTime(
            QTime.fromString(settings.scheduled_time, "HH:mm:ss")
        )
        self.start_delay.setValue(settings.start_delay_ms)
        self.after_schedule.setCurrentIndex(
            self.after_schedule.findData(settings.scheduled_retry_mode)
        )
        self.concurrent.setChecked(settings.concurrent)
        self.requests.setValue(settings.request_workers)
        self.parallel_links.setChecked(settings.parallel_links)
        self.links.setValue(settings.link_workers)
        self._update_controls()
        self._applying = False
        self.changed.emit()

    def _update_controls(self) -> None:
        mode = self.mode.currentData()
        scheduled = mode == "scheduled"
        self.schedule_group.setVisible(scheduled)
        for widget in (
            self.schedule_time,
            self.schedule_label,
            self.start_delay,
            self.delay_label,
            self.after_schedule,
            self.after_label,
            self.schedule_note,
        ):
            widget.setVisible(scheduled)
        if scheduled:
            self.sync.setChecked(True)
            mode = self.after_schedule.currentData()
        self.sync.setEnabled(not scheduled)
        self.sync_group.setVisible(self.sync.isChecked())
        for widget in (
            self.server,
            self.server_label,
            self.sync_interval,
            self.sync_interval_label,
        ):
            widget.setVisible(self.sync.isChecked())
        self.sync_status.setVisible(self.sync.isChecked())
        self.jitter_ms.setVisible(self.jitter.isChecked())
        self.jitter_label.setVisible(self.jitter.isChecked())
        self.max_retry.setVisible(mode == "count")
        self.count_label.setVisible(mode == "count")
        self.duration.setVisible(mode == "duration")
        self.duration_label.setVisible(mode == "duration")
        self.duration_note.setVisible(mode == "duration")
        enabled = self.concurrent.isChecked()
        self.requests.setEnabled(enabled)
        self.parallel_links.setEnabled(enabled)
        self.links.setEnabled(enabled and self.parallel_links.isChecked())
        risky = enabled or self.interval_ms.value() < 1000
        self.warning.setText(
            "并发或较短间隔可能触发 IP 异常；没有通用的安全频率保证。"
            if risky
            else "默认：串行请求、逐条链接。"
        )
        self.warning.setStyleSheet(
            f"color: {theme.AMBER if risky else theme.TEXT_SUB};"
        )
