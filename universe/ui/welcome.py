"""Centre pages: the welcome / subject overview, and the open-lecture view."""

from __future__ import annotations

import html
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QBoxLayout,
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
from ..services import moodle_ical, practice
from ..services.study_store import StudyStore
from ..storage import Lecture, Subject, Workspace
from . import icons
from .audio import PlayerBar
from .claude_panel import exam_date
from .note_editor import NoteEditor
from .platform import floating

ROLE = Qt.ItemDataRole.UserRole
DEADLINES_SHOWN = 8
GLASS_ROOM = 56  # below the last line of notes, for the floating player


STACK_BUTTONS_BELOW = 380  # page width at which the two buttons go one under the other
FIRST_STEPS = (
    "<ol style='margin-left: -24px'>"
    "<li><b>Create a subject</b>, one per course, e.g. Corporate Finance.</li>"
    "<li><b>Record the lecture.</b> Notes taken with ⌘T get timestamps that replay the audio.</li>"
    "<li><b>Study with Claude:</b> export, paste the reply back, and practise with quizzes "
    "tailored to your professor.</li>"
    "</ol>"
)


class WelcomePage(QWidget):
    """Shown when no lecture is open: onboarding, or the selected subject's lectures."""

    createSubject = Signal()
    createLecture = Signal()
    quickRecord = Signal()
    openLecture = Signal(object)  # Lecture

    def __init__(self, workspace: Workspace, parent=None):
        super().__init__(parent)
        self.workspace = workspace

        self.title = QLabel(wordWrap=True)
        self.title.setObjectName("welcomeTitle")
        self.subtitle = QLabel(wordWrap=True)
        self.subtitle.setObjectName("welcomeSubtitle")
        self.readiness = QLabel(wordWrap=True)
        self.readiness.hide()

        # First run: three steps as a short list, not three boxes.
        self.steps = QLabel(FIRST_STEPS, textFormat=Qt.TextFormat.RichText, wordWrap=True)

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

        self.primary = QPushButton(icon=icons.icon("plus", "white"))
        self.primary.setObjectName("primaryButton")
        self.primary.clicked.connect(self._primary_clicked)
        self.record = QPushButton("Start recording now")
        self.record.setIcon(icons.dot("white"))
        self.record.setObjectName("recordButton")
        self.record.clicked.connect(self.quickRecord)

        buttons = self._buttons = QBoxLayout(QBoxLayout.Direction.LeftToRight)  # stacked when very narrow
        buttons.addWidget(self.primary, 0, Qt.AlignmentFlag.AlignLeft)  # natural width, never a full-width bar
        buttons.addWidget(self.record, 0, Qt.AlignmentFlag.AlignLeft)
        buttons.addStretch()

        column = QVBoxLayout()
        column.setSpacing(16)
        column.addWidget(self.title)
        column.addWidget(self.subtitle)
        column.addWidget(self.readiness)
        column.addLayout(buttons)
        column.addWidget(self.steps)
        column.addWidget(self.deadlines)
        column.addWidget(self.lectures, 1)
        column.addStretch()

        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 24, 16, 24)
        holder = QWidget()
        holder.setLayout(column)
        holder.setMaximumWidth(760)
        holder.setMinimumWidth(160)  # rows side by side must not stop the window narrowing; resizeEvent stacks them
        outer.addWidget(holder, 1)
        outer.addStretch(0)

        self.subject: Subject | None = None
        self.show_subject(None)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        narrow = event.size().width() < STACK_BUTTONS_BELOW
        self._buttons.setDirection(QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight)

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
        self._show_readiness()
        has_subjects = bool(self.workspace.subjects())
        self.lectures.clear()
        if subject is None:
            if has_subjects:
                self.title.setText("UniVerse")
                self.subtitle.setText("Pick a lecture in the sidebar — or just start recording; UniVerse files it for you.")
                self.primary.setText("New subject")
            else:
                self.title.setText("Start with a subject")
                self.subtitle.setText("Recordings, notes and slides stay on this Mac, sorted by course and lecture.")
                self.primary.setText("Create a subject")
            self.steps.setVisible(not has_subjects)
            self.lectures.hide()
            return

        lectures = subject.lectures()
        self.title.setText(subject.name)
        self.subtitle.setText(
            f"{len(lectures)} lecture{'s' if len(lectures) != 1 else ''}. Open one below, or record a new lecture."
            if lectures
            else "No lectures yet. Press Start recording now when class begins — or create an empty lecture first."
        )
        self.primary.setText("New lecture")
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

    def _show_readiness(self) -> None:
        """One line, only once Claude's reply is in: "Exam in 34 days · 62% practised · 12 weak · 42 new"."""
        line = ""
        if self.subject is not None:
            store = StudyStore(self.subject)
            knowledge = store.knowledge()
            if knowledge.objectives:
                when = exam_date(self.subject)
                events = store.events()
                ready = practice.readiness(knowledge, events, practice.replay(knowledge, events, when), datetime.now(), when)
                days = [f"Exam in {ready.days_left} days"] if ready.days_left is not None else []
                line = " · ".join([*days, f"{ready.coverage:.0%} practised", f"{ready.weak} weak", f"{ready.new} new"])
        self.readiness.setText(line)
        self.readiness.setVisible(bool(line))

    def _primary_clicked(self) -> None:
        (self.createLecture if self.subject else self.createSubject).emit()


class LectureView(QWidget):
    """Lecture header, the note editor and the player bar."""

    assignProfessor = Signal()
    closeRequested = Signal()

    def __init__(self, notes: NoteEditor, player: PlayerBar, professors: ProfessorStore, parent=None):
        super().__init__(parent)
        self.professors = professors
        self.title = QLabel(wordWrap=True)
        self.title.setObjectName("lectureTitle")
        self.meta = QLabel(wordWrap=True)
        self.meta.setObjectName("muted")
        self.meta.linkActivated.connect(lambda _href: self.assignProfessor.emit())

        header = QVBoxLayout()
        header.setContentsMargins(16, 12, 16, 4)
        header.setSpacing(2)
        header.addWidget(self.title)
        header.addWidget(self.meta)

        close = QPushButton()
        close.setIcon(icons.icon("x"))
        close.setFlat(True)
        close.setFixedWidth(28)
        close.setToolTip("Hide notes — bring them back from View → Notes (⌥⌘N)")
        close.clicked.connect(self.closeRequested)
        top = QHBoxLayout()
        top.addLayout(header, 1)
        top.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        top.setContentsMargins(0, 8, 8, 0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(top)
        layout.addWidget(notes, 1)
        layout.addWidget(player)
        self._glass = None
        if floating.available():
            # macOS 26: playback floats over the bottom of the notes in a Liquid Glass capsule.
            F = floating
            audio = player.audio
            player.floats = True
            player._refresh()
            notes.set_bottom_room(GLASS_ROOM)
            self._glass = F.attach(
                notes.edit,
                [
                    F.Button(lambda: "pause.fill" if audio.player.playing else "play.fill", player.play_btn, keep=F.MUST),
                    F.Button("gobackward.10", player.back_btn),
                    F.Button("goforward.10", player.fwd_btn),
                    F.Slider(player.slider, player.seek_to),
                    F.Text(player.time.text, changed=[audio.tick]),  # the slider gives way before the time
                    F.Menu("waveform", "Recording to play", player.files_menu),
                ],
                visible=player.has_files,
            )
            for signal in (audio.filesChanged, audio.currentChanged):  # the tick already refreshes (time)
                signal.connect(self._glass.refresh)

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
