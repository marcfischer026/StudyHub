"""Tests for the Recordings panel: selecting and asking to delete recordings."""

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
from universe.storage import Workspace  # noqa: E402
from universe.ui.audio import AudioController, RecordingsPanel  # noqa: E402

app = QApplication.instance() or QApplication([])


class RecordingsPanelTests(unittest.TestCase):
    def setUp(self):
        lecture = Workspace(Path(tempfile.mkdtemp())).create_subject("Mathe").create_lecture("Week 1")
        for name in ("A.wav", "B.wav", "C.wav"):
            (lecture.dir("audio") / name).write_bytes(b"")
        self.audio = AudioController(Settings())
        self.audio.lecture = lecture
        self.panel = RecordingsPanel(self.audio, ensure_lecture=lambda hint="": lecture)
        self.panel.refresh()
        self.requested = []
        self.panel.deleteRequested.connect(self.requested.append)

    def test_delete_is_disabled_without_selection(self):
        self.panel.list.clearSelection()
        self.assertFalse(self.panel.delete_btn.isEnabled())
        self.panel._delete()
        self.assertEqual(self.requested, [])

    def test_delete_requests_all_selected_recordings(self):
        self.panel.list.item(0).setSelected(True)
        self.panel.list.item(2).setSelected(True)
        self.assertTrue(self.panel.delete_btn.isEnabled())
        self.panel.delete_btn.click()
        self.assertEqual([[p.name for p in paths] for paths in self.requested], [["A.wav", "C.wav"]])


if __name__ == "__main__":
    unittest.main()
