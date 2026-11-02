"""macOS glass: a native NSVisualEffectView behind each glass widget.

Qt draws the whole window into one view, so the effect views sit *behind* it
(siblings of the content view, ordered below), sized to the widget's
rectangle. Qt leaves those pixels transparent and the blur shows through.
"""

from __future__ import annotations

import objc
from AppKit import (
    NSMakeRect,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectStateFollowsWindowActiveState,
    NSVisualEffectView,
    NSWindowBelow,
)
from PySide6.QtCore import QEvent, QObject, QPoint
from PySide6.QtWidgets import QWidget

MATERIALS = {  # NSVisualEffectMaterial, as the system uses them in its own apps
    "sidebar": 7,  # .sidebar
    "toolbar": 3,  # .titlebar
    "header": 10,  # .headerView
    "status": 3,  # .titlebar
}

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

    def sync(self) -> None:
        content = self._content_view()
        if content is None or content.superview() is None:
            return  # window not shown yet; the Show event syncs again
        if self.view is None:
            self.view = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 0))
            self.view.setMaterial_(self.material)
            self.view.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
            self.view.setState_(NSVisualEffectStateFollowsWindowActiveState)
            content.superview().addSubview_positioned_relativeTo_(self.view, NSWindowBelow, content)
        top = self.widget.window()
        origin = self.widget.mapTo(top, QPoint(0, 0))
        rect = NSMakeRect(origin.x(), origin.y(), self.widget.width(), self.widget.height())
        # Qt's content view is flipped (y down); its superview is not.
        self.view.setFrame_(content.convertRect_toView_(rect, content.superview()))
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
