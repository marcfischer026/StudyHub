"""Slide / PDF viewer rendered with PyMuPDF.

All pages sit in one continuous, scrollable column (like Preview). Pages are
rendered lazily — only those near the viewport hold a pixmap — so long slide
decks stay fast and light on memory.

Text on a slide can be selected and copied (drag across it, ⌘C or right-click),
and the highlighter marks text — or any area, e.g. a diagram — in a colour.
Highlights are saved into the PDF as standard annotations, so Preview and other
readers show them too.

The snip tool clips any area — a chart, a table, a diagram — into the notes,
with the text and tables found in it, so the Claude export can read them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pymupdf
from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QImage, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
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
WORD_SLACK = 3  # points: a press this close to a word starts a text selection, not an area
HIGHLIGHT_COLORS = {  # name -> RGB 0..1, as PDF annotations store them
    "Yellow": (1.0, 0.92, 0.23),
    "Green": (0.56, 0.93, 0.47),
    "Blue": (0.53, 0.81, 0.98),
    "Pink": (1.0, 0.6, 0.8),
}
SELECTION_COLOR = QColor(0, 122, 255, 70)
AREA_OPACITY = 0.35
MARK_TYPES = [pymupdf.PDF_ANNOT_HIGHLIGHT, pymupdf.PDF_ANNOT_SQUARE]  # what "Remove highlight" removes
FIGURE_WIDTH = 1600  # pixels a clipped figure is rendered at (sharp on Retina and for Claude)


def clip_figure(page: pymupdf.Page, area: pymupdf.Rect) -> tuple[bytes, str, list[str]]:
    """A PNG of ``area`` plus the text and the tables (as Markdown) inside it."""
    scale = max(2.0, min(6.0, FIGURE_WIDTH / max(area.width, 1)))
    png = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=area, alpha=False).tobytes("png")
    # Whole words the box touches: a box drawn roughly around a chart often cuts its labels.
    text = selection_text([w for w in page.get_text("words") if pymupdf.Rect(w[:4]).intersects(area)])
    tables = []
    try:
        for table in page.find_tables(clip=area).tables:
            if table.row_count > 1 and table.col_count > 1:
                tables.append(table.to_markdown(clean=False).strip())
    except Exception:  # table detection is best effort (old PyMuPDF, odd layouts)
        pass
    return png, text, tables


def selection_text(words: list[tuple]) -> str:
    """Words from ``page.get_text("words")`` joined back into lines."""
    out, last = [], None
    for w in words:
        line = (w[5], w[6])
        if last is not None:
            out.append(" " if line == last else "\n")
        out.append(w[4])
        last = line
    return "".join(out)


def line_rects(words: list[tuple]) -> list[pymupdf.Rect]:
    """One rectangle per text line covered by ``words`` — what a highlight marks."""
    lines: dict[tuple, pymupdf.Rect] = {}
    for w in words:
        rect = pymupdf.Rect(w[:4])
        key = (w[5], w[6])
        lines[key] = lines[key] | rect if key in lines else rect
    return list(lines.values())


class SlidePage(QLabel):
    """One rendered page; turns mouse drags into text selections and highlights."""

    def __init__(self, viewer: "PdfViewer", index: int):
        super().__init__()
        self.viewer = viewer
        self.index = index
        self.setObjectName("slidePage")
        self.setMouseTracking(True)  # I-beam cursor over text

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.viewer._begin_drag(self.index, event.position())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.viewer._continue_drag(self.index, event.position())
        else:
            self.viewer._update_cursor(self, event.position())

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.viewer._end_drag(self.index)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.viewer._select_line_at(self.index, event.position())

    def contextMenuEvent(self, event) -> None:
        self.viewer._context_menu(self.index, event.pos(), event.globalPos())

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        rects = self.viewer._selection_rects(self.index)
        if rects:
            painter = QPainter(self)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(SELECTION_COLOR)
            for rect in rects:
                painter.drawRect(rect)
            painter.end()


class PdfViewer(QWidget):
    pdfImported = Signal(object)  # Path
    interacted = Signal()  # the user scrolled, clicked or switched files here
    figureClipped = Signal(dict)  # {"png": bytes, "pdf", "page", "text", "tables"}
    linkRequested = Signal(dict)  # {"pdf", "page"}: put a link to this page into the notes

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
        self.page_labels: list[SlidePage] = []
        self._rendered: set[int] = set()
        self._words: dict[int, list[tuple]] = {}  # page -> get_text("words"), filled on demand
        self.highlight_color = "Yellow"
        # Current selection: a page plus either a word range or a dragged area.
        self._sel_page: int | None = None
        self._sel_words: list[tuple] = []
        self._sel_area: pymupdf.Rect | None = None  # unrotated PDF coordinates
        self._drag_start: pymupdf.Point | None = None
        self._drag_word: int | None = None  # index of the word the drag started on

        self.files = QComboBox()
        self.files.currentIndexChanged.connect(self._open_selected)
        self.files.activated.connect(self.interacted)
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
        for control in (self.prev_btn, self.next_btn):
            control.clicked.connect(self.interacted)
        self.page.valueChanged.connect(self.interacted)
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
        self.highlighter = QToolButton(text="Mark", checkable=True)
        self.highlighter.setToolTip(
            "Highlighter: drag over text — or over any area, e.g. a diagram — to mark it.\n"
            "Right-click a highlight to remove it. Pick the colour from the arrow menu."
        )
        self.highlighter.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        colors = QMenu(self.highlighter)
        group = QActionGroup(colors)
        for name in HIGHLIGHT_COLORS:
            action = QAction(name, colors, checkable=True, checked=name == self.highlight_color)
            action.setIcon(self._swatch(name))
            action.triggered.connect(lambda _=False, n=name: self._pick_color(n))
            group.addAction(action)
            colors.addAction(action)
        self.highlighter.setMenu(colors)
        self.highlighter.setIcon(self._swatch(self.highlight_color))
        self.highlighter.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.snipper = QToolButton(text="✂ Snip", checkable=True)
        self.snipper.setToolTip(
            "Snip: drag a box around a chart, table or diagram to put it into your notes.\n"
            "Text and tables inside it go along, so Claude can read them in the export."
        )
        self.eraser = QToolButton(text="⌫ Unmark", checkable=True)
        self.eraser.setToolTip("Unmark: click a highlight to remove it, or drag a box to remove every highlight it touches.")
        tools = (self.snipper, self.highlighter, self.eraser)  # at most one is on
        for tool in tools:
            tool.toggled.connect(
                lambda on, tool=tool: (
                    [other.setChecked(False) for other in tools if on and other is not tool],
                    self.clear_selection(),
                )
            )

        top = QHBoxLayout()
        top.addWidget(self.files, 1)
        top.addWidget(import_btn)
        nav = QHBoxLayout()
        for w in (self.prev_btn, self.page, self.page_count, self.next_btn):
            nav.addWidget(w)
        nav.addStretch()
        nav.addWidget(self.snipper)
        nav.addWidget(self.highlighter)
        nav.addWidget(self.eraser)
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
        copy = QShortcut(QKeySequence.StandardKey.Copy, self, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        copy.activated.connect(self.copy_selection)
        select_all = QShortcut(QKeySequence.StandardKey.SelectAll, self, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        select_all.activated.connect(lambda: self._select_page(self.page.value() - 1))
        escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), self, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        escape.activated.connect(self.clear_selection)

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

    def current(self) -> tuple[str, int] | None:
        """Open file name and page, if a PDF is showing."""
        return (self.files.currentText(), self.page.value()) if self.doc is not None else None

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
        self._words.clear()
        self._sel_page = None
        self._sel_words, self._sel_area = [], None

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
        for i in range(self.doc.page_count):
            label = SlidePage(self, i)
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
        if event.type() in (QEvent.Type.Wheel, QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress):
            self.interacted.emit()
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

    # --- selecting, copying and highlighting --------------------------------

    def _swatch(self, name: str) -> QPixmap:
        pixmap = QPixmap(12, 12)
        pixmap.fill(QColor.fromRgbF(*HIGHLIGHT_COLORS[name]))
        return pixmap

    def _pick_color(self, name: str) -> None:
        self.highlight_color = name
        self.highlighter.setIcon(self._swatch(name))
        self.highlighter.setChecked(True)

    def _page_words(self, index: int) -> list[tuple]:
        if index not in self._words:
            self._words[index] = self.doc[index].get_text("words") if self.doc is not None else []
        return self._words[index]

    def _to_pdf(self, index: int, pos: QPointF) -> pymupdf.Point:
        """Widget pixel -> unrotated PDF point (what text and annotations use)."""
        return pymupdf.Point(pos.x() / self.zoom, pos.y() / self.zoom) * self.doc[index].derotation_matrix

    def _to_widget(self, index: int, rect: pymupdf.Rect) -> QRect:
        r = (rect * self.doc[index].rotation_matrix) * self.zoom
        return QRectF(r.x0, r.y0, r.width, r.height).toAlignedRect()

    def _word_at(self, index: int, point: pymupdf.Point, slack: float = WORD_SLACK) -> int | None:
        """The word under ``point`` (within ``slack`` points), else None."""
        best, best_dist = None, slack
        for i, w in enumerate(self._page_words(index)):
            rect = pymupdf.Rect(w[:4])
            dx = max(rect.x0 - point.x, 0, point.x - rect.x1)
            dy = max(rect.y0 - point.y, 0, point.y - rect.y1)
            dist = (dx * dx + dy * dy) ** 0.5
            if dist <= best_dist:
                best, best_dist = i, dist
                if dist == 0:
                    break
        return best

    def _nearest_word(self, index: int, point: pymupdf.Point) -> int | None:
        return self._word_at(index, point, slack=float("inf"))

    def _begin_drag(self, index: int, pos: QPointF) -> None:
        if self.doc is None:
            return
        self.scroll.setFocus()  # so ⌘C / Esc reach this viewer
        self.clear_selection()
        self._sel_page = index
        self._drag_start = self._to_pdf(index, pos)
        area_tool = self.snipper.isChecked() or self.eraser.isChecked()
        self._drag_word = None if area_tool else self._word_at(index, self._drag_start)

    def _continue_drag(self, index: int, pos: QPointF) -> None:
        if self._drag_start is None or index != self._sel_page:
            return
        point = self._to_pdf(index, pos)
        words = self._page_words(index)
        if self._drag_word is not None:
            end = self._nearest_word(index, point)
            lo, hi = sorted((self._drag_word, end))
            self._sel_words, self._sel_area = words[lo : hi + 1], None
        else:  # started off the text: a rectangle, selecting the words inside it
            area = pymupdf.Rect(self._drag_start, self._drag_start) | point
            area.normalize()
            self._sel_area = area
            self._sel_words = [w for w in words if pymupdf.Rect(w[:4]).intersects(area)]
        self.page_labels[index].update()

    def _end_drag(self, index: int) -> None:
        if self._drag_start is None:
            return
        start, self._drag_start = self._drag_start, None
        if self.highlighter.isChecked() and index == self._sel_page:
            self.highlight_selection()
        elif self.snipper.isChecked() and index == self._sel_page:
            self.clip_selection()
        elif self.eraser.isChecked() and index == self._sel_page:
            area = self._sel_area
            self.clear_selection()
            if area is None or area.width < 4 or area.height < 4:  # a click: the highlight under it
                self._remove_highlight(index, start)
            else:
                self.remove_highlights(index, area)

    def _select_line_at(self, index: int, pos: QPointF) -> None:
        """Double-click: select the whole text line under the cursor."""
        if self.doc is None:
            return
        hit = self._word_at(index, self._to_pdf(index, pos))
        if hit is None:
            return
        words = self._page_words(index)
        line = words[hit][5:7]
        self.clear_selection()
        self._sel_page = index
        self._sel_words = [w for w in words if w[5:7] == line]
        self.page_labels[index].update()
        if self.highlighter.isChecked():
            self.highlight_selection()

    def _select_page(self, index: int) -> None:
        if self.doc is None or not 0 <= index < len(self.page_labels):
            return
        self.clear_selection()
        self._sel_page = index
        self._sel_words = list(self._page_words(index))
        self.page_labels[index].update()

    def _selection_rects(self, index: int) -> list[QRect]:
        if index != self._sel_page or self.doc is None:
            return []
        area_tool = self.highlighter.isChecked() or self.snipper.isChecked() or self.eraser.isChecked()
        if self._sel_area is not None and (not self._sel_words or area_tool):
            return [self._to_widget(index, self._sel_area)]
        return [self._to_widget(index, r) for r in line_rects(self._sel_words)]

    def _update_cursor(self, label: SlidePage, pos: QPointF) -> None:
        if self.doc is None:
            return
        on_text = self._word_at(label.index, self._to_pdf(label.index, pos)) is not None
        if self.eraser.isChecked():
            over = self._highlight_at(self.doc[label.index], self._to_pdf(label.index, pos)) is not None
            label.setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.CrossCursor)
        elif self.snipper.isChecked():
            label.setCursor(Qt.CursorShape.CrossCursor)
        elif on_text:
            label.setCursor(Qt.CursorShape.IBeamCursor)
        elif self.highlighter.isChecked():
            label.setCursor(Qt.CursorShape.CrossCursor)
        else:
            label.unsetCursor()

    def clear_selection(self) -> None:
        page, self._sel_page = self._sel_page, None
        self._sel_words, self._sel_area = [], None
        if page is not None and page < len(self.page_labels):
            self.page_labels[page].update()

    def selected_text(self) -> str:
        return selection_text(self._sel_words)

    def copy_selection(self) -> None:
        text = self.selected_text()
        if text:
            QApplication.clipboard().setText(text)

    def highlight_selection(self) -> None:
        """Mark the selected text (or dragged area) with the current colour."""
        index = self._sel_page
        if index is None or self.doc is None:
            return
        page = self.doc[index]
        if self._sel_area is not None and (self.highlighter.isChecked() or not self._sel_words):
            area = self._sel_area
            if area.width < 4 or area.height < 4:  # a stray click, not a drag
                self.clear_selection()
                return
            # A translucent box: highlight annotations draw rounded ends meant for text lines.
            color = HIGHLIGHT_COLORS[self.highlight_color]
            annot = page.add_rect_annot(area)
            annot.set_colors(stroke=color, fill=color)
            annot.set_border(width=0)
            annot.set_opacity(AREA_OPACITY)
        elif self._sel_words:
            annot = page.add_highlight_annot(quads=[r.quad for r in line_rects(self._sel_words)])
            annot.set_colors(stroke=HIGHLIGHT_COLORS[self.highlight_color])
        else:
            return
        annot.update()
        self.clear_selection()
        self._save_and_refresh(index)

    def clip_selection(self, index: int | None = None, area: pymupdf.Rect | None = None) -> None:
        """Send the dragged area (or ``area`` on page ``index``) to the notes as a figure."""
        if index is None:
            index, area = self._sel_page, self._sel_area
        if index is None or area is None or self.doc is None:
            return
        if area.width < 8 or area.height < 8:  # a stray click, not a drag
            self.clear_selection()
            return
        png, text, tables = clip_figure(self.doc[index], area)
        self.clear_selection()
        self.figureClipped.emit(
            {"png": png, "pdf": self.files.currentText(), "page": index + 1, "text": text, "tables": tables}
        )

    def _highlight_at(self, page: pymupdf.Page, point: pymupdf.Point):
        for annot in page.annots(types=MARK_TYPES):
            if point in annot.rect:
                return annot
        return None

    def _remove_highlight(self, index: int, point: pymupdf.Point) -> None:
        page = self.doc[index]  # keep it: an annot is only valid while its page object lives
        annot = self._highlight_at(page, point)
        if annot is not None:
            page.delete_annot(annot)
            self._save_and_refresh(index)

    def remove_highlights(self, index: int, area: pymupdf.Rect) -> int:
        """Remove every highlight touching ``area`` on page ``index``. Returns how many."""
        page = self.doc[index]
        doomed = [annot for annot in page.annots(types=MARK_TYPES) if annot.rect.intersects(area)]
        for annot in doomed:
            page.delete_annot(annot)
        if doomed:
            self._save_and_refresh(index)
        return len(doomed)

    def _selection_area(self) -> pymupdf.Rect | None:
        if self._sel_area is not None:
            return self._sel_area
        rects = line_rects(self._sel_words)
        if not rects:
            return None
        area = rects[0]
        for rect in rects[1:]:
            area |= rect
        return area

    def _save_and_refresh(self, index: int) -> None:
        try:
            if self.doc.can_save_incrementally():
                self.doc.saveIncr()
            else:  # e.g. a repaired PDF: write a full copy, then swap it in
                path = Path(self.doc.name)
                tmp = path.with_name(path.name + ".saving")
                self.doc.save(tmp, garbage=1)
                self.doc.close()
                tmp.replace(path)
                self.doc = pymupdf.open(path)
        except Exception as exc:
            QMessageBox.warning(self, "Highlight", f"Could not save the highlight:\n{exc}")
        self._rendered.discard(index)
        self._render(index)

    def _context_menu(self, index: int, pos, global_pos) -> None:
        if self.doc is None:
            return
        point = self._to_pdf(index, QPointF(pos))
        menu = QMenu(self)
        has_selection = index == self._sel_page and (self._sel_words or self._sel_area is not None)
        copy = menu.addAction("Copy", self.copy_selection)
        copy.setShortcut(QKeySequence.StandardKey.Copy)
        copy.setEnabled(bool(index == self._sel_page and self._sel_words))
        mark = menu.addAction(f"Highlight ({self.highlight_color})", self.highlight_selection)
        mark.setEnabled(bool(has_selection))
        if self._highlight_at(self.doc[index], point) is not None:
            menu.addAction("Remove highlight", lambda: self._remove_highlight(index, point))
        selected = self._selection_area() if index == self._sel_page else None
        if selected is not None and any(a.rect.intersects(selected) for a in self.doc[index].annots(types=MARK_TYPES)):
            menu.addAction(
                "Remove highlights in selection",
                lambda: (self.clear_selection(), self.remove_highlights(index, selected)),
            )
        menu.addSeparator()
        snip = menu.addAction("✂ Add selected area to notes", self.clip_selection)
        snip.setEnabled(index == self._sel_page and self._sel_area is not None)
        menu.addAction("✂ Add whole slide to notes", lambda: self.clip_selection(index, self.doc[index].rect))
        menu.addAction(
            "→ Link this slide in notes",
            lambda: self.linkRequested.emit({"pdf": self.files.currentText(), "page": index + 1}),
        )
        menu.addSeparator()
        page_text = self.doc[index].get_text("text").strip()
        slide_copy = menu.addAction("Copy slide text", lambda: QApplication.clipboard().setText(page_text))
        slide_copy.setEnabled(bool(page_text))
        menu.exec(global_pos)
