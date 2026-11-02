"""Lucide line icons (ISC licence, see ``assets/icons/LICENSE``), tinted to the theme.

The SVGs draw with ``currentColor``. :func:`icon` returns a QIcon whose colour
is looked up each time it is painted, so icons follow a light/dark switch
while the app runs, stay sharp on Retina screens, go muted on disabled buttons
and white on selected rows. Only the icons the app uses are bundled; to add
one, copy its SVG from the ``lucide-static`` package into ``assets/icons/``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRect, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QIconEngine, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

ICON_DIR = Path(__file__).parent / "assets" / "icons"
STROKE = 1.5  # Lucide draws at 2; thinner sits closer to the system symbols at 16 px
SIZE = 16  # chrome; inline icons use 14
TEXT, ACCENT = "text", "accent"  # colours looked up when painted, so they follow the theme


def names() -> list[str]:
    return sorted(path.stem for path in ICON_DIR.glob("*.svg"))


@lru_cache(maxsize=None)
def _svg(name: str) -> str:
    path = ICON_DIR / f"{name}.svg"
    if not path.exists():
        raise KeyError(f"No icon “{name}” in {ICON_DIR}")
    return path.read_text(encoding="utf-8").replace('stroke-width="2"', f'stroke-width="{STROKE}"')


@lru_cache(maxsize=1024)
def _render(name: str, color: str, pixels: int) -> QPixmap:
    color = QColor(color)
    svg = _svg(name).replace("currentColor", color.name())  # SVG wants #RRGGBB; alpha goes on the painter
    image = QPixmap(pixels, pixels)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setOpacity(color.alphaF())
    QSvgRenderer(QByteArray(svg.encode())).render(painter)
    painter.end()
    return image


def pixmap(name: str, color: QColor | str, size: int = SIZE, ratio: float = 2.0) -> QPixmap:
    """One tinted rendering of ``name``, ``size`` logical pixels square."""
    image = QPixmap(_render(name, QColor(color).name(QColor.NameFormat.HexArgb), round(size * ratio)))
    image.setDevicePixelRatio(ratio)
    return image


def _resolve(color: str) -> str:
    if color == TEXT:
        return QGuiApplication.palette().color(QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText).name(QColor.NameFormat.HexArgb)
    if color == ACCENT:
        from . import style  # style is imported after the app exists; icons may be imported before

        return style.tokens().accent
    return color


class _Tinted(QIconEngine):
    def __init__(self, name: str, color: str, on_color: str):
        super().__init__()
        self.name, self.color, self.on_color = name, color, on_color

    def _color(self, mode: QIcon.Mode, state: QIcon.State) -> str:
        if mode == QIcon.Mode.Disabled:
            palette = QGuiApplication.palette()
            return palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText).name(QColor.NameFormat.HexArgb)
        if mode == QIcon.Mode.Selected:
            return "#ffffffff"  # on the accent highlight
        return _resolve(self.on_color if state == QIcon.State.On and self.on_color else self.color)

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float) -> QPixmap:
        image = QPixmap(_render(self.name, self._color(mode, state), round(min(size.width(), size.height()) * scale)))
        image.setDevicePixelRatio(scale)
        return image

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        scale = painter.device().devicePixelRatioF() if painter.device() else 1.0
        painter.drawPixmap(rect, self.scaledPixmap(rect.size(), mode, state, scale))

    def clone(self) -> QIconEngine:
        return _Tinted(self.name, self.color, self.on_color)


def _key(color: QColor | str | None, default: str) -> str:
    if color is None:
        return default
    if color in (TEXT, ACCENT):
        return color
    return QColor(color).name(QColor.NameFormat.HexArgb)


@lru_cache(maxsize=512)
def _icon(name: str, color: str, on_color: str) -> QIcon:
    _svg(name)  # unknown names fail here, not silently at paint time
    return QIcon(_Tinted(name, color, on_color))


def icon(name: str, color: QColor | str | None = None, size: int = SIZE, on_color: QColor | str | None = None) -> QIcon:
    """Icon ``name`` (Lucide file name without ``.svg``) in ``color``.

    Without a colour it follows the theme's text colour, so it reads like the
    label next to it; ``"accent"`` follows the theme's accent. ``on_color``
    tints it on checked buttons (e.g. the selected inspector pane). The size
    comes from the widget showing it; ``size`` is kept for callers' clarity.
    """
    return _icon(name, _key(color, TEXT), _key(on_color, ""))


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
