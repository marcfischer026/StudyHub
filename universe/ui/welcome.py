"""Centre pages: the welcome / subject overview, and the open-lecture view."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..professors import ProfessorStore
from ..services import moodle_ical
from ..storage import Lecture, Subject, Workspace
from .audio import PlayerBar
from .note_editor import NoteEditor

ROLE = Qt.ItemDataRole.UserRole
DEADLINES_SHOWN = 8


def _card(title: str, body: str) -> QFrame:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    heading = QLabel(title)
    heading.setObjectName("cardTitle")
    text = QLabel(body, wordWrap=True)
    text.setObjectName("muted")
    layout.addWidget(heading)
    layout.addWidget(text)
    layout.addStretch()
    return frame


class WelcomePage(QWidget):
    """Shown when no lecture is open: onboarding, or the selected subject's lectures."""

    createSubject = Signal()
    createLecture = Signal()
    quickRecord = Signal()
    openLecture = Signal(object)  # Lecture

    def __init__(self, workspace: Workspace, parent=None):
        super().__init__(parent)
        self.workspace = workspace

        self.title = QLabel()
        self.title.setObjectName("welcomeTitle")
        self.subtitle = QLabel(wordWrap=True)
        self.subtitle.setObjectName("welcomeSubtitle")

        self.steps = QWidget()
        self.steps.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        steps = QHBoxLayout(self.steps)
        steps.setContentsMargins(0, 0, 0, 0)
        steps.setSpacing(12)
        steps.addWidget(_card("1 · Create a subject", "One per course — Corporate Finance, Physics, anything."))
        steps.addWidget(_card("2 · Record the lecture", "Hit ● Record. Notes with ⌘T get timestamps that replay the audio."))
        steps.addWidget(_card("3 · Study with Claude", "Export for Claude, paste the reply back, get quizzes tailored to your professor."))

        self.events: list[moodle_ical.Event] = []
        self.deadlines = QFrame()
        self.deadlines.setObjectName("card")
        self.deadlines.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        deadlines = QVBoxLayout(self.deadlines)
        self.deadlines_title = QLabel()
        self.deadlines_title.setObjectName("cardTitle")
        self.deadlines_list = QLabel(textFormat=Qt.TextFormat.RichText, wordWrap=True)
        deadlines.addWidget(self.deadlines_title)
        deadlines.addWidget(self.deadlines_list)

        self.lectures = QListWidget()
        self.lectures.setObjectName("lectureList")
        self.lectures.itemClicked.connect(lambda item: self.openLecture.emit(item.data(ROLE)))

        self.primary = QPushButton()
        self.primary.setObjectName("primaryButton")
        self.primary.clicked.connect(self._primary_clicked)
        self.record = QPushButton("●  Start recording now")
        self.record.setObjectName("recordButton")
        self.record.clicked.connect(self.quickRecord)

        buttons = QHBoxLayout()
        buttons.addWidget(self.primary)
        buttons.addWidget(self.record)
        buttons.addStretch()

        column = QVBoxLayout()
        column.setSpacing(14)
        column.addWidget(self.title)
        column.addWidget(self.subtitle)
        column.addSpacing(6)
        column.addLayout(buttons)
        column.addSpacing(10)
        column.addWidget(self.steps)
        column.addWidget(self.deadlines)
        column.addWidget(self.lectures, 1)
        column.addStretch()

        outer = QHBoxLayout(self)
        outer.setContentsMargins(40, 36, 40, 24)
        holder = QWidget()
        holder.setLayout(column)
        holder.setMaximumWidth(760)
        outer.addWidget(holder, 1)
        outer.addStretch(0)

        self.subject: Subject | None = None
        self.show_subject(None)

    def set_events(self, events: list[moodle_ical.Event]) -> None:
        """Moodle calendar events; the page shows the upcoming ones that fit the current view."""
        self.events = events
        self._show_deadlines()

    def _show_deadlines(self) -> None:
        events = moodle_ical.upcoming(self.events)
        if self.subject is not None:
            events = [e for e in events if moodle_ical.matches(e, self.subject.name, self.subject.short_name)]
        self.deadlines.setVisible(bool(events))
        if not events:
            return
        self.deadlines_title.setText("Upcoming in Moodle")
        rows = []
        for event in events[:DEADLINES_SHOWN]:
            course = ""
            if event.course and self.subject is None:  # on a subject page the course is obvious
                course = f" <span style='opacity:0.55'>· {html.escape(event.course)}</span>"
            rows.append(f"<b>{html.escape(moodle_ical.when(event))}</b> &nbsp;{html.escape(event.title)}{course}")
        if len(events) > DEADLINES_SHOWN:
            rows.append(f"<span style='opacity:0.55'>+ {len(events) - DEADLINES_SHOWN} more</span>")
        self.deadlines_list.setText("<br>".join(rows))

    def show_subject(self, subject: Subject | None) -> None:
        self.subject = subject
        self._show_deadlines()
        has_subjects = bool(self.workspace.subjects())
        self.lectures.clear()
        if subject is None:
            self.title.setText("Welcome to UniVerse")
            if has_subjects:
                self.subtitle.setText("Pick a lecture in the sidebar — or just start recording; UniVerse files it for you.")
                self.primary.setText("+  New subject")
            else:
                self.subtitle.setText(
                    "Your offline study space: record lectures, take timestamped notes, and turn it all into exam practice."
                )
                self.primary.setText("+  Create your first subject")
            self.steps.setVisible(not has_subjects)
            self.lectures.hide()
            return

        lectures = subject.lectures()
        self.title.setText(subject.name)
        self.subtitle.setText(
            f"{len(lectures)} lecture{'s' if len(lectures) != 1 else ''}. Open one below, or record a new lecture."
            if lectures
            else "No lectures yet. Press ● Start recording now when class begins — or create an empty lecture first."
        )
        self.primary.setText("+  New lecture")
        self.steps.hide()
        for lecture in reversed(lectures):
            n_audio = len(lecture.audio_files())
            n_pdf = len(lecture.pdfs())
            details = " · ".join(
                part
                for part in (
                    f"{n_audio} recording{'s' if n_audio != 1 else ''}" if n_audio else "",
                    f"{n_pdf} PDF{'s' if n_pdf != 1 else ''}" if n_pdf else "",
                    "transcribed" if lecture.transcripts() else "",
                )
                if part
            )
            item = QListWidgetItem(f"{lecture.name}\n{details or 'empty'}")
            item.setData(ROLE, lecture)
            self.lectures.addItem(item)
        self.lectures.setVisible(bool(lectures))

    def _primary_clicked(self) -> None:
        (self.createLecture if self.subject else self.createSubject).emit()


class LectureView(QWidget):
    """Lecture header, the note editor and the player bar."""

    assignProfessor = Signal()

    def __init__(self, notes: NoteEditor, player: PlayerBar, professors: ProfessorStore, parent=None):
        super().__init__(parent)
        self.professors = professors
        self.title = QLabel()
        self.title.setObjectName("lectureTitle")
        self.meta = QLabel()
        self.meta.setObjectName("muted")
        self.meta.linkActivated.connect(lambda _href: self.assignProfessor.emit())

        header = QVBoxLayout()
        header.setContentsMargins(16, 12, 16, 4)
        header.setSpacing(2)
        header.addWidget(self.title)
        header.addWidget(self.meta)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addWidget(notes, 1)
        layout.addWidget(player)

    def show_lecture(self, lecture: Lecture) -> None:
        self.title.setText(lecture.name)
        prof = self.professors.get(lecture.professor_id)
        prof_html = (
            f"Professor: <a href='#'>{html.escape(prof.name)}</a>"
            if prof
            else "<a href='#'>Assign a professor</a> to tailor quizzes"
        )
        subject = lecture.subject
        self.meta.setText(f"{html.escape(subject.name)} ({html.escape(subject.short_name)})  ·  {prof_html}")
