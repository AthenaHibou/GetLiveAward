from __future__ import annotations

from dataclasses import asdict
from html import escape

from PyQt5.QtCore import QSettings, Qt, QTime, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractSpinBox,
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
from ui.controls import SoftComboBox, require_click_to_wheel


class SettingsPanel(QWidget):
    """Persist user choices, then snapshot them at batch start."""

    changed = pyqtSignal()

    def __init__(self, preferences: QSettings, parent=None) -> None:
        super().__init__(parent)
        self.preferences = preferences
        self._applying = False
        self.setFixedWidth(round(300 * theme.SCALE))
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
        self.interval_label = QLabel("执行间隔 / ms")
        form.addRow(self.interval_label, self.interval_ms)
        self.interval_ms.setToolTip(
            "普通模式：收到上次响应后，再等待执行间隔。\n\n"
            "异步派发：按两次发起时间计算执行间隔，无需等待上次响应。\n\n"
            "所有请求仍受全局执行间隔和在途上限约束。1000 ms = 1 秒。"
        )
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
            "包含第一次提交。只有开启单任务请求并发时，才按每个领取线程分别计算次数。"
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
        global_note = QLabel("所有任务共用账号最小请求间隔；任务自身间隔独立计算。")
        global_note.setWordWrap(True)
        global_note.setStyleSheet(f"color: {theme.TEXT_SUB};")
        form.addRow(global_note)
        self.global_interval_ms = QSpinBox()
        self.global_interval_ms.setRange(100, 60000)
        self.global_interval_ms.setSingleStep(100)
        form.addRow("最小执行间隔 / ms", self.global_interval_ms)
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
        risk_title = QLabel("风控处理")
        risk_title.setProperty("cssClass", "sectionTitle")
        form.addRow(risk_title)
        self.ignore_risk_control = QCheckBox("无视风控继续尝试")
        self.ignore_risk_control.setToolTip(
            "默认关闭：遇到 IP 异常、风控错误或 HTTP 412/429 时停止当前任务。\n\n"
            "开启后仍按执行间隔重试，可能导致风控加剧。"
            "领取仍受尝试次数、执行时长和手动停止约束；准备请求只在有限时间内重试。\n\n"
            "-702/-705 等请求频率错误默认会重试，无需开启此项。"
        )
        form.addRow(self.ignore_risk_control)
        risk_style = f"color: {theme.RED}; background: #FFF0ED; padding: 8px; border-radius: 6px;"
        risk_warning = QLabel("警告：开启后可能导致风控加剧。")
        risk_warning.setWordWrap(True)
        risk_warning.setStyleSheet(risk_style)
        form.addRow(risk_warning)
        concurrency_title = QLabel("并发设置 · 高风险")
        concurrency_title.setProperty("cssClass", "sectionTitle")
        form.addRow(concurrency_title)
        self.concurrency_group = QWidget()
        concurrency_form = QFormLayout(self.concurrency_group)
        concurrency_form.setContentsMargins(0, 0, 0, 0)
        concurrency_form.setSpacing(round(8 * theme.SCALE))
        self.risk_note = QLabel(
            "高风险：并发可能触发限流、IP 异常或账号风控。\n"
            "如果你不清楚会有什么后果，请勿变动此项。"
        )
        self.risk_note.setWordWrap(True)
        self.risk_note.setStyleSheet(risk_style)
        concurrency_form.addRow(self.risk_note)
        self.parallel_links = QCheckBox("多任务异步派发（不等待响应）")
        self.parallel_links.setToolTip(
            "默认关闭：前一个任务的请求返回后，再按列表优先级派发就绪请求。\n\n"
            "开启：无需等待其他任务的请求返回，按全局执行间隔继续派发。"
            "不会增加单任务的领取线程。\n\n"
            "等待定时、重试或暂停不占执行机会。达到全局在途上限时仍需等待。"
        )
        concurrency_form.addRow(self.parallel_links)
        self.links = QSpinBox()
        self.links.setRange(1, 4)
        concurrency_form.addRow("同时在途任务数", self.links)
        self.concurrent = QCheckBox("启用单任务请求并发")
        self.concurrent.setToolTip("只增加每条任务的领取线程，与多任务并发分别控制。")
        concurrency_form.addRow(self.concurrent)
        self.requests = QSpinBox()
        self.requests.setRange(1, 4)
        concurrency_form.addRow("每任务请求上限", self.requests)
        self.global_requests = QSpinBox()
        self.global_requests.setRange(1, 4)
        self.global_requests.setToolTip(
            "所有任务合计的在途请求上限；只限制总量，不会自动增加单任务线程。"
        )
        concurrency_form.addRow("全局在途请求上限", self.global_requests)
        self.async_dispatch = QCheckBox("异步派发")
        self.async_dispatch.setToolTip(
            "作用于每条任务内部，多任务时也分别生效。\n\n"
            "开启后，按执行间隔发起下一次请求，无需等待上一次请求返回。\n\n"
            "仍受每任务和全局在途请求上限约束；达到上限时等待空位。"
            "上限为 1 时仍需等待响应。不积压或集中补发错过的请求。"
        )
        concurrency_form.addRow(self.async_dispatch)
        self.dispatch_note = QLabel(
            "多任务异步默认关闭：等前一个请求返回后，按列表顺序优先派发就绪请求。\n"
            "等待定时、重试或暂停不占执行机会。\n\n"
            "多任务异步：不等待其他任务的响应。\n"
            "异步派发：每条任务内部不等待上次响应。\n\n"
            "所有请求仍受执行间隔及在途上限约束；达到上限时等待。"
        )
        self.dispatch_note.setWordWrap(True)
        self.dispatch_note.setStyleSheet(f"color: {theme.TEXT_SUB};")
        concurrency_form.addRow(self.dispatch_note)
        self.warning = QLabel()
        self.warning.setWordWrap(True)
        concurrency_form.addRow(self.warning)
        form.addRow(self.concurrency_group)
        note = QLabel("频率过高会按设置重试；风控默认只停止当前任务，其他任务继续；成功后停止重复领取。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_SUB};")
        form.addRow(note)
        # Rich-text tooltips wrap within a predictable width on Windows, unlike
        # plain tooltips which can span the entire desktop as one long line.
        tooltip_width = round(300 * theme.SCALE)
        for widget in content.findChildren(QWidget):
            if isinstance(widget, (QAbstractSpinBox, QComboBox)):
                require_click_to_wheel(widget)
            if widget.toolTip():
                body = escape(widget.toolTip()).replace("\n", "<br>")
                widget.setToolTip(
                    f'<qt><table width="{tooltip_width}"><tr><td>{body}</td></tr></table></qt>'
                )
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
            self.async_dispatch,
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
                        self.sync_interval, self.concurrent, self.async_dispatch, self.requests, self.global_requests, self.parallel_links,
                        self.links, self.global_interval_ms, self.ignore_risk_control):
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
            async_dispatch=self.async_dispatch.isChecked(),
            ignore_risk_control=self.ignore_risk_control.isChecked(),
            request_workers=self.requests.value(),
            global_request_workers=self.global_requests.value(),
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
        self.async_dispatch.setChecked(settings.async_dispatch)
        self.ignore_risk_control.setChecked(settings.ignore_risk_control)
        self.requests.setValue(settings.request_workers)
        self.global_requests.setValue(settings.global_request_workers)
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
        self.requests.setEnabled(self.concurrent.isChecked() or self.async_dispatch.isChecked())
        self.links.setEnabled(self.parallel_links.isChecked())
        enabled = self.concurrent.isChecked() or self.parallel_links.isChecked() or self.async_dispatch.isChecked()
        self.global_requests.setEnabled(enabled)
        risky = enabled or self.interval_ms.value() < 1000
        self.warning.setText(
            "已启用并发或异步；每任务、任务间和全局在途上限仍然有效。"
            if enabled
            else "当前不允许请求重叠；任务各自等待，按列表优先派发。"
        )
        self.warning.setStyleSheet(f"color: {theme.AMBER if risky else theme.TEXT_SUB};")
