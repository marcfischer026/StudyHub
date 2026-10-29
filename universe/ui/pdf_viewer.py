"""Slide / PDF viewer rendered with PyMuPDF.

All pages sit in one continuous, scrollable column (like Preview). Pages are
rendered lazily — only those near the viewport hold a pixmap — so long slide
decks stay fast and light on memory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pymupdf
from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..storage import import_file

EMPTY_TEXT = "No {noun} yet.\n\nClick “Import PDF…” or drag a PDF file here."
PAGE_GAP = 12
KEEP_RENDERED = 6  # pages beyond this distance from the viewport drop their pixmap


class PdfViewer(QWidget):
    pdfImported = Signal(object)  # Path

    def __init__(self, ensure_folder: Callable[[str], Path | None], noun: str = "slides", parent=None):
        """Shows the PDFs in one folder. ``ensure_folder(name_hint)`` returns the
        folder imports go to — e.g. creating a lecture named after the PDF when
        none is open — or ``None`` if there is none (the user cancelled)."""
        super().__init__(parent)
        self.folder: Path | None = None
        self.ensure_folder = ensure_folder
        self.noun = noun
        self.empty_text = EMPTY_TEXT.format(noun=noun)
        self.setAcceptDrops(True)
        self.doc: pymupdf.Document | None = None
        self.zoom = 1.0
        self.fit_mode = True  # zoom follows the panel width until the user zooms by hand
        self.page_labels: list[QLabel] = []
        self._rendered: set[int] = set()

        self.files = QComboBox()
        self.files.currentIndexChanged.connect(self._open_selected)
        import_btn = QPushButton("Import PDF…")
        import_btn.clicked.connect(self.import_pdf)

        self.prev_btn = QToolButton(text="◀")
        self.prev_btn.setToolTip("Previous slide (←)")
        self.prev_btn.clicked.connect(lambda: self.go_to_page(self.page.value() - 1))
        self.next_btn = QToolButton(text="▶")
        self.next_btn.setToolTip("Next slide (→)")
        self.next_btn.clicked.connect(lambda: self.go_to_page(self.page.value() + 1))
        self.page = QSpinBox(minimum=1, maximum=1)
        self.page.setKeyboardTracking(False)
        self.page.valueChanged.connect(self.go_to_page)
        self.page_count = QLabel("/ 0")
        zoom_out = QToolButton(text="−")
        zoom_out.setToolTip("Zoom out")
        zoom_out.clicked.connect(lambda: self._set_zoom(self.zoom / 1.2))
        zoom_in = QToolButton(text="+")
        zoom_in.setToolTip("Zoom in")
        zoom_in.clicked.connect(lambda: self._set_zoom(self.zoom * 1.2))
        fit = QToolButton(text="Fit")
        fit.setToolTip("Fit slides to the panel width")
        fit.clicked.connect(self.fit_width)

        top = QHBoxLayout()
        top.addWidget(self.files, 1)
        top.addWidget(import_btn)
        nav = QHBoxLayout()
        for w in (self.prev_btn, self.page, self.page_count, self.next_btn):
            nav.addWidget(w)
        nav.addStretch()
        for w in (zoom_out, zoom_in, fit):
            nav.addWidget(w)

        self.pages = QWidget()
        self.pages_layout = QVBoxLayout(self.pages)
        self.pages_layout.setSpacing(PAGE_GAP)
        self.pages_layout.setContentsMargins(PAGE_GAP, PAGE_GAP, PAGE_GAP, PAGE_GAP)
        self.pages_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.message = QLabel(self.empty_text, alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True)
        self.message.setObjectName("muted")
        self.pages_layout.addWidget(self.message)

        self.scroll = QScrollArea(widgetResizable=True)
        self.scroll.setWidget(self.pages)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.scroll.verticalScrollBar().valueChanged.connect(self._update_visible)
        self.scroll.viewport().installEventFilter(self)
        self.scroll.installEventFilter(self)  # ← / → flip slides; ↑ ↓ and the trackpad scroll
        self._refit = QTimer(self, singleShot=True, interval=120)
        self._refit.timeout.connect(self._refit_now)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(top)
        layout.addLayout(nav)
        layout.addWidget(self.scroll, 1)

    # --- folder / files -----------------------------------------------------

    def set_folder(self, folder: Path | None) -> None:
        if folder == self.folder:
            return  # keep the open PDF and page, e.g. when switching lectures of one subject
        self.folder = folder
        self.refresh_files()

    def refresh_files(self, select: Path | None = None) -> None:
        self.files.blockSignals(True)
        self.files.clear()
        for pdf in sorted(self.folder.glob("*.pdf")) if self.folder and self.folder.is_dir() else []:
            self.files.addItem(pdf.name, str(pdf))
        if select is not None:
            self.files.setCurrentIndex(max(0, self.files.findData(str(select))))
        self.files.blockSignals(False)
        self._open_selected()

    def open_page(self, pdf_name: str, page: int) -> None:
        index = self.files.findText(pdf_name)
        if index >= 0:
            self.files.setCurrentIndex(index)
            QTimer.singleShot(0, lambda: self.go_to_page(page))  # after the page column is laid out

    def import_pdf(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, f"Import {self.noun}", str(Path.home()), "PDF files (*.pdf)")
        self.import_paths([Path(p) for p in paths])

    def import_paths(self, paths: list[Path]) -> None:
        pdfs = [p for p in paths if p.suffix.lower() == ".pdf" and p.is_file()]
        if not pdfs:
            return
        folder = self.ensure_folder(pdfs[0].stem)
        if folder is None:
            return
        self.folder = folder
        last = None
        for path in pdfs:
            try:
                last = import_file(path, folder)
            except OSError as exc:
                QMessageBox.warning(self, f"Import {self.noun}", f"Could not import {path.name}:\n{exc}")
                continue
            self.pdfImported.emit(last)
        if last:
            self.refresh_files(select=last)

    def dragEnterEvent(self, event) -> None:
        if any(url.toLocalFile().lower().endswith(".pdf") for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        self.import_paths([Path(url.toLocalFile()) for url in event.mimeData().urls()])
        event.acceptProposedAction()

    # --- document -----------------------------------------------------------

    def _clear_pages(self) -> None:
        for label in self.page_labels:
            self.pages_layout.removeWidget(label)
            label.deleteLater()
        self.page_labels = []
        self._rendered.clear()

    def _show_message(self, text: str) -> None:
        self._clear_pages()
        self.message.setText(text)
        self.message.show()
        self.page.setMaximum(1)
        self.page_count.setText("/ 0")
        self.prev_btn.setEnabled(False)
        self.next_btn.setEnabled(False)

    def _open_selected(self) -> None:
        if self.doc is not None:
            self.doc.close()
            self.doc = None
        path = self.files.currentData()
        if not path:
            self._show_message(self.empty_text)
            return
        try:
            self.doc = pymupdf.open(path)
        except Exception as exc:  # damaged or encrypted PDF
            self._show_message(f"Cannot open {Path(path).name}:\n{exc}")
            return
        self.message.hide()
        self._clear_pages()
        for _ in range(self.doc.page_count):
            label = QLabel()
            label.setObjectName("slidePage")
            self.pages_layout.addWidget(label)
            self.page_labels.append(label)
        self.page.blockSignals(True)
        self.page.setMaximum(max(1, self.doc.page_count))
        self.page.setValue(1)
        self.page.blockSignals(False)
        self.page_count.setText(f"/ {self.doc.page_count}")
        if self.fit_mode:
            self.zoom = self._fit_zoom()
        self._apply_zoom(keep_page=1)

    # --- zoom ---------------------------------------------------------------

    def _fit_zoom(self) -> float:
        if self.doc is None or self.doc.page_count == 0:
            return self.zoom
        widest = max(self.doc[i].rect.width for i in range(min(self.doc.page_count, 5)))
        available = self.scroll.viewport().width() - 2 * PAGE_GAP - 4
        return max(0.25, available / widest)

    def fit_width(self) -> None:
        self.fit_mode = True
        self._set_zoom(self._fit_zoom(), manual=False)

    def _set_zoom(self, zoom: float, manual: bool = True) -> None:
        if manual:
            self.fit_mode = False
        self.zoom = min(5.0, max(0.25, zoom))
        self._apply_zoom(keep_page=self.page.value())

    def _apply_zoom(self, keep_page: int) -> None:
        """Size every page placeholder for the current zoom, then re-render what's visible."""
        if self.doc is None:
            return
        for i, label in enumerate(self.page_labels):
            rect = self.doc[i].rect
            label.clear()
            label.setFixedSize(int(rect.width * self.zoom), int(rect.height * self.zoom))
        self._rendered.clear()
        # Lay the pages out now: otherwise go_to_page scrolls and renders by the
        # old positions, and the slides actually in view stay blank white.
        self.pages_layout.activate()
        self.pages.resize(self.pages.sizeHint().expandedTo(self.scroll.viewport().size()))
        QTimer.singleShot(0, lambda: self.go_to_page(keep_page))

    def eventFilter(self, obj, event) -> bool:
        if obj is self.scroll.viewport() and event.type() == QEvent.Type.Resize and self.fit_mode:
            self._refit.start()
        if obj is self.scroll and event.type() == QEvent.Type.KeyPress and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            step = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1}.get(event.key())
            if step and self.page_labels:
                self.go_to_page(self.page.value() + step)
                return True
        return super().eventFilter(obj, event)

    def _refit_now(self) -> None:
        if self.doc is not None and self.fit_mode and abs(self._fit_zoom() - self.zoom) > 0.01:
            self._set_zoom(self._fit_zoom(), manual=False)

    # --- navigation and lazy rendering --------------------------------------

    def go_to_page(self, number: int) -> None:
        if not self.page_labels:
            return
        number = max(1, min(len(self.page_labels), number))
        label = self.page_labels[number - 1]
        self.scroll.verticalScrollBar().setValue(label.y() - PAGE_GAP)
        self._update_visible()
        self._set_page_indicator(number)

    def _set_page_indicator(self, number: int) -> None:
        self.page.blockSignals(True)
        self.page.setValue(number)
        self.page.blockSignals(False)
        self.prev_btn.setEnabled(number > 1)
        self.next_btn.setEnabled(number < len(self.page_labels))

    def _update_visible(self) -> None:
        if self.doc is None or not self.page_labels:
            return
        top = self.scroll.verticalScrollBar().value()
        height = self.scroll.viewport().height()
        visible = [
            i
            for i, label in enumerate(self.page_labels)
            if label.y() + label.height() >= top - height // 2 and label.y() <= top + height * 3 // 2
        ]
        for i in visible:
            self._render(i)
        if visible:
            for i in list(self._rendered):
                if min(abs(i - v) for v in visible) > KEEP_RENDERED:
                    self.page_labels[i].clear()
                    self._rendered.discard(i)
        # Current page: the last one whose top is above the upper third of the view.
        anchor = top + height // 3
        current = max((i for i, label in enumerate(self.page_labels) if label.y() <= anchor), default=0)
        if top >= self.scroll.verticalScrollBar().maximum() > 0:
            current = len(self.page_labels) - 1  # scrolled to the bottom: the last slide may never reach the anchor
        self._set_page_indicator(current + 1)

    def _render(self, index: int) -> None:
        if index in self._rendered or self.doc is None:
            return
        dpr = self.devicePixelRatioF()
        pix = self.doc[index].get_pixmap(matrix=pymupdf.Matrix(self.zoom * dpr, self.zoom * dpr), alpha=False)
        image = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(dpr)
        self.page_labels[index].setPixmap(pixmap)
        self._rendered.add(index)
