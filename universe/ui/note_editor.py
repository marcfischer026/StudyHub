"""Rich-text note editor that saves Markdown and links ``[▶ HH:MM:SS]`` stamps
to audio playback.

Each paragraph also gets a hidden anchor when you start writing it: the
recording time and the PDF page open at that moment. ⌘-click a paragraph (or
right-click it) to get back to both. Anchors live in ``notes.anchors.json``.

Markdown typed at the start of a line becomes formatting right away (``# `` →
heading, ``- `` → bullets, ``1. `` → numbered list, ``[ ] `` → checkbox), as do
``**bold**`` and ``*italic*``. Tab / ⇧Tab nest list items; ⌘F finds text.
⌘L links the PDF page in view; clicking such a link opens that page again.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QRegularExpression, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QImage,
    QKeySequence,
    QSyntaxHighlighter,
    QTextBlockFormat,
    QTextBlockUserData,
    QTextCharFormat,
    QTextCursor,
    QTextListFormat,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QSizePolicy,
    QTextEdit,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..services import slide_links, timestamps
from ..storage import DOC_TYPES, Lecture


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


ANCHOR_SEARCH = 50  # paragraphs to look ahead when re-attaching saved anchors
FIGURE_MAX_WIDTH = 560  # display width of clipped figures; the PNG itself stays full size
BULLET_STYLES = (QTextListFormat.Style.ListDisc, QTextListFormat.Style.ListCircle, QTextListFormat.Style.ListSquare)
NUMBERED = (QTextListFormat.Style.ListDecimal, QTextListFormat.Style.ListLowerAlpha, QTextListFormat.Style.ListLowerRoman)
FOUND_BG = QColor(255, 214, 0, 110)
CURRENT_BG = QColor(255, 150, 0, 170)
# Typed at the start of a line, followed by a space:
LINE_SHORTCUTS = (
    (re.compile(r"(#{1,3})"), "heading"),
    (re.compile(r"(?:[-*+] )?\[([ xX]?)\]"), "checkbox"),
    (re.compile(r"[-*+]"), "bullets"),
    (re.compile(r"(\d{1,3})[.)]"), "numbered"),
)
BOLD_TYPED = re.compile(r"(?<![*\w])\*\*([^*\s](?:[^*]*[^*\s])?)\*\*$")
ITALIC_TYPED = re.compile(r"(?<![*\w])\*([^*\s](?:[^*]*[^*\s])?)\*$")


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
    audio = None
    if "t" in context:
        clock = f"{context['clock']} (minute {timestamps.format_seconds(context['t'])})" if context.get("clock") else timestamps.format_seconds(context["t"])
        audio = f"▶  Play recording from {clock}"
    pdf = None
    if "pdf" in context:
        pdf = f"Open {context.get('type') or DOC_TYPES.get(context.get('doc'), 'PDF')}: {context['pdf']}, page {context.get('page', 1)}"
    return audio, pdf


class _StampTextEdit(QTextEdit):
    stampClicked = Signal(int)
    blockClicked = Signal(object)  # QTextBlock, ⌘-clicked outside a stamp
    slideLinkClicked = Signal(dict)  # {"doc", "pdf", "page"}

    def __init__(self):
        super().__init__()
        # Set by NoteEditor: before_key(event) -> True if it handled the key; after_key(event).
        self.before_key = lambda _event: False
        self.after_key = lambda _event: None

    def keyPressEvent(self, event) -> None:
        if not self.before_key(event):
            super().keyPressEvent(event)
            self.after_key(event)

    def mousePressEvent(self, event) -> None:
        """A click on a checkbox ticks it."""
        if event.button() == Qt.MouseButton.LeftButton:
            block = self.cursorForPosition(event.position().toPoint()).block()
            marker = block.blockFormat().marker()
            if marker != QTextBlockFormat.MarkerType.NoMarker and event.position().x() < self.cursorRect(QTextCursor(block)).left():
                fmt = block.blockFormat()
                checked = marker == QTextBlockFormat.MarkerType.Checked
                fmt.setMarker(QTextBlockFormat.MarkerType.Unchecked if checked else QTextBlockFormat.MarkerType.Checked)
                QTextCursor(block).setBlockFormat(fmt)
                return
        super().mousePressEvent(event)

    def _stamp_under(self, pos) -> int | None:
        cursor = self.cursorForPosition(pos)
        return timestamps.stamp_at(cursor.block().text(), cursor.positionInBlock())

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and not self.textCursor().hasSelection():
            target = slide_links.parse_href(self.anchorAt(event.position().toPoint()))
            seconds = self._stamp_under(event.position().toPoint())
            if target is not None:
                self.slideLinkClicked.emit(target)
            elif seconds is not None:
                self.stampClicked.emit(seconds)
            elif event.modifiers() & Qt.KeyboardModifier.ControlModifier:  # ⌘ on macOS
                self.blockClicked.emit(self.cursorForPosition(event.position().toPoint()).block())

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        pos = event.position().toPoint()
        over = self._stamp_under(pos) is not None or slide_links.parse_href(self.anchorAt(pos)) is not None
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.IBeamCursor)


class NoteEditor(QWidget):
    seekRequested = Signal(int)
    anchorActivated = Signal(dict)  # audio and/or PDF part of a paragraph's anchor
    saved = Signal()

    def __init__(
        self,
        time_source: Callable[[], float | None],
        context_source: Callable[[], dict] = dict,
        clock_source: Callable[[], datetime | None] = lambda: None,
        parent=None,
    ):
        """``time_source`` returns the current recording/playback position in
        seconds, or ``None`` when nothing is recording or loaded.
        ``context_source`` returns the anchor for a paragraph started now (see
        ``Lecture.read_anchors``), or ``{}``."""
        super().__init__(parent)
        self.lecture: Lecture | None = None
        self.time_source = time_source
        self.context_source = context_source
        self.clock_source = clock_source  # time of day at the recording/playback position
        self.dirty = False
        self._loading = False

        self.edit = _StampTextEdit()
        self.edit.setAcceptRichText(False)  # pasted HTML would not survive the Markdown round trip
        self.edit.setMouseTracking(True)
        self.edit.setObjectName("notesEdit")
        self.edit.document().setDocumentMargin(16)
        self.edit.setPlaceholderText(
            "Start typing your notes…\n\nMarkdown works as you type: # heading, - bullets, 1. numbers, "
            "[ ] checkbox, **bold**, *italic*. Tab / ⇧Tab indent list items.\n\n"
            "While recording or playing audio, press ⌘T to drop a [▶ 00:12:34] "
            "timestamp. Click a timestamp later to hear that moment again.\n\n"
            "Every line also remembers the recording time and the slide that was open — "
            "⌘-click it to jump back to both."
        )
        self.edit.before_key = self._before_key
        self.edit.after_key = self._after_key
        self.edit.textChanged.connect(self._on_changed)
        self.edit.stampClicked.connect(self.seekRequested)
        self.edit.blockClicked.connect(lambda block: self._activate(anchor_of(block)))
        self.edit.slideLinkClicked.connect(self.anchorActivated)
        self.edit.document().contentsChange.connect(self._anchor_new_text)
        self.edit.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.edit.customContextMenuRequested.connect(self._context_menu)
        _StampHighlighter(self.edit.document())

        self.autosave = QTimer(self, singleShot=True, interval=1500)
        self.autosave.timeout.connect(self.save)

        toolbar = QToolBar()
        toolbar.setObjectName("formatBar")
        # Lit up while the text at the cursor has that format (see _update_format_buttons).
        self.format_actions: dict[str, QAction] = {}
        self._format_action(toolbar, "bold", "B", "Bold (⌘B, or type **text**)", QKeySequence.StandardKey.Bold, self._toggle_bold)
        self._format_action(toolbar, "italic", "I", "Italic (⌘I, or type *text*)", QKeySequence.StandardKey.Italic, self._toggle_italic)
        for level in (1, 2, 3):
            self._format_action(toolbar, f"h{level}", f"H{level}", f"Heading {level} (⌥⌘{level}, or type {'#' * level} )", f"Ctrl+Alt+{level}", lambda _=False, lv=level: self._heading(lv))
        self._action(None, "Normal text", "Normal text (⌥⌘0)", "Ctrl+Alt+0", lambda: self._heading(0))
        self._format_action(toolbar, "bullets", "• List", "Bullet list (⇧⌘L, or type - )", "Ctrl+Shift+L", lambda: self._toggle_list(BULLET_STYLES))
        self._format_action(toolbar, "numbered", "1. List", "Numbered list (⇧⌘O, or type 1. )", "Ctrl+Shift+O", lambda: self._toggle_list(NUMBERED))
        self._format_action(toolbar, "checkbox", "☐ To-do", "Checkbox (⇧⌘K, or type [ ] ) — click the box to tick it", "Ctrl+Shift+K", lambda: self._toggle_checkbox())
        self._action(toolbar, "→ Slide", "Link the PDF page in view; click the link to open it again (⌘L)", "Ctrl+L", lambda: self.insert_slide_link())
        self._action(None, "Find", "Find in notes (⌘F)", QKeySequence.StandardKey.Find, self._show_find)
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

        self.edit.cursorPositionChanged.connect(self._update_format_buttons)
        self.edit.currentCharFormatChanged.connect(lambda _fmt: self._update_format_buttons())

        self.find_field = QLineEdit(placeholderText="Find in notes…")
        self.find_field.setClearButtonEnabled(True)
        self.find_field.textChanged.connect(lambda _t: self._find(0))
        self.find_field.returnPressed.connect(lambda: self._find(1))
        self.find_field.installEventFilter(self)  # ⇧↩ goes back, Esc closes
        self.find_count = QLabel()
        self.find_count.setObjectName("muted")
        self._matches: list[QTextCursor] = []
        self._match = -1
        self.find_bar = QWidget()
        find_row = QHBoxLayout(self.find_bar)
        find_row.setContentsMargins(0, 0, 0, 0)
        find_row.addWidget(self.find_field, 1)
        find_row.addWidget(self.find_count)
        for text, tip, step in (("▲", "Previous (⇧↩)", -1), ("▼", "Next (↩)", 1)):
            button = QToolButton(text=text, toolTip=tip)
            button.clicked.connect(lambda _=False, step=step: self._find(step))
            find_row.addWidget(button)
        close = QToolButton(text="✕", toolTip="Close (Esc)")
        close.clicked.connect(self._hide_find)
        find_row.addWidget(close)
        self.find_bar.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(4)
        layout.addWidget(toolbar)
        layout.addWidget(self.find_bar)
        layout.addWidget(self.edit, 1)
        self.setEnabled(False)

    def _action(self, toolbar: QToolBar | None, text: str, tip: str, shortcut, slot) -> QAction:
        """A formatting command; without a toolbar it is a keyboard shortcut only."""
        action = QAction(text, self, toolTip=tip)
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        action.triggered.connect(slot)
        if toolbar is not None:
            toolbar.addAction(action)
        else:
            self.addAction(action)
        return action

    def _format_action(self, toolbar: QToolBar, key: str, text: str, tip: str, shortcut, slot) -> None:
        action = self._action(toolbar, text, tip, shortcut, slot)
        action.setCheckable(True)
        action.triggered.connect(self._update_format_buttons)  # the click toggled it; the text decides
        self.format_actions[key] = action

    def _update_format_buttons(self) -> None:
        cursor = self.edit.textCursor()
        heading = cursor.blockFormat().headingLevel()
        checkbox = cursor.blockFormat().marker() != QTextBlockFormat.MarkerType.NoMarker
        current = cursor.currentList()
        style = current.format().style() if current else None
        states = {
            "bold": self.edit.fontWeight() >= QFont.Weight.Bold,
            "italic": self.edit.fontItalic(),
            "h1": heading == 1,
            "h2": heading == 2,
            "h3": heading == 3,
            "bullets": style in BULLET_STYLES and not checkbox,
            "numbered": style in NUMBERED,
            "checkbox": checkbox,
        }
        for key, active in states.items():
            self.format_actions[key].setChecked(active)

    # --- lecture binding ----------------------------------------------------

    def set_lecture(self, lecture: Lecture | None) -> None:
        self.save()
        self.lecture = lecture
        self.edit.blockSignals(True)
        self._loading = True
        # Figures are linked as "figures/x.png", relative to the lecture folder.
        self.edit.document().setBaseUrl(QUrl.fromLocalFile(f"{lecture.path}/") if lecture else QUrl())
        self.edit.setMarkdown(lecture.read_notes() if lecture else "")
        self._fit_images()
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

    # --- figures ------------------------------------------------------------

    def insert_figure(self, png: Path, caption: str, tables: list[str]) -> None:
        """Put a clipped graphic at the cursor: the image, its source, and any tables as real text."""
        if self.lecture is None:
            return
        cursor = self.edit.textCursor()
        if cursor.block().text().strip():
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
        relative = png.relative_to(self.lecture.path).as_posix()
        markdown = f"![{caption}]({relative})\n\n*{caption}*\n\n" + "".join(f"{table}\n\n" for table in tables)
        cursor.insertMarkdown(markdown)
        cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())  # keep writing below, outside any table
        self.edit.setTextCursor(cursor)
        self._fit_images()
        self.edit.setFocus()

    def insert_slide_link(self, target: dict | None = None) -> None:
        """Link ``target`` ({"doc", "pdf", "page"}), by default the PDF page in view, at the cursor."""
        if self.lecture is None:
            return
        target = target or self.context_source()
        if not target.get("pdf"):
            self.status.setText("Open a slide first, then ⌘L links it.")
            return
        cursor = self.edit.textCursor()
        plain = QTextCharFormat(cursor.charFormat())  # keeps a heading's size, but not a neighbouring link
        plain.setAnchor(False)
        plain.clearProperty(QTextCharFormat.Property.AnchorHref)
        plain.clearForeground()
        plain.setFontUnderline(False)
        link = QTextCharFormat(plain)
        link.setAnchor(True)
        link.setAnchorHref(slide_links.make_href(target))
        link.setForeground(self.edit.palette().link())
        link.setFontUnderline(True)
        cursor.insertText(slide_links.link_text(target), link)
        cursor.insertText(" ", plain)
        self.edit.setTextCursor(cursor)
        self.edit.setFocus()

    def _fit_images(self) -> None:
        """Show figures at a readable size (Markdown does not store a width)."""
        if self.lecture is None:
            return
        block = self.edit.document().begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                fmt = fragment.charFormat()
                if fmt.isImageFormat():
                    image_fmt = fmt.toImageFormat()
                    image = QImage(str(self.lecture.path / image_fmt.name()))
                    if not image.isNull() and image_fmt.width() <= 0:
                        width = min(FIGURE_MAX_WIDTH, image.width() / 2)
                        image_fmt.setWidth(width)
                        image_fmt.setHeight(width * image.height() / image.width())
                        cursor = QTextCursor(self.edit.document())
                        cursor.setPosition(fragment.position())
                        cursor.setPosition(fragment.position() + fragment.length(), QTextCursor.MoveMode.KeepAnchor)
                        cursor.setCharFormat(image_fmt)
                it += 1
            block = block.next()

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
        cursor.insertText(timestamps.make_stamp(seconds, self.clock_source()) + " ", QTextCharFormat())
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
        """Make the current line a heading of ``level`` (again: back to normal text; 0: normal)."""
        cursor = self.edit.textCursor()
        block_fmt = cursor.blockFormat()
        level = 0 if block_fmt.headingLevel() == level else level
        block_fmt.setHeadingLevel(level)
        cursor.setBlockFormat(block_fmt)
        char_fmt = QTextCharFormat()
        # The sizes Qt's own Markdown reader uses, so headings look the same after reopening.
        char_fmt.setProperty(QTextCharFormat.Property.FontSizeAdjustment, 4 - level if level else 0)
        char_fmt.setFontWeight(QFont.Weight.Bold if level else QFont.Weight.Normal)
        line = QTextCursor(cursor.block())
        line.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        line.mergeCharFormat(char_fmt)
        self.edit.mergeCurrentCharFormat(char_fmt)  # what is typed next, also on an empty line

    def _toggle_list(self, styles: tuple, start: int = 1) -> None:
        """Bullets or numbers for the current line; again turns the list off."""
        cursor = self.edit.textCursor()
        current = cursor.currentList()
        if current and current.format().style() in styles and cursor.blockFormat().marker() == QTextBlockFormat.MarkerType.NoMarker:
            self._leave_list(cursor)
            return
        fmt = QTextListFormat()
        indent = current.format().indent() if current else 1
        fmt.setStyle(styles[(indent - 1) % len(styles)])
        fmt.setIndent(indent)
        if start != 1:
            fmt.setStart(start)
        self._clear_marker(cursor)
        cursor.createList(fmt)

    def _toggle_checkbox(self, checked: bool = False) -> None:
        cursor = self.edit.textCursor()
        marker = cursor.blockFormat().marker()
        if marker != QTextBlockFormat.MarkerType.NoMarker:
            self._leave_list(cursor)
            return
        if not cursor.currentList() or cursor.currentList().format().style() not in BULLET_STYLES:
            fmt = QTextListFormat()
            fmt.setStyle(BULLET_STYLES[0])
            cursor.createList(fmt)
        block_fmt = cursor.blockFormat()
        block_fmt.setMarker(QTextBlockFormat.MarkerType.Checked if checked else QTextBlockFormat.MarkerType.Unchecked)
        cursor.setBlockFormat(block_fmt)

    def _clear_marker(self, cursor: QTextCursor) -> None:
        block_fmt = cursor.blockFormat()
        if block_fmt.marker() != QTextBlockFormat.MarkerType.NoMarker:
            block_fmt.setMarker(QTextBlockFormat.MarkerType.NoMarker)
            cursor.setBlockFormat(block_fmt)

    def _leave_list(self, cursor: QTextCursor) -> None:
        if cursor.currentList():
            cursor.currentList().remove(cursor.block())
        block_fmt = cursor.blockFormat()
        block_fmt.setIndent(0)
        block_fmt.setMarker(QTextBlockFormat.MarkerType.NoMarker)
        cursor.setBlockFormat(block_fmt)

    def _indent(self, delta: int) -> bool:
        """Tab / ⇧Tab inside a list: nest the item one level deeper or shallower."""
        cursor = self.edit.textCursor()
        current = cursor.currentList()
        if current is None:
            return False
        indent = current.format().indent() + delta
        if indent < 1:
            self._leave_list(cursor)
            return True
        styles = NUMBERED if current.format().style() in NUMBERED else BULLET_STYLES
        # Join the nearest list above at that depth, so numbering continues after nesting.
        block = cursor.block().previous()
        while block.isValid() and block.textList() is not None:
            above = block.textList()
            if above.format().indent() == indent and (above.format().style() in styles):
                above.add(cursor.block())
                return True
            if above.format().indent() < indent:
                break
            block = block.previous()
        fmt = QTextListFormat()
        fmt.setStyle(styles[(indent - 1) % len(styles)])
        fmt.setIndent(indent)
        cursor.createList(fmt)
        return True

    # --- typing ---------------------------------------------------------------

    def _before_key(self, event) -> bool:
        key = event.key()
        if key == Qt.Key.Key_Backtab or (key == Qt.Key.Key_Tab and event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            return self._indent(-1)
        if key == Qt.Key.Key_Tab and not event.modifiers():
            return self._indent(1)
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers():
            cursor = self.edit.textCursor()
            if cursor.currentList() and not cursor.block().text().strip():
                self._leave_list(cursor)  # Enter on an empty item ends the list
                return True
        if key == Qt.Key.Key_Backspace and not event.modifiers():
            cursor = self.edit.textCursor()
            if cursor.atBlockStart() and not cursor.hasSelection():
                if cursor.currentList():
                    self._leave_list(cursor)  # first ⌫ removes the bullet, not the line
                    return True
                if cursor.blockFormat().headingLevel():
                    self._heading(0)
                    return True
        return False

    def _after_key(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._after_enter()
        elif event.text() == " ":
            self._line_shortcut()
        elif event.text() == "*":
            self._inline_shortcut()

    def _after_enter(self) -> None:
        """A new line after a heading is normal text; after a ticked item, an empty box."""
        cursor = self.edit.textCursor()
        block_fmt = cursor.blockFormat()
        if block_fmt.headingLevel() and not cursor.block().text():
            self._heading(0)
        if block_fmt.marker() == QTextBlockFormat.MarkerType.Checked:
            block_fmt.setMarker(QTextBlockFormat.MarkerType.Unchecked)
            cursor.setBlockFormat(block_fmt)

    def _line_shortcut(self) -> None:
        cursor = self.edit.textCursor()
        typed = cursor.block().text()[: cursor.positionInBlock()]
        if not typed.endswith(" "):
            return
        for pattern, kind in LINE_SHORTCUTS:
            match = pattern.fullmatch(typed[:-1])
            if not match:
                continue
            if kind != "checkbox" and (cursor.currentList() or cursor.blockFormat().headingLevel()):
                return  # "- " inside a list item is text
            line = QTextCursor(cursor.block())
            line.setPosition(cursor.position(), QTextCursor.MoveMode.KeepAnchor)
            line.removeSelectedText()
            if kind == "heading":
                self._heading(len(match.group(1)))
            elif kind == "bullets":
                self._toggle_list(BULLET_STYLES)
            elif kind == "numbered":
                self._toggle_list(NUMBERED, start=int(match.group(1)))
            else:
                self._clear_marker(self.edit.textCursor())
                self._toggle_checkbox(checked=match.group(1).lower() == "x")
            return

    def _inline_shortcut(self) -> None:
        """``**bold**`` / ``*italic*`` just typed: format the text, drop the stars."""
        cursor = self.edit.textCursor()
        typed = cursor.block().text()[: cursor.positionInBlock()]
        for pattern, stars in ((BOLD_TYPED, 2), (ITALIC_TYPED, 1)):
            match = pattern.search(typed)
            if not match:
                continue
            start = cursor.block().position() + match.start()
            span = QTextCursor(self.edit.document())
            span.beginEditBlock()  # one ⌘Z undoes the formatting
            span.setPosition(start)
            span.setPosition(cursor.position(), QTextCursor.MoveMode.KeepAnchor)
            fmt = span.charFormat()
            if stars == 2:
                fmt.setFontWeight(QFont.Weight.Bold)
            else:
                fmt.setFontItalic(True)
            span.insertText(match.group(1), fmt)
            span.endEditBlock()
            plain = QTextCharFormat(fmt)
            if stars == 2:
                plain.setFontWeight(QFont.Weight.Normal)
            else:
                plain.setFontItalic(False)
            self.edit.setCurrentCharFormat(plain)  # keep typing unformatted
            return

    # --- find -----------------------------------------------------------------

    def _show_find(self) -> None:
        self.find_bar.show()
        selected = self.edit.textCursor().selectedText()
        if selected and "\u2029" not in selected:
            self.find_field.setText(selected)
        self.find_field.setFocus()
        self.find_field.selectAll()
        self._find(0)

    def _hide_find(self) -> None:
        self.find_bar.hide()
        self._matches, self._match = [], -1
        self.edit.setExtraSelections([])
        self.edit.setFocus()

    def _find(self, step: int) -> None:
        """Highlight every match; ``step`` moves to the next (1) or previous (-1) one."""
        needle = self.find_field.text()
        if step == 0:  # the search text changed: collect matches, start after the cursor
            self._matches = []
            cursor = QTextCursor(self.edit.document())
            while needle:
                cursor = self.edit.document().find(needle, cursor)
                if cursor.isNull():
                    break
                self._matches.append(QTextCursor(cursor))
            at = self.edit.textCursor().selectionStart()
            self._match = next((i for i, m in enumerate(self._matches) if m.selectionStart() >= at), 0) if self._matches else -1
        elif self._matches:
            self._match = (self._match + step) % len(self._matches)
        selections = []
        for i, match in enumerate(self._matches):
            selection = QTextEdit.ExtraSelection()
            selection.cursor = match
            selection.format.setBackground(CURRENT_BG if i == self._match else FOUND_BG)
            selections.append(selection)
        self.edit.setExtraSelections(selections)
        if self._match >= 0:
            self.edit.setTextCursor(QTextCursor(self._matches[self._match]))
            self.edit.ensureCursorVisible()
            self.find_count.setText(f"{self._match + 1}/{len(self._matches)}")
        else:
            self.find_count.setText("0/0" if needle else "")

    def eventFilter(self, obj, event) -> bool:
        if obj is self.find_field and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self._hide_find()
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self._find(-1)
                return True
        return super().eventFilter(obj, event)
