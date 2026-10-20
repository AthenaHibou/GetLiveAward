from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ui.theme import BLUE, BLUE_HOVER, BLUE_PRESS, CARD_BG, BORDER, TEXT_SUB, ROOT_BG
from ui import theme


class QrCodeDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("扫码登录")
        self.setModal(True)
        self.setMinimumSize(round(380 * theme.SCALE), round(460 * theme.SCALE))
        self.resize(round(420 * theme.SCALE), round(500 * theme.SCALE))

        self.setStyleSheet(f"QDialog {{ background-color: {ROOT_BG}; }}")

        self._qr_pixmap: QPixmap | None = None

        # Status heading
        self.status_label = QLabel("请使用 B 站 APP 扫码")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet(
            f"color: {BLUE}; font-size: {round(18 * theme.SCALE)}px; font-weight: 700; padding: {round(12 * theme.SCALE)}px;"
        )

        # QR image — white card with border
        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setMinimumSize(round(300 * theme.SCALE), round(300 * theme.SCALE))
        self.qr_label.setStyleSheet(
            f"border: {round(2 * theme.SCALE)}px solid {BORDER}; border-radius: {round(18 * theme.SCALE)}px; "
            f"background-color: #FFFFFF; padding: {round(12 * theme.SCALE)}px;"
        )

        # URL display
        self.url_label = QLabel()
        self.url_label.setAlignment(Qt.AlignCenter)
        self.url_label.setWordWrap(True)
        self.url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.url_label.setStyleSheet(
            f"color: {TEXT_SUB}; font-size: {round(12 * theme.SCALE)}px; padding: {round(12 * theme.SCALE)}px; "
            f"background-color: {CARD_BG}; border-radius: {round(12 * theme.SCALE)}px; "
            f"border: {round(1 * theme.SCALE)}px solid {BORDER};"
        )

        # Close button
        self.close_btn = QPushButton("关  闭")
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setFixedSize(round(120 * theme.SCALE), round(42 * theme.SCALE))
        self.close_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {BLUE};
                color: #FFFFFF;
                border: none;
                border-radius: {round(12 * theme.SCALE)}px;
                font-size: {round(16 * theme.SCALE)}px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {BLUE_HOVER};
            }}
            QPushButton:pressed {{
                background-color: {BLUE_PRESS};
            }}
        """)
        self.close_btn.clicked.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.addStretch()
        btn_row.addWidget(self.close_btn)
        btn_row.addStretch()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(round(32 * theme.SCALE), round(28 * theme.SCALE), round(32 * theme.SCALE), round(28 * theme.SCALE))
        layout.setSpacing(round(18 * theme.SCALE))
        layout.addWidget(self.status_label)
        layout.addWidget(self.qr_label, 1)
        layout.addWidget(self.url_label)
        layout.addSpacing(round(4 * theme.SCALE))
        layout.addLayout(btn_row)

    def set_qr_code(self, image_bytes: bytes, login_url: str) -> None:
        pixmap = QPixmap()
        pixmap.loadFromData(image_bytes, "PNG")
        self._qr_pixmap = pixmap
        self.url_label.setText(login_url)
        self._update_qr_pixmap()

    def set_status_text(self, text: str) -> None:
        self.status_label.setText(text)

    def _update_qr_pixmap(self) -> None:
        if self._qr_pixmap is None:
            return
        s = min(self.qr_label.width(), self.qr_label.height()) - round(24 * theme.SCALE)
        if s <= 0:
            return
        self.qr_label.setPixmap(
            self._qr_pixmap.scaled(s, s, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_qr_pixmap()
