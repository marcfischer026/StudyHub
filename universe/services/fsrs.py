"""FSRS-5, the memory model behind practice: how long something stays remembered.

Pure and stateless: a ``Memory`` goes in, a new one comes out, so progress can always be
recomputed from the event log. There are no learning steps and no queue here; what to ask
next is ``practice``'s business. Elapsed time counts whole days, as in py-fsrs, whose
published test values the tests assert.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

WEIGHTS = (0.40255, 1.18385, 3.173, 15.69105, 7.1949, 0.5345, 1.4604, 0.0046, 1.54575, 0.1192, 1.01925, 1.9395, 0.11, 0.29605, 2.2698, 0.2315, 2.9898, 0.51655, 0.6621)
AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4
DECAY = -0.5
FACTOR = 19 / 81  # makes R(S, S) = 0.9
MIN_STABILITY = 0.001  # same-day Agains shrink stability without limit


@dataclass(frozen=True)
class Memory:
    stability: float  # days until recall falls to 90%
    difficulty: float  # 1 to 10
    last: datetime
    reviews: int = 1
    lapses: int = 0


def _check(rating: int) -> None:
    if rating not in (AGAIN, HARD, GOOD, EASY):
        raise ValueError(f"rating must be 1 to 4, not {rating!r}")


def _clamp(difficulty: float) -> float:
    return min(10.0, max(1.0, difficulty))


def _elapsed(last: datetime, when: datetime) -> int:
    return max(0, (when - last).days)


def _first_difficulty(rating: int) -> float:
    return _clamp(WEIGHTS[4] - math.exp(WEIGHTS[5] * (rating - 1)) + 1)


def recall(days: float, stability: float) -> float:
    """R(t, S): the chance of remembering after ``days`` days."""
    return (1 + FACTOR * max(days, 0) / stability) ** DECAY


def first_review(rating: int, when: datetime) -> Memory:
    _check(rating)
    return Memory(WEIGHTS[rating - 1], _first_difficulty(rating), when)


def review(memory: Memory, rating: int, when: datetime) -> Memory:
    _check(rating)
    w, s, d = WEIGHTS, memory.stability, memory.difficulty
    days = _elapsed(memory.last, when)
    lapses = memory.lapses
    if days < 1:
        stability = s * math.exp(w[17] * (rating - 3 + w[18]))
    else:
        r = recall(days, s)
        if rating == AGAIN:
            lapses += 1
            stability = min(w[11] * d ** -w[12] * ((s + 1) ** w[13] - 1) * math.exp((1 - r) * w[14]), s / math.exp(w[17] * w[18]))
        else:
            bonus = w[15] if rating == HARD else w[16] if rating == EASY else 1
            stability = s * (1 + math.exp(w[8]) * (11 - d) * s ** -w[9] * (math.exp((1 - r) * w[10]) - 1) * bonus)
    difficulty = _clamp(w[7] * _first_difficulty(EASY) + (1 - w[7]) * (d + (10 - d) * (-w[6] * (rating - 3)) / 9))
    return Memory(max(MIN_STABILITY, stability), difficulty, when, memory.reviews + 1, lapses)


def retrievability(memory: Memory, when: datetime) -> float:
    return recall(_elapsed(memory.last, when), memory.stability)


def interval_days(stability: float, retention: float = 0.9) -> float:
    return float(max(1, round(stability / FACTOR * (retention ** (1 / DECAY) - 1))))
