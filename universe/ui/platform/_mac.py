"""macOS glass: a native NSVisualEffectView behind each glass widget.

Qt draws the whole window into one view, so the effect views sit *behind* it,
sized to the widget's rectangle; Qt leaves those pixels transparent and the
blur shows through. They live inside the window's own background effect view
(the frame view's first subview, below the content). Adding them to the frame
view itself is unsupported: macOS then drops its title bar layout and squeezes
the title under the window buttons.
"""

from __future__ import annotations

import objc
from AppKit import (
    NSMakeRect,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectStateFollowsWindowActiveState,
    NSVisualEffectView,
)
from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtWidgets import QWidget

MATERIALS = {  # NSVisualEffectMaterial, as the system uses them in its own apps
    "sidebar": 7,  # .sidebar
    "toolbar": 3,  # .titlebar
    "header": 10,  # .headerView
    "status": 3,  # .titlebar
}

class UniVerseGlassView(NSVisualEffectView):
    """A blur view that never takes the mouse.

    It sits at the window's edges; if it answered hit tests, the window frame would
    not get the clicks it needs for resizing (e.g. dragging the top-left corner).
    """

    def hitTest_(self, point):
        return None


_TRACKED = (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.LayoutRequest)


class _Tracker(QObject):
    """Keeps one effect view on top of (well, behind) its widget's rectangle."""

    def __init__(self, widget: QWidget, material: int) -> None:
        super().__init__(widget)
        self.widget = widget
        self.material = material
        self.view = None  # created once the window has a native view
        self.watched: list[QWidget] = []
        node = widget
        while node is not None:  # an ancestor moving moves the widget without telling it
            node.installEventFilter(self)
            self.watched.append(node)
            node = node.parentWidget()
        widget.destroyed.connect(lambda *_: self._remove())
        self.sync()

    def eventFilter(self, obj, event) -> bool:
        if event.type() in _TRACKED:
            self.sync()
        return False

    def _content_view(self):
        win_id = self.widget.window().internalWinId()  # 0 until the window is created
        return objc.objc_object(c_void_p=int(win_id)) if win_id else None

    def _host(self, content):
        """The window's own background effect view: behind the content, covering the window."""
        frame = content.superview()
        below = list(frame.subviews())[: list(frame.subviews()).index(content)]
        return next((v for v in below if v.isKindOfClass_(NSVisualEffectView)), None)

    def sync(self) -> None:
        content = self._content_view()
        if content is None or content.superview() is None:
            return  # window not shown yet; the Show event syncs again
        if self.view is None:
            host = self._host(content)
            if host is None:  # an older macOS without one: solid, never a see-through hole
                self.widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
                self.widget.setAutoFillBackground(True)
                return
            self.view = UniVerseGlassView.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 0))
            self.view.setMaterial_(self.material)
            self.view.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
            self.view.setState_(NSVisualEffectStateFollowsWindowActiveState)
            host.addSubview_(self.view)
        top = self.widget.window()
        origin = self.widget.mapTo(top, QPoint(0, 0))
        rect = NSMakeRect(origin.x(), origin.y(), self.widget.width(), self.widget.height())
        # Qt's content view is flipped (y down); the host is not.
        self.view.setFrame_(content.convertRect_toView_(rect, self.view.superview()))
        self.view.setHidden_(not self.widget.isVisible())

    def _remove(self, *_) -> None:
        if self.view is not None:
            self.view.removeFromSuperview()
            self.view = None


def attach(widget: QWidget, role: str) -> None:
    old = getattr(widget, "_glass_tracker", None)
    if old is not None:
        old._remove()
    widget._glass_tracker = _Tracker(widget, MATERIALS[role])
