"""Main window: toolbar with Record, notes in the centre, dockable panels around."""

from __future__ import annotations

import base64
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, QFile, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QInputDialog,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QMenu,
    QStackedWidget,
    QToolBar,
    QToolButton,
    QWidget,
)

from ..config import Settings
from ..professors import ProfessorStore
from ..services import local_llm, moodle_ical, relevance, transcriber
from ..services.vector_index import SubjectIndex, get_embedder
from ..storage import Lecture, Subject, Workspace
from . import workers
from .ai_panel import AIPanel, hit_target
from .audio import AudioController, PlayerBar, RecordControls, RecordingsPanel, open_mic_privacy_settings
from .note_editor import NoteEditor
from .pdf_viewer import PdfViewer
from .professor_dialog import AssignProfessorDialog, ProfessorManager
from .settings_dialog import SettingsDialog
from .sidebar import Sidebar
from .transcript_panel import TranscriptPanel
from .jobs import ActivityIndicator, JobManager
from .welcome import LectureView, WelcomePage
from .workers import Worker

# Bump when the default dock arrangement changes, so stale saved layouts are ignored.
STATE_VERSION = 2


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.workspace = Workspace()
        self.professors = ProfessorStore()
        self.lecture: Lecture | None = None
        self.subject: Subject | None = None
        self._keep_alive: set[Worker] = set()
        self._ready = False

        self.setWindowTitle("UniVerse")
        self.resize(1440, 900)
        self.setDockNestingEnabled(True)
        self.setDockOptions(
            QMainWindow.DockOption.AnimatedDocks
            | QMainWindow.DockOption.AllowNestedDocks
            | QMainWindow.DockOption.AllowTabbedDocks
        )

        self.jobs = JobManager(self)  # transcription + local AI, one at a time, pausable

        # centre: welcome page or the open lecture (header, notes, player)
        self.audio = AudioController(settings, self)
        self.notes = NoteEditor(time_source=self.audio.time_source)
        self.player_bar = PlayerBar(self.audio)
        self.lecture_view = LectureView(self.notes, self.player_bar, self.professors)
        self.welcome = WelcomePage(self.workspace)
        self.center = QStackedWidget()
        self.center.addWidget(self.welcome)
        self.center.addWidget(self.lecture_view)
        self.setCentralWidget(self.center)

        # docks
        self.sidebar = Sidebar(self.workspace)
        self.pdf = PdfViewer(ensure_lecture=self.ensure_lecture)
        self.transcript = TranscriptPanel()
        self.ai = AIPanel(self.professors)
        self.recordings = RecordingsPanel(
            self.audio,
            ensure_lecture=self.ensure_lecture,
            lectures=lambda: [lec for subject in self.workspace.subjects() for lec in subject.lectures()],
        )
        self.docks: dict[str, QDockWidget] = {}
        for key, title, widget in (
            ("workspace", "Subjects", self.sidebar),
            ("slides", "Slides", self.pdf),
            ("transcript", "Transcript", self.transcript),
            ("ai", "AI Tutor", self.ai),
            ("recordings", "Recordings", self.recordings),
        ):
            dock = QDockWidget(title, self)
            dock.setObjectName(f"dock_{key}")
            dock.setWidget(widget)
            self.docks[key] = dock

        self._build_toolbar()
        self.activity = ActivityIndicator(self.jobs, settings)
        self.statusBar().addPermanentWidget(self.activity)
        self.activity.models.message.connect(lambda text: self.statusBar().showMessage(text, 10000))
        self.jobs.changed.connect(self._on_jobs_changed)
        self._default_layout()
        self._build_menus()
        self._connect()

        self._restore_state()
        self.center.setVisible(self.settings.notes_visible)
        self.open_lecture(None)
        self.sidebar.select_by_name(settings.last_subject, settings.last_lecture)
        QTimer.singleShot(0, lambda: setattr(self, "_ready", True))
        if settings.moodle_calendar_url:
            self.welcome.set_events(moodle_ical.parse(moodle_ical.cached()))  # until the download below is done
        self.refresh_moodle()
        self._moodle_timer = QTimer(self, interval=60 * 60 * 1000)  # Moodle's export changes rarely
        self._moodle_timer.timeout.connect(self.refresh_moodle)
        self._moodle_timer.start()
        if transcriber.check_setup(settings):
            self.statusBar().showMessage("Transcription not set up — see Tools → Check Transcription Setup.", 8000)

    # --- layout -------------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main")
        bar.setObjectName("mainToolbar")
        # An unmovable QToolBar loses its overflow menu and forces the window to
        # fit every button; keep it "movable" and hide the grip in the stylesheet.
        bar.setMovable(True)
        bar.setFloatable(False)
        bar.setAllowedAreas(Qt.ToolBarArea.TopToolBarArea)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)

        self.record_controls = RecordControls(self.audio)
        bar.addWidget(self.record_controls)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)

        stamp = QPushButton("⏱  Timestamp")
        stamp.setToolTip("Insert [▶ HH:MM:SS] at the current recording/playback time (⌘T)")
        stamp.clicked.connect(self.notes.insert_timestamp)
        export = QPushButton("⇪  Export for Claude")
        export.setObjectName("primaryButton")
        export.setToolTip("Build Context.md for this lecture and copy it to the clipboard (⌘E)")
        export.clicked.connect(self._export_for_claude)
        import_btn = QPushButton("⇩  Import reply")
        import_btn.setToolTip("Paste Claude's JSON answer")
        import_btn.clicked.connect(self._focus_import)
        self.lecture_buttons = (stamp, export, import_btn)
        for button in self.lecture_buttons:
            bar.addWidget(button)

        # Closed panels come back from here (same menu as View in the menu bar).
        self.panels_menu = QMenu("Panels", self)
        # Notes live in the central area, not a dock; hiding it lets the docks take the space.
        self.notes_action = QAction("Notes", self, checkable=True, checked=self.settings.notes_visible)
        self.notes_action.setShortcut(QKeySequence("Ctrl+0"))
        self.notes_action.toggled.connect(self._set_notes_visible)
        self.panels_menu.addAction(self.notes_action)
        for number, dock in enumerate(self.docks.values(), start=1):
            action = dock.toggleViewAction()
            action.setShortcut(QKeySequence(f"Ctrl+{number}"))
            action.toggled.connect(lambda visible, d=dock: self._on_panel_toggled(d, visible))
            self.panels_menu.addAction(action)
        self.panels_menu.addSeparator()
        self.panels_menu.addAction("Reset Layout", lambda: (self.notes_action.setChecked(True), self._default_layout()))
        panels = QToolButton(text="▦  Panels")
        panels.setObjectName("panelsButton")
        panels.setToolTip("Show or hide panels — closed panels can be reopened here")
        panels.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        panels.setMenu(self.panels_menu)
        bar.addWidget(panels)

    def _on_panel_toggled(self, dock: QDockWidget, visible: bool) -> None:
        if not self._ready:  # restoreState also toggles panels
            return
        if visible:
            dock.raise_()
        else:
            shortcut = dock.toggleViewAction().shortcut().toString(QKeySequence.SequenceFormat.NativeText)
            self.statusBar().showMessage(
                f"“{dock.windowTitle()}” closed — reopen it any time from ▦ Panels (top right) or {shortcut}.", 10000
            )

    def _set_notes_visible(self, visible: bool) -> None:
        self.settings.notes_visible = visible
        self.center.setVisible(visible)
        if not self._ready:
            return
        if visible:
            self.notes.edit.setFocus()
        else:
            shortcut = self.notes_action.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
            self.statusBar().showMessage(
                f"“Notes” closed — reopen it any time from ▦ Panels (top right) or {shortcut}.", 10000
            )

    def _default_layout(self) -> None:
        d = self.docks
        ready, self._ready = self._ready, False  # no "panel closed" hints while rearranging
        for dock in d.values():
            dock.setFloating(False)
            self.removeDockWidget(dock)  # drop old tab groups so the arrangement is rebuilt cleanly
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, d["workspace"])
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, d["slides"])
        for key in ("ai", "transcript", "recordings"):
            self.tabifyDockWidget(d["slides"], d[key])
        for dock in d.values():
            dock.show()
        d["slides"].raise_()
        if ready:  # re-enable after Qt's queued show/hide events
            QTimer.singleShot(0, lambda: setattr(self, "_ready", True))
        # Dock sizes only stick once the window has its real size.
        QTimer.singleShot(
            0, lambda: self.resizeDocks([d["workspace"], d["slides"]], [250, 480], Qt.Orientation.Horizontal)
        )

    def _build_menus(self) -> None:
        bar = self.menuBar()

        def add(menu, text, slot, shortcut=None):
            action = QAction(text, self)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(slot)
            menu.addAction(action)
            return action

        file_menu = bar.addMenu("&File")
        add(file_menu, "New Subject…", self.sidebar.new_subject, "Ctrl+Shift+N")
        add(file_menu, "New Lecture…", self.sidebar.new_lecture, "Ctrl+N")
        file_menu.addSeparator()
        add(file_menu, "Import Slides (PDF)…", self._import_pdf, "Ctrl+Shift+I")
        add(file_menu, "Import Audio…", self.recordings._import)
        file_menu.addSeparator()
        add(file_menu, "Reveal in Finder", self._reveal)
        add(file_menu, "Settings…", self._open_settings, QKeySequence.StandardKey.Preferences)
        add(file_menu, "Quit", self.close, QKeySequence.StandardKey.Quit)

        notes_menu = bar.addMenu("&Notes")
        add(notes_menu, "Insert Timestamp", self.notes.insert_timestamp, "Ctrl+T")
        add(notes_menu, "Save Notes", self.notes.save, QKeySequence.StandardKey.Save)

        audio_menu = bar.addMenu("&Audio")
        add(audio_menu, "Start / Stop Recording", self.toggle_recording, "Ctrl+R")
        add(audio_menu, "Pause / Resume Recording", self.audio.toggle_pause)
        add(audio_menu, "Play / Pause", self.audio.toggle_play, "Ctrl+Shift+Space")
        add(audio_menu, "Transcribe Selected Audio", self.recordings._transcribe)
        audio_menu.addSeparator()
        add(audio_menu, "Microphone Permissions…", open_mic_privacy_settings)

        prof_menu = bar.addMenu("&Professors")
        add(prof_menu, "Manage Profiles…", self._manage_professors)
        add(prof_menu, "Assign to Current Lecture…", lambda: self.lecture and self._assign_professor(self.lecture))
        add(prof_menu, "Assign to Current Subject…", lambda: self.subject and self._assign_professor(self.subject))

        ai_menu = bar.addMenu("A&I")
        add(ai_menu, "Export for Claude", self._export_for_claude, "Ctrl+E")
        add(ai_menu, "Import AI Response", self._focus_import)

        tools = bar.addMenu("&Tools")
        add(tools, "Rebuild Search Index (Subject)", self._rebuild_index)
        add(tools, "Check Transcription Setup", self._check_whisper)

        self.panels_menu.setTitle("&View")
        bar.addMenu(self.panels_menu)

    def _connect(self) -> None:
        self.sidebar.lectureSelected.connect(self.open_lecture)
        self.sidebar.subjectSelected.connect(self._open_subject)
        self.sidebar.assignProfessorRequested.connect(self._assign_professor)
        self.sidebar.aboutToChange.connect(self._release_files)
        self.sidebar.lectureDeleted.connect(self._lecture_deleted)
        self.sidebar.lectureRenamed.connect(self._lecture_renamed)
        self.sidebar.subjectChanged.connect(lambda _s: self.lecture and self.lecture_view.show_lecture(self.lecture))

        self.welcome.createSubject.connect(self.sidebar.new_subject)
        self.welcome.createLecture.connect(self.sidebar.new_lecture)
        self.welcome.quickRecord.connect(self.toggle_recording)
        self.welcome.openLecture.connect(lambda lecture: self.sidebar.refresh(select=lecture))
        self.lecture_view.closeRequested.connect(lambda: self.notes_action.setChecked(False))
        self.lecture_view.assignProfessor.connect(lambda: self.lecture and self._assign_professor(self.lecture))

        self.record_controls.recordClicked.connect(self.toggle_recording)
        self.audio.recordingChanged.connect(self._on_recording_changed)
        self.audio.recordingFinished.connect(self._on_recording_finished)
        self.audio.transcribeRequested.connect(self.transcribe)
        self.recordings.deleteRequested.connect(self.delete_recordings)
        self.recordings.moveRequested.connect(self.move_recordings)
        self.sidebar.recordingsDropped.connect(self.move_recordings)
        self.audio.silenceDetected.connect(
            lambda: self.statusBar().showMessage("⚠ The microphone is silent — click the warning in the toolbar for help.")
        )
        self.audio.error.connect(lambda msg: QMessageBox.warning(self, "Audio", msg))

        self.notes.seekRequested.connect(self._seek_from_notes)
        self.transcript.seekRequested.connect(self._seek_audio)
        self.transcript.labelsChanged.connect(self.schedule_index)
        self.transcript.classifyRequested.connect(self.classify)
        self.pdf.pdfImported.connect(lambda _p: self.lecture and self.schedule_index(self.lecture))

        self.ai.searchRequested.connect(self.search)
        self.ai.hitActivated.connect(self._open_hit)
        self.ai.responseImported.connect(self._on_response_imported)

    # --- lecture switching --------------------------------------------------

    def open_lecture(self, lecture: Lecture | None) -> None:
        previous = self.lecture
        if previous is not None and previous.path.exists() and self.notes.save():
            self.schedule_index(previous)
        self.lecture = lecture
        if lecture is not None:
            self.subject = lecture.subject
            if not self.audio.recording and lecture.migrate_recording_names():
                self.schedule_index(lecture)  # transcript refs carry the file name
            self.lecture_view.show_lecture(lecture)
            self.center.setCurrentWidget(self.lecture_view)
        else:
            self.welcome.show_subject(self.subject)
            self.center.setCurrentWidget(self.welcome)
        self.notes.set_lecture(lecture)
        self.pdf.set_lecture(lecture)
        self.audio.set_lecture(lecture)
        self.transcript.set_lecture(lecture)
        self.ai.set_lecture(lecture)
        for button in self.lecture_buttons:
            button.setEnabled(lecture is not None)
        where = f"{lecture.subject.name} › {lecture.name}" if lecture else (self.subject.name if self.subject else "")
        self.setWindowTitle(f"UniVerse — {where}" if where else "UniVerse")

    def _open_subject(self, subject: Subject) -> None:
        self.subject = subject
        self.open_lecture(None)

    def _release_files(self) -> None:
        """Called before the sidebar renames or deletes folders on disk."""
        self.notes.save()
        self.audio.stop_recording()
        self.audio.release()

    def _lecture_deleted(self, subject: Subject, name: str) -> None:
        if self.lecture is not None and not self.lecture.path.exists():
            self.lecture = None
        if self.subject is not None and not self.subject.path.exists():
            self.subject = None
        if self.lecture is None:
            self.open_lecture(None)
        if subject.path.exists():
            self._run_index(lambda: SubjectIndex(subject, self._embedder()).remove_lecture(name))

    def _lecture_renamed(self, old_name: str, lecture: Lecture) -> None:
        self.lecture = None  # old path is gone; don't try to save it
        self._lecture_deleted(lecture.subject, old_name)
        self.schedule_index(lecture)

    # --- recording ----------------------------------------------------------

    def toggle_recording(self) -> None:
        if self.audio.recording:
            self.audio.stop_recording()
            return
        lecture = self.ensure_lecture()
        if lecture is not None:
            self.audio.start_recording(lecture)

    def ensure_lecture(self, name_hint: str = "") -> Lecture | None:
        """The open lecture — or, when none is open, a new one so a recording or
        import is never lost. Named after ``name_hint`` (e.g. the PDF) or the date."""
        if self.lecture is not None:
            return self.lecture
        subject = self.subject or self.sidebar.current_subject()
        if subject is None:
            subjects = self.workspace.subjects()
            if len(subjects) == 1:
                subject = subjects[0]
            elif subjects:
                names = [s.name for s in subjects] + ["New subject…"]
                choice, ok = QInputDialog.getItem(self, "New lecture", "Which subject is this lecture for?", names, 0, False)
                if not ok:
                    return None
                subject = self.workspace.subject(choice) if choice != names[-1] else self.sidebar.new_subject()
            else:
                name, ok = QInputDialog.getText(
                    self, "New lecture", "Which course is this lecture for?\n(e.g. Corporate Finance)", text="My Course"
                )
                if not ok or not name.strip():
                    return None
                try:
                    subject = self.workspace.create_subject(name.strip())
                except (FileExistsError, ValueError) as exc:
                    QMessageBox.warning(self, "New lecture", str(exc))
                    return None
        if subject is None:
            return None
        base = name_hint.replace("_", " ").strip() or f"Lecture {datetime.now():%d %b, %H.%M}"
        name, n = base, 2
        while subject.lecture(name) is not None:
            name, n = f"{base} ({n})", n + 1
        try:
            lecture = subject.create_lecture(name)
        except ValueError as exc:
            QMessageBox.warning(self, "New lecture", str(exc))
            return None
        self.sidebar.refresh(select=lecture)  # opens it via lectureSelected
        self.statusBar().showMessage(f"Created lecture “{name}” in {subject.name} — rename it any time (right-click).", 8000)
        return lecture

    def delete_recordings(self, paths: list[Path]) -> None:
        """Move recordings and their transcripts to the macOS Trash, after asking."""
        lecture = self.lecture
        if lecture is None or not paths:
            return
        if self.audio.recording and self.audio.recorder.path in paths:
            QMessageBox.information(self, "Delete recording", "Stop the recording before deleting it.")
            return
        names = "\n".join(f"• {p.stem}" for p in paths)
        transcribed = sum(len(lecture.audio_bundle(p)) > 1 for p in paths)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Delete recording")
        box.setText(f"Move {'this recording' if len(paths) == 1 else f'{len(paths)} recordings'} to the Trash?")
        box.setInformativeText(
            names
            + ("\n\nTheir transcripts and sorting labels go with them." if transcribed else "")
            + "\nYou can restore them from the Trash in Finder."
        )
        delete_btn = box.addButton("Move to Trash", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is not delete_btn:
            return

        for path in paths:
            job = self.jobs.find(("transcribe", str(path)))
            if job is not None:
                self.jobs.cancel(job)
        if self.audio.current in paths:
            self.audio.release()
        failed = [f.name for p in paths for f in lecture.audio_bundle(p) if not QFile.moveToTrash(str(f))]
        if failed:
            QMessageBox.warning(self, "Delete recording", "Could not move to the Trash:\n" + "\n".join(failed))
        self.audio.filesChanged.emit()
        self.transcript.refresh()
        self.schedule_index(lecture)
        moved = sum(not p.exists() for p in paths)
        self.statusBar().showMessage(f"Moved {moved} recording{'s' * (moved != 1)} to the Trash.", 6000)

    def move_recordings(self, paths: list[Path], target: Lecture) -> None:
        """Move recordings (with transcripts and labels) to another lecture, e.g. one
        recorded while the wrong lecture was open. They slot in by recording time."""
        lecture = self.lecture
        paths = [p for p in paths if lecture is not None and p.parent == lecture.dir("audio") and p.exists()]
        if not paths or target.path == lecture.path:
            self.recordings.refresh()
            return
        if self.audio.recording and self.audio.recorder.path in paths:
            QMessageBox.information(self, "Move recording", "Stop the recording before moving it.")
            self.recordings.refresh()
            return
        busy = [p.stem for p in paths if self.jobs.find(("transcribe", str(p)))]
        if busy:
            QMessageBox.information(
                self, "Move recording", "Wait until transcribing has finished:\n" + "\n".join(f"• {s}" for s in busy)
            )
            self.recordings.refresh()
            return
        if self.audio.current in paths:
            self.audio.release()
        moved, failed = [], []
        for path in paths:
            try:
                moved.append(lecture.move_audio(path, target))
            except OSError as exc:
                failed.append(f"{path.name}: {exc}")
        if failed:
            QMessageBox.warning(self, "Move recording", "Could not move:\n" + "\n".join(failed))
        self.audio.set_lecture(lecture)  # reload the list and player
        self.transcript.refresh()
        self.sidebar.update_icons()
        self.schedule_index(lecture)
        self.schedule_index(target)
        if moved:
            count = f"{len(moved)} recording{'s' * (len(moved) != 1)}"
            renamed = [p.stem for p, m in zip(paths, moved) if p.stem != m.stem]
            note = f" (renamed to {', '.join(m.stem for m in moved)})" if renamed else ""
            self.statusBar().showMessage(f"Moved {count} to {target.subject.name} › {target.name}{note}.", 8000)

    def _on_recording_changed(self, recording: bool) -> None:
        if recording:
            self.statusBar().showMessage("Recording… ⌘T adds a timestamp to your notes.")
            self.notes.edit.setFocus()

    def _on_recording_finished(self, path: Path) -> None:
        self.statusBar().showMessage(f"Saved {path.name}.", 6000)
        self.recordings.refresh()

    # --- audio / notes ------------------------------------------------------

    def _seek_from_notes(self, seconds: int) -> None:
        if self.audio.player.path is None:
            self.statusBar().showMessage("No audio in this lecture yet.", 4000)
            return
        self.audio.seek(seconds)

    def _seek_audio(self, stem: str, seconds: float) -> None:
        if not self.audio.select_stem(stem):
            self.statusBar().showMessage(f"Audio file for '{stem}' not found.", 4000)
            return
        self.audio.seek(seconds)

    def transcribe(self, audio_path: Path) -> None:
        """Queue a whisper.cpp transcription (see the activity indicator, bottom right)."""
        problems = transcriber.check_setup(self.settings)
        if problems:
            QMessageBox.warning(self, "Transcription", "\n\n".join(problems))
            return
        # The file may belong to a lecture other than the open one (recording kept
        # running while switching), so derive it from the path: <Subject>/Lectures/<Lecture>/audio/<file>.
        lecture = Lecture(Subject(audio_path.parents[3]), audio_path.parents[1])
        language = lecture.subject.meta.get("language") or self.settings.whisper_language
        prompt = f"{lecture.subject.name}."  # primes subject vocabulary

        def job(progress, control):
            result = transcriber.transcribe(
                audio_path, self.settings, progress=progress, language=language, prompt=prompt, control=control
            )
            relevance.apply_exam_cues(result)  # stage 1: instant, offline
            result.save(lecture.dir("transcripts"))
            return result

        def done(result):
            self.statusBar().showMessage(f"Transcribed {audio_path.name}: {len(result.segments)} segments.", 8000)
            if self._is_open(lecture):
                self.transcript.refresh(select_stem=audio_path.stem)
                self.recordings.refresh()
                self.docks["transcript"].raise_()
            if self.settings.local_classify:
                self.classify(lecture, only=audio_path.stem, automatic=True)  # stage 3; re-indexes when done
            else:
                self.schedule_index(lecture)

        queued = self.jobs.submit(
            "transcribe",
            f"Transcribing {audio_path.stem}",
            job,
            done,
            lambda message: QMessageBox.warning(self, "Transcription failed", message),
            key=("transcribe", str(audio_path)),
        )
        if queued is None:
            self.statusBar().showMessage(f"{audio_path.name} is already being transcribed.", 4000)

    # --- relevance: what is lecture content, what is side talk -------------

    def classify(self, lecture: Lecture, only: str = "", automatic: bool = False) -> None:
        """Queue sorting of transcript segments with the local LLM (lecture / question /
        answer / off_topic, exam hints). ``only`` limits it to one audio stem."""
        context = relevance.course_context(lecture)

        def job(progress, control):
            paths = lecture.transcripts()
            targets = [(n, p) for n, p in enumerate(paths, start=1) if not only or p.stem == only]
            for done_count, (t_no, path) in enumerate(targets):
                transcript = transcriber.Transcript.load(path)
                audio = lecture.dir("audio") / transcript.audio
                found_speakers = transcriber.identify_speakers_in(transcript, audio, self.settings, control)
                if relevance.apply_exam_cues(transcript) or found_speakers:
                    transcript.save(path.parent)

                def step(pct, k=done_count):
                    progress(int((k * 100 + pct) / len(targets)))

                labels = local_llm.classify(
                    transcript, t_no, lecture.subject.name, context, self.settings, step, control=control
                )
                relevance.apply_labels(lecture, labels, "local")
            return relevance.summary(lecture)

        def done(counts):
            if self._is_open(lecture):
                self.transcript.refresh()
            self.statusBar().showMessage(
                f"Sorted: ★ {counts['exam']} exam-relevant · ❓ {counts['question']} questions · "
                f"🔇 {counts['off_topic']} private (left out of export & search).",
                10000,
            )
            self.schedule_index(lecture)

        def failed(message):
            self.schedule_index(lecture)
            if automatic:
                self.statusBar().showMessage(
                    "Local AI not reachable — transcript not sorted. Start LM Studio, then use "
                    "“Sort with local AI” in the Transcript panel (or let Claude do it via Export).",
                    12000,
                )
            else:
                QMessageBox.warning(self, "Sort with local AI", message)

        title = f"Sorting {only or lecture.name}"
        if self.jobs.submit("classify", title, job, done, failed, key=("classify", str(lecture.path), only)) is None:
            if not automatic:
                self.statusBar().showMessage("This transcript is already being sorted.", 4000)

    def _on_jobs_changed(self) -> None:
        """Mirror the running job in the Recordings and Transcript panels."""
        job = self.jobs.current
        transcribing = job is not None and job.kind == "transcribe"
        classifying = job is not None and job.kind == "classify"
        if transcribing or self.recordings.progress.isVisible():
            self.recordings.set_transcribing(transcribing, job.progress if transcribing else 0)
        if classifying or self.transcript.progress.isVisible():
            self.transcript.set_classifying(classifying, job.progress if classifying else 0)

    def _is_open(self, lecture: Lecture) -> bool:
        return self.lecture is not None and self.lecture.path == lecture.path

    def _on_response_imported(self) -> None:
        self.docks["ai"].raise_()
        if self.lecture is not None:
            self.transcript.refresh()
            self.schedule_index(self.lecture)

    # --- search index -------------------------------------------------------

    def _embedder(self):
        return get_embedder(self.settings.embedding_model)

    def _run_index(self, fn, on_done=None) -> None:
        self._start(
            Worker(fn),
            on_done,
            lambda msg: self.statusBar().showMessage(f"Indexing failed: {msg}", 8000),
            pool=workers.index_pool,
        )

    def schedule_index(self, lecture: Lecture) -> None:
        self._run_index(lambda: SubjectIndex(lecture.subject, self._embedder()).index_lecture(lecture))

    def _rebuild_index(self) -> None:
        subject = self.subject
        if subject is None:
            return
        self.statusBar().showMessage(f"Rebuilding search index for {subject.name}…")
        self.notes.save()
        self._run_index(
            lambda: SubjectIndex(subject, self._embedder()).index_all(),
            lambda n: self.statusBar().showMessage(f"{subject.name}: indexed {n} chunks.", 8000),
        )

    def search(self, query: str, lecture_only: bool) -> None:
        subject = self.subject
        if subject is None:
            self.ai.show_results([])
            return
        lecture_name = self.lecture.name if lecture_only and self.lecture else None
        self._start(
            Worker(lambda: SubjectIndex(subject, self._embedder()).search(query, limit=12, lecture=lecture_name)),
            self.ai.show_results,
            lambda msg: QMessageBox.warning(self, "Search", msg),
            pool=workers.index_pool,
        )

    def _open_hit(self, hit) -> None:
        if hit is None or self.subject is None:
            return
        if self.lecture is None or hit.lecture != self.lecture.name:
            self.sidebar.select_by_name(self.subject.name, hit.lecture)
        target = hit_target(hit)
        if target is None:
            return
        kind, name, value = target
        if kind == "audio":
            self._seek_audio(name, value)
        else:
            self.pdf.open_page(name, int(value))
            self.docks["slides"].raise_()

    # --- professors ---------------------------------------------------------

    def _manage_professors(self) -> None:
        ProfessorManager(self.professors, self).exec()
        self._refresh_professor()

    def _assign_professor(self, target: Subject | Lecture) -> None:
        current = target.meta.get("professor_id", "")
        dialog = AssignProfessorDialog(self.professors, target.name, current, self)
        if dialog.exec():
            target.update_meta(professor_id=dialog.selected_id())
            self._refresh_professor()

    def _refresh_professor(self) -> None:
        self.ai.refresh_professor()
        if self.lecture is not None:
            self.lecture_view.show_lecture(self.lecture)

    # --- misc ---------------------------------------------------------------

    def _import_pdf(self) -> None:
        self.docks["slides"].raise_()
        self.pdf.import_pdf()

    def _export_for_claude(self) -> None:
        if self.lecture is None:
            return
        self.docks["ai"].raise_()
        self.ai.tabs.setCurrentIndex(0)
        self.ai.export_context()
        self.statusBar().showMessage("Context.md copied — paste it into Claude, then bring the reply back with ⇩ Import reply.", 10000)

    def _focus_import(self) -> None:
        self.docks["ai"].raise_()
        self.ai.tabs.setCurrentIndex(0)
        self.ai.paste.setFocus()

    def _reveal(self) -> None:
        target = self.lecture.path if self.lecture else self.subject.path if self.subject else self.workspace.root
        subprocess.run(["open", str(target)])

    def _open_settings(self) -> None:
        if SettingsDialog(self.settings, self).exec():
            # panels that show a setting read it once; bring them up to date
            self.recordings.auto.setChecked(self.settings.auto_transcribe)
            self.recordings._fill_speech_settings()
            self.record_controls._update_mic_tooltip()
            self.activity._apply_battery_rules()
            self.activity.refresh()
            if not self.settings.moodle_calendar_url:
                self.welcome.set_events([])
            self.refresh_moodle()

    def refresh_moodle(self) -> None:
        url = self.settings.moodle_calendar_url
        if not url:
            return

        def failed(message: str) -> None:
            self.statusBar().showMessage(f"Moodle calendar not updated: {message}", 8000)

        self._start(
            Worker(lambda: moodle_ical.parse(moodle_ical.fetch(url))),
            on_done=self.welcome.set_events,
            on_fail=failed,
        )

    def _check_whisper(self) -> None:
        problems = transcriber.check_setup(self.settings)
        if problems:
            QMessageBox.warning(self, "Transcription Setup", "\n\n".join(problems))
        else:
            QMessageBox.information(
                self,
                "Transcription Setup",
                f"Ready.\nBinary: {self.settings.whisper_binary}\nModel: {self.settings.whisper_model_path()}",
            )

    def _start(self, worker: Worker, on_done=None, on_fail=None, pool=None) -> None:
        # Keep a Python reference until the job finishes so its signals stay alive.
        self._keep_alive.add(worker)
        worker.setAutoDelete(False)

        def finished(result):
            self._keep_alive.discard(worker)
            if on_done:
                on_done(result)

        def failed(message):
            self._keep_alive.discard(worker)
            if on_fail:
                on_fail(message)

        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        workers.run(worker, pool)

    def _restore_state(self) -> None:
        if self.settings.window_geometry:
            self.restoreGeometry(QByteArray(base64.b64decode(self.settings.window_geometry)))
        if self.settings.window_state:
            self.restoreState(QByteArray(base64.b64decode(self.settings.window_state)), STATE_VERSION)

    def closeEvent(self, event) -> None:
        if self.audio.recording:
            answer = QMessageBox.question(self, "Quit", "A recording is running. Stop it and quit?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if self.jobs.busy and not self.audio.recording:
            answer = QMessageBox.question(
                self,
                "Quit",
                f"Background AI work is still running ({self.jobs.current.title if self.jobs.current else 'queued'}).\n"
                "Quit anyway? It will be cancelled — you can start it again later with Transcribe / Sort.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.audio.shutdown()
        self.jobs.shutdown()
        if self.lecture is not None and self.notes.save():
            # Index synchronously on quit so the latest notes are searchable next time.
            try:
                SubjectIndex(self.lecture.subject, self._embedder()).index_lecture(self.lecture)
            except Exception:
                pass
        self.settings.window_geometry = base64.b64encode(bytes(self.saveGeometry())).decode()
        self.settings.window_state = base64.b64encode(bytes(self.saveState(STATE_VERSION))).decode()
        self.settings.last_subject = self.subject.name if self.subject else ""
        self.settings.last_lecture = self.lecture.name if self.lecture else ""
        self.settings.save()
        workers.index_pool.waitForDone(5000)
        super().closeEvent(event)
