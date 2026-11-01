"""Progress tab: the exam's facts, three honest numbers, and how each topic stands.

Never a grade. A number the data cannot carry yet says so instead of guessing.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..professors import ProfessorStore
from ..services import moodle_ical, practice
from ..services.subject_model import load_subject
from ..services.study_store import StudyStore
from ..storage import Lecture, Subject
from .claude_panel import ScopedPanel, card, exam_date, same_subject, scrolling, text_label
from .style import ACCENT

EXAM_WORDS = re.compile(r"klausur|prüfung|pruefung|exam", re.IGNORECASE)
ROLE = Qt.ItemDataRole.UserRole


def exam_event(events: list[moodle_ical.Event], subject: Subject) -> moodle_ical.Event | None:
    """The next Moodle event of this course whose title sounds like its exam. The student confirms it."""
    found = [e for e in moodle_ical.upcoming(events) if EXAM_WORDS.search(e.title) and moodle_ical.matches(e, subject.name, subject.short_name)]
    return found[0] if found else None


class ExamForm(QWidget):
    """Date, duration, aids; each change is saved at once. Also the sidebar's Exam date… dialog."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.subject: Subject | None = None
        self._suggested: date | None = None
        self._loading = False
        self.known = QCheckBox("Date is known")
        self.date = QDateEdit(calendarPopup=True, displayFormat="dd.MM.yyyy")
        self.duration = QSpinBox(minimum=0, maximum=600, singleStep=15, suffix=" min", specialValueText="Not known")
        self.duration.setMinimumWidth(self.duration.fontMetrics().horizontalAdvance("Not known") + 48)
        self.aids = QLineEdit(placeholderText="Calculator, one A4 page, …")
        self.suggestion = QPushButton()
        self.suggestion.clicked.connect(self._use_suggestion)
        self.suggestion.hide()
        self.known.toggled.connect(self.date.setEnabled)
        self.known.toggled.connect(lambda _on: self.save())
        self.date.dateChanged.connect(lambda _d: self.save())
        self.duration.valueChanged.connect(lambda _v: self.save())
        self.aids.editingFinished.connect(self.save)
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow(self.known, self.date)
        form.addRow("Duration", self.duration)
        form.addRow("Aids", self.aids)
        form.addRow(self.suggestion)

    def load(self, subject: Subject | None, events: list[moodle_ical.Event] | None = None) -> None:
        self.subject = subject
        self._loading = True
        exam = subject.exam if subject else {}
        when = exam_date(subject)
        self.known.setChecked(when is not None)
        self.date.setEnabled(when is not None)
        self.date.setDate(QDate(when.year, when.month, when.day) if when else QDate.currentDate().addDays(30))
        self.duration.setValue(int(exam.get("duration_min") or 0))
        self.aids.setText(str(exam.get("aids") or ""))
        self._loading = False
        event = exam_event(events or [], subject) if subject else None
        self._suggested = event.start.date() if event else None
        if event and self._suggested != when:
            self.suggestion.setText(f"Moodle: “{event.title[:40]}” on {event.start:%d.%m.%Y}. Use this date")
        self.suggestion.setVisible(bool(event) and self._suggested != when)

    def _use_suggestion(self) -> None:
        if self._suggested is not None:
            self._loading = True
            self.known.setChecked(True)
            self.date.setDate(QDate(self._suggested.year, self._suggested.month, self._suggested.day))
            self._loading = False
            self.save()
            self.suggestion.hide()

    def save(self) -> None:
        if self._loading or self.subject is None:
            return
        chosen = self.date.date().toPython().isoformat() if self.known.isChecked() else ""
        exam = self.subject.exam
        new = {"date": chosen, "duration_min": self.duration.value(), "aids": self.aids.text().strip()}
        if any((exam.get(key) or "") != (value or "") for key, value in new.items()):
            self.subject.set_exam(**new)
            self.changed.emit()


class ExamDialog(QDialog):
    def __init__(self, subject: Subject, events: list[moodle_ical.Event], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Exam of {subject.name}")
        self.form = ExamForm()
        self.form.load(subject, events)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addWidget(self.form)
        layout.addWidget(buttons)


class HistoryChart(QWidget):
    """One bar per day: its height is answers, the accent part is what was right."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.days: list[practice.DayStats] = []
        self.setFixedHeight(56)

    def set_days(self, days: list[practice.DayStats]) -> None:
        self.days = days
        self.update()

    def paintEvent(self, _event) -> None:
        if not self.days:
            return
        painter = QPainter(self)
        most = max(max(day.answers for day in self.days), 1)
        width = self.width() / len(self.days)
        missed = self.palette().color(QPalette.ColorRole.Mid)
        for index, day in enumerate(self.days):
            x, bar = int(index * width), max(1, int(width) - 2)
            height = round(day.answers / most * (self.height() - 2))
            right = round(day.correct / most * (self.height() - 2))
            if not day.answers:
                painter.fillRect(x, self.height() - 1, bar, 1, missed)
                continue
            painter.fillRect(x, self.height() - height, bar, height - right, missed)
            painter.fillRect(x, self.height() - right, bar, right, QColor(ACCENT))


class ProgressPanel(ScopedPanel):
    status = Signal(str)
    practiseTopic = Signal(str)  # topic id
    examChanged = Signal()

    def __init__(self, professors: ProfessorStore, parent=None):
        super().__init__(parent)
        self.professors = professors
        self.store: StudyStore | None = None
        self.events: list[moodle_ical.Event] = []
        self.readiness: practice.Readiness | None = None

        self.exam = ExamForm()
        self.exam.changed.connect(self._exam_saved)

        self.coverage = text_label()
        self.recall = text_label()
        self.mock = text_label()
        self.calibration = text_label(muted=True)
        self.unprocessed = text_label(muted=True)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Topic", "Recall", "Weak", "Answers"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().hide()
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 90)
        for column in (2, 3):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._topic_clicked)
        self.table_hint = text_label("Double-click a topic to practise it.", muted=True)

        self.chart = HistoryChart()
        self.chart_caption = text_label(muted=True)

        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(6, 6, 6, 6)
        column.setSpacing(10)
        column.addWidget(card("Exam", self.exam))
        column.addWidget(card("Where you stand", self.coverage, self.recall, self.mock, self.calibration, self.unprocessed))
        column.addWidget(card("Topics", self.table, self.table_hint))
        column.addWidget(card("Last 30 days", self.chart, self.chart_caption))
        column.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scrolling(body))

    def set_scope(self, subject: Subject | None, lecture: Lecture | None) -> None:
        if not same_subject(subject, self.subject):
            self.store = StudyStore(subject) if subject else None
        super().set_scope(subject, lecture)

    def set_events(self, events: list[moodle_ical.Event]) -> None:
        self.events = events
        self.mark_changed()

    def _exam_saved(self) -> None:
        self.examChanged.emit()
        if self.store is not None:
            self._show_study()

    def refresh(self) -> None:
        super().refresh()
        self.exam.load(self.subject, self.events)
        if self.store is None:
            for label in (self.coverage, self.recall, self.mock, self.calibration, self.unprocessed, self.chart_caption):
                label.clear()
            self.table.setRowCount(0)
            self.chart.set_days([])
            return
        self._show_study()
        knowledge = self.store.knowledge()
        view = load_subject(self.subject, self.professors, pages=False)
        waiting = [v.ref for v in view.lectures if not self._is_processed(knowledge, v)]
        self.unprocessed.setText(f"Lectures not processed yet: {', '.join(waiting)}" if waiting else "Every lecture has been processed by Claude." if view.lectures else "")

    def _show_study(self) -> None:
        knowledge = self.store.knowledge()
        events = self.store.events()
        now = datetime.now()
        when = exam_date(self.subject)
        cards = practice.replay(knowledge, events, when)
        ready = self.readiness = practice.readiness(knowledge, events, cards, now, when)
        self._show_numbers(ready, when)
        self._show_topics(practice.topic_progress(knowledge, cards, events, now, when))
        days = practice.history(events, now)
        self.chart.set_days(days)
        answers = sum(day.answers for day in days)
        right = sum(day.correct for day in days)
        active = sum(day.answers > 0 for day in days)
        self.chart_caption.setText(f"{answers} answers on {active} day{'s' * (active != 1)}, {right} right." if answers else "No answers yet.")

    @staticmethod
    def _is_processed(knowledge, lecture) -> bool:
        record = knowledge.lectures.get(lecture.uid)
        return bool(record and record.get("processed") and record.get("hash") == lecture.content_hash)

    def _show_numbers(self, ready: practice.Readiness, when=None) -> None:
        if ready.objectives:
            self.coverage.setText(f"Coverage: {ready.coverage:.0%}. Share of what matters you have practised ({ready.practised} of {ready.objectives} objectives).")
        else:
            self.coverage.setText("Coverage: nothing imported from Claude yet.")
        if when is not None and when < date.today():
            self.recall.setText("Exam-day recall: the exam date has passed.")
        elif ready.recall_at_exam is not None:
            self.recall.setText(f"Exam-day recall: {ready.recall_at_exam:.0%}. A model's estimate of what you would still know on the day.")
        elif ready.answers >= practice.RECALL_MIN_ANSWERS and ready.days_active >= practice.RECALL_MIN_DAYS and ready.days_left is None:
            self.recall.setText("Exam-day recall: set an exam date to see it.")
        else:
            self.recall.setText(
                f"Exam-day recall: not enough data yet ({ready.answers} of {practice.RECALL_MIN_ANSWERS} answers, "
                f"{ready.days_active} of {practice.RECALL_MIN_DAYS} days)."
            )
        mock = ready.last_mock
        if mock is None:
            self.mock.setText("Last unseen mock: not enough data yet. Take a mock exam on the Practice tab.")
        else:
            self.mock.setText(f"Last unseen mock: {mock.score:.0%} on {mock.n} question{'s' * (mock.n != 1)}; the true level lies between {mock.low:.0%} and {mock.high:.0%}.")
        self.calibration.setText(
            "Of the answers you marked Sure, no data yet." if ready.sure_right is None else f"Of the answers you marked Sure, {ready.sure_right:.0%} were right."
        )

    def _show_topics(self, topics: list[practice.TopicProgress]) -> None:
        self.table.setRowCount(len(topics))
        for row, topic in enumerate(topics):
            name = QTableWidgetItem(f"{'★' * topic.importance} {topic.title}")
            name.setData(ROLE, topic.id)
            name.setToolTip(topic.title)
            self.table.setItem(row, 0, name)
            recall = topic.recall_at_exam if topic.recall_at_exam is not None else topic.recall_now
            if recall is None:
                self.table.removeCellWidget(row, 1)
                self.table.setItem(row, 1, QTableWidgetItem("not yet"))
            else:
                bar = QProgressBar(minimum=0, maximum=100, value=round(recall * 100))
                bar.setTextVisible(True)
                bar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)  # the double click belongs to the row
                self.table.setItem(row, 1, QTableWidgetItem(""))
                self.table.setCellWidget(row, 1, bar)
            self.table.setItem(row, 2, QTableWidgetItem(str(topic.weak)))
            self.table.setItem(row, 3, QTableWidgetItem(str(topic.answers)))
        height = self.table.horizontalHeader().height() + sum(self.table.rowHeight(r) for r in range(len(topics))) + 4
        self.table.setFixedHeight(height)
        self.table_hint.setVisible(bool(topics))

    def _topic_clicked(self, row: int, _column: int = 0) -> None:
        item = self.table.item(row, 0)
        if item is not None:
            self.practiseTopic.emit(item.data(ROLE))
