"""Regression tests for export findings (synthetic, made-up data)."""

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
from universe.services import claude_export, pdf_conversion, relevance, subject_model  # noqa: E402
from universe.services.study_store import Knowledge  # noqa: E402
from universe.services.transcriber import Segment, Transcript  # noqa: E402
from universe.storage import Workspace  # noqa: E402


def _make_pdf(path: Path, pages: list[str]) -> Path:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((50, 60), text)
    doc.save(path)
    doc.close()
    return path


class Export3Tests(unittest.TestCase):
    def test_edited_markdown_and_highlight_flip_hash(self):
        ws = Workspace(Path(tempfile.mkdtemp()))
        subject = ws.create_subject("Export3Garten")
        lecture = subject.create_lecture("Moose")
        _make_pdf(lecture.dir("slides") / "Deck.pdf", ["Alfalfa eins", "Begonia zwei", "Clover drei"])
        store = ProfessorStore(Path(tempfile.mkdtemp()))
        view = subject_model.load_subject(subject, store)
        doc = view.documents[0]
        pdf_conversion.save_pages(subject, doc.key, doc.title, doc.ref, {1: "Alfalfa eins", 2: "Begonia zwei", 3: "Clover drei"})
        before = subject_model.load_subject(subject, store).lecture("L01").content_hash
        # Edit converted markdown text, same page count.
        pdf_conversion.save_pages(subject, doc.key, doc.title, doc.ref, {1: "Alfalfa eins improved words", 2: "Begonia zwei", 3: "Clover drei"})
        after_edit = subject_model.load_subject(subject, store).lecture("L01").content_hash
        self.assertNotEqual(before, after_edit, "edited markdown should change content_hash")
        # Add a highlight mark, same page count.
        pdf_path = lecture.dir("slides") / "Deck.pdf"
        old = pymupdf.open(pdf_path)
        page = old[1]
        hits = page.search_for("Begonia")
        self.assertTrue(hits)
        page.add_highlight_annot(hits[0])
        tmp = pdf_path.with_suffix(".tmp.pdf")
        old.save(tmp)
        old.close()
        tmp.replace(pdf_path)
        after_mark = subject_model.load_subject(subject, store).lecture("L01").content_hash
        self.assertNotEqual(after_edit, after_mark, "new highlight should change content_hash")


class Export4Tests(unittest.TestCase):
    def test_untranscribed_of_done_lecture_warns_in_review(self):
        ws = Workspace(Path(tempfile.mkdtemp()))
        subject = ws.create_subject("Export4Hafen")
        lecture = subject.create_lecture("Dune")
        Transcript("Dune.m4a", [Segment(0, 1, "Salbei Inhalt")]).save(lecture.dir("transcripts"))
        store = ProfessorStore(Path(tempfile.mkdtemp()))
        view = subject_model.load_subject(subject, store)
        done = Knowledge(lectures={view.lecture("L01").uid: {"processed": True, "hash": view.lecture("L01").content_hash}})
        # New audio without transcript; hash stays, so next plan is a review.
        (lecture.dir("audio") / "Neu.m4a").write_bytes(b"x")
        view = subject_model.load_subject(subject, store)
        self.assertEqual(view.lecture("L01").untranscribed, ("Neu",))
        plan = claude_export.plan_export(view, done)
        self.assertEqual(plan.lectures, [])
        self.assertIn("L01 Dune: 1 recording not transcribed yet.", plan.warnings)


class Export5Tests(unittest.TestCase):
    def test_progress_file_counted_in_plan(self):
        ws = Workspace(Path(tempfile.mkdtemp()))
        subject = ws.create_subject("Export5Turm")
        for n in range(19):
            subject.create_lecture(f"Meadow {n}")
        store = ProfessorStore(Path(tempfile.mkdtemp()))
        view = subject_model.load_subject(subject, store)
        events = [{"v": 1, "id": "a_test", "at": "2026-10-30T10:00:00", "type": "attempt", "q": "q_1", "o": "o_1", "topic": "t_a", "rev": 1, "mode": "today", "answer": "x", "correct": False, "score": 0.0, "confidence": "sure", "hints": 0, "ms": 1000, "by": "auto", "rating": 1}]
        plain = claude_export.plan_export(view, Knowledge(), budget=claude_export.LONG_BUDGET)
        self.assertFalse(any("too many for one chat" in w for w in plain.warnings))
        with_events = claude_export.plan_export(view, Knowledge(), budget=claude_export.LONG_BUDGET, events=events)
        self.assertTrue(any("too many for one chat" in w for w in with_events.warnings))
        result = claude_export.write_export(view, with_events, Knowledge(), events)
        self.assertIn(claude_export.PROGRESS_FILE, [p.name for p in result.attach])
        self.assertTrue(result.too_many_files)
        self.assertEqual(len(result.attach), 21)


class Import2Tests(unittest.TestCase):
    def test_uncovered_without_reason_warns(self):
        ws = Workspace(Path(tempfile.mkdtemp()))
        subject = ws.create_subject("Import2Wiese")
        lecture = subject.create_lecture("Klee")
        _make_pdf(lecture.dir("slides") / "Deck.pdf", ["Alfalfa eins", "Begonia zwei", "Clover drei"])
        store = ProfessorStore(Path(tempfile.mkdtemp()))
        view = subject_model.load_subject(subject, store)
        uid = view.lecture("L01").uid
        knowledge = Knowledge(lectures={uid: {"processed": True, "hash": "x", "export": "x1", "uncovered": [{"page": "SL01.p2", "why": ""}, {"page": "SL01.p3", "why": ""}], "accounted": [], "imported": ""}})
        plan = claude_export.plan_export(view, knowledge, budget=claude_export.LONG_BUDGET)
        self.assertIn("L01: Claude never accounted for SL01.p2, SL01.p3. Export this lecture again to close them.", plan.warnings)


class Consumers2Tests(unittest.TestCase):
    def test_explicit_cue_survives_two_classifiers(self):
        seg = Segment(0, 1, "Das kommt garantiert in der Klausur dran")
        transcript = Transcript("a.wav", [seg])
        relevance.apply_exam_cues(transcript)
        self.assertTrue(seg.exam)
        relevance.apply_label(seg, "lecture", False, "lokal", "local")
        self.assertTrue(seg.exam)
        relevance.apply_label(seg, "lecture", False, "claude", "claude")
        self.assertIs(seg.exam, True)  # a plain bool: the transcript is saved as JSON


class Consumers5Tests(unittest.TestCase):
    def test_missing_lectures_folder_gives_no_lecture_examples(self):
        import shutil

        sys.path.insert(0, str(ROOT / "modal_training"))
        import dataset as training_dataset

        name = f"Consumers5Wiese {Path(tempfile.mkdtemp()).name}"
        subject = Workspace().create_subject(name)
        shutil.rmtree(subject.path / "Lectures")
        rows = training_dataset.build_dataset(name)
        self.assertEqual(rows, [])


class Consumers6Tests(unittest.TestCase):
    def test_clock_suffix_is_stripped(self):
        sys.path.insert(0, str(ROOT / "modal_training"))
        import dataset as training_dataset

        self.assertEqual(training_dataset._STAMP.sub("", "[▶ 00:15:03 · 12:15] intro").strip(), "intro")
        self.assertEqual(training_dataset._STAMP.sub("", "[▶ 00:15:03] intro").strip(), "intro")


if __name__ == "__main__":
    unittest.main()

