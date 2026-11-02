"""Transcript pane: whisper.cpp segments with relevance labels.

Click a line to hear it. Exam-relevant lines are highlighted with a star icon,
questions and answers carry their own icons, private talk is greyed out, other
voices than the lecturer's are named — and right-click corrects any label or
speaker. With several recordings in
a lecture, "All recordings" shows them as one transcript in recording order.

Lines show the time of day they were said. Pauses and the time between
recordings show up as their own rows, so gaps in the recording are visible.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QIcon
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
from ..services.audio_player import duration
from ..services.timestamps import format_seconds
from ..services.transcriber import Transcript
from ..storage import Lecture, clock_time, pauses, read_timeline, recording_time
from . import icons, style

ROLE = Qt.ItemDataRole.UserRole
EXAM_BG = QColor(255, 196, 0, 70)  # translucent so it works in light and dark mode
QUESTION_BG = QColor(style.ACCENT)
QUESTION_BG.setAlpha(35)
SOURCE_NAMES = {"rules": "exam cue", "local": "local AI", "claude": "Claude", "manual": "you"}

ALL_RECORDINGS = "All recordings (in order)"
MIN_GAP_S = 60  # shorter breaks between recordings are not worth a row

FILTERS = (
    "All",
    "Exam-relevant",
    "Questions & answers",
    "Hide private talk",
    "Private talk only",
    "Lecturer only",
    "Other speakers",
)


class TranscriptPanel(QWidget):
    seekRequested = Signal(str, float)  # audio stem, seconds
    labelsChanged = Signal(object)  # Lecture
    classifyRequested = Signal(object)  # Lecture
    status = Signal(str)  # for the status bar

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lecture: Lecture | None = None
        self.parts: list[tuple[Path, Transcript]] = []  # what is shown: one transcript, or all in order
        self.timelines: list[list[tuple[float, datetime]]] = []  # per part, see storage.read_timeline
        self.path: Path | None = None  # the single transcript shown; None for "All recordings"

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
        """Rebuild the recording chooser. Keeps the current choice; with several
        transcripts and nothing chosen yet, shows them all together."""
        previous = self.files.currentData() if self.files.count() else None
        self.files.blockSignals(True)
        self.files.clear()
        paths = self.lecture.transcripts() if self.lecture else []
        if len(paths) > 1:
            self.files.addItem(ALL_RECORDINGS, ALL_RECORDINGS)
        for path in paths:
            self.files.addItem(path.stem, str(path))
        if select_stem:
            self.files.setCurrentIndex(max(0, self.files.findText(select_stem)))
        elif previous is not None and self.files.findData(previous) >= 0:
            self.files.setCurrentIndex(self.files.findData(previous))
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
        if data == ALL_RECORDINGS:
            self.path = None
            paths = self.lecture.transcripts() if self.lecture else []
        else:
            self.path = Path(data) if data else None
            paths = [self.path] if self.path else []
        self.parts = [(path, Transcript.load(path)) for path in paths]
        audio = {p.stem: p for p in self.lecture.audio_files()} if self.lecture else {}
        self.timelines = []
        for path, transcript in self.parts:
            source = audio.get(Path(transcript.audio).stem)
            self.timelines.append(read_timeline(source) if source else [(0.0, recording_time(path))])
        self.classify_btn.setEnabled(bool(self.parts) and self.progress.isHidden())
        if not self.parts:
            self.list.addItem("No transcript yet. Record or import audio, then press Transcribe.")
            self.summary.clear()
            return
        previous_end: datetime | None = None
        for part, (path, transcript) in enumerate(self.parts):
            stem = Path(transcript.audio).stem
            timeline = self.timelines[part]
            source = audio.get(stem)
            length = (duration(source) if source else 0.0) or (transcript.segments[-1].end if transcript.segments else 0.0)
            start, end = timeline[0][1], clock_time(path, length, timeline)
            if previous_end is not None and (start - previous_end).total_seconds() >= MIN_GAP_S:
                self.list.addItem(self._gap(previous_end, start, "not recorded"))
            if len(self.parts) > 1:
                self.list.addItem(self._heading(path.stem, part, start, end))
            previous_end = max(end, previous_end or end)
            breaks = pauses(timeline)
            for index, seg in enumerate(transcript.segments):
                while breaks and seg.start >= breaks[0][0]:
                    _offset, paused_at, resumed_at = breaks.pop(0)
                    self.list.addItem(self._gap(paused_at, resumed_at, "paused"))
                item = QListWidgetItem()
                item.setData(ROLE, (stem, seg.start, index, part))
                self._style_item(item)
                self.list.addItem(item)
        self._apply_filter()
        self._update_summary()
        self.list.verticalScrollBar().setValue(scroll)

    def _heading(self, name: str, part: int, start: datetime, end: datetime) -> QListWidgetItem:
        """Divider between recordings in the "All recordings" view: when each ran and for how long."""
        minutes = round((end - start).total_seconds() / 60)
        item = self._info(f"{part + 1}. {name}  ·  {start:%a %d.%m.  %H:%M}–{end:%H:%M}  ·  {minutes} min")
        font = QFont(item.font())
        font.setBold(True)
        item.setFont(font)
        return item

    def _gap(self, start: datetime, end: datetime, what: str) -> QListWidgetItem:
        """A stretch of time with no audio: a pause, or the time between two recordings."""
        minutes = max(1, round((end - start).total_seconds() / 60))
        span = f"{start:%H:%M}–{end:%H:%M}" if start.date() == end.date() else f"{start:%d.%m. %H:%M}–{end:%d.%m. %H:%M}"
        return self._info(f"{span}  ·  {minutes} min {what}")

    def _info(self, text: str) -> QListWidgetItem:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)  # not selectable; carries no segment
        item.setForeground(QBrush(self.list.palette().color(self.list.palette().ColorRole.PlaceholderText)))
        return item

    def _segment(self, item: QListWidgetItem):
        _stem, _start, index, part = item.data(ROLE)
        return self.parts[part][1].segments[index]

    def _segment_items(self) -> list[QListWidgetItem]:
        return [self.list.item(i) for i in range(self.list.count()) if self.list.item(i).data(ROLE)]

    def _save(self, items: list[QListWidgetItem]) -> None:
        """Write back only the changed lines, onto the file as it is now: a background job
        may have labelled other lines since this view read it."""
        changed: dict[int, list[int]] = {}
        for item in items:
            _stem, _start, index, part = item.data(ROLE)
            changed.setdefault(part, []).append(index)
        refresh_needed = False
        unkept = 0
        for part, indexes in sorted(changed.items()):
            path, shown = self.parts[part]
            current = Transcript.load(path)
            mine = [shown.segments[index] for index in indexes]
            where = {(seg.start, seg.end): n for n, seg in enumerate(current.segments)}
            for seg in mine:
                if (n := where.get((seg.start, seg.end))) is not None:
                    current.segments[n] = seg
                else:  # same lines, new times: keep the label on the segment holding the old middle
                    mid = (seg.start + seg.end) / 2
                    target = next((i for i, s in enumerate(current.segments) if s.start <= mid <= s.end), None)
                    if target is None:
                        unkept += 1
                        continue
                    kept = current.segments[target]
                    kept.category, kept.exam, kept.note, kept.source = seg.category, seg.exam, seg.note, seg.source
                    if seg.speaker:
                        kept.speaker = seg.speaker
            current.save(path.parent)
            if len(current.segments) != len(shown.segments):  # transcribed again meanwhile
                refresh_needed = True
                continue
            self.parts[part] = (path, current)
        if refresh_needed:
            self._show_selected()
            if unkept:
                self.status.emit(f"{unkept} label{'s' * (unkept != 1)} could not be kept: the segment changed.")
            return
        if unkept:
            self.status.emit(f"{unkept} label{'s' * (unkept != 1)} could not be kept: the segment changed.")
        for item in self._segment_items():
            self._style_item(item)

    def _style_item(self, item: QListWidgetItem) -> None:
        seg = self._segment(item)
        voice = "" if not seg.speaker or seg.speaker == speakers.LECTURER else f"{speakers.speaker_name(seg.speaker)}: "
        part = item.data(ROLE)[3]
        clock = clock_time(self.parts[part][0], seg.start, self.timelines[part])
        item.setText(f"{clock:%H:%M:%S}  {voice}{seg.text}")
        if seg.exam:
            item.setIcon(icons.icon("star", style.WARNING))
        elif seg.category == "question":
            item.setIcon(icons.icon("circle-help", icons.ACCENT))
        elif seg.category == "answer":
            item.setIcon(icons.icon("message-circle", icons.ACCENT))
        elif seg.category == "off_topic":
            item.setIcon(icons.icon("volume-x", style.NEUTRAL))
        else:
            item.setIcon(QIcon())
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
        item.setToolTip(
            f"{clock:%H:%M:%S} · minute {format_seconds(seg.start)} of the recording\n"
            + " · ".join(details)
            + ("\nVoice: " + " · ".join(voice) if voice else "")
            + "\nRight-click to change."
        )

    def _apply_filter(self) -> None:
        if not self.parts:
            return
        mode = self.filter.currentText()
        needle = self.find.text().lower()
        for item in self._segment_items():
            seg = self._segment(item)
            show = {
                "Exam-relevant": seg.exam,
                "Questions & answers": seg.category in ("question", "answer"),
                "Hide private talk": not seg.private,
                "Private talk only": seg.private,
                "Lecturer only": seg.speaker == speakers.LECTURER,
                "Other speakers": bool(seg.speaker) and seg.speaker != speakers.LECTURER,
            }.get(mode, True)
            item.setHidden(not show or (bool(needle) and needle not in seg.text.lower()))

    def _update_summary(self) -> None:
        segs = [seg for _path, transcript in self.parts for seg in transcript.segments]
        exam = sum(s.exam for s in segs)
        private = sum(s.private for s in segs)
        questions = sum(s.category == "question" for s in segs)
        unsorted = sum(not s.category for s in segs)
        parts = [f"{exam} exam-relevant", f"{questions} questions", f"{private} private (left out of export & search)"]
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
        if not items or not self.parts:
            return
        segs = [self._segment(i) for i in items]
        wanted = {(i.data(ROLE)[3], i.data(ROLE)[2]) for i in items}

        def live() -> list[QListWidgetItem]:
            return [i for i in self._segment_items() if (i.data(ROLE)[3], i.data(ROLE)[2]) in wanted]

        menu = QMenu(self)
        exam_now = all(s.exam for s in segs)

        def add(text: str, icon: str, slot) -> None:
            action = menu.addAction(text, slot)
            if action is not None:  # the regression tests' stub menu returns nothing
                action.setIcon(icons.icon(icon))

        add("Not exam-relevant" if exam_now else "Mark exam-relevant", "star-off" if exam_now else "star",
            lambda: self._set(live(), exam=not exam_now))
        menu.addSeparator()
        for category, icon in (("lecture", "book-open"), ("question", "circle-help"), ("answer", "message-circle"), ("off_topic", "volume-x")):
            add(relevance.CATEGORY_LABELS[category], icon, lambda c=category: self._set(live(), category=c))
        menu.addSeparator()
        add("The lecturer said this", "graduation-cap", lambda: self._set_speaker(live(), speakers.LECTURER))
        add("Someone else said this", "users", lambda: self._set_speaker(live(), speakers.OTHER))
        voices = {s.speaker for s in segs}
        parts = {i.data(ROLE)[3] for i in items}  # speaker ids only mean something within one recording
        if len(voices) == 1 and len(parts) == 1 and (voice := voices.pop()) not in ("", speakers.LECTURER, speakers.OTHER):
            part = parts.pop()
            add(f"{speakers.speaker_name(voice)} is the lecturer (whole recording)", "graduation-cap",
                lambda: self._make_lecturer(voice, part))
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _set_speaker(self, items: list[QListWidgetItem], speaker: str) -> None:
        for item in items:
            self._segment(item).speaker = speaker
        self._save_speakers(items)

    def _make_lecturer(self, speaker: str, part: int = 0) -> None:
        speakers.make_lecturer(self.parts[part][1], speaker)
        self._save_speakers([i for i in self._segment_items() if i.data(ROLE)[3] == part])

    def _save_speakers(self, changed: list[QListWidgetItem]) -> None:
        for item in self._segment_items():
            self._style_item(item)
        self._save(changed)
        self._apply_filter()
        self.labelsChanged.emit(self.lecture)

    def _set(self, items: list[QListWidgetItem], category: str | None = None, exam: bool | None = None) -> None:
        for item in items:
            relevance.set_manual(self._segment(item), category=category, exam=exam)
            self._style_item(item)
        self._save(items)
        self._apply_filter()
        self._update_summary()
        self.labelsChanged.emit(self.lecture)
