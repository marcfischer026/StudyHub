"""Tests for the Qt-free core: storage, timestamps, Claude bridge, index."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_tmp = tempfile.TemporaryDirectory()
os.environ["UNIVERSE_DATA_DIR"] = _tmp.name

from universe.professors import ProfessorProfile, ProfessorStore  # noqa: E402
from universe.services import claude_bridge, timestamps  # noqa: E402
from universe.services.transcriber import Segment, Transcript  # noqa: E402
from universe.services.vector_index import HashingEmbedder, SubjectIndex  # noqa: E402
from universe.storage import Workspace, safe_name  # noqa: E402

VALID_REPLY = {
    "summary": "NPV and IRR.",
    "key_concepts": [{"term": "NPV", "explanation": "Discounted cash flows."}],
    "exam_focus": [{"topic": "IRR", "likelihood": "high", "reason": "Repeated twice."}],
    "quizzes": [
        {"question": "IRR sets NPV to?", "type": "multiple_choice", "options": ["0", "1"], "answer": "0", "explanation": ""},
        {"question": "NPV uses WACC.", "type": "true_false", "answer": "True"},
        {"question": "Define IRR.", "type": "short_answer", "answer": "The rate where NPV = 0."},
    ],
    "flashcards": [{"front": "NPV?", "back": "Net present value"}],
}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.ws = Workspace(self.root)

    def test_create_subject_and_lecture_layout(self):
        subject = self.ws.create_subject("Corporate Finance")
        lecture = subject.create_lecture("Week 1: NPV")
        self.assertEqual(lecture.name, "Week 1: NPV")
        self.assertEqual(lecture.path.name, "Week 1_ NPV")
        for sub in ("audio", "slides", "transcripts", "ai"):
            self.assertTrue((lecture.path / sub).is_dir())
        self.assertEqual([lec.name for lec in subject.lectures()], ["Week 1: NPV"])

    def test_duplicates_rejected(self):
        self.ws.create_subject("Physics")
        with self.assertRaises(FileExistsError):
            self.ws.create_subject("Physics")

    def test_professor_fallback_to_subject(self):
        subject = self.ws.create_subject("Physics", professor_id="abc")
        lecture = subject.create_lecture("Optics")
        self.assertEqual(lecture.professor_id, "abc")
        lecture.update_meta(professor_id="xyz")
        self.assertEqual(lecture.professor_id, "xyz")

    def test_rename_lecture(self):
        subject = self.ws.create_subject("Physics")
        lecture = subject.create_lecture("Optics")
        renamed = subject.rename_lecture(lecture, "Optics II")
        self.assertEqual(renamed.name, "Optics II")
        self.assertFalse(lecture.path.exists())

    def test_abbreviation(self):
        from universe.storage import abbreviate

        self.assertEqual(abbreviate("Einführung ins BWL"), "BWL")
        self.assertEqual(abbreviate("Corporate Finance"), "CF")
        self.assertEqual(abbreviate("Mikroökonomie"), "MIKR")
        subject = self.ws.create_subject("Corporate Finance")
        subject.update_meta(short_name="CorpFin")
        self.assertEqual(subject.short_name, "CorpFin")

    def test_recording_names_and_migration(self):
        from datetime import datetime

        lecture = self.ws.create_subject("Einführung ins BWL").create_lecture("VL 1")
        when = datetime(2026, 10, 28, 15, 30, 36)
        first = lecture.new_recording_path(when)
        self.assertEqual(first.name, "BWL 2026-10-28 15.30.wav")
        first.write_bytes(b"")
        self.assertEqual(lecture.new_recording_path(when).name, "BWL 2026-10-28 15.30 (2).wav")
        first.unlink()

        legacy = lecture.dir("audio") / "recording_20261028_153036.wav"
        legacy.write_bytes(b"RIFF")
        Transcript(legacy.name, [Segment(0, 1, "Hallo")]).save(lecture.dir("transcripts"))
        self.assertTrue(lecture.migrate_recording_names())
        self.assertEqual([p.name for p in lecture.audio_files()], ["BWL 2026-10-28 15.30.wav"])
        moved = Transcript.load(lecture.dir("transcripts") / "BWL 2026-10-28 15.30.json")
        self.assertEqual(moved.audio, "BWL 2026-10-28 15.30.wav")
        self.assertTrue((lecture.dir("transcripts") / "BWL 2026-10-28 15.30.txt").exists())
        self.assertFalse(lecture.migrate_recording_names())

    def test_safe_name(self):
        self.assertEqual(safe_name("a/b:c"), "a_b_c")
        with self.assertRaises(ValueError):
            safe_name("  ..  ")


class ProfessorTests(unittest.TestCase):
    def test_round_trip(self):
        store = ProfessorStore(Path(tempfile.mkdtemp()))
        prof = ProfessorProfile(name="Dr. Weber", focus_areas=["WACC"], testing_style="closed book")
        store.save(prof)
        loaded = store.get(prof.id)
        self.assertEqual(loaded, prof)
        self.assertIn("WACC", loaded.to_markdown())
        with self.assertRaises(ValueError):
            store.get("../etc")


class TimestampTests(unittest.TestCase):
    def test_format_and_parse(self):
        self.assertEqual(timestamps.make_stamp(3725), "[▶ 01:02:05]")
        line = "see [▶ 00:01:05] here"
        self.assertEqual(timestamps.stamp_at(line, 6), 65)
        self.assertIsNone(timestamps.stamp_at(line, 0))

    def test_normalize_qt_escaping(self):
        self.assertEqual(timestamps.normalize(r"\[▶ 00:00:07\] x"), "[▶ 00:00:07] x")


class ClaudeBridgeTests(unittest.TestCase):
    def test_parse_fenced_reply_with_prose(self):
        text = "Here you go:\n```json\n" + json.dumps(VALID_REPLY) + "\n```\nGood luck!"
        response = claude_bridge.parse_response(text)
        self.assertEqual(len(response.quizzes), 3)
        self.assertEqual(response.quizzes[0]["difficulty"], "medium")

    def test_parse_bare_json_with_braces_in_strings(self):
        data = dict(VALID_REPLY, summary="Sets {x} and } braces")
        response = claude_bridge.parse_response("prefix " + json.dumps(data))
        self.assertEqual(response.data["summary"], "Sets {x} and } braces")

    def test_rejects_invalid(self):
        bad = dict(VALID_REPLY, quizzes=[{"question": "q", "type": "multiple_choice", "options": ["a", "b"], "answer": "c"}])
        with self.assertRaises(claude_bridge.ResponseError) as ctx:
            claude_bridge.parse_response(json.dumps(bad))
        self.assertIn("must match one of its options", str(ctx.exception))
        with self.assertRaises(claude_bridge.ResponseError):
            claude_bridge.parse_response("no json here")

    def test_export_and_save(self):
        lecture = Workspace(Path(tempfile.mkdtemp())).create_subject("Finance").create_lecture("Week 1")
        lecture.write_notes("# Week 1\n\n[▶ 00:00:05] NPV intro\n")
        Transcript("rec.wav", [Segment(5, 9, "Net present value.")]).save(lecture.dir("transcripts"))
        path = claude_bridge.export_context(lecture, ProfessorProfile(name="Dr. Weber"), n_questions=7)
        text = path.read_text(encoding="utf-8")
        for needle in ("7 quiz questions", "Dr. Weber", "[S1.0 ▶ 00:00:05] Net present value.", "```json"):
            self.assertIn(needle, text)
        claude_bridge.save_response(lecture, claude_bridge.parse_response(json.dumps(VALID_REPLY)))
        self.assertEqual(len(claude_bridge.load_latest(lecture).quizzes), 3)


class IndexTests(unittest.TestCase):
    def test_index_and_search(self):
        subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finance")
        lecture = subject.create_lecture("Week 1")
        Transcript(
            "rec.wav",
            [Segment(0, 4, "Welcome to the course."), Segment(60, 65, "The internal rate of return makes NPV zero.")],
        ).save(lecture.dir("transcripts"))
        lecture.write_notes("Photosynthesis is unrelated to finance.")
        index = SubjectIndex(subject, HashingEmbedder())
        self.assertGreater(index.index_lecture(lecture), 0)
        count = index._table().count_rows()
        index.index_lecture(lecture)  # re-indexing replaces, never duplicates
        self.assertEqual(index._table().count_rows(), count)
        hits = index.search("internal rate of return", limit=2)
        self.assertEqual(hits[0].source, "transcript")
        index.remove_lecture("Week 1")
        self.assertEqual(index._table().count_rows(), 0)


class DatasetTests(unittest.TestCase):
    def test_build_dataset(self):
        sys.path.insert(0, str(ROOT / "modal_training"))
        import dataset

        lecture = Workspace().create_subject("Dataset Subject").create_lecture("L1")
        lecture.write_notes("# L1\n\n[▶ 00:00:01] Some notes")
        claude_bridge.save_response(lecture, claude_bridge.parse_response(json.dumps(VALID_REPLY)))
        rows = dataset.build_dataset("Dataset Subject")
        self.assertEqual(len(rows), 1 + 3 + 1)  # notes chunk + quizzes + flashcard
        self.assertNotIn("▶", rows[0]["messages"][2]["content"])


if __name__ == "__main__":
    unittest.main()
