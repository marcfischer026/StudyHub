"""Tests for the main window: moving around the sidebar while a recording runs."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import universe  # noqa: E402,F401
from universe.config import Settings  # noqa: E402
from universe.ui.main_window import MainWindow  # noqa: E402  (before PySide6: lancedb fails to import after it on Python 3.15)

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])


class SwitchingWhileRecordingTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("universe.ui.main_window.QMessageBox")
        self.message_box = patcher.start()
        self.addCleanup(patcher.stop)
        self.window = MainWindow(Settings())
        self.addCleanup(self.window.deleteLater)
        name = self.id().rsplit(".", 1)[-1]
        self.lecture = self.window.workspace.create_subject(f"{name} A").create_lecture("Week 1")
        self.other = self.window.workspace.create_subject(f"{name} B").create_lecture("Week 1")
        self.window.sidebar.refresh(select=self.lecture)

        recorder = self.window.audio.recorder  # the recorder's state only, no audio device
        recorder.path = self.lecture.new_recording_path()
        recorder.path.write_bytes(b"")  # no audio data yet, as at the start of a recording
        recorder._stream = object()
        self.addCleanup(setattr, recorder, "_stream", None)

    def test_clicking_a_subject_keeps_the_open_lecture(self):
        self.window.sidebar.refresh(select=self.other.subject)
        self.assertIs(self.window.center.currentWidget(), self.window.lecture_view)
        self.assertEqual(self.window.lecture.path, self.lecture.path)
        self.assertEqual(self.window.subject.path, self.lecture.subject.path)

    def test_clicking_a_lecture_still_switches(self):
        self.window.sidebar.refresh(select=self.other)
        self.assertEqual(self.window.lecture.path, self.other.path)

    def test_clicking_a_subject_opens_it_when_not_recording(self):
        self.window.audio.recorder._stream = None
        self.window.sidebar.refresh(select=self.other.subject)
        self.assertIs(self.window.center.currentWidget(), self.window.welcome)
        self.assertEqual(self.window.subject.path, self.other.subject.path)

    def test_returning_to_the_recording_lecture_does_not_load_the_running_file(self):
        self.window.sidebar.refresh(select=self.other)
        self.window.sidebar.refresh(select=self.lecture)
        self.assertIsNone(self.window.audio.player.path)
        self.message_box.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
