"""Rich-text note editor that saves Markdown and links ``[▶ HH:MM:SS]`` stamps
to audio playback.

Each paragraph also gets a hidden anchor when you start writing it: the
recording time and the PDF page open at that moment. ⌘-click a paragraph (or
right-click it) to get back to both. Anchors live in ``notes.anchors.json``.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QRegularExpression, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QKeySequence,
    QSyntaxHighlighter,
    QTextBlockUserData,
    QTextCharFormat,
    QTextCursor,
    QTextListFormat,
)
from PySide6.QtWidgets import QLabel, QMenu, QSizePolicy, QTextEdit, QToolBar, QVBoxLayout, QWidget

from ..services import timestamps
from ..storage import Lecture


class _StampHighlighter(QSyntaxHighlighter):
    """Colours timestamps without changing the stored text."""

    def __init__(self, document):
        super().__init__(document)
        self.pattern = QRegularExpression(timestamps.TIMESTAMP_RE.pattern)
        self.fmt = QTextCharFormat()
        self.fmt.setForeground(QColor("#2f7de1"))
        self.fmt.setFontWeight(QFont.Weight.DemiBold)

    def highlightBlock(self, text: str) -> None:
        it = self.pattern.globalMatch(text)
        while it.hasNext():
            m = it.next()
            self.setFormat(m.capturedStart(), m.capturedLength(), self.fmt)


DOC_NAMES = {"slides": "Slides", "exams": "Past Exams", "exercises": "Exercises"}
ANCHOR_SEARCH = 50  # paragraphs to look ahead when re-attaching saved anchors


class _Anchor(QTextBlockUserData):
    """Hidden context of one paragraph. Not copied to a new paragraph on Enter."""

    def __init__(self, context: dict):
        super().__init__()
        self.context = context


def anchor_of(block) -> dict | None:
    data = block.userData()
    return data.context if isinstance(data, _Anchor) else None


def describe(context: dict) -> tuple[str | None, str | None]:
    """Menu texts for the audio and the PDF part of an anchor."""
    audio = f"▶  Play recording from {timestamps.format_seconds(context['t'])}" if "t" in context else None
    pdf = None
    if "pdf" in context:
        pdf = f"Open {DOC_NAMES.get(context.get('doc'), 'PDF')}: {context['pdf']}, page {context.get('page', 1)}"
    return audio, pdf


class _StampTextEdit(QTextEdit):
    stampClicked = Signal(int)
    blockClicked = Signal(object)  # QTextBlock, ⌘-clicked outside a stamp

    def _stamp_under(self, pos) -> int | None:
        cursor = self.cursorForPosition(pos)
        return timestamps.stamp_at(cursor.block().text(), cursor.positionInBlock())

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and not self.textCursor().hasSelection():
            seconds = self._stamp_under(event.position().toPoint())
            if seconds is not None:
                self.stampClicked.emit(seconds)
            elif event.modifiers() & Qt.KeyboardModifier.ControlModifier:  # ⌘ on macOS
                self.blockClicked.emit(self.cursorForPosition(event.position().toPoint()).block())

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        over = self._stamp_under(event.position().toPoint()) is not None
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.IBeamCursor)


class NoteEditor(QWidget):
    seekRequested = Signal(int)
    anchorActivated = Signal(dict)  # audio and/or PDF part of a paragraph's anchor
    saved = Signal()

    def __init__(
        self, time_source: Callable[[], float | None], context_source: Callable[[], dict] = dict, parent=None
    ):
        """``time_source`` returns the current recording/playback position in
        seconds, or ``None`` when nothing is recording or loaded.
        ``context_source`` returns the anchor for a paragraph started now (see
        ``Lecture.read_anchors``), or ``{}``."""
        super().__init__(parent)
        self.lecture: Lecture | None = None
        self.time_source = time_source
        self.context_source = context_source
        self.dirty = False
        self._loading = False

        self.edit = _StampTextEdit()
        self.edit.setAcceptRichText(False)  # pasted HTML would not survive the Markdown round trip
        self.edit.setMouseTracking(True)
        self.edit.setObjectName("notesEdit")
        self.edit.document().setDocumentMargin(16)
        self.edit.setPlaceholderText(
            "Start typing your notes…\n\nWhile recording or playing audio, press ⌘T to drop a [▶ 00:12:34] "
            "timestamp. Click a timestamp later to hear that moment again.\n\n"
            "Every line also remembers the recording time and the slide that was open — "
            "⌘-click it to jump back to both."
        )
        self.edit.textChanged.connect(self._on_changed)
        self.edit.stampClicked.connect(self.seekRequested)
        self.edit.blockClicked.connect(lambda block: self._activate(anchor_of(block)))
        self.edit.document().contentsChange.connect(self._anchor_new_text)
        self.edit.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.edit.customContextMenuRequested.connect(self._context_menu)
        _StampHighlighter(self.edit.document())

        self.autosave = QTimer(self, singleShot=True, interval=1500)
        self.autosave.timeout.connect(self.save)

        toolbar = QToolBar()
        toolbar.setObjectName("formatBar")
        self._action(toolbar, "B", "Bold (⌘B)", QKeySequence.StandardKey.Bold, self._toggle_bold)
        self._action(toolbar, "I", "Italic (⌘I)", QKeySequence.StandardKey.Italic, self._toggle_italic)
        self._action(toolbar, "H1", "Heading", None, lambda: self._heading(1))
        self._action(toolbar, "H2", "Subheading", None, lambda: self._heading(2))
        self._action(toolbar, "• List", "Bullet list", None, self._bullets)
        toolbar.addSeparator()
        self._action(toolbar, "⏱ Timestamp", "Insert timestamp at the current recording/playback time (⌘T)", None, self.insert_timestamp)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        self.status = QLabel()
        self.status.setObjectName("muted")
        self.status.setMinimumWidth(70)
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        toolbar.addWidget(self.status)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(4)
        layout.addWidget(toolbar)
        layout.addWidget(self.edit, 1)
        self.setEnabled(False)

    def _action(self, toolbar: QToolBar, text: str, tip: str, shortcut, slot) -> QAction:
        action = QAction(text, self, toolTip=tip)
        if shortcut is not None:
            action.setShortcut(shortcut)
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        action.triggered.connect(slot)
        toolbar.addAction(action)
        return action

    # --- lecture binding ----------------------------------------------------

    def set_lecture(self, lecture: Lecture | None) -> None:
        self.save()
        self.lecture = lecture
        self.edit.blockSignals(True)
        self._loading = True
        self.edit.setMarkdown(lecture.read_notes() if lecture else "")
        self._restore_anchors(lecture.read_anchors() if lecture else [])
        self._loading = False
        self.edit.blockSignals(False)
        self.dirty = False
        self.setEnabled(lecture is not None)
        self.status.setText("")

    def save(self) -> bool:
        """Write notes if changed. Returns True when something was written."""
        self.autosave.stop()
        if self.lecture is None or not self.dirty or not self.lecture.path.exists():
            return False
        self.lecture.write_notes(timestamps.normalize(self.edit.toMarkdown()))
        self.lecture.write_anchors(self._collect_anchors())
        self.dirty = False
        self.status.setText("✓ Saved")
        self.saved.emit()
        return True

    def _on_changed(self) -> None:
        self.dirty = True
        self.status.setText("Editing…")
        self.autosave.start()

    # --- hidden anchors -----------------------------------------------------

    def _anchor_new_text(self, position: int, _removed: int, added: int) -> None:
        """Give each paragraph that was just typed into and has no anchor yet the current context.

        Text that only moved to a new paragraph (Enter inside a line) keeps the
        context it was written in instead."""
        if self._loading or added <= 0:
            return
        doc = self.edit.document()
        block, end = doc.findBlock(position), doc.findBlock(position + added)
        context = None
        while block.isValid():
            if block.text().strip() and anchor_of(block) is None:
                start = max(position, block.position()) - block.position()
                typed = block.text()[start : position + added - block.position()]
                previous = block.previous()
                if not typed.strip():  # only a line break: the text was written earlier
                    if previous.isValid() and anchor_of(previous) is not None:
                        block.setUserData(_Anchor(anchor_of(previous)))
                        if not previous.text().strip():  # Enter at the start of the line: the anchor moves down
                            previous.setUserData(None)
                else:
                    context = context if context is not None else self.context_source()
                    if not context:
                        return
                    block.setUserData(_Anchor(context))
            if block == end:
                break
            block = block.next()

    def _collect_anchors(self) -> list[dict]:
        anchors = []
        block = self.edit.document().begin()
        while block.isValid():
            context = anchor_of(block)
            if context and block.text().strip():
                anchors.append({"text": block.text(), **context})
            block = block.next()
        return anchors

    def _restore_anchors(self, anchors: list[dict]) -> None:
        """Re-attach saved anchors to paragraphs by their text, in order."""
        blocks = []
        block = self.edit.document().begin()
        while block.isValid():
            blocks.append(block)
            block = block.next()
        start = 0
        for anchor in anchors:
            text = anchor.get("text", "")
            for i in range(start, min(len(blocks), start + ANCHOR_SEARCH)):
                if blocks[i].text().strip() == text.strip():  # Markdown drops leading/trailing spaces
                    blocks[i].setUserData(_Anchor({k: v for k, v in anchor.items() if k != "text"}))
                    start = i + 1
                    break

    def _activate(self, context: dict | None) -> None:
        if context:
            self.anchorActivated.emit(context)
        else:
            self.status.setText("Nothing remembered for this line.")

    def _context_menu(self, pos) -> None:
        menu = self.edit.createStandardContextMenu(pos)
        context = anchor_of(self.edit.cursorForPosition(pos).block())
        if context:
            audio, pdf = describe(context)
            first = menu.actions()[0] if menu.actions() else None
            for text, part in (
                (audio, {k: context[k] for k in ("audio", "t") if k in context}),
                (pdf, {k: context[k] for k in ("doc", "pdf", "page") if k in context}),
            ):
                if text:
                    action = QAction(text, menu)
                    action.triggered.connect(lambda _=False, part=part: self.anchorActivated.emit(part))
                    menu.insertAction(first, action)
            menu.insertSeparator(first)
        menu.exec(self.edit.viewport().mapToGlobal(pos))
        menu.deleteLater()

    # --- formatting ---------------------------------------------------------

    def insert_timestamp(self) -> None:
        seconds = self.time_source()
        if seconds is None:
            self.status.setText("Record or play audio first, then ⌘T stamps that moment.")
            return
        # Each stamp starts its own plain paragraph, after the current line.
        cursor = self.edit.textCursor()
        if cursor.block().text().strip():
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            block_fmt = cursor.blockFormat()
            block_fmt.setHeadingLevel(0)
            cursor.insertBlock(block_fmt, QTextCharFormat())
        cursor.insertText(timestamps.make_stamp(seconds) + " ", QTextCharFormat())
        self.edit.setTextCursor(cursor)
        self.edit.setFocus()

    def _toggle_bold(self) -> None:
        fmt = QTextCharFormat()
        bold = self.edit.fontWeight() < QFont.Weight.Bold
        fmt.setFontWeight(QFont.Weight.Bold if bold else QFont.Weight.Normal)
        self.edit.mergeCurrentCharFormat(fmt)

    def _toggle_italic(self) -> None:
        fmt = QTextCharFormat()
        fmt.setFontItalic(not self.edit.fontItalic())
        self.edit.mergeCurrentCharFormat(fmt)

    def _heading(self, level: int) -> None:
        cursor = self.edit.textCursor()
        block_fmt = cursor.blockFormat()
        level = 0 if block_fmt.headingLevel() == level else level
        block_fmt.setHeadingLevel(level)
        cursor.setBlockFormat(block_fmt)
        char_fmt = QTextCharFormat()
        sizes = {0: 0, 1: 6, 2: 4}
        char_fmt.setProperty(QTextCharFormat.Property.FontSizeAdjustment, sizes[level])
        char_fmt.setFontWeight(QFont.Weight.Bold if level else QFont.Weight.Normal)
        cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
        cursor.mergeCharFormat(char_fmt)

    def _bullets(self) -> None:
        cursor = self.edit.textCursor()
        current = cursor.currentList()
        if current:
            current.remove(cursor.block())
            block_fmt = cursor.blockFormat()
            block_fmt.setIndent(0)
            cursor.setBlockFormat(block_fmt)
        else:
            cursor.createList(QTextListFormat.Style.ListDisc)
