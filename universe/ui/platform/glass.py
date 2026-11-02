"""Frosted-glass surfaces for the window chrome (sidebar, toolbar, headers).

Usage, before the window is first shown::

    glass.prepare_window(main_window)        # once per window
    glass.apply(sidebar, glass.Role.SIDEBAR)  # glass behind this widget
    glass.solid(center)                       # everything else paints opaque

Reading surfaces (notes, slides, transcript) are never glass. When glass is
unavailable (not macOS, pyobjc missing, "Reduce transparency" on, or the Qt
platform isn't Cocoa) the same calls paint plain window-coloured surfaces.
"""

from __future__ import annotations

import sys
from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QPalette
from PySide6.QtWidgets import QWidget

from . import reduce_transparency


class Role(Enum):
    SIDEBAR = "sidebar"
    TOOLBAR = "toolbar"
    HEADER = "header"  # inspector header strip
    STATUS = "status"


def _backend():
    if sys.platform != "darwin" or QGuiApplication.platformName() != "cocoa" or reduce_transparency():
        return None
    try:
        from . import _mac
    except ImportError:  # pyobjc missing: solid chrome, same layout
        return None
    return _mac


def available() -> bool:
    return _backend() is not None


def prepare_window(window: QWidget) -> bool:
    """Let ``window`` show glass. Must run before the window is first shown.

    With glass, the window background becomes see-through, so every area that
    isn't glass has to be marked :func:`solid`. Returns whether glass is on.
    """
    if _backend() is None:
        return False
    window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    window.setProperty("glass", True)
    return True


def apply(widget: QWidget, role: Role) -> bool:
    """Put glass behind ``widget``; returns False when it fell back to solid."""
    backend = _backend()
    if backend is None or not widget.window().property("glass"):
        solid(widget)
        return False
    widget.setAutoFillBackground(False)
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    widget.setProperty("glassRole", role.value)  # stylesheet hook: transparent backgrounds
    backend.attach(widget, role.value)
    return True


def solid(widget: QWidget) -> None:
    """Paint ``widget`` in the opaque window colour (also the no-glass fallback)."""
    widget.setBackgroundRole(QPalette.ColorRole.Window)
    widget.setAutoFillBackground(True)
