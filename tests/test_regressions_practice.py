"""Regressions for the practice review list. Small and synthetic."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from universe.services import practice  # noqa: E402

NOW = datetime(2026, 11, 1, 18, 0)


def question(key, objective, **fields):
    return {
        "id": key, "objective": objective, "topic": fields.pop("topic", "t_a"), "pool": "practice", "type": "short", "stem": f"Stem of {key}",
        "answer": "Model answer", "verified": True, "rev": 1, "retired": False,
        "suspended": False, "flagged": "", **fields,
    }


def objective(key, topic="t_a", importance=2, **fields):
    return {"id": key, "topic": topic, "statement": f"Statement of {key}", "importance": importance, "verified": True, "retired": False, "lectures": ["l_1"], "confusable_with": [], **fields}


def knowledge(objectives=(), questions=(), **fields):
    topics = {t: {"id": t, "title": t.upper(), "importance": 2, "lectures": ["l_1"]} for t in {o["topic"] for o in objectives}}
    return {"topics": topics, "objectives": {o["id"]: o for o in objectives}, "questions": {q["id"]: q for q in questions}, "aliases": {}, "exam_model": {}, **fields}


_serial = iter(range(1000, 10**9))


def attempt(q, at=NOW, score=1.0, **fields):
    fields.setdefault("type", {"type": "mc"})
    return {
        "v": 1, "id": f"e{next(_serial):07d}", "at": at.isoformat(), "type": "attempt", "q": q, "rev": 1, "mode": "today", "answer": "yes", "correct": score >= 0.5,
        "score": score, "confidence": "sure", "hints": 0, "ms": 1000, "by": "auto",
        "rating": practice.rating_for(score, "sure", 0, fields.pop("type")), **fields,
    }


class MockPendingTests(unittest.TestCase):
    def test_pending_mock_free_answer_waits_for_grade(self):
        k = knowledge([objective("o1")], [question("q1", "o1")])
        at = NOW - timedelta(hours=1)
        pending = {
            "v": 1, "id": "a_pending", "at": at.isoformat(), "type": "attempt", "q": "q1", "rev": 1,
            "mode": "mock", "answer": "My words", "correct": False, "score": None, "confidence": "",
            "hints": 0, "ms": 1000, "by": "self", "rating": 1, "pending": True,
        }
        cards = practice.replay(k, [pending])
        self.assertEqual(cards["o1"].attempts, 0)
        self.assertIsNone(cards["o1"].memory)
        answers = [a for a in practice._answers(k, [pending]) if not a.stale]
        self.assertEqual(answers, [])
        state = practice.readiness(k, [pending], cards, NOW, None)
        self.assertEqual(state.answers, 0)
        self.assertIsNone(state.last_mock)
        days = practice.history([pending], NOW)
        self.assertEqual(sum(d.answers for d in days), 0)
        grade = {"v": 1, "id": "g1", "at": NOW.isoformat(), "type": "grade", "attempt": "a_pending", "score": 1.0, "by": "self"}
        cards2 = practice.replay(k, [pending, grade])
        self.assertEqual(cards2["o1"].attempts, 1)
        self.assertEqual(cards2["o1"].correct, 1)
        state2 = practice.readiness(k, [pending, grade], cards2, NOW, None)
        self.assertEqual(state2.answers, 1)
        self.assertIsNotNone(state2.last_mock)
        self.assertAlmostEqual(state2.last_mock.score, 1.0)


class PastExamTests(unittest.TestCase):
    def test_past_exam_clamps_days_and_hides_recall(self):
        from datetime import date

        k = knowledge([objective("o1")], [question("q1", "o1")])
        yesterday = (NOW - timedelta(days=1)).date()
        events = [attempt("q1", NOW - timedelta(days=d), score=1.0) for d in range(3) for _ in range(10)]
        cards = practice.replay(k, events)
        state = practice.readiness(k, events, cards, NOW, yesterday)
        self.assertEqual(state.days_left, 0)
        self.assertIsNone(state.recall_at_exam)
        self.assertEqual(practice._horizon(NOW, yesterday), practice.NO_DATE_DAYS)
        self.assertIsNotNone(state.recall_now)


class FreeConfidenceTests(unittest.TestCase):
    def panel(self):
        import os
        import tempfile

        os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        from universe.services.study_store import Knowledge, StudyStore
        from universe.storage import Workspace
        from universe.ui.practice_panel import PracticePanel

        app = QApplication.instance() or QApplication([])
        ws = Workspace(Path(tempfile.mkdtemp()))
        subject = ws.create_subject("Zynth")
        lecture = subject.create_lecture("Quil Week")
        store = StudyStore(subject)
        store._save(
            Knowledge(
                topics={"t_a": {"id": "t_a", "title": "T_A", "importance": 2, "lectures": [lecture.uid]}},
                objectives={"o1": {"id": "o1", "topic": "t_a", "statement": "Quil flarn explained", "importance": 2, "verified": True, "retired": False, "lectures": [lecture.uid], "confusable_with": []}},
                questions={
                    "q1": {
                        "id": "q1", "objective": "o1", "topic": "t_a", "pool": "practice", "type": "short",
                        "stem": "Explain quil flarn.", "answer": "Flarn quilts the quib.",
                        "verified": True, "rev": 1, "retired": False, "suspended": False, "flagged": "",
                    }
                },
            )
        )
        panel = PracticePanel()
        panel.set_scope(subject, None)
        self.addCleanup(app.processEvents)
        self.addCleanup(panel.deleteLater)
        return panel, store

    def test_not_knowing_a_free_answer_shows_it(self):
        panel, store = self.panel()
        panel.start()
        panel.guess.click()
        self.assertIn("Flarn quilts", panel.model_answer.text())
        panel.missed.click()
        self.assertEqual([e["confidence"] for e in store.events() if e["type"] == "attempt"], ["guess"])

    def test_only_a_written_mock_answer_waits_for_its_grade(self):
        panel, store = self.panel()
        panel.start()
        panel.mode = "mock"
        panel.save_free_for_later()
        blank = [e for e in store.events() if e["type"] == "attempt"][-1]
        self.assertEqual((blank["score"], blank.get("pending")), (0.0, None))

    def test_free_answer_asks_confidence_outside_mock(self):
        panel, store = self.panel()
        panel.start()
        self.assertFalse(panel.confidence_row.isHidden())
        self.assertIn("shown", panel.confidence_title.text().lower())
        panel.answer_text.setPlainText("My quil words")
        panel.sure.click()
        self.assertIn("Flarn quilts", panel.model_answer.text())
        panel.got_it.click()
        events = [e for e in store.events() if e["type"] == "attempt"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["confidence"], "sure")


class RatingFourTests(unittest.TestCase):
    def test_rating_four_from_events_is_recomputed(self):
        k = knowledge([objective("o1")], [question("q1", "o1")])
        at = NOW - timedelta(days=1)
        old = {
            "v": 1, "id": "a_old", "at": at.isoformat(), "type": "attempt", "q": "q1", "rev": 1,
            "mode": "today", "answer": "My words", "correct": True, "score": 1.0, "confidence": "sure",
            "hints": 0, "ms": 1000, "by": "auto", "rating": 4,
        }
        answers = practice._answers(k, [old])
        self.assertEqual(len(answers), 1)
        self.assertIn(answers[0].rating, (1, 2, 3))
        expected = practice.rating_for(1.0, "sure", 0, k["questions"]["q1"])
        self.assertEqual(answers[0].rating, expected)
        cards = practice.replay(k, [old])
        self.assertNotEqual(cards["o1"].memory.stability, practice.fsrs.WEIGHTS[3])


if __name__ == "__main__":
    unittest.main()
