"""All documents of the open lecture and its subject in one list, above the PDF viewer.

Slides, past exams and tasks are just tags here, next to the user's own coloured
ones. Files stay in the folder they were imported to (see ``storage``): slides with
their lecture, past exams and tasks with the whole subject. Changing a tag never
moves a file, so note links and the search index keep working.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..storage import DOC_TYPES, TAG_PALETTE, Document, Lecture, Subject, import_file
from .pdf_viewer import PdfViewer

EMPTY_TEXT = "No documents yet.\n\nClick “Import PDF…” or drag a PDF file here."
FILTERED_TEXT = "No document has all the selected tags."
UNKNOWN_TAG_COLOR = TAG_PALETTE["Grey"]
IMPORT_CHOICES = {
    "Slides (this lecture)": "slides",
    "Past exam (whole subject)": "exams",
    "Task (whole subject)": "exercises",
}


def _text_on(color: str) -> str:
    """Black or white, whichever is readable on ``color``."""
    c = QColor(color)
    return "#1a1a1a" if 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue() > 160 else "white"


def _swatch(color: str) -> QPixmap:
    pixmap = QPixmap(12, 12)
    pixmap.fill(QColor(color))
    return pixmap


class _FlowLayout(QLayout):
    """Lays out widgets in rows, wrapping when the panel is narrow."""

    GAP = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, move=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _arrange(self, rect: QRect, move: bool) -> int:
        x, y, row_height = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x > rect.x() and x + hint.width() > rect.right() + 1:
                x, y, row_height = rect.x(), y + row_height + self.GAP, 0
            if move:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self.GAP
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y()


class NewTagDialog(QDialog):
    """A name and a colour. The colour alone is enough: the tag is then called "Red"."""

    def __init__(self, taken: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("New tag")
        self.taken = taken
        self.name = QLineEdit(placeholderText="Name (optional, e.g. Important)")
        self.colors = QButtonGroup(self)
        row = QHBoxLayout()
        row.setSpacing(4)
        free = next((n for n in TAG_PALETTE if n not in taken), "Grey")
        for name, color in TAG_PALETTE.items():
            button = QToolButton(checkable=True, checked=name == free)
            button.setToolTip(name)
            button.setProperty("colorName", name)
            button.setFixedSize(24, 24)
            button.setStyleSheet(
                f"QToolButton {{ background: {color}; border: 2px solid transparent; border-radius: 12px; }}"
                f"QToolButton:checked {{ border-color: palette(text); }}"
            )
            self.colors.addButton(button)
            row.addWidget(button)
        row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.name)
        layout.addLayout(row)
        layout.addWidget(buttons)

    def result_tag(self) -> tuple[str, str]:
        color_name = self.colors.checkedButton().property("colorName")
        return self.name.text().strip() or color_name, TAG_PALETTE[color_name]


class DocumentsPanel(QWidget):
    pdfImported = Signal(object)  # Path

    def __init__(self, ensure_folder: Callable[[str, str], Path | None], parent=None):
        """``ensure_folder(kind, name_hint)`` returns the folder imports of that kind go
        to — e.g. creating a lecture named after the PDF when none is open — or ``None``
        if there is none (the user cancelled)."""
        super().__init__(parent)
        self.ensure_folder = ensure_folder
        self.subject: Subject | None = None
        self.lecture: Lecture | None = None
        self.documents: list[Document] = []
        self.active_tags: set[str] = set()
        self.setAcceptDrops(True)

        self.viewer = PdfViewer()
        import_btn = QPushButton("Import PDF…")
        import_btn.clicked.connect(self.import_pdf)
        self.tags_btn = QToolButton(text="🏷 Tags")
        self.tags_btn.setToolTip("Add or remove tags on the selected document (or right-click it)")
        self.tags_btn.clicked.connect(lambda: self._show_tag_menu(self.current_document(), self.tags_btn.mapToGlobal(self.tags_btn.rect().bottomLeft())))

        self.filter_bar = QWidget()
        self.filter_layout = _FlowLayout(self.filter_bar)
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.filter_bar.setSizePolicy(policy)

        self.list = QListWidget()
        self.list.setObjectName("documentList")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        self.list.currentItemChanged.connect(self._open_current)

        top = QHBoxLayout()
        top.addWidget(self.filter_bar, 1)
        top.addWidget(self.tags_btn, alignment=Qt.AlignmentFlag.AlignTop)
        top.addWidget(import_btn, alignment=Qt.AlignmentFlag.AlignTop)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.list)
        split.addWidget(self.viewer)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([170, 600])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 0)
        layout.addLayout(top)
        layout.addWidget(split, 1)
        self.refresh()

    # --- what is listed -----------------------------------------------------

    def set_scope(self, subject: Subject | None, lecture: Lecture | None) -> None:
        """List ``lecture``'s documents, then those of the whole ``subject``."""
        if subject != self.subject:
            self.active_tags.clear()  # tags belong to one subject
        self.subject, self.lecture = subject, lecture
        self.refresh()

    def current_document(self) -> Document | None:
        return next((d for d in self.documents if d.path == self.viewer.path), None)

    def _tag_colors(self) -> dict[str, str]:
        return self.subject.tag_colors() if self.subject else {}

    def refresh(self, select: Path | None = None) -> None:
        """Rebuild filter and list. Keeps the open document (and its page) if it is still listed."""
        self.documents = (self.lecture.documents() if self.lecture else []) + (self.subject.documents() if self.subject else [])
        present = {tag for doc in self.documents for tag in doc.tags}
        self.active_tags &= present
        self._fill_filter(present)
        shown = [doc for doc in self.documents if self.active_tags <= set(doc.tags)]

        keep = select or self.viewer.path
        colors = self._tag_colors()
        self.list.blockSignals(True)
        self.list.clear()
        for doc in shown:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, doc)
            self.list.addItem(item)
            row = self._row(doc, colors)
            item.setSizeHint(row.sizeHint())
            self.list.setItemWidget(item, row)
        current = next((i for i, doc in enumerate(shown) if doc.path == keep), 0 if shown else -1)
        self.list.setCurrentRow(current)
        self.list.blockSignals(False)
        self.tags_btn.setEnabled(bool(shown))
        self._open_current()

    def _open_current(self) -> None:
        item = self.list.currentItem()
        if item is None:
            self.viewer.open_file(None, FILTERED_TEXT if self.documents else EMPTY_TEXT)
            return
        path = item.data(Qt.ItemDataRole.UserRole).path
        if path != self.viewer.path:
            self.viewer.open_file(path)

    def _row(self, doc: Document, colors: dict[str, str]) -> QWidget:
        row = QWidget()
        row.setToolTip(f"{doc.path.name}\n{doc.lecture or 'Whole subject'}\nTags: {', '.join(doc.tags) or 'none'}")
        name = QLabel(doc.path.name)
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        chips = QHBoxLayout()
        chips.setSpacing(3)
        for tag in doc.tags:
            chip = QLabel(tag)
            color = colors.get(tag, UNKNOWN_TAG_COLOR)
            chip.setStyleSheet(
                f"background: {color}; color: {_text_on(color)}; border-radius: 7px; padding: 0 6px; font-size: 11px;"
            )
            chips.addWidget(chip)
        scope = QLabel(doc.lecture or "Whole subject")
        scope.setObjectName("muted")
        scope.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        chips.addWidget(scope, 1)
        layout = QVBoxLayout(row)
        layout.setContentsMargins(6, 3, 6, 4)
        layout.setSpacing(2)
        layout.addWidget(name)
        layout.addLayout(chips)
        return row

    # --- filter -------------------------------------------------------------

    def _fill_filter(self, present: set[str]) -> None:
        while (item := self.filter_layout.takeAt(0)) is not None:
            item.widget().hide()
            item.widget().deleteLater()
        colors = self._tag_colors()
        for tag in [t for t in colors if t in present] + sorted(present - colors.keys()):
            color = colors.get(tag, UNKNOWN_TAG_COLOR)
            button = QToolButton(text=tag, checkable=True, checked=tag in self.active_tags)
            button.setProperty("tag", tag)
            button.setToolTip(f"Show only documents tagged “{tag}”")
            button.setStyleSheet(
                f"QToolButton {{ border: 1px solid {color}; border-radius: 8px; padding: 1px 7px; font-size: 11px; }}"
                f"QToolButton:checked {{ background: {color}; color: {_text_on(color)}; }}"
            )
            button.toggled.connect(lambda on, tag=tag: self.set_tag_filter(tag, on))
            self.filter_layout.addWidget(button)

    def set_tag_filter(self, tag: str, on: bool) -> None:
        (self.active_tags.add if on else self.active_tags.discard)(tag)
        QTimer.singleShot(0, self.refresh)  # not while the clicked button is handling its signal

    def open_page(self, folder: str, pdf_name: str, page: int) -> bool:
        """Open a page of the PDF ``pdf_name`` stored in ``folder`` (a note link's "doc").
        False if there is no such document here."""
        doc = next((d for d in self.documents if d.folder == folder and d.path.name == pdf_name), None)
        if doc is None:
            return False
        if not self.active_tags <= set(doc.tags):
            self.active_tags.clear()  # the filter must not hide what a link points to
        self.refresh(select=doc.path)
        QTimer.singleShot(0, lambda: self.viewer.go_to_page(page))  # after the page column is laid out
        return True

    # --- tags ---------------------------------------------------------------

    def set_document_tag(self, doc: Document, tag: str, on: bool) -> None:
        tags = doc.tags
        self._save_tags(doc, tags + [tag] if on else [t for t in tags if t != tag])

    def _save_tags(self, doc: Document, tags: list[str]) -> None:
        try:
            doc.set_tags(tags)
        except OSError as exc:
            QMessageBox.warning(self, "Tags", f"Could not save the tags:\n{exc}")
        self.refresh()

    def _new_tag(self, doc: Document | None) -> None:
        dialog = NewTagDialog(list(self._tag_colors()), self)
        if dialog.exec():
            self.create_tag(doc, *dialog.result_tag())

    def create_tag(self, doc: Document | None, name: str, color: str) -> None:
        """A new tag (an existing name keeps its colour), put on ``doc``."""
        if name not in self._tag_colors():
            self.subject.set_tag_color(name, color)
        if doc is not None and name not in doc.tags:
            self.set_document_tag(doc, name, True)
        else:
            self.refresh()

    def _set_color(self, tag: str, color: str) -> None:
        self.subject.set_tag_color(tag, color)
        self.refresh()

    def _delete_tag(self, tag: str) -> None:
        answer = QMessageBox.question(
            self, "Delete tag", f"Remove the tag “{tag}” from every document of {self.subject.name}?\nThe documents stay."
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.subject.delete_tag(tag)
            self.refresh()

    def _tag_menu(self, doc: Document | None) -> QMenu:
        menu = QMenu(self)
        colors = self._tag_colors()
        tags = doc.tags if doc else []
        for tag in list(colors) + [t for t in tags if t not in colors]:
            action = QAction(tag, menu, checkable=True, checked=tag in tags)
            action.setIcon(_swatch(colors.get(tag, UNKNOWN_TAG_COLOR)))
            action.setEnabled(doc is not None)
            action.toggled.connect(lambda on, tag=tag: self.set_document_tag(doc, tag, on))
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction("New tag…", lambda: self._new_tag(doc)).setEnabled(self.subject is not None)
        edit = menu.addMenu("Edit tag")
        for tag, current in colors.items():
            sub = edit.addMenu(_swatch(current), tag)
            for name, color in TAG_PALETTE.items():
                sub.addAction(_swatch(color), name, lambda tag=tag, color=color: self._set_color(tag, color))
            if tag not in DOC_TYPES.values():
                sub.addSeparator()
                sub.addAction("Delete tag…", lambda tag=tag: self._delete_tag(tag))
        return menu

    def _show_tag_menu(self, doc: Document | None, global_pos) -> None:
        menu = self._tag_menu(doc)
        menu.exec(global_pos)
        menu.deleteLater()

    def _list_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is not None:
            self.list.setCurrentItem(item)
            self._show_tag_menu(item.data(Qt.ItemDataRole.UserRole), self.list.viewport().mapToGlobal(pos))

    # --- import -------------------------------------------------------------

    def import_pdf(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Import documents", str(Path.home()), "PDF files (*.pdf)")
        self.import_paths([Path(p) for p in paths])

    def _ask_kind(self) -> str | None:
        """What is being imported, offering the type being filtered by first."""
        choices = sorted(IMPORT_CHOICES, key=lambda c: DOC_TYPES[IMPORT_CHOICES[c]] not in self.active_tags)
        choice, ok = QInputDialog.getItem(self, "Import documents", "What is this?", choices, 0, False)
        return IMPORT_CHOICES[choice] if ok else None

    def import_paths(self, paths: list[Path], kind: str | None = None) -> None:
        pdfs = [p for p in paths if p.suffix.lower() == ".pdf" and p.is_file()]
        if not pdfs:
            return
        kind = kind or self._ask_kind()
        folder = self.ensure_folder(kind, pdfs[0].stem) if kind else None
        if folder is None:
            return
        last = None
        for path in pdfs:
            try:
                last = import_file(path, folder)
            except OSError as exc:
                QMessageBox.warning(self, "Import documents", f"Could not import {path.name}:\n{exc}")
                continue
            self.pdfImported.emit(last)
        if last:
            self.active_tags.clear()  # the new document must show up
            self.refresh(select=last)

    def dragEnterEvent(self, event) -> None:
        if any(url.toLocalFile().lower().endswith(".pdf") for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls()]
        event.acceptProposedAction()
        # Not inside the drop: the type dialog would block the app the file was dragged from.
        QTimer.singleShot(0, lambda: self.import_paths(paths))
