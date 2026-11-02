"""NSToolbar for a Qt main window: SF Symbols, Liquid Glass on macOS 26."""

from __future__ import annotations

import objc
from AppKit import (
    NSColor,
    NSImage,
    NSImageSymbolConfiguration,
    NSObject,
    NSToolbar,
    NSToolbarDisplayModeIconOnly,
    NSToolbarFlexibleSpaceItemIdentifier,
    NSToolbarItem,
    NSToolbarItemGroup,
    NSWindowTitleHidden,
    NSWindowToolbarStyleUnified,
)
from PySide6.QtGui import QColor, QCursor

from .. import style
from .toolbar import SPACE, Button, Group, Menu

SELECT_ONE = 0  # NSToolbarItemGroupSelectionModeSelectOne
PROMINENT = 1  # NSToolbarItemStyleProminent (macOS 26)


def _ns_color(color: str):
    if color == "red":
        return NSColor.systemRedColor()
    c = QColor(style.tokens().accent_fill if color == "accent" else color)
    return NSColor.colorWithSRGBRed_green_blue_alpha_(c.redF(), c.greenF(), c.blueF(), 1.0)


def _symbol(name: str, label: str, tint: str = ""):
    image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, label)
    if image is not None and tint:
        config = NSImageSymbolConfiguration.configurationWithPaletteColors_([_ns_color(tint)])
        image = image.imageWithSymbolConfiguration_(config)
    return image


class UniVerseToolbarDelegate(NSObject):
    def init(self):
        self = objc.super(UniVerseToolbarDelegate, self).init()
        self.items = {}
        self.order = []
        self.handlers = {}
        return self

    def toolbarAllowedItemIdentifiers_(self, toolbar):
        return self.order

    def toolbarDefaultItemIdentifiers_(self, toolbar):
        return self.order

    def toolbar_itemForItemIdentifier_willBeInsertedIntoToolbar_(self, toolbar, identifier, flag):
        return self.items.get(str(identifier))

    def clicked_(self, sender):
        handler = self.handlers.get(str(sender.itemIdentifier()))
        if handler is not None:
            handler(sender)


class NativeToolbar:
    """Keeps the NSToolbar and its delegate alive and in step with the QActions."""

    def __init__(self, window, items: list):
        view = objc.objc_object(c_void_p=int(window.winId()))
        self.nswindow = view.window()
        self.delegate = UniVerseToolbarDelegate.alloc().init()
        for spec in items:
            if spec == SPACE:
                self.delegate.order.append(NSToolbarFlexibleSpaceItemIdentifier)
            elif isinstance(spec, Button):
                self._button(spec)
            elif isinstance(spec, Group):
                self._group(spec)
            elif isinstance(spec, Menu):
                self._menu(spec)
        self.toolbar = NSToolbar.alloc().initWithIdentifier_("UniVerseMain")
        self.toolbar.setDelegate_(self.delegate)
        self.toolbar.setDisplayMode_(NSToolbarDisplayModeIconOnly)
        self.toolbar.setAllowsUserCustomization_(False)
        self.nswindow.setToolbar_(self.toolbar)
        self.nswindow.setToolbarStyle_(NSWindowToolbarStyleUnified)
        # Like Apple's own apps on macOS 26: no title text; where you are shows in the content.
        self.nswindow.setTitleVisibility_(NSWindowTitleHidden)

    def _add(self, key: str, item) -> None:
        item.setTarget_(self.delegate)
        item.setAction_("clicked:")
        item.setAutovalidates_(False)  # enabled state comes from the QAction, not AppKit
        self.delegate.items[key] = item
        self.delegate.order.append(key)

    def _button(self, spec: Button) -> None:
        item = NSToolbarItem.alloc().initWithItemIdentifier_(spec.key)
        item.setBordered_(True)
        if spec.prominent and hasattr(item, "setStyle_"):
            item.setStyle_(PROMINENT)
            item.setBackgroundTintColor_(_ns_color("accent"))
        self._add(spec.key, item)

        def refresh() -> None:
            action = spec.action
            checked = action.isCheckable() and action.isChecked()
            name = spec.symbol_when_checked if checked and spec.symbol_when_checked else spec.symbol
            label = action.text().replace("&", "")
            item.setImage_(_symbol(name, label, "white" if spec.prominent else spec.tint))
            item.setLabel_(label)
            item.setToolTip_(action.toolTip() or label)
            item.setEnabled_(action.isEnabled())

        refresh()
        spec.action.changed.connect(refresh)
        self.delegate.handlers[spec.key] = lambda _sender: spec.action.trigger()

    def _group(self, spec: Group) -> None:
        labels = [action.text().replace("&", "") for _, action in spec.choices]
        images = [_symbol(symbol, label) for (symbol, _), label in zip(spec.choices, labels)]
        item = NSToolbarItemGroup.groupWithItemIdentifier_images_selectionMode_labels_target_action_(
            spec.key, images, SELECT_ONE, labels, self.delegate, "clicked:"
        )
        for sub, (_, action) in zip(item.subitems(), spec.choices):
            sub.setToolTip_(action.toolTip() or action.text())
        self.delegate.items[spec.key] = item
        self.delegate.order.append(spec.key)

        def refresh() -> None:
            index = next((i for i, (_, a) in enumerate(spec.choices) if a.isChecked()), -1)
            if index >= 0 and item.selectedIndex() != index:
                item.setSelectedIndex_(index)

        def chosen(sender) -> None:
            index = sender.selectedIndex()
            if 0 <= index < len(spec.choices) and not spec.choices[index][1].isChecked():
                spec.choices[index][1].trigger()
            refresh()  # a refused change snaps back

        refresh()
        for _, action in spec.choices:
            action.changed.connect(refresh)
        self.delegate.handlers[spec.key] = chosen

    def _menu(self, spec: Menu) -> None:
        item = NSToolbarItem.alloc().initWithItemIdentifier_(spec.key)
        item.setBordered_(True)
        item.setImage_(_symbol(spec.symbol, spec.label))
        item.setLabel_(spec.label)
        item.setToolTip_(spec.label)
        self._add(spec.key, item)
        self.delegate.handlers[spec.key] = lambda _sender: spec.menu().popup(QCursor.pos())
