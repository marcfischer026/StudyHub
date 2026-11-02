"""The window's three columns: sidebar, centre (notes beside slides), inspector.

Replaces the old floating docks. Each part can be shown or hidden and the
dividers dragged. Workspace modes (Lecture, Read, Review, Exam) are presets for
what you are doing; each remembers your own changes until you reset it.
:meth:`Workbench.state` and :meth:`Workbench.restore` keep all of it between
launches. No free docking, no floating windows (see ``docs/ui-overhaul.md``).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QSplitter, QStackedWidget, QToolButton, QVBoxLayout, QWidget

from . import icons, motion, style

PARTS = ("sidebar", "notes", "documents", "inspector")
STEP_ASIDE = ("inspector", "sidebar", "documents", "notes")  # first steps aside first in a narrow window
SIDEBAR_WIDTH = 240
PANEL_GAP = 6  # between panels (the drag handle) and to the window edge


class Panel(QWidget):
    """A part floating on the window background: rounded, a light rim, a soft shadow below.

    The look of the macOS 26 sidebar and inspector, drawn by Qt so text stays
    sharp and solid. The card takes the content's own background colour, so
    the content's square corners, kept inside the inset, don't show.
    """

    SHADOW = 3  # px below the card for the shadow
    SIDE = 1  # px left and right for the rim's anti-aliasing

    def __init__(self, content: QWidget, parent=None):
        super().__init__(parent)
        self.content = content
        layout = QVBoxLayout(self)
        inset = style.PANEL_INSET
        layout.setContentsMargins(self.SIDE + inset, inset, self.SIDE + inset, self.SHADOW + inset)
        layout.addWidget(content)

    def paintEvent(self, event) -> None:
        t = style.tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = QRectF(self.rect()).adjusted(self.SIDE + 0.5, 0.5, -self.SIDE - 0.5, -self.SHADOW - 0.5)
        path = QPainterPath()
        path.addRoundedRect(card, style.RADIUS_PANEL, style.RADIUS_PANEL)
        painter.setPen(Qt.PenStyle.NoPen)
        for offset, alpha in ((3, 6), (2, 10), (1, 16)):  # soft, short, straight down
            painter.setBrush(QColor(0, 0, 0, alpha * (3 if t.dark else 1)))
            painter.drawPath(path.translated(0, offset))
        painter.setBrush(self.palette().color(QPalette.ColorRole.Base))
        painter.setPen(QColor(t.rim))
        painter.drawPath(path)


@dataclass(frozen=True)
class Mode:
    title: str
    icon: str
    hint: str
    hidden: tuple[str, ...]  # parts this mode starts without
    center: tuple[int, int]  # notes : slides
    inspector_width: int
    pane: str  # inspector pane in front


MODES = {
    "lecture": Mode("Lecture", "notebook-pen", "Notes beside the slides, live transcript", (), (3, 2), 380, "transcript"),
    "read": Mode("Read", "book-open", "Slides large, notes narrow, nothing else", ("sidebar", "inspector"), (1, 2), 380, "transcript"),
    "review": Mode("Review", "history", "Notes with transcript and playback", ("documents",), (1, 0), 480, "transcript"),
    "exam": Mode("Exam", "clipboard-check", "Practice and Claude beside your notes", ("documents",), (1, 0), 520, "ai"),
}
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
        button.setIcon(icons.icon(icon, on_color=icons.ACCENT))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.group.addButton(button, len(self.keys))
        self.segments.insertWidget(self.segments.count() - 1, button)
        self.keys.append(key)
        self.titles[key] = title
        self.stack.addWidget(widget)
        if len(self.keys) == 1:
            self.show_pane(key)

    def show_pane(self, key: str, animate: bool = True) -> None:
        index = self.keys.index(key)
        self.group.button(index).setChecked(True)
        if self.stack.currentIndex() != index:
            if animate:
                motion.crossfade(self.stack, lambda: self.stack.setCurrentIndex(index))
            else:
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
    modeChanged = Signal(str, bool)  # mode, started from its preset

    def __init__(self, sidebar: QWidget, notes: QWidget, documents: QWidget, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setObjectName("workbench")
        self.setHandleWidth(PANEL_GAP)
        self.setContentsMargins(PANEL_GAP, 0, PANEL_GAP, PANEL_GAP)
        self.inspector = Inspector()
        self.center = QSplitter(Qt.Orientation.Horizontal, objectName="centerSplit")
        self.center.setHandleWidth(PANEL_GAP)
        self.parts = {"sidebar": sidebar, "notes": notes, "documents": documents, "inspector": self.inspector}
        self.frames = {part: Panel(widget) for part, widget in self.parts.items()}  # what the splitters hold
        self.center.addWidget(self.frames["notes"])
        self.center.addWidget(self.frames["documents"])
        self.wanted = dict.fromkeys(PARTS, True)
        self.priority = list(STEP_ASIDE)
        self.mode = "lecture"
        self.saved: dict[str, dict] = {}  # mode -> your changes to it (see _snapshot)
        for widget in (self.frames["sidebar"], self.center, self.frames["inspector"]):
            self.addWidget(widget)
        for splitter in (self, self.center):
            splitter.setChildrenCollapsible(False)  # hiding is explicit, via View or the toolbar
        self.center.setStretchFactor(0, 3)
        self.center.setStretchFactor(1, 2)
        self.reset()

    def is_shown(self, part: str) -> bool:
        return not self.frames[part].isHidden()

    def set_shown(self, part: str, visible: bool) -> None:
        """Show or hide a part because the user asked to."""
        if self.wanted[part] == visible and self.is_shown(part) == visible:
            return
        self.wanted[part] = visible
        if visible:  # asked for last: the last to give way
            self.priority.remove(part)
            self.priority.append(part)
        motion.crossfade(self, self._fit)

    def show_pane(self, key: str) -> None:
        """Bring an inspector pane to the front, opening the inspector if needed."""
        self.set_shown("inspector", True)
        self.inspector.show_pane(key)

    def reset(self) -> None:
        """Every mode back to its preset; Lecture in front."""
        self.saved = {}
        self.mode = "lecture"
        self.set_inspector_left(False)
        self.set_stacked(False)
        self._apply_preset(self.mode)

    # --- workspace modes ------------------------------------------------------

    def set_mode(self, key: str) -> None:
        if key == self.mode:
            return
        self.saved[self.mode] = self._snapshot()
        self.mode = key
        fresh = key not in self.saved
        # Instant, like a segmented control: a fade here felt like lag.
        self._rearrange(lambda: self._apply_preset(key) if fresh else self._restore_snapshot(self.saved[key]))
        self.modeChanged.emit(key, fresh)

    def reset_mode(self) -> None:
        """Forget your changes to the current mode."""
        self.saved.pop(self.mode, None)
        self._rearrange(lambda: self._apply_preset(self.mode))
        self.modeChanged.emit(self.mode, True)

    def _rearrange(self, change) -> None:
        """Several resizes and show/hides, painted once."""
        window = self.window()
        window.setUpdatesEnabled(False)
        try:
            change()
        finally:
            window.setUpdatesEnabled(True)

    def _apply_preset(self, key: str) -> None:
        mode = MODES[key]
        self.wanted = {part: part not in mode.hidden for part in PARTS}
        self.priority = list(STEP_ASIDE)
        self._fit()
        total = self.width() if self.isVisible() else 1440
        middle = max(total - SIDEBAR_WIDTH - mode.inspector_width, 400)
        self._set_column_sizes(SIDEBAR_WIDTH, middle, mode.inspector_width)
        notes, slides = mode.center
        self.center.setSizes([middle * notes // (notes + slides) or 1, middle * slides // (notes + slides) or 1])
        if mode.pane in self.inspector.keys:
            self.inspector.show_pane(mode.pane, animate=False)

    def _set_column_sizes(self, sidebar: int, center: int, inspector: int) -> None:
        width = {self.frames["sidebar"]: sidebar, self.center: center, self.frames["inspector"]: inspector}
        self.setSizes([width[self.widget(i)] for i in range(self.count())])

    # --- the two low-key layout options -----------------------------------------

    def inspector_left(self) -> bool:
        return self.indexOf(self.frames["inspector"]) < self.indexOf(self.center)

    def set_inspector_left(self, left: bool) -> None:
        """Inspector between sidebar and centre instead of on the right edge."""
        if left != self.inspector_left():
            sizes = dict(zip((self.widget(i) for i in range(self.count())), self.sizes()))
            self.insertWidget(1 if left else 2, self.frames["inspector"])
            self.setSizes([sizes[self.widget(i)] for i in range(self.count())])
        for i in range(self.count()):  # only the centre grows with the window
            self.setStretchFactor(i, int(self.widget(i) is self.center))

    def stacked(self) -> bool:
        return self.center.orientation() == Qt.Orientation.Vertical

    def set_stacked(self, stacked: bool) -> None:
        """Slides below the notes instead of beside them."""
        self.center.setOrientation(Qt.Orientation.Vertical if stacked else Qt.Orientation.Horizontal)

    # --- fitting the window ---------------------------------------------------

    def _needs(self, parts: list[str]) -> int:
        widths = [self.frames[p].minimumSizeHint().width() for p in parts]
        return sum(widths) + self.handleWidth() * max(len(parts) - 1, 0)

    def _fit(self) -> None:
        on = [p for p in self.priority if self.wanted[p]]
        if self.isVisible():  # before the first show the width means nothing yet
            while len(on) > 1 and self._needs(on) > self.width():
                on.pop(0)
        for part in PARTS:
            visible = part in on
            if self.is_shown(part) != visible:
                self.frames[part].setVisible(visible)
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
        return QSize(max(self.frames[p].minimumSizeHint().width() for p in PARTS), hint.height())

    # --- kept between launches ------------------------------------------------

    def _snapshot(self) -> dict:
        encode = lambda data: base64.b64encode(bytes(data)).decode()  # noqa: E731
        return {
            "main": encode(self.saveState()),
            "center": encode(self.center.saveState()),
            "shown": dict(self.wanted),
            "pane": self.inspector.current(),
        }

    def _restore_snapshot(self, snapshot: dict) -> None:
        stacked = self.stacked()  # a global choice; restoreState would bring back the saved one
        try:
            if snapshot.get("main"):
                self.restoreState(QByteArray(base64.b64decode(snapshot["main"])))
            if snapshot.get("center"):
                self.center.restoreState(QByteArray(base64.b64decode(snapshot["center"])))
        except (ValueError, TypeError):
            pass  # a broken saved layout keeps the current sizes
        self.set_stacked(stacked)
        self.wanted = {part: bool(snapshot.get("shown", {}).get(part, True)) for part in PARTS}
        self.priority = list(STEP_ASIDE)
        self._fit()
        if snapshot.get("pane") in self.inspector.keys:
            self.inspector.show_pane(snapshot["pane"], animate=False)

    def state(self) -> dict:
        saved = dict(self.saved)
        saved[self.mode] = self._snapshot()
        return {"mode": self.mode, "modes": saved, "inspector_left": self.inspector_left(), "stacked": self.stacked()}

    def restore(self, state: dict) -> None:
        if "main" in state:  # saved before modes existed: that layout was Lecture
            state = {"mode": "lecture", "modes": {"lecture": state}}
        self.set_inspector_left(bool(state.get("inspector_left")))
        stacked = bool(state.get("stacked"))
        self.saved = {k: v for k, v in state.get("modes", {}).items() if k in MODES and isinstance(v, dict)}
        self.mode = state.get("mode") if state.get("mode") in MODES else "lecture"
        if self.mode in self.saved:
            self._restore_snapshot(self.saved[self.mode])
        else:
            self._apply_preset(self.mode)
        self.set_stacked(stacked)
        self.modeChanged.emit(self.mode, False)
