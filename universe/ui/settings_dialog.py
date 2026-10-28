"""Settings dialog (⌘,): everything in settings.json a user may want to change."""

from __future__ import annotations

import dataclasses
import subprocess

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import LANGUAGES, Settings, data_root, installed_models
from ..services import lmstudio, local_llm
from ..services.audio_recorder import AudioRecorder
from . import workers
from .workers import Worker


def find_models(settings: Settings) -> tuple[str, list[str]]:
    """(base url, chat model ids) of the local AI server ``settings`` points at."""
    url, model = local_llm.find_server(dataclasses.replace(settings, local_llm_model=""))
    return url, lmstudio.chat_models(lmstudio.server_root(url)) or [model]


def _form(*rows) -> QWidget:
    page = QWidget()
    form = QFormLayout(page)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)  # macOS default keeps fields narrow
    for label, widget in rows:
        if label is None:
            form.addRow(widget)
        else:
            form.addRow(label, widget)
    return page


def _hint(text: str) -> QLabel:
    label = QLabel(text, wordWrap=True)
    label.setObjectName("muted")
    return label


class SettingsDialog(QDialog):
    """Edits ``settings`` in place and saves it when the user presses OK."""

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self.settings = settings
        self._worker: Worker | None = None

        tabs = QTabWidget()
        tabs.addTab(self._transcription_tab(), "Transcription")
        tabs.addTab(self._recording_tab(), "Recording")
        tabs.addTab(self._local_ai_tab(), "Local AI")
        tabs.addTab(self._power_tab(), "Battery")
        tabs.addTab(self._general_tab(), "General")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # --- tabs ---------------------------------------------------------------

    def _transcription_tab(self) -> QWidget:
        s = self.settings
        self.whisper_binary = QLineEdit(s.whisper_binary)
        browse = QPushButton("Choose…", autoDefault=False)
        browse.clicked.connect(self._choose_binary)
        binary_row = QHBoxLayout()
        binary_row.addWidget(self.whisper_binary, 1)
        binary_row.addWidget(browse)
        binary = QWidget()
        binary.setLayout(binary_row)
        binary_row.setContentsMargins(0, 0, 0, 0)

        self.whisper_model = QComboBox()
        self.whisper_model.addItem(f"Automatic — best installed ({Settings().whisper_model_path().name})", "")
        for name in installed_models():
            self.whisper_model.addItem(name, name)
        if s.whisper_model and self.whisper_model.findData(s.whisper_model) < 0:
            self.whisper_model.addItem(s.whisper_model, s.whisper_model)  # absolute path or missing file
        self.whisper_model.setCurrentIndex(max(0, self.whisper_model.findData(s.whisper_model)))

        self.whisper_language = QComboBox()
        for code, label in LANGUAGES:
            self.whisper_language.addItem(label, code)
        self.whisper_language.setCurrentIndex(max(0, self.whisper_language.findData(s.whisper_language)))

        self.whisper_threads = QSpinBox(minimum=1, maximum=32, value=s.whisper_threads)
        self.auto_transcribe = QCheckBox("Transcribe automatically after recording")
        self.auto_transcribe.setChecked(s.auto_transcribe)
        return _form(
            ("whisper.cpp CLI", binary),
            ("Model", self.whisper_model),
            ("Default language", self.whisper_language),
            (None, _hint("Each subject can override the language in the Recordings panel.")),
            ("CPU threads", self.whisper_threads),
            (None, self.auto_transcribe),
        )

    def _recording_tab(self) -> QWidget:
        self.input_device = QComboBox()
        self.input_device.addItem(f"System default ({AudioRecorder.default_input_name() or 'none'})", "")
        for name in AudioRecorder().input_devices():  # no rescan: it would cut off a running recording
            self.input_device.addItem(name, name)
        current = self.settings.input_device
        if current and self.input_device.findData(current) < 0:
            self.input_device.addItem(f"{current} (not connected)", current)
        self.input_device.setCurrentIndex(max(0, self.input_device.findData(current)))
        return _form(
            ("Microphone", self.input_device),
            (None, _hint("Recordings are 16 kHz mono WAV, the format whisper.cpp expects.")),
        )

    def _local_ai_tab(self) -> QWidget:
        s = self.settings
        self.local_classify = QCheckBox("Sort new transcripts with a local AI model")
        self.local_classify.setChecked(s.local_classify)
        self.local_llm_url = QLineEdit(s.local_llm_url, placeholderText="Automatic — try ports 1234, 8080, 11434")
        self.local_llm_model = QComboBox(editable=True)
        self.local_llm_model.addItem("Automatic — first chat model", "")
        if s.local_llm_model:
            self.local_llm_model.addItem(s.local_llm_model, s.local_llm_model)
        self.local_llm_model.setCurrentIndex(self.local_llm_model.findData(s.local_llm_model))

        self.test_btn = QPushButton("Test connection", autoDefault=False)
        self.test_btn.clicked.connect(self._test_connection)
        self.test_result = _hint("")
        test_row = QHBoxLayout()
        test_row.setContentsMargins(0, 0, 0, 0)
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_result, 1)
        test = QWidget()
        test.setLayout(test_row)

        self.unload_after = QSpinBox(minimum=0, maximum=120, suffix=" min", specialValueText="Never")
        self.unload_after.setValue(s.local_llm_unload_after // 60)
        return _form(
            (None, self.local_classify),
            ("Server URL", self.local_llm_url),
            ("Model", self.local_llm_model),
            (None, test),
            ("Unload model after", self.unload_after),
            (None, _hint("Minutes without AI work before UniVerse unloads the model it used.")),
        )

    def _power_tab(self) -> QWidget:
        self.battery_pause_below = QSpinBox(minimum=0, maximum=95, singleStep=5, suffix=" %", specialValueText="Never")
        self.battery_pause_below.setValue(self.settings.battery_pause_below)
        self.only_when_charging = QCheckBox("Only run background AI work while charging")
        self.only_when_charging.setChecked(self.settings.only_when_charging)
        return _form(
            ("Pause on battery below", self.battery_pause_below),
            (None, self.only_when_charging),
            (None, _hint("Applies to transcription and sorting; recording always works.")),
        )

    def _general_tab(self) -> QWidget:
        self.embedding_model = QLineEdit(self.settings.embedding_model)
        reveal = QPushButton("Show in Finder", autoDefault=False)
        reveal.clicked.connect(lambda: subprocess.run(["open", str(data_root())]))
        folder_row = QHBoxLayout()
        folder_row.setContentsMargins(0, 0, 0, 0)
        folder_row.addWidget(QLabel(str(data_root())), 1)
        folder_row.addWidget(reveal)
        folder = QWidget()
        folder.setLayout(folder_row)
        return _form(
            ("Data folder", folder),
            (None, _hint("Set UNIVERSE_DATA_DIR before starting UniVerse to use another folder.")),
            ("Search embedding model", self.embedding_model),
            (None, _hint("Used only when sentence-transformers is installed; changes apply after a search index rebuild.")),
        )

    # --- actions ------------------------------------------------------------

    def _choose_binary(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "whisper.cpp CLI", "/opt/homebrew/bin")
        if path:
            self.whisper_binary.setText(path)

    def _model_value(self) -> str:
        index = self.local_llm_model.findText(self.local_llm_model.currentText())
        return self.local_llm_model.itemData(index) if index >= 0 else self.local_llm_model.currentText().strip()

    def _test_connection(self) -> None:
        probe = dataclasses.replace(self.settings, local_llm_url=self.local_llm_url.text().strip())
        self.test_btn.setEnabled(False)
        self.test_result.setText("Looking for a local AI server…")
        self._worker = Worker(find_models, probe)
        self._worker.setAutoDelete(False)  # keep the signals alive while the dialog is open
        self._worker.signals.finished.connect(self._connection_found)
        self._worker.signals.failed.connect(self._connection_failed)
        workers.run(self._worker)

    def _connection_found(self, result: tuple[str, list[str]]) -> None:
        url, models = result
        chosen = self._model_value()
        self.local_llm_model.clear()
        self.local_llm_model.addItem(f"Automatic — first chat model ({models[0]})" if models else "Automatic", "")
        for model in models:
            self.local_llm_model.addItem(model, model)
        self.local_llm_model.setCurrentIndex(max(0, self.local_llm_model.findData(chosen)))
        self.test_result.setText(f"✓ {url} — {len(models)} chat model{'s' * (len(models) != 1)}")
        self.test_btn.setEnabled(True)

    def _connection_failed(self, message: str) -> None:
        self.test_result.setText(f"✗ {message}")
        self.test_btn.setEnabled(True)

    def apply(self) -> None:
        s = self.settings
        s.whisper_binary = self.whisper_binary.text().strip() or Settings().whisper_binary
        s.whisper_model = self.whisper_model.currentData()
        s.whisper_language = self.whisper_language.currentData()
        s.whisper_threads = self.whisper_threads.value()
        s.auto_transcribe = self.auto_transcribe.isChecked()
        s.input_device = self.input_device.currentData()
        s.local_classify = self.local_classify.isChecked()
        s.local_llm_url = self.local_llm_url.text().strip()
        s.local_llm_model = self._model_value()
        s.local_llm_unload_after = self.unload_after.value() * 60
        s.battery_pause_below = self.battery_pause_below.value()
        s.only_when_charging = self.only_when_charging.isChecked()
        s.embedding_model = self.embedding_model.text().strip() or Settings().embedding_model
        s.save()

    def accept(self) -> None:
        self.apply()
        super().accept()
