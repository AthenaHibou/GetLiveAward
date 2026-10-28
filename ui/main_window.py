from __future__ import annotations

from dataclasses import dataclass
import os
import re
from urllib.parse import parse_qs, urlparse

from PyQt5.QtCore import Qt, QThread, QSettings, QTimer, QEvent
from core.storage import get_runtime_paths
from PyQt5.QtWidgets import (
    QFrame,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QSplitter,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.logging_utils import format_log_line
from core.models import BatchClaimResult, LinkTaskItem, QrCodeData, RunSettings
from core.task_config import SavedTask, with_globals
from core.service import RewardService
from core.accounts import AccountStore, AccountProfile, DEFAULT_ACCOUNT
from core.errors import BiliRewardError
from ui.qr_dialog import QrCodeDialog
from ui.onboarding import OnboardingOverlay
from ui.settings_panel import SettingsPanel
from ui.log_highlighter import LogHighlighter, LOG_NORMAL, LOG_SUCCESS, LOG_FAILURE
from ui import theme
from ui.worker import ClaimWorker, LoginWorker
from ui.controls import SoftComboBox
from ui.task_workspace import TaskWorkspaceMixin


# ── Helpers ────────────────────────────────────────────
def _css(widget, class_name: str) -> None:
    widget.setProperty("cssClass", class_name)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def _btn(text: str, css: str, parent: QWidget) -> QPushButton:
    btn = QPushButton(text, parent)
    btn.setCursor(Qt.PointingHandCursor)
    _css(btn, css)
    return btn


def _icon_btn(text: str, parent: QWidget) -> QPushButton:
    btn = QPushButton(text, parent)
    btn.setCursor(Qt.PointingHandCursor)
    _css(btn, "iconBtn")
    return btn


# ── Data ───────────────────────────────────────────────
@dataclass
class LinkRowWidgets:
    frame: QFrame
    index_label: QLabel
    input: QLineEdit
    add_btn: QPushButton
    remove_btn: QPushButton
    pause_btn: QPushButton
    resume_btn: QPushButton
    cancel_btn: QPushButton
    status_label: QLabel
    config: SavedTask
    name_edit: QLineEdit
    enabled: QCheckBox
    summary: QLabel
    run_btn: QPushButton
    settings_btn: QPushButton


# ── Main Window ────────────────────────────────────────
class MainWindow(TaskWorkspaceMixin, QMainWindow):
    STATUS_IDLE = "空闲"
    STATUS_RUNNING = "运行中"
    STATUS_SUCCESS = "成功"
    STATUS_FAILED = "失败"
    STATUS_CANCELLED = "已取消"

    def __init__(self, service: RewardService) -> None:
        super().__init__()
        self.service = service
        self.worker_thread: QThread | None = None
        self.worker: ClaimWorker | None = None
        self.qr_dialog: QrCodeDialog | None = None
        self._is_running = False
        self._login_verified = False
        self._login_checking = False
        self._adding_account = False
        self._closing = False
        self._current_qr_link_index: int | None = None
        self._row_claim_stage_active: dict[int, bool] = {}
        self.link_rows: list[LinkRowWidgets] = []
        self.guide: OnboardingOverlay | None = None
        self._init_workspace()
        self._global_settings = RunSettings()
        self._settings = QSettings(os.path.join(service.root_paths.data_dir, "ui.ini"), QSettings.IniFormat)
        self.account_store = AccountStore(service.root_paths.data_dir)
        self._account_error = ""
        try:
            self._account_profiles = self.account_store.list_profiles()
        except BiliRewardError as exc:
            self._account_profiles = [AccountProfile(DEFAULT_ACCOUNT, "默认账号")]
            self._account_error = str(exc)
        self._first_show = True
        self._guide_timer = QTimer(self)
        self._guide_timer.setSingleShot(True)
        self._guide_timer.timeout.connect(self._maybe_show_tutorial)

        self.setWindowTitle("Bilibili Live Reward")
        available = self.screen().availableGeometry()
        self.resize(min(round(1040 * theme.SCALE), available.width() - 32), min(round(700 * theme.SCALE), available.height() - 64))
        self.setMinimumSize(min(round(920 * theme.SCALE), available.width() - 32), min(round(620 * theme.SCALE), available.height() - 64))
        self._build_ui()
        selected = self._settings.value("accounts/active_id", service.account_id, type=str)
        if selected not in {profile.account_id for profile in self._account_profiles}:
            selected = DEFAULT_ACCOUNT
        if selected != self.service.account_id:
            self._activate_account(selected)
        self._select_account_item(self.service.account_id)
        self._load_workspace()
        if self._account_error:
            self.append_ui_log(f"[账号] {self._account_error}")

    # ── Build ───────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)

        # Header
        title = QLabel("直播激励领取")
        _css(title, "title")

        self.help_btn = _btn("使用教程", "helpBtn", root)
        self.help_btn.clicked.connect(self.show_tutorial)
        self.login_btn = _btn("登录当前账号", "loginBtn", root)
        self.login_btn.setToolTip("验证当前账号的登录状态；未登录或已失效时会弹出二维码，不会提交领取")
        self.login_btn.clicked.connect(self.start_login)
        self.start_btn = _btn("全部开始", "batchStart", root)
        self.start_btn.setToolTip("独立运行所有已勾选任务；单条失败不影响其他任务，共用全局请求限制")
        self.start_btn.clicked.connect(lambda: self.start_claim())
        self.stop_all_btn = _btn("全部停止", "cancelBtn", root)
        self.stop_all_btn.setEnabled(False)
        self.stop_all_btn.clicked.connect(self.stop_all)

        self.batch_label = QLabel("批次状态")
        _css(self.batch_label, "dim")

        self.batch_status = QLabel(self.STATUS_IDLE)
        _css(self.batch_status, "dim")

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.help_btn)
        header.addSpacing(round(12 * theme.SCALE))
        header.addWidget(self.batch_label)
        header.addSpacing(round(8 * theme.SCALE))
        header.addWidget(self.batch_status)
        header.addSpacing(round(12 * theme.SCALE))
        header.addWidget(self.login_btn)
        header.addWidget(self.stop_all_btn)
        header.addWidget(self.start_btn)

        self.login_status = QLabel("本地登录待验证" if os.path.exists(self.service.paths.cookie_file) else "尚未登录")
        _css(self.login_status, "dim")
        subtitle = QHBoxLayout()
        self.product_hint = QLabel("BILIBILI  /  登录当前账号后，等待奖励开放再开始领取")
        subtitle.addWidget(self.product_hint)
        subtitle.addStretch()
        self.account_combo = SoftComboBox(root)
        self.account_combo.setMinimumWidth(round(130 * theme.SCALE))
        self.account_combo.setMaximumWidth(round(200 * theme.SCALE))
        self.account_combo.setToolTip("当前使用的账号；运行期间不能切换")
        self.account_combo.setAccessibleName("当前账号")
        for profile in self._account_profiles:
            self.account_combo.addItem(self._account_display_name(profile), profile.account_id)
            self.account_combo.setItemData(self.account_combo.count() - 1, self._account_tooltip(profile), Qt.ToolTipRole)
        self.account_combo.currentIndexChanged.connect(self._account_changed)
        self.add_account_btn = _btn("添加账号", "helpBtn", root)
        self.add_account_btn.setToolTip("直接扫码添加，自动使用昵称和 UID；已添加的账号会更新登录并切换，不会启动领取")
        self.add_account_btn.clicked.connect(self.add_account)
        self.account_combo.setEnabled(not self._account_error)
        self.add_account_btn.setEnabled(not self._account_error)
        subtitle.addWidget(self.account_combo)
        subtitle.addWidget(self.add_account_btn)
        subtitle.addSpacing(round(8 * theme.SCALE))
        subtitle.addWidget(self.login_status)

        self.flow_hint = QLabel("① 登录当前账号     →     ② 添加任务并设置参数     →     ③ 单条运行 / 全部开始    ·    配置自动保存")
        self.flow_hint.setWordWrap(True)
        self.flow_hint.setObjectName("flowHint")

        # Separator
        sep = QFrame(root)
        _css(sep, "sep")

        # Link rows
        self.rows_container = QWidget(root)
        self.rows_wrap = QVBoxLayout(self.rows_container)
        self.rows_wrap.setContentsMargins(0, 0, 0, 0)
        self.rows_wrap.setSpacing(round(8 * theme.SCALE))
        self.rows_wrap.addStretch()
        self.link_scroll = QScrollArea(root)
        self.link_scroll.setWidgetResizable(True)
        self.link_scroll.setFrameShape(QFrame.NoFrame)
        self.link_scroll.setWidget(self.rows_container)
        self.link_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.link_scroll.viewport().setAutoFillBackground(False)
        self.add_link_row()

        # Log area
        self.log_text = QPlainTextEdit(root)
        self.log_highlighter = LogHighlighter(self.log_text.document())
        self.log_text.setMaximumBlockCount(2000)
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("还没有运行记录\n\n首次使用：点击「登录当前账号」验证或扫码，不会提交领取。\n已登录：开放前手动开始，或选择定时执行。")
        links_title = QLabel("领取任务")
        _css(links_title, "sectionTitle")
        links_hint = QLabel("粘贴带 task_id 的领取链接；点「设置」编辑该任务，勾选参与全部开始。")
        links_hint.setWordWrap(True)
        _css(links_hint, "dim")
        log_title = QLabel("运行记录与领取结果")
        _css(log_title, "sectionTitle")
        self.settings_panel = SettingsPanel(QSettings(os.path.join(self.service.paths.data_dir, "ui.ini"), QSettings.IniFormat), root)
        task_toolbar = self._build_workspace_tools()
        log_header = QHBoxLayout()
        log_header.addWidget(log_title)
        log_header.addStretch()
        log_header.addWidget(self.log_filter)

        # Root layout
        layout = QVBoxLayout(root)
        layout.setContentsMargins(round(24 * theme.SCALE), round(18 * theme.SCALE), round(24 * theme.SCALE), round(20 * theme.SCALE))
        layout.setSpacing(round(9 * theme.SCALE))
        layout.addLayout(header)
        layout.addLayout(subtitle)
        layout.addWidget(sep)
        layout.addWidget(self.flow_hint)
        body = QHBoxLayout()
        body.setSpacing(round(18 * theme.SCALE))
        task_pane = QWidget()
        task_pane.setMinimumHeight(round(190 * theme.SCALE))
        task_layout = QVBoxLayout(task_pane)
        task_layout.setContentsMargins(0, 0, 0, 0)
        task_layout.setSpacing(round(8 * theme.SCALE))
        task_layout.addWidget(links_title)
        task_layout.addWidget(links_hint)
        task_layout.addLayout(task_toolbar)
        task_layout.addWidget(self.link_scroll, 1)
        log_pane = QWidget()
        log_pane.setMinimumHeight(round(120 * theme.SCALE))
        log_layout = QVBoxLayout(log_pane)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(round(8 * theme.SCALE))
        log_layout.addLayout(log_header)
        log_layout.addWidget(self.log_text, 1)
        self.task_log_splitter = QSplitter(Qt.Vertical)
        self.task_log_splitter.setObjectName("taskLogSplitter")
        self.task_log_splitter.setChildrenCollapsible(False)
        self.task_log_splitter.setHandleWidth(round(10 * theme.SCALE))
        self.task_log_splitter.addWidget(task_pane)
        self.task_log_splitter.addWidget(log_pane)
        self.task_log_splitter.setStretchFactor(0, 2)
        self.task_log_splitter.setStretchFactor(1, 1)
        self.task_log_splitter.setSizes([640, 360])
        handle = self.task_log_splitter.handle(1)
        handle.setCursor(Qt.SplitVCursor)
        handle.setToolTip("上下拖动，调整任务列表与日志区域；位置自动保存")
        saved_splitter = self._settings.value("layout/task_log_splitter")
        if saved_splitter is not None:
            self.task_log_splitter.restoreState(saved_splitter)
        self.task_log_splitter.splitterMoved.connect(
            lambda *_: self._settings.setValue(
                "layout/task_log_splitter", self.task_log_splitter.saveState()
            )
        )
        body.addWidget(self.task_log_splitter, 1)
        body.addWidget(self.settings_panel)
        layout.addLayout(body, 1)

    def _select_account_item(self, account_id: str) -> None:
        self.account_combo.blockSignals(True)
        self.account_combo.setCurrentIndex(self.account_combo.findData(account_id))
        self.account_combo.blockSignals(False)

    def _account_display_name(self, profile: AccountProfile) -> str:
        if (profile.account_id != DEFAULT_ACCOUNT
                and not os.path.exists(get_runtime_paths(self.service.base_dir, profile.account_id).cookie_file)):
            return f"{profile.name}（待登录）"
        return profile.name

    @staticmethod
    def _account_tooltip(profile: AccountProfile) -> str:
        return f"{profile.name}\n原备注：{profile.remark}" if profile.remark else profile.name

    def _reload_account_profiles(self) -> None:
        self._account_profiles = self.account_store.list_profiles()
        self.account_combo.blockSignals(True)
        try:
            self.account_combo.clear()
            for profile in self._account_profiles:
                self.account_combo.addItem(self._account_display_name(profile), profile.account_id)
                self.account_combo.setItemData(self.account_combo.count() - 1, self._account_tooltip(profile), Qt.ToolTipRole)
            self.account_combo.setCurrentIndex(self.account_combo.findData(self.service.account_id))
        finally:
            self.account_combo.blockSignals(False)
        self.setWindowTitle(f"Bilibili Live Reward · {self.account_combo.currentText()}")

    def _account_changed(self, index: int) -> None:
        account_id = self.account_combo.itemData(index)
        if self._is_running or self._closing or not account_id:
            self._select_account_item(self.service.account_id)
            return
        try:
            self._activate_account(account_id)
        except (BiliRewardError, OSError, ValueError) as exc:
            self._select_account_item(self.service.account_id)
            QMessageBox.warning(self, "切换账号失败", str(exc))

    def _activate_account(self, account_id: str) -> None:
        if self._is_running or self._closing:
            raise ValueError("请先停止当前任务，再切换账号")
        if account_id not in {profile.account_id for profile in self._account_profiles}:
            raise ValueError("账号不存在")
        if account_id == self.service.account_id:
            return
        self._save_timer.stop()
        if not self._loading_workspace:
            if not self._workspace_error and not self._autosave_workspace():
                raise ValueError("当前账号的配置未保存，请修正设置或检查文件写入权限后再切换账号")
        new_service = RewardService(self.service.base_dir, account_id=account_id)
        try:
            self.settings_panel.save()
        except ValueError:
            pass
        self.service.close()
        self.service = new_service
        self._loading_workspace = True
        self.settings_panel.load_preferences(QSettings(os.path.join(new_service.paths.data_dir, "ui.ini"), QSettings.IniFormat))
        self._settings.setValue("accounts/active_id", account_id)
        self._settings.sync()
        self.log_text.clear()
        self._log_history.clear()
        self._load_workspace()
        if os.path.exists(new_service.paths.cookie_file):
            self._on_login_checked(False, "本地登录待验证")
        elif account_id != DEFAULT_ACCOUNT:
            self._on_login_checked(False, "账号待登录")
        else:
            self._on_login_checked(False, "尚未登录")
        self.set_batch_status(self.STATUS_IDLE, "dim")
        self._row_claim_stage_active.clear()
        for row in self.link_rows:
            row.status_label.setText(self.STATUS_IDLE)
        self._refresh_link_rows()
        self._select_account_item(account_id)
        if self.guide is not None:
            self.guide.hide()
        name = next(profile.name for profile in self._account_profiles if profile.account_id == account_id)
        self.setWindowTitle(f"Bilibili Live Reward · {name}")
        self.append_ui_log(f"[账号] 已切换到「{name}」，登录、任务配置和日志独立；需手动开始")

    def add_account(self) -> None:
        if self._is_running or self._closing or self._account_error:
            return
        self.start_login(add_account=True)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            self._guide_timer.start(300)

    def _maybe_show_tutorial(self) -> None:
        if self.isVisible() and not self._settings.value("onboarding/hidden", False, type=bool):
            self.show_tutorial()

    def show_tutorial(self) -> None:
        if self._is_running or self._closing:
            return
        self.link_scroll.ensureWidgetVisible(self.link_rows[0].input)
        if self.guide is not None:
            self.guide.deleteLater()
        account_step_title = "确认当前账号" if self._login_verified else "选择账号并登录"
        account_step_body = (
            "当前账号已验证。切换账号后需要重新验证；添加另一账号用旁边的「添加账号」。"
            if self._login_verified
            else "点「登录当前账号」验证或扫码，不会提交领取。添加另一账号用旁边的「添加账号」；各账号的登录和设置独立，运行期间不能切换。"
        )
        self.guide = OnboardingOverlay(self.centralWidget(), [
            (self.link_rows[0].input, "添加领取任务", "粘贴具体奖励的领取 / 兑换页链接，地址需带 task_id。可给任务起名；勾选的任务参与「全部开始」。"),
            (self.account_combo, account_step_title, account_step_body),
            (self.settings_panel.mode, "每条任务，独立设置", "点任务的「设置」，右侧选次数、时长或定时，再点「运行」单跑。定时默认北京时间 00:00 + 20 ms，需提前登录并保持电脑唤醒。\n\n配置自动保存，可存模板复用；并发与最小间隔在全局设置中调整。"),
            (self.log_text, "结果看这里，随时停止", "领取结果和兑换码会显示在这里。部分奖励直接发放至账号，不提供兑换码。无资格会继续尝试，IP 异常会停止。可逐条取消或点「全部停止」。"),
        ])
        self.guide.dismissed.connect(self._save_tutorial_preference)
        self.guide.start(hide_next_time=self._settings.value("onboarding/hidden", False, type=bool))

    def _save_tutorial_preference(self, hidden: bool) -> None:
        self._settings.setValue("onboarding/hidden", hidden)
        self._settings.sync()
        if self._settings.status() != QSettings.NoError:
            self.append_ui_log("[教程] 无法保存偏好，下次启动可能再次显示教程")

    # ── Row management ──────────────────────────────────
    def _create_link_row(self, text: str = "", config: SavedTask | None = None) -> LinkRowWidgets:
        config = config or SavedTask(name=f"任务 {len(self.link_rows) + 1}", url=text)
        frame = QFrame(self.centralWidget())
        _css(frame, "card")

        card_layout = QVBoxLayout(frame)
        card_layout.setContentsMargins(round(12 * theme.SCALE), round(8 * theme.SCALE), round(12 * theme.SCALE), round(8 * theme.SCALE))
        card_layout.setSpacing(round(4 * theme.SCALE))
        row = QHBoxLayout()
        header_row = row
        card_layout.addLayout(row)
        row.setSpacing(round(12 * theme.SCALE))
        enabled = QCheckBox()
        enabled.setChecked(config.enabled)
        enabled.setAccessibleName("参与全部开始")
        enabled.setToolTip("勾选后参与全部开始；不影响单条运行")
        row.addWidget(enabled)

        idx = QLabel()
        _css(idx, "indexLabel")
        idx.setFixedWidth(round(28 * theme.SCALE))
        idx.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        row.addWidget(idx)
        name_edit = QLineEdit(config.name)
        name_edit.setMaxLength(48)
        name_edit.setPlaceholderText("任务名称")
        name_edit.setAccessibleName("任务名称")
        name_edit.setProperty("cssClass", "taskName")
        name_edit.setMinimumWidth(round(70 * theme.SCALE))
        name_edit.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(name_edit, 2)
        status_lbl = QLabel(self.STATUS_IDLE)
        _css(status_lbl, "dim")
        status_lbl.setMinimumWidth(round(64 * theme.SCALE))
        status_lbl.setAlignment(Qt.AlignVCenter | Qt.AlignRight)
        row.addWidget(status_lbl)
        row = QHBoxLayout()
        row.setSpacing(round(8 * theme.SCALE))
        card_layout.addLayout(row)

        inp = QLineEdit()
        inp.setText(config.url)
        inp.setPlaceholderText("输入兑换链接，如 https://www.bilibili.com/blackboard/era/award-exchange.html?task_id=...")
        row.addWidget(inp, 1)

        add_btn = _icon_btn("+", frame)
        row.addWidget(add_btn)

        rem_btn = _icon_btn("−", frame)
        row.addWidget(rem_btn)

        row = header_row
        summary = QLabel()
        summary.setProperty("cssClass", "taskSummary")
        summary.setMinimumWidth(0)
        summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.insertWidget(3, summary, 2)
        settings_btn = _btn("设置", "taskAction", frame)
        row.addWidget(settings_btn)
        run_btn = _btn("运行", "taskRun", frame)
        run_btn.setToolTip("只运行这一条任务，使用本任务的设置")
        row.addWidget(run_btn)

        pause_btn = _btn("暂停", "pauseBtn", frame)
        row.addWidget(pause_btn)

        resume_btn = _btn("继续", "resumeBtn", frame)
        row.addWidget(resume_btn)

        cancel_btn = _btn("取消", "cancelBtn", frame)
        row.addWidget(cancel_btn)

        rw = LinkRowWidgets(
            frame=frame, index_label=idx, input=inp,
            add_btn=add_btn, remove_btn=rem_btn,
            pause_btn=pause_btn, resume_btn=resume_btn,
            cancel_btn=cancel_btn, status_label=status_lbl,
            config=config, name_edit=name_edit, enabled=enabled, summary=summary,
            run_btn=run_btn, settings_btn=settings_btn,
        )

        add_btn.clicked.connect(lambda: self.add_link_row(after_row=rw))
        rem_btn.clicked.connect(lambda: self.remove_link_row(rw))
        pause_btn.clicked.connect(lambda: self.pause_claim(rw))
        resume_btn.clicked.connect(lambda: self.resume_claim(rw))
        cancel_btn.clicked.connect(lambda: self.cancel_claim(rw))
        run_btn.clicked.connect(lambda: self.start_claim(only_row=rw))
        settings_btn.clicked.connect(lambda: self.select_task(rw))
        inp.textChanged.connect(lambda: self._task_text_changed(rw))
        name_edit.textChanged.connect(lambda: self._task_text_changed(rw))
        enabled.toggled.connect(lambda: self._task_text_changed(rw))
        self._update_task_summary(rw)
        for widget in (frame, inp, name_edit):
            widget.setProperty("taskKey", config.key)
            widget.installEventFilter(self)

        return rw

    def add_link_row(self, *, after_row: LinkRowWidgets | None = None, text: str = "", config: SavedTask | None = None) -> None:
        if self._is_running or len(self.link_rows) >= 100:
            return
        new_row = self._create_link_row(text, config)
        idx = self.link_rows.index(after_row) + 1 if after_row else len(self.link_rows)
        self.link_rows.insert(idx, new_row)
        self.rows_wrap.insertWidget(idx, new_row.frame)
        self._refresh_link_rows()
        if not self._loading_workspace:
            self.select_task(new_row)
            self._queue_workspace_save()

    def remove_link_row(self, row: LinkRowWidgets) -> None:
        if self._is_running or len(self.link_rows) <= 1:
            return
        if row is self._selected_row:
            self._selected_row = None
        self.link_rows.remove(row)
        self.rows_wrap.removeWidget(row.frame)
        row.frame.deleteLater()
        self._refresh_link_rows()
        if self._selected_row is None:
            self.select_task(self.link_rows[0], commit=False)
        self._queue_workspace_save()

    def _refresh_link_rows(self) -> None:
        for i, rw in enumerate(self.link_rows):
            rw.index_label.setText(f"{i + 1:02d}")
            rw.input.setEnabled(not self._is_running)
            rw.add_btn.setEnabled(not self._is_running)
            rw.remove_btn.setEnabled(not self._is_running and len(self.link_rows) > 1)
            rw.name_edit.setEnabled(not self._is_running)
            rw.enabled.setEnabled(not self._is_running)
            self._sync_row_action_buttons(i)

    # ── URL parsing ─────────────────────────────────────
    def eventFilter(self, watched, event):
        if (event.type() in (QEvent.FocusIn, QEvent.MouseButtonPress)
                and not self._loading_workspace):
            key = watched.property("taskKey")
            if key:
                for row in self.link_rows:
                    if row.config.key == key:
                        self.select_task(row, quiet=True)
                        break
        return super().eventFilter(watched, event)

    @staticmethod
    def _parse_task_id_from_url(task_url: str) -> str:
        parsed = urlparse(task_url)
        qs = parse_qs(parsed.query)
        ids = qs.get("task_id") or qs.get("taskId")
        return ids[0].strip() if ids else ""

    def _collect_link_tasks(self, only_row: LinkRowWidgets | None = None) -> list[LinkTaskItem]:
        self._commit_selected_task()
        tasks: list[LinkTaskItem] = []
        for i, rw in enumerate(self.link_rows):
            if only_row is not None and rw is not only_row:
                continue
            if only_row is None and not rw.enabled.isChecked():
                continue
            url = rw.input.text().strip()
            if not url:
                continue
            tid = self._parse_task_id_from_url(url)
            if not tid:
                raise ValueError(f"第 {i + 1} 条领取链接无效，未解析到 task_id。")
            tasks.append(LinkTaskItem(index=i, task_url=url, task_id=tid,
                                      settings=with_globals(rw.config.settings, self._global_settings)))
        return tasks

    # ── Claim flow ──────────────────────────────────────
    def start_claim(self, *, only_row: LinkRowWidgets | None = None) -> None:
        if self._is_running:
            self.append_ui_log("[批次] 批次任务进行中，请勿重复点击")
            return

        try:
            link_tasks = self._collect_link_tasks(only_row)
            settings = self.settings_panel.snapshot()
        except ValueError as exc:
            QMessageBox.warning(self, "链接或设置无效", str(exc))
            return

        if not link_tasks:
            QMessageBox.warning(self, "没有可运行任务", "请填写领取链接；全部开始还需要勾选任务。")
            return

        for rw in self.link_rows:
            rw.status_label.setText(self.STATUS_IDLE if rw.input.text().strip() else self.STATUS_IDLE)

        for task in link_tasks:
            self.append_ui_log(f"[链接{task.index + 1}] [任务] task_id: {task.task_id}")

        self.settings_panel.save()
        self._autosave_workspace()
        self._set_running(True)
        self.set_batch_status(self.STATUS_RUNNING, "primary")
        self._start_worker(ClaimWorker(self.service, link_tasks, settings))

    def start_login(self, *, add_account: bool = False) -> None:
        if self._is_running or self._closing:
            return
        try:
            settings = self.settings_panel.snapshot()
            self.settings_panel.save()
        except ValueError as exc:
            QMessageBox.warning(self, "运行设置无效", str(exc))
            return
        saved = self._autosave_workspace()
        if add_account and not self._workspace_error and not saved:
            QMessageBox.warning(self, "添加账号失败", "当前账号配置未保存，请检查文件写入权限后重试")
            return
        self._adding_account = add_account
        self._login_checking = not add_account
        self._refresh_login_ui()
        self._set_running(True)
        self.set_batch_status("添加账号中" if add_account else "检查登录中", "primary")
        if not add_account:
            self.login_status.setText("正在检查登录…")
        worker = LoginWorker(self.service, settings, add_account=add_account)
        if not add_account:
            worker.login_checked.connect(self._on_login_checked)
        self._start_worker(worker)

    def _on_login_checked(self, success: bool, message: str) -> None:
        self._login_checking = False
        self._login_verified = success
        self.login_status.setText(message)
        self.login_status.setStyleSheet(f"color: {theme.GREEN if success else theme.TEXT_SUB};")
        self._refresh_login_ui()

    def _refresh_login_ui(self) -> None:
        if self._login_checking:
            self.login_btn.setText("检查中…")
            self.login_btn.setToolTip("正在验证当前账号的登录状态")
        elif self._login_verified:
            self.login_btn.setText("检查登录")
            self.login_btn.setToolTip("重新验证当前账号；登录已失效时会弹出二维码，不会提交领取")
        else:
            self.login_btn.setText("登录当前账号")
            self.login_btn.setToolTip("验证当前账号的登录状态；未登录或已失效时会弹出二维码，不会提交领取")
        self.product_hint.setText(
            "BILIBILI  /  登录已验证，等待奖励开放后开始领取"
            if self._login_verified
            else "BILIBILI  /  登录当前账号后，等待奖励开放再开始领取"
        )
        self.flow_hint.setText(
            "① 账号已就绪     →     ② 添加任务并设置参数     →     ③ 单条运行 / 全部开始    ·    配置自动保存"
            if self._login_verified
            else "① 登录当前账号     →     ② 添加任务并设置参数     →     ③ 单条运行 / 全部开始    ·    配置自动保存"
        )

    def _register_logged_in_account(self) -> None:
        if self._account_error or not self.service.account_identity:
            return
        try:
            self.account_store.bind_login(self.service.account_id, self.service.account_identity)
            self._reload_account_profiles()
        except (BiliRewardError, ValueError, OSError) as exc:
            self.append_ui_log(f"[账号] 登录身份保存失败：{exc}")

    def stop_all(self) -> None:
        if self.worker is not None:
            self.worker.cancel_all()
            self.stop_all_btn.setEnabled(False)
            self.set_batch_status("正在停止", "dim")
            self.append_ui_log("[批次] 已请求停止全部任务，等待在途请求返回")

    def _start_worker(self, worker: ClaimWorker) -> None:
        self.worker_thread = QThread(self)
        self.worker = worker
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.run)
        self.worker.log_message.connect(self.append_log)
        self.worker.status_changed.connect(lambda s: self.set_batch_status(s, None))
        self.worker.clock_status_changed.connect(self.settings_panel.sync_status.setText)
        self.worker.link_status_changed.connect(self.set_link_status)
        self.worker.link_claim_stage_changed.connect(self.set_link_claim_stage_active)
        self.worker.qr_code_ready.connect(self.show_qr_dialog)
        self.worker.batch_completed.connect(self.on_batch_completed)
        self.worker_thread.finished.connect(self.on_worker_finished)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.start()

    # ── Logging ─────────────────────────────────────────
    def append_log(self, message: str) -> None:
        match = re.search(r"\[链接(\d+)\]", message)
        task_key = None
        if match:
            index = int(match.group(1)) - 1
            if 0 <= index < len(self.link_rows):
                task_key = self.link_rows[index].config.key
        self._log_history.append((task_key, message))
        selected_key = self._selected_row.config.key if self._selected_row is not None else None
        if (not hasattr(self, "log_filter") or self.log_filter.currentIndex() == 0
                or task_key is None or task_key == selected_key):
            self.log_text.appendPlainText(message)
        if not self._adding_account and "登录状态验证通过" in message:
            self._on_login_checked(True, "登录已验证")
        elif not self._adding_account and "扫码登录成功" in message:
            self._on_login_checked(False, "扫码完成，验证登录中…")
        if self.qr_dialog and self.qr_dialog.isVisible() and "[登录]" in message:
            self.qr_dialog.set_status_text(message)
            if "扫码登录成功" in message:
                self.qr_dialog.hide()

    def append_ui_log(self, message: str) -> None:
        self.append_log(format_log_line(message))

    # ── State ───────────────────────────────────────────
    def set_batch_status(self, text: str, css: str | None) -> None:
        self.batch_status.setText(text)
        if css:
            _css(self.batch_status, css)

    def _set_running(self, running: bool) -> None:
        self._is_running = running
        self._refresh_login_ui()
        self.login_btn.setEnabled(not running)
        self.help_btn.setEnabled(not running)
        self.account_combo.setEnabled(not running and not self._account_error)
        self.add_account_btn.setEnabled(not running and not self._account_error)
        self.settings_panel.setEnabled(not running)
        self._save_timer.stop()
        for button in (self.new_task_btn, self.save_tasks_btn, self.import_tasks_btn, self.export_tasks_btn):
            button.setEnabled(not running)
        self.stop_all_btn.setEnabled(running)
        if running and self.guide is not None:
            self.guide.hide()
        self._refresh_link_rows()
        if not running:
            self.start_btn.setEnabled(True)
        else:
            self.start_btn.setEnabled(False)

    def _sync_row_action_buttons(self, link_index: int) -> None:
        if link_index >= len(self.link_rows):
            return
        rw = self.link_rows[link_index]
        st = rw.status_label.text()
        active = self._row_claim_stage_active.get(link_index, False)
        rw.run_btn.setEnabled(not self._is_running and bool(rw.input.text().strip()))
        rw.run_btn.setVisible(not self._is_running)
        rw.pause_btn.setVisible(self._is_running and st != "已暂停")
        rw.resume_btn.setVisible(self._is_running and st == "已暂停")
        rw.cancel_btn.setVisible(self._is_running)

        rw.pause_btn.setEnabled(self._is_running and active and st == "抢奖中")
        rw.resume_btn.setEnabled(self._is_running and active and st == "已暂停")
        rw.cancel_btn.setEnabled(self._is_running and st in {"排队中", "预处理中", "待抢奖", "等待定时", "抢奖中", "已暂停"})

        # Color the status label
        color_map = {"抢奖中": "resumeBtn", "已暂停": "pauseBtn",
                     "成功": "resumeBtn", "失败": "cancelBtn", "已取消": "cancelBtn"}
        _css(rw.status_label, color_map.get(st, "dim"))

    def set_link_status(self, link_index: int, status: str) -> None:
        if link_index >= len(self.link_rows):
            return
        self.link_rows[link_index].status_label.setText(status)
        self._sync_row_action_buttons(link_index)

    def set_link_claim_stage_active(self, link_index: int, active: bool) -> None:
        self._row_claim_stage_active[link_index] = active
        self._sync_row_action_buttons(link_index)

    # ── Actions ─────────────────────────────────────────
    def pause_claim(self, row: LinkRowWidgets) -> None:
        if self.worker:
            self.worker.pause_claim(self.link_rows.index(row))

    def resume_claim(self, row: LinkRowWidgets) -> None:
        if self.worker:
            self.worker.resume_claim(self.link_rows.index(row))

    def cancel_claim(self, row: LinkRowWidgets) -> None:
        if self.worker:
            self.worker.cancel_claim(self.link_rows.index(row))

    # ── QR dialog ───────────────────────────────────────
    def show_qr_dialog(self, link_index: int, qr_data: QrCodeData) -> None:
        if self._closing:
            return
        self._current_qr_link_index = link_index if link_index >= 0 else None
        if self.qr_dialog is None:
            self.qr_dialog = QrCodeDialog(self)
            self.qr_dialog.rejected.connect(self.cancel_login)
        account_name = "添加账号" if self._adding_account else self.account_combo.currentText()
        self.qr_dialog.setWindowTitle(f"{account_name} · 扫码登录")
        self.qr_dialog.set_status_text("请确认手机当前账号，再扫码并确认登录")
        self.qr_dialog.set_qr_code(qr_data.image_bytes, qr_data.login_url)
        self.qr_dialog.show()
        self.qr_dialog.raise_()
        self.qr_dialog.activateWindow()

    def cancel_login(self) -> None:
        if self.worker is None:
            return
        idx = self._current_qr_link_index
        self.append_ui_log(f"[链接{idx + 1}] [登录] 用户关闭二维码弹窗" if idx is not None else "[登录] 关闭弹窗")
        self.worker.cancel_login()

    # ── Completion ──────────────────────────────────────
    def on_batch_completed(self, result: BatchClaimResult) -> None:
        sc = sum(1 for r in result.results if r.status == "success")
        fc = sum(1 for r in result.results if r.status == "failed")
        cc = sum(1 for r in result.results if r.status == "cancelled")
        self.append_ui_log(f"[批次] 完成 — 成功 {sc} / 失败 {fc} / 取消 {cc}")
        if not self._closing:
            QMessageBox.information(self, "批次完成",
                f'<span style="color: {LOG_SUCCESS}">成功 {sc} 条</span><br>'
                f'<span style="color: {LOG_FAILURE}">失败 {fc} 条</span><br>'
                f'<span style="color: {LOG_NORMAL}">取消 {cc} 条</span>')

    def on_worker_finished(self) -> None:
        was_login_check = isinstance(self.worker, LoginWorker)
        adding = self._adding_account
        new_login = self.worker.new_account_login if adding and was_login_check else None
        cancelled = self.worker._is_login_cancelled() if adding and self.worker else False
        if not adding:
            self._register_logged_in_account()
        self._adding_account = False
        self._set_running(False)
        if was_login_check:
            self.set_batch_status(self.STATUS_IDLE, "dim")
        self._row_claim_stage_active.clear()
        self._current_qr_link_index = None
        for i in range(len(self.link_rows)):
            self._sync_row_action_buttons(i)
        if self.qr_dialog:
            self.qr_dialog.hide()
        self.worker_thread = None
        self.worker = None
        if adding and new_login and not cancelled and not self._closing:
            cookies, identity = new_login
            try:
                profile = self.account_store.save_login(identity, cookies, preferred_id=self.service.account_id)
                existed = profile.account_id in {p.account_id for p in self._account_profiles}
                self._reload_account_profiles()
                self._activate_account(profile.account_id)
                self.service.account_identity = identity
                self._on_login_checked(True, "登录已验证")
                action = "已更新登录并选中" if existed else "已添加"
                self.append_ui_log(f"[账号] {action}「{profile.name}」")
            except (BiliRewardError, ValueError, OSError) as exc:
                self.append_ui_log(f"[账号] 添加失败：{exc}")
                QMessageBox.warning(self, "添加账号失败", str(exc))
        self._queue_workspace_save()
        if self._closing:
            self.close()

    def closeEvent(self, event) -> None:
        if self.worker_thread is not None and self.worker_thread.isRunning():
            self._closing = True
            if self.worker is not None:
                self.worker.cancel_all()
            self.set_batch_status("正在退出", "dim")
            if self.qr_dialog:
                self.qr_dialog.hide()
            event.ignore()
            return
        try:
            self.settings_panel.save()
        except ValueError:
            self.append_ui_log("[设置] 无效设置未保存")
        self._save_timer.stop()
        self._settings.setValue("layout/task_log_splitter", self.task_log_splitter.saveState())
        self._autosave_workspace()
        self.service.close()
        super().closeEvent(event)
