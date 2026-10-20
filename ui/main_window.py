from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import parse_qs, urlparse

from PyQt5.QtCore import Qt, QThread, QSettings, QTimer
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QInputDialog,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.logging_utils import format_log_line
from core.models import BatchClaimResult, LinkTaskItem, QrCodeData
from core.service import RewardService
from core.accounts import AccountStore, AccountProfile, DEFAULT_ACCOUNT
from core.errors import BiliRewardError
from ui.qr_dialog import QrCodeDialog
from ui.onboarding import OnboardingOverlay
from ui.settings_panel import SettingsPanel
from ui import theme
from ui.worker import ClaimWorker, LoginWorker
from ui.controls import SoftComboBox


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


# ── Main Window ────────────────────────────────────────
class MainWindow(QMainWindow):
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
        self._closing = False
        self._current_qr_link_index: int | None = None
        self._row_claim_stage_active: dict[int, bool] = {}
        self.link_rows: list[LinkRowWidgets] = []
        self.guide: OnboardingOverlay | None = None
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
        if self._account_error:
            self.append_ui_log(f"[账号] {self._account_error}")

    # ── Build ───────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)

        # Header
        title = QLabel("直播奖励领取")
        _css(title, "title")

        self.help_btn = _btn("使用教程", "helpBtn", root)
        self.help_btn.clicked.connect(self.show_tutorial)
        self.login_btn = _btn("先登录", "loginBtn", root)
        self.login_btn.setToolTip("检查本地登录；需要时弹出二维码，不会提交领取")
        self.login_btn.clicked.connect(self.start_login)
        self.start_btn = _btn("全部开始", "primary", root)
        self.start_btn.setToolTip("按活动开放时间手动开始；每条链接独立执行")
        self.start_btn.clicked.connect(self.start_claim)
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
        subtitle.addWidget(QLabel("BILIBILI  /  先完成扫码，再等待奖励开放"))
        subtitle.addStretch()
        self.account_combo = SoftComboBox(root)
        self.account_combo.setMinimumWidth(round(130 * theme.SCALE))
        self.account_combo.setMaximumWidth(round(200 * theme.SCALE))
        self.account_combo.setToolTip("当前使用的账号；运行期间不能切换")
        self.account_combo.setAccessibleName("当前账号")
        for profile in self._account_profiles:
            self.account_combo.addItem(profile.name, profile.account_id)
        self.account_combo.currentIndexChanged.connect(self._account_changed)
        self.add_account_btn = _btn("添加账号", "helpBtn", root)
        self.add_account_btn.setToolTip("创建独立账号档案并扫码登录，不会启动领取")
        self.add_account_btn.clicked.connect(self.add_account)
        self.account_combo.setEnabled(not self._account_error)
        self.add_account_btn.setEnabled(not self._account_error)
        subtitle.addWidget(self.account_combo)
        subtitle.addWidget(self.add_account_btn)
        subtitle.addSpacing(round(8 * theme.SCALE))
        subtitle.addWidget(self.login_status)

        self.flow_hint = QLabel("① 复制领取界面的链接     →     ② 点击「先登录」完成扫码     →     ③ 手动开始 / 定时执行")
        self.flow_hint.setWordWrap(True)
        self.flow_hint.setObjectName("flowHint")

        # Separator
        sep = QFrame(root)
        _css(sep, "sep")

        # Link rows
        self.rows_container = QWidget(root)
        self.rows_wrap = QVBoxLayout(self.rows_container)
        self.rows_wrap.setContentsMargins(0, 0, 0, 0)
        self.rows_wrap.setSpacing(round(12 * theme.SCALE))
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
        self.log_text.setMaximumBlockCount(2000)
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("还没有运行记录\n\n第一次使用：点击「先登录」完成扫码，不会提交领取。\n已登录：在开放前手动开始，或从右侧选择定时执行。")
        links_title = QLabel("领取链接")
        _css(links_title, "sectionTitle")
        links_hint = QLabel("从具体奖励的领取 / 兑换界面复制完整链接，通常包含 task_id；每行一个任务，点击 + 添加。")
        links_hint.setWordWrap(True)
        _css(links_hint, "dim")
        log_title = QLabel("运行记录与领取结果")
        _css(log_title, "sectionTitle")

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
        left = QVBoxLayout()
        left.setSpacing(round(9 * theme.SCALE))
        left.addWidget(links_title)
        left.addWidget(links_hint)
        left.addWidget(self.link_scroll)
        left.addWidget(log_title)
        left.addWidget(self.log_text, 1)
        self.settings_panel = SettingsPanel(QSettings(os.path.join(self.service.paths.data_dir, "ui.ini"), QSettings.IniFormat), root)
        body.addLayout(left, 1)
        body.addWidget(self.settings_panel)
        layout.addLayout(body, 1)

    def _select_account_item(self, account_id: str) -> None:
        self.account_combo.blockSignals(True)
        self.account_combo.setCurrentIndex(self.account_combo.findData(account_id))
        self.account_combo.blockSignals(False)

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
        new_service = RewardService(self.service.base_dir, account_id=account_id)
        try:
            self.settings_panel.save()
        except ValueError:
            pass
        self.service.close()
        self.service = new_service
        self.settings_panel.load_preferences(QSettings(os.path.join(new_service.paths.data_dir, "ui.ini"), QSettings.IniFormat))
        self._settings.setValue("accounts/active_id", account_id)
        self._settings.sync()
        self.log_text.clear()
        self._on_login_checked(False, "本地登录待验证" if os.path.exists(new_service.paths.cookie_file) else "尚未登录")
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
        self.append_ui_log(f"[账号] 已切换到「{name}」，Cookie、日志和运行设置独立；当前链接保留，需手动开始")

    def add_account(self) -> None:
        if self._is_running or self._closing or self._account_error:
            return
        name, accepted = QInputDialog.getText(self, "添加账号", "账号备注（1–24 字）\n请先在 B 站 App 切换到要添加的账号，再扫码：")
        if not accepted:
            return
        try:
            self.add_account_named(name)
        except (BiliRewardError, ValueError, OSError) as exc:
            QMessageBox.warning(self, "添加账号失败", str(exc))

    def add_account_named(self, name: str) -> None:
        if self._is_running or self._closing:
            raise ValueError("请先停止当前任务，再添加账号")
        added = self.account_store.add(name)
        self._account_profiles = self.account_store.list_profiles()
        self.account_combo.blockSignals(True)
        self.account_combo.addItem(added.name, added.account_id)
        self.account_combo.blockSignals(False)
        self._activate_account(added.account_id)
        self.start_login()

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
        self.guide = OnboardingOverlay(self.centralWidget(), [
            (self.link_rows[0].input, "粘贴领取链接", "复制具体奖励的领取 / 兑换页链接，地址需带 task_id。活动首页或直播间链接不能代替。每行一条，点 + 添加。"),
            (self.account_combo, "选账号，先登录", "点「先登录」扫码，不会提交领取。加账号用旁边的「添加账号」；各账号的登录和设置独立，运行期间不能切换。"),
            (self.settings_panel.mode, "设好方式，再开始", "右侧可选次数、时长或定时。定时默认北京时间 00:00 + 100 ms，自动校时；抖动可选，并发默认关闭。\n\n设好后点「全部开始」。定时请提前扫码，并保持电脑唤醒、联网。"),
            (self.log_text, "结果看这里，随时停止", "扫码成功不等于领取成功，兑换码会显示在这里。无资格会继续尝试，IP 异常会停止。可逐条取消或点「全部停止」，教程随时可重看。"),
        ])
        self.guide.dismissed.connect(self._save_tutorial_preference)
        self.guide.start(hide_next_time=self._settings.value("onboarding/hidden", False, type=bool))

    def _save_tutorial_preference(self, hidden: bool) -> None:
        self._settings.setValue("onboarding/hidden", hidden)
        self._settings.sync()
        if self._settings.status() != QSettings.NoError:
            self.append_ui_log("[教程] 无法保存偏好，下次启动可能再次显示教程")

    # ── Row management ──────────────────────────────────
    def _create_link_row(self, text: str = "") -> LinkRowWidgets:
        frame = QFrame(self.centralWidget())
        _css(frame, "card")

        card_layout = QVBoxLayout(frame)
        card_layout.setContentsMargins(round(16 * theme.SCALE), round(10 * theme.SCALE), round(16 * theme.SCALE), round(10 * theme.SCALE))
        card_layout.setSpacing(round(6 * theme.SCALE))
        row = QHBoxLayout()
        card_layout.addLayout(row)
        row.setSpacing(round(12 * theme.SCALE))

        idx = QLabel()
        _css(idx, "indexLabel")
        idx.setFixedWidth(round(68 * theme.SCALE))
        idx.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        row.addWidget(idx)

        inp = QLineEdit()
        inp.setText(text)
        inp.setPlaceholderText("输入兑换链接，如 https://www.bilibili.com/blackboard/era/award-exchange.html?task_id=...")
        row.addWidget(inp, 1)

        add_btn = _icon_btn("+", frame)
        row.addWidget(add_btn)

        rem_btn = _icon_btn("−", frame)
        row.addWidget(rem_btn)

        row = QHBoxLayout()
        card_layout.addLayout(row)
        row.addStretch()

        pause_btn = _btn("暂停", "pauseBtn", frame)
        row.addWidget(pause_btn)

        resume_btn = _btn("继续", "resumeBtn", frame)
        row.addWidget(resume_btn)

        cancel_btn = _btn("取消", "cancelBtn", frame)
        row.addWidget(cancel_btn)

        status_lbl = QLabel(self.STATUS_IDLE)
        _css(status_lbl, "dim")
        status_lbl.setFixedWidth(round(64 * theme.SCALE))
        status_lbl.setAlignment(Qt.AlignVCenter | Qt.AlignCenter)
        row.addWidget(status_lbl)

        rw = LinkRowWidgets(
            frame=frame, index_label=idx, input=inp,
            add_btn=add_btn, remove_btn=rem_btn,
            pause_btn=pause_btn, resume_btn=resume_btn,
            cancel_btn=cancel_btn, status_label=status_lbl,
        )

        add_btn.clicked.connect(lambda: self.add_link_row(after_row=rw))
        rem_btn.clicked.connect(lambda: self.remove_link_row(rw))
        pause_btn.clicked.connect(lambda: self.pause_claim(rw))
        resume_btn.clicked.connect(lambda: self.resume_claim(rw))
        cancel_btn.clicked.connect(lambda: self.cancel_claim(rw))

        return rw

    def add_link_row(self, *, after_row: LinkRowWidgets | None = None, text: str = "") -> None:
        new_row = self._create_link_row(text)
        idx = self.link_rows.index(after_row) + 1 if after_row else len(self.link_rows)
        self.link_rows.insert(idx, new_row)
        self.rows_wrap.insertWidget(idx, new_row.frame)
        self._refresh_link_rows()

    def remove_link_row(self, row: LinkRowWidgets) -> None:
        if self._is_running or len(self.link_rows) <= 1:
            return
        self.link_rows.remove(row)
        self.rows_wrap.removeWidget(row.frame)
        row.frame.deleteLater()
        self._refresh_link_rows()

    def _refresh_link_rows(self) -> None:
        for i, rw in enumerate(self.link_rows):
            rw.index_label.setText(f"链接 {i + 1}")
            rw.input.setEnabled(not self._is_running)
            rw.add_btn.setEnabled(not self._is_running)
            rw.remove_btn.setEnabled(not self._is_running and len(self.link_rows) > 1)
            self._sync_row_action_buttons(i)
        # Fit a single row snugly; extra links scroll instead of squeezing the log.
        content_height = sum(rw.frame.sizeHint().height() for rw in self.link_rows)
        content_height += self.rows_wrap.spacing() * max(0, len(self.link_rows) - 1)
        self.link_scroll.setFixedHeight(min(content_height + 2, round(230 * theme.SCALE)))

    # ── URL parsing ─────────────────────────────────────
    @staticmethod
    def _parse_task_id_from_url(task_url: str) -> str:
        parsed = urlparse(task_url)
        qs = parse_qs(parsed.query)
        ids = qs.get("task_id") or qs.get("taskId")
        return ids[0].strip() if ids else ""

    def _collect_link_tasks(self) -> list[LinkTaskItem]:
        tasks: list[LinkTaskItem] = []
        for i, rw in enumerate(self.link_rows):
            url = rw.input.text().strip()
            if not url:
                continue
            tid = self._parse_task_id_from_url(url)
            if not tid:
                raise ValueError(f"第 {i + 1} 条领取链接无效，未解析到 task_id。")
            tasks.append(LinkTaskItem(index=i, task_url=url, task_id=tid))
        return tasks

    # ── Claim flow ──────────────────────────────────────
    def start_claim(self) -> None:
        if self._is_running:
            self.append_ui_log("[批次] 批次任务进行中，请勿重复点击")
            return

        try:
            link_tasks = self._collect_link_tasks()
            settings = self.settings_panel.snapshot()
        except ValueError as exc:
            QMessageBox.warning(self, "链接或设置无效", str(exc))
            return

        if not link_tasks:
            QMessageBox.warning(self, "领取链接为空", "请至少输入一条领取链接。")
            return

        for rw in self.link_rows:
            rw.status_label.setText(self.STATUS_IDLE if rw.input.text().strip() else self.STATUS_IDLE)

        for task in link_tasks:
            self.append_ui_log(f"[链接{task.index + 1}] [任务] task_id: {task.task_id}")

        self.settings_panel.save()
        self._set_running(True)
        self.set_batch_status(self.STATUS_RUNNING, "primary")
        self._start_worker(ClaimWorker(self.service, link_tasks, settings))

    def start_login(self) -> None:
        if self._is_running or self._closing:
            return
        try:
            settings = self.settings_panel.snapshot()
            self.settings_panel.save()
        except ValueError as exc:
            QMessageBox.warning(self, "运行设置无效", str(exc))
            return
        self._set_running(True)
        self.set_batch_status("检查登录中", "primary")
        self.login_status.setText("正在检查登录…")
        worker = LoginWorker(self.service, settings)
        worker.login_checked.connect(self._on_login_checked)
        self._start_worker(worker)

    def _on_login_checked(self, success: bool, message: str) -> None:
        self.login_status.setText(message)
        self.login_status.setStyleSheet(f"color: {theme.GREEN if success else theme.TEXT_SUB};")

    def _register_logged_in_account(self) -> None:
        if self._account_error or not self.service.account_identity:
            return
        try:
            self.account_store.bind_login(self.service.account_id, self.service.account_identity)
            self._account_profiles = self.account_store.list_profiles()
            for profile in self._account_profiles:
                index = self.account_combo.findData(profile.account_id)
                self.account_combo.setItemText(index, profile.name)
                self.account_combo.setItemData(index, profile.name, Qt.ToolTipRole)
            self.setWindowTitle(f"Bilibili Live Reward · {self.account_combo.currentText()}")
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
        self.log_text.appendPlainText(message)
        if "扫码登录成功" in message or "登录状态验证通过" in message:
            self._on_login_checked(True, "登录已验证")
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
        self.login_btn.setEnabled(not running)
        self.help_btn.setEnabled(not running)
        self.account_combo.setEnabled(not running and not self._account_error)
        self.add_account_btn.setEnabled(not running and not self._account_error)
        self.settings_panel.setEnabled(not running)
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
        account_name = self.account_combo.currentText()
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
                f"成功 {sc} 条\n失败 {fc} 条\n取消 {cc} 条")

    def on_worker_finished(self) -> None:
        self._register_logged_in_account()
        self._set_running(False)
        self._row_claim_stage_active.clear()
        self._current_qr_link_index = None
        for i in range(len(self.link_rows)):
            self._sync_row_action_buttons(i)
        if self.qr_dialog:
            self.qr_dialog.hide()
        self.worker_thread = None
        self.worker = None
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
        self.service.close()
        super().closeEvent(event)
