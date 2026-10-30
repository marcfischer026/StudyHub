"""App-wide stylesheet. Colours come from the system palette (``palette(...)``)
where possible so light and dark mode both work; only accents are fixed."""

from __future__ import annotations

from PySide6.QtGui import QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

ACCENT = "#2f6fed"
RECORD = "#e5484d"
WARNING = "#c27c0e"


def _is_dark() -> bool:
    return QGuiApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128


def stylesheet() -> str:
    dark = _is_dark()
    card = "rgba(255,255,255,0.05)" if dark else "rgba(0,0,0,0.035)"
    border = "rgba(255,255,255,0.12)" if dark else "rgba(0,0,0,0.12)"
    muted = "rgba(255,255,255,0.55)" if dark else "rgba(0,0,0,0.5)"
    hover = "rgba(255,255,255,0.08)" if dark else "rgba(0,0,0,0.06)"
    return f"""
    QMainWindow::separator {{ width: 1px; height: 1px; background: {border}; }}

    QToolBar#mainToolbar {{
        spacing: 8px; padding: 6px 10px; border: none;
        border-bottom: 1px solid {border};
    }}
    QToolBar#mainToolbar::handle {{ image: none; width: 0px; }}
    QToolBar#formatBar {{ spacing: 2px; border: none; padding: 2px 0; }}
    QToolBar#formatBar QToolButton {{ padding: 3px 7px; border-radius: 5px; }}
    QToolBar#formatBar QToolButton:hover {{ background: {hover}; }}
    QToolBar#formatBar QToolButton:checked {{ background: {ACCENT}; color: white; }}

    QDockWidget {{ font-weight: 600; }}
    QDockWidget::title {{ padding: 6px 8px; text-align: left; }}
    QDockWidget > QWidget {{ font-weight: normal; }}
    QTabBar::tab {{ padding: 5px 12px; }}

    QPushButton#recordButton {{
        background: {RECORD}; color: white; border: none; border-radius: 15px;
        padding: 6px 18px; font-weight: 700; min-height: 18px;
    }}
    QPushButton#recordButton:hover {{ background: #f05a5f; }}
    QPushButton#recordButton[recording="true"] {{ background: #8f1d21; }}
    QPushButton#recordButton:disabled {{ background: {border}; color: {muted}; }}
    QPushButton#primaryButton {{
        background: {ACCENT}; color: white; border: none; border-radius: 7px;
        padding: 6px 16px; font-weight: 600; min-height: 18px;
    }}
    QPushButton#primaryButton:hover {{ background: #4a83f0; }}
    QPushButton#primaryButton:disabled {{ background: {border}; color: {muted}; }}
    QPushButton#warningButton {{
        background: transparent; color: {WARNING}; border: 1px solid {WARNING};
        border-radius: 12px; padding: 3px 10px; font-weight: 600;
    }}
    QLabel#recordTime {{ font-family: Menlo; font-size: 14px; font-weight: 600; color: {RECORD}; }}
    QProgressBar#levelMeter {{ border: none; border-radius: 4px; background: {border}; }}
    QProgressBar#levelMeter::chunk {{ border-radius: 4px; background: #30a46c; }}
    QToolButton#panelsButton {{ padding: 4px 10px; border: 1px solid {border}; border-radius: 6px; }}
    QToolButton#panelsButton::menu-indicator {{ image: none; width: 0px; }}
    QToolButton#micButton {{ font-size: 15px; padding: 2px 4px; }}
    QLabel#breadcrumb {{ font-weight: 600; padding: 0 6px; }}

    QLabel#muted {{ color: {muted}; }}
    QLabel#welcomeTitle {{ font-size: 28px; font-weight: 700; }}
    QLabel#welcomeSubtitle {{ font-size: 14px; color: {muted}; }}
    QLabel#lectureTitle {{ font-size: 20px; font-weight: 700; }}
    QFrame#card {{ background: {card}; border: 1px solid {border}; border-radius: 10px; padding: 6px; }}
    QLabel#cardTitle {{ font-weight: 700; font-size: 13px; }}

    QListWidget#lectureList {{ border: none; background: transparent; }}
    QListWidget#lectureList::item {{
        background: {card}; border: 1px solid {border}; border-radius: 8px;
        padding: 10px 12px; margin: 3px 0;
    }}
    QListWidget#lectureList::item:hover {{ border-color: {ACCENT}; }}

    QTreeWidget#subjectTree {{ border: none; background: transparent; }}
    QTreeWidget#subjectTree::item {{ padding: 5px 4px; border-radius: 6px; }}
    QTreeWidget#subjectTree::item:hover {{ background: {hover}; }}
    QTreeWidget#subjectTree::item:selected {{ background: {ACCENT}; color: white; }}

    QTextEdit#notesEdit {{ border: none; font-size: 14px; }}
    QToolButton#activityLabel {{ border: none; padding: 1px 4px; }}
    QToolButton#activityLabel:hover {{ background: {hover}; border-radius: 5px; }}
    QToolButton#activityPause {{ border: none; padding: 0 4px; }}
    QProgressBar#activityBar {{ border: none; border-radius: 3px; background: {border}; }}
    QProgressBar#activityBar::chunk {{ border-radius: 3px; background: {ACCENT}; }}
    QFrame#activityPopup {{ background: palette(window); border: 1px solid {border}; border-radius: 10px; }}
    QFrame#rule {{ color: {border}; }}
    QLabel#slidePage {{ background: white; border: 1px solid {border}; }}
    QWidget#playerBar {{ border-top: 1px solid {border}; }}
    QToolButton#playButton {{ font-size: 14px; min-width: 26px; }}
    """


def apply(app: QApplication) -> None:
    app.setStyleSheet(stylesheet())
