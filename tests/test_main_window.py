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

from PySide6.QtCore import Qt  # noqa: E402
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


class NarrowWindowTests(unittest.TestCase):
    """The window fits half a laptop screen (macOS Split View); parts step aside, none is lost."""

    def make_window(self, suffix: str = "") -> MainWindow:
        window = MainWindow(Settings())
        self.addCleanup(window.deleteLater)
        lecture = window.workspace.create_subject(self.id().rsplit(".", 1)[-1] + suffix).create_lecture("Week 1")
        window.sidebar.refresh(select=lecture)
        window.show()
        self.addCleanup(window.hide)
        return window

    def test_default_layout_fits_half_a_screen(self):
        window = self.make_window()
        window.resize(700, 800)
        app.processEvents()
        self.assertLessEqual(window.width(), 700)
        bench = window.workbench
        self.assertTrue(bench.is_shown("notes") and bench.is_shown("documents"))  # what you read stays
        self.assertFalse(bench.is_shown("inspector"))  # stepped aside first
        self.assertTrue(bench.wanted["inspector"])  # ... but comes back with room
        viewer = window.documents.viewer
        self.assertEqual(viewer.snipper.text(), "✂")  # short labels, every tool still there
        window.resize(1600, 900)
        bench.center.setSizes([500, 700])
        app.processEvents()
        self.assertTrue(bench.is_shown("inspector"))
        self.assertEqual(viewer.snipper.text(), "✂ Snip")

    def test_asking_for_a_part_in_a_narrow_window_makes_room_for_it(self):
        window = self.make_window()
        window.resize(700, 800)
        app.processEvents()
        window._export_for_claude()  # opens the AI pane
        app.processEvents()
        bench = window.workbench
        self.assertTrue(bench.is_shown("inspector"))
        self.assertEqual(bench.inspector.current(), "ai")
        self.assertLessEqual(window.width(), 700)

    def test_hidden_parts_and_pane_are_remembered(self):
        window = self.make_window()
        window.part_actions["sidebar"].setChecked(False)
        window.workbench.show_pane("recordings")
        state = window.workbench.state()
        other = self.make_window(" 2")
        other.workbench.restore(state)
        self.assertFalse(other.workbench.is_shown("sidebar"))
        self.assertFalse(other.part_actions["sidebar"].isChecked())
        self.assertEqual(other.workbench.inspector.current(), "recordings")

    def test_no_docks_left(self):
        from PySide6.QtWidgets import QDockWidget

        self.assertEqual(self.make_window().findChildren(QDockWidget), [])


class ModeTests(unittest.TestCase):
    """Lecture · Read · Review · Exam: presets that remember your changes."""

    def setUp(self):
        self.window = MainWindow(Settings())
        self.addCleanup(self.window.deleteLater)
        lecture = self.window.workspace.create_subject(self.id().rsplit(".", 1)[-1]).create_lecture("Week 1")
        self.window.sidebar.refresh(select=lecture)
        self.window.resize(1600, 900)
        self.window.show()
        self.addCleanup(self.window.hide)
        app.processEvents()
        self.bench = self.window.workbench

    def shown(self) -> set[str]:
        return {part for part in ("sidebar", "notes", "documents", "inspector") if self.bench.is_shown(part)}

    def test_cmd_1_to_4_switch_modes(self):
        keys = [a.shortcut().toString() for a in self.window.mode_actions.values()]
        self.assertEqual(keys, ["Ctrl+1", "Ctrl+2", "Ctrl+3", "Ctrl+4"])
        self.assertEqual(self.bench.mode, "lecture")
        self.window.mode_actions["read"].trigger()
        self.assertEqual(self.bench.mode, "read")
        self.assertEqual(self.shown(), {"notes", "documents"})
        self.window.mode_actions["lecture"].trigger()
        self.assertEqual(self.shown(), {"sidebar", "notes", "documents", "inspector"})

    def test_a_mode_keeps_your_changes_until_reset(self):
        self.window.part_actions["sidebar"].setChecked(False)
        self.window.mode_actions["review"].trigger()
        self.assertIn("sidebar", self.shown())  # Review starts from its own preset
        self.window.mode_actions["lecture"].trigger()
        self.assertNotIn("sidebar", self.shown())  # your Lecture, as you left it
        self.window._reset_mode()
        self.assertIn("sidebar", self.shown())
        self.assertTrue(self.window.part_actions["sidebar"].isChecked())

    def test_exam_opens_practice(self):
        self.window.mode_actions["exam"].trigger()
        self.assertEqual(self.bench.inspector.current(), "ai")
        self.assertIs(self.window.ai.tabs.currentWidget(), self.window.ai.practice)
        self.assertNotIn("documents", self.shown())

    def test_switching_modes_does_not_report_parts_as_closed(self):
        self.window._ready = True
        self.window.statusBar().clearMessage()
        self.window.mode_actions["read"].trigger()
        self.assertNotIn("hidden", self.window.statusBar().currentMessage())

    def test_modes_and_options_survive_a_restart(self):
        self.window.mode_actions["review"].trigger()
        self.window.stacked_action.setChecked(True)
        self.window.inspector_left_action.setChecked(True)
        self.assertLess(self.bench.indexOf(self.bench.inspector), self.bench.indexOf(self.bench.center))
        self.window.settings.layout = self.bench.state()
        other = MainWindow(self.window.settings)
        self.addCleanup(other.deleteLater)
        self.assertEqual(other.workbench.mode, "review")
        self.assertTrue(other.mode_actions["review"].isChecked())
        self.assertTrue(other.workbench.stacked() and other.stacked_action.isChecked())
        self.assertTrue(other.workbench.inspector_left() and other.inspector_left_action.isChecked())

    def test_layout_saved_before_modes_becomes_lecture(self):
        old = {"main": "", "center": "", "shown": {"sidebar": False}, "pane": "recordings"}
        self.bench.restore(old)
        self.assertEqual(self.bench.mode, "lecture")
        self.assertNotIn("sidebar", self.shown())
        self.assertEqual(self.bench.inspector.current(), "recordings")

if __name__ == "__main__":
    unittest.main()
