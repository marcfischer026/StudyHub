"""Lucide line icons (ISC licence, see ``assets/icons/LICENSE``), tinted at load.

The SVGs draw with ``currentColor``; :func:`icon` swaps in the theme colour
and renders sharp pixmaps for normal and Retina screens, plus a muted copy for
disabled buttons. Only the icons the app uses are bundled; to add one, copy its
SVG from the ``lucide-static`` package into ``assets/icons/``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

ICON_DIR = Path(__file__).parent / "assets" / "icons"
STROKE = 1.5  # Lucide draws at 2; thinner sits closer to the system symbols at 16 px
SIZE = 16  # chrome; inline icons use 14


def names() -> list[str]:
    return sorted(path.stem for path in ICON_DIR.glob("*.svg"))


@lru_cache(maxsize=None)
def _svg(name: str) -> str:
    path = ICON_DIR / f"{name}.svg"
    if not path.exists():
        raise KeyError(f"No icon “{name}” in {ICON_DIR}")
    return path.read_text(encoding="utf-8").replace('stroke-width="2"', f'stroke-width="{STROKE}"')


def pixmap(name: str, color: QColor | str, size: int = SIZE, ratio: float = 2.0) -> QPixmap:
    """One tinted rendering of ``name``, ``size`` logical pixels square."""
    color = QColor(color)
    svg = _svg(name).replace("currentColor", color.name())  # SVG wants #RRGGBB; alpha goes on the painter
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    image = QPixmap(round(size * ratio), round(size * ratio))
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setOpacity(color.alphaF())
    renderer.render(painter)
    painter.end()
    image.setDevicePixelRatio(ratio)
    return image


def _palette_color(role: QPalette.ColorRole, group: QPalette.ColorGroup = QPalette.ColorGroup.Active) -> QColor:
    return QGuiApplication.palette().color(group, role)


@lru_cache(maxsize=512)
def _icon(name: str, color: str, disabled: str, size: int) -> QIcon:
    result = QIcon()
    for ratio in (1.0, 2.0):
        result.addPixmap(pixmap(name, color, size, ratio), QIcon.Mode.Normal)
        result.addPixmap(pixmap(name, disabled, size, ratio), QIcon.Mode.Disabled)
    return result


def icon(name: str, color: QColor | str | None = None, size: int = SIZE) -> QIcon:
    """Icon ``name`` (Lucide file name without ``.svg``) in ``color``.

    Without a colour it follows the text colour of the current light/dark
    palette, so it reads like the label next to it.
    """
    normal = QColor(color) if color is not None else _palette_color(QPalette.ColorRole.WindowText)
    disabled = _palette_color(QPalette.ColorRole.WindowText, QPalette.ColorGroup.Disabled)
    hexargb = QColor.NameFormat.HexArgb
    return _icon(name, normal.name(hexargb), disabled.name(hexargb), size)
