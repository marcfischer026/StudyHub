"""Tests for the Documents tab: one list, type and own tags, filtering, links and the export."""

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

import pymupdf  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from universe.services import claude_bridge, slide_links  # noqa: E402
from universe.services.vector_index import lecture_chunks  # noqa: E402
from universe.storage import TAG_PALETTE, Workspace  # noqa: E402
from universe.ui.ai_panel import hit_target  # noqa: E402
from universe.ui.documents_panel import DocumentsPanel  # noqa: E402

app = QApplication.instance() or QApplication([])


def make_pdf(path: Path, text: str = "Hello", pages: int = 1) -> Path:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page().insert_text((50, 60), text)
    doc.save(path)
    return path


class Fixture(unittest.TestCase):
    def setUp(self):
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finance")
        self.lecture = self.subject.create_lecture("Week 1")
        self.slides = make_pdf(self.lecture.dir("slides") / "VL1.pdf", "Kapitalwert folie")
        self.exam = make_pdf(self.subject.dir("exams") / "Klausur WS24.pdf", "Aufgabe 1 Kapitalwert", pages=3)
        self.task = make_pdf(self.subject.dir("exercises") / "Blatt 1.pdf", "Rechne")

    def by_name(self, name: str):
        return next(d for d in self.lecture.documents() + self.subject.documents() if d.path.name == name)


class StorageTests(Fixture):
    def test_existing_files_are_tagged_by_their_folder_and_nothing_is_written(self):
        self.assertEqual(self.by_name("VL1.pdf").tags, ["Slides"])
        self.assertEqual(self.by_name("Klausur WS24.pdf").tags, ["Past exam"])
        self.assertEqual(self.by_name("Blatt 1.pdf").tags, ["Task"])
        self.assertEqual([p.name for p in self.lecture.dir("slides").iterdir()], ["VL1.pdf"])

    def test_tags_are_kept_and_never_move_the_file(self):
        doc = self.by_name("VL1.pdf")
        doc.set_tags(["Task", "Important", "Task"])
        self.assertEqual(self.by_name("VL1.pdf").tags, ["Task", "Important"])
        self.assertEqual(doc.types, ["Task"])
        self.assertEqual(doc.unit, "page")
        self.assertTrue(self.slides.exists())
        doc.set_tags([])  # a document may have no tag at all
        self.assertEqual(self.by_name("VL1.pdf").tags, [])
        self.assertEqual(self.by_name("VL1.pdf").type_label, "Document")

    def test_tags_follow_a_renamed_lecture(self):
        self.by_name("VL1.pdf").set_tags(["Slides", "Important"])
        moved = self.subject.rename_lecture(self.lecture, "Woche 1")
        self.assertEqual(moved.documents()[0].tags, ["Slides", "Important"])

    def test_damaged_tag_file_falls_back_to_the_type(self):
        (self.slides.parent / "tags.json").write_text("not json", encoding="utf-8")
        self.assertEqual(self.by_name("VL1.pdf").tags, ["Slides"])

    def test_upper_case_suffix_is_listed(self):
        make_pdf(self.subject.dir("exams") / "ALT.PDF")
        self.assertIn("ALT.PDF", [d.path.name for d in self.subject.documents()])

    def test_tag_colors_and_deleting_a_tag(self):
        self.subject.set_tag_color("Important", TAG_PALETTE["Green"])
        self.assertEqual(self.subject.tag_colors()["Important"], TAG_PALETTE["Green"])
        self.assertIn("Past exam", self.subject.tag_colors())
        self.by_name("VL1.pdf").set_tags(["Slides", "Important"])
        self.by_name("Blatt 1.pdf").set_tags(["Important"])
        self.subject.delete_tag("Important")
        self.assertEqual(self.by_name("VL1.pdf").tags, ["Slides"])
        self.assertEqual(self.by_name("Blatt 1.pdf").tags, [])
        self.assertNotIn("Important", self.subject.tag_colors())
        self.assertTrue(self.slides.exists() and self.task.exists())


class PanelTests(Fixture):
    def setUp(self):
        super().setUp()
        self.panel = DocumentsPanel(self.folder_for)
        self.addCleanup(self.panel.deleteLater)
        self.panel.resize(600, 800)
        self.panel.set_scope(self.subject, self.lecture)

    def folder_for(self, kind, _hint):
        return self.lecture.dir("slides") if kind == "slides" else self.subject.dir(kind)

    def listed(self) -> list[str]:
        items = (self.panel.list.item(i) for i in range(self.panel.list.count()))
        return [item.data(Qt.ItemDataRole.UserRole).path.name for item in items]

    def filter_tags(self) -> list[str]:
        return [self.panel.filter_layout.itemAt(i).widget().property("tag") for i in range(self.panel.filter_layout.count())]

    def test_lists_lecture_and_subject_documents_together(self):
        self.assertEqual(self.listed(), ["VL1.pdf", "Klausur WS24.pdf", "Blatt 1.pdf"])
        self.assertEqual(self.panel.viewer.path, self.slides)
        self.assertEqual(self.filter_tags(), ["Slides", "Past exam", "Task"])

    def test_subject_without_lecture_lists_only_subject_documents(self):
        self.panel.set_scope(self.subject, None)
        self.assertEqual(self.listed(), ["Klausur WS24.pdf", "Blatt 1.pdf"])
        self.panel.set_scope(None, None)
        self.assertEqual(self.listed(), [])
        self.assertIsNone(self.panel.viewer.doc)

    def test_each_row_shows_its_tags_in_their_colours(self):
        self.panel.create_tag(self.by_name("VL1.pdf"), "Red", TAG_PALETTE["Red"])
        row = self.panel.list.itemWidget(self.panel.list.item(0))
        chips = {label.text(): label.styleSheet() for label in row.findChildren(QLabel)}
        self.assertIn(TAG_PALETTE["Blue"], chips["Slides"])
        self.assertIn(TAG_PALETTE["Red"], chips["Red"])

    def test_clicking_a_row_opens_the_document(self):
        self.panel.show()
        QTest.qWait(10)
        rect = self.panel.list.visualItemRect(self.panel.list.item(1))
        QTest.mouseClick(self.panel.list.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        self.assertEqual(self.panel.viewer.path, self.exam)

    def test_adding_and_removing_a_tag_keeps_the_open_page(self):
        doc = self.by_name("Klausur WS24.pdf")
        self.panel.list.setCurrentRow(1)
        self.panel.viewer.page.setValue(2)
        self.panel.create_tag(doc, "Important", TAG_PALETTE["Green"])
        self.assertEqual(self.by_name("Klausur WS24.pdf").tags, ["Past exam", "Important"])
        self.assertIn("Important", self.filter_tags())
        self.assertEqual(self.panel.viewer.path, self.exam)
        self.assertEqual(self.panel.viewer.page.value(), 2)
        self.panel.set_document_tag(self.by_name("Klausur WS24.pdf"), "Important", False)
        self.assertEqual(self.by_name("Klausur WS24.pdf").tags, ["Past exam"])
        self.assertNotIn("Important", self.filter_tags())  # unused tags do not clutter the filter
        self.assertIn("Important", self.subject.tag_colors())  # but stay available

    def test_filter_narrows_with_every_tag_and_opens_what_is_shown(self):
        self.panel.create_tag(self.by_name("Klausur WS24.pdf"), "Important", TAG_PALETTE["Green"])
        self.panel.create_tag(self.by_name("Blatt 1.pdf"), "Important", TAG_PALETTE["Green"])
        self.panel.set_tag_filter("Important", True)
        self.panel.refresh()
        self.assertEqual(self.listed(), ["Klausur WS24.pdf", "Blatt 1.pdf"])
        self.assertEqual(self.panel.viewer.path, self.exam)
        self.panel.set_tag_filter("Task", True)
        self.panel.refresh()
        self.assertEqual(self.listed(), ["Blatt 1.pdf"])
        self.panel.set_tag_filter("Important", False)
        self.panel.set_tag_filter("Task", False)
        self.panel.refresh()
        self.assertEqual(len(self.listed()), 3)
        QTest.qWait(1)

    def test_a_filter_with_no_match_shows_a_message_and_stays_clearable(self):
        self.panel.create_tag(self.by_name("VL1.pdf"), "Important", TAG_PALETTE["Green"])
        self.panel.set_tag_filter("Important", True)
        self.panel.set_tag_filter("Task", True)
        self.panel.refresh()
        self.assertEqual(self.listed(), [])
        self.assertIsNone(self.panel.viewer.path)
        self.assertEqual(self.filter_tags(), ["Slides", "Past exam", "Task", "Important"])

    def test_filter_is_forgotten_in_another_subject(self):
        self.panel.set_tag_filter("Task", True)
        other = Workspace(self.subject.path.parent).create_subject("Other")
        self.panel.set_scope(other, None)
        self.assertEqual(self.panel.active_tags, set())
        QTest.qWait(1)

    def test_import_puts_each_type_in_its_place(self):
        source = make_pdf(Path(tempfile.mkdtemp()) / "Neu.pdf")
        self.panel.set_tag_filter("Task", True)
        self.panel.refresh()
        self.panel.import_paths([source], "exams")
        self.assertTrue((self.subject.dir("exams") / "Neu.pdf").exists())
        self.assertEqual(self.panel.viewer.path.name, "Neu.pdf")
        self.assertEqual(self.panel.current_document().tags, ["Past exam"])
        self.assertEqual(self.panel.active_tags, set())  # the new one is not filtered away
        self.panel.import_paths([source], "slides")
        self.assertTrue((self.lecture.dir("slides") / "Neu.pdf").exists())
        self.assertEqual(self.panel.current_document().tags, ["Slides"])
        self.assertTrue(source.exists())

    def test_import_asks_for_the_type_and_can_be_cancelled(self):
        source = make_pdf(Path(tempfile.mkdtemp()) / "Neu.pdf")
        with mock.patch("universe.ui.documents_panel.QInputDialog.getItem", return_value=("Task (whole subject)", True)):
            self.panel.import_paths([source])
        self.assertTrue((self.subject.dir("exercises") / "Neu.pdf").exists())
        with mock.patch("universe.ui.documents_panel.QInputDialog.getItem", return_value=("", False)):
            self.panel.import_paths([make_pdf(source.with_name("Noch eins.pdf"))])
        self.assertNotIn("Noch eins.pdf", self.listed())

    def test_tag_menu_offers_every_tag_with_the_current_ones_checked(self):
        actions = {a.text(): a for a in self.panel._tag_menu(self.by_name("VL1.pdf")).actions()}
        self.assertTrue(actions["Slides"].isChecked())
        self.assertFalse(actions["Task"].isChecked())
        actions["Task"].trigger()
        self.assertEqual(self.by_name("VL1.pdf").tags, ["Slides", "Task"])

    def test_open_page_tells_same_named_files_apart_and_clears_a_hiding_filter(self):
        same = make_pdf(self.subject.dir("exercises") / "VL1.pdf", pages=2)
        self.panel.refresh()
        self.assertTrue(self.panel.open_page("exercises", "VL1.pdf", 2))
        self.assertEqual(self.panel.viewer.path, same)
        self.panel.set_tag_filter("Task", True)
        self.panel.refresh()
        self.assertTrue(self.panel.open_page("slides", "VL1.pdf", 1))
        self.assertEqual(self.panel.viewer.path, self.slides)
        self.assertEqual(self.panel.active_tags, set())
        self.assertFalse(self.panel.open_page("exams", "gone.pdf", 1))
        QTest.qWait(1)


class ConsumersTests(Fixture):
    def test_old_note_links_and_anchors_still_resolve_in_the_export(self):
        self.lecture.write_notes(
            "Siehe [→ Exercise Blatt 1, p. 2](slide:exercises/Blatt%201.pdf#2)\n\nZeile mit Anker\n"
        )
        self.lecture.write_anchors([{"text": "Zeile mit Anker", "doc": "exams", "pdf": "Klausur WS24.pdf", "page": 3}])
        text = claude_bridge.build_context(self.lecture, None)
        self.assertIn("(→ see task Blatt 1.pdf p. 2)", text)
        self.assertIn("⟨past exam Klausur WS24.pdf page 3⟩", text)

    def test_export_says_type_and_tags_of_each_document(self):
        self.by_name("Klausur WS24.pdf").set_tags(["Past exam", "Important"])
        text = claude_bridge.build_context(self.lecture, None)
        self.assertIn("### VL1.pdf — Type: Slides\n", text)
        self.assertIn("### Klausur WS24.pdf — Type: Past exam\n\n**Tags:** Important\n", text)
        self.assertIn("### Blatt 1.pdf — Type: Task\n", text)
        self.assertIn("Aufgabe 1 Kapitalwert", text)
        self.assertLess(text.index("## Documents of This Lecture"), text.index("### VL1.pdf"))
        self.assertLess(text.index("## Documents of the Whole Subject"), text.index("### Klausur WS24.pdf"))

    def test_export_follows_a_changed_type(self):
        self.by_name("VL1.pdf").set_tags(["Task"])
        self.lecture.write_notes("[→ VL1, slide 2](slide:slides/VL1.pdf#2)\n")
        text = claude_bridge.build_context(self.lecture, None)
        self.assertIn("### VL1.pdf — Type: Task", text)
        self.assertIn("(→ see task VL1.pdf p. 2)", text)

    def test_link_text_uses_the_type_and_old_links_still_parse(self):
        self.assertEqual(slide_links.link_text({"doc": "slides", "type": "Task", "pdf": "VL1.pdf", "page": 2}), "→ Task VL1, p. 2")
        self.assertEqual(slide_links.link_text({"doc": "exams", "pdf": "K.pdf", "page": 1}), "→ Past exam K, p. 1")
        self.assertEqual(slide_links.parse_href("slide:exercises/B.pdf#4"), {"doc": "exercises", "pdf": "B.pdf", "page": 4})

    def test_search_index_keeps_lecture_pdfs_with_a_changed_type(self):
        self.by_name("VL1.pdf").set_tags(["Past exam", "Important"])
        chunks = [c for c in lecture_chunks(self.lecture) if c.source == "slides"]
        self.assertEqual([c.ref for c in chunks], ["VL1.pdf p.1"])
        self.assertEqual(hit_target(chunks[0]), ("slides", "VL1.pdf", 1.0))


class MainWindowTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("universe.ui.main_window.QMessageBox")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window = MainWindow(Settings())  # never deleted: its destructor waits for threads that need the GIL
        self.addCleanup(self.window.hide)
        name = self.id().rsplit(".", 1)[-1]
        self.subject = self.window.workspace.create_subject(name)
        self.lecture = self.subject.create_lecture("Week 1")
        self.other = self.subject.create_lecture("Week 2")
        self.slides = make_pdf(self.lecture.dir("slides") / "VL1.pdf", pages=2)
        self.exam = make_pdf(self.subject.dir("exams") / "Klausur.pdf", pages=4)
        self.window.sidebar.refresh(select=self.lecture)

    def test_one_tab_instead_of_three(self):
        self.assertIn("documents", self.window.docks)
        for gone in ("slides", "exams", "exercises"):
            self.assertNotIn(gone, self.window.docks)

    def test_lecture_switch_keeps_an_open_subject_document_and_swaps_the_slides(self):
        panel = self.window.documents
        panel.open_page("exams", "Klausur.pdf", 3)
        self.window.sidebar.refresh(select=self.other)
        self.assertEqual(panel.viewer.path, self.exam)
        names = [panel.list.item(i).data(Qt.ItemDataRole.UserRole).path.name for i in range(panel.list.count())]
        self.assertEqual(names, ["Klausur.pdf"])

    def test_note_link_opens_its_page_in_the_documents_tab(self):
        self.window._jump_to_anchor({"doc": "exams", "pdf": "Klausur.pdf", "page": 3})
        self.assertEqual(self.window.documents.viewer.path, self.exam)
        self.window._jump_to_anchor({"doc": "slides", "pdf": "VL1.pdf", "page": 2})
        self.assertEqual(self.window.documents.viewer.path, self.slides)
        self.window._jump_to_anchor({"doc": "slides", "pdf": "gone.pdf", "page": 2})
        self.assertIn("gone.pdf", self.window.statusBar().currentMessage())

    def test_snip_and_link_name_the_document_type(self):
        self.window.documents.open_page("exams", "Klausur.pdf", 1)
        with mock.patch.object(self.window.notes, "insert_figure") as insert:
            self.window._add_figure({"png": b"png", "pdf": "Klausur.pdf", "page": 2, "text": "", "tables": []})
        self.assertEqual(insert.call_args[0][1], "Past exam: Klausur.pdf, page 2")
        with mock.patch.object(self.window.notes, "insert_slide_link") as link:
            self.window._link_page({"pdf": "Klausur.pdf", "page": 2})
        self.assertEqual(link.call_args[0][0], {"doc": "exams", "type": "Past exam", "pdf": "Klausur.pdf", "page": 2})

    def test_slide_import_with_no_lecture_open_still_creates_one(self):
        self.window.audio.recorder._stream = None
        self.window.sidebar.refresh(select=self.subject)
        source = make_pdf(Path(tempfile.mkdtemp()) / "Neue Folien.pdf")
        self.window.documents.import_paths([source], "slides")
        self.assertEqual(self.window.lecture.name, "Neue Folien")
        self.assertEqual(self.window.documents.viewer.path.name, "Neue Folien.pdf")


del Fixture  # only a base class

if __name__ == "__main__":
    unittest.main()
