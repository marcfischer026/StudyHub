"""Typing in the note editor: Markdown shortcuts, lists, checkboxes and find."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QTextBlockFormat, QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import universe  # noqa: E402,F401
from universe.services import claude_bridge, slide_links  # noqa: E402
from universe.storage import Workspace  # noqa: E402
from universe.ui.note_editor import NoteEditor  # noqa: E402

app = QApplication.instance() or QApplication([])


class NoteEditorTypingTests(unittest.TestCase):
    def setUp(self):
        self.notes = NoteEditor(time_source=lambda: None)
        self.notes.setEnabled(True)
        self.notes.resize(600, 400)
        self.notes.show()
        self.edit = self.notes.edit
        self.edit.setFocus()

    def type(self, text: str) -> None:
        QTest.keyClicks(self.edit, text)

    def key(self, key, modifier=Qt.KeyboardModifier.NoModifier) -> None:
        QTest.keyClick(self.edit, key, modifier)

    def markdown(self) -> str:
        return self.edit.toMarkdown().strip()

    def test_heading_shortcut_and_normal_text_after_enter(self):
        self.type("## Kapitalwert")
        self.key(Qt.Key.Key_Return)
        self.type("Text danach")
        self.assertEqual(self.markdown(), "## Kapitalwert\n\nText danach")

    def test_heading_button_on_empty_line_then_enter(self):
        self.notes._heading(2)
        self.type("Titel")
        self.key(Qt.Key.Key_Return)
        self.type("normal")
        self.assertEqual(self.markdown(), "## Titel\n\nnormal")

    def test_bullets_nest_with_tab_and_end_on_empty_item(self):
        self.type("- eins")
        self.key(Qt.Key.Key_Return)
        self.key(Qt.Key.Key_Tab)
        self.type("unterpunkt")
        self.key(Qt.Key.Key_Return)
        self.key(Qt.Key.Key_Backtab)
        self.type("zwei")
        self.key(Qt.Key.Key_Return)
        self.key(Qt.Key.Key_Return)  # empty item: leave the list
        self.type("danach")
        self.assertEqual(self.markdown(), "- eins\n  * unterpunkt\n- zwei\n\ndanach")  # Qt writes level 2 as *

    def test_numbered_list_keeps_counting_after_nesting(self):
        self.type("1. erstens")
        self.key(Qt.Key.Key_Return)
        self.key(Qt.Key.Key_Tab)
        self.type("detail")
        self.key(Qt.Key.Key_Return)
        self.key(Qt.Key.Key_Backtab)
        self.type("zweitens")
        lines = self.markdown().splitlines()
        self.assertTrue(lines[0].startswith("1.") and lines[0].endswith("erstens"))
        self.assertTrue(lines[2].startswith("2.") and lines[2].endswith("zweitens"))

    def test_checkbox_typed_and_ticked_by_click(self):
        self.type("[ ] Formel lernen")
        self.key(Qt.Key.Key_Return)
        self.type("[x] erledigt")
        self.assertEqual(self.markdown(), "- [ ] Formel lernen\n- [x] erledigt")
        first = self.edit.document().begin()
        left = self.edit.cursorRect(QTextCursor(first)).left()  # where the text starts; the box is left of it
        y = self.edit.document().documentLayout().blockBoundingRect(first).center().y()
        QTest.mouseClick(self.edit.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(max(2, left - 10), int(y)))
        self.assertEqual(first.blockFormat().marker(), QTextBlockFormat.MarkerType.Checked)

    def test_enter_after_ticked_item_gives_empty_box(self):
        self.type("[x] fertig")
        self.key(Qt.Key.Key_Return)
        self.type("neu")
        self.assertEqual(self.markdown(), "- [x] fertig\n- [ ] neu")

    def test_bold_and_italic_while_typing(self):
        self.type("Das ist **wichtig** und *neu* hier")
        self.assertEqual(self.markdown(), "Das ist **wichtig** und *neu* hier")
        self.assertEqual(self.edit.toPlainText(), "Das ist wichtig und neu hier")

    def test_stars_in_math_stay_text(self):
        self.type("a * b * c")
        self.assertEqual(self.edit.toPlainText(), "a * b * c")

    def test_dash_inside_a_list_item_is_text(self):
        self.type("- Punkt - mit Strich")
        self.assertEqual(self.markdown(), "- Punkt - mit Strich")

    def test_backspace_at_start_removes_the_bullet_first(self):
        self.type("- eins")
        self.key(Qt.Key.Key_Home)
        self.key(Qt.Key.Key_Backspace)
        self.assertEqual(self.markdown(), "eins")

    def test_find_counts_and_cycles(self):
        self.type("NPV hier, NPV da, npv dort")
        self.notes._show_find()
        QTest.keyClicks(self.notes.find_field, "npv")
        self.assertEqual(self.notes.find_count.text().split("/")[1], "3")
        start = self.edit.textCursor().selectionStart()
        self.notes._find(1)
        self.assertGreater(self.edit.textCursor().selectionStart(), start)
        self.notes._hide_find()
        self.assertFalse(self.notes.find_bar.isVisible())
        self.assertEqual(self.edit.extraSelections(), [])


class SlideLinkTests(unittest.TestCase):
    TARGET = {"doc": "slides", "pdf": "VL 1 (neu).pdf", "page": 12}

    def setUp(self):
        self.lecture = Workspace(Path(tempfile.mkdtemp())).create_subject("Finance").create_lecture("Week 1")
        self.lecture.write_notes("")
        self.notes = NoteEditor(time_source=lambda: None, context_source=lambda: dict(self.TARGET))
        self.notes.set_lecture(self.lecture)
        self.notes.resize(600, 400)
        self.notes.show()
        self.edit = self.notes.edit

    def test_href_round_trip_with_odd_file_name(self):
        self.assertEqual(slide_links.parse_href(slide_links.make_href(self.TARGET)), self.TARGET)
        self.assertIsNone(slide_links.parse_href("https://example.com"))

    def test_link_survives_saving_and_click_opens_the_page(self):
        self.notes.insert_slide_link()
        QTest.keyClicks(self.edit, "danach")
        self.notes.save()
        self.notes.set_lecture(None)
        self.notes.set_lecture(self.lecture)
        self.assertEqual(self.edit.toPlainText(), "→ VL 1 (neu), slide 12 danach")
        opened = []
        self.notes.anchorActivated.connect(opened.append)
        cursor = QTextCursor(self.edit.document())
        cursor.setPosition(3)
        rect = self.edit.cursorRect(cursor)
        QTest.mouseClick(self.edit.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        self.assertEqual(opened, [self.TARGET])

    def test_text_after_a_link_is_not_part_of_it(self):
        self.notes.insert_slide_link()
        QTest.keyClicks(self.edit, "x")
        self.assertTrue(self.edit.toMarkdown().strip().endswith(") x"))

    def test_export_names_the_pdf_and_keeps_the_line_tag(self):
        self.notes.insert_slide_link()
        QTest.keyClicks(self.edit, "wichtig")
        self.notes.save()
        text = claude_bridge.build_context(self.lecture, None)
        self.assertIn("(→ see VL 1 (neu).pdf slide 12) wichtig", text)
        self.assertNotIn("](slide:", text)
        self.assertIn("⟨VL 1 (neu).pdf slide 12⟩", text)


if __name__ == "__main__":
    unittest.main()
