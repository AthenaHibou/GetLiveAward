from __future__ import annotations

import os
import sys

if sys.version_info < (3, 10):
    raise SystemExit("GetLiveAward 需要 Python 3.10 或以上版本，建议使用 Python 3.12。")

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication

from core.service import RewardService
from ui.main_window import MainWindow
from ui.theme import apply_theme

# Enable high-DPI scaling — must happen before QApplication creation
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)


def main() -> int:
    app = QApplication(sys.argv)
    asset_root = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    app_icon = QIcon(os.path.join(asset_root, "ui", "assets", "app_icon.png"))
    app.setWindowIcon(app_icon)

    # Report detected scale factor
    screen = app.primaryScreen()
    if screen is not None:
        dpi = screen.logicalDotsPerInch()
        ratio = screen.devicePixelRatio()
        print(f"Screen DPI: {dpi:.0f}, devicePixelRatio: {ratio:.1f}")

    apply_theme(app)
    service = RewardService()
    window = MainWindow(service)
    window.setWindowIcon(app_icon)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
