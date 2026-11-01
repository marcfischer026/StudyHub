"""Tests for the Qt-free core: storage, timestamps, the Claude round trip, index, training data."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_tmp = tempfile.TemporaryDirectory()
os.environ["UNIVERSE_DATA_DIR"] = _tmp.name

from tests.test_claude_reply import jsonl, make_pdf  # noqa: E402
from universe.professors import ProfessorProfile, ProfessorStore  # noqa: E402
from universe.services import audio_player, claude_export, claude_reply, pdf_conversion, relevance, subject_model, timestamps  # noqa: E402
from universe.services.claude_reply import check_reply, parse_reply  # noqa: E402
from universe.services.study_store import Knowledge, StudyStore  # noqa: E402
from universe.services.transcriber import Segment, Transcript  # noqa: E402
from universe.services.vector_index import HashingEmbedder, SubjectIndex, lecture_chunks, subject_chunks  # noqa: E402
from universe.storage import Workspace, clock_time, import_file, write_json, pauses, read_timeline, safe_name, write_timeline  # noqa: E402

REPLY = (
    {"t": "topic", "id": "t_irr", "title": "IRR", "lectures": ["L01"], "importance": 3, "why": "Repeated twice."},
    {"t": "objective", "id": "L01-o001", "topic": "t_irr", "statement": "Define IRR", "importance": 3, "truth": "said"},
    {
        "t": "q", "id": "q_1", "objective": "L01-o001", "type": "mc", "stem": "IRR sets NPV to?",
        "options": [{"text": "0", "correct": True}, {"text": "1", "why_wrong": "That is a ratio"}], "explain": "By definition.",
    },
    {"t": "q", "id": "q_2", "objective": "L01-o001", "type": "tf", "stem": "NPV uses WACC.", "answer": "True"},
    {
        "t": "q", "id": "q_3", "objective": "L01-o001", "type": "short", "stem": "Define IRR.", "answer": "The rate where NPV = 0.",
        "rubric": [{"point": "NPV = 0", "marks": 1}],
    },
    {"t": "q", "id": "q_4", "objective": "L01-o001", "type": "card", "stem": "NPV?", "answer": "Net present value"},
)  # fmt: skip


def merge_reply(subject, *records: dict, export: str = "x1") -> StudyStore:
    """Import records the way the app does: parsed, checked, merged."""
    store = StudyStore(subject)
    view = subject_model.load_subject(subject)
    text = jsonl(*records, head={"t": "head", "schema": claude_reply.SCHEMA, "export": export, "part": 1})
    store.merge(check_reply(parse_reply(text), view, store.known_ids()), text, view)
    return store


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.ws = Workspace(self.root)

    def test_a_file_that_no_longer_parses_is_kept_when_written_over(self):
        path = self.root / "tags.json"
        path.write_text("kaputt", encoding="utf-8")
        write_json(path, {"a": 1})
        self.assertIn('"a": 1', path.read_text(encoding="utf-8"))
        self.assertEqual([p.read_text(encoding="utf-8") for p in self.root.glob("tags.broken-*.json")], ["kaputt"])
        write_json(path, {"a": 2})
        self.assertEqual(len(list(self.root.glob("tags.broken-*.json"))), 1)

    def test_a_stem_is_taken_whatever_its_suffix(self):
        # A recording's transcript goes by the stem alone, so song.mp3 must not share song.wav's.
        source = self.root / "from" / "song.mp3"
        source.parent.mkdir()
        source.write_bytes(b"x")
        folder = self.root / "audio"
        folder.mkdir()
        (folder / "song.wav").write_bytes(b"y")
        self.assertEqual(import_file(source, folder).name, "song (1).mp3")

    def test_create_subject_and_lecture_layout(self):
        subject = self.ws.create_subject("Corporate Finance")
        lecture = subject.create_lecture("Week 1: NPV")
        self.assertEqual(lecture.name, "Week 1: NPV")
        self.assertEqual(lecture.path.name, "Week 1_ NPV")
        for sub in ("audio", "slides", "transcripts", "figures"):
            self.assertTrue((lecture.path / sub).is_dir())
        self.assertFalse((lecture.path / "ai").exists())
        self.assertEqual([lec.name for lec in subject.lectures()], ["Week 1: NPV"])

    def test_figures_get_link_safe_names(self):
        lecture = self.ws.create_subject("Mathe").create_lecture("Week 1")
        first = lecture.add_figure(b"1", {"pdf": "VL 1 Folien.pdf", "page": 12})
        second = lecture.add_figure(b"2", {"pdf": "VL 1 Folien.pdf", "page": 12})
        self.assertEqual((first.name, second.name), ("VL-1-Folien-p12.png", "VL-1-Folien-p12-2.png"))
        self.assertEqual(lecture.figure_source(second)["page"], 12)

    def test_audio_bundle_includes_its_transcripts_only(self):
        lecture = self.ws.create_subject("Mathe").create_lecture("Week 1")
        audio = lecture.dir("audio") / "Memo.m4a"
        other = lecture.dir("audio") / "Memo (1).m4a"
        for path in (audio, other):
            path.write_bytes(b"x")
        for name in ("Memo.json", "Memo.txt", "Memo (1).json"):
            (lecture.dir("transcripts") / name).write_text("{}")
        bundle = lecture.audio_bundle(audio)
        self.assertEqual([p.name for p in bundle], ["Memo.m4a", "Memo.json", "Memo.txt"])
        self.assertEqual([p.name for p in lecture.audio_bundle(other)], ["Memo (1).m4a", "Memo (1).json"])

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

    def test_recordings_and_transcripts_in_recording_order(self):
        lecture = self.ws.create_subject("Einführung ins BWL").create_lecture("VL 1")
        # Created out of order on purpose: file dates must not decide the order.
        names = ["BWL 2026-10-28 15.44.wav", "Mitschnitt 20261028 090000.m4a", "BWL 2026-10-28 15.30.wav"]
        for name in names:
            (lecture.dir("audio") / name).write_bytes(b"")
            Transcript(name, [Segment(0, 1, name)]).save(lecture.dir("transcripts"))
        expected = ["Mitschnitt 20261028 090000", "BWL 2026-10-28 15.30", "BWL 2026-10-28 15.44"]
        self.assertEqual([p.stem for p in lecture.audio_files()], expected)
        self.assertEqual([p.stem for p in lecture.transcripts()], expected)

    def test_move_recording_to_another_lecture(self):
        from datetime import datetime

        bwl = self.ws.create_subject("Einführung ins BWL").create_lecture("VL 1")
        cf = self.ws.create_subject("Corporate Finance").create_lecture("Week 1")
        audio = bwl.new_recording_path(datetime(2026, 10, 28, 15, 30))
        audio.write_bytes(b"RIFF")
        Transcript(audio.name, [Segment(0, 1, "NPV", exam=True)]).save(bwl.dir("transcripts"))

        moved = bwl.move_audio(audio, cf)

        self.assertEqual(moved.name, "CF 2026-10-28 15.30.wav")  # takes the new subject's abbreviation
        self.assertEqual(bwl.audio_files(), [])
        self.assertEqual(list(bwl.dir("transcripts").iterdir()), [])
        self.assertEqual(cf.audio_files(), [moved])
        transcript = Transcript.load(cf.dir("transcripts") / "CF 2026-10-28 15.30.json")
        self.assertEqual(transcript.audio, moved.name)
        self.assertTrue(transcript.segments[0].exam)  # labels travel with it
        self.assertTrue((cf.dir("transcripts") / "CF 2026-10-28 15.30.txt").exists())

    def test_move_keeps_other_names_and_avoids_clashes(self):
        subject = self.ws.create_subject("Mathe")
        first, second = subject.create_lecture("A"), subject.create_lecture("B")
        for lecture in (first, second):
            (lecture.dir("audio") / "Memo.m4a").write_bytes(b"x")
        moved = first.move_audio(first.dir("audio") / "Memo.m4a", second)
        self.assertEqual(moved.name, "Memo (2).m4a")
        self.assertEqual(sorted(p.name for p in second.audio_files()), ["Memo (2).m4a", "Memo.m4a"])
        self.assertEqual(second.move_audio(moved, second), moved)  # same lecture: nothing happens

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

    def test_stamp_with_time_of_day(self):
        from datetime import datetime

        stamp = timestamps.make_stamp(903, datetime(2026, 10, 29, 12, 15))
        self.assertEqual(stamp, "[▶ 00:15:03 · 12:15]")
        self.assertEqual(timestamps.stamp_at(f"x {stamp}", 4), 903)
        self.assertEqual(timestamps.normalize(f"\\{stamp[:-1]}\\]"), stamp)  # Qt's escaping goes, the clock stays


class TimelineTests(unittest.TestCase):
    def test_clock_time_skips_pauses(self):
        from datetime import datetime, timedelta

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "BWL 2026-10-29 12.00.wav"
            start = datetime(2026, 10, 29, 12, 0, 5)
            # 10 min recorded, paused 5 min, then recording went on
            write_timeline(audio, [(0.0, start), (600.0, start + timedelta(minutes=15))])
            timeline = read_timeline(audio)
            self.assertEqual(clock_time(audio, 300), start + timedelta(minutes=5))
            self.assertEqual(clock_time(audio, 660), start + timedelta(minutes=16))
            self.assertEqual(pauses(timeline), [(600.0, start + timedelta(minutes=10), start + timedelta(minutes=15))])

    def test_without_timeline_the_name_gives_the_start(self):
        from datetime import datetime

        audio = Path("/nowhere/BWL 2026-10-29 12.00.wav")
        self.assertEqual(clock_time(audio, 15 * 60), datetime(2026, 10, 29, 12, 15))

    def test_normalize_qt_escaping(self):
        self.assertEqual(timestamps.normalize(r"\[▶ 00:00:07\] x"), "[▶ 00:00:07] x")


class ClaudeRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.professors = ProfessorStore(Path(tempfile.mkdtemp()))
        profile = ProfessorProfile(name="Dr. Weber")
        self.professors.save(profile)
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finance", professor_id=profile.id)
        self.lecture = self.subject.create_lecture("Week 1")

    def view(self):
        return subject_model.load_subject(self.subject, self.professors)

    def test_parse_fenced_reply_with_prose(self):
        parsed = parse_reply("Here you go:\n```jsonl\n" + jsonl(*REPLY) + "\n```\nGood luck!")
        self.assertEqual([r["id"] for r in parsed.records if r["t"] == "q"], ["q_1", "q_2", "q_3", "q_4"])
        self.assertTrue(parsed.finished)

    def test_parse_bare_json_with_braces_in_strings(self):
        record = {**REPLY[3], "stem": "Sets {x} and } braces"}
        parsed = parse_reply("prefix " + jsonl(record))
        self.assertEqual(next(r for r in parsed.records if r["t"] == "q")["stem"], "Sets {x} and } braces")

    def test_a_bad_record_is_dropped_and_the_others_stay(self):
        bad = {**REPLY[2], "id": "q_bad", "options": [{"text": "a"}, {"text": "b"}]}
        checked = check_reply(parse_reply(jsonl(*REPLY, bad)), self.view())
        self.assertIn("q_bad", " ".join(f"{i.record} {i.message}" for i in checked.issues if i.level == "error"))
        self.assertEqual([q["id"] for q in checked.records["q"]], ["q_1", "q_2", "q_3", "q_4"])
        self.assertEqual(parse_reply("no json here").records, [])

    def test_export_and_import(self):
        self.lecture.write_notes("# Week 1\n\n[▶ 00:00:05] NPV intro\n")
        Transcript("rec.wav", [Segment(5, 9, "Net present value.")]).save(self.lecture.dir("transcripts"))
        view = self.view()
        plan = claude_export.plan_export(view, Knowledge())
        result = claude_export.write_export(view, plan, Knowledge(), [])
        files = {path.name: path.read_text(encoding="utf-8") for path in result.attach}
        self.assertIn("Dr. Weber", files[claude_export.OVERVIEW_FILE])
        self.assertIn("R01@5 Net present value.", files["L01 Week 1.md"])
        self.assertIn("NPV intro", files["L01 Week 1.md"])
        self.assertIn("JSON Lines", result.prompt)
        self.assertEqual(result.prompt_path.read_text(encoding="utf-8"), result.prompt)
        store = merge_reply(self.subject, *REPLY, export=result.export_id)
        self.assertEqual(sorted(store.knowledge().questions), ["q_1", "q_2", "q_3", "q_4"])

    def test_export_reads_figures_highlights_and_past_exams(self):
        import pymupdf

        png = self.lecture.add_figure(b"png", {"caption": "VL1.pdf, slide 2", "text": "x", "tables": ["|A|B|\n|---|---|\n|1|2|"]})
        self.lecture.write_notes(f"Intro\n\n![VL1.pdf, slide 2](figures/{png.name})\n")
        slides = pymupdf.open()
        page = slides.new_page()
        page.insert_text((50, 60), "Kapitalwert")
        page.add_highlight_annot(page.search_for("Kapitalwert")[0])
        slides.save(self.lecture.dir("slides") / "VL1.pdf")
        exam = pymupdf.open()
        exam.new_page().insert_text((50, 60), "Aufgabe 1: Kapitalwert berechnen")
        exam.save(self.subject.dir("exams") / "Klausur WS24.pdf")
        view = self.view()
        text = claude_export.render_lecture(view, view.lectures[0])
        self.assertIn("[Figure F01.1 — VL1.pdf, slide 2]", text)
        self.assertIn("|1|2|", text)
        self.assertNotIn("](figures/", text)  # no image links Claude cannot open
        self.assertRegex(text, r'✎ highlighted[^:\n]*: "Kapitalwert"')
        self.assertIn("Aufgabe 1: Kapitalwert berechnen", claude_export.render_shared_documents(view))
        plan = claude_export.plan_export(view, Knowledge())
        names = [p.name for p in claude_export.write_export(view, plan, Knowledge(), []).attach]
        self.assertIn(claude_export.SHARED_FILE, names)


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

    def test_past_exams_and_tasks_belong_to_no_lecture(self):
        subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finance")
        lecture = subject.create_lecture("Week 1")
        make_pdf(subject.dir("exams") / "K.pdf", ["Aufgabe Kapitalwert berechnen"])
        make_pdf(subject.dir("exercises") / "U.pdf", ["Uebung Buchungssatz"])
        make_pdf(lecture.dir("slides") / "VL.pdf", ["Folie Bilanz"])
        lecture.write_notes("")
        chunks = subject_chunks(subject)
        self.assertEqual([(c.lecture, c.source, c.ref) for c in chunks], [("", "exams", "K.pdf p.1"), ("", "exercises", "U.pdf p.1")])
        index = SubjectIndex(subject, HashingEmbedder())
        self.assertEqual(index.index_all(), 3)
        self.assertEqual(index.index_documents(), 2)  # replaced, not added
        self.assertEqual(index._table().count_rows(), 3)
        self.assertEqual(index.search("Aufgabe Kapitalwert berechnen", limit=1)[0].source, "exams")
        self.assertEqual({hit.source for hit in index.search("Aufgabe Kapitalwert", lecture="Week 1")}, {"slides"})
        index.index_lecture(lecture)  # re-indexing a lecture leaves the shared documents alone
        self.assertEqual(index._table().count_rows(), 3)

    def test_a_conversion_is_what_gets_searched(self):
        subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finance")
        lecture = subject.create_lecture("Week 1")
        path = make_pdf(lecture.dir("slides") / "Scan.pdf", ["", "Roher Text"])
        lecture.write_notes("")
        key, _ = subject_model.document_key(path)
        pdf_conversion.save_pages(subject, key, "Scan.pdf", "SL01", {1: "Bilanz aus dem Scan"})
        self.assertEqual([(c.ref, c.text) for c in lecture_chunks(lecture)], [("Scan.pdf p.1", "Bilanz aus dem Scan"), ("Scan.pdf p.2", "Roher Text")])
        self.assertIn("Bilanz aus dem Scan", relevance.course_context(lecture))

    def test_an_unreadable_pdf_does_not_stop_the_others(self):
        subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finance")
        (subject.dir("exams") / "broken.pdf").write_bytes(b"not a pdf")
        make_pdf(subject.dir("exams") / "K.pdf", ["Aufgabe"])
        self.assertEqual([c.ref for c in subject_chunks(subject)], ["K.pdf p.1"])


class DatasetTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "modal_training"))
        import dataset

        self.dataset = dataset
        self.name = self.id().rsplit(".", 1)[-1]
        self.subject = Workspace().create_subject(self.name)
        self.lecture = self.subject.create_lecture("L1")
        self.lecture.write_notes("")

    def test_build_dataset(self):
        self.lecture.write_notes("# L1\n\n[▶ 00:00:01] Some notes")
        merge_reply(self.subject, *REPLY)
        rows = self.dataset.build_dataset(self.name)
        self.assertEqual(len(rows), 1 + 4)  # notes chunk + questions
        self.assertNotIn("▶", rows[0]["messages"][2]["content"])
        pairs = [(row["messages"][1]["content"], row["messages"][2]["content"]) for row in rows[1:]]
        self.assertEqual(pairs[0], ("IRR sets NPV to?\n- 0\n- 1", "0\n\nBy definition."))
        self.assertEqual(pairs[1], ("NPV uses WACC.", "True"))
        self.assertEqual(pairs[2], ("Define IRR.", "The rate where NPV = 0."))
        self.assertEqual(pairs[3], ("NPV?", "Net present value"))  # a card is front and back

    def test_retired_and_flagged_questions_stay_out(self):
        store = merge_reply(self.subject, *REPLY, {"t": "retire", "id": "q_2", "why": "doppelt"})
        store.flag_question("q_3", "falsch")
        asked = [row["messages"][1]["content"] for row in self.dataset.build_dataset(self.name)]
        self.assertEqual(asked, ["IRR sets NPV to?\n- 0\n- 1", "NPV?"])

    def test_a_calculation_without_answer_text_gives_its_number(self):
        calc = {
            "t": "q", "id": "q_5", "objective": "L01-o001", "type": "calc", "stem": "Kapitalwert?",
            "numeric": {"value": 1200, "tolerance": 0.5, "unit": "€"}, "rubric": [{"point": "Ergebnis", "marks": 1}],
        }  # fmt: skip
        merge_reply(self.subject, *REPLY[:2], calc)
        self.assertEqual(self.dataset.build_dataset(self.name)[0]["messages"][2]["content"], "1200 €")

    def test_private_talk_and_no_knowledge_yet(self):
        Transcript("a.wav", [Segment(0, 1, "Inhalt"), Segment(2, 3, "Kino", category="off_topic")]).save(self.lecture.dir("transcripts"))
        rows = self.dataset.build_dataset(self.name)
        self.assertEqual([row["messages"][2]["content"] for row in rows], ["Inhalt"])

    def test_dataset_needs_nothing_of_the_app(self):
        source = (ROOT / "modal_training" / "dataset.py").read_text(encoding="utf-8")
        self.assertNotIn("universe.", source.replace(".universe_data", ""))


@unittest.skipUnless(shutil.which("afconvert"), "needs macOS afconvert")
class PlaybackCopyTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        wav = self.dir / "tone.wav"
        with wave.open(str(wav), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16_000)
            out.writeframes(b"\x00\x10" * 16_000)  # 1 s
        self.m4a = self.dir / "Memo.m4a"
        subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", str(wav), str(self.m4a)], check=True)
        self.wav = wav

    def test_m4a_gets_a_cached_wav_copy(self):
        copy = audio_player.playable_path(self.m4a)
        self.assertEqual(copy.suffix, ".wav")
        self.assertNotEqual(copy.parent, self.m4a.parent)  # not listed among the lecture's audio
        with wave.open(str(copy), "rb") as wav:
            self.assertAlmostEqual(wav.getnframes() / wav.getframerate(), 1.0, delta=0.1)
        mtime = copy.stat().st_mtime_ns
        self.assertEqual(audio_player.playable_path(self.m4a), copy)
        self.assertGreaterEqual(copy.stat().st_mtime_ns, mtime)  # reused, not converted again

    def test_playable_formats_are_used_directly(self):
        self.assertEqual(audio_player.playable_path(self.wav), self.wav)

    def test_duration_of_m4a_without_converting(self):
        self.assertAlmostEqual(audio_player.duration(self.m4a), 1.0, delta=0.1)
        self.assertAlmostEqual(audio_player.duration(self.wav), 1.0, delta=0.01)
        self.assertEqual(audio_player.duration(self.dir / "missing.m4a"), 0.0)

    def test_old_copies_are_pruned(self):
        copies = []
        for n in range(audio_player.KEEP_COPIES + 2):
            memo = self.dir / f"Memo {n}.m4a"
            shutil.copy(self.m4a, memo)
            copies.append(audio_player.playable_path(memo))
        kept = [c for c in copies if c.exists()]
        self.assertEqual(kept, copies[-audio_player.KEEP_COPIES :])


if __name__ == "__main__":
    unittest.main()
