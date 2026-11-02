"""NSGlassEffectView capsule over a Qt widget (see floating.py)."""

from __future__ import annotations

import objc
from AppKit import (
    NSBox,
    NSBoxSeparator,
    NSButton,
    NSFont,
    NSGlassEffectView,
    NSEvent,
    NSObject,
    NSSlider,
    NSStackView,
    NSTextField,
    NSUserInterfaceLayoutOrientationHorizontal,
)
from PySide6.QtCore import QEvent, QObject, QPoint
from PySide6.QtGui import QCursor

from ._mac_toolbar import _ns_color, _symbol
from .floating import DIVIDER, LOW, MUST, Button, Menu, Slider, Text

HEIGHT = 36  # points; the capsule's corners are half of it
MARGIN = 14  # above the host's bottom edge
TRACKED = {QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide}


class _Target(NSObject):
    def init(self):
        self = objc.super(_Target, self).init()
        self.handlers = {}
        return self

    def clicked_(self, sender):
        handler = self.handlers.get(sender.tag())
        if handler is not None:
            handler()


class GlassBar(QObject):
    def __init__(self, host, items: list, visible):
        super().__init__(host)
        self.host = host
        self.items = items
        self.visible = visible
        self.glass = None
        self.target = _Target.alloc().init()
        self._refreshers = []
        self._layout_key = None  # what the last fitting was for
        widget = host
        while widget is not None:  # a move of any parent (a splitter drag) moves the host on screen
            widget.installEventFilter(self)
            widget = widget.parentWidget()

    def eventFilter(self, obj, event) -> bool:
        if event.type() in TRACKED:
            if self.glass is None and event.type() == QEvent.Type.Show and self.host.isVisible():
                self._build()
            self.place()
        elif event.type() == QEvent.Type.EnabledChange:
            self.refresh()
        return False

    def _build(self) -> None:
        content = objc.objc_object(c_void_p=int(self.host.window().winId()))
        stack = NSStackView.alloc().init()
        stack.setOrientation_(NSUserInterfaceLayoutOrientationHorizontal)
        stack.setSpacing_(10)
        stack.setEdgeInsets_((0, 14, 0, 14))
        stack.setDetachesHiddenViews_(True)  # a hidden item gives its room back
        self.views = []
        for tag, spec in enumerate(self.items):
            view = self._view(tag, spec)
            stack.addArrangedSubview_(view)
            self.views.append((LOW if spec == DIVIDER else spec.keep, tag, view))
        self.glass = NSGlassEffectView.alloc().init()
        self.glass.setCornerRadius_(HEIGHT / 2)
        self.glass.setContentView_(stack)
        self.stack = stack
        content.addSubview_(self.glass)  # above everything Qt draws in the window
        self.refresh()

    def _view(self, tag: int, spec):
        if spec == DIVIDER:
            line = NSBox.alloc().initWithFrame_(((0, 0), (1, 18)))
            line.setBoxType_(NSBoxSeparator)
            return line
        if isinstance(spec, Slider):
            return self._slider(tag, spec)
        if isinstance(spec, Text):
            label = NSTextField.labelWithString_("")
            label.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(12, 0))
            label.setToolTip_(spec.tooltip)
            if spec.on_click is None:
                self._refreshers.append(lambda: label.setStringValue_(spec.text()))
                for signal in spec.changed:
                    signal.connect(self.refresh)
                return label
            # Clickable text (e.g. "3 / 12" to jump to a page): a borderless button.
            button = NSButton.buttonWithTitle_target_action_("", self.target, "clicked:")
            button.setBordered_(False)
            button.setFont_(label.font())
            button.setToolTip_(spec.tooltip)
            button.setTag_(tag)
            self.target.handlers[tag] = spec.on_click
            self._refreshers.append(lambda: button.setTitle_(spec.text()))
            for signal in spec.changed:
                signal.connect(self.refresh)
            return button
        symbol = spec.symbol if isinstance(spec.symbol, str) else spec.symbol()
        button = NSButton.buttonWithImage_target_action_(_symbol(symbol, spec.tooltip if isinstance(spec, Menu) else ""), self.target, "clicked:")
        button.setBordered_(False)
        button.setTag_(tag)
        if isinstance(spec, Menu):
            button.setToolTip_(spec.tooltip)
            self.target.handlers[tag] = lambda: spec.menu().popup(QCursor.pos())
            return button
        mirror = spec.mirror
        self.target.handlers[tag] = mirror.click
        mirror.installEventFilter(self)  # enabled changes
        if mirror.isCheckable():
            mirror.toggled.connect(self.refresh)

        shown_symbol = [symbol]

        def refresh() -> None:
            if not isinstance(spec.symbol, str) and spec.symbol() != shown_symbol[0]:
                shown_symbol[0] = spec.symbol()
                button.setImage_(_symbol(shown_symbol[0], ""))
            button.setEnabled_(mirror.isEnabled())
            button.setToolTip_(mirror.toolTip())
            # VoiceOver reads the tool's name, not the symbol's ("scissors").
            button.setAccessibilityLabel_(mirror.text() or mirror.toolTip().split("\n")[0].split(":")[0])
            # A tool that is on shows in the accent colour, like a selected toolbar item.
            button.setContentTintColor_(_ns_color("accent") if mirror.isChecked() else None)

        self._refreshers.append(refresh)
        return button

    def _slider(self, tag: int, spec: Slider):
        slider = NSSlider.sliderWithTarget_action_(self.target, "clicked:")
        slider.setContinuous_(False)  # one seek when you let go, not dozens while dragging
        slider.setControlSize_(1)  # small
        slider.setTag_(tag)
        slider.widthAnchor().constraintEqualToConstant_(spec.width).setActive_(True)
        slider.setAccessibilityLabel_("Position")
        if hasattr(slider, "setTrackFillColor_"):
            slider.setTrackFillColor_(_ns_color("accent"))
        self.target.handlers[tag] = lambda: spec.released(int(slider.intValue()))

        def refresh() -> None:
            if NSEvent.pressedMouseButtons() & 1:  # don't pull the knob out from under the pointer
                return
            slider.setMinValue_(spec.mirror.minimum())
            slider.setMaxValue_(max(spec.mirror.maximum(), spec.mirror.minimum() + 1))
            slider.setIntValue_(spec.mirror.value())

        self._refreshers.append(refresh)
        return slider

    def refresh(self) -> None:
        if self.glass is None:
            return
        for refresh in self._refreshers:
            refresh()
        self.place()

    def place(self) -> None:
        if self.glass is None:
            return
        host = self.host
        shown = host.isVisible() and self.visible()
        self.glass.setHidden_(not shown)
        if not shown:
            return
        room = host.width() - 2 * MARGIN
        # Fitting is only redone when the room or a text changed, not on every playback tick.
        key = (room, tuple(len(spec.text()) for spec in self.items if isinstance(spec, Text)))
        if key != self._layout_key:
            self._layout_key = key
            # Too narrow for all: leave out what matters least, the later of equals first.
            for _keep, _tag, view in self.views:
                view.setHidden_(False)
            for keep, _tag, view in sorted(self.views, key=lambda v: (v[0], -v[1])):
                if self.stack.fittingSize().width <= room or keep >= MUST:
                    break
                view.setHidden_(True)
            self._width = min(self.stack.fittingSize().width, room)
        width = self._width
        corner = host.mapTo(host.window(), QPoint(0, 0))  # the window's view is flipped: y down, like Qt
        x = corner.x() + (host.width() - width) / 2
        y = corner.y() + host.height() - HEIGHT - MARGIN
        self.glass.setFrame_(((x, y), (width, HEIGHT)))
