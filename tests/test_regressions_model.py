"""Regression tests for pdf_model findings."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())

import pymupdf  # noqa: E402

from universe.professors import ProfessorStore  # noqa: E402
from universe.services import pdf_conversion, subject_model  # noqa: E402
from universe.services.pdf_conversion import parse_converted  # noqa: E402
from universe.storage import Workspace, read_json, write_json  # noqa: E402


def make_pdf(path: Path, pages: list[str]) -> Path:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((50, 60), text)
    doc.save(path)
    doc.close()
    return path


class PdfModel1Tests(unittest.TestCase):
    def test_inline_mention_stays_plain_text(self):
        pages, _ = parse_converted("[[SL01 p.1]] Aktiva, see [[SL01 p.2]] for detail")
        self.assertEqual(pages, [(1, 1, "Aktiva, see [[SL01 p.2]] for detail")])


class PdfModel11Tests(unittest.TestCase):
    def test_two_fenced_parts_pasted_as_one_leave_no_fence_behind(self):
        for fence in ("```", "~~~"):
            pages, _ = parse_converted(f"{fence}markdown\n[[SL01 p.1]]\nEins\n{fence}\n{fence}markdown\n[[SL01 p.2]]\nZwei\n{fence}")
            self.assertEqual([body for _, _, body in pages], ["Eins", "Zwei"])

    def test_same_line_stamp_keeps_bold_markup(self):
        pages, _ = parse_converted("[[SL01 p.1]] **Bilanz**")
        self.assertEqual(pages, [(1, 1, "**Bilanz**")])


class PdfModel2Tests(unittest.TestCase):
    def setUp(self):
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finanzen")
        self.store = ProfessorStore(Path(tempfile.mkdtemp()))
        self.lecture = self.subject.create_lecture("Woche 1")

    def test_broken_pdf_does_not_hide_subject(self):
        make_pdf(self.lecture.dir("slides") / "Gut.pdf", ["Aktiva"])
        (self.lecture.dir("slides") / "broken.pdf").write_bytes(b"not a pdf")
        view = subject_model.load_subject(self.subject, self.store)
        self.assertEqual(len(view.lectures), 1)
        self.assertTrue(any(d.title == "Gut.pdf" for d in view.documents))
        self.assertTrue(any("broken.pdf" in p for p in view.problems))


class PdfModel3Tests(unittest.TestCase):
    def setUp(self):
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finanzen")
        self.store = ProfessorStore(Path(tempfile.mkdtemp()))
        self.lecture = self.subject.create_lecture("Woche 1")

    def test_bad_conversion_file_returns_empty(self):
        make_pdf(self.lecture.dir("slides") / "Gut.pdf", ["Aktiva"])
        view = subject_model.load_subject(self.subject, self.store)
        key = view.documents[0].key
        path = pdf_conversion.converted_path(self.subject, key)
        if path is None:
            pdf_conversion.save_pages(self.subject, key, "Gut.pdf", "SL01", {1: "Aktiva"})
            path = pdf_conversion.converted_path(self.subject, key)
        path.write_bytes(b"\xff\xfe bad bytes \xe4")
        self.assertEqual(pdf_conversion.load_pages(self.subject, key), {})


class PdfModel4Tests(unittest.TestCase):
    def setUp(self):
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finanzen")
        self.store = ProfessorStore(Path(tempfile.mkdtemp()))
        self.lecture = self.subject.create_lecture("Woche 1")

    def test_bad_transcript_does_not_hide_subject(self):
        from universe.services.transcriber import Segment, Transcript

        Transcript("gut.wav", [Segment(0, 1, "Hallo")]).save(self.lecture.dir("transcripts"))
        (self.lecture.dir("transcripts") / "a.json").write_bytes(b"not json")
        view = subject_model.load_subject(self.subject, self.store)
        self.assertEqual(len(view.lectures), 1)
        self.assertEqual(len(view.lecture("L01").recordings), 1)
        self.assertTrue(any("a.json" in p for p in view.problems))


class PdfModel5Tests(unittest.TestCase):
    def setUp(self):
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finanzen")
        self.store = ProfessorStore(Path(tempfile.mkdtemp()))
        self.lecture = self.subject.create_lecture("Woche 1")

    def test_list_shaped_anchors_do_not_crash_subject(self):
        self.lecture.write_notes("Hallo\n")
        (self.lecture.path / "notes.anchors.json").write_text("[]", encoding="utf-8")
        view = subject_model.load_subject(self.subject, self.store)
        self.assertEqual(len(view.lectures), 1)


class PdfModel6Tests(unittest.TestCase):
    def setUp(self):
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finanzen")
        self.store = ProfessorStore(Path(tempfile.mkdtemp()))
        self.lecture = self.subject.create_lecture("Woche 1")

    def test_broken_pdf_does_not_abort_export(self):
        make_pdf(self.lecture.dir("slides") / "Gut.pdf", ["Aktiva"])
        (self.lecture.dir("slides") / "broken.pdf").write_bytes(b"not a pdf")
        view = subject_model.load_subject(self.subject, self.store)
        result = pdf_conversion.export_pdfs(view)
        self.assertTrue(any("Gut" in p.name for p in result.files))
        self.assertFalse(any("broken" in p.name for p in result.files))
        self.assertTrue(any("broken.pdf" in p for p in result.problems))


class BestTextTests(unittest.TestCase):
    def test_a_conversion_that_failed_its_check_gives_way_to_the_pdf(self):
        subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Zynth")
        lecture = subject.create_lecture("Quil")
        path = lecture.dir("slides") / "Deck.pdf"
        make_pdf(path, ["Kapitalwert eintausend Euro Zinssatz fuenf Prozent", "Bilanz Aktiva Passiva Eigenkapital Fremdkapital"])
        view = subject_model.load_subject(subject, ProfessorStore(Path(tempfile.mkdtemp())))
        document = view.documents[0]
        pdf_conversion.save_pages(subject, document.key, document.title, document.ref, {1: "hallo welt ganz etwas anderes", 2: "Bilanz Aktiva Passiva Eigenkapital Fremdkapital **fett**"})
        texts = dict(subject_model.best_pages(lecture.documents()[0], subject))
        self.assertIn("Kapitalwert", texts[1])
        self.assertIn("**fett**", texts[2])

    def test_an_import_checks_pages_even_from_a_view_without_them(self):
        subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Zynth")
        lecture = subject.create_lecture("Quil")
        make_pdf(lecture.dir("slides") / "Deck.pdf", ["Bilanz Aktiva Passiva Eigenkapital Fremdkapital 1200"])
        view = subject_model.load_subject(subject, ProfessorStore(Path(tempfile.mkdtemp())), pages=False)
        report = pdf_conversion.import_converted(view, f"[[{view.documents[0].ref} p.1]]\nganz etwas anderes ohne die zahl")
        self.assertTrue(report.documents[0].weak)


class SidecarShapeTests(unittest.TestCase):
    def test_a_sidecar_of_the_wrong_shape_reads_as_empty_and_is_kept(self):
        path = Path(tempfile.mkdtemp()) / "tags.json"
        path.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(read_json(path), {})
        write_json(path, {"a": 1})
        self.assertEqual([p.read_text(encoding="utf-8") for p in path.parent.glob("tags.broken-*.json")], ["[1, 2]"])

    def test_a_bad_professor_file_is_skipped(self):
        store = ProfessorStore(Path(tempfile.mkdtemp()))
        (store.root / "kaputt.json").write_text("{}", encoding="utf-8")
        self.assertEqual(store.all(), [])

    def test_a_long_file_name_is_cut(self):
        self.assertLessEqual(len(pdf_conversion._file_stem("Ü" * 300)), pdf_conversion.FILE_STEM_CHARS)


if __name__ == "__main__":
    unittest.main()
