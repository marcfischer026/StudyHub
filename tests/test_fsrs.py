"""FSRS-5 against the published test values of py-fsrs v5.1.3."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from universe.services import fsrs  # noqa: E402
from universe.services.fsrs import AGAIN, EASY, GOOD, HARD, Memory  # noqa: E402

START = datetime(2026, 1, 1, 9, 0)


def run(ratings_and_gaps):
    memory, when = None, START
    for rating, gap in ratings_and_gaps:
        when += timedelta(days=gap)
        memory = fsrs.first_review(rating, when) if memory is None else fsrs.review(memory, rating, when)
    return memory


class PublishedValues(unittest.TestCase):
    def test_memory_state_vector(self):
        memory = run(zip([AGAIN, GOOD, GOOD, GOOD, GOOD, GOOD, GOOD], [0, 0, 0, 1, 3, 8, 21]))
        self.assertAlmostEqual(memory.stability, 48.717, places=3)
        self.assertEqual(round(memory.stability), 49)
        self.assertAlmostEqual(memory.difficulty, 7.0866, places=4)
        self.assertEqual(memory.reviews, 7)

    def test_interval_vector(self):
        """py-fsrs walks learning steps; this module has none, so the test holds that state."""
        expected = [0, 4, 14, 44, 125, 328, 0, 0, 7, 16, 34, 71, 142]
        memory, state, steps, gap, when, out = None, "learning", 0, 0, START, []
        for rating in [GOOD] * 6 + [AGAIN, AGAIN] + [GOOD] * 5:
            when += timedelta(days=gap)
            memory = fsrs.first_review(rating, when) if memory is None else fsrs.review(memory, rating, when)
            interval = 0
            if state == "learning":
                steps += 1
                if steps == 2:
                    state, interval = "review", round(fsrs.interval_days(memory.stability))
            elif state == "review":
                if rating == AGAIN:
                    state = "relearning"
                else:
                    interval = round(fsrs.interval_days(memory.stability))
            elif rating == GOOD:
                state, interval = "review", round(fsrs.interval_days(memory.stability))
            out.append(interval)
            gap = interval
        self.assertEqual(out, expected)

    def test_recall_is_ninety_percent_after_stability_days(self):
        for stability in (0.5, 5.0, 48.7, 1000.0):
            self.assertAlmostEqual(fsrs.recall(stability, stability), 0.9)
        self.assertAlmostEqual(fsrs.FACTOR, 0.9 ** (1 / fsrs.DECAY) - 1)


class Behaviour(unittest.TestCase):
    def test_first_review_uses_the_rating_weights(self):
        self.assertEqual(fsrs.first_review(GOOD, START).stability, fsrs.WEIGHTS[2])
        self.assertEqual(fsrs.first_review(AGAIN, START).difficulty, fsrs.WEIGHTS[4])
        self.assertAlmostEqual(fsrs.first_review(EASY, START).difficulty, 3.2245, places=4)

    def test_retrievability_falls_with_whole_days(self):
        memory = Memory(10.0, 5.0, START)
        self.assertEqual(fsrs.retrievability(memory, START), 1.0)
        self.assertEqual(fsrs.retrievability(memory, START + timedelta(hours=23)), 1.0)
        self.assertAlmostEqual(fsrs.retrievability(memory, START + timedelta(days=10)), 0.9)
        self.assertEqual(fsrs.retrievability(memory, START - timedelta(days=3)), 1.0)

    def test_ratings_order_the_new_stability(self):
        memory = Memory(10.0, 5.0, START)
        when = START + timedelta(days=10)
        after = [fsrs.review(memory, rating, when).stability for rating in (AGAIN, HARD, GOOD, EASY)]
        self.assertEqual(after, sorted(after))
        self.assertLess(after[0], 10.0)
        self.assertGreater(after[2], 10.0)

    def test_only_a_later_again_is_a_lapse(self):
        memory = Memory(10.0, 5.0, START)
        self.assertEqual(fsrs.review(memory, AGAIN, START + timedelta(hours=2)).lapses, 0)
        self.assertEqual(fsrs.review(memory, AGAIN, START + timedelta(days=4)).lapses, 1)
        self.assertEqual(fsrs.review(memory, GOOD, START + timedelta(days=4)).lapses, 0)

    def test_same_day_agains_stay_usable(self):
        memory = fsrs.first_review(AGAIN, START)
        for _ in range(200):
            memory = fsrs.review(memory, AGAIN, START)
        self.assertGreater(memory.stability, 0)
        self.assertGreaterEqual(memory.difficulty, 1)
        self.assertLessEqual(memory.difficulty, 10)
        self.assertLess(fsrs.retrievability(memory, START + timedelta(days=30)), 0.1)

    def test_interval_rounds_with_a_minimum_of_one(self):
        self.assertEqual(fsrs.interval_days(0.2), 1.0)
        self.assertEqual(fsrs.interval_days(48.717), 49.0)
        self.assertGreater(fsrs.interval_days(10.0, 0.8), fsrs.interval_days(10.0, 0.9))

    def test_a_bad_rating_is_refused(self):
        for rating in (0, 5):
            with self.assertRaises(ValueError):
                fsrs.first_review(rating, START)


if __name__ == "__main__":
    unittest.main()
