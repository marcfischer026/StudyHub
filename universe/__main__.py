"""Entry point: ``python -m universe``."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .config import Settings
from .ui import style
from .ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("UniVerse")
    app.setApplicationDisplayName("UniVerse")
    app.setOrganizationName("UniVerse")
    style.apply(app)
    settings = Settings.load()
    window = MainWindow(settings)
    window.show()
    window.raise_()  # started from a terminal, macOS would otherwise leave it behind
    window.activateWindow()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
