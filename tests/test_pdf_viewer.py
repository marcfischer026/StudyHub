"""Tests for the slide viewer: selecting and copying text, highlighting it into the PDF."""

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

import pymupdf  # noqa: E402
from PySide6.QtCore import QPointF  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import universe  # noqa: E402,F401
from universe.ui.pdf_viewer import PdfViewer  # noqa: E402

app = QApplication.instance() or QApplication([])


def make_pdf(folder: Path, rotation: int = 0) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=400)
    page.insert_text((50, 100), "Entropy measures disorder")
    page.insert_text((50, 130), "Second line here")
    page.set_rotation(rotation)
    path = folder / "lecture.pdf"
    doc.save(path)
    return path


class PdfViewerTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def open_viewer(self, rotation: int = 0) -> PdfViewer:
        self.path = make_pdf(self.folder, rotation)
        viewer = PdfViewer(lambda _hint: self.folder)
        viewer.resize(700, 600)
        viewer.set_folder(self.folder)
        viewer._set_zoom(1.0)
        return viewer

    def widget_pos(self, viewer: PdfViewer, x: float, y: float) -> QPointF:
        """Unrotated PDF point -> widget position at the viewer's zoom."""
        p = pymupdf.Point(x, y) * viewer.doc[0].rotation_matrix * viewer.zoom
        return QPointF(p.x, p.y)

    def drag(self, viewer: PdfViewer, start: tuple, end: tuple) -> None:
        viewer._begin_drag(0, self.widget_pos(viewer, *start))
        viewer._continue_drag(0, self.widget_pos(viewer, *end))
        viewer._end_drag(0)

    def test_drag_selects_text_across_lines(self):
        viewer = self.open_viewer()
        self.drag(viewer, (60, 95), (80, 125))
        self.assertEqual(viewer.selected_text(), "Entropy measures disorder\nSecond")
        viewer.copy_selection()
        self.assertEqual(QApplication.clipboard().text(), "Entropy measures disorder\nSecond")

    def test_area_drag_selects_words_inside(self):
        viewer = self.open_viewer()
        self.drag(viewer, (40, 115), (300, 140))
        self.assertEqual(viewer.selected_text(), "Second line here")

    def test_highlight_is_saved_into_the_pdf(self):
        viewer = self.open_viewer()
        viewer.highlighter.setChecked(True)
        self.drag(viewer, (60, 95), (200, 95))
        with pymupdf.open(self.path) as doc:
            types = [a.type[0] for a in doc[0].annots()]
        self.assertEqual(types, [pymupdf.PDF_ANNOT_HIGHLIGHT])
        self.assertEqual(viewer.selected_text(), "")  # the selection becomes the mark

        point = pymupdf.Point(70, 95)
        viewer._remove_highlight(0, point)
        with pymupdf.open(self.path) as doc:
            self.assertEqual(len(list(doc[0].annots())), 0)

    def test_highlighting_an_area_without_text(self):
        viewer = self.open_viewer()
        viewer.highlighter.setChecked(True)
        self.drag(viewer, (300, 200), (450, 300))
        with pymupdf.open(self.path) as doc:
            self.assertEqual(len(list(doc[0].annots())), 1)

    def test_snip_clips_an_area_even_when_starting_on_text(self):
        viewer = self.open_viewer()
        clipped = []
        viewer.figureClipped.connect(clipped.append)
        viewer.snipper.setChecked(True)
        self.drag(viewer, (60, 95), (300, 140))  # starts on "Entropy": still a box, not a text selection
        self.assertEqual(len(clipped), 1)
        figure = clipped[0]
        self.assertTrue(figure["png"].startswith(b"\x89PNG"))
        self.assertEqual((figure["pdf"], figure["page"]), ("lecture.pdf", 1))
        self.assertIn("Second line here", figure["text"])
        with pymupdf.open(self.path) as doc:
            self.assertEqual(len(list(doc[0].annots())), 0)  # snipping leaves the PDF alone

    def test_selection_on_rotated_page(self):
        viewer = self.open_viewer(rotation=90)
        self.drag(viewer, (60, 95), (200, 95))
        self.assertEqual(viewer.selected_text(), "Entropy measures disorder")


if __name__ == "__main__":
    unittest.main()
