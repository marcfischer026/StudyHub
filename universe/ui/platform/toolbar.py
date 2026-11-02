"""A native toolbar for the main window where the OS has one (macOS: NSToolbar).

On macOS 26 an NSToolbar gets Liquid Glass by itself: buttons in glass
capsules, SF Symbols, the window content running up under it. The toolbar is
only a view: every item drives a QAction that menus and shortcuts share, and
follows that action's enabled/checked state. Elsewhere (and in tests) the
window keeps its Qt toolbar.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtGui import QAction, QGuiApplication


@dataclass
class Button:
    key: str
    symbol: str  # SF Symbol name
    action: QAction
    symbol_when_checked: str = ""  # e.g. stop instead of record while recording
    tint: str = ""  # "red" | "accent" | "": colour of the symbol
    prominent: bool = False  # the one primary action: filled with the accent


@dataclass
class Group:
    """One-of-several choice, like the workspace modes."""

    key: str
    choices: list[tuple[str, QAction]] = field(default_factory=list)  # (SF Symbol, action)


@dataclass
class Menu:
    """A button that opens a Qt menu (built when clicked)."""

    key: str
    symbol: str
    label: str
    menu: Callable


SPACE = "space"  # flexible space between the left and right clusters


def available() -> bool:
    if sys.platform != "darwin" or QGuiApplication.platformName() != "cocoa":
        return False
    try:
        from . import _mac_toolbar  # noqa: F401
    except ImportError:  # pyobjc missing: the Qt toolbar stays
        return False
    return True


def install(window, items: list) -> object:
    """Put ``items`` into ``window``'s native toolbar; returns the object to keep alive."""
    from . import _mac_toolbar

    return _mac_toolbar.NativeToolbar(window, items)
