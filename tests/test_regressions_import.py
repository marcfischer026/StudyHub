"""Regression tests for import findings."""
from __future__ import annotations
import os
import sys
import tempfile
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())
from tests.test_claude_reply import HEAD, MC, OBJECTIVE, TOPIC
from tests.test_study_store import BASE, Fixture as StoreFixture, probe


class Import3Tests(StoreFixture):
    def test_weak_recitation_keeps_verified(self):
        self.merge(*BASE)
        self.assertTrue(self.question()["verified"])
        weak = {**MC, "src": [{"id": "N01.1", "q": "Die Bilanz hat zwei Seiten"}]}
        self.merge(weak)
        kept = self.question()
        self.assertTrue(kept["verified"])
        self.assertTrue(any(s.get("label") == "SL01.p1" for s in kept["sources"]))


class Practice5Tests(StoreFixture):
    def holdouts(self):
        return [k for k, q in self.store.knowledge().questions.items() if q["pool"] == "holdout"]

    def test_flagged_holdout_gets_a_replacement(self):
        self.merge(TOPIC, OBJECTIVE, probe(1), probe(2), probe(3))
        first = self.holdouts()
        self.assertEqual(len(first), 1)
        self.store.flag_question(first[0], "doubt")
        self.merge(probe(4))
        usable = [q for k, q in self.store.knowledge().questions.items() if q["pool"] == "holdout" and not q.get("flagged") and not q.get("suspended") and q.get("verified")]
        self.assertEqual(len(usable), 1)
        self.assertNotEqual(usable[0]["id"], first[0])


class Import5Tests(StoreFixture):
    def holdouts(self):
        return [k for k, q in self.store.knowledge().questions.items() if q["pool"] == "holdout"]

    def test_suspended_holdout_gets_a_replacement(self):
        self.merge(TOPIC, OBJECTIVE, probe(1), probe(2), probe(3))
        first = self.holdouts()
        self.assertEqual(len(first), 1)
        self.store.set_suspended(first[0], True)
        self.merge(probe(4), probe(5))
        usable = [q for k, q in self.store.knowledge().questions.items() if q["pool"] == "holdout" and not q.get("suspended") and not q.get("flagged") and q.get("verified")]
        self.assertEqual(len(usable), 1)
        self.assertNotEqual(usable[0]["id"], first[0])


class Practice6Tests(StoreFixture):
    def test_objective_without_importance_still_gets_a_holdout(self):
        from universe.services.study_store import Knowledge, StudyStore
        knowledge = Knowledge()
        knowledge.objectives["L01-o001"] = {"topic": "t_b", "verified": True}
        for n in (1, 2, 3):
            knowledge.questions[f"q_{n}"] = {"id": f"q_{n}", "objective": "L01-o001", "topic": "t_b", "verified": True, "pool": "practice"}
        StudyStore._assign_holdouts(knowledge, [])
        holdouts = [k for k, q in knowledge.questions.items() if q["pool"] == "holdout"]
        self.assertEqual(len(holdouts), 1)


class Import6Tests(StoreFixture):
    def test_figure_locates_its_lecture(self):
        from universe.services.subject_model import locate
        source = {"kind": "figure", "lecture": self.lecture.uid, "label": "F01.1"}
        found = locate(self.view, source)
        self.assertIsNotNone(found)
        self.assertEqual(found["lecture"], self.view.lecture(self.lecture.uid).lecture)


class Export1Tests(StoreFixture):
    def test_recording_must_be_accounted_for(self):
        checked = self.check({"t": "coverage", "lecture": "L01", "covered": ["SL01"]})
        pages = [i["page"] for i in checked.uncovered.get("L01", ())]
        self.assertIn("R01", pages)

    def test_covered_recording_is_accounted(self):
        checked = self.check({"t": "coverage", "lecture": "L01", "covered": ["SL01", "R01", "R02"]})
        self.assertEqual(checked.uncovered, {})

    def test_cited_line_accounts_for_its_recording(self):
        checked = self.check({"t": "coverage", "lecture": "L01", "covered": ["SL01"]}, TOPIC, {**OBJECTIVE, "truth": "said", "src": [{"id": "R01@797", "q": "Das kommt in der Klausur dran"}]})
        pages = [i["page"] for i in checked.uncovered.get("L01", ())]
        self.assertNotIn("R01", pages)
        self.assertIn("R02", pages)


class Export2Tests(StoreFixture):
    def test_citation_of_excluded_document_warns(self):
        import universe.services.claude_reply as reply_mod
        from tests.test_claude_reply import jsonl as make_jsonl
        from universe.services.claude_reply import check_reply, parse_reply
        self.subject.set_document_excluded(self.view.document("SL01").key, True)
        view = self.load()
        reply_mod.save_manifest(self.subject, reply_mod.build_manifest(view, list(view.lectures), "x2"))
        text = make_jsonl(TOPIC, OBJECTIVE, head={**HEAD, "export": "x2"})
        checked = check_reply(parse_reply(text), view)
        sources = checked.records["objective"][0]["sources"]
        self.assertEqual(sources, [])
        self.assertIn("SL01.p1", "\n".join(i.message for i in checked.issues))


if __name__ == "__main__":
    unittest.main()
