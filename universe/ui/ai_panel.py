"""AI Tutor pane: hosts the Claude, Practice, Guide and Progress tabs, and Search.

It binds to the subject, not to a lecture: everything here works with no lecture open.
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..professors import ProfessorStore
from ..services.timestamps import TIMESTAMP_RE, stamp_seconds
from ..services.vector_index import Hit
from ..storage import Lecture, Subject
from ..config import Settings
from .claude_panel import ClaudePanel
from .guide_panel import GuidePanel
from .practice_panel import PracticePanel
from .progress_panel import ProgressPanel
from .segmented import SegmentedTabs

ROLE = Qt.ItemDataRole.UserRole
DOCUMENT_SOURCES = ("slides", "exams", "exercises")  # a hit's source is the folder of its PDF


class AIPanel(QWidget):
    searchRequested = Signal(str, bool)  # query, this lecture only
    hitActivated = Signal(object)  # Hit
    sourceActivated = Signal(dict)  # a durable source to jump to
    status = Signal(str)  # for the status bar
    replyImported = Signal()
    conversionsImported = Signal()

    def __init__(self, professors: ProfessorStore, parent=None, settings: Settings | None = None):
        super().__init__(parent)
        self.subject: Subject | None = None
        self.lecture: Lecture | None = None
        self.professors = professors

        self.claude = ClaudePanel(professors, settings=settings)
        self.practice = PracticePanel()
        self.guide = GuidePanel(professors)
        self.progress = ProgressPanel(professors)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.claude, "Claude")
        self.tabs.addTab(self.practice, "Practice")
        self.tabs.addTab(self.guide, "Guide")
        self.tabs.addTab(self.progress, "Progress")
        self.tabs.addTab(self._build_search_tab(), "Search")

        for panel in (self.claude, self.practice, self.guide, self.progress):
            panel.status.connect(self.status)
        for panel in (self.claude, self.practice, self.guide):
            panel.sourceActivated.connect(self.sourceActivated)
        self.claude.replyImported.connect(self._on_reply_imported)
        self.claude.conversionsImported.connect(self.conversionsImported)
        self.practice.answered.connect(self._on_answered)
        self.guide.changed.connect(self.refresh)
        self.progress.examChanged.connect(self.refresh_except_progress)
        self.progress.practiseTopic.connect(self.practise_topic)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 8, 4, 4)
        self.tab_bar = SegmentedTabs(self.tabs)
        layout.addWidget(self.tab_bar)
        layout.addWidget(self.tabs)
        self.set_scope(None, None)

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
        row.addWidget(self.scope, 1)
        row.addWidget(go)
        layout = QVBoxLayout(tab)
        layout.addWidget(self.query)
        layout.addLayout(row)
        layout.addWidget(self.results, 1)
        layout.addWidget(QLabel("<span style='color:gray'>Double-click a result to jump to the slide or audio.</span>", wordWrap=True))
        return tab

    # --- binding ------------------------------------------------------------

    def set_scope(self, subject: Subject | None, lecture: Lecture | None) -> None:
        if (subject.path if subject else None) != (self.subject.path if self.subject else None):
            self.results.clear()
        self.subject, self.lecture = subject, lecture
        self.tabs.setEnabled(subject is not None)
        self.tab_bar.setEnabled(subject is not None)
        for panel in (self.claude, self.practice, self.guide, self.progress):
            panel.set_scope(subject, lecture)

    def refresh(self) -> None:
        """Something the tabs read changed; each re-reads once it is on screen."""
        for panel in (self.claude, self.practice, self.guide, self.progress):
            panel.mark_changed()

    def refresh_except_progress(self) -> None:
        for panel in (self.claude, self.practice, self.guide):
            panel.mark_changed()

    def set_events(self, events: list) -> None:
        self.progress.set_events(events)

    def _on_reply_imported(self) -> None:
        for panel in (self.practice, self.guide, self.progress):
            panel.mark_changed()
        self.replyImported.emit()

    def _on_answered(self) -> None:
        for panel in (self.guide, self.progress):
            panel.mark_changed()

    def practise_topic(self, topic: str) -> None:
        self.tabs.setCurrentWidget(self.practice)
        self.practice.start("topic", topic=topic)

    def show_export(self) -> None:
        self.tabs.setCurrentWidget(self.claude)

    def show_import(self) -> None:
        self.tabs.setCurrentWidget(self.claude)
        self.claude.show_import()

    # --- search -------------------------------------------------------------

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
            item = QListWidgetItem(f"[{hit.lecture or 'Whole subject'} · {hit.source} · {hit.ref}]  {hit.score:.2f}\n{snippet}")
            item.setData(ROLE, hit)
            self.results.addItem(item)


def hit_target(hit: Hit) -> tuple[str, str, float] | None:
    """For a transcript hit: (kind, audio stem, seconds); for a PDF: (its folder, pdf, page)."""
    if hit.source == "transcript":
        match = TIMESTAMP_RE.search(hit.ref)
        if match:
            return "audio", hit.ref[: match.start()].strip(), float(stamp_seconds(match))
    if hit.source in DOCUMENT_SOURCES:
        match = re.match(r"(.+) p\.(\d+)$", hit.ref)
        if match:
            return hit.source, match.group(1), float(match.group(2))
    return None
