"""Controls that float over content in a Liquid Glass capsule (macOS 26), like Preview's.

Glass goes on controls above the material, never behind text you read: the
slide scrolls under the capsule and shows through it, blurred. Each item
mirrors a Qt control that stays in the widget tree (hidden), so the logic,
shortcuts and tests keep working on the Qt side. Without native glass the
Qt controls simply stay visible.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import SignalInstance
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QAbstractButton, QWidget


# How much an item wants to stay when the capsule is narrower than everything (the
# rest is still in the Qt widget's menus and shortcuts). macOS drops the lowest first.
MUST, HIGH, LOW = 1000, 750, 250


@dataclass
class Button:
    symbol: str  # SF Symbol name
    mirror: QAbstractButton  # clicking the glass button clicks this; its checked/enabled/tooltip show
    keep: int = HIGH


@dataclass
class Text:
    text: Callable[[], str]
    changed: list[SignalInstance] = field(default_factory=list)  # when to read ``text`` again
    tooltip: str = ""
    on_click: Callable[[], None] | None = None
    keep: int = MUST


@dataclass
class Menu:
    symbol: str
    tooltip: str
    menu: Callable  # returns the QMenu to pop up
    keep: int = LOW


DIVIDER = "divider"


def available() -> bool:
    if sys.platform != "darwin" or QGuiApplication.platformName() != "cocoa":
        return False
    try:
        from . import _mac_floating  # noqa: F401
    except ImportError:  # pyobjc missing, or a macOS without NSGlassEffectView
        return False
    return True


def attach(host: QWidget, items: list, visible: Callable[[], bool] = lambda: True):
    """Float ``items`` at the bottom centre of ``host``; returns an object with ``refresh()``."""
    from . import _mac_floating

    return _mac_floating.GlassBar(host, items, visible)
