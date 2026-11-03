"""Tutor tab: a chat with the local AI that explains, quizzes as it goes, or starts from zero.

Answers lean on the subject's search index and cite its passages; a cited passage is a link
that jumps to the slide or audio like a search result. ``src:<turn>:<n>`` is the n-th source
of that turn.
"""

from __future__ import annotations

import html
from datetime import datetime

from PySide6.QtCore import QUrl, Signal
from PySide6.QtWidgets import QComboBox, QLineEdit, QPushButton, QTextBrowser, QVBoxLayout
from shiboken6 import isValid

from ..config import Settings
from ..services import tutor
from ..services.study_store import StudyStore
from ..services.vector_index import Hit, SubjectIndex, get_embedder
from ..storage import Lecture, Subject
from .claude_panel import ScopedPanel, button_row, same_subject, text_label
from .guide_panel import markdown_html
from .workers import Worker, run

WHOLE_COURSE = "Whole course"
UNAVAILABLE = "No local AI is running. Start LM Studio's local server (Settings → Local AI) and send again."


class TutorPanel(ScopedPanel):
    status = Signal(str)
    hitActivated = Signal(object)  # Hit

    def __init__(self, parent=None, settings: Settings | None = None):
        super().__init__(parent)
        self.settings = settings or Settings()
        self.history: list[tutor.Turn] = []
        self.chat_id = ""
        self.busy = False
        self._workers: set[Worker] = set()

        self.mode = QComboBox()
        for key, label in tutor.MODES.items():
            self.mode.addItem(label, key)
        self.topic = QComboBox(editable=True)
        self.topic.lineEdit().setPlaceholderText("Topic, or leave it at the whole course")
        self.start_btn = QPushButton("Start", objectName="primaryButton")
        self.start_btn.clicked.connect(self.start)
        self.new_btn = QPushButton("New chat")
        self.new_btn.clicked.connect(self.new_chat)
        self.podcast_btn = QPushButton("Podcast · coming soon")
        self.podcast_btn.setEnabled(False)
        self.podcast_btn.setToolTip("Coming soon: turn a topic into a short audio episode to listen to on the go.")

        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(self.open_link)
        self.input = QLineEdit(placeholderText="Ask a question or answer the tutor…")
        self.input.returnPressed.connect(self.send)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send)
        self.hint = text_label(muted=True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.mode)
        layout.addWidget(self.topic)
        layout.addWidget(button_row(self.start_btn, self.new_btn, self.podcast_btn))
        layout.addWidget(self.browser, 1)
        layout.addWidget(self.input)
        layout.addWidget(button_row(self.send_btn))
        layout.addWidget(self.hint)
        self.new_chat()

    # --- binding ------------------------------------------------------------

    def set_scope(self, subject: Subject | None, lecture: Lecture | None) -> None:
        if not same_subject(subject, self.subject):
            self.subject = subject
            self.new_chat()
        super().set_scope(subject, lecture)

    def refresh(self) -> None:
        super().refresh()
        current = self.topic.currentText()
        titles = tutor.topic_titles(StudyStore(self.subject).knowledge().topics) if self.subject else []
        self.topic.blockSignals(True)
        self.topic.clear()
        self.topic.addItems([WHOLE_COURSE, *titles])
        self.topic.setCurrentText(current or WHOLE_COURSE)
        self.topic.blockSignals(False)

    # --- conversation -------------------------------------------------------

    @property
    def mode_key(self) -> str:
        return self.mode.currentData()

    @property
    def topic_text(self) -> str:
        text = self.topic.currentText().strip()
        return "" if text == WHOLE_COURSE else text

    def new_chat(self) -> None:
        self.history = []
        self.chat_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.busy = False
        self._render()
        self._set_busy(False)

    def start(self) -> None:
        """A fresh chat that opens with the chosen way of learning."""
        self.new_chat()
        self._ask(tutor.opener(self.mode_key, self.topic_text), auto=True)

    def send(self) -> None:
        text = self.input.text().strip()
        if text and not self.busy and self.subject is not None:
            self.input.clear()
            self._ask(text)

    def _ask(self, text: str, auto: bool = False) -> None:
        if self.subject is None:
            return
        turn = tutor.Turn("user", text, auto=auto)
        self.history.append(turn)
        self._log(turn)
        self._set_busy(True)
        self._render()

        subject, chat_id, mode, topic = self.subject, self.chat_id, self.mode_key, self.topic_text
        settings = self.settings

        def search(query: str) -> list[Hit]:
            return SubjectIndex(subject, get_embedder(settings.embedding_model)).search(query, limit=tutor.SOURCES)

        worker = Worker(tutor.reply, mode, subject.name, topic, list(self.history), settings, search)
        worker.setAutoDelete(False)
        self._workers.add(worker)

        def current() -> bool:
            return isValid(self) and self.chat_id == chat_id and same_subject(self.subject, subject)

        def done(answer: tutor.Turn) -> None:
            self._workers.discard(worker)
            if current():
                self.history.append(answer)
                self._log(answer)
                self._set_busy(False)
                self._render()

        def failed(message: str) -> None:
            self._workers.discard(worker)
            if current():
                self._set_busy(False)
                self.hint.setText(UNAVAILABLE if "No local AI" in message else f"The tutor could not answer: {message}")

        worker.signals.finished.connect(done)
        worker.signals.failed.connect(failed)
        run(worker)

    def _log(self, turn: tutor.Turn) -> None:
        try:
            tutor.log_turn(self.subject, self.chat_id, self.mode_key, self.topic_text, turn)
        except OSError as exc:
            self.status.emit(f"Could not save the tutor chat: {exc}")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        for widget in (self.send_btn, self.start_btn, self.input):
            widget.setEnabled(not busy)
        self.hint.setText("The tutor is thinking…" if busy else "Local AI, grounded in your material: check key points against the sources.")

    # --- rendering ----------------------------------------------------------

    def _render(self) -> None:
        if not self.history:
            self.browser.setHtml(
                "<p><b>Learn with the tutor.</b></p>"
                "<p><i>Explain</i>: ask anything about the course.<br>"
                "<i>Quiz me as we go</i>: short explanations, each followed by a question.<br>"
                "<i>Start from zero</i>: step by step from the very basics.</p>"
                "<p>Pick a way and a topic, then press Start, or just type a question.</p>"
            )
            return
        parts = []
        for index, turn in enumerate(self.history):
            if turn.role == "user":
                parts.append(f"<p><b>You:</b> {html.escape(turn.text)}</p>")
                continue
            parts.append(f"<p><b>Tutor:</b></p>{markdown_html(turn.text)}")
            if turn.sources:
                links = " · ".join(
                    f"<a href='src:{index}:{n}'>[{n}] {html.escape(turn.sources[n - 1].ref or turn.sources[n - 1].source)}</a>" for n in turn.cited()
                )
                parts.append(f"<p><small>Sources: {links}</small></p>")
        if self.busy:
            parts.append("<p><i>…</i></p>")
        self.browser.setHtml("".join(parts))
        bar = self.browser.verticalScrollBar()
        bar.setValue(bar.maximum())

    def open_link(self, url: QUrl) -> None:
        kind, _, rest = url.toString().partition(":")
        if kind != "src":
            return
        turn, _, n = rest.partition(":")
        try:
            hit = self.history[int(turn)].sources[int(n) - 1]
        except (ValueError, IndexError):
            return
        self.hitActivated.emit(hit)
