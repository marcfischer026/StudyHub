"""Tests for what is stored: merging replies without losing progress, the event log, what the student decides."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())

from tests.test_claude_reply import HEAD, MC, OBJECTIVE, PASSIV, TOPIC, Fixture as ReplyFixture, jsonl  # noqa: E402
from universe.services import practice  # noqa: E402
from universe.services.claude_reply import check_reply, parse_reply  # noqa: E402
from universe.services.study_store import Knowledge, MergeSummary, StudyStore  # noqa: E402
from universe.services.transcriber import Segment, Transcript  # noqa: E402

BASE = (TOPIC, OBJECTIVE, MC)


def probe(number: int, **fields) -> dict:
    return {**MC, "id": f"q_{number}", "stem": f"Frage Nummer {number}?", **fields}


class Fixture(ReplyFixture):
    def setUp(self):
        super().setUp()
        self.store = StudyStore(self.subject)

    def merge(self, *records: dict, **options) -> MergeSummary:
        text = jsonl(*records, **options)
        checked = check_reply(parse_reply(text), self.view, self.store.known_ids())
        return self.store.merge(checked, text, self.view)

    def question(self, key: str = "q_1") -> dict:
        return self.store.knowledge().questions[key]

    def transcript(self) -> Transcript:
        return Transcript.load(self.lecture.dir("transcripts") / "Woche.json")


class StoredShapeTests(Fixture):
    def test_records_are_stored_in_the_documented_shape(self):
        summary = self.merge(*BASE)
        knowledge = self.store.knowledge()
        topic = knowledge.topics["t_b"]
        self.assertEqual(topic["lectures"], [self.lecture.uid])
        self.assertFalse(topic["retired"])
        objective = knowledge.objectives["L01-o001"]
        self.assertTrue(objective["verified"])
        self.assertEqual(objective["sources"][0]["label"], "SL01.p1")
        question = self.question()
        for field, value in {"pool": "practice", "rev": 1, "retired": False, "suspended": False, "flagged": "", "topic": "t_b", "verified": True}.items():
            self.assertEqual(question[field], value, field)
        for bookkeeping in ("t", "_line", "_part", "src"):
            self.assertNotIn(bookkeeping, question)
        self.assertEqual(summary.added, {"topics": 1, "objectives": 1, "questions": 1})

    def test_knowledge_survives_a_new_store_and_rereads_when_the_file_changed(self):
        self.assertEqual(self.store.knowledge().questions, {})
        other = StudyStore(self.subject)
        self.assertEqual(other.knowledge().questions, {})
        self.merge(*BASE)
        self.assertIn("q_1", other.knowledge().questions)
        self.assertIsInstance(other.knowledge(), Knowledge)

    def test_known_ids_lets_a_later_reply_point_back(self):
        self.merge(*BASE)
        summary = self.merge(probe(2))
        self.assertEqual(summary.added, {"questions": 1})
        known = self.store.known_ids()
        self.assertEqual((known["topics"], known["objectives"]), ({"t_b"}, {"L01-o001"}))
        self.assertEqual(known["questions"], {"q_1", "q_2"})

    def test_the_raw_reply_is_archived(self):
        self.merge(*BASE)
        files = list((self.subject.study_dir / "imports").glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_text(encoding="utf-8"), jsonl(*BASE))
        self.assertTrue(files[0].stem.endswith(hashlib.sha1(jsonl(*BASE).encode()).hexdigest()[:8]))

    def test_describe_says_what_happened(self):
        text = self.merge(*BASE).describe()
        self.assertIn("1 questions", text)
        self.assertEqual(MergeSummary().describe(), "Nothing new in this reply.")


class MergeTableTests(Fixture):
    def setUp(self):
        super().setUp()
        self.merge(*BASE)

    def test_same_id_same_content_refreshes_sources(self):
        source = [{"id": "SL01.p2", "q": "Die Aktivseite zeigt"}]
        summary = self.merge({**MC, "src": source})
        self.assertEqual(summary.updated, {})
        self.assertEqual(self.question()["sources"][0]["label"], "SL01.p2")

    def test_no_sources_in_the_new_version_keeps_the_old_ones(self):
        self.merge({**MC, "src": []})
        self.assertEqual(self.question()["sources"][0]["label"], "SL01.p1")
        self.assertTrue(self.question()["verified"])

    def test_same_topic_and_normalised_stem_is_the_same_question(self):
        summary = self.merge({**MC, "id": "q_other", "stem": "  was zeigt die **PASSIVSEITE**?"})
        self.assertEqual(summary.duplicates, 1)
        self.assertEqual(set(self.store.knowledge().questions), {"q_1"})
        self.assertEqual(summary.added, {})

    def test_the_same_stem_in_another_topic_is_another_question(self):
        other = {**TOPIC, "id": "t_c"}
        objective = {**OBJECTIVE, "id": "L01-o002", "topic": "t_c"}
        summary = self.merge(other, objective, {**MC, "id": "q_other", "objective": "L01-o002"})
        self.assertEqual(summary.duplicates, 0)
        self.assertIn("q_other", self.store.knowledge().questions)

    def test_a_duplicate_in_one_reply_folds_too(self):
        summary = self.merge(probe(5), probe(6, stem="Frage Nummer 5?"))
        self.assertEqual(summary.duplicates, 1)
        self.assertNotIn("q_6", self.store.knowledge().questions)

    def test_changed_wording_with_the_same_answer_keeps_rev(self):
        summary = self.merge({**MC, "stem": "Worüber gibt die Passivseite Auskunft?", "explain": "Neu"})
        self.assertEqual(self.question()["rev"], 1)
        self.assertEqual(self.question()["explain"], "Neu")
        self.assertEqual(summary.answer_changed, 0)
        self.assertEqual(summary.updated, {"questions": 1})

    def test_shuffled_options_are_the_same_answer(self):
        self.merge({**MC, "options": list(reversed(MC["options"]))})
        self.assertEqual(self.question()["rev"], 1)

    def test_a_changed_answer_bumps_rev_and_keeps_history(self):
        event = self.store.record_attempt(self.question(), "Mittelherkunft", 1.0, "sure", 0, 900, "today", "auto")
        options = [{"text": "Mittelherkunft", "why_wrong": "Falsch"}, {"text": "Mittelverwendung", "correct": True}]
        summary = self.merge({**MC, "options": options})
        self.assertEqual(self.question()["rev"], 2)
        self.assertEqual(summary.answer_changed, 1)
        self.assertEqual(self.store.events(), [event])
        self.assertEqual(self.merge({**MC, "options": options}).answer_changed, 0)  # the same again changes nothing
        self.assertEqual(self.question()["rev"], 2)

    def test_answer_changes_by_question_type(self):
        base = {k: v for k, v in MC.items() if k != "options"}
        first = {**base, "id": "q_c", "stem": "Rechne", "type": "calc", "numeric": {"value": 12, "tolerance": 0.5}, "answer": "12"}
        self.merge(first)
        self.merge({**first, "answer": "Zwölf Euro", "steps": ["a"]})
        self.assertEqual(self.question("q_c")["rev"], 1)  # the number decides
        self.merge({**first, "numeric": {"value": 13, "tolerance": 0.5}})
        self.assertEqual(self.question("q_c")["rev"], 2)
        card = {**base, "id": "q_k", "stem": "Karte", "type": "card", "answer": "Rückseite"}
        self.merge(card)
        self.merge({**card, "answer": "  RÜCKSEITE "})
        self.assertEqual(self.question("q_k")["rev"], 1)
        self.merge({**card, "answer": "Andere Rückseite"})
        self.assertEqual(self.question("q_k")["rev"], 2)

    def test_what_a_reply_does_not_mention_is_untouched(self):
        before = self.store.knowledge()
        self.merge({**TOPIC, "id": "t_neu", "title": "Neu"})
        after = self.store.knowledge()
        self.assertEqual(after.questions, before.questions)
        self.assertEqual(after.objectives, before.objectives)
        self.assertEqual(set(after.topics), {"t_b", "t_neu"})

    def test_suspended_and_flagged_stay_through_imports(self):
        self.store.set_suspended("q_1", True)
        self.store.flag_question("q_1", "Antwort falsch")
        self.merge({**MC, "explain": "Neu", "suspended": False, "flagged": "", "retired": True, "pool": "holdout", "rev": 9})
        question = self.question()
        self.assertEqual((question["suspended"], question["flagged"], question["retired"], question["pool"], question["rev"]), (True, "Antwort falsch", False, "practice", 1))
        self.assertEqual(question["explain"], "Neu")

    def test_lectures_of_a_topic_add_up(self):
        second = self.subject.create_lecture("Woche 2")
        self.view = self.load()
        self.merge({**TOPIC, "lectures": ["L02"]})
        self.assertEqual(self.store.knowledge().topics["t_b"]["lectures"], [self.lecture.uid, second.uid])


class HoldoutTests(Fixture):
    def holdouts(self) -> list[str]:
        return [key for key, q in self.store.knowledge().questions.items() if q["pool"] == "holdout"]

    def lowest(self, *keys: str) -> str:
        return min(keys, key=lambda key: hashlib.sha1(key.encode()).hexdigest())

    def test_the_app_chooses_the_lowest_hash_at_three_probes(self):
        self.merge(TOPIC, OBJECTIVE, probe(1), probe(2))
        self.assertEqual(self.holdouts(), [])
        self.merge(probe(3, pool="practice"))
        self.assertEqual(self.holdouts(), [self.lowest("q_1", "q_2", "q_3")])

    def test_claude_cannot_decide(self):
        self.merge(TOPIC, OBJECTIVE, probe(1, pool="holdout"), probe(2, pool="holdout"))
        self.assertEqual(self.holdouts(), [])

    def test_importance_one_has_no_holdout(self):
        self.merge(TOPIC, {**OBJECTIVE, "importance": 1}, probe(1), probe(2), probe(3))
        self.assertEqual(self.holdouts(), [])

    def test_a_chosen_holdout_stays_and_no_second_one_comes(self):
        self.merge(TOPIC, OBJECTIVE, probe(1), probe(2), probe(3))
        first = self.holdouts()
        self.merge(*(probe(n) for n in range(4, 30)))
        self.assertEqual(self.holdouts(), first)

    def test_a_question_already_answered_is_never_held_out(self):
        wanted = self.lowest("q_1", "q_2", "q_3")
        self.store.record_attempt({"id": wanted}, "x", 1.0, "sure", 0, 1, "today", "auto")
        self.merge(TOPIC, OBJECTIVE, probe(1), probe(2), probe(3))
        self.assertEqual(len(self.holdouts()), 1)
        self.assertNotEqual(self.holdouts(), [wanted])

    def test_retired_probes_do_not_count(self):
        self.merge(TOPIC, OBJECTIVE, probe(1), probe(2), probe(3), {"t": "retire", "id": "q_1"}, {"t": "retire", "id": "q_2"})
        self.assertEqual(self.holdouts(), [])

    def test_an_unverified_probe_is_not_held_out(self):
        unsupported = dict(src=[{"id": "N01.1", "q": "Die Bilanz hat zwei Seiten"}])
        self.merge(TOPIC, OBJECTIVE, probe(1, **unsupported), probe(2, **unsupported), probe(3, **unsupported))
        self.assertEqual(self.holdouts(), [])
        self.assertEqual(self.merge(probe(4)).unverified, 0)
        self.assertEqual(self.holdouts(), ["q_4"])


class MergeRetireTests(Fixture):
    def setUp(self):
        super().setUp()
        self.old = {**OBJECTIVE, "id": "L01-o009", "statement": "Dasselbe"}
        self.merge(TOPIC, OBJECTIVE, self.old, probe(7, objective="L01-o009"))

    def test_merge_adds_an_alias_and_retires_the_old_objective(self):
        self.merge({"t": "merge", "into": "L01-o001", "from": ["L01-o009"]})
        knowledge = self.store.knowledge()
        self.assertEqual(knowledge.aliases, {"L01-o009": "L01-o001"})
        self.assertTrue(knowledge.objectives["L01-o009"]["retired"])
        self.assertFalse(knowledge.objectives["L01-o001"]["retired"])
        self.assertEqual(knowledge.questions["q_7"]["objective"], "L01-o001")

    def test_replay_folds_attempts_together(self):
        self.store.record_attempt(self.question("q_7"), "x", 1.0, "sure", 0, 1, "today", "auto")
        self.merge({"t": "merge", "into": "L01-o001", "from": ["L01-o009"]})
        knowledge = self.store.knowledge()
        cards = practice.replay(knowledge, self.store.events())
        self.assertEqual(cards["L01-o001"].attempts, 1)

    def test_a_new_probe_of_the_old_objective_lands_on_the_new_one(self):
        self.merge({"t": "merge", "into": "L01-o001", "from": ["L01-o009"]})
        self.merge(probe(8, objective="L01-o009"))
        self.assertEqual(self.question("q_8")["objective"], "L01-o001")

    def test_a_cycle_cannot_be_built(self):
        self.merge({"t": "merge", "into": "L01-o001", "from": ["L01-o009"]})
        self.merge({"t": "merge", "into": "L01-o009", "from": ["L01-o001"]})
        self.assertEqual(self.store.knowledge().aliases, {"L01-o009": "L01-o001"})

    def test_retire_sets_retired_and_deletes_nothing(self):
        self.merge({"t": "retire", "id": "q_7", "why": "doppelt"}, {"t": "retire", "id": "L01-o009"}, {"t": "retire", "id": "t_b"})
        knowledge = self.store.knowledge()
        self.assertTrue(knowledge.questions["q_7"]["retired"])
        self.assertEqual(knowledge.questions["q_7"]["retired_why"], "doppelt")
        self.assertTrue(knowledge.objectives["L01-o009"]["retired"])
        self.assertTrue(knowledge.topics["t_b"]["retired"])
        self.merge(probe(7, objective="L01-o009"))  # sending it again does not bring it back
        self.assertTrue(self.question("q_7")["retired"])


class IdempotenceTests(Fixture):
    REPLY = (
        *BASE,
        probe(2),
        probe(3),
        {"t": "exam_model", "formats": ["90 min"], "confidence": "low", "src": [{"id": "EX02.p1", "q": "Erstellen Sie eine Bilanz"}]},
        {"t": "guide", "topic": "t_b", "md": "Siehe [SL01.p1]."},
        {"t": "said", "lecture": "L01", "kind": "exam_hint", "text": "Kommt dran", "src": [{"id": "R01@797", "q": "Das kommt in der Klausur dran"}]},
        {"t": "conflict", "id": "c_1", "a": "SL01.p1", "b": "R01@797", "issue": "Widerspruch"},
        {"t": "note_fix", "lecture": "L01", "line": "N01.1", "verdict": "incomplete", "fix": "Besser"},
        {"t": "label", "from": "R01@948", "to": "R01@995", "category": "question", "exam": True, "reason": "Frage"},
        {"t": "doc_note", "doc": "SL01", "issue": "outdated", "detail": "Alt"},
        {"t": "prof", "prof": "PR1", "field": "style_notes", "value": "Fragt gern", "src": []},
        {"t": "coverage", "lecture": "L01", "covered": ["SL01.p1-2"], "uncovered": [{"page": "SL01.p4", "why": "Agenda"}]},
        {"t": "attempt", "q": "q_1", "correct": True, "score": 1.0, "answer": "Mittelherkunft", "at": "2026-11-02T18:00:00"},
        {"t": "grade", "attempt": "a_later", "score": 0.5, "feedback": "Halb"},
        {"t": "tutor_note", "topic": "t_b", "diagnosis": "Verwechselt", "next": "Üben"},
        {"t": "merge", "into": "L01-o001", "from": ["L01-o001"]},
        {"t": "retire", "id": "q_3"},
    )

    def snapshot(self) -> dict:
        folder = self.subject.study_dir
        wanted = [folder / "knowledge.json", folder / "knowledge.prev.json", folder / "events.jsonl", self.lecture.dir("transcripts") / "Woche.json"]
        return {path.name: path.read_bytes() for path in wanted if path.exists()} | {"imports": sorted(p.name for p in (folder / "imports").iterdir())}

    def test_the_same_reply_twice_changes_nothing(self):
        self.store.append_event("attempt", id="a_later", q="q_1", o="L01-o001", score=0.0, rating=1, at="2026-11-01T10:00:00")
        first = self.merge(*self.REPLY)
        self.assertEqual(first.added["questions"], 3)
        self.assertEqual(first.attempts_added, 1)
        before = self.snapshot()
        again = self.merge(*self.REPLY)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((again.added, again.updated, again.attempts_added, again.answer_changed), ({}, {}, 0, 0))

    def test_undo_after_a_repeated_import_still_goes_to_before_it(self):
        self.merge(*BASE)
        self.merge(*BASE)
        self.assertTrue(self.store.undo_last_import())
        self.assertEqual(self.store.knowledge().questions, {})


class UndoTests(Fixture):
    def test_nothing_to_undo(self):
        self.assertFalse(self.store.undo_last_import())

    def test_one_step_back(self):
        self.merge(*BASE)
        self.merge(probe(2), {**MC, "explain": "Zweite Fassung"})
        self.assertTrue(self.store.undo_last_import())
        knowledge = self.store.knowledge()
        self.assertEqual(set(knowledge.questions), {"q_1"})
        self.assertEqual(knowledge.questions["q_1"]["explain"], "Siehe Folie")
        self.assertFalse(self.store.undo_last_import())

    def test_the_first_import_goes_back_to_nothing(self):
        self.merge(*BASE)
        self.assertTrue(self.store.undo_last_import())
        self.assertEqual(self.store.knowledge().topics, {})

    def test_what_the_student_did_since_stays(self):
        note = {"t": "doc_note", "doc": "SL01", "issue": "outdated", "detail": "Alt"}
        self.merge(*BASE, note)
        self.merge(probe(2))
        self.store.flag_question("q_1", "Falsch")
        self.store.set_suspended("q_1", True)
        self.store.dismiss_doc_note(0)
        event = self.store.record_attempt(self.question(), "x", 0.0, "sure", 0, 1, "today", "auto")
        self.store.undo_last_import()
        self.assertEqual((self.question()["flagged"], self.question()["suspended"]), ("Falsch", True))
        self.assertTrue(self.store.knowledge().doc_notes[0]["dismissed"])
        self.assertIn(event, self.store.events())

    def test_a_reimport_after_undo_works(self):
        self.merge(*BASE)
        self.store.undo_last_import()
        self.merge(*BASE)
        self.assertIn("q_1", self.store.knowledge().questions)


class LabelTests(Fixture):
    def test_every_segment_with_its_midpoint_inside_gets_it(self):
        summary = self.merge({"t": "label", "from": "R01@948", "to": "R01@995", "category": "question", "reason": "Frage"})
        segments = self.transcript().segments
        self.assertEqual([s.category for s in segments[2:5]], ["question"] * 3)
        self.assertEqual([s.source for s in segments[2:5]], ["claude"] * 3)
        self.assertEqual(segments[2].note, "Frage")
        self.assertEqual(segments[0].source, "")
        self.assertEqual((summary.labels_applied, summary.labels_stale), (1, 0))

    def test_without_to_only_that_line(self):
        self.merge({"t": "label", "from": "R01@970", "category": "off_topic"})
        self.assertEqual([s.category for s in self.transcript().segments[2:5]], ["", "off_topic", ""])

    def test_a_label_set_by_hand_survives(self):
        transcript = self.transcript()
        transcript.segments[3].category, transcript.segments[3].source, transcript.segments[3].note = "lecture", "manual", "Set by you"
        transcript.save(self.lecture.dir("transcripts"))
        self.view = self.load()
        self.merge({"t": "label", "from": "R01@948", "to": "R01@995", "category": "off_topic", "exam": True, "reason": "Nein"})
        segments = self.transcript().segments
        self.assertEqual((segments[3].category, segments[3].source, segments[3].exam), ("lecture", "manual", False))
        self.assertEqual(segments[2].category, "off_topic")

    def test_a_private_line_stays_private(self):
        self.merge({"t": "label", "from": "R01@948", "to": "R01@995", "category": "lecture"})
        private = self.transcript().segments[5]
        self.assertEqual((private.category, private.source), ("off_topic", "manual"))

    def test_after_transcribing_again_it_lands_by_time_or_counts_as_stale(self):
        Transcript("Woche.m4a", [Segment(949.0, 951.0, "Neu getrennt"), Segment(1500.0, 1501.0, "Woanders")], id="r_one").save(self.lecture.dir("transcripts"))
        self.view = self.load()
        text = jsonl({"t": "label", "from": "R01@948", "category": "question"}, {"t": "label", "from": "R01@970", "category": "lecture"})
        summary = self.store.merge(check_reply(parse_reply(text), self.view), text, self.view)
        self.assertEqual((summary.labels_applied, summary.labels_stale), (1, 1))
        self.assertEqual(self.transcript().segments[0].category, "question")
        self.assertEqual(self.transcript().segments[1].category, "")

    def test_a_range_does_not_wave_through_side_talk_it_never_read(self):
        transcript = self.transcript()
        side = transcript.segments[3]
        side.category, side.source, side.speaker = "off_topic", "local", "speaker_2"
        transcript.save(self.lecture.dir("transcripts"))
        self.view = self.load()
        self.merge({"t": "label", "from": "R01@948", "to": "R01@995", "category": "lecture"})
        self.assertEqual(self.transcript().segments[3].category, "off_topic")

    def test_a_rule_found_exam_cue_survives_importing_twice(self):
        transcript = self.transcript()
        cue = transcript.segments[1]
        cue.exam, cue.source, cue.note = True, "rules", "Exam cue"
        transcript.save(self.lecture.dir("transcripts"))
        self.view = self.load()
        label = {"t": "label", "from": "R01@797", "category": "lecture", "exam": False, "reason": "Vorlesung"}
        for _ in range(2):
            self.merge(label)
        self.assertTrue(self.transcript().segments[1].exam)

    def test_a_later_exam_cue_from_claude_still_applies(self):
        self.merge({"t": "label", "from": "R01@797", "category": "lecture", "exam": False, "reason": "Vorlesung"})
        self.merge({"t": "label", "from": "R01@797", "category": "lecture", "exam": True, "reason": "Vorlesung"})
        self.assertTrue(self.transcript().segments[1].exam)


class UndoLabelTests(Fixture):
    LABEL = {"t": "label", "from": "R01@948", "to": "R01@995", "category": "off_topic", "reason": "Nein"}

    def test_undo_takes_the_labels_of_the_import_back(self):
        before = [(s.category, s.exam, s.note, s.source) for s in self.transcript().segments]
        self.merge(*BASE, self.LABEL)
        self.assertEqual(self.transcript().segments[2].category, "off_topic")
        self.assertTrue(self.store.undo_last_import())
        self.assertEqual([(s.category, s.exam, s.note, s.source) for s in self.transcript().segments], before)
        self.assertFalse(self.store.undo_last_import())

    def test_what_the_student_changed_since_stays(self):
        self.merge(*BASE, self.LABEL)
        transcript = self.transcript()
        transcript.segments[2].category, transcript.segments[2].source, transcript.segments[2].note = "lecture", "manual", "Set by you"
        transcript.save(self.lecture.dir("transcripts"))
        self.store.undo_last_import()
        segments = self.transcript().segments
        self.assertEqual((segments[2].category, segments[2].source), ("lecture", "manual"))
        self.assertEqual((segments[3].category, segments[3].source), ("", ""))

    def test_a_reply_of_only_labels_can_be_undone_too(self):
        self.merge(self.LABEL)
        self.assertTrue(self.store.undo_last_import())
        self.assertEqual(self.transcript().segments[2].category, "")

    def test_a_reply_that_changes_nothing_else_joins_the_step_before_it(self):
        self.merge(*BASE, self.LABEL)
        self.merge(*BASE, {"t": "label", "from": "R01@0", "category": "question"})
        self.store.undo_last_import()
        segments = self.transcript().segments
        self.assertEqual((segments[0].category, segments[2].category), ("", ""))

    def test_a_second_import_replaces_what_undo_takes_back(self):
        self.merge(*BASE, self.LABEL)
        self.merge({**TOPIC, "title": "Anders"}, {"t": "label", "from": "R01@0", "category": "question"})
        self.store.undo_last_import()
        segments = self.transcript().segments
        self.assertEqual(segments[0].category, "")
        self.assertEqual(segments[2].category, "off_topic")


class CoverageTests(Fixture):
    COVERAGE = {"t": "coverage", "lecture": "L01", "covered": ["SL01.p1", "R01"], "uncovered": [{"page": "SL01.p4", "why": "Agenda"}]}

    def test_marks_a_lecture_processed_with_the_manifests_hash(self):
        summary = self.merge(self.COVERAGE)
        entry = self.store.knowledge().lectures[self.lecture.uid]
        self.assertTrue(entry["processed"])
        self.assertEqual(entry["hash"], self.view.lectures[0].content_hash)
        self.assertEqual(entry["export"], "x1")
        self.assertTrue(entry["imported"])
        self.assertEqual(summary.lectures_done, ["L01"])
        self.assertEqual(summary.uncovered_pages, 1)
        self.assertEqual(entry["uncovered"], [{"page": "SL01.p4", "why": "Agenda"}, {"page": "SL01.p2", "why": ""}])

    def test_a_later_part_shrinks_what_is_open(self):
        self.merge(self.COVERAGE)
        summary = self.merge(TOPIC, {"t": "guide", "topic": "t_b", "md": "Siehe [SL01.p2]."}, head={**HEAD, "part": 2})
        self.assertEqual(summary.uncovered_pages, 0)
        self.assertEqual(self.store.knowledge().lectures[self.lecture.uid]["uncovered"], [{"page": "SL01.p4", "why": "Agenda"}])

    def test_no_coverage_no_processed_lecture(self):
        self.merge(*BASE)
        self.assertEqual(self.store.knowledge().lectures, {})


class NotesTests(Fixture):
    def test_notes_are_byte_identical_after_any_import(self):
        path = self.lecture.notes_path
        path.write_bytes("Die Bilanz hat **zwei** Seiten\r\n\n  krumm formatiert  \n".encode())
        before = path.read_bytes()
        stamp = path.stat().st_mtime_ns
        self.merge(
            *IdempotenceTests.REPLY[:3],
            {"t": "note_fix", "lecture": "L01", "line": "N01.1", "verdict": "wrong", "fix": "Drei Seiten", "src": [{"id": "SL01.p1", "q": PASSIV}]},
            {"t": "label", "from": "R01@948", "category": "question"},
            {"t": "coverage", "lecture": "L01", "covered": ["SL01"]},
        )
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.store.undo_last_import()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.store.knowledge().note_fixes, {})

    def test_note_fixes_are_stored_per_lecture_and_replaced_by_line(self):
        self.merge({"t": "note_fix", "lecture": "L01", "line": "N01.1", "verdict": "wrong", "fix": "Eins"})
        self.merge({"t": "note_fix", "lecture": "L01", "line": "N01.1", "verdict": "wrong", "fix": "Zwei"})
        fixes = self.store.knowledge().note_fixes[self.lecture.uid]
        self.assertEqual([f["fix"] for f in fixes], ["Zwei"])
        self.assertEqual(fixes[0]["line"], "N01.1")


class EventTests(Fixture):
    def test_every_event_says_what_it_is(self):
        event = self.store.append_event("flag", q="q_1", reason="x")
        self.assertEqual((event["v"], event["type"], event["q"]), (1, "flag", "q_1"))
        self.assertTrue(event["id"].startswith("f_"))
        self.assertTrue(event["at"])
        self.assertEqual(self.store.events(), [event])

    def test_appending_never_rewrites(self):
        self.store.append_event("flag", q="a", reason="")
        before = self.store.events_path.read_bytes()
        self.store.append_event("flag", q="b", reason="")
        self.assertTrue(self.store.events_path.read_bytes().startswith(before))
        self.assertEqual([e["q"] for e in self.store.events()], ["a", "b"])

    def test_no_file_no_events(self):
        self.assertEqual(self.store.events(), [])

    def test_a_torn_last_line_is_skipped_and_not_glued_onto(self):
        first = self.store.append_event("flag", q="a", reason="")
        with self.store.events_path.open("ab") as file:
            file.write(b'{"v":1,"id":"a_torn","at":"2026-11-0')
        self.assertEqual(self.store.events(), [first])
        after = self.store.append_event("flag", q="b", reason="")
        self.assertEqual(self.store.events(), [first, after])

    def test_a_torn_multibyte_character_costs_only_its_line(self):
        first = self.store.append_event("flag", q="a", reason="")
        with self.store.events_path.open("ab") as file:
            file.write('{"answer":"ü'.encode()[:-1])
        self.assertEqual(self.store.events(), [first])

    def test_unusual_line_separators_in_an_answer_stay_in_one_event(self):
        answer = "erste zweite\x85dritte\x0cvierte"
        self.store.record_attempt({**MC}, answer, 1.0, "sure", 0, 1, "today", "auto")
        self.assertEqual(self.store.events()[0]["answer"], answer)

    def test_record_attempt_fills_everything_and_rates(self):
        question = {**MC, "topic": "t_b", "rev": 2}
        event = self.store.record_attempt(question, "Mittelherkunft", 1.0, "guess", 0, 1500, "weak", "auto")
        expected = {"type": "attempt", "q": "q_1", "o": "L01-o001", "topic": "t_b", "rev": 2, "mode": "weak", "answer": "Mittelherkunft", "correct": True, "score": 1.0, "confidence": "guess", "hints": 0, "ms": 1500, "by": "auto", "rating": 1}
        for key, value in expected.items():
            self.assertEqual(event[key], value, key)
        self.assertEqual(self.store.record_attempt(question, "x", 0.2, "sure", 0, 1, "today", "auto")["rating"], 1)
        self.assertEqual(self.store.record_attempt(question, "x", 1.0, "sure", 1, 1, "today", "auto")["rating"], 2)
        self.assertEqual(self.store.record_attempt(question, "x", 1.0, "sure", 0, 1, "today", "auto")["rating"], 3)
        self.assertTrue(self.store.events()[0]["id"].startswith("a_"))

    def test_flag_goes_into_the_history_and_onto_the_question(self):
        self.merge(*BASE)
        self.store.flag_question("q_1", "Antwort falsch")
        self.assertEqual(self.question()["flagged"], "Antwort falsch")
        self.assertEqual(self.store.events()[-1]["reason"], "Antwort falsch")
        self.assertIn("flag", [e["type"] for e in self.store.events()])
        self.store.set_suspended("q_1", True)
        self.assertTrue(self.question()["suspended"])
        self.store.set_suspended("q_1", False)
        self.assertFalse(self.question()["suspended"])
        self.store.flag_question("q_gone", "egal")  # a stale id is no crash


class ClaudeAttemptTests(Fixture):
    ATTEMPT = {"t": "attempt", "id": "a_c1", "q": "q_1", "correct": True, "score": 0.9, "answer": "Mittelherkunft", "at": "2026-11-02T18:00:00", "by": "claude"}

    def test_attempts_become_tutor_events(self):
        summary = self.merge(*BASE, self.ATTEMPT)
        event = self.store.events()[0]
        expected = {"id": "a_c1", "type": "attempt", "q": "q_1", "o": "L01-o001", "topic": "t_b", "rev": 1, "mode": "tutor", "by": "claude", "at": "2026-11-02T18:00:00", "correct": True, "score": 0.9, "rating": 3, "v": 1}
        for key, value in expected.items():
            self.assertEqual(event[key], value, key)
        self.assertEqual(summary.attempts_added, 1)

    def test_idempotent_by_id(self):
        self.merge(*BASE, self.ATTEMPT)
        self.assertEqual(self.merge({**self.ATTEMPT, "answer": "Anders"}).attempts_added, 0)
        self.assertEqual(len(self.store.events()), 1)

    def test_idempotent_without_id_by_question_answer_and_time(self):
        plain = {k: v for k, v in self.ATTEMPT.items() if k != "id"}
        self.merge(*BASE, plain)
        self.merge(plain)
        self.assertEqual(len(self.store.events()), 1)
        self.merge({**plain, "at": "2026-11-03T18:00:00"})
        self.assertEqual(len(self.store.events()), 2)

    def test_score_from_correct_when_missing(self):
        plain = {k: v for k, v in self.ATTEMPT.items() if k != "score"}
        self.merge(*BASE, plain)
        self.assertEqual(self.store.events()[0]["score"], 1.0)

    def test_an_attempt_on_a_question_folded_as_duplicate_lands_on_the_stored_one(self):
        self.merge(*BASE)
        self.merge({**MC, "id": "q_other"}, {**self.ATTEMPT, "q": "q_other"})
        self.assertEqual(self.store.events()[0]["q"], "q_1")

    def test_grades_are_events_that_arrive_once(self):
        attempt = self.store.record_attempt({**MC, "topic": "t_b"}, "x", 1.0, "sure", 0, 1, "today", "self")
        grade = {"t": "grade", "attempt": attempt["id"], "score": 0.5, "feedback": "Nur halb"}
        self.merge(*BASE, grade)
        self.merge(grade)
        grades = [e for e in self.store.events() if e["type"] == "grade"]
        self.assertEqual(len(grades), 1)
        self.assertEqual((grades[0]["attempt"], grades[0]["score"], grades[0]["feedback"], grades[0]["by"]), (attempt["id"], 0.5, "Nur halb", "claude"))
        self.assertEqual(self.store.events()[0]["score"], 1.0)  # the attempt itself is never rewritten

    def test_a_grade_for_an_attempt_in_the_same_reply(self):
        self.merge(*BASE, {"t": "grade", "attempt": "a_c1", "score": 0.4}, self.ATTEMPT)
        self.assertEqual([e["type"] for e in self.store.events()], ["attempt", "grade"])

    def test_a_score_out_of_range_is_clamped(self):
        self.merge(*BASE, {**self.ATTEMPT, "score": 7})
        self.assertEqual(self.store.events()[0]["score"], 1.0)

    def test_tutor_notes_arrive_once(self):
        note = {"t": "tutor_note", "topic": "t_b", "diagnosis": "Verwechselt", "next": "Üben"}
        self.merge(*BASE, note)
        self.merge(note)
        notes = self.store.knowledge().tutor_notes
        self.assertEqual(len(notes), 1)
        self.assertEqual((notes[0]["topic"], notes[0]["diagnosis"], notes[0]["next"]), ("t_b", "Verwechselt", "Üben"))
        self.assertTrue(notes[0]["at"])


class SuggestionTests(Fixture):
    def suggest(self, field: str, value) -> None:
        self.merge({"t": "prof", "prof": "PR1", "field": field, "value": value, "src": []})

    def profile_now(self):
        return self.professors.get(self.profile.id)

    def test_nothing_is_applied_at_import(self):
        self.suggest("testing_style", "Viele Rechenaufgaben")
        self.merge({"t": "doc_note", "doc": "SL01", "issue": "wrong_subject", "detail": "Anderer Kurs"})
        self.assertEqual(self.profile_now().testing_style, "Eigener Text")
        self.assertEqual(self.store.knowledge().prof_suggestions[0]["state"], "open")
        self.assertEqual(self.subject.excluded_documents, set())
        self.assertFalse(self.store.knowledge().doc_notes[0]["dismissed"])

    def test_accepting_appends_and_never_replaces_the_students_text(self):
        self.suggest("testing_style", "Viele Rechenaufgaben")
        self.store.resolve_suggestion(0, True, self.professors)
        self.assertEqual(self.profile_now().testing_style, "Eigener Text\nViele Rechenaufgaben")
        self.assertEqual(self.store.knowledge().prof_suggestions[0]["state"], "accepted")

    def test_accepting_twice_applies_once(self):
        self.suggest("testing_style", "Viele Rechenaufgaben")
        self.store.resolve_suggestion(0, True, self.professors)
        self.store.resolve_suggestion(0, True, self.professors)
        self.assertEqual(self.profile_now().testing_style.count("Viele Rechenaufgaben"), 1)

    def test_lists_grow(self):
        profile = self.profile_now()
        profile.focus_areas = ["Bilanz"]
        self.professors.save(profile)
        self.suggest("focus_areas", ["Bilanz", "GuV"])
        self.store.resolve_suggestion(0, True, self.professors)
        self.assertEqual(self.profile_now().focus_areas, ["Bilanz", "GuV"])
        self.suggest("question_types", "Rechnen")
        self.store.resolve_suggestion(1, True, self.professors)
        self.assertEqual(self.profile_now().question_types, ["Rechnen"])

    def test_an_empty_field_is_simply_filled(self):
        self.suggest("grading_notes", "Streng")
        self.store.resolve_suggestion(0, True, self.professors)
        self.assertEqual(self.profile_now().grading_notes, "Streng")

    def test_dismissing_changes_nothing_and_the_suggestion_does_not_come_back(self):
        self.suggest("style_notes", "Mag Beispiele")
        self.store.resolve_suggestion(0, False, self.professors)
        self.assertEqual(self.profile_now().style_notes, "")
        self.assertEqual(self.store.knowledge().prof_suggestions[0]["state"], "dismissed")
        self.suggest("style_notes", "mag beispiele")
        self.assertEqual(len(self.store.knowledge().prof_suggestions), 1)

    def test_doc_notes_are_applied_only_on_request(self):
        key = self.view.document("SL01").key
        wrong = {"t": "doc_note", "doc": "SL01", "issue": "wrong_subject", "detail": "Anderer Kurs"}
        self.merge(wrong, {"t": "doc_note", "doc": "SL01", "issue": "outdated", "detail": "Alt"}, wrong)
        notes = self.store.knowledge().doc_notes
        self.assertEqual(len(notes), 2)
        self.assertEqual((notes[0]["doc"], notes[0]["ref"]), (key, "SL01"))
        self.store.apply_doc_note(1)
        self.assertEqual(self.subject.excluded_documents, set())
        self.assertTrue(self.store.knowledge().doc_notes[1]["dismissed"])
        self.store.dismiss_doc_note(0)
        self.assertEqual(self.subject.excluded_documents, set())
        self.store.apply_doc_note(0)
        self.assertEqual(self.subject.excluded_documents, {key})


class ReplyEventFileTests(Fixture):
    def test_events_file_holds_one_json_object_per_line(self):
        self.merge(*BASE, ClaudeAttemptTests.ATTEMPT)
        self.store.flag_question("q_1", "mehrere\nZeilen")
        lines = self.store.events_path.read_text(encoding="utf-8").split("\n")
        self.assertEqual(lines[-1], "")
        self.assertEqual([json.loads(line)["type"] for line in lines[:-1]], ["attempt", "flag"])


if __name__ == "__main__":
    unittest.main()
