"""Claude tab: the round trip, as four numbered steps.

PDFs out to Gemini or Copilot and their Markdown back in, the subject out to Claude as ``.md``
files, Claude's reply back in, then tutor rounds. The dock is narrow, so the tab scrolls and
buttons wrap. Reading a subject takes seconds, so everything that does runs in a worker, one at a
time: a merge never overlaps an export. The small widgets the other study tabs share live here too.
"""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import Settings
from ..professors import ProfessorStore
from ..services import api_mode, claude_export, local_conversion, pdf_conversion
from ..services.claude_export import CHAT_BUDGET, LONG_BUDGET, ExportPlan, ExportResult
from ..services.jobcontrol import JobControl
from ..services.claude_reply import CheckedReply, check_reply, follow_up_prompt, parse_reply, repair_prompt
from ..services.study_store import MergeSummary, StudyStore
from ..services.subject_model import SubjectView, load_subject
from ..storage import Lecture, Subject
from . import workers
from .documents_panel import _FlowLayout
from .workers import Worker

SCOPES = (("Next lectures to process", "next"), ("Whole subject", "all"), ("This lecture", "lecture"))
SIZES = (("Normal chat ≈180k tokens", CHAT_BUDGET), ("Long context or Claude Code ≈800k", LONG_BUDGET))
WATCH_MS = 1000
MAX_LISTED = 8  # issues spelled out in a report; the fix request has them all
ROLE = Qt.ItemDataRole.UserRole

study_pool = QThreadPool()  # one job at a time: reading, exporting and merging never overlap
study_pool.setMaxThreadCount(1)


# --- small widgets the study tabs share -------------------------------------


def reveal(path: Path) -> None:
    subprocess.run(["open", str(path)])


def shown_path(path: Path) -> str:
    """The path as Finder's Go to Folder takes it, with ``~`` for the home folder."""
    home = Path.home()
    return f"~/{path.relative_to(home).as_posix()}" if path.is_relative_to(home) else str(path)


def same_subject(a: Subject | None, b: Subject | None) -> bool:
    return (a.path if a else None) == (b.path if b else None)


def exam_date(subject: Subject | None) -> date | None:
    try:
        return date.fromisoformat(str(subject.exam.get("date", ""))) if subject else None
    except ValueError:
        return None


def wrapping_policy() -> QSizePolicy:
    """Never widens the narrow dock, and asks for the height the wrapped lines need. A policy set by
    hand drops the height-for-width that word wrap gave the label, and only a later setText brings it
    back: a fixed text was squeezed to the line count its size hint guessed."""
    policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    policy.setHeightForWidth(True)
    return policy


def text_label(text: str = "", muted: bool = False, rich: bool = False, selectable: bool = False) -> QLabel:
    """A label that wraps and never widens the narrow dock, whatever its text."""
    label = QLabel(text, wordWrap=True)
    label.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
    label.setSizePolicy(wrapping_policy())
    if muted:
        label.setObjectName("muted")
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction if rich else Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def button_row(*buttons: QWidget) -> QWidget:
    """Buttons side by side, wrapping onto new rows when the dock is narrow."""
    row = QWidget()
    flow = _FlowLayout(row)
    for button in buttons:
        flow.addWidget(button)
    return row


def card(title: str, *rows: QWidget | QLayout) -> QFrame:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setSpacing(8)
    if title:
        heading = QLabel(title, wordWrap=True)
        heading.setObjectName("cardTitle")
        heading.setSizePolicy(wrapping_policy())
        layout.addWidget(heading)
    for row in rows:
        layout.addLayout(row) if isinstance(row, QLayout) else layout.addWidget(row)
    return frame


def scrolling(content: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(content)
    return area


class WrapButton(QWidget):
    """A radio button or checkbox whose text wraps: Qt's own stay on one line, however long."""

    def __init__(self, box: QAbstractButton, text: str = "", parent=None):
        super().__init__(parent)
        self.box = box
        self.label = text_label(text)
        self.label.mousePressEvent = lambda _event: self.box.click()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(box, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.label, 1)


class ScopedPanel(QWidget):
    """Bound to a subject. Reads its data only while on screen, so clicking through lectures stays instant."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.subject: Subject | None = None
        self.lecture: Lecture | None = None
        self._dirty = True

    def set_scope(self, subject: Subject | None, lecture: Lecture | None) -> None:
        self.subject, self.lecture = subject, lecture
        self.mark_changed()

    def mark_changed(self) -> None:
        self._dirty = True
        if self.isVisible():
            self.refresh()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dirty:
            self.refresh()

    def refresh(self) -> None:
        self._dirty = False


# --- texts ------------------------------------------------------------------


def plan_line(plan: ExportPlan) -> str:
    """The live line: "L01–L05 · 172k tokens · fits one chat"."""
    tokens = f"{plan.tokens / 1000:.0f}k tokens"
    room = "fits one chat" if plan.tokens <= plan.budget else "too big for one chat"
    if not plan.lectures:
        return f"All lectures are processed: a review round · {tokens} · {room}"
    refs = [lecture.ref for lecture in plan.lectures]
    span = refs[0] if len(refs) == 1 else f"{refs[0]}–{refs[-1]}"
    line = f"{span} · {tokens} · {room}"
    if plan.left_out:
        line += f" · {', '.join(lecture.ref for lecture in plan.left_out)} next time"
    return line


def conversion_status(view: SubjectView) -> str:
    """ "5 of 8 PDFs converted · 180 of 222 pages · 2 pages to check". """
    documents = view.documents_for_export
    if not documents:
        return "No PDFs in this subject yet." if not view.documents else "Every PDF is excluded from exports."
    done = sum(d.is_converted for d in documents)
    pages = sum(d.page_count for d in documents)
    converted = sum(d.converted_pages for d in documents)
    weak = sum(1 for d in documents for p in d.pages if p.check is not None and not p.check.ok)
    line = f"{done} of {len(documents)} PDFs converted · {converted} of {pages} pages"
    return line + (f" · {weak} page{'s' * (weak != 1)} to check" if weak else "")


def reply_report(checked: CheckedReply, summary: MergeSummary, parts: set[int]) -> str:
    """What came in, what is still open, in the order the student needs it."""
    parsed = checked.parsed
    seen = f"Part{'s' * (len(parts) != 1)} {', '.join(map(str, sorted(parts)))}" if parts else "This reply"
    if parsed.truncated:
        lines = [f"{seen}: cut off. What was readable is imported; press Copy fix request and send it to Claude."]
    elif parsed.finished:
        lines = [f"{seen}: the last part has arrived."]
    else:
        lines = [f"{seen}: more to come. Say next in Claude and copy the next part."]
    lines.append(summary.describe())
    missing = sum(map(len, checked.uncovered.values()))
    if missing and parsed.finished:
        lines.append(f"Pages and recordings not covered: {missing}. Copy follow-up asks Claude for exactly those.")
    if checked.warnings:
        lines.append(f"{len(checked.warnings)} to look at, such as sources that could not be checked:")
        lines += [f"   line {i.line}: {i.message}" for i in checked.warnings[:MAX_LISTED]]
    if checked.errors:
        lines.append(f"{len(checked.errors)} could not be used; the rest is imported:")
        lines += [f"   line {i.line}: {i.message}" for i in checked.errors[:MAX_LISTED]]
    return "\n".join(lines)


def _looks_like_reply(text: str) -> bool:
    """Watching the clipboard must not take the prompt's own record catalogue for a reply."""
    if '"t"' not in text or "<source_rules>" in text or "<output_format>" in text:
        return False
    return bool(parse_reply(text).records)


# --- documents dialog -------------------------------------------------------


class DocumentsDialog(QDialog):
    """Every PDF of the subject: how far converted, what to check, what stays out of exports."""

    pageRequested = Signal(dict)  # a durable page source

    def __init__(self, view: SubjectView, parent=None):
        super().__init__(parent)
        self.view = view
        self.changed = False
        self.setWindowTitle(f"Documents of {view.name}")
        self.resize(640, 420)
        self.table = QTableWidget(len(view.documents), 6)
        self.table.setHorizontalHeaderLabels(["Ref", "File", "Pages", "Converted", "Check", "Exclude"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.boxes: list[QCheckBox] = []
        for row, document in enumerate(view.documents):
            weak = sum(1 for p in document.pages if p.check is not None and not p.check.ok)
            cells = (document.ref, document.title, str(document.page_count), str(document.converted_pages), f"{weak} to check" if weak else "ok")
            for column, text in enumerate(cells):
                self.table.setItem(row, column, QTableWidgetItem(text))
            box = QCheckBox(checked=document.excluded)
            box.toggled.connect(lambda on, key=document.key: self._set_excluded(key, on))
            self.table.setCellWidget(row, 5, box)
            self.boxes.append(box)
        self.table.cellDoubleClicked.connect(self._open)
        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addWidget(text_label("Double-click a row to open it. Excluded documents stay out of everything sent to Claude.", muted=True))

    def _set_excluded(self, key: str, excluded: bool) -> None:
        self.view.subject.set_document_excluded(key, excluded)
        self.changed = True

    def _open(self, row: int, _column: int = 0) -> None:
        self.pageRequested.emit({"kind": "page", "doc": self.view.documents[row].key, "page": 1})


# --- where an export went ---------------------------------------------------


class FolderLine(QWidget):
    """The folder an export went to. The data folder starts with a dot, so file pickers hide it:
    besides Finder, its path pasted into Go to Folder is the way in."""

    def __init__(self, copy, parent=None):
        super().__init__(parent)
        self.folder: Path | None = None
        self._copy = copy  # the tab's, so watching the clipboard knows the path as its own text
        self.where = text_label(selectable=True)
        self.hint = text_label("File pickers hide this folder. In one, press ⌘⇧G and paste the path.", muted=True)
        open_btn, copy_btn = QPushButton("Open folder"), QPushButton("Copy path")
        open_btn.clicked.connect(lambda: self.open_folder())
        copy_btn.clicked.connect(lambda: self.copy_path())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.where)
        layout.addWidget(self.hint)
        layout.addWidget(button_row(open_btn, copy_btn))
        self.hide()

    def show_folder(self, folder: Path) -> None:
        self.folder = folder
        self.where.setText(f"Saved in {shown_path(folder)}")
        self.hint.setVisible(any(part.startswith(".") for part in folder.parts))
        self.show()

    def open_folder(self) -> None:
        if self.folder is not None:
            reveal(self.folder)

    def copy_path(self) -> None:
        if self.folder is not None:
            self._copy(str(self.folder), "The folder path")


# --- the tab ----------------------------------------------------------------


class ClaudePanel(ScopedPanel):
    status = Signal(str)
    sourceActivated = Signal(dict)  # a durable source to jump to
    conversionsImported = Signal()
    replyImported = Signal()  # or undone: knowledge changed
    apiProgress = Signal(str)  # from a worker thread
    localProgress = Signal(str)  # from a worker thread

    def __init__(self, professors: ProfessorStore, parent=None, settings: Settings | None = None):
        super().__init__(parent)
        self.professors = professors
        self.settings = settings or Settings()
        self._api_client: api_mode.ApiClient | None = None
        self._local_control: JobControl | None = None
        self.view: SubjectView | None = None
        self.plan: ExportPlan | None = None
        self.result: ExportResult | None = None
        self.checked: CheckedReply | None = None
        self.conversion: pdf_conversion.ConversionReport | None = None
        self._jobs: set[Worker] = set()
        self._running = 0
        self._generation = 0  # a newer choice makes a waiting plan pointless
        self._parts: dict[str, set[int]] = {}  # export id → parts seen, for multi-part replies
        self._own_copies: set[str] = set()  # what this tab itself put on the clipboard
        self._imported: set[str] = set()  # replies taken in, so a copied part counts once
        self._last_clip = ""
        self._watch = QTimer(self, interval=WATCH_MS)
        self._watch.timeout.connect(self.check_clipboard)  # macOS only signals clipboard changes on activation

        self.busy = text_label(muted=True)
        self.busy.hide()
        self.steps = QWidget()
        column = QVBoxLayout(self.steps)
        column.setContentsMargins(8, 8, 8, 8)
        column.setSpacing(8)
        self.api_card = self._build_api_card()
        column.addWidget(self.api_card)
        column.addWidget(self._build_pdf_step())
        column.addWidget(self._build_export_step())
        self.import_step = self._build_import_step()
        column.addWidget(self.import_step)
        column.addWidget(self._build_tutor_step())
        column.addStretch(1)
        self.scroll = scrolling(self.steps)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.busy)
        layout.addWidget(self.scroll, 1)

    # --- building -----------------------------------------------------------

    def _button(self, text: str, slot, primary: bool = False, hidden: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(lambda: slot())
        if primary:
            button.setObjectName("primaryButton")
        button.setVisible(not hidden)
        return button

    def _build_api_card(self) -> QFrame:
        self.apiProgress.connect(self.busy.setText)
        self.api_note = text_label(muted=True)
        self.api_run_btn = self._button("Run everything through the API", self.run_api, primary=True)
        self.api_questions_btn = self._button("New questions for my weak spots", self.run_api_questions)
        self.api_stop_btn = self._button("Stop", self.stop_api, hidden=True)
        self.api_report = text_label(selectable=True)
        self.api_report.hide()
        built = card("API mode", self.api_note, button_row(self.api_run_btn, self.api_questions_btn, self.api_stop_btn), self.api_report)
        built.setVisible(self.settings.api_enabled)
        self.api_note.setText(self._api_note())
        return built

    def _api_note(self) -> str:
        return f"Converts the PDFs, exports with the choices in step 2 and imports the reply, through {api_mode.host(self.settings)}."

    def _build_pdf_step(self) -> QFrame:
        self.pdf_status = text_label()
        self.export_pdfs_btn = self._button("Export PDFs", self.export_pdfs, primary=True)
        self.export_pdfs_btn.setToolTip("Writes stamped copies, copies the converter prompt and shows the folder")
        self.localProgress.connect(self.busy.setText)
        self.convert_local_btn = self._button("Convert on this Mac", self.convert_local)
        self.convert_local_btn.setToolTip(
            "Marker and MinerU read the PDFs here, offline; each page keeps the better version"
            if local_conversion.installed()
            else "Not installed yet: see README → Local PDF conversion"
        )
        self.stop_local_btn = self._button("Stop", self.stop_local, hidden=True)
        self.pdf_report = text_label(selectable=True)
        self.pdf_report.hide()
        self.pdf_folder = FolderLine(self._copy)
        self.copy_missing_btn = self._button("Copy request for missing pages", self.copy_missing, hidden=True)
        return card(
            "1 · PDFs to Markdown",
            text_label("Marker and MinerU on this Mac, or Gemini and Copilot, turn the PDFs into Markdown, so Claude reads formulas, tables and graphics too.", muted=True),
            self.pdf_status,
            button_row(self.convert_local_btn, self.stop_local_btn),
            button_row(
                self.export_pdfs_btn,
                self._button("Paste result", self.paste_conversion),
                self._button("Import .md files…", self.choose_conversion_files),
                self._button("Documents…", self.show_documents),
            ),
            self.pdf_report,
            self.pdf_folder,
            self.copy_missing_btn,
        )

    def _build_export_step(self) -> QFrame:
        self.scope = QComboBox()
        for text, value in SCOPES:
            self.scope.addItem(text, value)
        self.size_choice = QComboBox()
        for text, value in SIZES:
            self.size_choice.addItem(text, value)
        self.scope.currentIndexChanged.connect(lambda _i: self.replan())
        self.size_choice.currentIndexChanged.connect(lambda _i: self.replan())
        self.side_talk = WrapButton(QCheckBox())
        self.side_talk.box.toggled.connect(lambda _on: self.replan())
        self.side_talk.hide()
        self.uncertain = text_label(muted=True)
        self.uncertain.hide()
        self.plan_label = text_label()
        self.plan_label.setObjectName("planLabel")
        self.warnings = text_label(muted=True, selectable=True)
        self.warnings.hide()
        self.export_btn = self._button("Export .md files", self.export, primary=True)
        self.export_done = text_label()
        self.export_done.hide()
        self.export_actions = button_row(
            self._button("Copy prompt", self.copy_prompt),
            self._button("Copy all as one text", self.copy_all),
        )
        self.export_actions.hide()
        self.export_folder = FolderLine(self._copy)
        return card(
            "2 · Export for Claude",
            text_label("Everything as Markdown files to drag into Claude, with the prompt on the clipboard.", muted=True),
            self.scope,
            self.size_choice,
            self.side_talk,
            self.uncertain,
            self.plan_label,
            self.warnings,
            button_row(self.export_btn),
            self.export_done,
            self.export_actions,
            self.export_folder,
        )

    def _build_import_step(self) -> QFrame:
        choose = QPushButton("Import file or folder…")
        menu = QMenu(choose)
        menu.addAction("Files…", self.choose_reply_files)
        menu.addAction("Folder…", self.choose_reply_folder)
        choose.setMenu(menu)
        self.watch = WrapButton(QCheckBox(), "Watch clipboard: import each copied reply, for replies in several parts")
        self.watch.box.toggled.connect(self.set_watching)
        self.reply_report = text_label(selectable=True)
        self.reply_report.hide()
        self.fix_btn = self._button("Copy fix request", self.copy_fix, hidden=True)
        self.follow_up_btn = self._button("Copy follow-up for missing pages", self.copy_follow_up, hidden=True)
        self.undo_btn = self._button("Undo last import", self.undo_import)
        return card(
            "3 · Import Claude's reply",
            button_row(self._button("Paste from clipboard", self.paste_reply, primary=True), choose),
            self.watch,
            self.reply_report,
            button_row(self.fix_btn, self.follow_up_btn, self.undo_btn),
        )

    def _build_tutor_step(self) -> QFrame:
        self.tutor_note = text_label(muted=True)
        self.tutor_buttons = (
            self._button("Copy: new questions for my weak spots", lambda: self.copy_tutor(claude_export.tutor_questions_prompt, "Prompt for new questions")),
            self._button("Copy: live tutor session", lambda: self.copy_tutor(claude_export.tutor_session_prompt, "Prompt for a live session")),
        )
        return card("4 · Tutor", self.tutor_note, button_row(*self.tutor_buttons))

    # --- running work -------------------------------------------------------

    def _run(self, message: str, job, done, on_fail=None) -> None:
        """Off the UI thread, one at a time. ``done`` is skipped when the student moved to another subject."""
        subject = self.subject
        worker = Worker(job)
        worker.setAutoDelete(False)
        self._jobs.add(worker)
        self._running += 1
        self.busy.setText(message)
        self.busy.show()
        self.status.emit(message)

        def ended() -> None:
            self._jobs.discard(worker)
            self._running -= 1
            self.busy.setVisible(self._running > 0)
            if not self._running:
                self._set_api_running(False)

        def finished(result) -> None:
            ended()
            if same_subject(self.subject, subject):
                done(result)

        def failed(text: str) -> None:
            ended()
            self.status.emit(f"{message.rstrip('…')} failed: {text}")
            if on_fail is not None:
                on_fail(text)
            else:
                QMessageBox.warning(self, "Claude", text)

        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        workers.run(worker, study_pool)

    def _load(self, subject: Subject) -> SubjectView:
        return load_subject(subject, self.professors)

    def _copy(self, text: str, what: str) -> None:
        self._own_copies.add(text)
        self._last_clip = text
        QGuiApplication.clipboard().setText(text)
        self.status.emit(f"{what} copied to the clipboard.")

    # --- reading the subject ------------------------------------------------

    def set_scope(self, subject: Subject | None, lecture: Lecture | None) -> None:
        same = same_subject(subject, self.subject)
        if not same:
            self._new_subject()
        item = self.scope.model().item(2)
        item.setEnabled(lecture is not None)
        if lecture is None and self.scope.currentData() == "lecture":
            self.scope.blockSignals(True)
            self.scope.setCurrentIndex(0)
            self.scope.blockSignals(False)
        if same and self.view is not None and not self._dirty:  # opening a lecture changes nothing else here
            self.subject, self.lecture = subject, lecture
            if self.scope.currentData() == "lecture":
                self.replan()
            return
        super().set_scope(subject, lecture)

    def _new_subject(self) -> None:
        self.view = self.plan = self.result = self.checked = self.conversion = None
        self._parts.clear()
        self._imported.clear()
        self._own_copies.clear()
        self.watch.box.setChecked(False)
        for widget in (self.pdf_report, self.pdf_folder, self.copy_missing_btn, self.export_done, self.export_actions, self.export_folder, self.reply_report, self.fix_btn, self.follow_up_btn):
            widget.hide()

    def _choices(self) -> tuple[str, str, int, bool]:
        lecture = self.lecture.uid if self.lecture else ""
        return self.scope.currentData(), lecture, self.size_choice.currentData(), self.side_talk.box.isChecked()

    def refresh(self) -> None:
        super().refresh()
        self.view = None
        subject = self.subject
        self._generation += 1
        if subject is None:
            self.pdf_status.setText("Pick a subject first.")
            self.plan_label.clear()
            return
        self.undo_btn.setEnabled(StudyStore(subject).prev_path.exists())
        self._show_tutor_state(subject)
        generation, choices = self._generation, self._choices()

        def job():
            if generation != self._generation:
                return None
            view = self._load(subject)
            return view, self._make_plan(view, subject, choices)

        self._run(f"Reading {subject.name}…", job, lambda result: result and self._show_state(*result))

    def _make_plan(self, view: SubjectView, subject: Subject, choices: tuple[str, str, int, bool]) -> ExportPlan:
        scope, lecture, budget, side_talk = choices
        store = StudyStore(subject)
        return claude_export.plan_export(view, store.knowledge(), scope, lecture if scope == "lecture" else "", budget, side_talk, store.events())

    def replan(self) -> None:
        """A choice changed: the subject is already read, only the sizes change."""
        view, subject = self.view, self.subject
        if view is None or subject is None:
            return
        self._generation += 1
        generation, choices = self._generation, self._choices()

        def job():
            return None if generation != self._generation else (view, self._make_plan(view, subject, choices))

        self._run("Working out the export…", job, lambda result: result and self._show_state(*result))

    def _show_state(self, view: SubjectView, plan: ExportPlan) -> None:
        self.view, self.plan = view, plan
        self.pdf_status.setText(conversion_status(view))
        self.export_pdfs_btn.setEnabled(bool(view.documents_for_export))
        self.convert_local_btn.setEnabled(bool(view.documents_for_export) and local_conversion.installed() and self._local_control is None)
        count = plan.side_talk_lines
        self.side_talk.label.setText(f"Also send {count} line{'s' * (count != 1)} of other voices sorted out as side talk (may include classmates' private talk)")
        self.side_talk.setVisible(count > 0)
        self.uncertain.setText(f"{plan.uncertain_lines} line{'s' * (plan.uncertain_lines != 1)} a small local AI doubted go along marked ?, for Claude to decide.")
        self.uncertain.setVisible(plan.uncertain_lines > 0)
        self.plan_label.setText(plan_line(plan))
        self.warnings.setText("\n".join(f"• {warning}" for warning in plan.warnings))
        self.warnings.setVisible(bool(plan.warnings))

    def _show_tutor_state(self, subject: Subject) -> None:
        ready = bool(StudyStore(subject).knowledge().objectives)
        for button in self.tutor_buttons:
            button.setEnabled(ready)
        self.tutor_note.setText(
            "Your progress goes to Claude in a new chat. Bring what it writes back in step 3."
            if ready
            else "Available once a reply from Claude is imported."
        )

    # --- step 1 -------------------------------------------------------------

    def export_pdfs(self) -> None:
        subject = self.subject
        if subject is None:
            return

        def done(export: pdf_conversion.PdfExport) -> None:
            self._copy(pdf_conversion.PROMPT, "The converter prompt")
            reveal(export.folder)
            self.pdf_folder.show_folder(export.folder)
            parts = [f"Wrote {len(export.files)} PDF file{'s' * (len(export.files) != 1)}. Attach them to Gemini or Copilot and paste the prompt from the clipboard."]
            if export.already_converted:
                parts.append(f"{export.already_converted} already converted, left out.")
            parts += export.problems
            self._show_conversion_text("\n".join(parts))

        self._run("Writing stamped PDFs…", lambda: pdf_conversion.export_pdfs(self._load(subject)), done)

    def convert_local(self) -> None:
        subject = self.subject
        if subject is None or self._local_control is not None:
            return
        control = self._local_control = JobControl()
        self._set_local_running(True)

        def job():
            return local_conversion.convert_subject(self._load(subject), control, self.localProgress.emit)

        def done(report: pdf_conversion.ConversionReport) -> None:
            self._local_ended()
            self._conversion_done(report)

        def failed(text: str) -> None:
            self._local_ended()
            self._show_conversion_text(("Stopped." if text == "Cancelled" else f"Stopped: {text}") + "\nThe pages finished before that are saved.")
            self.conversionsImported.emit()
            self.mark_changed()

        self._run("Starting the converters…", job, done, failed)

    def stop_local(self) -> None:
        if self._local_control is not None:
            self._local_control.cancel()
            self.busy.setText("Stopping the converters…")

    def _set_local_running(self, running: bool) -> None:
        self.convert_local_btn.setEnabled(not running and local_conversion.installed())
        self.stop_local_btn.setVisible(running)

    def _local_ended(self) -> None:
        self._local_control = None
        self._set_local_running(False)

    def _show_conversion_text(self, text: str) -> None:
        self.pdf_report.setText(text)
        self.pdf_report.show()

    def paste_conversion(self) -> None:
        self.import_conversion_text(QGuiApplication.clipboard().text())

    def import_conversion_text(self, text: str) -> None:
        subject = self.subject
        if subject is None:
            return
        if not text.strip():
            self._show_conversion_text("The clipboard is empty. Copy the tool's Markdown first.")
            return
        self._run("Importing the conversion…", lambda: pdf_conversion.import_converted(self._load(subject), text), self._conversion_done)

    def choose_conversion_files(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "Import converted Markdown", "", "Markdown (*.md *.markdown *.txt);;All files (*)")
        if names:
            self.import_conversion_files([Path(name) for name in names])

    def import_conversion_files(self, paths: list[Path]) -> None:
        subject = self.subject
        if subject is not None:
            self._run("Importing the conversion…", lambda: pdf_conversion.import_files(self._load(subject), paths), self._conversion_done)

    def _conversion_done(self, report: pdf_conversion.ConversionReport) -> None:
        self.conversion = report
        self._show_conversion_text(report.summary())
        self.copy_missing_btn.setVisible(bool(report.fix_request()))
        if report.imported_pages:
            self.conversionsImported.emit()
            self.mark_changed()

    def copy_missing(self) -> None:
        if self.conversion is not None:
            self._copy(self.conversion.fix_request(), "The request for the missing pages")

    def show_documents(self) -> None:
        if self.view is None:
            self.status.emit("Still reading the subject. Try again in a moment.")
            return
        dialog = DocumentsDialog(self.view, self)
        dialog.pageRequested.connect(self.sourceActivated)
        dialog.exec()
        if dialog.changed:
            self.mark_changed()

    # --- step 2 -------------------------------------------------------------

    def export(self) -> None:
        subject = self.subject
        if subject is None:
            self.status.emit("Pick a subject first.")
            return
        choices = self._choices()

        def job() -> ExportResult:
            view = self._load(subject)
            store = StudyStore(subject)
            return claude_export.write_export(view, self._make_plan(view, subject, choices), store.knowledge(), store.events())

        self._run(f"Exporting {subject.name}…", job, self._export_done)

    def _export_done(self, result: ExportResult) -> None:
        self.result = result
        self._copy(result.prompt, "The prompt")
        folder = self._attach_folder(result)
        reveal(folder)
        self.export_folder.show_folder(folder)
        count = len(result.attach)
        text = f"Drag the {count} file{'s' * (count != 1)} into Claude, then paste the prompt."
        if result.too_many_files:
            text += " That is more than one chat takes: use Copy all as one text instead."
        self.export_done.setText(text)
        self.export_done.show()
        self.export_actions.show()
        self.status.emit(text)

    @staticmethod
    def _attach_folder(result: ExportResult) -> Path:
        return result.attach[0].parent if result.attach else result.folder

    def copy_prompt(self) -> None:
        if self.result is not None:
            self._copy(self.result.prompt, "The prompt")

    def copy_all(self) -> None:
        if self.result is not None:
            self._copy(claude_export.all_as_one_text(self.result), "All files and the prompt")

    # --- step 3 -------------------------------------------------------------

    def paste_reply(self) -> None:
        self.import_reply(QGuiApplication.clipboard().text())

    def import_reply(self, text: str) -> None:
        subject = self.subject
        if subject is None:
            return
        if not text.strip():
            self._show_reply_text("The clipboard is empty. Copy Claude's reply first.")
            return
        self._imported.add(text)
        self._last_clip = QGuiApplication.clipboard().text()

        def job():
            parsed = parse_reply(text)
            if not parsed.records:
                return parsed, None
            view = self._load(subject)
            store = StudyStore(subject)
            checked = check_reply(parsed, view, store.known_ids())
            return checked, store.merge(checked, text, view)

        self._run("Importing Claude's reply…", job, self._reply_done)

    def _show_reply_text(self, text: str) -> None:
        self.reply_report.setText(text)
        self.reply_report.show()

    def _reply_done(self, result) -> None:
        checked, summary = result
        if summary is None:
            self._show_reply_text("Nothing in this looks like a reply from Claude: no records found.")
            for button in (self.fix_btn, self.follow_up_btn):
                button.hide()
            return
        self.checked = checked
        parts = self._parts.setdefault(checked.parsed.export_id, set())
        parts.update(checked.parsed.parts)
        self._show_reply_text(reply_report(checked, summary, parts))
        self.fix_btn.setVisible(bool(checked.errors) or checked.parsed.truncated)
        self.follow_up_btn.setVisible(checked.parsed.finished and any(checked.uncovered.values()))
        self.status.emit("Claude's reply is imported.")
        self.undo_btn.setEnabled(True)
        self.replyImported.emit()
        self.mark_changed()

    def choose_reply_files(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "Import Claude's reply", "", "Replies (*.jsonl *.json *.txt *.md);;All files (*)")
        if names:
            self.import_reply_paths([Path(name) for name in names])

    def choose_reply_folder(self) -> None:
        name = QFileDialog.getExistingDirectory(self, "Folder with Claude's reply")
        if name:
            self.import_reply_paths([Path(name)])

    def import_reply_paths(self, paths: list[Path]) -> None:
        """Files, or a folder like ``reply/`` from Claude Code; parts arrive in name order and read as one."""
        files = [f for p in paths for f in (sorted(p.glob("*.jsonl")) or sorted(p.glob("*.txt")) if p.is_dir() else [p])]
        try:
            text = "\n".join(path.read_text(encoding="utf-8") for path in files)
        except (OSError, UnicodeDecodeError) as exc:
            QMessageBox.warning(self, "Import Claude's reply", f"Could not read it:\n{exc}")
            return
        if not text.strip():
            self._show_reply_text("No reply found there: no .jsonl or .txt files, or they are empty.")
            return
        self.import_reply(text)

    def set_watching(self, on: bool) -> None:
        """What is on the clipboard now was not copied for this, so only what comes next counts."""
        if on:
            self._last_clip = QGuiApplication.clipboard().text()
            self._watch.start()
        else:
            self._watch.stop()

    def check_clipboard(self) -> None:
        text = QGuiApplication.clipboard().text()
        if not text or text == self._last_clip:
            return
        self._last_clip = text
        if text not in self._own_copies and text not in self._imported and _looks_like_reply(text):
            self.import_reply(text)

    def copy_fix(self) -> None:
        if self.checked is not None:
            self._copy(repair_prompt(self.checked), "The fix request")

    def copy_follow_up(self) -> None:
        if self.checked is not None:
            self._copy(follow_up_prompt(self.checked), "The follow-up")

    def undo_import(self) -> None:
        if self.subject is None:
            return
        answer = QMessageBox.question(
            self,
            "Undo last import",
            "Put the imported knowledge back as it was before the last import?\n\n"
            "Your answers, flags and suspended questions stay. Transcript labels from that reply go back, unless you changed them since.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        undone = StudyStore(self.subject).undo_last_import()
        self._show_reply_text("The last import is undone." if undone else "There is nothing to undo.")
        for button in (self.fix_btn, self.follow_up_btn):
            button.hide()
        self.checked = None
        self._imported.clear()  # the same reply may be imported again
        self.undo_btn.setEnabled(StudyStore(self.subject).prev_path.exists())
        if undone:
            self.replyImported.emit()
        self.mark_changed()

    def show_import(self) -> None:
        self.scroll.ensureWidgetVisible(self.import_step)

    # --- API mode -----------------------------------------------------------

    def show_api_mode(self) -> None:
        self.api_card.setVisible(self.settings.api_enabled)
        self.api_note.setText(self._api_note())

    def _confirm_api(self, what: str) -> bool:
        answer = QMessageBox.question(self, "API mode", f"Send {what} to {api_mode.host(self.settings)}? This costs money on that account.")
        return answer == QMessageBox.StandardButton.Yes

    def _start_api(self, message: str, job) -> None:
        """``job(client)`` returns what ``_api_done`` shows."""
        try:
            client = api_mode.client_from(self.settings)
        except api_mode.ApiError as exc:
            self.api_report.setText(str(exc))
            self.api_report.show()
            return
        self._api_client = client
        self.api_report.hide()
        self._set_api_running(True)
        self._run(message, lambda: job(client), self._api_done, self._api_failed)

    def _set_api_running(self, running: bool) -> None:
        self.api_run_btn.setEnabled(not running)
        self.api_questions_btn.setEnabled(not running)
        self.api_stop_btn.setVisible(running)

    def stop_api(self) -> None:
        if self._api_client is not None:
            self._api_client.stop.set()
            self.busy.setText("Stopping after the current call…")

    def run_api(self) -> None:
        subject, plan = self.subject, self.plan
        if subject is None or plan is None:
            self.status.emit("Still reading the subject. Try again in a moment.")
            return
        if not self._confirm_api(f"the PDFs still unconverted and about {plan.tokens:,} tokens of {subject.name}"):
            return
        choices = self._choices()

        def job(client: api_mode.ApiClient):
            report = api_mode.convert_pdfs(self._load(subject), client, self.apiProgress.emit)
            view = self._load(subject)
            store = StudyStore(subject)
            export_plan = self._make_plan(view, subject, choices)
            if not export_plan.lectures and not store.knowledge().objectives:
                raise api_mode.ApiError("Nothing to send yet: this subject has no lecture content.")
            self.apiProgress.emit(f"Exporting {subject.name}…")
            result = claude_export.write_export(view, export_plan, store.knowledge(), store.events())
            return report, api_mode.exchange(client, claude_export.all_as_one_text(result), lambda: self._load(subject), store, self.apiProgress.emit)

        self._start_api("Starting the API run…", job)

    def run_api_questions(self) -> None:
        subject = self.subject
        if subject is None or not StudyStore(subject).knowledge().objectives:
            self.status.emit("Available once a reply is imported.")
            return
        if not self._confirm_api(f"your progress in {subject.name}, with the slide pages and transcript excerpts of your weak spots"):
            return

        def job(client: api_mode.ApiClient):
            store = StudyStore(subject)
            prompt = claude_export.tutor_questions_prompt(self._load(subject), store.knowledge(), store.events())
            return None, api_mode.exchange(client, prompt, lambda: self._load(subject), store, self.apiProgress.emit)

        self._start_api("Writing the tutor prompt…", job)

    def _api_failed(self, text: str) -> None:
        self._set_api_running(False)
        self.conversionsImported.emit()  # whatever was converted before the stop is saved
        self.mark_changed()
        self.api_report.setText(f"Stopped: {text}\nWhat came back before that is saved.")
        self.api_report.show()

    def _api_done(self, result) -> None:
        self._set_api_running(False)
        report, exchange = result
        parts = []
        if report is not None and report.imported_pages:
            parts.append(report.summary())
            self.conversionsImported.emit()
        for checked, summary in exchange.steps:
            parts.append(reply_report(checked, summary, set(checked.parsed.parts)))
        if exchange.problem:
            parts.append(f"A follow-up round failed: {exchange.problem}")
        self.checked = exchange.checked
        checked = self.checked
        self.api_report.setText("\n\n".join(parts))
        self.api_report.show()
        if checked is not None:
            self.fix_btn.setVisible(bool(checked.errors) or checked.parsed.truncated)
            self.follow_up_btn.setVisible(checked.parsed.finished and any(checked.uncovered.values()))
        self.status.emit("The API run is imported.")
        self.replyImported.emit()
        self.mark_changed()

    # --- step 4 -------------------------------------------------------------

    def copy_tutor(self, build, what: str) -> None:
        subject = self.subject
        if subject is None:
            return

        def job() -> str:
            store = StudyStore(subject)
            return build(self._load(subject), store.knowledge(), store.events())

        def done(prompt: str) -> None:
            self._copy(prompt, what)
            self.tutor_note.setText(f"{what} copied. Paste it into a new Claude chat, and bring what it writes back in step 3.")

        self._run("Writing the tutor prompt…", job, done)
