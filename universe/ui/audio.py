"""Audio: one controller (recorder + player state) and the widgets that show it.

* :class:`RecordControls` — the big Record button, timer, level meter and mic
  picker in the main toolbar. Always usable; the main window creates a lecture
  on the fly if none is open.
* :class:`PlayerBar` — playback under the notes, so timestamps and audio sit
  together.
* :class:`RecordingsPanel` — the "Recordings" dock: file list, import,
  transcription, auto-transcribe.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import QMimeData, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..config import LANGUAGES, Settings, installed_models
from ..services.audio_player import AudioPlayer, duration, needs_playback_copy
from ..services.audio_recorder import AudioRecorder
from ..services import live_check
from ..services.live_check import LiveMonitor, LiveStatus
from ..services.timestamps import format_seconds
from ..storage import Lecture

MIC_PRIVACY_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
SILENCE_AFTER_S = 4.0
ROLE = Qt.ItemDataRole.UserRole


def open_mic_privacy_settings() -> None:
    subprocess.run(["open", MIC_PRIVACY_URL])


class AudioController(QObject):
    filesChanged = Signal()
    currentChanged = Signal()
    recordingChanged = Signal(bool)
    recordingFinished = Signal(object)  # Path
    transcribeRequested = Signal(object)  # Path
    silenceDetected = Signal()
    error = Signal(str)
    tick = Signal()

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.recorder = AudioRecorder(sample_rate=settings.sample_rate)
        self.player = AudioPlayer()
        self.lecture: Lecture | None = None
        self.recording_lecture: Lecture | None = None
        self.live: LiveMonitor | None = None
        self._silence_warned = False

        self.timer = QTimer(self, interval=100)
        self.timer.timeout.connect(self._on_tick)
        self.timer.start()

    # --- lecture / files ----------------------------------------------------

    def set_lecture(self, lecture: Lecture | None) -> None:
        self.player.stop()
        self.lecture = lecture
        files = self.files()
        self._load(files[-1] if files else None)  # newest recording names sort last
        self.filesChanged.emit()

    def files(self) -> list[Path]:
        return self.lecture.audio_files() if self.lecture and self.lecture.path.exists() else []

    @property
    def current(self) -> Path | None:
        return self.player.path

    def select(self, path: Path | None) -> None:
        if path != self.player.path:
            self._load(path)

    def select_stem(self, stem: str) -> bool:
        for path in self.files():
            if path.stem == stem:
                self.select(path)
                return True
        return False

    def _load(self, path: Path | None) -> None:
        if path is None or not AudioPlayer.available():
            if AudioPlayer.available():
                self.player.unload()
        else:
            # .m4a and similar are converted once for playback (a few seconds per hour of audio)
            busy = needs_playback_copy(path)
            if busy:
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self.player.load(path)
            except Exception as exc:
                self.error.emit(f"Cannot play {path.name}: {exc}")
                self.player.unload()
            finally:
                if busy:
                    QApplication.restoreOverrideCursor()
        self.currentChanged.emit()

    def import_files(self, paths: list[Path]) -> None:
        if self.lecture is None or not paths:
            return
        last = None
        for path in paths:
            last = self.lecture.import_file(path, "audio")
        self.filesChanged.emit()
        self.select(last)

    def release(self) -> None:
        """Let go of open files (before the sidebar renames/deletes folders)."""
        self.player.unload()
        self.currentChanged.emit()

    # --- recording ----------------------------------------------------------

    @property
    def recording(self) -> bool:
        return self.recorder.recording

    def start_recording(self, lecture: Lecture) -> bool:
        self.player.pause()
        path = lecture.new_recording_path()
        try:
            self.recorder.start(path, self.settings.input_device)
        except Exception as exc:
            self.error.emit(f"Could not start recording: {exc}")
            return False
        self.recording_lecture = lecture
        self._silence_warned = False
        language = lecture.subject.meta.get("language") or self.settings.whisper_language
        self.live = LiveMonitor(self.settings, language=language)
        self.recorder.monitor = self.live
        self.live.start()
        self.recordingChanged.emit(True)
        return True

    def stop_recording(self) -> None:
        if not self.recorder.recording:
            return
        if self.live is not None:
            self.live.stop()
        self.recorder.monitor = self.live = None  # detach first: the audio callback stays minimal while stopping
        path = self.recorder.stop()
        lecture, self.recording_lecture = self.recording_lecture, None
        self.recordingChanged.emit(False)
        if path is None or not path.exists():
            return
        if lecture is not None and self.lecture is not None and lecture.path == self.lecture.path:
            self.filesChanged.emit()
            self.select(path)
        self.recordingFinished.emit(path)
        if self.settings.auto_transcribe:
            self.transcribeRequested.emit(path)

    def toggle_pause(self) -> None:
        if self.recorder.paused:
            self.recorder.resume()
        else:
            self.recorder.pause()

    def set_device(self, name: str) -> None:
        self.settings.input_device = name
        self.settings.save()

    def time_source(self) -> float | None:
        """Position for note timestamps: live recording of this lecture first, then playback."""
        if self.recording and self.recording_lecture and self.lecture and self.recording_lecture.path == self.lecture.path:
            return self.recorder.elapsed()
        if self.player.path is not None:
            return self.player.position()
        return None

    # --- playback -----------------------------------------------------------

    def toggle_play(self) -> None:
        if self.player.path is not None:
            self.player.toggle()

    def seek(self, seconds: float, play: bool = True) -> None:
        if self.player.path is None:
            return
        if play:
            self.player.play(max(0.0, seconds))
        else:
            self.player.seek(seconds)

    def _on_tick(self) -> None:
        if (
            self.recorder.recording
            and not self._silence_warned
            and not self.recorder.paused
            and self.recorder.elapsed() > SILENCE_AFTER_S
            and self.recorder.peak_level < 0.01
        ):
            self._silence_warned = True
            self.silenceDetected.emit()
        self.tick.emit()

    def shutdown(self) -> None:
        self.stop_recording()
        self.recorder.wait_closed()
        self.player.stop()


class RecordControls(QWidget):
    """Toolbar cluster: ● Record / ■ Stop, pause, timer, level meter, mic menu."""

    recordClicked = Signal()

    def __init__(self, audio: AudioController, parent=None):
        super().__init__(parent)
        self.audio = audio

        self.record_btn = QPushButton("●  Record")
        self.record_btn.setObjectName("recordButton")
        self.record_btn.setToolTip("Start / stop recording (⌘R)")
        self.record_btn.clicked.connect(self.recordClicked)
        self.pause_btn = QToolButton(text="❚❚")
        self.pause_btn.setToolTip("Pause / resume recording")
        self.pause_btn.clicked.connect(audio.toggle_pause)
        self.time = QLabel("00:00:00")
        self.time.setObjectName("recordTime")
        self.level = QProgressBar(maximum=100, textVisible=False)
        self.level.setObjectName("levelMeter")
        self.level.setFixedSize(90, 8)

        self.mic_btn = QToolButton(text="🎙")
        self.mic_btn.setObjectName("micButton")
        self.mic_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.mic_menu = QMenu(self)
        self.mic_menu.aboutToShow.connect(self._fill_mic_menu)
        self.mic_btn.setMenu(self.mic_menu)

        self.live = QToolButton()
        self.live.setObjectName("liveCheck")
        self.live.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.live.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.live_menu = QMenu(self)
        self.live_menu.aboutToShow.connect(self._fill_live_menu)
        self.live.setMenu(self.live_menu)

        self.warning = QPushButton("⚠ No sound — fix microphone")
        self.warning.setObjectName("warningButton")
        self.warning.clicked.connect(self._show_mic_help)
        self.warning.hide()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.record_btn.setFixedWidth(118)
        for w in (self.record_btn, self.pause_btn, self.time, self.level, self.live, self.mic_btn, self.warning):
            layout.addWidget(w)
        layout.addStretch()

        audio.recordingChanged.connect(self._on_recording_changed)
        audio.silenceDetected.connect(self.warning.show)
        audio.tick.connect(self._on_tick)
        self._on_recording_changed(False)
        if not AudioRecorder.available():
            self.record_btn.setEnabled(False)
            self.record_btn.setToolTip("Recording unavailable: sounddevice/PortAudio could not be loaded.")
        self._update_mic_tooltip()

    def _on_recording_changed(self, recording: bool) -> None:
        self.record_btn.setText("■  Stop" if recording else "●  Record")
        self.record_btn.setProperty("recording", recording)
        self.record_btn.style().unpolish(self.record_btn)
        self.record_btn.style().polish(self.record_btn)
        self.pause_btn.setVisible(recording)
        self.time.setVisible(recording)
        self.level.setVisible(recording)
        self.live.setVisible(recording)
        self.live.setText(LIVE_ICONS["listening"] + " Listening…")
        if not recording:
            self.warning.hide()
            self.level.setValue(0)

    def _on_tick(self) -> None:
        rec = self.audio.recorder
        if rec.recording:
            self.time.setText(format_seconds(rec.elapsed()) + ("  paused" if rec.paused else ""))
            self.level.setValue(int(rec.level * 100))
            self.pause_btn.setText("▶" if rec.paused else "❚❚")
            if self.audio.live is not None and not rec.paused:
                status = self.audio.live.status
                self.live.setText(live_summary(status, rec.elapsed()))
                self.live.setToolTip(live_details(status, rec.elapsed()) + "\n\nClick for what the computer heard.")

    def _fill_live_menu(self) -> None:
        self.live_menu.clear()
        live = self.audio.live
        if live is None:
            return
        status = live.status
        for line in live_details(status, self.audio.recorder.elapsed()).splitlines():
            self.live_menu.addAction(line).setEnabled(False)
        if status.history:
            self.live_menu.addSeparator()
            self.live_menu.addAction("What the computer heard (compare with your ears):").setEnabled(False)
            for heard in reversed(status.history[-8:]):
                text = _clip(heard.text) or "(nothing recognised)"
                if heard.text and heard.state in ("silence", "listening"):
                    text += "  — no clear speech then, probably made up"
                icon = LIVE_ICONS.get(heard.state, "⚪")
                self.live_menu.addAction(f"{icon} {format_seconds(heard.at)}  {text}").setEnabled(False)
        elif not self.audio.settings.live_check:
            self.live_menu.addSeparator()
            self.live_menu.addAction("Live transcription check is off (Settings).").setEnabled(False)

    def _fill_mic_menu(self) -> None:
        self.mic_menu.clear()
        current = self.audio.settings.input_device
        names = self.audio.recorder.input_devices(refresh=True)
        group = QActionGroup(self.mic_menu)
        default = QAction(f"System default ({AudioRecorder.default_input_name() or 'none'})", self.mic_menu, checkable=True)
        default.setChecked(current == "" or current not in names)
        default.triggered.connect(lambda: self._choose(""))
        group.addAction(default)
        self.mic_menu.addAction(default)
        self.mic_menu.addSeparator()
        for name in names:
            action = QAction(name, self.mic_menu, checkable=True)
            action.setChecked(name == current)
            action.triggered.connect(lambda _=False, n=name: self._choose(n))
            group.addAction(action)
            self.mic_menu.addAction(action)
        self.mic_menu.addSeparator()
        self.mic_menu.addAction("Microphone permissions…", open_mic_privacy_settings)
        if self.audio.recording:  # can't switch mid-recording
            for action in group.actions():
                action.setEnabled(False)

    def _choose(self, name: str) -> None:
        self.audio.set_device(name)
        self._update_mic_tooltip()

    def _update_mic_tooltip(self) -> None:
        name = self.audio.settings.input_device or f"System default ({AudioRecorder.default_input_name() or 'none'})"
        self.mic_btn.setToolTip(f"Microphone: {name}")

    def _show_mic_help(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("No sound is being recorded")
        box.setText("UniVerse is recording, but the microphone delivers silence.")
        box.setInformativeText(
            "• Pick another microphone from the 🎙 menu (an iPhone or headset mic may be selected but not nearby).\n"
            "• Or allow microphone access for the app you launched UniVerse from (Terminal, iTerm, VS Code, Claude) "
            "in System Settings → Privacy & Security → Microphone, then restart UniVerse."
        )
        open_btn = box.addButton("Open Privacy Settings", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is open_btn:
            open_mic_privacy_settings()


class PlayerBar(QWidget):
    """Playback strip under the notes: ▶, ±10 s, slider, time, file picker."""

    def __init__(self, audio: AudioController, parent=None):
        super().__init__(parent)
        self.setObjectName("playerBar")
        self.audio = audio
        self._dragging = False

        self.play_btn = QToolButton(text="▶")
        self.play_btn.setObjectName("playButton")
        self.play_btn.setToolTip("Play / pause (⌘⇧Space)")
        self.play_btn.clicked.connect(audio.toggle_play)
        back = QToolButton(text="↺ 10")
        back.setToolTip("Back 10 seconds")
        back.clicked.connect(lambda: audio.seek(audio.player.position() - 10, play=audio.player.playing))
        fwd = QToolButton(text="10 ↻")
        fwd.setToolTip("Forward 10 seconds")
        fwd.clicked.connect(lambda: audio.seek(audio.player.position() + 10, play=audio.player.playing))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.sliderPressed.connect(lambda: setattr(self, "_dragging", True))
        self.slider.sliderReleased.connect(self._released)
        self.time = QLabel("0:00 / 0:00")
        self.time.setObjectName("muted")
        self.files = QComboBox()
        self.files.setMinimumContentsLength(14)
        self.files.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.files.activated.connect(lambda i: audio.select(Path(self.files.itemData(i))))
        self.empty = QLabel("No recordings yet — press ● Record in the toolbar, or import audio in the Recordings panel.")
        self.empty.setObjectName("muted")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        for w in (self.play_btn, back, fwd):
            layout.addWidget(w)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.time)
        layout.addWidget(self.files)
        layout.addWidget(self.empty, 1)
        self._controls = (self.play_btn, back, fwd, self.slider, self.time, self.files)

        audio.filesChanged.connect(self._refresh)
        audio.currentChanged.connect(self._refresh)
        audio.recordingChanged.connect(lambda _rec: self._refresh())
        audio.tick.connect(self._on_tick)
        self._refresh()

    def _refresh(self) -> None:
        files = self.audio.files()
        has = bool(files)
        for w in self._controls:
            w.setVisible(has)
        self.empty.setVisible(not has)
        self.empty.setText(
            "Recording… it will appear here when you press ■ Stop."
            if self.audio.recording
            else "No recordings yet — press ● Record in the toolbar, or import audio in the Recordings panel."
        )
        self.files.blockSignals(True)
        self.files.clear()
        for path in files:
            self.files.addItem(_short_name(path), str(path))
        if self.audio.current is not None:
            self.files.setCurrentIndex(max(0, self.files.findData(str(self.audio.current))))
        self.files.blockSignals(False)
        self.slider.setRange(0, int(self.audio.player.duration * 10))

    def _released(self) -> None:
        self._dragging = False
        self.audio.seek(self.slider.value() / 10, play=self.audio.player.playing)

    def _on_tick(self) -> None:
        player = self.audio.player
        if player.path is None:
            return
        pos = player.position()
        self.play_btn.setText("❚❚" if player.playing else "▶")
        self.time.setText(f"{_clock(pos)} / {_clock(player.duration)}")
        if not self._dragging:
            self.slider.setValue(int(pos * 10))


RECORDINGS_MIME = "application/x-universe-recordings"  # newline-separated paths


def recordings_from_mime(mime: QMimeData) -> list[Path]:
    if not mime.hasFormat(RECORDINGS_MIME):
        return []
    return [Path(p) for p in bytes(mime.data(RECORDINGS_MIME)).decode("utf-8").splitlines() if p]


class _RecordingList(QListWidget):
    """Recordings can be dragged onto a lecture in the sidebar to move them there.
    Only an app-internal format is offered, so dropping on Finder does nothing."""

    def __init__(self):
        super().__init__()
        self.setDragEnabled(True)
        self.setDragDropMode(QListWidget.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def mimeTypes(self) -> list[str]:
        return [RECORDINGS_MIME]

    def mimeData(self, items) -> QMimeData:
        mime = QMimeData()
        mime.setData(RECORDINGS_MIME, "\n".join(str(item.data(ROLE)) for item in items).encode("utf-8"))
        return mime

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction


class RecordingsPanel(QWidget):
    """Dock: list of the lecture's audio, import, delete, transcription settings."""

    deleteRequested = Signal(list)  # list[Path] the user chose to delete
    moveRequested = Signal(list, object)  # list[Path], target Lecture

    def __init__(self, audio: AudioController, ensure_lecture, lectures=None, parent=None):
        """``ensure_lecture(name_hint)`` returns the open lecture or creates one;
        ``lectures()`` lists all lectures, for the "Move to" menu."""
        super().__init__(parent)
        self.audio = audio
        self.ensure_lecture = ensure_lecture
        self.lectures = lectures or (lambda: [])
        self.list = _RecordingList()
        self.list.setToolTip("Drag recordings onto a lecture in the sidebar to move them there.")
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)  # ⌘/⇧-click to delete several
        self.list.itemClicked.connect(lambda item: audio.select(item.data(ROLE)))
        self.list.itemDoubleClicked.connect(lambda item: (audio.select(item.data(ROLE)), audio.seek(0)))
        self.list.itemSelectionChanged.connect(self._update_delete)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        for key in (QKeySequence.StandardKey.Delete, QKeySequence(Qt.Key.Key_Backspace)):
            shortcut = QShortcut(key, self.list, context=Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self._delete)

        import_btn = QPushButton("Import audio…")
        import_btn.clicked.connect(self._import)
        self.delete_btn = QPushButton("Delete…")
        self.delete_btn.setToolTip("Move the selected recordings and their transcripts to the Trash (⌫)")
        self.delete_btn.clicked.connect(self._delete)
        self.transcribe_btn = QPushButton("Transcribe")
        self.transcribe_btn.setObjectName("primaryButton")
        self.transcribe_btn.clicked.connect(self._transcribe)
        self.auto = QCheckBox("Transcribe automatically after recording")
        self.auto.setChecked(audio.settings.auto_transcribe)
        self.auto.toggled.connect(self._set_auto)
        self.progress = QProgressBar(maximum=100)
        self.progress.hide()

        self.language = QComboBox()
        for code, label in LANGUAGES:
            self.language.addItem(label, code)
        self.language.setToolTip(
            "Spoken language for this subject. Choose the main language for lectures that mix "
            "German and English — the other language's terms are still recognised."
        )
        self.language.activated.connect(self._set_language)
        self.model = QComboBox()
        self.model.setToolTip("Speech model (in ~/.universe_data/models). Models ending in .en only understand English.")
        self.model.activated.connect(self._set_model)
        self.hint = QLabel(wordWrap=True)
        self.hint.setObjectName("muted")

        buttons = QHBoxLayout()
        buttons.addWidget(import_btn)
        buttons.addWidget(self.delete_btn)
        buttons.addStretch()
        buttons.addWidget(self.transcribe_btn)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.progress)
        layout.addWidget(self.auto)
        form = QHBoxLayout()
        form.addWidget(QLabel("Language:"))
        form.addWidget(self.language, 1)
        layout.addLayout(form)
        form = QHBoxLayout()
        form.addWidget(QLabel("Model:"))
        form.addWidget(self.model, 1)
        layout.addLayout(form)
        layout.addWidget(self.hint)

        audio.filesChanged.connect(self.refresh)
        audio.currentChanged.connect(self._highlight)
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        lecture = self.audio.lecture
        transcribed = {p.stem for p in lecture.transcripts()} if lecture else set()
        for path in self.audio.files():
            mark = "✓ transcribed" if path.stem in transcribed else "not transcribed"
            item = QListWidgetItem(f"{_short_name(path)}\n{_clock(duration(path))} · {mark}")
            item.setData(ROLE, path)
            self.list.addItem(item)
        self.hint.setText("" if self.list.count() else "Recordings and imported audio for this lecture appear here.")
        self._fill_speech_settings()
        self._highlight()

    def _fill_speech_settings(self) -> None:
        lecture = self.audio.lecture
        code = (lecture.subject.meta.get("language") if lecture else "") or self.audio.settings.whisper_language
        self.language.setCurrentIndex(max(0, self.language.findData(code)))
        self.language.setEnabled(lecture is not None)
        self.model.clear()
        best = self.audio.settings.__class__().whisper_model_path().name  # what "automatic" resolves to
        self.model.addItem(f"Automatic — best installed ({best})", "")
        for name in installed_models():
            self.model.addItem(name, name)
        self.model.setCurrentIndex(max(0, self.model.findData(self.audio.settings.whisper_model)))

    def _set_language(self) -> None:
        if self.audio.lecture is not None:
            self.audio.lecture.subject.update_meta(language=self.language.currentData())

    def _set_model(self) -> None:
        self.audio.settings.whisper_model = self.model.currentData()
        self.audio.settings.save()

    def _highlight(self) -> None:
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.data(ROLE) == self.audio.current:
                self.list.setCurrentItem(item)
        self.transcribe_btn.setEnabled(self.audio.current is not None and self.progress.isHidden())
        self._update_delete()

    def selected_paths(self) -> list[Path]:
        return [item.data(ROLE) for item in self.list.selectedItems()]

    def _update_delete(self) -> None:
        self.delete_btn.setEnabled(bool(self.list.selectedItems()))

    def _context_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():
            self.list.setCurrentItem(item)
        menu = QMenu(self)
        menu.addAction("Transcribe", lambda: self.audio.transcribeRequested.emit(item.data(ROLE)))
        self._add_move_menu(menu)
        menu.addSeparator()
        count = len(self.list.selectedItems())
        menu.addAction("Move to Trash…" if count == 1 else f"Move {count} recordings to Trash…", self._delete)
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _add_move_menu(self, menu: QMenu) -> None:
        current = self.audio.lecture
        others = [lec for lec in self.lectures() if current is None or lec.path != current.path]
        if not others:
            return
        move = menu.addMenu("Move to Lecture")
        paths = self.selected_paths()
        subject = None
        for lecture in others:
            if subject is None or lecture.subject.path != subject.path:
                if subject is not None:
                    move.addSeparator()
                subject = lecture.subject
                move.addSection(subject.name)
            move.addAction(lecture.name, lambda lec=lecture: self.moveRequested.emit(paths, lec))

    def _delete(self) -> None:
        if self.list.selectedItems():
            self.deleteRequested.emit(self.selected_paths())

    def _import(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import audio", str(Path.home()), "Audio (*.wav *.mp3 *.m4a *.ogg *.flac)"
        )
        if paths and self.ensure_lecture(Path(paths[0]).stem) is not None:
            self.audio.import_files([Path(p) for p in paths])

    def _transcribe(self) -> None:
        if self.audio.current is not None:
            self.audio.transcribeRequested.emit(self.audio.current)

    def _set_auto(self, on: bool) -> None:
        self.audio.settings.auto_transcribe = on
        self.audio.settings.save()

    def set_transcribing(self, active: bool, percent: int = 0) -> None:
        self.progress.setVisible(active)
        self.progress.setValue(percent)
        self.progress.setFormat("Transcribing… %p%")
        self.transcribe_btn.setEnabled(not active and self.audio.current is not None)
        if not active:
            self.refresh()


LIVE_ICONS = {
    "listening": "⚪",
    "silence": "⚪",
    "no_lecturer": "⚪",
    "clear": "🟢",
    "okay": "🟡",
    "quiet": "🔴",
    "neighbours": "🟠",
}
HEARD_FRESH_S = 90  # older live checks no longer describe the room


def live_summary(status: LiveStatus, now: float) -> str:
    """Toolbar text, e.g. "🟢 Lecturer clear"."""
    return f"{LIVE_ICONS.get(status.state, '⚪')} {status.message}"


def live_details(status: LiveStatus, now: float) -> str:
    lines = [status.message]
    if status.snr_db is not None:
        who = "Lecturer" if status.voices else "Speech"
        lines.append(f"{who} {status.snr_db:.0f} dB above the room background (clear ≥ {live_check.CLEAR_DB:.0f} dB)")
    if status.neighbour_db is not None and status.voice_db is not None:
        lines.append(f"Other voices {status.neighbour_db - status.voice_db:+.0f} dB compared with the lecturer")
    if not status.voices:
        lines.append("Install the speaker model to tell the lecturer from neighbours (README → Speaker recognition).")
    heard = status.heard
    if heard is not None and now - heard.at <= HEARD_FRESH_S:
        made_up = " (no clear speech then, probably made up)" if heard.text and heard.state in ("silence", "listening") else ""
        lines.append(f"Heard {max(0, round(now - heard.at))} s ago: “{_clip(heard.text) or '…'}”{made_up}")
    return "\n".join(lines)


def _clip(text: str, limit: int = 110) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _short_name(path: Path) -> str:
    """Display name: the file name without extension, e.g. 'BWL 2026-10-28 15.30'."""
    return path.stem


def _clock(seconds: float) -> str:
    text = format_seconds(seconds)
    return text[3:] if text.startswith("00:") else text
