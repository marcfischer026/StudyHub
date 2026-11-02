"""App-wide design tokens and stylesheet.

Every colour, radius and spacing value the UI uses lives here (see
``docs/ui-overhaul.md``). Neutrals come from the system palette so light and
dark mode follow macOS; only the accent and the state colours are fixed.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

# Muted blue, hue ~210°: calm enough for hours of reading, kept off the stock
# macOS blue and out of the indigo/violet band. Contrast ≥ 5:1 in both themes.
ACCENT_LIGHT = "#2F6497"
ACCENT_DARK = "#6FA0D0"  # text and icons on dark backgrounds
ACCENT_FILL_DARK = "#3A72A8"  # filled controls in dark mode, white text on top

# State colours: the only saturated colours in the app, so they stand out.
RECORD = "#E5484D"
WARNING = "#C27C0E"
SUCCESS = "#1A7F37"
DANGER = "#CF222E"
NEUTRAL = "#8A8F98"  # "not started yet", e.g. unseen topics
ATTENTION = "#D9622B"  # between warning and danger, e.g. other voices close to the microphone

# Back-compat for modules that still import the old names.
ACCENT = ACCENT_LIGHT

SPACE = (4, 8, 12, 16, 24)  # the only spacing values; index with SPACE[n]
RADIUS_S, RADIUS_M, RADIUS_L = 4, 6, 10  # controls / cards and inputs / popovers


@dataclass(frozen=True)
class Tokens:
    dark: bool
    accent: str  # text, icons, focus
    accent_fill: str  # filled buttons and selection, white text on top
    accent_hover: str
    text: str
    muted: str
    card: str
    border: str
    hover: str
    separator: str  # opaque hairline
    raised: str  # the chosen segment of a segmented control
    mono: str  # font family for times and timestamps

    def accent_tint(self, alpha: int) -> QColor:
        """Translucent accent for backgrounds (drop targets, highlighted lines)."""
        colour = QColor(self.accent_fill)
        colour.setAlpha(alpha)
        return colour


SF_MONO = Path("/System/Library/Fonts/SFNSMono.ttf")  # ships with macOS but Qt doesn't list it


@lru_cache(maxsize=1)
def _mono_family() -> str:
    """SF Mono on macOS (loaded from the system, not bundled); else the system monospace."""
    if SF_MONO.exists():
        families = QFontDatabase.applicationFontFamilies(QFontDatabase.addApplicationFont(str(SF_MONO)))
        if families:
            return families[0]
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()


def _is_dark() -> bool:
    scheme = QGuiApplication.styleHints().colorScheme()
    if scheme != Qt.ColorScheme.Unknown:  # macOS tells us; the palette may lag behind a switch
        return scheme == Qt.ColorScheme.Dark
    return QGuiApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128


def tokens(dark: bool | None = None) -> Tokens:
    """Tokens for the current system appearance (or the one asked for)."""
    dark = _is_dark() if dark is None else dark
    mono = _mono_family()
    if dark:
        return Tokens(
            dark=True,
            accent=ACCENT_DARK,
            accent_fill=ACCENT_FILL_DARK,
            accent_hover="#346A9E",
            text="rgba(255,255,255,0.88)",
            muted="rgba(255,255,255,0.55)",
            card="rgba(255,255,255,0.05)",
            border="rgba(255,255,255,0.12)",
            hover="rgba(255,255,255,0.08)",
            separator="#3A3A3C",
            raised="#636366",
            mono=mono,
        )
    return Tokens(
        dark=False,
        accent=ACCENT_LIGHT,
        accent_fill=ACCENT_LIGHT,
        accent_hover="#3A72A8",
        text="rgba(0,0,0,0.85)",
        muted="rgba(0,0,0,0.5)",
        card="rgba(0,0,0,0.035)",
        border="rgba(0,0,0,0.12)",
        hover="rgba(0,0,0,0.06)",
        separator="#D5D5D7",
        raised="#FFFFFF",
        mono=mono,
    )


def stylesheet(t: Tokens | None = None) -> str:
    t = t or tokens()
    s, m, l = RADIUS_S, RADIUS_M, RADIUS_L
    return f"""
    QToolBar#mainToolbar {{
        spacing: 8px; padding: 4px 12px; border: none;
        border-bottom: 1px solid {t.border};
    }}
    QToolBar#mainToolbar::handle {{ image: none; width: 0px; }}
    QToolBar#formatBar {{ spacing: 4px; border: none; padding: 4px 0; }}
    QToolBar#formatBar QToolButton {{ padding: 4px 8px; border-radius: {s}px; }}
    QToolBar#formatBar QToolButton:hover {{ background: {t.hover}; }}
    QToolBar#formatBar QToolButton:checked {{ background: {t.accent_fill}; color: white; }}

    QSplitter#workbench::handle, QSplitter#centerSplit::handle {{ background: {t.separator}; }}
    QWidget#inspectorHeader {{ border-bottom: 1px solid {t.separator}; }}
    QToolButton#segment {{ border: none; border-radius: {s}px; padding: 4px 8px; }}
    QToolButton#segment:hover {{ background: {t.hover}; }}
    QToolButton#segment:checked {{ background: {t.hover}; color: {t.accent}; font-weight: 600; }}
    QToolButton#chromeToggle, QToolButton#chromeToggle:checked {{
        border: none; border-radius: {s}px; padding: 4px; background: transparent;
    }}
    QToolButton#chromeToggle:hover {{ background: {t.hover}; }}
    QToolButton#sidebarAction {{ border: none; border-radius: {s}px; padding: 4px 8px; background: transparent; }}
    QToolButton#sidebarAction:hover {{ background: {t.hover}; }}
    QTabBar::tab {{ padding: 4px 12px; }}
    /* A macOS segmented control (segmented.py): a light track, the chosen segment raised. */
    QFrame#segmentTrack {{ background: {t.hover}; border-radius: {m}px; }}
    QToolButton#segmentItem {{
        background: transparent; border: none; border-radius: {s}px; padding: 3px 12px; color: {t.text};
    }}
    QToolButton#segmentItem:checked {{ background: {t.raised}; font-weight: 600; }}
    QToolButton#segmentItem:hover:!checked {{ color: {t.accent}; }}
    QToolButton#segmentItem:disabled {{ color: {t.muted}; }}

    QPushButton#recordButton {{
        background: {RECORD}; color: white; border: none; border-radius: 13px;
        padding: 4px 16px; font-weight: 700; min-height: 18px;
    }}
    QPushButton#recordButton:hover {{ background: #F05A5F; }}
    QPushButton#recordButton[recording="true"] {{ background: #8F1D21; }}
    QPushButton#recordButton:disabled {{ background: {t.border}; color: {t.muted}; }}
    QPushButton#primaryButton {{
        background: {t.accent_fill}; color: white; border: none; border-radius: {m}px;
        padding: 4px 16px; font-weight: 600; min-height: 18px;
    }}
    QPushButton#primaryButton:hover {{ background: {t.accent_hover}; }}
    QPushButton#primaryButton:disabled {{ background: {t.border}; color: {t.muted}; }}
    QPushButton#warningButton {{
        background: transparent; color: {WARNING}; border: 1px solid {WARNING};
        border-radius: {m}px; padding: 4px 12px; font-weight: 600;
    }}
    QLabel#recordTime {{ font-family: "{t.mono}"; font-size: 14px; font-weight: 600; color: {RECORD}; }}
    QProgressBar#levelMeter {{ border: none; border-radius: {s}px; background: {t.border}; }}
    QProgressBar#levelMeter::chunk {{ border-radius: {s}px; background: #30A46C; }}
    QToolButton#micButton {{ padding: 4px; }}

    QLabel#muted {{ color: {t.muted}; }}
    QLabel#welcomeTitle {{ font-size: 28px; font-weight: 700; }}
    QLabel#welcomeSubtitle {{ font-size: 14px; color: {t.muted}; }}
    QLabel#lectureTitle {{ font-size: 20px; font-weight: 700; }}
    QFrame#card {{ background: {t.card}; border: 1px solid {t.border}; border-radius: {m}px; padding: 8px; }}
    QLabel#cardTitle {{ font-weight: 700; font-size: 13px; }}

    QListWidget#lectureList {{ border: none; background: transparent; }}
    QListWidget#lectureList::item {{
        background: {t.card}; border: 1px solid {t.border}; border-radius: {m}px;
        padding: 8px 12px; margin: 4px 0;
    }}
    QListWidget#lectureList::item:hover {{ border-color: {t.accent}; }}

    QTreeWidget#subjectTree {{ border: none; background: transparent; }}
    QTreeWidget#subjectTree::item {{ padding: 4px; border-radius: {s}px; }}
    QTreeWidget#subjectTree::item:hover {{ background: {t.hover}; }}
    QTreeWidget#subjectTree::item:selected {{ background: {t.accent_fill}; color: white; }}

    QTextEdit#notesEdit {{ border: none; font-size: 15px; }}
    QToolButton#activityLabel {{ border: none; padding: 0 4px; }}
    QToolButton#activityLabel:hover {{ background: {t.hover}; border-radius: {s}px; }}
    QToolButton#activityPause {{ border: none; padding: 0 4px; }}
    QProgressBar#activityBar {{ border: none; border-radius: {s}px; background: {t.border}; }}
    QProgressBar#activityBar::chunk {{ border-radius: {s}px; background: {t.accent_fill}; }}
    QFrame#activityPopup {{ background: palette(window); border: 1px solid {t.border}; border-radius: {l}px; }}
    QFrame#rule {{ color: {t.border}; }}
    QListWidget#documentList::item {{ border-radius: {s}px; }}
    QListWidget#documentList::item:hover {{ background: {t.hover}; }}
    QListWidget#documentList::item:selected {{ background: {t.hover}; border: 1px solid {t.accent}; }}
    QLabel#slidePage {{ background: white; border: 1px solid {t.border}; }}
    QWidget#playerBar {{ border-top: 1px solid {t.border}; }}
    QToolButton#playButton {{ min-width: 26px; }}
    QLabel#practiceHeader, QLabel#planLabel {{ font-weight: 600; }}
    QLabel#practiceStem {{ font-size: 15px; font-weight: 600; }}
    """


def apply(app: QApplication) -> None:
    app.setStyleSheet(stylesheet())
    # Switching macOS between light and dark while the app runs: rebuild with the new tokens.
    # Icons look their colour up when painted, so they follow by themselves.
    app.styleHints().colorSchemeChanged.connect(lambda _scheme: QTimer.singleShot(0, lambda: app.setStyleSheet(stylesheet())))
