from PyQt5.QtCore import Qt, QPointF, QEvent, QObject
from PyQt5.QtGui import QColor, QPainter, QPen, QPolygonF, QWheelEvent
from PyQt5.QtWidgets import QApplication, QAbstractScrollArea, QComboBox, QListView

from ui import theme


class ClickToWheelGuard(QObject):
    """Only a deliberate click arms wheel editing; hovering scrolls the page."""

    def __init__(self, control):
        super().__init__(control)
        self.control = control
        self.armed = False
        control.setFocusPolicy(Qt.StrongFocus)
        control.installEventFilter(self)
        # Spin boxes receive text-field clicks through their internal editor.
        if hasattr(control, "lineEdit") and control.lineEdit() is not None:
            control.lineEdit().installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self.armed = True
        elif event.type() == QEvent.FocusOut or (
                event.type() == QEvent.Leave and watched is self.control):
            self.armed = False
        elif event.type() == QEvent.Wheel and not (self.armed and self.control.hasFocus()):
            ancestor = self.control.parentWidget()
            while ancestor is not None:
                if isinstance(ancestor, QAbstractScrollArea):
                    viewport = ancestor.viewport()
                    forwarded = QWheelEvent(
                        QPointF(viewport.mapFromGlobal(event.globalPos())), event.globalPosF(),
                        event.pixelDelta(), event.angleDelta(), event.buttons(), event.modifiers(),
                        event.phase(), event.inverted(), event.source(),
                    )
                    QApplication.sendEvent(viewport, forwarded)
                    event.accept()
                    return True
                ancestor = ancestor.parentWidget()
            event.ignore()
            return True
        return False


def require_click_to_wheel(control):
    if not hasattr(control, "_wheel_guard"):
        control._wheel_guard = ClickToWheelGuard(control)


class SoftComboBox(QComboBox):
    """Rounded selector with a matching popup and a resource-free chevron."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setView(QListView())
        require_click_to_wheel(self)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(theme.BLUE if self.isEnabled() else theme.TEXT_DIM),
                            1.6 * theme.SCALE, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        x, y, d = self.width() - 13 * theme.SCALE, self.height() / 2, 3 * theme.SCALE
        painter.drawPolyline(QPolygonF([QPointF(x - d, y - d / 2),
                                        QPointF(x, y + d / 2),
                                        QPointF(x + d, y - d / 2)]))
