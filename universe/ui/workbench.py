"""The window's three columns: sidebar, centre (notes beside slides), inspector.

Replaces the old floating docks. Each part can be shown or hidden and the
dividers dragged; :meth:`Workbench.state` and :meth:`Workbench.restore` keep
that between launches. No free docking, no floating windows (see
``docs/ui-overhaul.md``).
"""

from __future__ import annotations

import base64

from PySide6.QtCore import QByteArray, QSize, Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QSplitter, QStackedWidget, QToolButton, QVBoxLayout, QWidget

from . import icons, style

PARTS = ("sidebar", "notes", "documents", "inspector")
DEFAULT_SIZES = {"main": [240, 820, 380], "center": [480, 340]}
ICONS_ONLY_BELOW = 300  # inspector width at which segment labels give way to icons


class Inspector(QWidget):
    """One column, several panes; a segmented control on top picks which one."""

    paneChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.header = QWidget(objectName="inspectorHeader")
        self.segments = QHBoxLayout(self.header)
        self.segments.setContentsMargins(style.SPACE[1], style.SPACE[0], style.SPACE[1], style.SPACE[0])
        self.segments.setSpacing(style.SPACE[0])
        self.segments.addStretch(1)
        self.group = QButtonGroup(self, exclusive=True)
        self.group.idClicked.connect(lambda index: self.show_pane(self.keys[index]))
        self.stack = QStackedWidget()
        self.keys: list[str] = []
        self.titles: dict[str, str] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.header)
        layout.addWidget(self.stack, 1)

    def add_pane(self, key: str, title: str, widget: QWidget, icon: str) -> None:
        button = QToolButton(objectName="segment", text=title, checkable=True, toolTip=title)
        button.setIcon(icons.icon(icon, on_color=style.tokens().accent))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.group.addButton(button, len(self.keys))
        self.segments.insertWidget(self.segments.count() - 1, button)
        self.keys.append(key)
        self.titles[key] = title
        self.stack.addWidget(widget)
        if len(self.keys) == 1:
            self.show_pane(key)

    def show_pane(self, key: str) -> None:
        index = self.keys.index(key)
        self.group.button(index).setChecked(True)
        if self.stack.currentIndex() != index:
            self.stack.setCurrentIndex(index)
            self.paneChanged.emit(key)

    def current(self) -> str:
        return self.keys[self.stack.currentIndex()] if self.keys else ""

    def resizeEvent(self, event) -> None:
        compact = event.size().width() < ICONS_ONLY_BELOW
        mode = Qt.ToolButtonStyle.ToolButtonIconOnly if compact else Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        for button in self.group.buttons():
            button.setToolButtonStyle(mode)
        super().resizeEvent(event)


class Workbench(QSplitter):
    """``sidebar | (notes | documents) | inspector``, each part optional.

    Parts you hid stay hidden. When the window is too narrow for every part you
    want (e.g. half a laptop screen), parts step aside by priority, the
    inspector first, then the sidebar, and come back once there is room. The
    part you asked for last always wins its place.
    """

    visibilityChanged = Signal(str, bool)  # part, now on screen

    def __init__(self, sidebar: QWidget, notes: QWidget, documents: QWidget, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setObjectName("workbench")
        self.setHandleWidth(1)
        self.inspector = Inspector()
        self.center = QSplitter(Qt.Orientation.Horizontal, objectName="centerSplit")
        self.center.setHandleWidth(1)
        self.center.addWidget(notes)
        self.center.addWidget(documents)
        self.parts = {"sidebar": sidebar, "notes": notes, "documents": documents, "inspector": self.inspector}
        self.wanted = dict.fromkeys(PARTS, True)
        self.priority = ["inspector", "sidebar", "documents", "notes"]  # first steps aside first
        for widget in (sidebar, self.center, self.inspector):
            self.addWidget(widget)
        for splitter in (self, self.center):
            splitter.setChildrenCollapsible(False)  # hiding is explicit, via View or the toolbar
        self.setStretchFactor(1, 1)
        self.center.setStretchFactor(0, 3)
        self.center.setStretchFactor(1, 2)
        self.reset()

    def is_shown(self, part: str) -> bool:
        return not self.parts[part].isHidden()

    def set_shown(self, part: str, visible: bool) -> None:
        """Show or hide a part because the user asked to."""
        self.wanted[part] = visible
        if visible:  # asked for last: the last to give way
            self.priority.remove(part)
            self.priority.append(part)
        self._fit()

    def show_pane(self, key: str) -> None:
        """Bring an inspector pane to the front, opening the inspector if needed."""
        self.set_shown("inspector", True)
        self.inspector.show_pane(key)

    def reset(self) -> None:
        self.wanted = dict.fromkeys(PARTS, True)
        self.priority = ["inspector", "sidebar", "documents", "notes"]
        self._fit()
        self.setSizes(DEFAULT_SIZES["main"])
        self.center.setSizes(DEFAULT_SIZES["center"])
        if self.inspector.keys:
            self.inspector.show_pane(self.inspector.keys[0])

    # --- fitting the window ---------------------------------------------------

    def _needs(self, parts: list[str]) -> int:
        widths = [self.parts[p].minimumSizeHint().width() for p in parts]
        return sum(widths) + self.handleWidth() * max(len(parts) - 1, 0)

    def _fit(self) -> None:
        on = [p for p in self.priority if self.wanted[p]]
        if self.isVisible():  # before the first show the width means nothing yet
            while len(on) > 1 and self._needs(on) > self.width():
                on.pop(0)
        for part in PARTS:
            visible = part in on
            if self.is_shown(part) != visible:
                self.parts[part].setVisible(visible)
                self.visibilityChanged.emit(part, visible)
        # The centre column goes away only when both of its parts are hidden.
        self.center.setVisible(self.is_shown("notes") or self.is_shown("documents"))

    def resizeEvent(self, event) -> None:
        self._fit()
        super().resizeEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit()

    def minimumSizeHint(self) -> QSize:
        """Narrow enough for one part: the others step aside (see :meth:`_fit`)."""
        hint = super().minimumSizeHint()
        return QSize(max(self.parts[p].minimumSizeHint().width() for p in PARTS), hint.height())

    # --- kept between launches ------------------------------------------------

    def state(self) -> dict:
        encode = lambda data: base64.b64encode(bytes(data)).decode()  # noqa: E731
        return {
            "main": encode(self.saveState()),
            "center": encode(self.center.saveState()),
            "shown": dict(self.wanted),
            "pane": self.inspector.current(),
        }

    def restore(self, state: dict) -> None:
        try:
            if state.get("main"):
                self.restoreState(QByteArray(base64.b64decode(state["main"])))
            if state.get("center"):
                self.center.restoreState(QByteArray(base64.b64decode(state["center"])))
        except (ValueError, TypeError):
            pass  # a broken saved layout falls back to the default sizes
        for part, visible in state.get("shown", {}).items():
            if part in self.wanted:
                self.wanted[part] = bool(visible)
        self._fit()
        if state.get("pane") in self.inspector.keys:
            self.inspector.show_pane(state["pane"])
