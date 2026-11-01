"""Tests for reading Claude's reply: tolerant parsing, citations, quotes, the hierarchy, and what goes back."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())

import pymupdf  # noqa: E402

from universe.professors import ProfessorProfile, ProfessorStore  # noqa: E402
from universe.services import claude_reply, pdf_conversion, subject_model  # noqa: E402
from universe.services.claude_reply import check_reply, follow_up_prompt, normalise, parse_reply, quote_found, repair_prompt  # noqa: E402
from universe.services.transcriber import Segment, Transcript  # noqa: E402
from universe.storage import Workspace  # noqa: E402

PAGES = ["Die Passivseite zeigt die Mittelherkunft", "Die Aktivseite zeigt die Mittelverwendung", "", "Agenda heute"]
PASSIV = "Die Passivseite zeigt die Mittelherkunft"
HEAD = {"t": "head", "schema": claude_reply.SCHEMA, "export": "x1", "part": 1}
END = {"t": "end", "records": 1, "more": False}
TOPIC = {"t": "topic", "id": "t_b", "title": "Bilanz", "lectures": ["L01"], "importance": 3, "why": "oft geprüft", "src": [{"id": "SL01.p1", "q": PASSIV}]}
OBJECTIVE = {"t": "objective", "id": "L01-o001", "topic": "t_b", "statement": "Passivseite erklären", "importance": 3, "truth": "slides", "src": [{"id": "SL01.p1", "q": PASSIV}]}
MC = {
    "t": "q", "id": "q_1", "objective": "L01-o001", "type": "mc", "stem": "Was zeigt die Passivseite?",
    "options": [{"text": "Mittelherkunft", "correct": True}, {"text": "Mittelverwendung", "why_wrong": "Das ist die Aktivseite"}],
    "explain": "Siehe Folie", "difficulty": "easy", "bloom": "remember", "src": [{"id": "SL01.p1", "q": PASSIV}],
}  # fmt: skip


def make_pdf(path: Path, pages: list[str]) -> Path:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((50, 60), text)
    doc.save(path)
    doc.close()
    return path


def jsonl(*records: dict, head: dict | None = HEAD, end: dict | None = END) -> str:
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in ([head] if head else []) + list(records) + ([end] if end else []))


def build_subject(subject, lecture) -> None:
    """A deck with an empty page, a past exam, one recording with a private line, notes."""
    make_pdf(lecture.dir("slides") / "VL.pdf", PAGES)
    make_pdf(subject.dir("exams") / "K.pdf", ["Aufgabe eins Erstellen Sie eine Bilanz"])
    segments = [
        Segment(751.2, 754.8, "Die Passivseite zeigt die Mittelherkunft und zwar immer", speaker="lecturer"),
        Segment(797.0, 800.0, "Das kommt in der Klausur dran", speaker="lecturer"),
        Segment(948.0, 950.0, "Eine Frage dazu", speaker="lecturer"),
        Segment(970.0, 972.0, "Dazwischen etwas", speaker="lecturer"),
        Segment(995.0, 997.0, "Und die Antwort", speaker="lecturer"),
        Segment(2000.0, 2001.0, "Privat", category="off_topic", source="manual"),
    ]
    Transcript("Woche.m4a", segments, id="r_one").save(lecture.dir("transcripts"))
    Transcript("Zweite.m4a", [Segment(10.0, 12.0, "Zweite Aufnahme")], id="r_two").save(lecture.dir("transcripts"))
    lecture.write_notes("Die Bilanz hat **zwei** Seiten\n")


class Fixture(unittest.TestCase):
    """L01 with SL01 (four pages, the third empty), EX02, R01 and R02, N01.1, PR1; export x1."""

    def setUp(self):
        self.professors = ProfessorStore(Path(tempfile.mkdtemp()))
        self.profile = ProfessorProfile("Muster", testing_style="Eigener Text")
        self.professors.save(self.profile)
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finanzen", professor_id=self.profile.id)
        self.lecture = self.subject.create_lecture("Woche 1")
        build_subject(self.subject, self.lecture)
        self.view = self.load()
        claude_reply.save_manifest(self.subject, claude_reply.build_manifest(self.view, list(self.view.lectures), "x1"))

    def load(self):
        return subject_model.load_subject(self.subject, self.professors)

    def check(self, *records: dict, known: dict | None = None, **options):
        return check_reply(parse_reply(jsonl(*records, **options)), self.view, known)

    def messages(self, checked, level: str = "warning") -> str:
        return "\n".join(i.message for i in checked.issues if i.level == level)


class ParseTests(unittest.TestCase):
    def test_fences_and_prose_around_records_are_ignored(self):
        text = "Gern, hier ist es:\n\n```jsonl\n" + jsonl(TOPIC) + "\n```\n\nSag Bescheid, wenn etwas fehlt!"
        parsed = parse_reply(text)
        self.assertEqual([r["t"] for r in parsed.records], ["head", "topic", "end"])
        self.assertEqual(parsed.issues, [])
        self.assertEqual(parsed.export_id, "x1")
        self.assertTrue(parsed.finished)
        self.assertFalse(parsed.truncated)

    def test_objects_may_span_lines(self):
        parsed = parse_reply(json.dumps(HEAD, indent=2) + "\n" + json.dumps(TOPIC, indent=2) + '\n{"t":"said","text":"zwei\nZeilen"}\n' + json.dumps(END))
        self.assertEqual([r["t"] for r in parsed.records], ["head", "topic", "said", "end"])
        self.assertEqual(parsed.records[2]["text"], "zwei\nZeilen")
        self.assertEqual(parsed.issues, [])

    def test_a_json_array_or_records_object_is_read(self):
        for text in (json.dumps([HEAD, TOPIC, END]), json.dumps({"records": [HEAD, TOPIC, END]}), json.dumps({"records": [HEAD, TOPIC, END]}, indent=1)):
            with self.subTest(text=text[:20]):
                parsed = parse_reply(text)
                self.assertEqual([r["t"] for r in parsed.records], ["head", "topic", "end"])
                self.assertEqual(parsed.issues, [])
                self.assertTrue(parsed.finished)

    def test_a_broken_line_costs_only_that_line(self):
        broken = '{"t":"q","id":"q_2","stem":"Er sagte "hallo" und ging, und danach folgte noch ein sehr langer Satz, der weit über die Grenze hinaus geht"}'
        text = "\n".join([json.dumps(HEAD), json.dumps(TOPIC), broken, json.dumps(OBJECTIVE), json.dumps(END)])
        parsed = parse_reply(text)
        self.assertEqual([r["t"] for r in parsed.records], ["head", "topic", "objective", "end"])
        self.assertEqual(len(parsed.issues), 1)
        issue = parsed.issues[0]
        self.assertEqual((issue.level, issue.line), ("error", 3))
        self.assertIn(broken[:60], issue.message)
        self.assertNotIn(broken[90:], issue.message)
        self.assertFalse(parsed.truncated)
        self.assertEqual(parsed.records[2]["_line"], 4)

    def test_an_unclosed_string_does_not_swallow_the_next_line(self):
        text = "\n".join([json.dumps(HEAD), '{"t":"said","text":"offen', json.dumps(TOPIC), json.dumps(END)])
        parsed = parse_reply(text)
        self.assertEqual([r["t"] for r in parsed.records], ["head", "topic", "end"])
        self.assertEqual([i.line for i in parsed.issues], [2])

    def test_several_parts_pasted_together(self):
        first = jsonl(TOPIC, end={"t": "end", "records": 1, "more": True})
        second = jsonl(OBJECTIVE, head={**HEAD, "part": 2})
        parsed = parse_reply(f"Teil 1:\n```jsonl\n{first}\n```\nTeil 2:\n```jsonl\n{second}\n```")
        self.assertEqual(parsed.parts, [1, 2])
        self.assertTrue(parsed.finished)
        self.assertFalse(parsed.truncated)
        self.assertEqual([r.get("_part") for r in parsed.records if r["t"] in ("topic", "objective")], [1, 2])

    def test_truncation(self):
        cases = {
            "no end record": (jsonl(TOPIC, end=None), True, False),
            "broken last line": (jsonl(TOPIC, end=None) + '\n{"t":"objective","id":"L01-o0', True, False),
            "more to come is a pause": (jsonl(TOPIC, end={"t": "end", "records": 1, "more": True}), False, False),
            "complete": (jsonl(TOPIC), False, True),
        }
        for name, (text, truncated, finished) in cases.items():
            with self.subTest(name):
                parsed = parse_reply(text)
                self.assertEqual((parsed.truncated, parsed.finished), (truncated, finished))
                self.assertEqual(any(i.message == claude_reply.CUT_OFF for i in parsed.issues), truncated)

    def test_nothing_usable(self):
        parsed = parse_reply("Ich konnte die Dateien leider nicht lesen.")
        self.assertEqual(parsed.records, [])
        self.assertEqual([i.level for i in parsed.issues], ["error"])

    def test_a_bom_and_windows_line_ends_do_not_matter(self):
        parsed = parse_reply("﻿" + jsonl(TOPIC).replace("\n", "\r\n"))
        self.assertEqual(len(parsed.records), 3)
        self.assertEqual(parsed.issues, [])


class QuoteTests(unittest.TestCase):
    def found(self, quote: str, text: str) -> bool:
        return quote_found(normalise(quote), normalise(text))

    def test_what_pdfs_and_markdown_add_says_nothing(self):
        cases = {
            "soft hyphen": ("Mittelherkunft", "die Mittel­herkunft zeigt"),
            "soft hyphen at a line end": ("Mittelherkunft", "die Mittel­\nherkunft zeigt"),
            "private-use glyph": ("Aktiva und Passiva", "Aktiva  und Passiva"),
            "hyphenation": ("Mittelherkunft des Unternehmens", "die Mittel-\nherkunft des Unternehmens"),
            "bold": ("zeigt die Mittelherkunft", "zeigt **die** Mittelherkunft"),
            "heading and list": ("Bilanz Aktiva", "## Bilanz\n- Aktiva"),
            "table": ("Aktiva Passiva", "| Aktiva | Passiva |"),
            "case": ("DIE BILANZ", "die Bilanz"),
            "whitespace": ("die  Bilanz\nzeigt", "die Bilanz zeigt"),
            "compatibility form": ("fi", "ﬁ"),
            "quotes": ('"Bilanz"', "„Bilanz“"),
        }
        for name, (quote, text) in cases.items():
            with self.subTest(name):
                self.assertTrue(self.found(quote, text))

    def test_fuzzy_fallback_needs_most_of_the_quote_in_one_run(self):
        text = "abcdefghijklmnopqrstuvwxyz0123"
        self.assertTrue(self.found("abcdefghijklmnopXXXX", text))  # 16 of 20
        self.assertFalse(self.found("abcdefghijklmnoXXXXX", text))  # 15 of 20
        self.assertFalse(self.found("abcdefghijXXklmnopqr", text))  # two runs do not add up

    def test_empty_never_matches(self):
        self.assertFalse(quote_found("", "text"))
        self.assertFalse(quote_found("text", ""))

    def test_a_quote_may_leave_out_the_bullets_of_a_list(self):
        self.assertTrue(self.found("Anschaffungskosten Lagerhaltungskosten Fehlmengenkosten", "▪Anschaffungskosten\n▪Lagerhaltungskosten\n▪Fehlmengenkosten"))


class ManifestTests(Fixture):
    def test_round_trip(self):
        manifest = claude_reply.load_manifest(self.subject, "x1")
        self.assertEqual(manifest.lectures["L01"]["uid"], self.lecture.uid)
        self.assertEqual(manifest.lectures["L01"]["hash"], self.view.lectures[0].content_hash)
        self.assertEqual(manifest.documents["SL01"]["pages"], 4)
        self.assertEqual(manifest.recordings["R01"]["uid"], "r_one")
        self.assertEqual(manifest.recordings["R01"]["lines"][0], [751.2, 754.8])
        self.assertEqual(manifest.notes["N01.1"]["lecture"], self.lecture.uid)

    def test_a_pasted_export_id_cannot_leave_the_folder(self):
        for wanted in ("../../subject", "", ".hidden", "a/b"):
            self.assertIsNone(claude_reply.load_manifest(self.subject, wanted))

    def test_only_the_newest_are_kept(self):
        for number in range(claude_reply.KEEP_MANIFESTS + 3):
            claude_reply.save_manifest(self.subject, claude_reply.Manifest(f"m{number}", ""))
        self.assertEqual(len(list((self.subject.study_dir / "exports").glob("*.json"))), claude_reply.KEEP_MANIFESTS)
        self.assertIsNotNone(claude_reply.load_manifest(self.subject, f"m{claude_reply.KEEP_MANIFESTS + 2}"))


class CitationTests(Fixture):
    def source(self, ref: str, quote: str = "") -> tuple[list[dict], object]:
        checked = self.check(TOPIC, {**OBJECTIVE, "truth": "said", "src": [{"id": ref, "q": quote}]})
        return checked.records["objective"][0]["sources"], checked

    def test_a_page_becomes_a_durable_source(self):
        sources, checked = self.source("SL01.p1", PASSIV)
        document = self.view.document("SL01")
        self.assertEqual(sources, [{"kind": "page", "doc": document.key, "page": 1, "label": "SL01.p1", "quote": PASSIV, "verified": True}])
        self.assertEqual(checked.errors, [])

    def test_documents_resolve_by_number_whatever_the_prefix(self):
        for ref in ("SL01.p2", "EX01.p2", "TK01.p2", "DO01.p2", "SL1.p2", "SL001.p2"):
            with self.subTest(ref):
                sources, _ = self.source(ref, "Die Aktivseite zeigt")
                self.assertEqual(sources[0]["doc"], self.view.document(1).key)
                self.assertEqual(sources[0]["page"], 2)
                self.assertEqual(sources[0]["label"], "SL01.p2")
                self.assertTrue(sources[0]["verified"])

    def test_a_retagged_deck_still_resolves_and_shows_its_new_prefix(self):
        self.lecture.documents()[0].set_tags(["Task"])
        self.view = self.load()
        sources, _ = self.source("SL01.p2", "Die Aktivseite zeigt")
        self.assertEqual(sources[0]["label"], "TK01.p2")
        self.assertEqual(sources[0]["doc"], self.view.document(1).key)

    def test_an_id_that_does_not_exist_is_dropped_with_a_warning(self):
        for ref in ("SL09.p1", "SL01.p99", "R09@1", "R01@12345", "N09.1", "F09.1", "PR9", "Quatsch"):
            with self.subTest(ref):
                sources, checked = self.source(ref, "egal")
                self.assertEqual(sources, [])
                self.assertIn(ref, self.messages(checked))
                self.assertEqual(checked.errors, [])
                self.assertEqual(len(checked.records["objective"]), 1)

    def test_a_page_range_stands_on_its_first_page(self):
        sources, _ = self.source("SL01.p1-2", PASSIV)
        self.assertEqual((sources[0]["page"], sources[0]["verified"]), (1, True))

    def test_a_bad_quote_warns_and_the_record_stays(self):
        sources, checked = self.source("SL01.p1", "Das steht hier ganz sicher nicht")
        self.assertFalse(sources[0]["verified"])
        self.assertIn("Quote not found on SL01.p1", self.messages(checked))
        self.assertFalse(checked.records["objective"][0]["verified"])

    def test_a_missing_quote_is_not_verified(self):
        sources, checked = self.source("SL01.p1")
        self.assertFalse(sources[0]["verified"])
        self.assertIn("no quote", self.messages(checked))

    def test_quotes_are_checked_against_the_conversion_too(self):
        document = self.view.document("SL01")
        pdf_conversion.save_pages(self.subject, document.key, document.title, document.ref, {4: "## Tagesordnung\n\n**Nur** in der Umwandlung"})
        self.view = self.load()
        self.assertTrue(self.source("SL01.p4", "Tagesordnung Nur in der Umwandlung")[0][0]["verified"])
        self.assertTrue(self.source("SL01.p4", "Agenda heute")[0][0]["verified"])

    def test_a_line_resolves_through_the_manifest(self):
        sources, _ = self.source("R01@751", "zeigt die Mittelherkunft und zwar")
        self.assertEqual(
            sources,
            [{"kind": "line", "rec": "r_one", "t": 751.2, "end": 754.8, "label": "R01@751", "quote": "zeigt die Mittelherkunft und zwar", "verified": True}],
        )

    def test_a_quote_may_stand_on_a_neighbouring_line_but_not_further(self):
        self.assertTrue(self.source("R01@751", "Das kommt in der Klausur dran")[0][0]["verified"])
        self.assertFalse(self.source("R01@751", "Und die Antwort darauf")[0][0]["verified"])

    def test_private_lines_cannot_verify_a_quote(self):
        self.assertFalse(self.source("R01@995", "Privat")[0][0]["verified"])

    def test_a_line_keeps_its_exported_time_after_transcribing_again(self):
        Transcript("Woche.m4a", [Segment(750.0, 756.0, "Die Passivseite zeigt die Mittelherkunft")], id="r_one").save(self.lecture.dir("transcripts"))
        self.view = self.load()
        sources, _ = self.source("R01@751", "Die Passivseite zeigt")
        self.assertEqual((sources[0]["rec"], sources[0]["t"], sources[0]["verified"]), ("r_one", 751.2, True))

    def test_notes_figures_professors_and_own_knowledge(self):
        note = self.source("N01.1", "Die Bilanz hat zwei Seiten")[0][0]
        self.assertEqual((note["kind"], note["lecture"], note["label"], note["verified"]), ("note", self.lecture.uid, "N01.1", True))
        self.assertFalse(self.source("N01.1", "Etwas ganz anderes steht da")[0][0]["verified"])
        professor = self.source("PR1", "Eigener Text")[0][0]
        self.assertEqual((professor["prof"], professor["verified"]), (self.profile.id, True))
        self.assertEqual(self.source("KN")[0], [{"kind": "own", "label": "KN", "quote": "", "verified": True}])

    def test_an_unknown_export_is_read_with_the_subject_as_it_is_now(self):
        for head in ({**HEAD, "export": "x9"}, {"t": "head", "part": 1}, None):
            with self.subTest(head=head):
                checked = self.check(TOPIC, OBJECTIVE, head=head)
                self.assertEqual(checked.records["objective"][0]["sources"][0]["label"], "SL01.p1")
                self.assertRegex(self.messages(checked), "ids were read with the subject as it is now")

    def test_a_known_export_gives_no_such_warning(self):
        self.assertNotIn("as it is now", self.messages(self.check(TOPIC, OBJECTIVE)))

    def test_a_lecture_uid_replaces_its_ref(self):
        self.assertEqual(self.check(TOPIC).records["topic"][0]["lectures"], [self.lecture.uid])
        checked = self.check({**TOPIC, "lectures": ["L01", "L77"]})
        self.assertEqual(checked.records["topic"][0]["lectures"], [self.lecture.uid])
        self.assertIn("L77", self.messages(checked))

    def test_a_signal_citing_nothing_warns(self):
        self.assertNotIn("Signal", self.messages(self.check({**TOPIC, "signals": ["lecturer_cue:R01@797", "past_exam:EX02.p1"]})))
        self.assertIn("R01@1", self.messages(self.check({**TOPIC, "signals": ["lecturer_cue:R01@1"]})))


class LabelTests(Fixture):
    def test_from_and_to_become_a_time_range(self):
        label = self.check({"t": "label", "from": "R01@948", "to": "R01@995", "category": "question", "exam": False, "reason": "Frage"}).records["label"][0]
        self.assertEqual((label["rec"], label["start"], label["end"]), ("r_one", 948.0, 997.0))

    def test_without_to_it_is_that_line_alone(self):
        label = self.check({"t": "label", "from": "R01@970", "category": "off_topic"}).records["label"][0]
        self.assertEqual((label["start"], label["end"]), (970.0, 972.0))

    def test_from_and_to_swapped_is_still_the_range(self):
        label = self.check({"t": "label", "from": "R01@995", "to": "R01@948", "exam": True}).records["label"][0]
        self.assertEqual((label["start"], label["end"]), (948.0, 997.0))

    def test_what_cannot_be_used(self):
        cases = {
            "unknown line": {"t": "label", "from": "R01@1", "category": "lecture"},
            "unknown end": {"t": "label", "from": "R01@948", "to": "R01@1", "category": "lecture"},
            "two recordings": {"t": "label", "from": "R01@948", "to": "R02@10", "category": "lecture"},
            "bad category": {"t": "label", "from": "R01@948", "category": "smalltalk"},
            "says nothing": {"t": "label", "from": "R01@948"},
        }
        for name, record in cases.items():
            with self.subTest(name):
                checked = self.check(record)
                self.assertEqual(len(checked.errors), 1)
                self.assertNotIn("label", checked.records)


class HierarchyTests(Fixture):
    def objective(self, truth: str, *sources: dict) -> tuple[dict, object]:
        checked = self.check(TOPIC, {**OBJECTIVE, "truth": truth, "src": list(sources)})
        return checked.records["objective"][0], checked

    def test_what_makes_an_objective_verified(self):
        slide = {"id": "SL01.p1", "q": PASSIV}
        line = {"id": "R01@797", "q": "Das kommt in der Klausur dran"}
        exam = {"id": "EX02.p1", "q": "Erstellen Sie eine Bilanz"}
        note = {"id": "N01.1", "q": "Die Bilanz hat zwei Seiten"}
        own = {"id": "KN", "q": ""}
        bad = {"id": "SL01.p1", "q": "Das steht hier ganz sicher nicht"}
        cases = [
            ("slides", [slide], True),
            ("slides", [line], False),  # the slide itself is what it claims
            ("slides", [bad], False),
            ("said", [line], True),
            ("exam", [exam], True),
            ("notes", [note], False),  # a note alone carries nothing
            ("said", [own], False),
            ("said", [], False),
            ("outside", [own], False),  # own knowledge never stands alone
            ("outside", [own, slide], True),
            ("outside", [slide], False),  # must say where it comes from
        ]
        for truth, sources, verified in cases:
            with self.subTest(truth=truth, sources=[s["id"] for s in sources]):
                self.assertEqual(self.objective(truth, *sources)[0]["verified"], verified)

    def test_unverified_records_are_kept_and_say_why(self):
        objective, checked = self.objective("slides", {"id": "R01@797", "q": "Das kommt in der Klausur dran"})
        self.assertEqual(objective["id"], "L01-o001")
        self.assertEqual(checked.errors, [])
        self.assertIn('truth "slides" needs a verified slide source', self.messages(checked))
        outside = self.objective("outside", {"id": "SL01.p1", "q": PASSIV})[1]
        self.assertIn('truth "outside" must carry KN', self.messages(outside))

    def test_a_question_needs_verified_evidence_too(self):
        good = self.check(TOPIC, OBJECTIVE, MC).records["q"][0]
        self.assertTrue(good["verified"])
        bad = self.check(TOPIC, OBJECTIVE, {**MC, "src": [{"id": "N01.1", "q": "Die Bilanz hat zwei Seiten"}]}).records["q"][0]
        self.assertFalse(bad["verified"])

    def test_the_objective_belongs_to_the_lecture_in_its_id(self):
        self.assertEqual(self.check(TOPIC, OBJECTIVE).records["objective"][0]["lectures"], [self.lecture.uid])


class RecordRuleTests(Fixture):
    def test_question_type_rules(self):
        one_option = {"options": [{"text": "A", "correct": True}]}
        two_right = {"options": [{"text": "A", "correct": True}, {"text": "B", "correct": True}]}
        none_right = {"options": [{"text": "A"}, {"text": "B"}]}
        cases = [
            ("mc", one_option), ("mc", two_right), ("mc", none_right), ("mc", {"options": "A"}),
            ("tf", {"answer": "vielleicht"}), ("tf", {}),
            ("calc", {}), ("calc", {"numeric": {"value": "viel"}}),
            ("short", {}), ("case", {"rubric": [{"point": "x", "marks": 1}]}), ("card", {}),
            ("essay", {"answer": "x"}),
        ]  # fmt: skip
        for kind, fields in cases:
            with self.subTest(kind=kind, fields=fields):
                base = {k: v for k, v in MC.items() if k != "options"}
                checked = self.check(TOPIC, OBJECTIVE, {**base, "type": kind, **fields})
                self.assertEqual(len(checked.errors), 1)
                self.assertNotIn("q", checked.records)
                self.assertEqual(checked.counts["objective"], 1)  # the others still import

    def test_usable_question_types(self):
        base = {k: v for k, v in MC.items() if k != "options"}
        cases = [
            {"type": "tf", "answer": "True"},
            {"type": "tf", "answer": "falsch"},
            {"type": "calc", "numeric": {"value": 1200, "tolerance": 0.5, "unit": "€"}, "steps": ["1"]},
            {"type": "calc", "answer": "1200 €"},
            {"type": "short", "answer": "Modell", "rubric": [{"point": "x", "marks": 1}]},
            {"type": "case", "answer": "Modell", "rubric": [{"point": "x", "marks": 1}]},
            {"type": "card", "answer": "Rückseite"},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                checked = self.check(TOPIC, OBJECTIVE, {**base, **fields})
                self.assertEqual(checked.errors, [])
                self.assertEqual(checked.counts["q"], 1)
        self.assertEqual(self.check(TOPIC, OBJECTIVE, {**base, "type": "tf", "answer": "falsch"}).records["q"][0]["answer"], "False")

    def test_a_wrong_option_should_say_why(self):
        options = [{"text": "A", "correct": True}, {"text": "Bohne"}]
        checked = self.check(TOPIC, OBJECTIVE, {**MC, "options": options})
        self.assertEqual(checked.errors, [])
        self.assertIn("why_wrong", self.messages(checked))

    def test_a_short_answer_without_rubric_can_still_be_self_graded(self):
        base = {k: v for k, v in MC.items() if k != "options"}
        checked = self.check(TOPIC, OBJECTIVE, {**base, "type": "short", "answer": "Modell"})
        self.assertEqual(checked.counts["q"], 1)
        self.assertIn("rubric", self.messages(checked))

    def test_a_dangling_reference_costs_that_record(self):
        checked = self.check(TOPIC, {**OBJECTIVE, "topic": "t_x"}, MC)
        self.assertEqual(checked.counts, {"topic": 1})
        self.assertEqual(len(checked.errors), 2)
        self.assertIn("t_x", self.messages(checked, "error"))
        self.assertIn("L01-o001", self.messages(checked, "error"))

    def test_references_resolve_through_known_ids_and_in_any_order(self):
        self.assertEqual(self.check(MC, OBJECTIVE, TOPIC).errors, [])
        known = {"topics": {"t_b"}, "objectives": {"L01-o001"}, "questions": set(), "attempts": set()}
        checked = self.check(MC, known=known)
        self.assertEqual(checked.errors, [])
        self.assertEqual(checked.counts["q"], 1)
        self.assertEqual(self.check(OBJECTIVE, known=known).errors, [])

    def test_a_question_without_id_gets_one_from_topic_and_stem(self):
        base = {k: v for k, v in MC.items() if k != "id"}
        first = self.check(TOPIC, OBJECTIVE, base).records["q"][0]
        second = self.check(TOPIC, OBJECTIVE, {**base, "stem": "  was ZEIGT die **Passivseite**? "}).records["q"][0]
        self.assertRegex(first["id"], r"^q_[0-9a-f]{10}$")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["id"], claude_reply.question_id("t_b", MC["stem"]))
        self.assertEqual(first["topic"], "t_b")

    def test_unknown_fields_are_kept_and_unknown_records_reported(self):
        checked = self.check({**TOPIC, "neu": 1}, {"t": "zukunft", "x": 1})
        self.assertEqual(checked.records["topic"][0]["neu"], 1)
        self.assertIn("zukunft", self.messages(checked, "error"))

    def test_a_field_of_the_wrong_type_costs_that_record(self):
        checked = self.check(TOPIC, {"t": "guide", "topic": "t_b", "md": ["keine", "Zeichenkette"]})
        self.assertEqual(checked.counts, {"topic": 1})
        self.assertEqual(len(checked.errors), 1)

    def test_other_records_become_durable(self):
        checked = self.check(
            {"t": "said", "lecture": "L01", "kind": "exam_hint", "text": "Kommt dran", "slide": "SL01.p1", "src": [{"id": "R01@797", "q": "Das kommt in der Klausur dran"}]},
            {"t": "conflict", "id": "c_1", "a": "SL01.p1", "b": "R01@797", "issue": "Widerspruch", "working": "a", "needs_student_check": False},
            {"t": "note_fix", "lecture": "L01", "line": "N01.1", "verdict": "wrong", "fix": "Besser", "src": [{"id": "SL01.p1", "q": PASSIV}]},
            {"t": "doc_note", "doc": "SL01", "issue": "outdated", "detail": "Alt"},
            {"t": "prof", "prof": "PR1", "field": "focus_areas", "value": "Bilanz", "src": []},
        )
        self.assertEqual(checked.errors, [])
        said = checked.records["said"][0]
        self.assertEqual((said["lecture"], said["slide"]["page"], said["sources"][0]["kind"]), (self.lecture.uid, 1, "line"))
        self.assertTrue(said["id"])
        self.assertEqual(checked.records["conflict"][0]["b"]["rec"], "r_one")
        self.assertEqual(checked.records["note_fix"][0]["lecture"], self.lecture.uid)
        self.assertEqual(checked.records["doc_note"][0]["doc"], self.view.document("SL01").key)
        self.assertEqual(checked.records["prof"][0]["prof"], self.profile.id)

    def test_enumerations(self):
        for record in (
            {"t": "note_fix", "lecture": "L01", "line": "N01.1", "verdict": "naja", "fix": "x"},
            {"t": "note_fix", "lecture": "L01", "line": "N01.1", "verdict": "wrong"},
            {"t": "doc_note", "doc": "SL01", "issue": "hässlich"},
            {"t": "doc_note", "doc": "SL09", "issue": "outdated"},
            {"t": "prof", "prof": "PR1", "field": "schuhgröße", "value": "44"},
            {"t": "prof", "prof": "PR7", "field": "style_notes", "value": "x"},
            {"t": "said", "lecture": "L99", "text": "x"},
            {"t": "coverage", "lecture": "L99", "covered": []},
        ):
            with self.subTest(record=record):
                self.assertEqual(len(self.check(record).errors), 1)

    def test_merge_retire_attempt_and_grade_need_what_they_name(self):
        for record in (
            {"t": "merge", "into": "L01-o001", "from": ["L09-o001"]},
            {"t": "merge", "into": "L09-o001", "from": ["L01-o001"]},
            {"t": "retire", "id": "q_nix"},
            {"t": "attempt", "q": "q_nix", "score": 1.0},
            {"t": "attempt", "q": "q_1"},
            {"t": "grade", "attempt": "a_nix", "score": 0.5},
        ):
            with self.subTest(record=record):
                checked = self.check(TOPIC, OBJECTIVE, MC, record)
                self.assertGreaterEqual(len(checked.errors), 1)
                self.assertNotIn(record["t"], checked.records)


class GuideTests(Fixture):
    def test_inline_citations_become_a_map(self):
        md = "Die Passivseite [SL01.p1] und die Aktivseite [SL01.p2, R01@797]; dazu [EX02.p1; KN]. Kein Zitat: [Bilanz] und [1]."
        checked = self.check(TOPIC, {"t": "guide", "topic": "t_b", "md": md})
        guide = checked.records["guide"][0]
        self.assertEqual(set(guide["citations"]), {"SL01.p1", "SL01.p2", "R01@797", "EX02.p1", "KN"})
        self.assertEqual(guide["citations"]["SL01.p2"]["page"], 2)
        self.assertEqual(guide["citations"]["R01@797"]["rec"], "r_one")
        self.assertEqual(guide["md"], md)
        self.assertEqual(checked.warnings[1:], [])

    def test_an_unresolvable_citation_warns_and_the_guide_stays(self):
        checked = self.check(TOPIC, {"t": "guide", "topic": "t_b", "md": "Falsch [SL09.p1], richtig [SL01.p1]."})
        self.assertEqual(set(checked.records["guide"][0]["citations"]), {"SL01.p1"})
        self.assertIn("[SL09.p1]", self.messages(checked))

    def test_a_guide_needs_its_topic(self):
        self.assertEqual(len(self.check({"t": "guide", "topic": "t_x", "md": "x"}).errors), 1)


class UncoveredTests(Fixture):
    def pages(self, checked) -> list[str]:
        return [item["page"] for item in checked.uncovered.get("L01", ()) if not item["page"].startswith("R")]

    def test_every_page_with_text_is_asked_for_and_empty_ones_are_not(self):
        self.assertEqual(self.pages(self.check()), ["SL01.p1", "SL01.p2", "SL01.p4"])

    def test_pages_listed_in_coverage_count(self):
        coverage = {"t": "coverage", "lecture": "L01", "covered": ["SL01.p1-2", "R01", "R02"], "uncovered": [{"page": "SL01.p4", "why": "Agenda"}]}
        checked = self.check(coverage)
        self.assertEqual(checked.uncovered, {})
        self.assertEqual(checked.records["coverage"][0]["uncovered"], [{"page": "SL01.p4", "why": "Agenda"}])
        self.assertEqual(checked.records["coverage"][0]["hash"], self.view.lectures[0].content_hash)

    def test_a_whole_document_in_coverage_counts(self):
        self.assertEqual(self.pages(self.check({"t": "coverage", "lecture": "L01", "covered": ["SL01"]})), [])

    def test_pages_cited_anywhere_count(self):
        self.assertEqual(self.pages(self.check(TOPIC, {"t": "guide", "topic": "t_b", "md": "[SL01.p2]"})), ["SL01.p4"])
        self.assertEqual(self.pages(self.check({"t": "said", "lecture": "L01", "text": "x", "slide": "SL01.p4"})), ["SL01.p1", "SL01.p2"])

    def test_pages_accounted_for_in_earlier_parts_count(self):
        key = self.view.document("SL01").key
        known = {"accounted_pages": {f"{key}.p1", f"{key}.p2"}}
        self.assertEqual(self.pages(self.check(known=known)), ["SL01.p4"])

    def test_coverage_of_what_does_not_exist_is_reported(self):
        checked = self.check({"t": "coverage", "lecture": "L01", "covered": ["SL01.p1-9", "SL77", "R77", "Seite 3"]})
        self.assertEqual(checked.unknown_pages["L01"], ["SL01.p1-9", "SL77", "R77", "Seite 3"])
        self.assertEqual(self.pages(checked), [])  # the existing pages of the range still count

    def test_an_excluded_deck_is_not_asked_for(self):
        self.subject.set_document_excluded(self.view.document("SL01").key, True)
        view = self.load()
        claude_reply.save_manifest(self.subject, claude_reply.build_manifest(view, list(view.lectures), "x2"))
        checked = check_reply(parse_reply(jsonl(head={**HEAD, "export": "x2"})), view)
        self.assertEqual(self.pages(checked), [])

    def test_a_deck_of_two_lectures_is_asked_for_once(self):
        second = self.subject.create_lecture("Woche 2")
        make_pdf(second.dir("slides") / "VL.pdf", PAGES)
        view = self.load()
        claude_reply.save_manifest(self.subject, claude_reply.build_manifest(view, list(view.lectures), "x2"))
        checked = check_reply(parse_reply(jsonl(head={**HEAD, "export": "x2"})), view)
        self.assertEqual(sorted(checked.uncovered), ["L01"])

    def test_without_a_manifest_only_lectures_with_coverage_are_held_to_it(self):
        self.assertEqual(self.check(head={**HEAD, "export": "x9"}).uncovered, {})
        checked = self.check({"t": "coverage", "lecture": "L01", "covered": ["SL01.p1"]}, head={**HEAD, "export": "x9"})
        self.assertEqual(self.pages(checked), ["SL01.p2", "SL01.p4"])


class PromptTests(Fixture):
    def test_nothing_to_ask(self):
        coverage = {"t": "coverage", "lecture": "L01", "covered": ["SL01", "R01", "R02"]}
        checked = self.check(TOPIC, OBJECTIVE, MC, coverage)
        self.assertEqual(repair_prompt(checked), "")
        self.assertEqual(follow_up_prompt(checked), "")

    def test_repair_names_part_line_id_and_what_is_wrong(self):
        first = jsonl(TOPIC, end={"t": "end", "records": 1, "more": True})
        bad = {**MC, "id": "q_bad", "options": [{"text": "A"}, {"text": "B"}]}
        second = jsonl(OBJECTIVE, bad, {**MC, "src": [{"id": "SL01.p1", "q": "Das steht nicht auf der Folie drauf"}]}, head={**HEAD, "part": 2})
        prompt = repair_prompt(check_reply(parse_reply(first + "\n" + second), self.view))
        self.assertIn("Resend ONLY these records", prompt)
        self.assertIn("part 2, line 6, q q_bad: An mc question needs exactly one correct option", prompt)
        self.assertIn("part 2, line 7, q q_1 (kept for now): Quote not found on SL01.p1", prompt)
        self.assertIn('"export": "x1"', prompt)

    def test_repair_names_a_broken_line(self):
        text = "\n".join([json.dumps(HEAD), json.dumps(TOPIC), '{"t":"q","stem":"Er sagte "hallo""}', json.dumps(END)])
        prompt = repair_prompt(check_reply(parse_reply(text), self.view))
        self.assertIn("part", prompt)
        self.assertIn("line 3", prompt)
        self.assertIn('Er sagte "hallo"', prompt)

    def test_truncation_says_what_arrived_last_and_where_to_continue(self):
        text = jsonl(TOPIC, OBJECTIVE, end=None) + '\n{"t":"q","id":"q_cu'
        prompt = repair_prompt(check_reply(parse_reply(text), self.view))
        self.assertIn("cut off", prompt)
        self.assertIn("objective L01-o001", prompt)
        self.assertIn("Continue from the next record", prompt)
        self.assertNotIn("Resend", prompt.split("cut off")[1])

    def test_truncation_before_anything_arrived(self):
        prompt = repair_prompt(check_reply(parse_reply('{"t":"he'), self.view))
        self.assertIn("Start again from the beginning", prompt)

    def test_the_state_of_this_app_is_not_claudes_to_fix(self):
        self.assertEqual(repair_prompt(self.check(TOPIC, head={**HEAD, "export": "x9"})), "")

    def test_follow_up_asks_for_exactly_the_missing_pages(self):
        prompt = follow_up_prompt(self.check({"t": "coverage", "lecture": "L01", "covered": ["SL01.p1"]}))
        self.assertIn("L01: SL01.p2, SL01.p4", prompt)
        self.assertNotIn("SL01.p1", prompt)
        self.assertNotIn("SL01.p3", prompt)
        self.assertIn("JSON Lines", prompt)

    def test_the_catalogue_names_every_record_the_checker_takes(self):
        for kind in (*claude_reply.RECORD_ORDER, "head", "end"):
            self.assertIn(f'"t":"{kind}"', claude_reply.RECORD_CATALOGUE)
        self.assertIn(claude_reply.SCHEMA, claude_reply.RECORD_CATALOGUE)
        for values in (claude_reply.TRUTHS, claude_reply.DOC_ISSUES, claude_reply.LABEL_CATEGORIES, claude_reply.SAID_KINDS):
            self.assertIn("|".join(values), claude_reply.RECORD_CATALOGUE)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"UNIVERSE_DATA_DIR": tempfile.mkdtemp()})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.subject = Workspace().create_subject("Finanzen")
        build_subject(self.subject, self.subject.create_lecture("Woche 1"))
        view = subject_model.load_subject(self.subject)
        claude_reply.save_manifest(self.subject, claude_reply.build_manifest(view, list(view.lectures), "x1"))
        self.folder = Path(tempfile.mkdtemp())

    def run_main(self, path: Path, subject: str = "Finanzen") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = claude_reply.main([str(path), "--subject", subject])
        return code, out.getvalue(), err.getvalue()

    def test_a_clean_file_passes(self):
        path = self.folder / "reply.jsonl"
        path.write_text(jsonl(TOPIC, OBJECTIVE, {"t": "coverage", "lecture": "L01", "covered": ["SL01"]}), encoding="utf-8")
        code, out, _ = self.run_main(path)
        self.assertEqual(code, 0)
        self.assertIn("1 topic", out)
        self.assertIn("0 errors", out)
        self.assertIn("complete", out)

    def test_errors_fail_and_name_the_file_and_line_of_a_part(self):
        (self.folder / "part-01.jsonl").write_text(jsonl(TOPIC, end={"t": "end", "records": 1, "more": True}), encoding="utf-8")
        (self.folder / "part-02.jsonl").write_text(jsonl({**OBJECTIVE, "topic": "t_x"}, head={**HEAD, "part": 2}), encoding="utf-8")
        (self.folder / "notizen.pdf").write_bytes(b"not a reply")
        code, out, _ = self.run_main(self.folder)
        self.assertEqual(code, 1)
        self.assertIn("ERROR part-02.jsonl:2 objective L01-o001: Topic t_x does not exist.", out)
        self.assertIn("Not accounted for in L01", out)

    def test_a_cut_off_reply_fails(self):
        path = self.folder / "reply.jsonl"
        path.write_text(jsonl(TOPIC, end=None), encoding="utf-8")
        code, out, _ = self.run_main(path)
        self.assertEqual(code, 1)
        self.assertIn("cut off", out)
        self.assertIn("not finished", out)

    def test_what_is_not_there(self):
        path = self.folder / "reply.jsonl"
        path.write_text(jsonl(), encoding="utf-8")
        self.assertEqual(self.run_main(path, "Gibt es nicht")[0], 2)
        self.assertEqual(self.run_main(self.folder / "fehlt.jsonl")[0], 2)

    def test_replies_already_imported_are_known(self):
        from universe.services.study_store import StudyStore

        view = subject_model.load_subject(self.subject)
        text = jsonl(TOPIC, OBJECTIVE)
        StudyStore(self.subject).merge(check_reply(parse_reply(text), view), text, view)
        path = self.folder / "reply.jsonl"
        path.write_text(jsonl(MC, {"t": "coverage", "lecture": "L01", "covered": ["SL01"]}), encoding="utf-8")
        self.assertEqual(self.run_main(path)[0], 0)


if __name__ == "__main__":
    unittest.main()
