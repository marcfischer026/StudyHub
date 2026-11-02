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
def _icon(name: str, color: str, disabled: str, on: str, size: int) -> QIcon:
    result = QIcon()
    for ratio in (1.0, 2.0):
        result.addPixmap(pixmap(name, color, size, ratio), QIcon.Mode.Normal, QIcon.State.Off)
        result.addPixmap(pixmap(name, on or color, size, ratio), QIcon.Mode.Normal, QIcon.State.On)
        result.addPixmap(pixmap(name, disabled, size, ratio), QIcon.Mode.Disabled)
        result.addPixmap(pixmap(name, "white", size, ratio), QIcon.Mode.Selected)  # on the accent highlight
    return result


def icon(name: str, color: QColor | str | None = None, size: int = SIZE, on_color: QColor | str | None = None) -> QIcon:
    """Icon ``name`` (Lucide file name without ``.svg``) in ``color``.

    Without a colour it follows the text colour of the current light/dark
    palette, so it reads like the label next to it. ``on_color`` tints it on
    checked buttons (e.g. the selected inspector pane).
    """
    normal = QColor(color) if color is not None else _palette_color(QPalette.ColorRole.WindowText)
    disabled = _palette_color(QPalette.ColorRole.WindowText, QPalette.ColorGroup.Disabled)
    hexargb = QColor.NameFormat.HexArgb
    on = QColor(on_color).name(hexargb) if on_color is not None else ""
    return _icon(name, normal.name(hexargb), disabled.name(hexargb), on, size)


@lru_cache(maxsize=64)
def _dot(color: str, size: int) -> QIcon:
    result = QIcon()
    for ratio in (1.0, 2.0):
        image = QPixmap(round(size * ratio), round(size * ratio))
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        inset = 3 * ratio  # a small dot centred in the icon box, like a status light
        painter.drawEllipse(image.rect().adjusted(inset, inset, -inset, -inset))
        painter.end()
        image.setDevicePixelRatio(ratio)
        result.addPixmap(image)
    return result


def dot(color: QColor | str, size: int = SIZE) -> QIcon:
    """A filled status light (e.g. microphone check: green clear, red too quiet)."""
    return _dot(QColor(color).name(QColor.NameFormat.HexArgb), size)
