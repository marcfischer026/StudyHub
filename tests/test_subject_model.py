"""Tests for the read model: stable ids, one view per document, what counts as private."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())

import pymupdf  # noqa: E402

from universe.professors import ProfessorProfile, ProfessorStore  # noqa: E402
from universe.services import pdf_conversion, pdf_tools, subject_model  # noqa: E402
from universe.services.transcriber import Segment, Transcript  # noqa: E402
from universe.storage import Workspace, write_json, write_slide_marks  # noqa: E402


def make_pdf(path: Path, pages: list[str], highlight: str = "") -> Path:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((50, 60), text)
        if highlight and highlight in text:
            page.add_highlight_annot(page.search_for(highlight)[0])
    doc.save(path)
    doc.close()
    return path


class Fixture(unittest.TestCase):
    def setUp(self):
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finanzen")
        self.store = ProfessorStore(Path(tempfile.mkdtemp()))
        self.first = self.subject.create_lecture("Woche 1")
        self.second = self.subject.create_lecture("Woche 2")

    def load(self, **options):
        return subject_model.load_subject(self.subject, self.store, **options)


class DocumentTests(Fixture):
    def test_copies_of_a_deck_are_one_document_with_the_marks_of_both(self):
        make_pdf(self.first.dir("slides") / "VL.pdf", ["Aktiva", "Passiva"], highlight="Aktiva")
        make_pdf(self.second.dir("slides") / "VL neu.pdf", ["Aktiva", "Passiva"], highlight="Passiva")
        view = self.load()
        self.assertEqual(len(view.documents), 1)
        document = view.documents[0]
        self.assertEqual(document.ref, "SL01")
        self.assertEqual(document.lecture_refs, ("L01", "L02"))
        self.assertEqual([m.text for m in document.page(1).marks], ["Aktiva"])
        self.assertEqual([m.text for m in document.page(2).marks], ["Passiva"])
        self.assertEqual(view.lecture("L02").document_refs, ("SL01",))

    def test_the_prefix_follows_the_tags_and_the_number_stays(self):
        make_pdf(self.first.dir("slides") / "VL.pdf", ["Aktiva"])
        make_pdf(self.subject.dir("exams") / "K.pdf", ["Klausur"])
        view = self.load()
        self.assertEqual([d.ref for d in view.documents], ["SL01", "EX02"])
        self.assertTrue(view.document("EX02").whole_subject)
        self.first.documents()[0].set_tags(["Task"])
        view = self.load()
        self.assertEqual(view.documents[0].ref, "TK01")
        self.assertIs(view.document("SL01"), view.documents[0])  # old citations keep working by number

    def test_numbers_are_never_reused(self):
        pdf = make_pdf(self.first.dir("slides") / "A.pdf", ["Aktiva"])
        make_pdf(self.first.dir("slides") / "B.pdf", ["Passiva"])
        self.assertEqual([d.ref for d in self.load().documents], ["SL01", "SL02"])
        pdf.unlink()
        make_pdf(self.first.dir("slides") / "C.pdf", ["Eigenkapital"])
        self.assertEqual([d.ref for d in self.load().documents], ["SL02", "SL03"])

    def test_exclusion_is_remembered_by_content(self):
        make_pdf(self.first.dir("slides") / "Mathe.pdf", ["Integral"])
        key = self.load().documents[0].key
        self.subject.set_document_excluded(key, True)
        view = self.load()
        self.assertTrue(view.documents[0].excluded)
        self.assertEqual(view.documents_for_export, ())

    def test_converted_markdown_wins_and_is_checked(self):
        make_pdf(self.first.dir("slides") / "VL.pdf", ["Aktiva 1200 Euro", "Passiva"])
        view = self.load()
        document = view.documents[0]
        pdf_conversion.save_pages(self.subject, document.key, document.title, document.ref, {1: "## Aktiva\n\n1200 Euro"})
        document = self.load().documents[0]
        self.assertEqual(document.converted_pages, 1)
        self.assertFalse(document.is_converted)
        self.assertEqual(document.page(1).text, "## Aktiva\n\n1200 Euro")
        self.assertTrue(document.page(1).check.ok)
        self.assertEqual(document.page(2).text, "Passiva")
        self.assertIsNone(document.page(2).check)

    def test_fast_load_has_no_pages_but_knows_the_counts(self):
        make_pdf(self.first.dir("slides") / "VL.pdf", ["Aktiva", "Passiva"])
        view = self.load(pages=False, recordings=False)
        self.assertEqual(view.documents[0].page_count, 2)
        self.assertEqual(view.documents[0].pages, ())


class RecordingTests(Fixture):
    def test_ids_survive_renames_and_saving(self):
        audio = self.first.dir("audio") / "FIN 2026-10-29 12.00.wav"
        audio.write_bytes(b"")
        Transcript(audio.name, [Segment(0, 2, "Hallo")]).save(self.first.dir("transcripts"))
        view = self.load()
        uid = view.lecture("L01").recordings[0].uid
        self.first.rename_audio(audio, "Neu 2026-10-29 12.00")
        view = self.load()
        recording = view.lecture("L01").recordings[0]
        self.assertEqual((recording.ref, recording.uid, recording.stem), ("R01", uid, "Neu 2026-10-29 12.00"))

    def test_visibility_says_who_decided_it_is_off_topic_and_about_which_voice(self):
        Transcript(
            "a.wav",
            [
                Segment(0, 1, "Inhalt", category="lecture"),
                Segment(1, 2, "Fallstudie", category="off_topic", source="local", speaker="lecturer"),
                Segment(2, 3, "Unbekannt", category="off_topic", source="local"),
                Segment(3, 4, "Kino?", category="off_topic", source="local", speaker="speaker_2"),
                Segment(4, 5, "Nein", category="off_topic", source="claude"),
                Segment(5, 6, "Privat", category="off_topic", source="manual"),
            ],
        ).save(self.first.dir("transcripts"))
        segments = self.load().lecture("L01").recordings[0].segments
        self.assertEqual(
            [s.visibility for s in segments], ["normal", "uncertain", "uncertain", "side_talk", "judged", "private"]
        )

    def test_someone_by_the_microphone_counts_as_side_talk(self):
        segments = [Segment(i, i + 1, f"Satz {i}", loudness=-30.0) for i in range(4)]
        segments.append(Segment(4, 5, "Gehen wir essen?", category="off_topic", source="local", loudness=-5.0))
        Transcript("a.wav", segments).save(self.first.dir("transcripts"))
        recording = self.load().lecture("L01").recordings[0]
        self.assertEqual(recording.segments[4].visibility, "side_talk")

    def test_lines_are_paragraphs_named_by_their_second(self):
        Transcript(
            "a.wav",
            [
                Segment(0, 2, "Die Bilanz"),
                Segment(2, 4, "hat zwei Seiten."),
                Segment(4.5, 6, "Das kommt dran.", exam=True, source="rules"),
                Segment(6, 8, "Wirklich?", category="question", speaker="speaker_2"),
                Segment(30, 32, "Weiter nach einer Pause."),
                Segment(32, 34, "Privat", category="off_topic", source="manual"),
                Segment(34, 36, "Danach geht es weiter."),
            ],
        ).save(self.first.dir("transcripts"))
        lines = self.load().lecture("L01").recordings[0].lines
        self.assertEqual(
            [(l.ref, l.text) for l in lines],
            [
                ("R01@0", "Die Bilanz hat zwei Seiten."),
                ("R01@4", "Das kommt dran."),
                ("R01@6", "Wirklich?"),
                ("R01@30", "Weiter nach einer Pause."),
                ("R01@34", "Danach geht es weiter."),
            ],
        )
        self.assertEqual(lines[1].exam, "machine")
        self.assertEqual(lines[2].category, "question")
        self.assertEqual(lines[2].speaker_tag, "[Speaker 2]")

    def test_a_long_stretch_breaks_up_and_ids_stay_unique(self):
        segments = [Segment(i * 0.4, i * 0.4 + 0.4, "wort " * 12) for i in range(30)]
        Transcript("a.wav", segments).save(self.first.dir("transcripts"))
        lines = self.load().lecture("L01").recordings[0].lines
        self.assertGreater(len(lines), 1)
        self.assertEqual(len({l.ref for l in lines}), len(lines))

    def test_page_turns_become_slide_refs_and_a_flicker_is_ignored(self):
        make_pdf(self.first.dir("slides") / "VL.pdf", ["Aktiva", "Passiva", "Eigenkapital"])
        audio = self.first.dir("audio") / "FIN 2026-10-29 12.00.wav"
        audio.write_bytes(b"")
        Transcript(audio.name, [Segment(0, 1, "a"), Segment(30, 31, "b"), Segment(61, 62, "c")]).save(self.first.dir("transcripts"))
        write_slide_marks(
            audio,
            [
                {"offset": 0, "doc": "slides", "pdf": "VL.pdf", "page": 1},
                {"offset": 20, "doc": "slides", "pdf": "VL.pdf", "page": 2},  # scrolled past
                {"offset": 21, "doc": "slides", "pdf": "VL.pdf", "page": 3},
            ],
        )
        segments = self.load().lecture("L01").recordings[0].segments
        self.assertEqual([s.slide for s in segments], ["SL01.p1", "SL01.p3", "SL01.p3"])

    def test_untranscribed_recordings_are_listed(self):
        (self.first.dir("audio") / "Memo.wav").write_bytes(b"")
        self.assertEqual(self.load().lecture("L01").untranscribed, ("Memo",))

    def test_transcribing_again_keeps_id_and_manual_labels(self):
        old = Transcript("a.wav", [Segment(0, 4, "Privat", category="off_topic", source="manual", note="Set by you")], id="r_x")
        new = Transcript("a.wav", [Segment(0.5, 3.5, "Privat neu", category="lecture"), Segment(5, 6, "Rest")])
        new.carry_over(old)
        self.assertEqual(new.id, "r_x")
        self.assertTrue(new.segments[0].private)
        self.assertEqual(new.segments[0].text, "Privat neu")
        self.assertEqual(new.segments[1].source, "")

    def test_a_private_line_stays_private_when_it_is_split(self):
        old = Transcript("a.wav", [Segment(0, 10, "Privat", category="off_topic", source="manual")], id="r_x")
        new = Transcript("a.wav", [Segment(0, 5, "Privat eins"), Segment(5, 10, "Privat zwei"), Segment(10, 12, "Vorlesung")])
        new.carry_over(old)
        self.assertEqual([s.private for s in new.segments], [True, True, False])

    def test_no_other_label_takes_a_private_mark_back(self):
        old = Transcript(
            "a.wav",
            [Segment(0, 5, "Frage", category="question", source="manual"), Segment(5, 10, "Privat", category="off_topic", source="manual")],
            id="r_x",
        )
        new = Transcript("a.wav", [Segment(2, 8, "Beides")])
        new.carry_over(old)
        self.assertTrue(new.segments[0].private)


class SpeakerResultTests(unittest.TestCase):
    def test_a_label_set_during_speaker_recognition_survives(self):
        recognised = Transcript("a.wav", [Segment(0, 4, "Hallo", speaker="lecturer", loudness=0.5)])
        now = Transcript("a.wav", [Segment(0, 4, "Hallo", category="off_topic", source="manual")])
        now.take_speakers(recognised)
        self.assertEqual((now.segments[0].speaker, now.segments[0].loudness), ("lecturer", 0.5))
        self.assertEqual((now.segments[0].category, now.segments[0].source), ("off_topic", "manual"))


class RegistryRecoveryTests(Fixture):
    def setUp(self):
        super().setUp()
        self.registry = self.subject.study_dir / "ids.json"
        (self.subject.study_dir / "exports").mkdir()
        write_json(self.subject.study_dir / "exports" / "x1.json", {
            "documents": {"SL01": {"key": "d_a", "pages": 1, "empty": []}, "EX03": {"key": "d_b", "pages": 1, "empty": []}},
            "lectures": {"L02": {"uid": "l_a", "hash": "", "documents": []}},
            "recordings": {"R05": {"uid": "r_a", "lecture": "l_a", "lines": []}},
        })

    def test_a_broken_file_does_not_restart_the_numbering(self):
        self.registry.write_text("kaputt", encoding="utf-8")
        registry = subject_model.IdRegistry(self.subject)
        self.assertEqual((registry.number("documents", "d_b"), registry.number("lectures", "l_a"), registry.number("recordings", "r_a")), (3, 2, 5))
        self.assertEqual(registry.number("documents", "d_new"), 4)
        self.assertEqual(self.registry.with_name("ids.broken.json").read_text(encoding="utf-8"), "kaputt")

    def test_a_lost_file_does_not_restart_the_numbering(self):
        self.assertEqual(subject_model.IdRegistry(self.subject).number("documents", "d_new"), 4)


class NotesTests(Fixture):
    def test_lines_get_ids_tags_and_links_by_id(self):
        make_pdf(self.first.dir("slides") / "VL 1.pdf", ["Aktiva", "Passiva"])
        audio = self.first.dir("audio") / "FIN 2026-10-29 12.00.wav"
        audio.write_bytes(b"")
        Transcript(audio.name, [Segment(0, 5, "Hallo"), Segment(5, 9, "Bilanz")]).save(self.first.dir("transcripts"))
        self.first.write_notes("# Woche 1\n\nkommt **dran**\n\nSiehe [→ VL 1, slide 2](slide:slides/VL%201.pdf#2)\n\nohne Anker\n")
        self.first.write_anchors(
            [{"text": "kommt dran", "audio": audio.stem, "t": 6.0, "clock": "12:00", "doc": "slides", "pdf": "VL 1.pdf", "page": 2}]
        )
        lecture = self.load().lecture("L01")
        self.assertTrue(lecture.has_own_notes)
        by_ref = {n.ref: n for n in lecture.notes}
        self.assertEqual(by_ref["N01.2"].tags, ("SL01.p2", "12:00", "R01@0"))
        self.assertEqual(by_ref["N01.3"].text, "Siehe (→ SL01.p2)")
        self.assertEqual(by_ref["N01.4"].tags, ())

    def test_renaming_a_recording_keeps_its_note_anchors(self):
        audio = self.first.dir("audio") / "Memo.wav"
        audio.write_bytes(b"")
        Transcript(audio.name, [Segment(0, 5, "Hallo")]).save(self.first.dir("transcripts"))
        self.first.write_notes("Zeile\n")
        self.first.write_anchors([{"text": "Zeile", "audio": "Memo", "t": 1.0}])
        self.first.rename_audio(audio, "Neu")
        self.assertEqual(self.first.read_anchors()[0]["audio"], "Neu")
        self.assertIn("R01@0", self.load().lecture("L01").notes[0].tags)

    def test_only_the_starting_heading_does_not_count_as_notes(self):
        self.assertFalse(self.load().lecture("L01").has_own_notes)

    def test_a_figure_keeps_source_text_and_tables_without_repeating_them(self):
        make_pdf(self.first.dir("slides") / "VL.pdf", ["Aktiva"])
        png = self.first.add_figure(
            b"png", {"pdf": "VL.pdf", "page": 1, "doc": "slides", "caption": "VL.pdf, slide 1", "text": "A", "tables": ["|A|B|\n|---|---|\n|1|2|"]}
        )
        self.first.write_notes(f"![VL.pdf, slide 1](figures/{png.name})\n\n*VL.pdf, slide 1*\n\n|A|B|\n|---|---|\n|1|2|\n\nmein Text\n")
        notes = self.load().lecture("L01").notes
        self.assertEqual(notes[0].figure.ref, "F01.1")
        self.assertEqual(notes[0].figure.source, "SL01.p1")
        self.assertEqual([n.text for n in notes[1:]], ["mein Text"])


class ProfessorAndLocateTests(Fixture):
    def test_professors_with_the_lectures_they_teach(self):
        default = ProfessorProfile(name="Dr. Weber")
        guest = ProfessorProfile(name="Dr. Gast")
        self.store.save(default)
        self.store.save(guest)
        self.subject.update_meta(professor_id=default.id)
        self.second.update_meta(professor_id=guest.id)
        view = self.load()
        self.assertEqual([(p.profile.name, p.lecture_refs, p.is_default) for p in view.professors],
                         [("Dr. Weber", ("L01",), True), ("Dr. Gast", ("L02",), False)])
        self.assertEqual(view.lecture("L02").professor_ref, view.professors[1].ref)

    def test_locate_finds_pages_and_moments_after_a_rename(self):
        make_pdf(self.first.dir("slides") / "VL.pdf", ["Aktiva"])
        make_pdf(self.subject.dir("exams") / "K.pdf", ["Klausur"])
        Transcript("a.wav", [Segment(0, 1, "x")]).save(self.first.dir("transcripts"))
        view = self.load()
        key = view.documents[0].key
        uid = view.lecture("L01").recordings[0].uid
        self.subject.rename_lecture(self.first, "Woche Eins")
        view = self.load()
        found = subject_model.locate(view, {"kind": "page", "doc": key, "page": 1})
        self.assertEqual((found["doc"], found["pdf"], found["page"], found["lecture"].name), ("slides", "VL.pdf", 1, "Woche Eins"))
        exam = subject_model.locate(view, {"kind": "page", "doc": view.documents[1].key, "page": 1})
        self.assertIsNone(exam["lecture"])
        moment = subject_model.locate(view, {"kind": "line", "rec": uid, "t": 0.5})
        self.assertEqual((moment["audio"], moment["t"]), ("a", 0.5))
        self.assertIsNone(subject_model.locate(view, {"kind": "page", "doc": "d_gone", "page": 1}))


class PdfToolsTests(unittest.TestCase):
    def test_a_highlight_reads_only_its_own_lines_and_a_box_may_be_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.pdf"
            doc = pymupdf.open()
            page = doc.new_page()
            page.insert_text((50, 60), "links wichtig rechts")
            highlight = page.add_highlight_annot(page.search_for("wichtig")[0])
            highlight.set_colors(stroke=(0.56, 0.93, 0.47))
            highlight.update()
            box = page.add_rect_annot(pymupdf.Rect(50, 300, 200, 400))
            box.set_colors(stroke=(1.0, 0.6, 0.8))
            box.update()
            doc.save(path)
            doc.close()
            marks = pdf_tools.read_pages(path)[0].marks
        self.assertEqual(sorted((m.text, m.color) for m in marks), [("", "pink"), ("wichtig", "green")])

    def test_symbol_font_bullets_are_cleaned(self):
        self.assertEqual(pdf_tools.clean_text("Soll"), "•Soll")


if __name__ == "__main__":
    unittest.main()
