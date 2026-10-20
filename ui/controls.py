from PyQt5.QtCore import Qt, QPointF
from PyQt5.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt5.QtWidgets import QComboBox, QListView

from ui import theme


class SoftComboBox(QComboBox):
    """Rounded selector with a matching popup and a resource-free chevron."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setView(QListView())

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
