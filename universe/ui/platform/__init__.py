"""Operating-system specifics for the window chrome, behind one small interface.

Panels only call :mod:`.glass` and the accessibility checks here; each OS gets
its own backend (``_mac`` today, Windows later). Without a backend everything
falls back to solid, ordinary Qt widgets, so the app always stays usable.
"""

from __future__ import annotations

import sys


def _workspace():
    if sys.platform != "darwin":
        return None
    try:
        from AppKit import NSWorkspace
    except ImportError:  # pyobjc missing
        return None
    return NSWorkspace.sharedWorkspace()


def reduce_transparency() -> bool:
    """The user asked the OS for solid surfaces (macOS: Accessibility → Display)."""
    workspace = _workspace()
    return bool(workspace and workspace.accessibilityDisplayShouldReduceTransparency())


def reduce_motion() -> bool:
    """The user asked the OS to cut animations; skip ours too."""
    workspace = _workspace()
    return bool(workspace and workspace.accessibilityDisplayShouldReduceMotion())
