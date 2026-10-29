"""Tests for the Recordings panel: selecting, deleting and moving recordings,
and the transcript panel's "All recordings" view."""

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

from PySide6.QtCore import QMimeData, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QDragMoveEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu  # noqa: E402

import universe  # noqa: E402,F401
from universe.config import Settings  # noqa: E402
from universe.services.transcriber import Segment, Transcript  # noqa: E402
from universe.storage import Workspace  # noqa: E402
from universe.ui.audio import RECORDINGS_MIME, AudioController, RecordingsPanel, recordings_from_mime  # noqa: E402
from universe.ui.sidebar import Sidebar  # noqa: E402
from universe.ui.transcript_panel import ALL_RECORDINGS, TranscriptPanel  # noqa: E402

app = QApplication.instance() or QApplication([])


class RecordingsPanelTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Workspace(Path(tempfile.mkdtemp()))
        lecture = self.workspace.create_subject("Mathe").create_lecture("Week 1")
        self.other = lecture.subject.create_lecture("Week 2")
        for name in ("A.wav", "B.wav", "C.wav"):
            (lecture.dir("audio") / name).write_bytes(b"")
        self.lecture = lecture
        self.audio = AudioController(Settings())
        self.audio.lecture = lecture
        self.panel = RecordingsPanel(
            self.audio, ensure_lecture=lambda hint="": lecture, lectures=lambda: lecture.subject.lectures()
        )
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

    def test_dragged_recordings_carry_their_paths_only(self):
        items = [self.panel.list.item(0), self.panel.list.item(2)]
        mime = self.panel.list.mimeData(items)
        self.assertEqual([p.name for p in recordings_from_mime(mime)], ["A.wav", "C.wav"])
        self.assertFalse(mime.hasUrls())  # dropping on Finder must not move files out

    def test_move_menu_lists_other_lectures_and_requests_the_move(self):
        moves = []
        self.panel.moveRequested.connect(lambda paths, lecture: moves.append(([p.name for p in paths], lecture.name)))
        self.panel.list.item(1).setSelected(True)
        menu = QMenu()
        self.panel._add_move_menu(menu)
        submenu = menu.actions()[0].menu()
        targets = [a for a in submenu.actions() if not a.isSeparator() and a.text() != "Mathe"]
        self.assertEqual([a.text() for a in targets], ["Week 2"])  # not the lecture it is already in
        targets[0].trigger()
        self.assertEqual(moves, [(["B.wav"], "Week 2")])


class SidebarDropTests(unittest.TestCase):
    def test_drop_target_is_another_lecture(self):
        workspace = Workspace(Path(tempfile.mkdtemp()))
        subject = workspace.create_subject("Mathe")
        week1, week2 = subject.create_lecture("Week 1"), subject.create_lecture("Week 2")
        audio = week1.dir("audio") / "A.wav"
        audio.write_bytes(b"")
        sidebar = Sidebar(workspace)
        sidebar.resize(300, 400)
        sidebar.show()
        tree = sidebar.tree
        mime = QMimeData()
        mime.setData(RECORDINGS_MIME, str(audio).encode())

        def target_at(lecture_name):
            item = next(
                tree.topLevelItem(0).child(i)
                for i in range(tree.topLevelItem(0).childCount())
                if tree.topLevelItem(0).child(i).data(0, Qt.ItemDataRole.UserRole).name == lecture_name
            )
            pos = QPointF(tree.visualItemRect(item).center())
            event = QDragMoveEvent(pos.toPoint(), Qt.DropAction.MoveAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            return tree._target(event)

        self.assertEqual(target_at("Week 2").name, "Week 2")
        self.assertIsNone(target_at("Week 1"))  # already there


class AllRecordingsViewTests(unittest.TestCase):
    def setUp(self):
        self.lecture = Workspace(Path(tempfile.mkdtemp())).create_subject("Einführung ins BWL").create_lecture("VL 1")
        for name, text in (("BWL 2026-10-28 15.44.wav", "after the break"), ("BWL 2026-10-28 15.30.wav", "first part")):
            (self.lecture.dir("audio") / name).write_bytes(b"")
            Transcript(name, [Segment(0, 1, text), Segment(1, 2, text + " 2")]).save(self.lecture.dir("transcripts"))
        self.panel = TranscriptPanel()
        self.panel.set_lecture(self.lecture)

    def test_shows_all_recordings_together_in_recording_order(self):
        self.assertEqual(self.panel.files.currentText(), ALL_RECORDINGS)
        texts = [self.panel.list.item(i).text() for i in range(self.panel.list.count())]
        self.assertIn("1. BWL 2026-10-28 15.30", texts[0])
        self.assertTrue(texts[1].endswith("first part"))
        self.assertIn("2. BWL 2026-10-28 15.44", texts[3])
        self.assertTrue(texts[4].endswith("after the break"))
        self.assertEqual(self.panel.list.item(4).data(Qt.ItemDataRole.UserRole)[0], "BWL 2026-10-28 15.44")  # plays the right file

    def test_label_saves_into_the_recording_it_belongs_to(self):
        self.panel._set([self.panel.list.item(5)], exam=True)
        later = Transcript.load(self.lecture.dir("transcripts") / "BWL 2026-10-28 15.44.json")
        first = Transcript.load(self.lecture.dir("transcripts") / "BWL 2026-10-28 15.30.json")
        self.assertEqual([s.exam for s in later.segments], [False, True])
        self.assertEqual([s.exam for s in first.segments], [False, False])

    def test_filter_skips_headings(self):
        self.panel.filter.setCurrentText("★ Exam-relevant")
        self.assertFalse(self.panel.list.item(0).isHidden())  # heading stays as a divider
        self.assertTrue(self.panel.list.item(1).isHidden())

    def test_single_recording_can_still_be_picked(self):
        self.panel.files.setCurrentText("BWL 2026-10-28 15.44")
        self.assertEqual(self.panel.list.count(), 2)  # no headings for one recording
        self.panel.refresh()
        self.assertEqual(self.panel.files.currentText(), "BWL 2026-10-28 15.44")  # choice survives a refresh


if __name__ == "__main__":
    unittest.main()
