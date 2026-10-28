"""AI Tutor dock: Claude export/import, interactive quiz, analysis, search."""

from __future__ import annotations

import html
import json
import random
import re
import subprocess
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..professors import ProfessorStore
from ..services import claude_bridge
from ..services.timestamps import TIMESTAMP_RE, stamp_seconds
from ..services.vector_index import Hit
from ..storage import Lecture

ROLE = Qt.ItemDataRole.UserRole


class QuizWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.lecture: Lecture | None = None
        self.questions: list[dict] = []
        self.index = 0
        self.score = 0
        self.answered: set[int] = set()

        self.header = QLabel()
        self.header.setStyleSheet("color: gray;")
        self.question = QLabel(wordWrap=True, textInteractionFlags=Qt.TextInteractionFlag.TextSelectableByMouse)
        self.question.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.options_box = QVBoxLayout()
        self.option_group = QButtonGroup(self)
        self.free_answer = QPlainTextEdit(placeholderText="Write your answer, then press Check.")
        self.free_answer.setFixedHeight(80)
        self.feedback = QTextBrowser()
        self.feedback.setMaximumHeight(160)

        self.check_btn = QPushButton("Check")
        self.check_btn.clicked.connect(self._check)
        self.got_it = QPushButton("✓ I had it")
        self.got_it.clicked.connect(lambda: self._self_grade(True))
        self.missed = QPushButton("✗ Missed it")
        self.missed.clicked.connect(lambda: self._self_grade(False))
        prev_btn = QPushButton("◀ Prev")
        prev_btn.clicked.connect(lambda: self._go(self.index - 1))
        next_btn = QPushButton("Next ▶")
        next_btn.clicked.connect(lambda: self._go(self.index + 1))
        shuffle = QPushButton("Shuffle && restart")
        shuffle.clicked.connect(self._restart)

        buttons = QHBoxLayout()
        for w in (prev_btn, self.check_btn, self.got_it, self.missed, next_btn):
            buttons.addWidget(w)

        layout = QVBoxLayout(self)
        layout.addWidget(self.header)
        layout.addWidget(self.question)
        layout.addLayout(self.options_box)
        layout.addWidget(self.free_answer)
        layout.addLayout(buttons)
        layout.addWidget(self.feedback, 1)
        layout.addWidget(shuffle, alignment=Qt.AlignmentFlag.AlignRight)
        self.set_questions(None, [])

    def set_questions(self, lecture: Lecture | None, questions: list[dict]) -> None:
        self.lecture = lecture
        self.questions = list(questions)
        self.index, self.score, self.answered = 0, 0, set()
        self._show()

    def _restart(self) -> None:
        random.shuffle(self.questions)
        self.index, self.score, self.answered = 0, 0, set()
        self._show()

    def _go(self, index: int) -> None:
        if self.questions:
            self.index = max(0, min(len(self.questions) - 1, index))
            self._show()

    def _clear_options(self) -> None:
        for button in self.option_group.buttons():
            self.option_group.removeButton(button)
            self.options_box.removeWidget(button)
            button.setParent(None)
            button.deleteLater()

    def _show(self) -> None:
        self._clear_options()
        self.feedback.clear()
        has = bool(self.questions)
        for w in (self.check_btn, self.got_it, self.missed):
            w.setVisible(has)
        if not has:
            self.header.setText("")
            self.question.setText("No quiz yet. Export for Claude, then import its JSON reply.")
            self.free_answer.hide()
            self.got_it.hide()
            self.missed.hide()
            return
        q = self.questions[self.index]
        self.header.setText(
            f"Question {self.index + 1}/{len(self.questions)} · {q.get('difficulty', 'medium')} · "
            f"score {self.score}/{len(self.answered)}"
        )
        self.question.setText(q["question"])
        qtype = q["type"]
        options = q.get("options", []) if qtype == "multiple_choice" else ["True", "False"] if qtype == "true_false" else []
        for option in options:
            button = QRadioButton(option)
            self.option_group.addButton(button)
            self.options_box.addWidget(button)
        self.free_answer.setVisible(qtype == "short_answer")
        self.free_answer.clear()
        done = self.index in self.answered
        self.check_btn.setEnabled(not done)
        self.got_it.setVisible(False)
        self.missed.setVisible(False)

    def _check(self) -> None:
        q = self.questions[self.index]
        explanation = html.escape(q.get("explanation", ""))
        source = f"<p style='color:gray'>Source: {html.escape(q['source_ref'])}</p>" if q.get("source_ref") else ""
        if q["type"] == "short_answer":
            self.feedback.setHtml(
                f"<b>Model answer:</b><p>{html.escape(q['answer'])}</p><p>{explanation}</p>{source}"
                "<p><i>Grade yourself below.</i></p>"
            )
            self.check_btn.setEnabled(False)
            self.got_it.setVisible(True)
            self.missed.setVisible(True)
            return
        chosen = self.option_group.checkedButton()
        if chosen is None:
            self.feedback.setHtml("<i>Pick an answer first.</i>")
            return
        correct = chosen.text().strip().lower() == str(q["answer"]).strip().lower()
        verdict = "<b style='color:#1a7f37'>Correct!</b>" if correct else (
            f"<b style='color:#cf222e'>Not quite.</b> Correct answer: <b>{html.escape(str(q['answer']))}</b>"
        )
        self.feedback.setHtml(f"{verdict}<p>{explanation}</p>{source}")
        self._record(correct)

    def _self_grade(self, correct: bool) -> None:
        self.got_it.setVisible(False)
        self.missed.setVisible(False)
        self._record(correct)

    def _record(self, correct: bool) -> None:
        if self.index in self.answered:
            return
        self.answered.add(self.index)
        self.score += int(correct)
        self.check_btn.setEnabled(False)
        q = self.questions[self.index]
        self.header.setText(
            f"Question {self.index + 1}/{len(self.questions)} · {q.get('difficulty', 'medium')} · "
            f"score {self.score}/{len(self.answered)}"
        )
        if len(self.answered) == len(self.questions):
            self._save_attempt()

    def _save_attempt(self) -> None:
        if self.lecture is None:
            return
        path = self.lecture.dir("ai") / "quiz_attempts.json"
        try:
            attempts = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except json.JSONDecodeError:
            attempts = []
        attempts.append({"date": datetime.now().isoformat(timespec="seconds"), "score": self.score, "total": len(self.questions)})
        path.write_text(json.dumps(attempts, indent=2), encoding="utf-8")
        self.feedback.append(f"<p><b>Quiz finished: {self.score}/{len(self.questions)}</b> — saved to quiz_attempts.json</p>")


def analysis_html(data: dict) -> str:
    e = html.escape
    parts = [f"<h2>{e(data.get('lecture', 'Analysis'))}</h2>"]
    if data.get("imported_at"):
        parts.append(f"<p style='color:gray'>Imported {e(data['imported_at'])}</p>")
    parts.append(f"<h3>Summary</h3><p>{e(data.get('summary', ''))}</p>")
    if data.get("exam_focus"):
        rows = "".join(
            f"<li><b>{e(str(f.get('topic', '')))}</b> <i>({e(str(f.get('likelihood', '?')))})</i> — {e(str(f.get('reason', '')))}</li>"
            for f in data["exam_focus"]
        )
        parts.append(f"<h3>Likely exam topics</h3><ul>{rows}</ul>")
    if data.get("key_concepts"):
        rows = "".join(
            f"<li><b>{e(str(c.get('term', '')))}</b>: {e(str(c.get('explanation', '')))}</li>" for c in data["key_concepts"]
        )
        parts.append(f"<h3>Key concepts</h3><ul>{rows}</ul>")
    if data.get("knowledge_gaps"):
        rows = "".join(f"<li>{e(str(g))}</li>" for g in data["knowledge_gaps"])
        parts.append(f"<h3>Gaps in your notes</h3><ul>{rows}</ul>")
    if data.get("flashcards"):
        rows = "".join(
            f"<tr><td><b>{e(str(c.get('front', '')))}</b></td><td>{e(str(c.get('back', '')))}</td></tr>"
            for c in data["flashcards"]
        )
        parts.append(f"<h3>Flashcards</h3><table border='1' cellpadding='4' cellspacing='0'>{rows}</table>")
    return "".join(parts)


class AIPanel(QWidget):
    searchRequested = Signal(str, bool)  # query, this lecture only
    hitActivated = Signal(object)  # Hit
    responseImported = Signal()

    def __init__(self, professors: ProfessorStore, parent=None):
        super().__init__(parent)
        self.lecture: Lecture | None = None
        self.professors = professors

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_roundtrip_tab(), "Claude")
        self.quiz = QuizWidget()
        self.tabs.addTab(self.quiz, "Quiz")
        self.analysis = QTextBrowser()
        self.tabs.addTab(self.analysis, "Analysis")
        self.tabs.addTab(self._build_search_tab(), "Search")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.tabs)
        self.set_lecture(None)

    # --- tabs ---------------------------------------------------------------

    def _build_roundtrip_tab(self) -> QWidget:
        tab = QWidget()
        self.prof_label = QLabel(wordWrap=True)
        self.n_questions = QSpinBox(minimum=3, maximum=50, value=10)
        self.copy_clipboard = QCheckBox("Copy to clipboard")
        self.copy_clipboard.setChecked(True)
        export_btn = QPushButton("⇪ Export for Claude")
        export_btn.clicked.connect(self.export_context)
        self.export_status = QLabel(wordWrap=True)
        self.export_status.linkActivated.connect(lambda href: subprocess.run(["open", "-R", href]))

        self.paste = QPlainTextEdit(placeholderText="Paste Claude's reply here (the JSON block, with or without ``` fences)…")
        paste_btn = QPushButton("Paste from clipboard")
        paste_btn.clicked.connect(lambda: self.paste.setPlainText(QGuiApplication.clipboard().text()))
        import_btn = QPushButton("⇩ Import AI Response")
        import_btn.clicked.connect(self.import_response)

        row = QHBoxLayout()
        row.addWidget(QLabel("Questions:"))
        row.addWidget(self.n_questions)
        row.addWidget(self.copy_clipboard)
        row.addStretch()
        row.addWidget(export_btn)
        row2 = QHBoxLayout()
        row2.addWidget(paste_btn)
        row2.addStretch()
        row2.addWidget(import_btn)

        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("<b>1 · Export</b> — builds Context.md with notes, transcript, slides and professor profile.", wordWrap=True))
        layout.addWidget(self.prof_label)
        layout.addLayout(row)
        layout.addWidget(self.export_status)
        layout.addWidget(QLabel("<b>2 · Import</b> — paste Claude's JSON answer.", wordWrap=True))
        layout.addWidget(self.paste, 1)
        layout.addLayout(row2)
        return tab

    def _build_search_tab(self) -> QWidget:
        tab = QWidget()
        self.query = QLineEdit(placeholderText="Ask or search across transcripts, slides and notes…")
        self.query.returnPressed.connect(self._search)
        self.scope = QComboBox()
        self.scope.addItems(["Whole subject", "This lecture"])
        go = QPushButton("Search")
        go.clicked.connect(self._search)
        self.results = QListWidget()
        self.results.setWordWrap(True)
        self.results.itemActivated.connect(lambda item: self.hitActivated.emit(item.data(ROLE)))
        self.results.itemDoubleClicked.connect(lambda item: self.hitActivated.emit(item.data(ROLE)))

        row = QHBoxLayout()
        row.addWidget(self.query, 1)
        row.addWidget(self.scope)
        row.addWidget(go)
        layout = QVBoxLayout(tab)
        layout.addLayout(row)
        layout.addWidget(self.results, 1)
        layout.addWidget(QLabel("<span style='color:gray'>Double-click a result to jump to the slide or audio.</span>", wordWrap=True))
        return tab

    # --- binding ------------------------------------------------------------

    def set_lecture(self, lecture: Lecture | None) -> None:
        self.lecture = lecture
        self.tabs.setEnabled(lecture is not None)
        self.export_status.clear()
        self.refresh_professor()
        self.load_latest()

    def refresh_professor(self) -> None:
        prof = self.professors.get(self.lecture.professor_id) if self.lecture else None
        self.prof_label.setText(
            f"Professor profile: <b>{html.escape(prof.name)}</b>" if prof else
            "Professor profile: <i>none — assign one via right-click in the sidebar</i>"
        )

    def load_latest(self) -> None:
        latest = claude_bridge.load_latest(self.lecture) if self.lecture else None
        if latest is None:
            self.quiz.set_questions(self.lecture, [])
            self.analysis.setHtml("<p style='color:gray'>No AI analysis imported for this lecture yet.</p>")
            return
        self.quiz.set_questions(self.lecture, latest.data.get("quizzes", []))
        self.analysis.setHtml(analysis_html(latest.data))

    # --- actions ------------------------------------------------------------

    def export_context(self) -> None:
        if self.lecture is None:
            return
        prof = self.professors.get(self.lecture.professor_id)
        try:
            path = claude_bridge.export_context(self.lecture, prof, self.n_questions.value())
        except Exception as exc:
            QMessageBox.warning(self, "Export for Claude", f"Export failed:\n{exc}")
            return
        text = path.read_text(encoding="utf-8")
        if self.copy_clipboard.isChecked():
            QGuiApplication.clipboard().setText(text)
        self.export_status.setText(
            f"Wrote <a href='{html.escape(str(path), quote=True)}'>{path.name}</a> ({len(text):,} characters)"
            + (" and copied it to the clipboard. Paste it into Claude." if self.copy_clipboard.isChecked() else ".")
        )

    def import_response(self) -> None:
        if self.lecture is None:
            return
        try:
            response = claude_bridge.parse_response(self.paste.toPlainText())
        except claude_bridge.ResponseError as exc:
            QMessageBox.warning(self, "Import AI Response", str(exc))
            return
        path = claude_bridge.save_response(self.lecture, response)
        labelled = claude_bridge.apply_segment_labels(self.lecture, response)
        self.paste.clear()
        self.load_latest()
        self.tabs.setCurrentWidget(self.quiz)
        self.responseImported.emit()
        QMessageBox.information(
            self,
            "Import AI Response",
            f"Imported {len(response.quizzes)} questions"
            + (f" and sorted {labelled} transcript segments" if labelled else "")
            + f".\nSaved to {path.parent.name}/{path.name}",
        )

    def _search(self) -> None:
        if self.query.text().strip():
            self.results.clear()
            self.results.addItem("Searching…")
            self.searchRequested.emit(self.query.text().strip(), self.scope.currentIndex() == 1)

    def show_results(self, hits: list[Hit]) -> None:
        self.results.clear()
        if not hits:
            self.results.addItem("No results. (Tools → Rebuild Search Index if you just added material.)")
            return
        for hit in hits:
            snippet = re.sub(r"\s+", " ", hit.text)[:240]
            item = QListWidgetItem(f"[{hit.lecture} · {hit.source} · {hit.ref}]  {hit.score:.2f}\n{snippet}")
            item.setData(ROLE, hit)
            self.results.addItem(item)


def hit_target(hit: Hit) -> tuple[str, str, float] | None:
    """For a transcript hit: (kind, audio stem, seconds); for slides: (kind, pdf, page)."""
    if hit.source == "transcript":
        match = TIMESTAMP_RE.search(hit.ref)
        if match:
            return "audio", hit.ref[: match.start()].strip(), float(stamp_seconds(match))
    if hit.source == "slides":
        match = re.match(r"(.+) p\.(\d+)$", hit.ref)
        if match:
            return "slides", match.group(1), float(match.group(2))
    return None
