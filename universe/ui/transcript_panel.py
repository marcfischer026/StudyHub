"""Transcript dock: whisper.cpp segments with relevance labels.

Click a line to hear it. Exam-relevant lines are highlighted (★), questions and
answers marked, private talk greyed out, other voices than the lecturer's tagged
(👥) — and right-click corrects any label or speaker.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..services import relevance, speakers
from ..services.timestamps import make_stamp
from ..services.transcriber import Transcript
from ..storage import Lecture

ROLE = Qt.ItemDataRole.UserRole
EXAM_BG = QColor(255, 196, 0, 70)  # translucent so it works in light and dark mode
QUESTION_BG = QColor(47, 111, 237, 35)
ICONS = {"question": "❓ ", "answer": "💬 ", "off_topic": "🔇 "}
SOURCE_NAMES = {"rules": "exam cue", "local": "local AI", "claude": "Claude", "manual": "you"}

FILTERS = (
    "All",
    "★ Exam-relevant",
    "Questions & answers",
    "Hide private talk",
    "Private talk only",
    "🎓 Lecturer only",
    "👥 Other speakers",
)


class TranscriptPanel(QWidget):
    seekRequested = Signal(str, float)  # audio stem, seconds
    labelsChanged = Signal(object)  # Lecture
    classifyRequested = Signal(object)  # Lecture

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lecture: Lecture | None = None
        self.transcript: Transcript | None = None
        self.path: Path | None = None

        self.files = QComboBox()
        self.files.currentIndexChanged.connect(self._show_selected)
        self.filter = QComboBox()
        self.filter.addItems(FILTERS)
        self.filter.currentIndexChanged.connect(lambda _i: self._apply_filter())
        self.find = QLineEdit(placeholderText="Find in transcript…")
        self.find.setClearButtonEnabled(True)
        self.find.textChanged.connect(lambda _t: self._apply_filter())

        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)  # label several lines at once
        self.list.itemClicked.connect(self._clicked)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)

        self.summary = QLabel(wordWrap=True)
        self.summary.setObjectName("muted")
        self.classify_btn = QPushButton("Sort with local AI")
        self.classify_btn.setToolTip(
            "Let the local model (LM Studio/Ollama) mark private talk, questions and exam hints. Runs on this Mac."
        )
        self.classify_btn.clicked.connect(lambda: self.lecture and self.classifyRequested.emit(self.lecture))
        self.progress = QProgressBar(maximum=100)
        self.progress.setFormat("Sorting… %p%")
        self.progress.hide()

        row = QHBoxLayout()
        row.addWidget(self.files, 1)
        row.addWidget(self.filter)
        bottom = QHBoxLayout()
        bottom.addWidget(self.summary, 1)
        bottom.addWidget(self.classify_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(row)
        layout.addWidget(self.find)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.progress)
        layout.addLayout(bottom)

    # --- binding ------------------------------------------------------------

    def set_lecture(self, lecture: Lecture | None) -> None:
        self.lecture = lecture
        self.refresh()

    def refresh(self, select_stem: str = "") -> None:
        current = select_stem or (self.path.stem if self.path else "")
        self.files.blockSignals(True)
        self.files.clear()
        for path in self.lecture.transcripts() if self.lecture else []:
            self.files.addItem(path.stem, str(path))
        if current:
            self.files.setCurrentIndex(max(0, self.files.findText(current)))
        self.files.blockSignals(False)
        self._show_selected()

    def set_classifying(self, active: bool, percent: int = 0) -> None:
        self.progress.setVisible(active)
        self.progress.setValue(percent)
        self.classify_btn.setEnabled(not active)

    # --- display ------------------------------------------------------------

    def _show_selected(self) -> None:
        scroll = self.list.verticalScrollBar().value()
        self.list.clear()
        data = self.files.currentData()
        self.path = Path(data) if data else None
        self.transcript = Transcript.load(self.path) if self.path else None
        self.classify_btn.setEnabled(self.transcript is not None and self.progress.isHidden())
        if self.transcript is None:
            self.list.addItem("No transcript yet. Record or import audio, then press Transcribe.")
            self.summary.clear()
            return
        stem = Path(self.transcript.audio).stem
        for index, seg in enumerate(self.transcript.segments):
            item = QListWidgetItem()
            item.setData(ROLE, (stem, seg.start, index))
            self._style_item(item, index)
            self.list.addItem(item)
        self._apply_filter()
        self._update_summary()
        self.list.verticalScrollBar().setValue(scroll)

    def _style_item(self, item: QListWidgetItem, index: int) -> None:
        seg = self.transcript.segments[index]
        star = "★ " if seg.exam else ""
        voice = "" if not seg.speaker else "🎓 " if seg.speaker == speakers.LECTURER else f"👥 {speakers.speaker_name(seg.speaker)}: "
        item.setText(f"{star}{ICONS.get(seg.category, '')}{make_stamp(seg.start)}  {voice}{seg.text}")
        font = QFont(item.font())
        font.setItalic(seg.private)
        font.setBold(seg.exam)
        item.setFont(font)
        palette = self.list.palette()
        text_color = palette.color(palette.ColorRole.PlaceholderText) if seg.private else palette.color(palette.ColorRole.Text)
        item.setForeground(QBrush(text_color))
        background = EXAM_BG if seg.exam else QUESTION_BG if seg.category in ("question", "answer") else QColor(0, 0, 0, 0)
        item.setBackground(QBrush(background))
        details = [relevance.CATEGORY_LABELS.get(seg.category, "Not sorted yet")]
        if seg.exam:
            details.append("exam-relevant")
        if seg.note:
            details.append(seg.note)
        if seg.source:
            details.append(f"by {SOURCE_NAMES.get(seg.source, seg.source)}")
        voice = [speakers.speaker_name(seg.speaker)] if seg.speaker else []
        if seg.loudness is not None:
            voice.append(f"{seg.loudness:.0f} dB")
        item.setToolTip(" · ".join(details) + ("\nVoice: " + " · ".join(voice) if voice else "") + "\nRight-click to change.")

    def _apply_filter(self) -> None:
        if self.transcript is None:
            return
        mode = self.filter.currentText()
        needle = self.find.text().lower()
        for i in range(self.list.count()):
            item = self.list.item(i)
            seg = self.transcript.segments[item.data(ROLE)[2]]
            show = {
                "★ Exam-relevant": seg.exam,
                "Questions & answers": seg.category in ("question", "answer"),
                "Hide private talk": not seg.private,
                "Private talk only": seg.private,
                "🎓 Lecturer only": seg.speaker == speakers.LECTURER,
                "👥 Other speakers": bool(seg.speaker) and seg.speaker != speakers.LECTURER,
            }.get(mode, True)
            item.setHidden(not show or (bool(needle) and needle not in seg.text.lower()))

    def _update_summary(self) -> None:
        segs = self.transcript.segments if self.transcript else []
        exam = sum(s.exam for s in segs)
        private = sum(s.private for s in segs)
        questions = sum(s.category == "question" for s in segs)
        unsorted = sum(not s.category for s in segs)
        parts = [f"★ {exam} exam-relevant", f"❓ {questions} questions", f"🔇 {private} private (left out of export & search)"]
        if unsorted:
            parts.append(f"{unsorted} not sorted yet")
        self.summary.setText(" · ".join(parts))

    # --- interaction --------------------------------------------------------

    def _clicked(self, item: QListWidgetItem) -> None:
        data = item.data(ROLE)
        if data:
            self.seekRequested.emit(data[0], data[1])

    def _context_menu(self, pos) -> None:
        items = [i for i in self.list.selectedItems() if i.data(ROLE)] or [
            i for i in [self.list.itemAt(pos)] if i is not None and i.data(ROLE)
        ]
        if not items or self.transcript is None:
            return
        segs = [self.transcript.segments[i.data(ROLE)[2]] for i in items]
        menu = QMenu(self)
        exam_now = all(s.exam for s in segs)
        menu.addAction("☆ Not exam-relevant" if exam_now else "★ Exam-relevant", lambda: self._set(items, exam=not exam_now))
        menu.addSeparator()
        for category, icon in (("lecture", "📖 "), ("question", "❓ "), ("answer", "💬 "), ("off_topic", "🔇 ")):
            menu.addAction(icon + relevance.CATEGORY_LABELS[category], lambda c=category: self._set(items, category=c))
        menu.addSeparator()
        menu.addAction("🎓 The lecturer said this", lambda: self._set_speaker(items, speakers.LECTURER))
        menu.addAction("👥 Someone else said this", lambda: self._set_speaker(items, speakers.OTHER))
        voices = {s.speaker for s in segs}
        if len(voices) == 1 and (voice := voices.pop()) not in ("", speakers.LECTURER, speakers.OTHER):
            menu.addAction(
                f"🎓 {speakers.speaker_name(voice)} is the lecturer (whole recording)",
                lambda: self._make_lecturer(voice),
            )
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _set_speaker(self, items: list[QListWidgetItem], speaker: str) -> None:
        for item in items:
            self.transcript.segments[item.data(ROLE)[2]].speaker = speaker
        self._save_speakers()

    def _make_lecturer(self, speaker: str) -> None:
        speakers.make_lecturer(self.transcript, speaker)
        self._save_speakers()

    def _save_speakers(self) -> None:
        for i in range(self.list.count()):
            self._style_item(self.list.item(i), self.list.item(i).data(ROLE)[2])
        self.transcript.save(self.path.parent)
        self._apply_filter()
        self.labelsChanged.emit(self.lecture)

    def _set(self, items: list[QListWidgetItem], category: str | None = None, exam: bool | None = None) -> None:
        for item in items:
            index = item.data(ROLE)[2]
            relevance.set_manual(self.transcript.segments[index], category=category, exam=exam)
            self._style_item(item, index)
        self.transcript.save(self.path.parent)
        self._apply_filter()
        self._update_summary()
        self.labelsChanged.emit(self.lecture)
