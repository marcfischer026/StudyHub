"""Main window: glass toolbar with Record, then sidebar | notes beside slides | inspector."""

from __future__ import annotations

import base64
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QFile, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QInputDialog,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolBar,
    QToolButton,
    QWidget,
)

from ..config import Settings
from ..professors import ProfessorStore
from ..services import local_llm, moodle_ical, relevance, transcriber
from ..services.pdf_tools import guess_kind
from ..services.subject_model import load_subject, locate
from ..services.vector_index import SubjectIndex, get_embedder
from ..storage import AUDIO_SUFFIXES, DOC_TYPES, Lecture, Subject, Workspace, clock_time
from . import icons, style, workers
from .ai_panel import AIPanel, hit_target
from .audio import AudioController, PlayerBar, RecordControls, RecordingsPanel, open_mic_privacy_settings
from .claude_panel import study_pool
from .documents_panel import DocumentsPanel
from .note_editor import NoteEditor
from .platform import glass
from .professor_dialog import AssignProfessorDialog, ProfessorManager
from .progress_panel import ExamDialog
from .settings_dialog import SettingsDialog
from .sidebar import Sidebar
from .transcript_panel import TranscriptPanel
from .jobs import ActivityIndicator, JobManager, poll_pool
from .welcome import LectureView, WelcomePage
from .workbench import MODES, Workbench
from .workers import Worker

DRAG_EVENTS = (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop)
COMPACT_TOOLBAR_BELOW = 1200  # window width at which the mode switcher drops its labels
# View menu: (part, title, shortcut). ⌥⌘ keeps ⌘1–⌘4 free for workspace modes.
PART_ACTIONS = (
    ("sidebar", "Sidebar", "Ctrl+Alt+S"),
    ("notes", "Notes", "Ctrl+Alt+N"),
    ("documents", "Documents", "Ctrl+Alt+P"),
    ("inspector", "Inspector", "Ctrl+Alt+I"),
)


def dropped_files(mime) -> list[Path]:
    """The PDFs and audio files in a drag from Finder; nothing for anything else."""
    paths = [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()] if mime.hasUrls() else []
    return [p for p in paths if p.suffix.lower() in AUDIO_SUFFIXES | {".pdf"} and p.is_file()]


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.workspace = Workspace()
        self.professors = ProfessorStore()
        self.lecture: Lecture | None = None
        self.subject: Subject | None = None
        self._keep_alive: set[Worker] = set()
        self._events: list[moodle_ical.Event] = []
        self._ready = False

        self.setWindowTitle("UniVerse")
        self.resize(1440, 900)
        self.setUnifiedTitleAndToolBarOnMac(True)
        glass.prepare_window(self)  # before the window exists natively

        self.jobs = JobManager(self)  # transcription + local AI, one at a time, pausable

        # notes: welcome page or the open lecture (header, notes, player)
        self.audio = AudioController(settings, self)
        self.notes = NoteEditor(
            time_source=self.audio.time_source, context_source=lambda: self._note_context(), clock_source=self.audio.clock
        )
        self.player_bar = PlayerBar(self.audio)
        self.lecture_view = LectureView(self.notes, self.player_bar, self.professors)
        self.welcome = WelcomePage(self.workspace)
        self.center = QStackedWidget()
        self.center.addWidget(self.welcome)
        self.center.addWidget(self.lecture_view)

        self.sidebar = Sidebar(self.workspace)
        self.documents = DocumentsPanel(self._import_folder)
        self.documents.viewer.figureClipped.connect(self._add_figure)
        self.documents.viewer.linkRequested.connect(self._link_page)
        self.transcript = TranscriptPanel()
        self.ai = AIPanel(self.professors, settings=self.settings)
        self.recordings = RecordingsPanel(
            self.audio,
            ensure_lecture=self.ensure_lecture,
            lectures=lambda: [lec for subject in self.workspace.subjects() for lec in subject.lectures()],
        )
        self.workbench = Workbench(self.sidebar, self.center, self.documents)
        for key, title, widget, icon in (
            ("transcript", "Transcript", self.transcript, "file-text"),
            ("recordings", "Recordings", self.recordings, "mic"),
            ("ai", "AI Tutor", self.ai, "graduation-cap"),
        ):
            self.workbench.inspector.add_pane(key, title, widget, icon)
        self.setCentralWidget(self.workbench)

        self._build_toolbar()
        self.activity = ActivityIndicator(self.jobs, settings)
        self.statusBar().addPermanentWidget(self.activity)
        self.activity.models.message.connect(lambda text: self.statusBar().showMessage(text, 10000))
        self.jobs.changed.connect(self._on_jobs_changed)
        self._apply_glass()
        self._build_menus()
        self._connect()

        self._restore_state()
        # PDFs and audio can be dropped anywhere in the window, panels included.
        self.setAcceptDrops(True)
        QApplication.instance().installEventFilter(self)
        self.open_lecture(None)
        self.sidebar.select_by_name(settings.last_subject, settings.last_lecture)
        QTimer.singleShot(0, lambda: setattr(self, "_ready", True))
        if settings.moodle_calendar_url:
            self._set_events(moodle_ical.parse(moodle_ical.cached()))  # until the download below is done
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
        self.toolbar = bar

        # One checkable action per part: the toolbar toggles and the View menu share them.
        self.part_actions: dict[str, QAction] = {}
        for part, title, shortcut in PART_ACTIONS:
            action = QAction(title, self, checkable=True, checked=True)
            action.setShortcut(QKeySequence(shortcut))
            action.toggled.connect(lambda visible, p=part: self.workbench.set_shown(p, visible))
            self.part_actions[part] = action
        self.notes_action = self.part_actions["notes"]
        self.workbench.visibilityChanged.connect(self._on_part_toggled)

        sidebar_btn = self._toggle_button("sidebar", "panel-left", "Show or hide the sidebar")
        bar.addWidget(sidebar_btn)
        # Where you are lives in the window title; the toolbar keeps its room for actions.
        self.record_controls = RecordControls(self.audio)
        self.record_controls.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        bar.addWidget(self.record_controls)
        stamp = QPushButton(" Timestamp", icon=icons.icon("timer"))
        stamp.setToolTip("Insert [▶ HH:MM:SS] at the current recording/playback time (⌘T)")
        stamp.clicked.connect(self.notes.insert_timestamp)
        bar.addWidget(stamp)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        bar.addWidget(self._build_mode_switcher())
        gap = QWidget()
        gap.setFixedWidth(style.SPACE[3])
        bar.addWidget(gap)

        export = QPushButton(" Export for Claude", icon=icons.icon("upload", "white"))
        export.setObjectName("primaryButton")
        export.setToolTip("Write this subject as .md files for Claude, with the prompt on the clipboard (⌘E)")
        export.clicked.connect(self._export_for_claude)
        import_btn = QPushButton(" Import reply", icon=icons.icon("download"))
        import_btn.setToolTip("Bring Claude's reply back in")
        import_btn.clicked.connect(self._focus_import)
        self.lecture_buttons = (stamp,)
        self._shrinking_labels = ((stamp, stamp.text()), (import_btn, import_btn.text()))  # icon only when narrow
        self.subject_buttons = (export, import_btn)  # work with no lecture open
        for button in self.subject_buttons:
            bar.addWidget(button)
        bar.addWidget(self._toggle_button("inspector", "panel-right", "Show or hide the inspector"))

    def _build_mode_switcher(self) -> QWidget:
        """Lecture · Read · Review · Exam, ⌘1–⌘4; also in the View menu."""
        self.mode_group = QActionGroup(self, exclusive=True)
        self.mode_actions: dict[str, QAction] = {}
        self.mode_buttons: list[QToolButton] = []
        switcher = QWidget(objectName="modeSwitcher")
        row = QHBoxLayout(switcher)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(style.SPACE[0] // 2)
        for number, (key, mode) in enumerate(MODES.items(), start=1):
            action = QAction(mode.title, self.mode_group, checkable=True)
            action.setShortcut(QKeySequence(f"Ctrl+{number}"))
            action.setIcon(icons.icon(mode.icon, on_color=style.tokens().accent))
            action.setIconVisibleInMenu(False)
            shortcut = action.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
            action.setToolTip(f"{mode.title}: {mode.hint} ({shortcut})")
            action.triggered.connect(lambda _=False, k=key: self._set_mode(k))
            self.mode_actions[key] = action
            button = QToolButton(objectName="segment")
            button.setDefaultAction(action)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            row.addWidget(button)
            self.mode_buttons.append(button)
        self.mode_actions[self.workbench.mode].setChecked(True)
        self.workbench.modeChanged.connect(self._on_mode_changed)
        return switcher

    def _set_mode(self, key: str) -> None:
        ready, self._ready = self._ready, False  # parts a mode hides are not "closed by you"
        self.workbench.set_mode(key)
        self._ready = ready

    def _on_mode_changed(self, key: str, fresh: bool) -> None:
        self.mode_actions[key].setChecked(True)
        if fresh and key == "exam":  # straight to practising; later visits keep the tab you left
            self.ai.tabs.setCurrentWidget(self.ai.practice)

    def _reset_mode(self) -> None:
        ready, self._ready = self._ready, False
        self.workbench.reset_mode()
        self._ready = ready

    def resizeEvent(self, event) -> None:
        # Mode labels need room; at half a screen the icons (with tooltips) stay.
        compact = event.size().width() < COMPACT_TOOLBAR_BELOW
        mode = Qt.ToolButtonStyle.ToolButtonIconOnly if compact else Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        for button in getattr(self, "mode_buttons", ()):
            button.setToolButtonStyle(mode)
        for button, label in getattr(self, "_shrinking_labels", ()):
            button.setText("" if compact else label)
        super().resizeEvent(event)

    def _toggle_button(self, part: str, icon: str, tip: str) -> QToolButton:
        button = QToolButton(objectName="chromeToggle")
        button.setDefaultAction(self.part_actions[part])
        button.setIcon(icons.icon(icon))
        shortcut = self.part_actions[part].shortcut().toString(QKeySequence.SequenceFormat.NativeText)
        button.setToolTip(f"{tip} ({shortcut})")
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        return button

    def _apply_glass(self) -> None:
        """Glass on the chrome; everything you read stays solid."""
        glass.apply(self.sidebar, glass.Role.SIDEBAR)
        glass.apply(self.toolbar, glass.Role.TOOLBAR)
        glass.apply(self.statusBar(), glass.Role.STATUS)
        glass.apply(self.workbench.inspector.header, glass.Role.HEADER)
        for widget in (self.center, self.documents, self.workbench.inspector.stack):
            glass.solid(widget)

    def _on_part_toggled(self, part: str, visible: bool) -> None:
        action = self.part_actions[part]
        if action.isChecked() != visible:
            action.blockSignals(True)  # the workbench already changed; just mirror it
            action.setChecked(visible)
            action.blockSignals(False)
        if not self._ready:  # restoring the saved layout also toggles parts
            return
        if visible and part == "notes":
            self.notes.edit.setFocus()
        elif not visible and not self.workbench.wanted[part]:  # not when it only stepped aside for room
            shortcut = action.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
            self.statusBar().showMessage(f"“{action.text()}” hidden — bring it back from View or with {shortcut}.", 10000)

    def _default_layout(self) -> None:
        ready, self._ready = self._ready, False  # no "hidden" hints while rearranging
        self.workbench.reset()
        self.mode_actions[self.workbench.mode].setChecked(True)
        self.inspector_left_action.setChecked(False)
        self.stacked_action.setChecked(False)
        self._ready = ready

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
        add(file_menu, "Import Document (PDF)…", self._import_pdf, "Ctrl+Shift+I")
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
        add(ai_menu, "Import Claude's Reply", self._focus_import)

        tools = bar.addMenu("&Tools")
        add(tools, "Rebuild Search Index (Subject)", self._rebuild_index)
        add(tools, "Check Transcription Setup", self._check_whisper)

        view = bar.addMenu("&View")
        for action in self.part_actions.values():
            view.addAction(action)
        view.addSeparator()
        for key in self.workbench.inspector.keys:
            add(view, f"Show {self.workbench.inspector.titles[key]}", lambda _=False, k=key: self.workbench.show_pane(k))
        view.addSeparator()
        for action in self.mode_actions.values():
            view.addAction(action)
        add(view, "Reset This Mode", self._reset_mode)
        view.addSeparator()
        self.inspector_left_action = QAction("Inspector on the Left", self, checkable=True)
        self.inspector_left_action.toggled.connect(self.workbench.set_inspector_left)
        self.stacked_action = QAction("Slides Below Notes", self, checkable=True)
        self.stacked_action.toggled.connect(self.workbench.set_stacked)
        view.addAction(self.inspector_left_action)
        view.addAction(self.stacked_action)
        # The same choice, where you'd look for it: right-click the inspector's header.
        header = self.workbench.inspector.header
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        header.addAction(self.inspector_left_action)
        view.addSeparator()
        add(view, "Reset All Modes", self._default_layout)

    def _connect(self) -> None:
        self.sidebar.lectureSelected.connect(self.open_lecture)
        self.sidebar.subjectSelected.connect(self._open_subject)
        self.sidebar.assignProfessorRequested.connect(self._assign_professor)
        self.sidebar.aboutToChange.connect(self._release_files)
        self.sidebar.lectureDeleted.connect(self._lecture_deleted)
        self.sidebar.lectureRenamed.connect(self._lecture_renamed)
        self.sidebar.subjectChanged.connect(lambda _s: self.lecture and self.lecture_view.show_lecture(self.lecture))
        self.sidebar.examDateRequested.connect(self._edit_exam)

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
            lambda: self.statusBar().showMessage("The microphone is silent — click the warning in the toolbar for help.")
        )
        self.audio.error.connect(lambda msg: QMessageBox.warning(self, "Audio", msg))

        self.notes.seekRequested.connect(self._seek_from_notes)
        self.notes.anchorActivated.connect(self._jump_to_anchor)
        self.transcript.seekRequested.connect(self._seek_audio)
        self.transcript.labelsChanged.connect(self.schedule_index)
        self.transcript.classifyRequested.connect(self.classify)
        self.transcript.status.connect(lambda text: self.statusBar().showMessage(text, 8000))
        self.documents.pdfImported.connect(self._on_pdf_imported)
        self.documents.viewer.pageChanged.connect(self._on_page_turned)

        self.ai.searchRequested.connect(self.search)
        self.ai.hitActivated.connect(self._open_hit)
        self.ai.sourceActivated.connect(self._jump_to_source)
        self.ai.status.connect(lambda text: self.statusBar().showMessage(text, 8000))
        self.ai.replyImported.connect(self._on_reply_imported)
        self.ai.conversionsImported.connect(self._reindex_subject)

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
        self.documents.set_scope(self.subject, lecture)
        self.audio.set_lecture(lecture)
        self.transcript.set_lecture(lecture)
        self.ai.set_scope(self.subject, lecture)
        for button in self.lecture_buttons:
            button.setEnabled(lecture is not None)
        for button in self.subject_buttons:
            button.setEnabled(self.subject is not None)
        where = f"{lecture.subject.name} › {lecture.name}" if lecture else (self.subject.name if self.subject else "")
        self.setWindowTitle(f"UniVerse — {where}" if where else "UniVerse")

    def _open_subject(self, subject: Subject) -> None:
        if self.audio.recording and self.lecture is not None:
            # The overview page would take the notes away in the middle of a recording.
            self.statusBar().showMessage("Still recording — your notes stay open. Pick a lecture to switch.", 6000)
            return
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

    def _import_folder(self, kind: str, name_hint: str) -> Path | None:
        """Where a PDF of this type goes: slides to the open lecture (a new one if none
        is open), past exams and tasks to the whole subject."""
        if kind == "slides":
            lecture = self.ensure_lecture(name_hint)
            return lecture.dir("slides") if lecture else None
        subject = self.subject or self.sidebar.current_subject()
        if subject is None:
            QMessageBox.information(self, "Import", "Pick or create a subject in Subjects first.")
            return None
        return subject.dir(kind)

    def eventFilter(self, obj, event) -> bool:
        if event.type() not in DRAG_EVENTS or not isinstance(obj, QWidget):
            return False
        window = obj.window()
        if window is not self:  # dialogs of their own
            return False
        paths = dropped_files(event.mimeData())
        if not paths:
            return False  # e.g. recordings dragged onto the sidebar
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        if event.type() == QEvent.Type.DragEnter:
            where = f"“{self.lecture.name}”" if self.lecture else "a new lecture"
            self.statusBar().showMessage(f"Drop to add to {where} — PDFs are sorted into slides, past exams or tasks.", 3000)
        elif event.type() == QEvent.Type.Drop:
            # Not inside the drop: dialogs would block Finder, which the file came from.
            QTimer.singleShot(0, lambda: self.import_dropped(paths))
        return True

    def import_dropped(self, paths: list[Path]) -> None:
        """Sort dropped files into the open lecture (a new one if none is open):
        audio to its recordings, PDFs as slides, past exams or tasks by their name."""
        audio = [p for p in paths if p.suffix.lower() in AUDIO_SUFFIXES]
        pdfs: dict[str, list[Path]] = {}
        for path in paths:
            if path.suffix.lower() == ".pdf":
                pdfs.setdefault(guess_kind(path), []).append(path)
        lecture = self.ensure_lecture(paths[0].stem) if audio or "slides" in pdfs else self.lecture
        if (audio or "slides" in pdfs) and lecture is None:
            return
        added = []  # (what, where)
        if audio:
            self.audio.import_files(audio)
            self.workbench.set_shown("inspector", True)
            added.append((f"{len(audio)} recording{'s' * (len(audio) != 1)}", f"{lecture.subject.name} › {lecture.name}"))
        for kind, files in pdfs.items():
            self.documents.import_paths(files, kind)
            if kind == "slides":
                where = f"{lecture.subject.name} › {lecture.name}"
            else:  # past exams and tasks belong to the whole subject
                subject = self.subject or self.sidebar.current_subject()
                if subject is None:
                    continue  # import_paths has said to pick a subject
                where = subject.name
            added.append((f"{len(files)} × {DOC_TYPES[kind]}", where))
        if pdfs:
            self.workbench.set_shown("documents", True)
        elif audio:
            self.workbench.show_pane("recordings")
        if added:
            self.statusBar().showMessage("Added " + "; ".join(f"{what} to {where}" for what, where in added) + ".", 8000)

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
        transcribed = sum((lecture.dir("transcripts") / f"{p.stem}.json").exists() for p in paths)
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
            document, shown = self.documents.current_document(), self.documents.viewer.current()
            if document and shown:
                self.audio.start_slide_marks(document.folder, document.path.name, shown[1])

    def _on_recording_finished(self, path: Path) -> None:
        self.statusBar().showMessage(f"Saved {path.name}.", 6000)
        self.recordings.refresh()

    # --- audio / notes ------------------------------------------------------

    def _seek_from_notes(self, seconds: int) -> None:
        if self.audio.player.path is None:
            self.statusBar().showMessage("No audio in this lecture yet.", 4000)
            return
        self.audio.seek(seconds)

    def _note_context(self) -> dict:
        """What a paragraph started now should remember: audio position and the PDF page in view."""
        context = {}
        position = self.audio.audio_position()
        if position is not None:
            context.update(audio=position[0].stem, t=round(position[1], 1), clock=f"{clock_time(*position):%H:%M}")
        viewer, document = self.documents.viewer, self.documents.current_document()
        if document and viewer.current() and viewer.isVisible() and not viewer.visibleRegion().isEmpty():
            types = document.types
            context.update(doc=document.folder, pdf=document.path.name, page=viewer.current()[1], type=types[0] if types else "")
        return context

    def _on_page_turned(self, page: int) -> None:
        document = self.documents.current_document()
        if document is not None:
            self.audio.mark_slide(document.folder, document.path.name, page)

    def _add_figure(self, data: dict) -> None:
        """A snip from the PDF viewer: store it with the lecture and put it into the notes."""
        document = self.documents.current_document()
        lecture = self.ensure_lecture(Path(data["pdf"]).stem)
        if lecture is None:
            return
        prefix = "" if document.unit == "slide" else f"{document.type_label}: "
        caption = f"{prefix}{data['pdf']}, {document.unit} {data['page']}"
        source = {k: data[k] for k in ("pdf", "page", "text", "tables")} | {"doc": document.folder, "caption": caption}
        png = lecture.add_figure(data["png"], source)
        self.notes_action.setChecked(True)
        self.notes.insert_figure(png, caption, data["tables"])
        found = f" with {len(data['tables'])} table(s) as text" if data["tables"] else ""
        self.statusBar().showMessage(f"Added figure from {caption} to your notes{found}.", 6000)

    def _link_page(self, target: dict) -> None:
        document = self.documents.current_document()
        if self.ensure_lecture(Path(target["pdf"]).stem) is not None:
            self.notes_action.setChecked(True)
            types = document.types
            self.notes.insert_slide_link({"doc": document.folder, "type": types[0] if types else "", **target})

    def _jump_to_anchor(self, context: dict) -> None:
        if "t" in context:
            if context.get("audio"):
                self._seek_audio(context["audio"], context["t"])
            else:
                self._seek_from_notes(int(context["t"]))
        if context.get("doc") and context.get("pdf"):
            self.workbench.set_shown("documents", True)
            if not self.documents.open_page(context["doc"], context["pdf"], context.get("page", 1)):
                self.statusBar().showMessage(f"“{context['pdf']}” is no longer there.", 4000)

    def _seek_audio(self, stem: str, seconds: float) -> None:
        if not self.audio.select_stem(stem):
            self.statusBar().showMessage(f"Audio file for '{stem}' not found.", 4000)
            return
        self.audio.seek(seconds)

    def _still_recording(self, target: Lecture) -> bool:
        """A jump must not take the notes away in the middle of a recording."""
        if not self.audio.recording or self._is_open(target):
            return False
        self.statusBar().showMessage(f"Still recording: this is in “{target.name}”. Your open lecture stays; look it up after you stop.", 8000)
        return True

    def _jump_to_source(self, source: dict) -> None:
        """A stored source, from a guide, a question or the documents list, to the page or moment it means."""
        subject = self.subject
        if subject is None:
            return
        if source.get("kind") in ("own", "prof"):
            self.statusBar().showMessage(f"{source.get('label')} is not in your material: nothing to open.", 6000)
            return
        view = load_subject(subject, self.professors, pages=False)
        target = locate(view, source)
        if target is None and source.get("kind") == "figure":
            lecture = view.lecture(source.get("lecture", ""))
            target = {"lecture": lecture.lecture} if lecture else None
        if target is None:
            self.statusBar().showMessage(f"{source.get('label') or 'That source'} is no longer there.", 6000)
            return
        if "pdf" in target:
            document = view.document(source.get("doc", ""))
            listed = {d.path: d for d in self.documents.documents}
            here = next((listed[o.path] for o in document.occurrences if o.path in listed), None) if document else None
            if here is not None:  # also a deck shared with the open lecture: no reason to leave it
                self._open_document_page(here.folder, here.path.name, target["page"], source.get("label", ""))
                return
        lecture = target.get("lecture")
        if lecture is not None and not self._is_open(lecture):
            if self._still_recording(lecture):
                return
            self.sidebar.refresh(select=lecture)  # opens it via lectureSelected
        if "audio" in target:
            self._seek_audio(target["audio"], target["t"])
        elif "pdf" in target:
            self._open_document_page(target["doc"], target["pdf"], target["page"], source.get("label", ""))
        else:
            self.notes_action.setChecked(True)

    def _open_document_page(self, folder: str, pdf: str, page: int, label: str = "") -> None:
        self.workbench.set_shown("documents", True)
        if not self.documents.open_page(folder, pdf, page):
            self.statusBar().showMessage(f"{label or pdf} is not in this lecture's documents.", 6000)

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
            folder = lecture.dir("transcripts")
            old = folder / f"{Path(result.audio).stem}.json"
            if old.exists():  # transcribing again must not lose the recording's id or labels set by hand
                try:
                    result.carry_over(transcriber.Transcript.load(old))
                except (OSError, ValueError, TypeError) as exc:
                    print(f"Could not read the old transcript {old.name}: {exc}")
            result.save(folder)
            return result

        def done(result):
            self.statusBar().showMessage(f"Transcribed {audio_path.name}: {len(result.segments)} segments.", 8000)
            self.ai.refresh()
            if self._is_open(lecture):
                self.transcript.refresh(select_stem=audio_path.stem)
                self.recordings.refresh()
                self.workbench.inspector.show_pane("transcript")
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
                if found_speakers:
                    recognised, transcript = transcript, transcriber.Transcript.load(path)
                    transcript.take_speakers(recognised)
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
                f"Sorted: {counts['exam']} exam-relevant · {counts['question']} questions · "
                f"{counts['off_topic']} private (left out of export & search).",
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

    def _on_reply_imported(self) -> None:
        """Claude's labels may reach any lecture of the subject."""
        if self.lecture is not None:
            self.transcript.refresh()
        self._reindex_subject()
        self._refresh_welcome()

    def _on_pdf_imported(self, _path: Path) -> None:
        if self.lecture is not None:
            self.schedule_index(self.lecture)
        subject = self.subject
        if subject is not None:  # past exams and tasks belong to no lecture
            self._run_index(lambda: SubjectIndex(subject, self._embedder()).index_documents())
        self.ai.refresh()

    def _reindex_subject(self) -> None:
        subject = self.subject
        if subject is not None:
            self._run_index(lambda: SubjectIndex(subject, self._embedder()).index_all())

    def _refresh_welcome(self) -> None:
        if self.lecture is None:
            self.welcome.show_subject(self.subject)

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
        wanted = subject.path

        def done(hits) -> None:
            if self.subject is None or self.subject.path != wanted:
                return
            self.ai.show_results(hits)

        self._start(
            Worker(lambda: SubjectIndex(subject, self._embedder()).search(query, limit=12, lecture=lecture_name)),
            done,
            lambda msg: QMessageBox.warning(self, "Search", msg),
            pool=workers.index_pool,
        )

    def _open_hit(self, hit) -> None:
        if hit is None or self.subject is None:
            return
        if hit.lecture and (self.lecture is None or hit.lecture != self.lecture.name):  # past exams and tasks have none
            lecture = self.subject.lecture(hit.lecture)
            if lecture is not None and self._still_recording(lecture):
                return
            self.sidebar.select_by_name(self.subject.name, hit.lecture)
        target = hit_target(hit)
        if target is None:
            return
        kind, name, value = target
        if kind == "audio":
            self._seek_audio(name, value)
        else:
            self.documents.open_page(kind, name, int(value))
            self.workbench.set_shown("documents", True)

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
        self.ai.refresh()
        if self.lecture is not None:
            self.lecture_view.show_lecture(self.lecture)

    # --- misc ---------------------------------------------------------------

    def _import_pdf(self) -> None:
        self.workbench.set_shown("documents", True)
        self.documents.import_pdf()

    def _export_for_claude(self) -> None:
        """With the choices on the Claude tab, whatever they are right now."""
        if self.subject is None:
            self.statusBar().showMessage("Pick a subject first.", 4000)
            return
        self.workbench.show_pane("ai")
        self.ai.show_export()
        self.notes.save()
        self.ai.claude.export()

    def _focus_import(self) -> None:
        if self.subject is None:
            self.statusBar().showMessage("Pick a subject first.", 4000)
            return
        self.workbench.show_pane("ai")
        self.ai.show_import()

    def _edit_exam(self, subject: Subject) -> None:
        ExamDialog(subject, self._events, self).exec()
        self.ai.refresh()
        self._refresh_welcome()

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
            self.ai.claude.show_api_mode()
            if not self.settings.moodle_calendar_url:
                self._set_events([])
            self.refresh_moodle()

    def _set_events(self, events: list) -> None:
        self._events = events
        self.welcome.set_events(events)
        self.ai.set_events(events)

    def refresh_moodle(self) -> None:
        url = self.settings.moodle_calendar_url
        if not url:
            return

        def failed(message: str) -> None:
            self.statusBar().showMessage(f"Moodle calendar not updated: {message}", 8000)

        self._start(
            Worker(lambda: moodle_ical.parse(moodle_ical.fetch(url))),
            on_done=self._set_events,
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
        if self.settings.layout:
            self.workbench.restore(self.settings.layout)
        self.inspector_left_action.setChecked(self.workbench.inspector_left())
        self.stacked_action.setChecked(self.workbench.stacked())

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
        self.settings.layout = self.workbench.state()
        self.settings.last_subject = self.subject.name if self.subject else ""
        self.settings.last_lecture = self.lecture.name if self.lecture else ""
        self.settings.save()
        study_pool.waitForDone(5000)
        workers.index_pool.waitForDone(5000)
        poll_pool.waitForDone(5000)
        super().closeEvent(event)
