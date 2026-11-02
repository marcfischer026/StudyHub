"""A macOS-style segmented control in place of a QTabWidget's tab bar.

On macOS Qt draws tab bars as a full-width strip with dividers; Apple's own apps
use a small rounded track with the chosen segment raised. This puts that above
the tab widget and hides its own bar, so ``tabs.setCurrentWidget`` etc. still work.
"""

from __future__ import annotations

from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QSizePolicy, QTabWidget, QToolButton, QWidget


class SegmentedTabs(QWidget):
    def __init__(self, tabs: QTabWidget, parent=None):
        super().__init__(parent)
        self.tabs = tabs
        tabs.tabBar().hide()
        tabs.setDocumentMode(True)  # no frame around the pages either
        self.group = QButtonGroup(self, exclusive=True)
        track = QFrame(objectName="segmentTrack")
        track.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        inner = QHBoxLayout(track)
        inner.setContentsMargins(2, 2, 2, 2)
        inner.setSpacing(2)
        for index in range(tabs.count()):
            button = QToolButton(objectName="segmentItem", text=tabs.tabText(index), checkable=True)
            button.setToolTip(tabs.tabToolTip(index))
            self.group.addButton(button, index)
            inner.addWidget(button)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch()
        row.addWidget(track)
        row.addStretch()
        self.group.idClicked.connect(tabs.setCurrentIndex)
        tabs.currentChanged.connect(self._follow)
        self._follow(tabs.currentIndex())

    def _follow(self, index: int) -> None:
        button = self.group.button(index)
        if button is not None:
            button.setChecked(True)
