"""Practice tab: one question at a time, each answer recorded the moment it is given.

Confidence is asked before the reveal, because a lucky guess does not count as knowing. A mock
exam gives no feedback until the end; free answers wait for the student's own grade until then,
stored first as 0 and corrected by a grade event, so walking away from a mock loses nothing.
"""

from __future__ import annotations

import html
import time
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..services import practice
from ..services.study_store import StudyStore
from ..storage import Lecture, Subject
from .claude_panel import ScopedPanel, WrapButton, button_row, exam_date, same_subject, scrolling, text_label
from .style import DANGER, SUCCESS

RIGHT = SUCCESS
WRONG = DANGER
MODES = (("Today", "today"), ("Weak topics", "weak"), ("Topic…", "topic"), ("Lecture…", "lecture"))
FREE_TYPES = ("short", "case", "card")
PARTLY = 0.6  # a pass, rated Hard


@dataclass
class Result:
    question: dict
    answer: str
    score: float | None
    attempt: str  # event id
    ungraded: bool = False  # a free answer in a mock, waiting for the student's own grade


def right_answer(question: dict) -> str:
    if question.get("type") == "mc":
        return next((str(o.get("text", "")) for o in question.get("options") or [] if o.get("correct")), "")
    numeric = question.get("numeric") if isinstance(question.get("numeric"), dict) else None
    if question.get("answer"):
        return str(question["answer"])
    return f"{numeric.get('value')} {numeric.get('unit') or ''}".strip() if numeric else ""


class PracticePanel(ScopedPanel):
    status = Signal(str)
    sourceActivated = Signal(dict)  # a durable source
    answered = Signal()  # the history grew

    def __init__(self, parent=None):
        super().__init__(parent)
        self.store: StudyStore | None = None
        self.session: list[dict] = []
        self.position = 0
        self.mode = ""
        self.results: list[Result] = []
        self.cards: dict = {}
        self.hints = 0
        self.answered_current = False
        self.choices: list[WrapButton] = []
        self.rubric: list[tuple[WrapButton, float]] = []
        self.links: list[dict] = []
        self.deadline = 0.0
        self._shown_at = 0.0
        self._confidence = ""
        self._reviewing: list[Result] = []  # mock answers the student still has to grade

        self.header = text_label()
        self.header.setObjectName("practiceHeader")
        self.mode_choice = QComboBox()
        for text, value in MODES:
            self.mode_choice.addItem(text, value)
        self.mode_choice.currentIndexChanged.connect(lambda _i: self._fill_targets())
        self.target = QComboBox()
        self.target.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.target.setMinimumContentsLength(10)
        self.target.hide()
        self.start_btn = QPushButton("Start", objectName="primaryButton")
        self.start_btn.clicked.connect(self.start_chosen)
        self.mock_btn = QPushButton("Mock exam")
        self.mock_btn.clicked.connect(self.start_mock)
        self.controls = QWidget()
        controls = QVBoxLayout(self.controls)
        controls.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self.mode_choice)
        row.addWidget(self.target, 1)
        controls.addLayout(row)
        controls.addWidget(button_row(self.start_btn, self.mock_btn))

        self.idle = text_label(rich=True, selectable=True)
        self.idle.linkActivated.connect(self._open_link)

        self.meta = text_label(muted=True)
        self.stem = text_label(selectable=True)
        self.stem.setObjectName("practiceStem")
        self.options = QVBoxLayout()
        self.option_group = QButtonGroup(self)
        self.answer_line = QLineEdit(placeholderText="Your result")
        self.answer_text = QPlainTextEdit(placeholderText="Write your answer first.")
        self.answer_text.setFixedHeight(100)
        self.hint_label = text_label(muted=True, selectable=True)
        self.hint_btn = QPushButton("Hint (caps how well this counts)")
        self.hint_btn.clicked.connect(self.show_hint)
        self.confidence_title = text_label("How sure are you? Your answer is checked when you press one.", muted=True)
        self.sure = QPushButton("Sure", objectName="primaryButton")
        self.unsure = QPushButton("Not sure")
        self.guess = QPushButton("Guessing")
        for button, confidence in ((self.sure, "sure"), (self.unsure, "unsure"), (self.guess, "guess")):
            button.clicked.connect(lambda _c=False, value=confidence: self.check(value))
        self.confidence_row = button_row(self.sure, self.unsure, self.guess)
        self.save_free_btn = QPushButton("Save and continue", objectName="primaryButton")
        self.save_free_btn.clicked.connect(self.save_free_for_later)
        self.model_answer = text_label(rich=True, selectable=True)
        self.rubric_box = QVBoxLayout()
        self.rubric_save = QPushButton("Save score", objectName="primaryButton")
        self.rubric_save.clicked.connect(self.save_rubric)
        self.got_it = QPushButton("Got it")
        self.got_it.clicked.connect(lambda: self.self_grade(1.0))
        self.partly = QPushButton("Partly")
        self.partly.clicked.connect(lambda: self.self_grade(PARTLY))
        self.missed = QPushButton("Missed")
        self.missed.clicked.connect(lambda: self.self_grade(0.0))
        self.grade_row = button_row(self.got_it, self.partly, self.missed)
        self.feedback = text_label(rich=True, selectable=True)
        self.feedback.setOpenExternalLinks(False)
        self.feedback.linkActivated.connect(self._open_link)
        self.next_btn = QPushButton("Next", objectName="primaryButton")
        self.next_btn.clicked.connect(self.next_question)
        self.flag_btn = QPushButton("Question is wrong…")
        self.flag_btn.clicked.connect(self.flag)
        self.end_btn = QPushButton("End session")
        self.end_btn.clicked.connect(self.finish)

        self.question_view = QWidget()
        column = QVBoxLayout(self.question_view)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        left = Qt.AlignmentFlag.AlignLeft
        for item in (self.meta, self.stem):
            column.addWidget(item)
        column.addLayout(self.options)
        for item in (self.answer_line, self.answer_text, self.hint_label):
            column.addWidget(item)
        column.addWidget(self.hint_btn, 0, left)
        for item in (self.confidence_title, self.confidence_row):
            column.addWidget(item)
        column.addWidget(self.save_free_btn, 0, left)
        column.addWidget(self.model_answer)
        column.addLayout(self.rubric_box)
        column.addWidget(self.rubric_save, 0, left)
        for item in (self.grade_row, self.feedback, button_row(self.next_btn, self.flag_btn, self.end_btn)):
            column.addWidget(item)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(6, 6, 6, 6)
        body_layout.addWidget(self.idle)
        body_layout.addWidget(self.question_view)
        body_layout.addStretch(1)
        self.scroll = scrolling(body)

        self.timer = QTimer(self, interval=1000)
        self.timer.timeout.connect(self._tick)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 0)
        layout.addWidget(self.header)
        layout.addWidget(self.controls)
        layout.addWidget(self.scroll, 1)
        self.question_view.hide()

    # --- binding ------------------------------------------------------------

    def set_scope(self, subject: Subject | None, lecture: Lecture | None) -> None:
        if not same_subject(subject, self.subject):
            self._stop_session()
            self.idle.clear()
        self.store = StudyStore(subject) if subject else None
        super().set_scope(subject, lecture)

    @property
    def in_session(self) -> bool:
        return self.question_view.isVisibleTo(self)

    def refresh(self) -> None:
        super().refresh()
        if self.store is None:
            self.header.setText("Pick a subject first.")
            return
        self.controls.setVisible(not self.in_session)
        knowledge = self.store.knowledge()
        events = self.store.events()
        now = datetime.now()
        when = exam_date(self.subject)
        self.cards = practice.replay(knowledge, events, when)
        ready = practice.readiness(knowledge, events, self.cards, now, when)
        if ready.days_left is None:
            days = "No exam date yet"
        elif when is not None and when < now.date():
            days = "Exam date has passed"
        else:
            days = f"Exam in {ready.days_left} days"
        self.header.setText(f"{days} · {ready.weak} weak · {ready.new} new")
        self._fill_targets()
        if not self.in_session and not self.idle.text():
            self.idle.setText(
                "Today picks what helps most for exam day. Your answers are saved as you go, so you can stop at any time."
                if knowledge.questions
                else "Nothing to practise yet. Export for Claude and import its reply on the Claude tab."
            )

    def _fill_targets(self) -> None:
        mode = self.mode_choice.currentData()
        self.target.setVisible(mode in ("topic", "lecture"))
        kept = self.target.currentData()
        self.target.clear()
        if self.store is None:
            return
        if mode == "topic":
            topics = self.store.knowledge().topics
            for key, topic in sorted(topics.items(), key=lambda item: item[1].get("title", "")):
                if not topic.get("retired"):
                    self.target.addItem(topic.get("title", key), key)
        elif mode == "lecture":
            for lecture in self.subject.lectures():
                self.target.addItem(lecture.name, lecture.uid)
            kept = kept or (self.lecture.uid if self.lecture else None)
        if kept is not None and self.target.findData(kept) >= 0:
            self.target.setCurrentIndex(self.target.findData(kept))

    # --- starting -----------------------------------------------------------

    def start_chosen(self) -> None:
        mode = self.mode_choice.currentData()
        target = self.target.currentData()
        if mode in ("topic", "lecture") and not target:
            self.idle.setText("Pick what to practise first.")
            return
        self.start(mode, topic=target if mode == "topic" else None, lecture=target if mode == "lecture" else None)

    def start(self, mode: str = "today", topic: str | None = None, lecture: str | None = None) -> None:
        if self.store is None:
            return
        knowledge = self.store.knowledge()
        self.cards = practice.replay(knowledge, self.store.events(), exam_date(self.subject))
        session = practice.plan_session(knowledge, self.cards, datetime.now(), exam_date(self.subject), mode=mode, topic=topic, lecture=lecture)
        if not session:
            self.idle.setText(
                "Nothing to practise here right now. Everything has been answered today, or there are no questions yet: "
                "the Claude tab can ask for new ones."
            )
            return
        self._begin(session, mode)

    def start_mock(self) -> None:
        if self.store is None:
            return
        knowledge = self.store.knowledge()
        self.cards = practice.replay(knowledge, self.store.events(), exam_date(self.subject))
        mock = practice.plan_mock(knowledge, self.cards, datetime.now(), self.subject.exam)
        if not mock.questions:
            self.idle.setText(
                "No mock possible: a mock only uses verified questions you have never seen. Ask Claude for new ones on the Claude tab."
            )
            return
        self.deadline = time.monotonic() + mock.minutes * 60
        self.timer.start()
        self._begin(mock.questions, "mock")

    def _begin(self, session: list[dict], mode: str) -> None:
        self.session, self.mode, self.position, self.results = session, mode, 0, []
        self._reviewing = []
        self.idle.clear()
        self.idle.hide()
        self.question_view.show()
        self.controls.hide()
        self.end_btn.setText("Finish mock" if mode == "mock" else "End session")
        self._show_question()

    # --- one question -------------------------------------------------------

    @property
    def question(self) -> dict:
        return self.session[self.position]

    @property
    def is_mock(self) -> bool:
        return self.mode == "mock"

    def _meta_text(self) -> str:
        question = self.question
        topics = self.store.knowledge().topics if self.store else {}
        title = topics.get(question.get("topic"), {}).get("title", "")
        parts = [f"Question {self.position + 1} of {len(self.session)}", *([title] if title else [])]
        if question.get("difficulty"):
            parts.append(question["difficulty"])
        if question.get("again"):
            parts.append("second chance")
        if question.get("verified") is False:
            parts.append("source not verified")
        if self.is_mock:
            parts.insert(0, f"Mock · {self._time_left()} left")
        return " · ".join(parts)

    def _clear_inputs(self) -> None:
        for choice in self.choices + [box for box, _marks in self.rubric]:
            choice.setParent(None)
            choice.deleteLater()
        for button in self.option_group.buttons():
            self.option_group.removeButton(button)
        self.choices, self.rubric, self.links = [], [], []

    def _show_question(self) -> None:
        self._clear_inputs()
        question = self.question
        kind = question.get("type")
        self.hints, self.answered_current, self._confidence = 0, False, ""
        self._shown_at = time.monotonic()
        self.meta.setText(self._meta_text())
        self.stem.setText(str(question.get("stem", "")))
        texts = [str(o.get("text", "")) for o in question.get("options") or []] if kind == "mc" else ["True", "False"] if kind == "tf" else []
        for text in texts:
            choice = WrapButton(QRadioButton(), text)
            self.option_group.addButton(choice.box)
            self.options.addWidget(choice)
            self.choices.append(choice)
        self.answer_line.clear()
        self.answer_line.setReadOnly(False)
        self.answer_line.setVisible(kind == "calc")
        self.answer_text.clear()
        self.answer_text.setReadOnly(False)
        self.answer_text.setVisible(kind in ("short", "case"))
        self.hint_label.clear()
        self.hint_label.hide()
        self.hint_btn.setVisible(bool(question.get("hints")) and not self.is_mock)
        self.hint_btn.setEnabled(True)
        auto = kind not in FREE_TYPES
        free_confidence = kind in FREE_TYPES and not self.is_mock
        self._set_visible(self.confidence_title, self.confidence_row, on=auto or free_confidence)
        if free_confidence:
            self.confidence_title.setText("How sure are you? Your answer is shown when you press one.")
        else:
            self.confidence_title.setText("How sure are you? Your answer is checked when you press one.")
        self.save_free_btn.setVisible(not auto and self.is_mock)
        self._set_visible(self.model_answer, self.rubric_save, self.grade_row, self.feedback, self.next_btn, on=False)
        self.flag_btn.show()
        self.end_btn.show()
        self.scroll.verticalScrollBar().setValue(0)

    @staticmethod
    def _set_visible(*widgets: QWidget, on: bool) -> None:
        for widget in widgets:
            widget.setVisible(on)

    def _given(self) -> str:
        kind = self.question.get("type")
        if kind in ("mc", "tf"):
            return next((c.label.text() for c in self.choices if c.box.isChecked()), "")
        return self.answer_line.text().strip() if kind == "calc" else self.answer_text.toPlainText().strip()

    def show_hint(self) -> None:
        hints = self.question.get("hints") or []
        self.hints = min(self.hints + 1, len(hints))
        self.hint_label.setText("\n".join(f"Hint {i + 1}: {hint}" for i, hint in enumerate(hints[: self.hints])))
        self.hint_label.show()
        self.hint_btn.setEnabled(self.hints < len(hints))

    def check(self, confidence: str) -> None:
        given = self._given()
        if not given and self.question.get("type") not in FREE_TYPES:  # not knowing a free answer is allowed: it shows the answer
            self.status.emit("Answer first: then say how sure you are.")
            self.feedback.setText("<i>Answer first: then say how sure you are.</i>")
            self.feedback.show()
            return
        score = practice.grade_answer(self.question, given)
        if score is None:  # a calculation with no number to compare: the student judges it
            self._confidence = confidence
            self.reveal()
            return
        self._record(given, score, confidence, "auto")

    def _record(self, answer: str, score: float | None, confidence: str, by: str, ungraded: bool = False) -> None:
        """At once, so a half-finished session keeps its data."""
        question = self.question
        ms = int((time.monotonic() - self._shown_at) * 1000)
        event = self.store.record_attempt(question, answer, score, confidence, self.hints, ms, self.mode, by)
        self.results.append(Result(question, answer, score, event["id"], ungraded))
        self.answered_current = True
        knowledge = self.store.knowledge()
        self.cards = practice.replay(knowledge, self.store.events(), exam_date(self.subject))
        if not self.is_mock:
            practice.requeue_again(knowledge, self.cards, self.session, self.position, question, event.get("rating", 0))
        self.answered.emit()
        if self.is_mock:
            self.next_question()
        else:
            self._show_feedback(answer, score)

    # --- free answers -------------------------------------------------------

    def reveal(self) -> None:
        """The model answer, and how to grade against it: rubric points, or three plain buttons."""
        question = self.question
        self.answer_text.setReadOnly(True)
        self.answer_line.setReadOnly(True)
        self._set_visible(self.hint_btn, self.confidence_title, self.confidence_row, self.save_free_btn, self.feedback, on=False)
        self.model_answer.setText(f"<b>Model answer</b><br>{_text(right_answer(question))}{self._details(question)}")
        self.model_answer.show()
        points = [p for p in question.get("rubric") or [] if isinstance(p, dict)]
        for point in points:
            marks = float(point.get("marks") or 1)
            box = WrapButton(QCheckBox(), f"{point.get('point', '')} ({marks:g})")
            self.rubric_box.addWidget(box)
            self.rubric.append((box, marks))
        self.rubric_save.setVisible(bool(points))
        self.grade_row.setVisible(not points)

    def save_rubric(self) -> None:
        total = sum(marks for _box, marks in self.rubric) or 1.0
        self.self_grade(sum(marks for box, marks in self.rubric if box.box.isChecked()) / total)

    def self_grade(self, score: float) -> None:
        self._set_visible(self.rubric_save, self.grade_row, on=False)
        if self._reviewing:
            result = self._reviewing.pop(0)
            result.score, result.ungraded = score, False
            self.store.append_event("grade", attempt=result.attempt, score=score, feedback="", by="self")
            self.answered.emit()
            self._review_next()
            return
        self.model_answer.hide()
        self._record(self._given(), score, self._confidence, "self")

    def save_free_for_later(self) -> None:
        """A mock gives no feedback, so its free answers are graded at the end."""
        answer = self._given()
        waiting = bool(answer) or self.question.get("type") == "card"  # a blank answer needs no grading
        self._record(answer, None if waiting else 0.0, "", "self", ungraded=waiting)

    # --- feedback -----------------------------------------------------------

    def _source_links(self, sources: list) -> str:
        links = []
        for source in sources or []:
            if isinstance(source, dict) and source.get("label"):
                self.links.append(source)
                links.append(f"<a href='src:{len(self.links) - 1}'>{html.escape(str(source['label']))}</a>")
        return " · ".join(links)

    def _details(self, question: dict) -> str:
        """What is worth reading after answering: why, how, and where it stands."""
        parts = []
        if question.get("explain"):
            parts.append(f"<p>{_text(question['explain'])}</p>")
        if question.get("steps"):
            steps = "".join(f"<li>{_text(step)}</li>" for step in question["steps"])
            parts.append(f"<ol>{steps}</ol>")
        links = self._source_links(question.get("sources"))
        if links:
            parts.append(f"<p>Source: {links}</p>")
        if isinstance(question.get("style_of"), dict):
            parts.append(f"<p>In the style of {self._source_links([question['style_of']])}</p>")
        return "".join(parts)

    def _verdict(self, question: dict, answer: str, score: float) -> str:
        if score >= practice.PASS:
            text = f"<b style='color:{RIGHT}'>Correct</b>" if score > practice.GOOD_SCORE else f"<b style='color:{RIGHT}'>Partly right ({score:.0%})</b>"
        else:
            text = f"<b style='color:{WRONG}'>Not quite.</b>"
            correct = right_answer(question)
            if correct and question.get("type") not in FREE_TYPES:
                text += f" Correct answer: <b>{_text(correct)}</b>"
        if question.get("type") == "mc" and score < practice.PASS:
            chosen = next((o for o in question.get("options") or [] if str(o.get("text", "")) == answer), {})
            if chosen.get("why_wrong"):
                text += f"<p>Why not this one: {_text(chosen['why_wrong'])}</p>"
        return text

    def _show_feedback(self, answer: str, score: float) -> None:
        question = self.question
        self.links = []
        self._set_visible(self.hint_btn, self.confidence_title, self.confidence_row, on=False)
        for choice in self.choices:
            choice.setEnabled(False)
        self.answer_line.setReadOnly(True)
        self.answer_text.setReadOnly(True)
        self.feedback.setText(self._verdict(question, answer, score) + self._details(question))
        self.feedback.show()
        self.next_btn.setText("Next" if self.position + 1 < len(self.session) else "Finish")
        self.next_btn.show()

    def _open_link(self, href: str) -> None:
        if href.startswith("src:") and int(href[4:]) < len(self.links):
            self.sourceActivated.emit(self.links[int(href[4:])])

    def next_question(self) -> None:
        self.position += 1
        if self.position >= len(self.session):
            self.finish()
        else:
            self._show_question()

    def flag(self) -> None:
        reason, ok = QInputDialog.getText(self, "Question is wrong", "What is wrong with it? Claude reads this with the next export.")
        if not ok:
            return
        question = self.question
        self.store.flag_question(question.get("id", ""), reason.strip())
        self.status.emit("Flagged. It stays out of practice, and Claude hears about it with the next export.")
        if self.answered_current:
            return
        self.session.pop(self.position)
        if self.position >= len(self.session):
            self.finish()
        else:
            self._show_question()

    # --- the end ------------------------------------------------------------

    def _time_left(self) -> str:
        left = max(0, int(self.deadline - time.monotonic()))
        return f"{left // 60}:{left % 60:02d}"

    def _tick(self) -> None:
        if self.deadline and time.monotonic() >= self.deadline:
            self.status.emit("Time is up.")
            self.finish()
        elif self.question_view.isVisibleTo(self) and self.session and not self._reviewing and self.position < len(self.session):
            self.meta.setText(self._meta_text())

    def finish(self) -> None:
        self.timer.stop()
        self.deadline = 0.0
        self._reviewing = [result for result in self.results if result.ungraded] if self.is_mock else []
        if self._reviewing:
            self._review_next()
        else:
            self._show_summary()

    def _review_next(self) -> None:
        """The mock is over: now the free answers can be held against the model answer."""
        if not self._reviewing:
            self._show_summary()
            return
        self._clear_inputs()
        result = self._reviewing[0]
        self.session, self.position = [result.question], 0
        self.meta.setText(f"Grade your free answers · {len(self._reviewing)} left")
        self.stem.setText(str(result.question.get("stem", "")))
        self.answer_text.setPlainText(result.answer)
        self.answer_text.setVisible(result.question.get("type") != "card")
        self.answer_line.hide()
        self.flag_btn.hide()
        self.end_btn.hide()
        self.next_btn.hide()
        self.reveal()

    def _show_summary(self) -> None:
        results, was_mock = self.results, self.is_mock
        self._clear_inputs()
        self._stop_session()
        self.idle.setText(self._mock_summary(results) if was_mock else self._session_summary(results))
        self.refresh()

    def _session_summary(self, results: list[Result]) -> str:
        if not results:
            return "Session ended before an answer. Nothing changed."
        right = sum(result.score >= practice.PASS for result in results)
        wrong = [result for result in results if result.score < practice.PASS]
        lines = [f"<b>Session finished: {right} of {len(results)} right.</b> Your answers are saved."]
        if wrong:
            lines.append("To look at again:<ul>" + "".join(f"<li>{_text(r.question.get('stem', ''))}</li>" for r in wrong) + "</ul>")
        return "".join(f"<p>{line}</p>" for line in lines)

    def _mock_summary(self, results: list[Result]) -> str:
        """Now the feedback a mock held back, question by question."""
        if not results:
            return "Mock ended before an answer. Nothing changed."
        self.links = []
        score = sum(result.score for result in results) / len(results)
        low, high = practice.wilson(score, len(results))
        parts = [
            f"<p><b>Mock finished: {score:.0%} on {len(results)} question{'s' * (len(results) != 1)}.</b><br>"
            f"With so few questions the true level lies between {low:.0%} and {high:.0%}.</p>"
        ]
        for result in results:
            mark = f"<span style='color:{RIGHT}'>✓</span>" if result.score >= practice.PASS else f"<span style='color:{WRONG}'>✗</span>"
            answer = _text(result.answer) if result.answer else "<i>no answer</i>"
            parts.append(
                f"<p>{mark} <b>{_text(result.question.get('stem', ''))}</b><br>You: {answer}<br>"
                f"Answer: {_text(right_answer(result.question))}</p>{self._details(result.question)}"
            )
        return "".join(parts)

    def _stop_session(self) -> None:
        self.timer.stop()
        self.deadline = 0.0
        self.session, self.results, self._reviewing, self.position, self.mode = [], [], [], 0, ""
        self.question_view.hide()
        self.idle.show()
        self.controls.show()


def _text(value: object) -> str:
    return html.escape(str(value)).replace("\n", "<br>")
