"""The app's only animation: a short cross-fade when the layout changes.

Used when you switch a mode or an inspector pane, or show or hide a part.
Nothing moves on hover and nothing loops. macOS "Reduce motion" turns it off.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, Qt
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QWidget

from . import platform

FADE_MS = 150


def enabled() -> bool:
    return not platform.reduce_motion()


def crossfade(widget: QWidget, change: Callable[[], None]) -> None:
    """Run ``change`` and fade from how ``widget`` looked before to how it looks after.

    A snapshot of the old look lies on top and fades out, so only one small
    label is animated, never the panes themselves.
    """
    old = getattr(widget, "_fade", None)
    if old is not None:  # a second change while fading: skip ahead
        old.stop()
        old.finished.emit()
    if not widget.isVisible() or not enabled():
        change()
        return
    snapshot = widget.grab()
    change()
    # On the window, not on the widget: a QSplitter would take a new child as one more column.
    window = widget.window()
    cover = QLabel(window)
    cover.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)  # clicks go through while it fades
    cover.setPixmap(snapshot)
    cover.setGeometry(QRect(widget.mapTo(window, QPoint(0, 0)), widget.size()))
    cover.show()
    cover.raise_()
    effect = QGraphicsOpacityEffect(cover)
    cover.setGraphicsEffect(effect)
    fade = QPropertyAnimation(effect, b"opacity", cover)
    fade.setDuration(FADE_MS)
    fade.setStartValue(1.0)
    fade.setEndValue(0.0)
    fade.setEasingCurve(QEasingCurve.Type.OutCubic)

    def done() -> None:
        widget._fade = None
        cover.deleteLater()

    fade.finished.connect(done)
    widget._fade = fade
    fade.start()
