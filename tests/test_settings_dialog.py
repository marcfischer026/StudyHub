"""Tests for the Settings dialog: loads current values, saves edits, finds local models."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import universe  # noqa: E402,F401
from universe.config import Settings  # noqa: E402
from universe.ui import settings_dialog  # noqa: E402
from universe.ui.settings_dialog import SettingsDialog  # noqa: E402

app = QApplication.instance() or QApplication([])


class SettingsDialogTests(unittest.TestCase):
    def test_ok_without_changes_keeps_settings(self):
        settings = Settings(whisper_language="de", local_llm_unload_after=600, battery_pause_below=30)
        before = Settings(**vars(settings))
        SettingsDialog(settings).accept()
        self.assertEqual(settings, before)

    def test_edits_are_saved(self):
        settings = Settings()
        dialog = SettingsDialog(settings)
        dialog.whisper_language.setCurrentIndex(dialog.whisper_language.findData("en"))
        dialog.local_llm_url.setText(" http://localhost:8080/v1 ")
        dialog.local_llm_model.setEditText("qwen3-14b-mlx")
        dialog.unload_after.setValue(0)
        dialog.battery_pause_below.setValue(40)
        dialog.auto_transcribe.setChecked(False)
        dialog.accept()
        saved = Settings.load()
        self.assertEqual(saved.whisper_language, "en")
        self.assertEqual(saved.local_llm_url, "http://localhost:8080/v1")
        self.assertEqual(saved.local_llm_model, "qwen3-14b-mlx")
        self.assertEqual(saved.local_llm_unload_after, 0)
        self.assertEqual(saved.battery_pause_below, 40)
        self.assertFalse(saved.auto_transcribe)

    def test_found_models_fill_the_model_list(self):
        dialog = SettingsDialog(Settings(local_llm_model="qwen3-14b-mlx"))
        dialog._connection_found(("http://localhost:8080/v1", ["gemma-3", "qwen3-14b-mlx"]))
        self.assertEqual(dialog._model_value(), "qwen3-14b-mlx")
        dialog.local_llm_model.setCurrentIndex(0)
        self.assertEqual(dialog._model_value(), "")  # "Automatic"
        self.assertIn("2 chat models", dialog.test_result.text())

    def test_find_models_ignores_the_saved_model(self):
        seen = []
        original = settings_dialog.local_llm.find_server, settings_dialog.lmstudio.chat_models
        settings_dialog.local_llm.find_server = lambda s: seen.append(s.local_llm_model) or ("http://x/v1", "a")
        settings_dialog.lmstudio.chat_models = lambda root: None  # not LM Studio
        try:
            found = settings_dialog.find_models(Settings(local_llm_model="gone-model"))
        finally:
            settings_dialog.local_llm.find_server, settings_dialog.lmstudio.chat_models = original
        self.assertEqual(found, ("http://x/v1", ["a"]))
        self.assertEqual(seen, [""])


if __name__ == "__main__":
    unittest.main()
