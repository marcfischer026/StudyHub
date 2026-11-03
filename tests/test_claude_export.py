"""Tests for the Markdown export: what goes along, what never does, and that a reply finds its way back."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())

from tests.test_claude_reply import jsonl, make_pdf  # noqa: E402
from universe.professors import ProfessorProfile, ProfessorStore  # noqa: E402
from universe.services import claude_export as export  # noqa: E402
from universe.services import claude_reply, subject_model, tutor  # noqa: E402
from universe.services.claude_reply import check_reply, parse_reply  # noqa: E402
from universe.services.pdf_conversion import EXPORT_FOLDER, PageCheck, save_pages  # noqa: E402
from universe.services.pdf_tools import Mark  # noqa: E402
from universe.services.study_store import Knowledge, StudyStore  # noqa: E402
from universe.services.subject_model import DocumentView, FigureView, NoteLineView, PageView  # noqa: E402
from universe.services.transcriber import Segment, Transcript  # noqa: E402
from universe.storage import DOC_TYPES, Workspace  # noqa: E402

NOW = datetime(2026, 11, 1, 18, 30)
DECK = ["Die Passivseite zeigt die Mittelherkunft", "", "Die Aktivseite zeigt die Mittelverwendung"]
SEGMENTS = [
    Segment(0.0, 4.0, "Willkommen zur Bilanz", speaker="lecturer"),
    Segment(5.0, 8.0, "Das kommt in der Klausur dran", category="lecture", exam=True, source="rules", speaker="lecturer"),
    Segment(20.0, 22.0, "Gilt das auch fuer Schulden", category="question", speaker="speaker_2"),
    Segment(30.0, 32.0, "UNCERTAINTEXT eine Fallaufgabe", category="off_topic", source="local", speaker="lecturer"),
    Segment(40.0, 42.0, "SIDETALKTEXT wir gehen nachher essen", category="off_topic", source="local", speaker="speaker_3"),
    Segment(50.0, 52.0, "PRIVATETEXT mein Geheimnis", category="off_topic", source="manual", speaker="lecturer"),
    Segment(55.0, 57.0, "PRIVATEEXAM auch geheim", category="off_topic", exam=True, source="manual", speaker="lecturer"),
    Segment(60.0, 62.0, "JUDGEDTEXT Wetter", category="off_topic", source="claude", speaker="lecturer"),
    Segment(400.0, 402.0, "Nach mehr als fuenf Minuten", speaker="lecturer"),
    Segment(410.0, 412.0, "Das habe ich selbst markiert", category="lecture", exam=True, source="manual", speaker="lecturer"),
]


def folder_text(subject) -> str:
    """Every file an export wrote or saved, as one text."""
    roots = [subject.claude_dir, subject.study_dir]
    return "\n".join(p.read_text(encoding="utf-8") for root in roots for p in root.rglob("*") if p.is_file() and p.suffix in (".md", ".json", ".jsonl"))


class Fixture(unittest.TestCase):
    """L01 with SL01 (three pages, the second empty), R01 and notes; L02 with the same deck and SL02;
    EX03 and TK04 for the whole subject; PR1."""

    def setUp(self):
        self.professors = ProfessorStore(Path(tempfile.mkdtemp()))
        profile = ProfessorProfile("Muster", testing_style="Klausur mit Rechenaufgaben")
        self.professors.save(profile)
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finanzen", professor_id=profile.id)
        self.first = self.subject.create_lecture("Bilanz")
        self.second = self.subject.create_lecture("GuV")
        make_pdf(self.first.dir("slides") / "VL.pdf", DECK)
        make_pdf(self.second.dir("slides") / "VL.pdf", DECK)
        make_pdf(self.second.dir("slides") / "Nur zwei.pdf", ["Die GuV zeigt Aufwand und Ertrag"])
        make_pdf(self.subject.dir("exams") / "K.pdf", ["Aufgabe eins Erstellen Sie eine Bilanz"])
        make_pdf(self.subject.dir("exercises") / "U.pdf", ["Uebung Buchungssatz bilden"])
        Transcript("Woche.m4a", list(SEGMENTS), id="r_one").save(self.first.dir("transcripts"))
        self.first.write_notes("Die Bilanz hat **zwei** Seiten\n")
        self.view = self.load()

    def load(self):
        return subject_model.load_subject(self.subject, self.professors)

    def lecture(self, ref: str = "L01"):
        return self.view.lecture(ref)

    def write(self, knowledge=None, events=(), now=NOW, **options):
        plan = export.plan_export(self.view, knowledge or Knowledge(), **options)
        return plan, export.write_export(self.view, plan, knowledge or Knowledge(), list(events), now)


def document_with(*pages: PageView) -> DocumentView:
    return DocumentView(
        key="d_x", number=4, ref="SL04", title="Block 1.pdf", types=(DOC_TYPES["slides"],), tags=("Important",), occurrences=(),
        lecture_refs=("L01",), whole_subject=False, page_count=len(pages), converted_pages=sum(p.converted for p in pages), pages=pages,
    )  # fmt: skip


class PageVariantTests(unittest.TestCase):
    def test_heading_says_what_the_document_is(self):
        text = export.render_document(document_with(PageView(1, "Text")))
        self.assertIn("### SL04 — Block 1.pdf — Slides — 1 slide — converted 0/1\nTags: Important\n", text)

    def test_marks_come_first(self):
        page = PageView(1, "Roh", marks=(Mark("Passivseite", "yellow"), Mark("", "pink")), markdown="# Bilanz")
        lines = export.render_document(document_with(page)).split("[[SL04 p.1]]\n")[1].splitlines()
        self.assertEqual(lines[:3], ['✎ highlighted (yellow): "Passivseite"', "✎ boxed something on this slide (pink)", "# Bilanz"])

    def test_converted_and_ok_is_only_the_markdown(self):
        text = export.render_document(document_with(PageView(1, "RAWTEXT", markdown="# Bilanz", check=PageCheck())))
        self.assertIn("# Bilanz", text)
        self.assertNotIn("RAWTEXT", text)
        self.assertNotIn("Check:", text)

    def test_failed_check_keeps_the_raw_text_in_a_fence(self):
        check = PageCheck(coverage=0.2, missing_numbers=("1200",))
        text = export.render_document(document_with(PageView(1, "RAWTEXT 1200", markdown="# Bilanz", check=check)))
        self.assertIn("# Bilanz", text)
        self.assertIn(f"(Check: {check.describe()}. The PDF's raw text follows; it is complete but unformatted.)", text)
        self.assertIn("```text\nRAWTEXT 1200\n```", text)

    def test_a_fence_in_the_raw_text_cannot_close_ours(self):
        text = export.render_document(document_with(PageView(1, "a\n```\nb", markdown="x", check=PageCheck(coverage=0.1))))
        self.assertIn("````text\na\n```\nb\n````", text)

    def test_a_converted_scan_says_so(self):
        text = export.render_document(document_with(PageView(1, "", markdown="# Gescannt", check=PageCheck(scanned=True))))
        self.assertIn("# Gescannt\n(A scan: this conversion could not be compared with the PDF.)", text)

    def test_not_converted_has_raw_text_tables_and_the_visual_hint(self):
        text = export.render_document(document_with(PageView(1, "Rohtext", tables=("| a | b |",), visual=True)))
        self.assertIn("Rohtext\nTable as detected:\n| a | b |\n(This page also has images or drawings the text misses; not converted yet. It may only be decoration.)", text)

    def test_an_empty_page_still_appears_with_its_marks(self):
        text = export.render_document(document_with(PageView(1, "", marks=(Mark("", "blue"),))))
        self.assertIn("[[SL04 p.1]]\n✎ boxed something on this slide (blue)\n(No text on this page; not converted yet.)", text)

    def test_a_page_the_tool_found_empty_is_not_called_unconverted(self):
        document = dataclasses.replace(document_with(PageView(1, "")), converted_pages=1)
        self.assertNotIn("not converted", export.render_document(document).split("[[SL04 p.1]]")[1])

    def test_every_page_of_a_real_deck_appears(self):
        view = subject_model.load_subject(_deck_subject(), ProfessorStore(Path(tempfile.mkdtemp())))
        text = export.render_document(view.documents[0])
        for number in (1, 2, 3):
            self.assertIn(f"[[SL01 p.{number}]]", text)
        self.assertIn("[[SL01 p.2]]\n(No text on this page; not converted yet.)", text)

    def test_lone_bullets_join_their_line(self):
        self.assertIn("• Erster Punkt", export.render_document(document_with(PageView(1, "•\nErster Punkt"))))


def _deck_subject():
    subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Nur Deck")
    make_pdf(subject.create_lecture("Eins").dir("slides") / "VL.pdf", DECK)
    return subject


class TranscriptTests(Fixture):
    def setUp(self):
        super().setUp()
        self.recording = self.lecture().recordings[0]
        self.text = export.render_recording(self.recording)
        self.lines = self.text.splitlines()

    def test_heading_counts_what_was_left_out(self):
        self.assertTrue(self.lines[0].startswith(f"### R01 — {self.recording.stem} — 7 min"))
        self.assertIn("2 lines I marked private are left out.", self.lines[1])
        self.assertIn("1 line sorted out as side talk stayed on my Mac.", self.lines[1])
        self.assertIn("1 line you already judged off-topic", self.lines[1])

    def test_the_flags_of_a_line(self):
        self.assertIn("R01@5 ★? Das kommt in der Klausur dran", self.lines)
        self.assertIn("R01@20 (question) Gilt das auch fuer Schulden", self.lines)
        self.assertIn("R01@30 ? UNCERTAINTEXT eine Fallaufgabe", self.lines)
        self.assertIn("R01@410 ★you Das habe ich selbst markiert", self.lines)

    def test_markers_appear_only_on_change(self):
        markers = [line for line in self.lines if line.startswith("⟨")]
        self.assertEqual(markers[0], "⟨00:00 · [Lecturer]⟩")
        self.assertIn("⟨[Speaker 2]⟩", markers)
        self.assertEqual(self.lines[self.lines.index("R01@0 Willkommen zur Bilanz") + 1], "R01@5 ★? Das kommt in der Klausur dran")
        self.assertIn("⟨00:06⟩", markers)  # 400 s later: the time again, and nothing else
        self.assertEqual(len(markers), 4)  # the start, a student, the lecturer again, the time

    def test_private_side_talk_and_judged_are_not_in_the_transcript(self):
        for word in ("PRIVATETEXT", "PRIVATEEXAM", "SIDETALKTEXT", "JUDGEDTEXT"):
            self.assertNotIn(word, self.text)

    def test_time_is_repeated_even_when_nothing_else_changes(self):
        stamped = [line for line in self.lines if line.startswith("⟨00:")]
        self.assertEqual(len(stamped), 2)

    def test_a_recording_with_nothing_left_says_so(self):
        Transcript("Leer.m4a", [Segment(1.0, 2.0, "PRIVATETEXT", category="off_topic", source="manual")], id="r_empty").save(self.second.dir("transcripts"))
        recording = self.load().lecture("L02").recordings[0]
        self.assertIn("(Nothing of this recording is left to read.)", export.render_recording(recording))

    def test_an_open_slide_is_named_but_never_an_excluded_one(self):
        lines = tuple(dataclasses.replace(line, slide="SL09.p3") for line in self.recording.lines)
        recording = dataclasses.replace(self.recording, lines=lines)
        self.assertIn("open: SL09.p3", export.render_recording(recording))
        self.assertNotIn("SL09", export.render_recording(recording, excluded={"SL09"}))


class NotesTests(Fixture):
    def test_note_lines_with_ids_and_tags(self):
        note = NoteLineView("N01.2", "Kommt dran", ("SL01.p1", "13:28", "R01@0"))
        lecture = dataclasses.replace(self.lecture(), notes=(note,), has_own_notes=True)
        self.assertEqual(export.render_notes(lecture), "N01.2 Kommt dran ⟨SL01.p1 · 13:28 · R01@0⟩\n")

    def test_a_tag_of_an_excluded_document_is_dropped(self):
        note = NoteLineView("N01.2", "Kommt dran", ("SL09.p1", "13:28"))
        lecture = dataclasses.replace(self.lecture(), notes=(note,), has_own_notes=True)
        self.assertEqual(export.render_notes(lecture, excluded={"SL09"}), "N01.2 Kommt dran ⟨13:28⟩\n")

    def test_a_figure(self):
        figure = FigureView("F01.1", "T-Konto", "SL01.p3", "Soll Haben", ("| Soll | Haben |",))
        lecture = dataclasses.replace(self.lecture(), notes=(NoteLineView("N01.3", "", figure=figure),), has_own_notes=True)
        self.assertEqual(export.render_notes(lecture), "N01.3 > [Figure F01.1 — T-Konto] clipped from SL01.p3 as important. Text in it: Soll Haben\n| Soll | Haben |\n")

    def test_no_notes_says_that_means_nothing(self):
        self.assertEqual(export.render_notes(self.lecture("L02")), export.NO_NOTES + "\n")
        self.assertIn("No notes for this lecture. That says nothing about what matters.", export.render_lecture(self.view, self.lecture("L02")))


class LectureFileTests(Fixture):
    def test_heading_and_sections(self):
        text = export.render_lecture(self.view, self.lecture())
        lecture = self.lecture()
        self.assertTrue(text.startswith(f"# L01 — Bilanz · Finanzen · {lecture.date}\nProfessor: PR1 Muster · Recordings: R01 (7 min) · Documents: SL01\n"))
        order = [text.index(s) for s in ("## Marked as exam-relevant", "## Slides and documents", "## What was said", "## My notes")]
        self.assertEqual(order, sorted(order))

    def test_exam_list_holds_marked_lines_but_nothing_private(self):
        text = export.render_lecture(self.view, self.lecture())
        listed = text.split("## Marked as exam-relevant while recording\n")[1].split("\n\n")[0]
        self.assertIn(export.EXAM_LEGEND, listed)
        self.assertIn("R01@5 ★? Das kommt", listed)
        self.assertIn("R01@410 ★you Das habe", listed)
        self.assertNotIn("PRIVATEEXAM", listed)

    def test_exam_mark_on_side_talk_stays_out_of_the_list(self):
        segments = [Segment(1.0, 2.0, "SIDETALKEXAM", category="off_topic", exam=True, source="local", speaker="speaker_3")]
        Transcript("Neben.m4a", segments, id="r_side").save(self.second.dir("transcripts"))
        self.assertNotIn("SIDETALKEXAM", export.render_lecture(self.load(), self.load().lecture("L02")))

    def test_a_deck_goes_into_the_first_lecture_and_is_referred_to_after(self):
        sent: set[str] = set()
        first = export.render_lecture(self.view, self.lecture("L01"), sent)
        second = export.render_lecture(self.view, self.lecture("L02"), sent)
        self.assertIn("[[SL01 p.1]]", first)
        self.assertNotIn("[[SL01 p.1]]", second)
        self.assertIn("SL01: see L01\n", second)

    def test_without_sent_each_lecture_is_whole(self):
        self.assertIn("[[SL01 p.1]]", export.render_lecture(self.view, self.lecture("L02")))

    def test_says_when_transcript_and_slides_are_missing(self):
        self.subject.create_lecture("Leer")
        view = self.load()
        text = export.render_lecture(view, view.lecture(f"L{view.lectures[-1].number:02d}"))
        self.assertIn("No slides or documents for this lecture.", text)
        self.assertIn("No recording for this lecture.", text)


class PlanTests(Fixture):
    def done(self, *refs: str) -> Knowledge:
        return Knowledge(lectures={self.lecture(r).uid: {"processed": True, "hash": self.lecture(r).content_hash} for r in refs})

    def test_statuses(self):
        knowledge = self.done("L01")
        knowledge.lectures[self.lecture("L02").uid] = {"processed": True, "hash": "old"}
        plan = export.plan_export(self.view, knowledge)
        self.assertEqual([s.status for s in plan.statuses], ["done", "changed"])
        self.assertEqual([s.tokens > 0 for s in plan.statuses], [True, True])

    def test_next_takes_what_is_not_done(self):
        plan = export.plan_export(self.view, self.done("L01"), budget=export.LONG_BUDGET)
        self.assertEqual([v.ref for v in plan.lectures], ["L02"])
        self.assertEqual(plan.left_out, [])

    def test_next_fills_the_budget_but_takes_at_least_one(self):
        plan = export.plan_export(self.view, Knowledge(), budget=1)
        self.assertEqual([v.ref for v in plan.lectures], ["L01"])
        self.assertEqual([v.ref for v in plan.left_out], ["L02"])
        whole = export.plan_export(self.view, Knowledge(), budget=export.LONG_BUDGET)
        self.assertEqual([v.ref for v in whole.lectures], ["L01", "L02"])
        self.assertLess(whole.tokens, whole.budget)

    def test_budget_stops_in_order(self):
        roomy = export.plan_export(self.view, Knowledge(), budget=export.LONG_BUDGET)
        plan = export.plan_export(self.view, Knowledge(), budget=roomy.tokens - 1)
        self.assertEqual([v.ref for v in plan.lectures], ["L01"])

    def test_all_includes_done_lectures_and_warns_when_over(self):
        roomy = export.plan_export(self.view, self.done("L01", "L02"), scope="all", budget=export.LONG_BUDGET)
        self.assertEqual(len(roomy.lectures), 2)
        tight = export.plan_export(self.view, Knowledge(), scope="all", budget=1)
        self.assertEqual([v.ref for v in tight.lectures], ["L01"])
        self.assertTrue(any("does not fit" in w for w in tight.warnings))

    def test_lecture_is_just_that_one(self):
        plan = export.plan_export(self.view, self.done("L02"), scope="lecture", lecture_ref="L02")
        self.assertEqual([v.ref for v in plan.lectures], ["L02"])
        self.assertEqual(plan.left_out, [])
        with self.assertRaises(ValueError):
            export.plan_export(self.view, Knowledge(), scope="lecture", lecture_ref="L99")

    def test_nothing_left_is_a_review_round(self):
        knowledge = self.done("L01", "L02")
        plan, result = self.write(knowledge)
        self.assertEqual(plan.lectures, [])
        names = [p.name for p in result.attach]
        self.assertEqual(names, [export.OVERVIEW_FILE, export.SHARED_FILE, export.KNOWLEDGE_FILE])
        self.assertIn(export.REVIEW_PARAGRAPH, result.prompt)
        self.assertTrue(result.folder.name.endswith(" review"))

    def test_side_talk_and_uncertain_counts(self):
        plan = export.plan_export(self.view, Knowledge(), scope="lecture", lecture_ref="L01")
        self.assertEqual((plan.side_talk_lines, plan.uncertain_lines), (1, 1))
        other = export.plan_export(self.view, Knowledge(), scope="lecture", lecture_ref="L02")
        self.assertEqual((other.side_talk_lines, other.uncertain_lines), (0, 0))


class WarningTests(Fixture):
    def warnings(self, knowledge=None, **options) -> str:
        return "\n".join(export.plan_export(self.view, knowledge or Knowledge(), budget=export.LONG_BUDGET, **options).warnings)

    def test_unconverted_documents_with_counts(self):
        plan = export.plan_export(self.view, Knowledge(), budget=export.LONG_BUDGET)
        self.assertIn("SL01 VL.pdf: 0 of 3 pages converted", "\n".join(plan.overview_warnings))
        self.assertIn("4 documents are not fully converted yet: 6 pages go along as plain PDF text.", "\n".join(plan.warnings))
        self.assertNotIn("SL01 VL.pdf: 0 of 3 pages converted", "\n".join(plan.warnings))

    def test_pages_that_failed_the_check(self):
        document = self.view.document("SL01")
        save_pages(self.subject, document.key, document.title, document.ref, {1: "Ganz etwas anderes", 2: "", 3: DECK[2]})
        self.view = self.load()
        self.assertIn("SL01 VL.pdf: the conversion of p.1 failed the check", self.warnings())

    def test_missing_material(self):
        text = self.warnings()
        self.assertIn("L02 GuV: no transcript.", text)
        self.assertIn("No exam date.", text)
        self.subject.create_lecture("Leer")
        self.view = self.load()
        self.assertIn("Leer: no slides.", self.warnings())

    def test_not_transcribed(self):
        (self.second.dir("audio") / "Neu.m4a").write_bytes(b"")
        self.view = self.load()
        self.assertIn("L02 GuV: 1 recording not transcribed yet.", self.warnings())

    def test_a_recording_mostly_sorted_out(self):
        many = [Segment(float(i), i + 0.5, "weg", category="off_topic", source="local", speaker="speaker_3") for i in range(4)]
        Transcript("Viel.m4a", many + [Segment(10.0, 11.0, "bleibt", speaker="lecturer")], id="r_many").save(self.second.dir("transcripts"))
        self.view = self.load()
        self.assertIn("a local model sorted out 4 of 5", self.warnings())

    def test_no_professor_no_exams(self):
        self.subject.update_meta(professor_id="")
        (self.subject.dir("exams") / "K.pdf").unlink()
        self.view = self.load()
        text = self.warnings()
        self.assertIn("No professor profile.", text)
        self.assertIn("No past exams.", text)

    def test_open_doc_notes(self):
        notes = [{"doc": "d_x", "ref": "SL01", "issue": "wrong_subject", "detail": "Mathe", "dismissed": False}, {"doc": "d_y", "ref": "SL99", "issue": "outdated", "detail": "alt", "dismissed": True}]
        text = self.warnings(Knowledge(doc_notes=notes))
        self.assertIn("SL01: may be misfiled or unusable (wrong_subject): Mathe", text)
        self.assertNotIn("SL99", text)

    def test_too_many_files(self):
        for number in range(25):
            self.subject.create_lecture(f"Vorlesung {number}")
        self.view = self.load()
        plan, result = self.write(budget=export.LONG_BUDGET)
        self.assertTrue(result.too_many_files)
        self.assertTrue(any("too many for one chat" in w for w in plan.warnings))

    def test_warnings_are_printed_in_the_overview(self):
        plan = export.plan_export(self.view, Knowledge())
        overview = export.render_overview(self.view, plan)
        self.assertIn("## Data check", overview)
        for warning in plan.overview_warnings:
            self.assertIn(warning, overview)


class PrivacyTests(Fixture):
    def test_private_never_in_any_file(self):
        self.write(budget=export.LONG_BUDGET, include_side_talk=True)
        text = folder_text(self.subject)
        self.assertNotIn("PRIVATETEXT", text)
        self.assertNotIn("PRIVATEEXAM", text)

    def test_judged_is_nowhere_but_counted(self):
        _, result = self.write(budget=export.LONG_BUDGET, include_side_talk=True)
        self.assertNotIn("JUDGEDTEXT", folder_text(self.subject))
        self.assertIn("1 line you already judged off-topic", (result.folder / "attach" / "L01 Bilanz.md").read_text(encoding="utf-8"))

    def test_side_talk_stays_on_the_mac_without_opt_in(self):
        _, result = self.write(budget=export.LONG_BUDGET)
        self.assertNotIn("SIDETALKTEXT", folder_text(self.subject))
        self.assertNotIn(export.SIDE_TALK_FILE, [p.name for p in result.attach])
        self.assertIn("stayed on my Mac", (result.folder / "attach" / "L01 Bilanz.md").read_text(encoding="utf-8"))

    def test_side_talk_opt_in_is_only_that_file(self):
        _, result = self.write(budget=export.LONG_BUDGET, include_side_talk=True)
        for path in result.attach:
            content = path.read_text(encoding="utf-8")
            self.assertEqual("SIDETALKTEXT" in content, path.name == export.SIDE_TALK_FILE, path.name)
        side = (result.folder / "attach" / export.SIDE_TALK_FILE).read_text(encoding="utf-8")
        self.assertIn("R01@40 [Speaker 3] SIDETALKTEXT", side)
        self.assertIn("never quote the rest", side)
        self.assertIn("category lecture", side)
        self.assertIn(f"are in {export.SIDE_TALK_FILE}", (result.folder / "attach" / "L01 Bilanz.md").read_text(encoding="utf-8"))

    def test_opt_in_without_such_lines_writes_no_file(self):
        _, result = self.write(scope="lecture", lecture_ref="L02", include_side_talk=True)
        self.assertNotIn(export.SIDE_TALK_FILE, [p.name for p in result.attach])


class ExclusionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.subject.set_document_excluded(self.view.document("SL02").key, True)
        self.view = self.load()

    def test_never_appears_but_is_listed_in_the_overview(self):
        _, result = self.write(budget=export.LONG_BUDGET)
        for path in result.attach:
            self.assertNotIn("Die GuV zeigt Aufwand", path.read_text(encoding="utf-8"), path.name)
        self.assertNotIn("Die GuV zeigt Aufwand", export.all_as_one_text(result))
        overview = result.attach[0].read_text(encoding="utf-8")
        self.assertIn("Documents I left out", overview)
        self.assertIn("SL02 Nur zwei.pdf", overview)
        self.assertNotIn("| SL02 |", overview)

    def test_the_lecture_does_not_name_it(self):
        text = export.render_lecture(self.view, self.lecture("L02"))
        self.assertNotIn("SL02", text)


class ExportFolderTests(Fixture):
    def test_layout_and_only_md_in_attach(self):
        _, result = self.write(budget=export.LONG_BUDGET)
        self.assertEqual(result.folder.parent, self.subject.claude_dir)
        self.assertEqual(result.folder.name, "2026-11-01 18.30 next")
        self.assertEqual({p.name for p in result.folder.iterdir()}, {"PROMPT.md", "ALL IN ONE.md", "CLAUDE.md", "attach", "reply"})
        self.assertTrue(all(p.suffix == ".md" for p in (result.folder / "attach").iterdir()))
        self.assertEqual([p.name for p in result.attach], ["01 Overview.md", "02 Past exams and tasks.md", "L01 Bilanz.md", "L02 GuV.md"])
        self.assertEqual(result.prompt_path.read_text(encoding="utf-8"), result.prompt)
        self.assertFalse(result.too_many_files)

    def test_a_deck_is_written_once_per_export(self):
        _, result = self.write(budget=export.LONG_BUDGET)
        first, second = [(result.folder / "attach" / name).read_text(encoding="utf-8") for name in ("L01 Bilanz.md", "L02 GuV.md")]
        self.assertIn("[[SL01 p.1]]", first)
        self.assertIn("SL01: see L01", second)
        self.assertNotIn("[[SL01 p.1]]", second)

    def test_a_later_export_sends_the_deck_again(self):
        uid = self.lecture("L01")
        knowledge = Knowledge(lectures={uid.uid: {"processed": True, "hash": uid.content_hash}})
        _, result = self.write(knowledge)
        self.assertIn("[[SL01 p.1]]", (result.folder / "attach" / "L02 GuV.md").read_text(encoding="utf-8"))

    def test_past_exams_and_tasks_always_go_along(self):
        _, result = self.write(scope="lecture", lecture_ref="L02")
        shared = (result.folder / "attach" / export.SHARED_FILE).read_text(encoding="utf-8")
        self.assertIn("Erstellen Sie eine Bilanz", shared)
        self.assertIn("Buchungssatz", shared)

    def test_no_shared_documents_no_file(self):
        subject = _deck_subject()
        view = subject_model.load_subject(subject, ProfessorStore(Path(tempfile.mkdtemp())))
        plan = export.plan_export(view, Knowledge())
        result = export.write_export(view, plan, Knowledge(), [], NOW)
        self.assertNotIn(export.SHARED_FILE, [p.name for p in result.attach])

    def test_a_task_attached_to_a_lecture_goes_with_the_shared_file(self):
        document = self.view.document("SL02")
        as_task = dataclasses.replace(document, types=(DOC_TYPES["exercises"],))
        self.assertTrue(export._is_shared(as_task))

    def test_all_in_one_has_data_first_and_the_prompt_last(self):
        _, result = self.write(budget=export.LONG_BUDGET)
        text = export.all_as_one_text(result)
        self.assertTrue(text.startswith('<documents>\n<document index="1">\n<source>01 Overview.md</source>\n<document_content>\n'))
        self.assertTrue(text.endswith(result.prompt))
        self.assertLess(text.index("</documents>"), text.index("You are my personal exam tutor"))
        for index, path in enumerate(result.attach, start=1):
            self.assertIn(f'<document index="{index}">\n<source>{path.name}</source>', text)
        self.assertEqual(text, result.all_in_one.read_text(encoding="utf-8"))

    def test_newest_five_are_kept_and_pdfs_to_convert_stays(self):
        pdfs = self.subject.claude_dir / EXPORT_FOLDER
        pdfs.mkdir()
        (pdfs / "keep.pdf").write_bytes(b"x")
        mine = self.subject.claude_dir / "Meine Sachen"
        mine.mkdir()
        for day in range(7):
            self.write(now=NOW + timedelta(days=day))
        names = sorted(p.name for p in self.subject.claude_dir.iterdir() if p.name not in (EXPORT_FOLDER, "Meine Sachen"))
        self.assertEqual(names, [f"2026-11-{day + 1:02d} 18.30 next" for day in range(2, 7)])
        self.assertTrue((pdfs / "keep.pdf").exists())
        self.assertTrue(mine.exists())

    def test_two_exports_in_one_minute_get_their_own_folders(self):
        _, one = self.write()
        _, two = self.write()
        self.assertNotEqual(one.folder, two.folder)
        self.assertTrue(one.prompt_path.exists() and two.prompt_path.exists())

    def test_claude_md(self):
        _, result = self.write()
        text = (result.folder / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("not delegate it to a local model", text)
        self.assertIn("even where a global instruction says local first", text)
        self.assertIn("PROMPT.md", text)
        self.assertIn("attach/", text)
        self.assertIn("offset and limit", text)
        self.assertIn("part-01.jsonl", text)
        self.assertIn("Subagents per lecture are welcome", text)
        self.assertIn("Compute numeric answers", text)
        self.assertIn(f'"{sys.executable}" -m universe.services.claude_reply "{result.folder}/reply" --subject "Finanzen"', text)
        self.assertIn(f'cd "{ROOT}"', text)
        self.assertIn(f'UNIVERSE_DATA_DIR="{os.environ["UNIVERSE_DATA_DIR"]}"', text)
        self.assertIn("press Import", text)


class SplitTests(unittest.TestCase):
    def setUp(self):
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Lang")
        lecture = self.subject.create_lecture("Riesig")
        make_pdf(lecture.dir("slides") / "VL.pdf", DECK)
        text = "wort " * 54
        segments = [Segment(i * 20.0, i * 20.0 + 1, f"{text}Ende{i}", speaker="lecturer") for i in range(1500)]
        Transcript("Lang.m4a", segments, id="r_long").save(lecture.dir("transcripts"))
        self.view = subject_model.load_subject(self.subject, ProfessorStore(Path(tempfile.mkdtemp())))
        plan = export.plan_export(self.view, Knowledge(), budget=export.LONG_BUDGET)
        self.result = export.write_export(self.view, plan, Knowledge(), [], NOW)
        self.parts = [p for p in self.result.attach if p.name.startswith("L01")]

    def test_parts_are_named_in_order_and_under_the_limit(self):
        self.assertEqual([p.name for p in self.parts], ["L01 Riesig (1).md", "L01 Riesig (2).md"])
        for path in self.parts:
            self.assertLessEqual(path.stat().st_size, export.MAX_FILE_BYTES)

    def test_each_part_says_what_it_continues(self):
        first, second = [p.read_text(encoding="utf-8").splitlines()[0] for p in self.parts]
        self.assertIn("part 1 of 2", first)
        self.assertRegex(second, r"^\(L01 — Riesig: part 2 of 2\. Continues part 1, going on at R01@\d+\.\)$")

    def test_no_line_is_cut_or_lost(self):
        text = "\n".join(p.read_text(encoding="utf-8") for p in self.parts)
        for i in range(1500):
            self.assertEqual(text.count(f" Ende{i}\n"), 1, i)

    def test_a_cut_keeps_a_marker_with_its_line(self):
        for path in self.parts:
            lines = path.read_text(encoding="utf-8").rstrip("\n").splitlines()
            self.assertFalse(lines[-1].startswith("⟨"))

    def test_prompt_lists_the_parts(self):
        for path in self.parts:
            self.assertIn(path.name, self.result.prompt)

    def test_a_long_page_is_not_cut(self):
        lecture = self.view.lectures[0]
        blocks = [("L01", "head\n"), ("SL01.p1", "a" * 500 + "\n"), ("SL01.p2", "b" * 500 + "\n")]
        parts = export._split_lecture(lecture, blocks, limit=400)
        self.assertEqual(len(parts), 3)
        self.assertIn("a" * 500, parts[1])
        self.assertIn("going on at SL01.p2", parts[2])


class PromptTests(Fixture):
    def prompt(self, files=("01 Overview.md", "L01 Bilanz.md"), **options) -> str:
        plan = export.plan_export(self.view, Knowledge(), budget=1, **options)
        return export.build_prompt(self.view, plan, list(files), "x1", NOW)

    def test_no_placeholder_survives(self):
        text = self.prompt()
        for placeholder in ("{subject}", "{exam_sentence}", "{file_list}", "{scope_paragraph}", "{reuse_sentence}", "{record_catalogue}"):
            self.assertNotIn(placeholder, text)

    def test_slides_are_unmistakably_the_professors(self):
        text = self.prompt()
        self.assertIn("SL = my professor's slides. DEFINITIVE. The professor wrote them and the exam is built on them.", text)

    def test_the_catalogue_comes_from_the_validator(self):
        text = self.prompt()
        for line in claude_reply.RECORD_CATALOGUE.splitlines():
            self.assertIn(line.replace("<the export id>", "x1"), text)
        self.assertIn(f'{{"t":"head","schema":"{claude_reply.SCHEMA}","export":"x1","part":1}}', text)

    def test_the_rules_are_in_it_verbatim(self):
        text = self.prompt()
        self.assertIn('You are my personal exam tutor for the university course "Finanzen". My study app UniVerse exported all my material for you as Markdown: 01 Overview.md and L01 Bilanz.md.', text)
        self.assertIn("Missing notes mean NOTHING", text)
        self.assertIn('{"t":"end","records":<number of records in this part>,"more":true}', text)
        self.assertIn(r'write \n in strings, and \" for a straight double quote', text)

    def test_exam_sentence(self):
        self.subject.set_exam(date="2026-11-11")
        self.view = self.load()
        self.assertIn('"Finanzen". My exam is on 2026-11-11, in 10 days. My study app', self.prompt())

    def test_scope_paragraph(self):
        self.assertIn("This export covers L01; 1 more lecture follow in later exports.", self.prompt())
        lectures = [self.lecture("L01"), self.lecture("L02")]
        self.assertEqual(export._span(lectures), "L01 and L02")

    def test_reuse_sentence_only_with_current_knowledge(self):
        self.assertNotIn("Reuse the ids", self.prompt())
        self.assertIn(export.REUSE_SENTENCE, self.prompt(files=("01 Overview.md", export.KNOWLEDGE_FILE)))
        self.assertIn("importance 1 to 3 and why.\n", self.prompt())


def reply_for(export_id: str, slide: str = "SL01") -> str:
    source = [{"id": f"{slide}.p1", "q": DECK[0]}]
    return jsonl(
        {"t": "head", "schema": claude_reply.SCHEMA, "export": export_id, "part": 1},
        {"t": "exam_model", "formats": ["90 min"], "confidence": "low", "why": "wenig", "src": source},
        {"t": "topic", "id": "t_b", "title": "Bilanz", "lectures": ["L01"], "importance": 3, "why": "oft", "src": source},
        {"t": "objective", "id": "L01-o001", "topic": "t_b", "statement": "Passivseite erklären", "importance": 3, "truth": "slides", "src": source},
        {
            "t": "q", "id": "q_L01_001", "objective": "L01-o001", "type": "mc", "stem": "Was zeigt die Passivseite?",
            "options": [{"text": "Mittelherkunft", "correct": True}, {"text": "Mittelverwendung", "why_wrong": "Aktivseite"}],
            "explain": "Folie", "difficulty": "easy", "bloom": "remember", "src": source,
        },
        {"t": "said", "id": "s_01", "lecture": "L01", "kind": "exam_hint", "text": "Kommt dran", "slide": f"{slide}.p1", "src": [{"id": "R01@5", "q": "Das kommt in der Klausur dran"}]},
        {"t": "label", "from": "R01@30", "category": "lecture", "exam": False, "reason": "Fallaufgabe"},
        {"t": "note_fix", "lecture": "L01", "line": "N01.1", "verdict": "ok", "fix": "", "src": [{"id": "N01.1", "q": "Die Bilanz hat zwei Seiten"}]},
        {"t": "coverage", "lecture": "L01", "covered": [f"{slide}.p1-3", "R01"], "uncovered": []},
        head=None,
        end={"t": "end", "records": 8, "more": False},
    )  # fmt: skip


class RoundTripTests(Fixture):
    def test_export_reply_merge(self):
        plan, result = self.write(scope="lecture", lecture_ref="L01")
        self.assertIsNotNone(claude_reply.load_manifest(self.subject, result.export_id))
        store = StudyStore(self.subject)
        text = reply_for(result.export_id)
        checked = check_reply(parse_reply(text), self.view, store.known_ids())
        self.assertEqual(checked.errors, [], checked.errors)
        self.assertEqual(checked.warnings, [], checked.warnings)
        self.assertEqual(checked.uncovered, {})
        self.assertEqual(checked.manifest.export_id, result.export_id)
        summary = store.merge(checked, text, self.view)
        self.assertEqual(summary.lectures_done, ["L01"])
        knowledge = store.knowledge()
        self.assertTrue(knowledge.objectives["L01-o001"]["verified"])
        self.assertTrue(knowledge.questions["q_L01_001"]["verified"])
        self.assertEqual(knowledge.questions["q_L01_001"]["sources"][0]["label"], "SL01.p1")
        self.assertEqual(knowledge.lectures[self.lecture().uid]["export"], result.export_id)

    def test_every_id_printed_can_be_cited(self):
        """A line in the file is a line the manifest knows, including ones marked ?."""
        _, result = self.write(scope="lecture", lecture_ref="L01")
        manifest = claude_reply.load_manifest(self.subject, result.export_id)
        self.assertIn("R01", manifest.recordings)
        self.assertEqual(manifest.lectures["L01"]["hash"], self.lecture().content_hash)
        self.assertEqual(manifest.documents["SL01"]["pages"], 3)

    def test_the_next_round_knows_what_came_before(self):
        _, result = self.write(scope="lecture", lecture_ref="L01")
        store = StudyStore(self.subject)
        text = reply_for(result.export_id)
        store.merge(check_reply(parse_reply(text), self.view, store.known_ids()), text, self.view)
        store.record_attempt(store.knowledge().questions["q_L01_001"], "Mittelverwendung", 0.0, "sure", 0, 1000, "today", "auto")
        self.view = self.load()
        plan, second = self.write(store.knowledge(), store.events(), now=NOW + timedelta(hours=1))
        self.assertEqual([v.ref for v in plan.lectures], ["L02"])
        names = [p.name for p in second.attach]
        self.assertIn(export.PROGRESS_FILE, names)
        self.assertIn(export.KNOWLEDGE_FILE, names)
        known = (second.folder / "attach" / export.KNOWLEDGE_FILE).read_text(encoding="utf-8")
        for expected in ("t_b", "L01-o001", "q_L01_001 → L01-o001: Was zeigt die Passivseite?", "- L01"):
            self.assertIn(expected, known)
        self.assertIn(export.REUSE_SENTENCE, second.prompt)
        self.assertIn("Mittelverwendung", (second.folder / "attach" / export.PROGRESS_FILE).read_text(encoding="utf-8"))


class OverviewTests(Fixture):
    def test_everything_is_listed(self):
        self.subject.set_exam(date="2027-02-12", duration_min=90, aids="Taschenrechner")
        self.subject.update_meta(description="Buchführung im ersten Semester")
        self.view = self.load()
        plan = export.plan_export(self.view, Knowledge(), scope="lecture", lecture_ref="L01")
        text = export.render_overview(self.view, plan)
        for expected in (
            "Buchführung im ersten Semester", "Date: 2027-02-12", "Duration: 90 minutes", "Aids: Taschenrechner",
            "### PR1", "Klausur mit Rechenaufgaben", "SL are my professor's slides", "| SL01 | VL.pdf | Slides |  | 3 | L01, L02 | 0/3 |",
            "| EX03 | K.pdf |", "| L01 | Bilanz |", "| yes | new |", "| no | new |", "## Ids", "R07@751",
        ):  # fmt: skip
            self.assertIn(expected, text)


class CurrentKnowledgeTests(Fixture):
    def test_what_it_lists(self):
        uid = self.lecture().uid
        knowledge = Knowledge(
            exam_model={"formats": ["90 min"], "confidence": "low"},
            topics={"t_a": {"title": "Bilanz", "importance": 3, "lectures": [uid]}, "t_old": {"title": "Alt", "retired": True}},
            objectives={"L01-o1": {"statement": "Erklären", "importance": 2, "topic": "t_a"}},
            questions={"q_1": {"objective": "L01-o1", "stem": "x" * 200, "flagged": "falsche Lösung"}},
            conflicts={"c_1": {"a": {"label": "SL01.p1"}, "b": {"label": "R01@5"}, "issue": "Zahl", "working": "a", "needs_student_check": True}},
            lectures={uid: {"processed": True, "uncovered": [{"page": "SL01.p2", "why": ""}]}},
        )
        text = export.render_current_knowledge(knowledge, self.view)
        for expected in ("Reuse these ids", "- formats:", "t_a: Bilanz, importance 3, L01", "L01-o1 (2, t_a): Erklären", "falsche Lösung", "SL01.p1 against R01@5", "not covered: SL01.p2"):
            self.assertIn(expected, text)
        self.assertNotIn("t_old", text)
        self.assertIn("x" * 80, text)
        self.assertNotIn("x" * 81, text)


class TutorChatExportTests(Fixture):
    def chat(self, chat_id: str, topic: str, *texts: str) -> None:
        for n, text in enumerate(texts):
            tutor.log_turn(self.subject, chat_id, "quiz", topic, tutor.Turn("user" if n % 2 == 0 else "assistant", text))

    def test_no_chats_no_file(self):
        _, result = self.write()
        self.assertNotIn(export.CHATS_FILE, [p.name for p in result.attach])

    def test_chats_go_along_with_the_rules_for_them(self):
        self.chat("c1", "Bilanz", "Was ist die Passivseite?", "Sie zeigt die Mittelherkunft.", "Also das Vermögen?")
        plan, result = self.write()
        self.assertIn(export.CHATS_FILE, [p.name for p in result.attach])
        chats = (result.folder / "attach" / export.CHATS_FILE).read_text(encoding="utf-8")
        self.assertIn("ME: Also das Vermögen?", chats)
        self.assertIn("TUTOR: Sie zeigt die Mittelherkunft.", chats)
        self.assertIn("never cite them", chats)
        self.assertIn("Tutor chats = my chats with a small local AI", result.prompt)
        self.assertIn(export.CHATS_FILE, result.prompt)

    def test_a_tutor_round_reads_the_chats_too(self):
        self.chat("c1", "Bilanz", "Was ist die Passivseite?", "Die Herkunft der Mittel.")
        text = export.tutor_questions_prompt(self.view, Knowledge(), [], NOW)
        self.assertIn("<source>My chats with the local AI tutor</source>", text)
        self.assertIn("ME: Was ist die Passivseite?", text)


class TutorTests(Fixture):
    def setUp(self):
        super().setUp()
        lecture = self.lecture()
        document = self.view.document("SL01")
        recording = lecture.recordings[0]
        self.page = {"kind": "page", "doc": document.key, "page": 1, "label": "SL01.p1", "quote": "", "verified": True}
        self.line = {"kind": "line", "rec": recording.uid, "t": 410.0, "end": 412.0, "label": "R01@410", "quote": "", "verified": True}
        objective = {"id": "L01-o1", "topic": "t_a", "statement": "Passivseite erklären", "importance": 3, "sources": [self.page, self.line], "lectures": [lecture.uid], "verified": True}
        question = {
            "id": "q_1", "objective": "L01-o1", "topic": "t_a", "pool": "practice", "type": "mc", "stem": "Was zeigt die Passivseite?",
            "options": [{"text": "Mittelherkunft", "correct": True}, {"text": "Mittelverwendung", "why_wrong": "Aktiv"}], "sources": [self.page], "verified": True, "rev": 1,
        }  # fmt: skip
        self.knowledge = Knowledge(topics={"t_a": {"id": "t_a", "title": "Bilanz", "importance": 3}}, objectives={"L01-o1": objective}, questions={"q_1": question})
        self.events = [{"v": 1, "id": "a_1", "at": "2026-10-30T10:00:00", "type": "attempt", "q": "q_1", "o": "L01-o1", "topic": "t_a", "rev": 1, "mode": "today", "answer": "Mittelverwendung", "correct": False, "score": 0.0, "confidence": "sure", "hints": 0, "ms": 1000, "by": "auto", "rating": 1}]

    def test_questions_round_is_self_contained(self):
        text = export.tutor_questions_prompt(self.view, self.knowledge, self.events, NOW)
        self.assertLess(text.index("</documents>"), text.index("<task>"))
        self.assertIn("my professor's slides", text)
        self.assertIn("# My progress", text)
        self.assertIn(f"Source SL01.p1:\n[[SL01 p.1]]\n{DECK[0]}", text)
        self.assertIn('Asked before: "Was zeigt die Passivseite?"', text)
        self.assertIn("never reuse a stem", text)
        self.assertIn("grade: one for each free answer", text)
        self.assertIn("tutor_note", text)
        self.assertIn('"t":"grade"', text)
        self.assertNotIn('"t":"label"', text)
        self.assertNotIn("{stamp}", text)
        self.assertNotIn("{catalogue}", text)

    def test_cited_lines_come_with_their_neighbours(self):
        text = export.tutor_questions_prompt(self.view, self.knowledge, self.events, NOW)
        self.assertIn("Source R01@410:\n", text)
        section = text.split("Source R01@410:\n")[1]
        for neighbour in ("R01@410 ★you Das habe ich", "R01@400 Nach mehr", "R01@30 ? UNCERTAINTEXT"):
            self.assertIn(neighbour, section)

    def test_private_talk_is_not_in_a_tutor_prompt(self):
        cited = dict(self.line, t=50.0, quote="PRIVATETEXT mein Geheimnis")
        self.knowledge.objectives["L01-o1"]["sources"] = [cited, self.line]
        for build in (export.tutor_questions_prompt, export.tutor_session_prompt):
            text = build(self.view, self.knowledge, self.events, NOW)
            for word in ("PRIVATETEXT", "PRIVATEEXAM", "SIDETALKTEXT", "JUDGEDTEXT"):
                self.assertNotIn(word, text)

    def test_session_is_socratic_and_returns_records(self):
        text = export.tutor_session_prompt(self.view, self.knowledge, self.events, NOW)
        for expected in ("Socratic", "One question at a time", "hint before", "about 15 questions", '"t":"attempt"', '"t":"tutor_note"', '"t":"q"'):
            self.assertIn(expected, text)
        self.assertNotIn('"t":"grade"', text)

    def test_stays_under_the_size_limit_and_says_what_was_cut(self):
        pages = [dict(self.page, page=1)]
        big_page = "Wort " * 4000
        document = self.view.document("SL01")
        huge = dataclasses.replace(document, pages=(PageView(1, big_page), *document.pages[1:]))
        view = dataclasses.replace(self.view, documents=(huge, *[d for d in self.view.documents if d.ref != "SL01"]))
        objectives = {}
        for i in range(600):
            objectives[f"L01-o{i}"] = {"id": f"L01-o{i}", "topic": "t_a", "statement": "Erklären " * 40, "importance": 3, "sources": pages, "lectures": [], "verified": True}
        questions = {
            f"q_{i}": {"id": f"q_{i}", "objective": f"L01-o{i}", "topic": "t_a", "pool": "practice", "type": "card", "stem": f"Frage {i} " + "lang " * 30, "answer": "x", "sources": pages, "verified": True, "rev": 1}
            for i in range(600)
        }
        knowledge = Knowledge(topics=self.knowledge.topics, objectives=objectives, questions=questions)
        for build in (export.tutor_questions_prompt, export.tutor_session_prompt):
            text = build(view, knowledge, [], NOW)
            self.assertLess(export.estimate_tokens(text), export.TUTOR_TOKENS)
            self.assertIn("left out to keep this short", text)

    def test_works_with_nothing_practised(self):
        text = export.tutor_questions_prompt(self.view, self.knowledge, [], NOW)
        self.assertIn("L01-o1", text)
        self.assertIn("[[SL01 p.1]]", text)

    def test_a_reply_to_the_prompt_can_be_checked(self):
        reply = jsonl(
            {"t": "head", "schema": claude_reply.SCHEMA, "export": "tutor", "part": 1},
            {"t": "tutor_note", "topic": "t_a", "diagnosis": "verwechselt die Seiten", "next": "T-Konto üben"},
            head=None,
            end={"t": "end", "records": 1, "more": False},
        )
        known = {"topics": {"t_a"}, "objectives": {"L01-o1"}, "questions": {"q_1"}}
        checked = check_reply(parse_reply(reply), self.view, known)
        self.assertEqual(checked.errors, [])
        self.assertEqual(checked.warnings, [])  # a tutor round has no manifest, and that is no news


class TokenTests(unittest.TestCase):
    def test_estimate(self):
        self.assertEqual(export.estimate_tokens("a" * 400), 100)
        self.assertEqual((export.CHAT_BUDGET, export.LONG_BUDGET), (180_000, 800_000))


class ManifestFileTests(Fixture):
    def test_saved_as_json_without_any_talk(self):
        _, result = self.write(scope="lecture", lecture_ref="L01", include_side_talk=True)
        raw = json.loads((self.subject.study_dir / "exports" / f"{result.export_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["export_id"], result.export_id)
        self.assertRegex(result.export_id, r"^x20261101-1830-[0-9a-f]{4}$")
        self.assertNotIn("PRIVATETEXT", json.dumps(raw))


if __name__ == "__main__":
    unittest.main()
