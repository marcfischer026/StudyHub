"""Practice: replay, ranking, sessions, mocks, readiness and the brief for Claude."""

from __future__ import annotations

import math
import random
import sys
import time as clock
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from universe.services import fsrs, practice  # noqa: E402
from universe.services.fsrs import AGAIN, GOOD, HARD  # noqa: E402
from universe.services.practice import Card  # noqa: E402

NOW = datetime(2026, 11, 1, 18, 0)
MC = {"type": "mc"}
FREE = {"type": "short"}


def question(key, objective, **fields):
    return {
        "id": key, "objective": objective, "topic": fields.pop("topic", "t_a"), "pool": "practice", "type": "mc", "stem": f"Stem of {key}",
        "options": [{"text": "yes", "correct": True}, {"text": "no", "why_wrong": "because"}], "verified": True, "rev": 1, "retired": False,
        "suspended": False, "flagged": "", **fields,
    }


def objective(key, topic="t_a", importance=2, **fields):
    return {"id": key, "topic": topic, "statement": f"Statement of {key}", "importance": importance, "verified": True, "retired": False, "lectures": ["l_1"], "confusable_with": [], **fields}


def knowledge(objectives=(), questions=(), **fields):
    topics = {t: {"id": t, "title": t.upper(), "importance": 2, "lectures": ["l_1"]} for t in {o["topic"] for o in objectives}}
    return {"topics": topics, "objectives": {o["id"]: o for o in objectives}, "questions": {q["id"]: q for q in questions}, "aliases": {}, "exam_model": {}, **fields}


_serial = iter(range(1, 10**9))


def attempt(q, at=NOW, score=1.0, confidence="sure", rating=None, rev=1, mode="today", answer="yes", by="auto", hints=0, id=None, **fields):
    question_type = fields.pop("type", MC)
    return {
        "v": 1, "id": id or f"e{next(_serial):07d}", "at": at.isoformat(), "type": "attempt", "q": q, "rev": rev, "mode": mode, "answer": answer, "correct": score >= 0.5,
        "score": score, "confidence": confidence, "hints": hints, "ms": 1000, "by": by,
        "rating": rating if rating is not None else practice.rating_for(score, confidence, hints, question_type), **fields,
    }


def days_ago(days, at=NOW):
    return at - timedelta(days=days)


def card_with(key, stability=5.0, days=5, seen=()):
    last = days_ago(days)
    return Card(key, fsrs.Memory(stability, 5.0, last), 1, 1, 0, last, {q: last for q in seen}, [GOOD])


class Grading(unittest.TestCase):
    def test_mc_by_option_text_and_letter(self):
        q = question("q", "o")
        self.assertEqual(practice.grade_answer(q, "yes"), 1.0)
        self.assertEqual(practice.grade_answer(q, " YES "), 1.0)
        self.assertEqual(practice.grade_answer(q, "no"), 0.0)
        self.assertEqual(practice.grade_answer(q, "a)"), 1.0)
        self.assertEqual(practice.grade_answer(q, "B"), 0.0)
        self.assertEqual(practice.grade_answer(q, ""), 0.0)
        self.assertEqual(practice.grade_answer(q, "maybe"), 0.0)

    def test_tf_understands_german(self):
        q = {"type": "tf", "answer": "True"}
        for answer in ("True", "true", "Wahr", "richtig", "ja"):
            self.assertEqual(practice.grade_answer(q, answer), 1.0, answer)
        for answer in ("False", "falsch", "nein", "", "keine Ahnung"):
            self.assertEqual(practice.grade_answer(q, answer), 0.0, answer)
        self.assertEqual(practice.grade_answer({"type": "tf", "answer": "False"}, "Falsch"), 1.0)

    def test_calc_reads_number_formats(self):
        q = {"type": "calc", "numeric": {"value": 1200.5, "tolerance": 0.01, "unit": "€"}}
        for answer in ("1.200,50", "1200.5", "1200,5", "1,200.50", "1 200,50", "1200,50 €", "€ 1.200,50", "x = 1.200,50", "2 * 600,25 = 1200,50"):
            self.assertEqual(practice.grade_answer(q, answer), 1.0, answer)
        for answer in ("1.200", "12005", "1.201,50", "", "keine Ahnung", "-1200,5"):
            self.assertEqual(practice.grade_answer(q, answer), 0.0, answer)

    def test_calc_three_digits_after_a_mark_can_mean_either(self):
        thousand = {"type": "calc", "numeric": {"value": 1200, "tolerance": 0}}
        small = {"type": "calc", "numeric": {"value": 1.2, "tolerance": 0}}
        for q in (thousand, small):
            self.assertEqual(practice.grade_answer(q, "1.200"), 1.0)
            self.assertEqual(practice.grade_answer(q, "1,200"), 1.0)
        self.assertEqual(practice.grade_answer(thousand, "1.200.000"), 0.0)
        self.assertEqual(practice.grade_answer({"type": "calc", "numeric": {"value": 1200000}}, "1.200.000"), 1.0)
        self.assertEqual(practice.grade_answer({"type": "calc", "numeric": {"value": 0.125}}, "0,125"), 1.0)

    def test_calc_tolerance(self):
        q = {"type": "calc", "numeric": {"value": 100, "tolerance": 0.5}}
        self.assertEqual(practice.grade_answer(q, "100,5"), 1.0)
        self.assertEqual(practice.grade_answer(q, "99.5"), 1.0)
        self.assertEqual(practice.grade_answer(q, "100,6"), 0.0)
        self.assertEqual(practice.grade_answer({"type": "calc", "numeric": {"value": -5, "tolerance": 0}}, "−5"), 1.0)
        self.assertEqual(practice.grade_answer({"type": "calc", "numeric": {"value": 100, "tolerance": 0}}, "100,01"), 0.0)

    def test_calc_without_a_tolerance_forgives_rounding_only(self):
        q = {"type": "calc", "numeric": {"value": 33.3333}}
        self.assertEqual(practice.grade_answer(q, "33,33"), 1.0)
        self.assertEqual(practice.grade_answer(q, "34"), 0.0)

    def test_calc_without_numeric_falls_back_or_asks_the_student(self):
        self.assertEqual(practice.grade_answer({"type": "calc", "answer": "1.200,50 €"}, "1200,5"), 1.0)
        self.assertIsNone(practice.grade_answer({"type": "calc", "answer": "Erst 5 addieren, dann durch 3 teilen"}, "5"))
        self.assertIsNone(practice.grade_answer({"type": "calc"}, "5"))

    def test_free_answers_need_the_student(self):
        for kind in ("short", "case", "card"):
            self.assertIsNone(practice.grade_answer({"type": kind, "answer": "x"}, "x"))


class RatingMap(unittest.TestCase):
    def test_rows(self):
        rows = [
            # score, confidence, hints, question, rating
            (0.0, "sure", 0, MC, AGAIN),
            (0.49, "sure", 0, FREE, AGAIN),
            (0.2, "guess", 0, FREE, AGAIN),
            (0.5, "sure", 0, FREE, HARD),
            (0.85, "sure", 0, FREE, HARD),
            (0.86, "sure", 0, FREE, GOOD),
            (1.0, "sure", 0, MC, GOOD),
            (1.0, "", 0, MC, GOOD),
            (1.0, "unsure", 0, MC, HARD),
            (1.0, "unsure", 0, FREE, HARD),
            (1.0, "guess", 0, MC, AGAIN),
            (1.0, "guess", 0, {"type": "tf"}, AGAIN),
            (1.0, "guess", 0, FREE, HARD),
            (1.0, "guess", 0, {"type": "calc"}, HARD),
            (1.0, "guess", 0, {"type": "card"}, HARD),
        ]
        for score, confidence, hints, q, rating in rows:
            self.assertEqual(practice.rating_for(score, confidence, hints, q), rating, (score, confidence, q))

    def test_any_hint_caps_at_hard(self):
        self.assertEqual(practice.rating_for(1.0, "sure", 1, MC), HARD)
        self.assertEqual(practice.rating_for(1.0, "sure", 3, FREE), HARD)
        self.assertEqual(practice.rating_for(0.7, "sure", 1, FREE), HARD)
        self.assertEqual(practice.rating_for(0.2, "sure", 1, FREE), AGAIN)
        self.assertEqual(practice.rating_for(1.0, "guess", 1, MC), AGAIN)

    def test_easy_is_never_given(self):
        for score in (0, 0.5, 0.9, 1):
            for confidence in (*practice.CONFIDENCE, ""):
                self.assertLessEqual(practice.rating_for(score, confidence, 0, MC), GOOD)


class Replay(unittest.TestCase):
    def setUp(self):
        self.k = knowledge(
            [objective("o1"), objective("o2")],
            [question("q1", "o1"), question("q2", "o1"), question("q3", "o2")],
        )

    def test_every_objective_gets_a_card(self):
        cards = practice.replay(self.k, [])
        self.assertEqual(set(cards), {"o1", "o2"})
        self.assertIsNone(cards["o1"].memory)
        self.assertEqual(cards["o1"].attempts, 0)

    def test_questions_are_probes_of_one_memory(self):
        first, second = days_ago(5), days_ago(1)
        cards = practice.replay(self.k, [attempt("q1", first), attempt("q2", second, score=0.0)])
        card = cards["o1"]
        expected = fsrs.review(fsrs.first_review(GOOD, first), AGAIN, second)
        self.assertEqual(card.memory, expected)
        self.assertEqual((card.attempts, card.correct, card.lapses), (2, 1, 1))
        self.assertEqual(card.last_seen, second)
        self.assertEqual(card.seen, {"q1": first, "q2": second})
        self.assertEqual(cards["o2"].attempts, 0)

    def test_order_in_the_file_does_not_matter(self):
        events = [attempt("q1", days_ago(n), score=(n % 3) / 2) for n in range(20)]
        forward = practice.replay(self.k, events)["o1"]
        shuffled = events[:]
        random.Random(7).shuffle(shuffled)
        self.assertEqual(practice.replay(self.k, shuffled)["o1"], forward)
        self.assertEqual(practice.replay(self.k, events)["o1"], forward)

    def test_grade_overrides_score_and_rating(self):
        wrong = attempt("q1", days_ago(5), score=1.0, id="a1", by="self")
        graded = {"v": 1, "id": "g1", "at": days_ago(2).isoformat(), "type": "grade", "attempt": "a1", "score": 0.0, "feedback": "No", "by": "claude"}
        card = practice.replay(self.k, [wrong, graded])["o1"]
        self.assertEqual(card.memory.stability, fsrs.WEIGHTS[0])
        self.assertEqual(card.correct, 0)
        later = {**graded, "id": "g2", "at": days_ago(1).isoformat(), "score": 0.7}
        card = practice.replay(self.k, [wrong, graded, later])["o1"]
        self.assertEqual(card.memory.stability, fsrs.WEIGHTS[1])
        self.assertEqual(card.correct, 1)

    def test_grade_of_something_unknown_is_harmless(self):
        grade = {"id": "g", "at": NOW.isoformat(), "type": "grade", "attempt": "nope", "score": 0.0}
        self.assertEqual(practice.replay(self.k, [grade, attempt("q1")])["o1"].correct, 1)

    def test_rev_bump_drops_older_attempts_from_memory_only(self):
        self.k["questions"]["q1"]["rev"] = 2
        old = days_ago(9)
        card = practice.replay(self.k, [attempt("q1", old, rev=1)])["o1"]
        self.assertIsNone(card.memory)
        self.assertEqual(card.attempts, 0)
        self.assertEqual(card.seen, {"q1": old})
        card = practice.replay(self.k, [attempt("q1", old, rev=1), attempt("q1", days_ago(1), rev=2, score=0.0)])["o1"]
        self.assertEqual(card.attempts, 1)
        self.assertEqual(card.memory.stability, fsrs.WEIGHTS[0])

    def test_aliases_fold_attempts_together(self):
        k = knowledge(
            [objective("new"), objective("old", retired=True), objective("older", retired=True)],
            [question("q1", "new"), question("q2", "old"), question("q3", "older")],
            aliases={"old": "new", "older": "old"},
        )
        cards = practice.replay(k, [attempt("q2", days_ago(5), o="old"), attempt("q3", days_ago(3), o="older"), attempt("q1", days_ago(1), o="new")])
        self.assertEqual(set(cards), {"new"})
        self.assertEqual(cards["new"].attempts, 3)
        self.assertEqual(set(cards["new"].seen), {"q1", "q2", "q3"})

    def test_alias_cycle_does_not_hang(self):
        k = knowledge([objective("a")], [question("q", "a")], aliases={"a": "b", "b": "a"})
        practice.replay(k, [attempt("q")])

    def test_unknown_question_goes_by_the_events_objective(self):
        cards = practice.replay(self.k, [attempt("gone", days_ago(1), o="o2"), attempt("gone too", days_ago(1))])
        self.assertEqual(cards["o2"].attempts, 1)
        self.assertEqual(len(cards), 2)

    def test_bad_events_are_skipped_and_missing_fields_filled(self):
        events = [
            {"type": "attempt", "q": "q1", "at": "not a date"},
            {"type": "attempt", "q": "q1"},
            {"type": "flag", "q": "q1", "reason": "x", "at": NOW.isoformat()},
            {"type": "attempt", "q": "q3", "at": "2026-10-20T10:00:00", "correct": True},
            {"type": "attempt", "q": "q3", "at": "2026-10-21T10:00:00+01:00", "score": 0.0},
        ]
        cards = practice.replay(self.k, events)
        self.assertEqual(cards["o1"].attempts, 0)
        self.assertEqual((cards["o2"].attempts, cards["o2"].correct), (2, 1))

    def test_ten_thousand_events_replay_fast(self):
        objectives = [objective(f"o{i}") for i in range(300)]
        questions = [question(f"q{i}_{j}", f"o{i}") for i in range(300) for j in range(4)]
        k = knowledge(objectives, questions)
        rng = random.Random(1)
        start = datetime(2026, 1, 1)
        events = []
        for i in range(10_000):
            at = start + timedelta(minutes=30 * i)
            events.append(attempt(f"q{rng.randrange(300)}_{rng.randrange(4)}", at, score=rng.random()))
            if i % 50 == 0:
                events.append({"id": f"g{i}", "at": at.isoformat(), "type": "grade", "attempt": events[-1]["id"], "score": 0.5})
        began = clock.perf_counter()
        cards = practice.replay(k, events)
        elapsed = clock.perf_counter() - began
        self.assertEqual(sum(card.attempts for card in cards.values()), 10_000)
        self.assertLess(elapsed, 1.0)


class Ranking(unittest.TestCase):
    def rank(self, k, cards, limit=1, **kwargs):
        return [q["objective"] for q in practice.plan_session(k, cards, NOW, limit=limit, **kwargs)]

    def test_formula_for_new_and_seen(self):
        new = practice._value(objective("o", importance=3), None, NOW, 30)
        w = fsrs.WEIGHTS
        self.assertAlmostEqual(new, 3.0 * (0.6 * fsrs.recall(30, w[2]) + 0.4 * fsrs.recall(30, w[0])))
        card = card_with("o", stability=5.0, days=5)
        memory = card.memory
        r, good, lapse = fsrs.recall(5, 5.0), fsrs.review(memory, GOOD, NOW).stability, fsrs.review(memory, AGAIN, NOW).stability
        expected = 1.5 * (r * fsrs.recall(30, good) + (1 - r) * fsrs.recall(30, lapse) - fsrs.recall(35, 5.0))
        self.assertAlmostEqual(practice._value(objective("o", importance=2), card, NOW, 30), expected)

    def test_importance_three_beats_one_at_equal_state(self):
        k = knowledge([objective("a", importance=1), objective("b", importance=3)], [question("qa", "a"), question("qb", "b")])
        self.assertEqual(self.rank(k, {}), ["b"])
        cards = {"a": card_with("a"), "b": card_with("b")}
        self.assertEqual(self.rank(k, cards), ["b"])

    def test_answered_today_is_excluded(self):
        k = knowledge([objective("a"), objective("b")], [question("qa", "a"), question("qb", "b")])
        cards = {"a": card_with("a", days=0)}
        cards["a"].last_seen = NOW.replace(hour=8)
        self.assertEqual(self.rank(k, cards, limit=5), ["b"])
        cards["a"].last_seen = days_ago(1)
        self.assertEqual(set(self.rank(k, cards, limit=5)), {"a", "b"})

    def test_new_material_and_reviews_compete_on_one_scale(self):
        k = knowledge(
            [objective("new", importance=1), objective("old", importance=3)],
            [question("qn", "new"), question("qo", "old")],
        )
        weak = {"old": card_with("old", stability=3.0, days=10)}
        self.assertEqual(self.rank(k, weak, exam_date=date(2026, 11, 20)), ["old"])
        strong = {"old": card_with("old", stability=400.0, days=1)}
        self.assertEqual(self.rank(k, strong, exam_date=date(2026, 11, 20)), ["new"])

    def test_no_date_means_thirty_days(self):
        k = knowledge([objective("a"), objective("b", importance=3)], [question("qa", "a"), question("qb", "b")])
        cards = {"a": card_with("a", stability=4.0, days=9)}
        without = practice._value(k["objectives"]["a"], cards["a"], NOW, practice._horizon(NOW, None))
        self.assertEqual(practice._horizon(NOW, None), 30)
        self.assertAlmostEqual(without, practice._value(k["objectives"]["a"], cards["a"], NOW, 30))
        self.assertEqual(practice._horizon(NOW, date(2026, 12, 1)), 30)
        self.assertEqual(practice._horizon(NOW, date(2026, 11, 1)), 1)
        self.assertEqual(practice._horizon(NOW, date(2020, 1, 1)), 1)
        self.assertEqual(practice._horizon(NOW, "2026-11-11"), 10)


class Sessions(unittest.TestCase):
    def test_ninety_ten_split(self):
        """20 slots: 18 by value, 2 from the objectives nobody has looked at for longest."""
        objectives = [objective(f"top{i:02d}", topic=f"t{i % 5}", importance=3) for i in range(30)]
        objectives += [objective("forgotten1"), objective("forgotten2"), objective("forgotten_low", importance=1)]
        questions = [question(f"q_{o['id']}", o["id"], topic=o["topic"]) for o in objectives]
        k = knowledge(objectives, questions)
        cards = {f"top{i:02d}": card_with(f"top{i:02d}", stability=2.0, days=4) for i in range(30)}
        for key in ("forgotten1", "forgotten2", "forgotten_low"):
            cards[key] = card_with(key, stability=900.0, days=200)
        cards["forgotten_low"] = card_with("forgotten_low", stability=900.0, days=300)
        session = practice.plan_session(k, cards, NOW, limit=20)
        chosen = {q["objective"] for q in session}
        self.assertEqual(len(session), 20)
        self.assertEqual(sum(key.startswith("top") for key in chosen), 18)
        self.assertLessEqual({"forgotten1", "forgotten2"}, chosen)
        self.assertNotIn("forgotten_low", chosen)

    def test_guard_slots_go_back_when_nothing_long_unseen_exists(self):
        objectives = [objective(f"o{i:02d}") for i in range(30)]
        k = knowledge(objectives, [question(f"q{i:02d}", f"o{i:02d}") for i in range(30)])
        self.assertEqual(len(practice.plan_session(k, {}, NOW, limit=20)), 20)

    def test_never_more_than_the_limit_and_one_probe_per_objective(self):
        k = knowledge([objective("a"), objective("b")], [question(f"q{i}", "a") for i in range(4)] + [question("qb", "b")])
        session = practice.plan_session(k, {}, NOW, limit=20)
        self.assertEqual(sorted(q["objective"] for q in session), ["a", "b"])
        self.assertEqual(practice.plan_session(k, {}, NOW, limit=0), [])

    def test_retired_suspended_and_flagged_never_appear(self):
        k = knowledge(
            [objective("o"), objective("gone", retired=True)],
            [
                question("retired", "o", retired=True),
                question("suspended", "o", suspended=True),
                question("flagged", "o", flagged="wrong answer"),
                question("fine", "o"),
                question("orphan", "gone"),
            ],
        )
        self.assertEqual([q["id"] for q in practice.plan_session(k, {}, NOW)], ["fine"])
        k["questions"]["fine"]["suspended"] = True
        self.assertEqual(practice.plan_session(k, {}, NOW), [])

    def test_retired_topic_takes_its_objectives_along(self):
        k = knowledge([objective("o")], [question("q", "o")])
        k["topics"]["t_a"]["retired"] = True
        self.assertEqual(practice.plan_session(k, {}, NOW), [])

    def test_holdout_stays_out_until_used_in_a_mock(self):
        k = knowledge([objective("o")], [question("held", "o", pool="holdout"), question("open", "o")])
        self.assertEqual([q["id"] for q in practice.plan_session(k, {}, NOW)], ["open"])
        cards = practice.replay(k, [attempt("open", days_ago(3))])
        self.assertEqual([q["id"] for q in practice.plan_session(k, cards, NOW)], ["open"])
        k["questions"]["open"]["retired"] = True
        self.assertEqual(practice.plan_session(k, cards, NOW), [])
        cards = practice.replay(k, [attempt("open", days_ago(3)), attempt("held", days_ago(2), mode="mock")])
        self.assertEqual([q["id"] for q in practice.plan_session(k, cards, NOW)], ["held"])

    def test_probe_is_the_least_recently_seen(self):
        k = knowledge([objective("o")], [question("a", "o"), question("b", "o"), question("c", "o")])
        cards = practice.replay(k, [attempt("a", days_ago(10)), attempt("b", days_ago(9)), attempt("c", days_ago(8))])
        self.assertEqual(practice.plan_session(k, cards, NOW)[0]["id"], "a")
        cards = practice.replay(k, [attempt("a", days_ago(8)), attempt("b", days_ago(9))])
        self.assertEqual(practice.plan_session(k, cards, NOW)[0]["id"], "c")

    def test_higher_bloom_once_the_memory_is_strong(self):
        k = knowledge([objective("o")], [question("a", "o", bloom="remember"), question("z", "o", bloom="analyze")])
        self.assertEqual(practice.plan_session(k, {"o": card_with("o", stability=2.0)}, NOW)[0]["id"], "a")
        self.assertEqual(practice.plan_session(k, {"o": card_with("o", stability=7.0)}, NOW)[0]["id"], "z")

    def test_no_topic_twice_in_a_row_when_avoidable(self):
        objectives = [objective(f"a{i}", topic="t_a", importance=3) for i in range(6)] + [objective(f"b{i}", topic="t_b") for i in range(5)] + [objective("c", topic="t_c", importance=1)]
        k = knowledge(objectives, [question(f"q_{o['id']}", o["id"], topic=o["topic"]) for o in objectives])
        session = practice.plan_session(k, {}, NOW, limit=12)
        self.assertEqual(len(session), 12)
        topics = [q["topic"] for q in session]
        self.assertFalse(any(x == y for x, y in zip(topics, topics[1:])), topics)

    def test_one_topic_only_still_works(self):
        k = knowledge([objective(f"o{i}") for i in range(4)], [question(f"q{i}", f"o{i}") for i in range(4)])
        self.assertEqual(len(practice.plan_session(k, {}, NOW)), 4)

    def test_confusable_pairs_sit_side_by_side(self):
        objectives = [objective(f"x{i}", topic=f"t{i}", importance=3) for i in range(6)]
        objectives += [objective("left", topic="t_l", importance=3, confusable_with=["right"]), objective("right", topic="t_r", importance=1)]
        k = knowledge(objectives, [question(f"q_{o['id']}", o["id"], topic=o["topic"]) for o in objectives])
        order = [q["objective"] for q in practice.plan_session(k, {}, NOW)]
        self.assertEqual(abs(order.index("left") - order.index("right")), 1)
        # also when only the other side names the pair, and through a merged id
        k["objectives"]["left"]["confusable_with"] = []
        k["objectives"]["right"]["confusable_with"] = ["was_left"]
        k["aliases"]["was_left"] = "left"
        order = [q["objective"] for q in practice.plan_session(k, {}, NOW)]
        self.assertEqual(abs(order.index("left") - order.index("right")), 1)

    def test_topic_and_lecture_modes(self):
        k = knowledge(
            [objective("a", topic="t_a", lectures=["l_1"]), objective("b", topic="t_b", lectures=["l_2"])],
            [question("qa", "a", topic="t_a"), question("qb", "b", topic="t_b")],
        )
        self.assertEqual([q["id"] for q in practice.plan_session(k, {}, NOW, mode="topic", topic="t_b")], ["qb"])
        self.assertEqual([q["id"] for q in practice.plan_session(k, {}, NOW, mode="lecture", lecture="l_1")], ["qa"])

    def test_weak_mode_orders_by_what_is_missing_and_skips_new(self):
        k = knowledge(
            [objective("low", importance=1), objective("high", importance=3), objective("firm", importance=3), objective("new", importance=3)],
            [question(f"q_{key}", key) for key in ("low", "high", "firm", "new")],
        )
        cards = {
            "low": card_with("low", stability=1.0, days=30),
            "high": card_with("high", stability=2.0, days=10),
            "firm": card_with("firm", stability=500.0, days=2),
        }
        weakest = [q["objective"] for q in practice.plan_session(k, cards, NOW, mode="weak", limit=1)]
        self.assertEqual(weakest, ["high"])
        self.assertNotIn("new", [q["objective"] for q in practice.plan_session(k, cards, NOW, mode="weak")])
        self.assertEqual([o["id"] for o in practice.weakest(k, cards, NOW)], ["high", "low", "firm"])

    def test_mock_mode_is_the_mock(self):
        k = knowledge([objective("o")], [question("held", "o", pool="holdout"), question("open", "o", verified=False)])
        self.assertEqual([q["id"] for q in practice.plan_session(k, {}, NOW, mode="mock")], ["held"])

    def test_plan_is_deterministic_and_leaves_knowledge_alone(self):
        k = knowledge([objective(f"o{i}", topic=f"t{i % 3}") for i in range(20)], [question(f"q{i}", f"o{i}", topic=f"t{i % 3}") for i in range(20)])
        first = practice.plan_session(k, {}, NOW)
        first[0]["stem"] = "changed"
        self.assertEqual(k["questions"][first[0]["id"]]["stem"], f"Stem of {first[0]['id']}")
        again = practice.plan_session(k, {}, NOW)
        self.assertEqual([q["id"] for q in first], [q["id"] for q in again])


class AgainInSession(unittest.TestCase):
    def setUp(self):
        self.k = knowledge(
            [objective("o"), *[objective(f"x{i}", topic=f"t{i}") for i in range(6)]],
            [question("q1", "o"), question("q2", "o"), question("q3", "o"), *[question(f"xq{i}", f"x{i}", topic=f"t{i}") for i in range(6)]],
        )
        self.session = [dict(question("q1", "o"))] + [dict(question(f"xq{i}", f"x{i}", topic=f"t{i}")) for i in range(6)]

    def test_rating_one_brings_another_probe_later(self):
        item = practice.requeue_again(self.k, {}, self.session, 0, self.session[0], AGAIN)
        self.assertEqual(item["objective"], "o")
        self.assertNotEqual(item["id"], "q1")
        self.assertTrue(item["again"])
        self.assertEqual(len(self.session), 8)
        self.assertGreater(self.session.index(item), 1)

    def test_only_rating_one(self):
        for rating in (HARD, GOOD):
            self.assertIsNone(practice.requeue_again(self.k, {}, self.session, 0, self.session[0], rating))
        self.assertEqual(len(self.session), 7)

    def test_only_once(self):
        item = practice.requeue_again(self.k, {}, self.session, 0, self.session[0], AGAIN)
        self.assertIsNone(practice.requeue_again(self.k, {}, self.session, self.session.index(item), item, AGAIN))
        self.assertEqual(len(self.session), 8)

    def test_no_other_probe_means_no_comeback(self):
        k = knowledge([objective("o")], [question("q1", "o"), question("held", "o", pool="holdout"), question("bad", "o", flagged="x")])
        session = [dict(k["questions"]["q1"])]
        self.assertIsNone(practice.requeue_again(k, {}, session, 0, session[0], AGAIN))

    def test_at_the_end_of_a_session_it_still_comes_back(self):
        session = [dict(question("q1", "o"))]
        self.assertIsNotNone(practice.requeue_again(self.k, {}, session, 0, session[0], AGAIN))
        self.assertEqual([q["id"] for q in session][0], "q1")
        self.assertEqual(len(session), 2)

    def test_a_new_session_the_same_day_keeps_the_promise(self):
        """The session may have been closed; the event log still knows."""
        morning = NOW.replace(hour=9)
        k = knowledge([objective("o"), objective("p")], [question("q1", "o"), question("q2", "o"), question("qp", "p")])
        cards = practice.replay(k, [attempt("q1", morning, score=0.0)])
        session = practice.plan_session(k, cards, NOW)
        self.assertEqual([q["id"] for q in session], ["qp", "q2"])
        self.assertTrue(session[-1]["again"])
        self.assertNotIn("again", session[0])
        cards = practice.replay(k, [attempt("q1", morning, score=0.0), attempt("q2", morning + timedelta(minutes=3), score=0.0)])
        self.assertEqual([q["id"] for q in practice.plan_session(k, cards, NOW)], ["qp"])
        cards = practice.replay(k, [attempt("q1", morning)])
        self.assertEqual([q["id"] for q in practice.plan_session(k, cards, NOW)], ["qp"])


class Mocks(unittest.TestCase):
    def build(self, per_topic=30):
        objectives, questions = [], []
        for topic in ("t_a", "t_b"):
            for i in range(per_topic):
                key = f"{topic}_o{i}"
                objectives.append(objective(key, topic=topic, importance=3))
                questions.append(question(f"{key}_mc", key, topic=topic, pool="holdout"))
                questions.append(question(f"{key}_calc", key, topic=topic, pool="holdout", type="calc"))
                questions.append(question(f"{key}_prac", key, topic=topic))
        return knowledge(objectives, questions)

    def test_follows_weights_and_type_mix(self):
        k = self.build()
        k["exam_model"] = {"topic_weights": {"t_a": 0.75, "t_b": 0.25}, "type_mix": {"mc": 0.5, "calc": 0.5}, "duration_min": 100}
        mock = practice.plan_mock(k, {}, NOW, {})
        self.assertEqual(mock.minutes, 100)
        self.assertEqual(mock.wanted, 20)
        self.assertEqual(len(mock.questions), 20)
        self.assertEqual(mock.holdout, 20)
        self.assertEqual({q["pool"] for q in mock.questions}, {"holdout"})
        self.assertEqual(sum(q["topic"] == "t_a" for q in mock.questions), 15)
        self.assertEqual(sum(q["type"] == "calc" for q in mock.questions), 10)
        self.assertEqual(len({q["objective"] for q in mock.questions}), 20)

    def test_students_duration_wins(self):
        k = self.build()
        k["exam_model"] = {"type_mix": {"mc": 1.0}, "duration_min": 100}
        mock = practice.plan_mock(k, {}, NOW, {"duration_min": 20})
        self.assertEqual((mock.minutes, len(mock.questions)), (20, 10))

    def test_never_seen_verified_and_askable_only(self):
        k = knowledge(
            [objective("o"), objective("bad", verified=False), objective("gone", retired=True)],
            [
                question("seen", "o", pool="holdout"),
                question("unverified", "o", pool="holdout", verified=False),
                question("suspended", "o", pool="holdout", suspended=True),
                question("flagged", "o", pool="holdout", flagged="x"),
                question("retired", "o", pool="holdout", retired=True),
                question("bad_objective", "bad", pool="holdout"),
                question("retired_objective", "gone", pool="holdout"),
                question("good", "o", pool="holdout"),
            ],
        )
        cards = practice.replay(k, [attempt("seen", days_ago(1))])
        self.assertEqual([q["id"] for q in practice.plan_mock(k, cards, NOW, {}).questions], ["good"])

    def test_few_holdouts_fill_up_with_unseen_practice_and_shrink(self):
        k = knowledge(
            [objective("o"), objective("p")],
            [question("held", "o", pool="holdout"), question("fresh", "o"), question("used", "p")],
        )
        cards = practice.replay(k, [attempt("used", days_ago(1))])
        mock = practice.plan_mock(k, cards, NOW, {"duration_min": 90})
        self.assertEqual({q["id"] for q in mock.questions}, {"held", "fresh"})
        self.assertEqual(mock.holdout, 1)
        self.assertLess(mock.minutes, 90)
        self.assertGreater(mock.minutes, 0)
        self.assertGreater(mock.wanted, 2)

    def test_nothing_available(self):
        mock = practice.plan_mock(knowledge(), {}, NOW, {"duration_min": 90})
        self.assertEqual((mock.questions, mock.minutes), ([], 0))
        k = knowledge([objective("o")], [question("q", "o", verified=False)])
        self.assertEqual(practice.plan_mock(k, {}, NOW, {}).questions, [])

    def test_used_mock_questions_are_not_unseen_any_more(self):
        k = self.build(per_topic=3)
        k["exam_model"] = {"type_mix": {"mc": 1.0}}
        first = practice.plan_mock(k, {}, NOW, {"duration_min": 10}).questions
        cards = practice.replay(k, [attempt(q["id"], days_ago(1), mode="mock") for q in first])
        second = practice.plan_mock(k, cards, NOW, {"duration_min": 10}).questions
        self.assertFalse({q["id"] for q in first} & {q["id"] for q in second})
        used = first[0]
        k["questions"] = {key: q for key, q in k["questions"].items() if key == used["id"]}
        self.assertEqual([q["id"] for q in practice.plan_session(k, cards, NOW)], [used["id"]])


class ReadinessNumbers(unittest.TestCase):
    def build(self):
        return knowledge(
            [objective("a", importance=3), objective("b", importance=1), objective("c", importance=2)],
            [question("qa", "a"), question("qb", "b"), question("qc", "c")],
        )

    def test_wilson_against_known_values(self):
        low, high = practice.wilson(0.8, 10)
        self.assertAlmostEqual(low, 0.4902, places=3)
        self.assertAlmostEqual(high, 0.9433, places=3)
        low, high = practice.wilson(0.5, 100)
        self.assertAlmostEqual(low, 0.4038, places=3)
        self.assertAlmostEqual(high, 0.5962, places=3)
        self.assertEqual(practice.wilson(0.5, 0), (0.0, 1.0))

    def test_recall_hidden_until_thirty_answers_over_three_days(self):
        k = self.build()
        exam = date(2026, 12, 1)
        too_few = [attempt("qa", days_ago(day)) for day in range(3) for _ in range(9)]
        state = practice.readiness(k, too_few, practice.replay(k, too_few), NOW, exam)
        self.assertEqual((state.answers, state.days_active), (27, 3))
        self.assertIsNone(state.recall_at_exam)
        self.assertIsNone(state.recall_now)
        two_days = [attempt("qa", days_ago(day)) for day in range(2) for _ in range(20)]
        self.assertIsNone(practice.readiness(k, two_days, practice.replay(k, two_days), NOW, exam).recall_at_exam)
        enough = [attempt("qa", days_ago(day)) for day in range(3) for _ in range(10)]
        cards = practice.replay(k, enough)
        state = practice.readiness(k, enough, cards, NOW, exam)
        self.assertEqual(state.answers, 30)
        memory = cards["a"].memory
        at_exam = fsrs.recall((exam - memory.last.date()).days, memory.stability)
        self.assertAlmostEqual(state.recall_at_exam, 3.0 * at_exam / 5.0, places=6)
        self.assertLess(state.recall_at_exam, state.recall_now)
        no_date = practice.readiness(k, enough, cards, NOW, None)
        self.assertIsNone(no_date.recall_at_exam)
        self.assertIsNotNone(no_date.recall_now)
        self.assertIsNone(no_date.days_left)

    def test_coverage_is_weighted_by_importance(self):
        k = self.build()
        events = [attempt("qa", days_ago(1))]
        state = practice.readiness(k, events, practice.replay(k, events), NOW, date(2026, 11, 11))
        self.assertAlmostEqual(state.coverage, 3.0 / 5.0)
        self.assertEqual((state.practised, state.objectives, state.new, state.days_left), (1, 3, 2, 10))

    def test_weak_counts_recall_below_seventy(self):
        k = self.build()
        cards = {"a": card_with("a", stability=1.0, days=30), "b": card_with("b", stability=50.0, days=1)}
        state = practice.readiness(k, [], cards, NOW, None)
        self.assertEqual((state.weak, state.new), (1, 1))

    def test_no_mock_means_none(self):
        k = self.build()
        events = [attempt("qa", days_ago(1))]
        self.assertIsNone(practice.readiness(k, events, practice.replay(k, events), NOW, None).last_mock)

    def test_last_mock_counts_first_encounters_of_the_last_sitting(self):
        k = knowledge([objective("o")], [question(f"q{i}", "o") for i in range(12)])
        older = [attempt(f"q{i}", days_ago(9), mode="mock", score=0.0) for i in range(2)]
        seen = [attempt("q2", days_ago(5))]
        sitting = days_ago(1)
        last = [attempt(f"q{i}", sitting + timedelta(minutes=i), mode="mock", score=1.0 if i < 10 else 0.0) for i in range(2, 12)]
        events = older + seen + last
        result = practice.readiness(k, events, practice.replay(k, events), NOW, None).last_mock
        self.assertEqual(result.n, 9)  # q2 was met before
        self.assertAlmostEqual(result.score, 7 / 9)
        low, high = practice.wilson(7 / 9, 9)
        self.assertAlmostEqual(result.low, low)
        self.assertAlmostEqual(result.high, high)
        self.assertEqual(result.at, sitting + timedelta(minutes=11))

    def test_grade_moves_mock_score(self):
        k = knowledge([objective("o")], [question("q", "o", type="short")])
        events = [attempt("q", days_ago(1), mode="mock", score=1.0, id="a", by="self"), {"id": "g", "at": NOW.isoformat(), "type": "grade", "attempt": "a", "score": 0.25}]
        self.assertAlmostEqual(practice.readiness(k, events, practice.replay(k, events), NOW, None).last_mock.score, 0.25)

    def test_calibration_is_of_answers_marked_sure(self):
        k = self.build()
        events = [
            attempt("qa", days_ago(4), score=1.0, confidence="sure"),
            attempt("qa", days_ago(3), score=1.0, confidence="sure"),
            attempt("qa", days_ago(2), score=1.0, confidence="sure"),
            attempt("qb", days_ago(4), score=0.0, confidence="sure"),
            attempt("qb", days_ago(3), score=0.0, confidence="guess"),
            attempt("qc", days_ago(3), score=0.0, confidence=""),
        ]
        self.assertAlmostEqual(practice.readiness(k, events, practice.replay(k, events), NOW, None).sure_right, 0.75)
        self.assertIsNone(practice.readiness(k, [], {}, NOW, None).sure_right)

    def test_empty_subject(self):
        state = practice.readiness(knowledge(), [], {}, NOW, None)
        self.assertEqual((state.coverage, state.objectives, state.answers), (0.0, 0, 0))


class ProgressViews(unittest.TestCase):
    def test_topic_progress(self):
        k = knowledge(
            [objective("a1", topic="t_a", importance=3), objective("a2", topic="t_a"), objective("b1", topic="t_b"), objective("gone", topic="t_b", retired=True)],
            [question("qa1", "a1", topic="t_a"), question("qa2", "a2", topic="t_a"), question("qb1", "b1", topic="t_b")],
        )
        k["topics"]["t_a"]["importance"] = 3
        k["topics"]["t_b"]["importance"] = 1
        events = [attempt("qa1", days_ago(40)), attempt("qa1", days_ago(39), score=0.0), attempt("qa2", days_ago(2))]
        cards = practice.replay(k, events)
        progress = practice.topic_progress(k, cards, events, NOW, date(2026, 11, 20))
        self.assertEqual([p.id for p in progress], ["t_a", "t_b"])
        first, second = progress
        self.assertEqual((first.title, first.importance, first.objectives, first.practised, first.answers, first.correct), ("T_A", 3, 2, 2, 3, 2))
        self.assertEqual(first.weak, 1)
        self.assertLess(first.recall_at_exam, first.recall_now)
        self.assertEqual((second.objectives, second.practised, second.answers), (1, 0, 0))
        self.assertIsNone(second.recall_now)
        self.assertIsNone(second.recall_at_exam)
        self.assertIsNone(practice.topic_progress(k, cards, events, NOW, None)[0].recall_at_exam)

    def test_history_has_every_day_and_applies_grades(self):
        events = [
            attempt("q", NOW, id="a1"),
            attempt("q", NOW, score=0.0),
            attempt("q", days_ago(3)),
            attempt("q", days_ago(30)),
            {"id": "g", "at": NOW.isoformat(), "type": "grade", "attempt": "a1", "score": 0.0},
            {"id": "f", "at": NOW.isoformat(), "type": "flag", "q": "q"},
        ]
        days = practice.history(events, NOW)
        self.assertEqual(len(days), 30)
        self.assertEqual(days[-1].day, NOW.date())
        self.assertEqual(days[0].day, NOW.date() - timedelta(days=29))
        self.assertEqual((days[-1].answers, days[-1].correct), (2, 0))
        self.assertEqual((days[-4].answers, days[-4].correct), (1, 1))
        self.assertEqual(sum(d.answers for d in days), 3)
        self.assertEqual(len(practice.history(events, NOW, days=7)), 7)


class Brief(unittest.TestCase):
    def build(self):
        k = knowledge(
            [objective("L01-o1", importance=3), objective("L01-o2", topic="t_b")],
            [
                question("q_wrong", "L01-o1", options=[{"text": "Aktiva", "correct": True}, {"text": "Passiva", "why_wrong": "x"}]),
                question("q_sure", "L01-o1"),
                question("q_free", "L01-o2", topic="t_b", type="short", answer="model"),
                question("q_graded", "L01-o2", topic="t_b", type="short", answer="model"),
                question("q_flag", "L01-o2", topic="t_b", flagged="Answer contradicts the slide"),
            ],
        )
        events = [
            attempt("q_wrong", days_ago(3), score=0.0, confidence="unsure", answer="Passiva"),
            attempt("q_sure", days_ago(2), score=0.0, confidence="sure", answer="MY SURE MISTAKE"),
            attempt("q_free", days_ago(1), score=0.5, by="self", answer="my own words", id="a_open"),
            attempt("q_graded", days_ago(1), score=0.5, by="self", answer="checked words", id="a_done"),
            {"id": "g", "at": NOW.isoformat(), "type": "grade", "attempt": "a_done", "score": 1.0, "by": "claude"},
            {"id": "f", "at": NOW.isoformat(), "type": "flag", "q": "q_sure", "reason": "Two options are right"},
        ]
        return k, events

    def test_contents(self):
        k, events = self.build()
        text = practice.progress_brief(k, events, practice.replay(k, events), NOW, {"date": "2026-11-11", "duration_min": 90, "aids": "Calculator"})
        self.assertIn("10 days", text)
        self.assertIn("90 minutes", text)
        self.assertIn("Calculator", text)
        self.assertIn("t_a", text)
        self.assertIn("Passiva", text)  # the wrong option they chose
        self.assertIn("Aktiva", text)
        self.assertIn("MY SURE MISTAKE", text)
        sure = text.index("marked sure that were wrong")
        self.assertLess(sure, text.index("MY SURE MISTAKE"))
        self.assertIn("a_open", text)
        self.assertIn("my own words", text)
        self.assertNotIn("a_done", text)
        self.assertNotIn("checked words", text)
        self.assertIn("Answer contradicts the slide", text)
        self.assertIn("Two options are right", text)
        self.assertIn("q_flag", text)
        self.assertIn("L01-o1", text)
        seen = text[text.index("Question ids already seen") :]
        self.assertIn("q_wrong", seen)
        self.assertIn("q_graded", seen)
        self.assertNotIn("left out", text)

    def test_empty_and_without_a_date(self):
        text = practice.progress_brief(knowledge(), [], {}, NOW, {})
        self.assertIn("No exam date", text)
        self.assertIn("0 answers", text)

    def test_stays_short_and_says_what_was_cut(self):
        objectives = [objective(f"L{i:02d}-o{i:04d}", topic=f"t_{i % 40}", importance=3) for i in range(800)]
        long_words = "wrote a very long answer " * 200
        questions = []
        events = []
        for i, o in enumerate(objectives):
            questions += [question(f"q_{i:04d}_{j}", o["id"], topic=o["topic"], type="short", stem="Sehr lange Frage " * 40, answer="Musterlösung " * 40, flagged="Wrong " * 30 if j == 2 else "") for j in range(3)]
            at = days_ago(i % 60)
            events.append(attempt(f"q_{i:04d}_0", at, score=0.0, confidence="sure", answer=long_words, by="self", id=f"a{i:04d}"))
            events.append(attempt(f"q_{i:04d}_1", at, score=0.0, confidence="unsure", answer=long_words, by="self", id=f"b{i:04d}"))
        k = knowledge(objectives, questions)
        text = practice.progress_brief(k, events, practice.replay(k, events), NOW, {"date": "2026-12-01"})
        self.assertLess(len(text) / 4, 8000)
        self.assertGreater(len(text) / 4, 4000)
        self.assertRegex(text, r"\(\d+ more left out")
        self.assertRegex(text, r"\(\d+ more ids left out")
        for section in ("Topics", "marked sure", "Recent wrong", "not checked", "Weakest", "flagged", "out of unseen", "already seen"):
            self.assertIn(section, text)

    def test_newest_wrong_answers_survive_the_cut(self):
        k = knowledge([objective("o")], [question(f"q{i}", "o") for i in range(2000)])
        events = [attempt(f"q{i}", days_ago(2000 - i), score=0.0, confidence="unsure", answer=f"answer {i}!") for i in range(2000)]
        text = practice.progress_brief(k, events, practice.replay(k, events), NOW, {})
        self.assertIn("answer 1999!", text)
        self.assertNotIn("answer 0!", text)


class Interfaces(unittest.TestCase):
    def test_knowledge_may_be_an_object(self):
        class Knowledge:
            def __init__(self, data):
                self.__dict__.update(data)

        k = knowledge([objective("o")], [question("q", "o")])
        obj = Knowledge(k)
        events = [attempt("q", days_ago(1))]
        cards = practice.replay(obj, events)
        self.assertEqual(cards["o"].attempts, 1)
        self.assertEqual(len(practice.plan_session(obj, cards, NOW)), 1)
        self.assertEqual(practice.readiness(obj, events, cards, NOW, None).practised, 1)
        self.assertTrue(math.isfinite(practice._value(k["objectives"]["o"], cards["o"], NOW, 30)))

    def test_objectives_out_of_unseen_probes_are_listed(self):
        k = knowledge([objective("spent"), objective("open"), objective("empty")], [question("q1", "spent"), question("q2", "open")])
        cards = practice.replay(k, [attempt("q1", days_ago(1))])
        self.assertEqual(practice.exhausted(k, cards), ["empty", "spent"])


if __name__ == "__main__":
    unittest.main()
