from __future__ import annotations

from PyQt5.QtCore import QEvent, QPoint, QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui import theme


class OnboardingOverlay(QWidget):
    """A local, non-networked tour; highlighted controls are explanatory only."""

    dismissed = pyqtSignal(bool)

    def __init__(self, parent: QWidget, steps: list[tuple[QWidget, str, str]]) -> None:
        super().__init__(parent)
        self.steps = steps
        self.step = 0
        self.highlight = QRectF()
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.card = QFrame(self)
        self.card.setObjectName("tourCard")
        self.card.setStyleSheet(f"""
            QFrame#tourCard {{ background: #FFFFFF; border: 2px solid {theme.BLUE}; border-radius: 16px; }}
            QLabel {{ background: transparent; border: none; color: {theme.TEXT_MAIN}; }}
            QLabel#tourProgress {{ color: {theme.BLUE}; font-weight: 700; }}
            QLabel#tourTitle {{ font-size: {round(20 * theme.SCALE)}px; font-weight: 700; }}
            QLabel#tourBody {{ color: {theme.TEXT_SUB}; font-size: {round(14 * theme.SCALE)}px; }}
            QPushButton#tourNext {{ background: {theme.BLUE}; color: white; border: none; font-weight: 700; }}
        """)
        self.progress = QLabel()
        self.progress.setObjectName("tourProgress")
        self.title = QLabel()
        self.title.setObjectName("tourTitle")
        self.title.setWordWrap(True)
        self.body = QLabel()
        self.body.setObjectName("tourBody")
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.PlainText)
        self.hide_next_time = QCheckBox("下次启动不再自动显示")
        self.previous_btn = QPushButton("上一步")
        self.previous_btn.clicked.connect(self.previous_step)
        self.next_btn = QPushButton("下一步")
        self.next_btn.setObjectName("tourNext")
        self.next_btn.clicked.connect(self.next_step)
        self.skip_btn = QPushButton("跳过")
        self.skip_btn.clicked.connect(self.finish)
        for button in (self.previous_btn, self.next_btn, self.skip_btn):
            button.setCursor(Qt.PointingHandCursor)
        navigation = QHBoxLayout()
        navigation.addWidget(self.skip_btn)
        navigation.addStretch()
        navigation.addWidget(self.previous_btn)
        navigation.addWidget(self.next_btn)
        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        layout.addWidget(self.progress)
        layout.addWidget(self.title)
        layout.addWidget(self.body)
        layout.addWidget(self.hide_next_time)
        layout.addLayout(navigation)
        parent.installEventFilter(self)
        self.hide()

    def start(self, *, hide_next_time: bool = False) -> None:
        self.step = 0
        self.hide_next_time.setChecked(hide_next_time)
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self.setFocus()
        self._refresh()

    def _refresh(self) -> None:
        _, title, body = self.steps[self.step]
        self.progress.setText(
            f"上手指南   /   {self.step + 1:02d} · {len(self.steps):02d}"
        )
        self.title.setText(title)
        self.body.setText(body)
        self.previous_btn.setVisible(self.step > 0)
        self.next_btn.setText(
            "开始使用" if self.step == len(self.steps) - 1 else "下一步"
        )
        self._position_card()

    def _position_card(self) -> None:
        target = self.steps[self.step][0]
        position = target.mapTo(self.parentWidget(), QPoint(0, 0))
        self.highlight = QRectF(
            position.x() - 5,
            position.y() - 5,
            target.width() + 10,
            target.height() + 10,
        )
        width = min(round(440 * theme.SCALE), self.width() - 32)
        max_width = min(round(600 * theme.SCALE), self.width() - 32)
        vertical_space = max(self.height() - self.highlight.bottom() - 32, self.highlight.top() - 32)
        while True:
            self.card.setFixedWidth(width)
            self.card.adjustSize()
            height = max(self.card.layout().totalHeightForWidth(width), self.card.sizeHint().height())
            if height <= vertical_space or width >= max_width:
                break
            width = min(width + round(60 * theme.SCALE), max_width)
        self.card.resize(width, height)
        x = max(
            16,
            min(
                int(self.highlight.center().x() - width / 2), self.width() - width - 16
            ),
        )
        y = int(self.highlight.bottom()) + 16
        if y + height > self.height() - 16:
            y = int(self.highlight.top()) - height - 16
        y = max(16, min(y, self.height() - height - 16))
        self.card.move(x, y)
        self.update()

    def next_step(self) -> None:
        if self.step == len(self.steps) - 1:
            self.finish()
        else:
            self.step += 1
            self._refresh()

    def previous_step(self) -> None:
        if self.step:
            self.step -= 1
            self._refresh()

    def finish(self) -> None:
        self.hide()
        self.dismissed.emit(self.hide_next_time.isChecked())

    def eventFilter(self, watched, event) -> bool:
        if watched is self.parentWidget() and event.type() == QEvent.Resize:
            self.setGeometry(self.parentWidget().rect())
            if self.isVisible():
                self._position_card()
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.finish()
        elif event.key() == Qt.Key_Right:
            self.next_step()
        elif event.key() == Qt.Key_Left:
            self.previous_step()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        mask = QPainterPath()
        mask.setFillRule(Qt.OddEvenFill)
        mask.addRect(QRectF(self.rect()))
        mask.addRoundedRect(self.highlight, 12, 12)
        painter.fillPath(mask, QColor(20, 34, 48, 145))
        painter.setPen(QPen(QColor(theme.BLUE), 2))
        painter.drawRoundedRect(self.highlight, 12, 12)
        # A speech-bubble pointer connects the card to the highlighted control.
        card = self.card.geometry()
        anchor_x = max(
            card.left() + 28, min(int(self.highlight.center().x()), card.right() - 28)
        )
        pointer = QPainterPath()
        if card.top() >= self.highlight.bottom():
            pointer.moveTo(anchor_x - 9, card.top() + 1)
            pointer.lineTo(anchor_x, card.top() - 10)
            pointer.lineTo(anchor_x + 9, card.top() + 1)
        elif card.bottom() <= self.highlight.top():
            pointer.moveTo(anchor_x - 9, card.bottom() - 1)
            pointer.lineTo(anchor_x, card.bottom() + 10)
            pointer.lineTo(anchor_x + 9, card.bottom() - 1)
        pointer.closeSubpath()
        painter.fillPath(pointer, QColor("white"))
        painter.drawPath(pointer)
