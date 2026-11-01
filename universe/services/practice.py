"""What to practise next and how it is going, computed from the event log.

The unit of memory is the learning objective, not the question: questions are interchangeable
probes of it, so repeating one MCQ cannot fake mastery. Everything here is recomputed from
``events.jsonl``, so a correction by Claude simply changes history's meaning, never history.

There are no due dates. Objectives are ranked by how much answering one now is expected to
add to recall on exam day, so missed days create no backlog and new material and reviews
compete on one scale. In a simulation under FSRS-5 this beat due dates at 14 and 30 days and
matched them at 90, while fixed interval caps hurt at every horizon.

Works on plain dicts and event lists; ``knowledge`` may be the dict form of ``knowledge.json``
or an object with the same attributes.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from . import fsrs
from .fsrs import AGAIN, GOOD, HARD, Memory

CONFIDENCE = ("sure", "unsure", "guess")
PASS = 0.5  # a score from here on counts as right, and as remembered in the rating map
GOOD_SCORE = 0.85
WEIGHT = {3: 3.0, 2: 1.5, 1: 0.5}
NO_DATE_DAYS = 30
FIRST_RIGHT = 0.6  # chance a brand-new objective is answered right at first sight
GUARD_SHARE = 0.1  # of a session: longest unseen, against a model too sure of itself
AGAIN_GAP = 3  # questions between a wrong answer and its second chance
STRONG_STABILITY = 7.0  # from here on, ask for more than recognition
WEAK_RECALL = 0.7
BLOOM = {"remember": 0, "understand": 1, "apply": 2, "analyze": 3}
RECALL_MIN_ANSWERS = 30
RECALL_MIN_DAYS = 3
DEFAULT_TOLERANCE = 0.001  # relative; a question without one still forgives rounding
MINUTES = {"mc": 2, "tf": 1, "card": 1, "short": 5, "case": 12, "calc": 8}
MOCK_DEFAULT = 20
MOCK_MIN, MOCK_MAX = 5, 60
MOCK_GAP = timedelta(hours=2)  # events carry no mock id; a longer pause starts another mock
Z95 = 1.959964
MAX_BRIEF_CHARS = 30_000  # about 7,500 tokens

_TRUE = {"true", "t", "wahr", "w", "richtig", "r", "ja", "j", "yes", "y", "1"}
_FALSE = {"false", "f", "falsch", "nein", "n", "no", "0"}
_NUMBER = re.compile(r"(?<![\d.,])[-+−–]?\s?(?:(?:\d{1,3}(?:[   '’]\d{3})+(?!\d)|\d+)(?:[.,]\d+)*|[.,]\d+)")


@dataclass
class Card:
    objective: str
    memory: Memory | None = None
    attempts: int = 0
    correct: int = 0
    lapses: int = 0
    last_seen: datetime | None = None
    seen: dict[str, datetime] = field(default_factory=dict)
    last_day: list[int] = field(default_factory=list)  # ratings on the day it was last answered


@dataclass
class Mock:
    questions: list[dict]
    minutes: int
    wanted: int = 0  # how many the exam's length calls for
    holdout: int = 0  # how many are held-out questions; the rest are unseen practice questions


@dataclass
class MockResult:
    at: datetime
    score: float
    n: int
    low: float
    high: float


@dataclass
class Readiness:
    coverage: float
    practised: int
    objectives: int
    recall_at_exam: float | None
    recall_now: float | None
    last_mock: MockResult | None
    sure_right: float | None
    answers: int
    days_active: int
    weak: int
    new: int
    days_left: int | None


@dataclass
class TopicProgress:
    id: str
    title: str
    importance: int
    objectives: int
    practised: int
    recall_now: float | None
    recall_at_exam: float | None
    weak: int
    answers: int
    correct: int


@dataclass
class DayStats:
    day: date
    answers: int
    correct: int


# --- reading plain data ---------------------------------------------------------------------


def _part(knowledge, name: str):
    value = knowledge.get(name) if isinstance(knowledge, dict) else getattr(knowledge, name, None)
    return value or {}


def _when(value) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment


def _day(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date) or not value:
        return value or None
    moment = _when(value)
    return moment.date() if moment else None


def _float(value, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _importance(item: dict) -> int:
    value = item.get("importance")
    return value if value in WEIGHT else 2


def _canon(objective: str, aliases: dict) -> str:
    hops = 0
    while objective in aliases and hops < 20:  # a cycle in a reply must not hang the app
        objective, hops = aliases[objective], hops + 1
    return objective


def _active_objectives(knowledge) -> dict[str, dict]:
    """Objectives still in play: not retired, not under a retired topic, not merged away."""
    topics, aliases = _part(knowledge, "topics"), _part(knowledge, "aliases")
    return {
        key: objective
        for key, objective in _part(knowledge, "objectives").items()
        if not objective.get("retired") and key not in aliases and not topics.get(objective.get("topic"), {}).get("retired")
    }


def _askable(question: dict) -> bool:
    return not (question.get("retired") or question.get("suspended") or question.get("flagged"))


def _questions_by_objective(knowledge) -> dict[str, list[dict]]:
    aliases, grouped = _part(knowledge, "aliases"), {}
    for question in _part(knowledge, "questions").values():
        grouped.setdefault(_canon(question.get("objective", ""), aliases), []).append(question)
    return grouped


def _in_practice(question: dict, card: Card | None) -> bool:
    """Holdout questions stay away until a mock has used them; only then are they ordinary."""
    return _askable(question) and (question.get("pool") != "holdout" or bool(card and question.get("id") in card.seen))


def _pick_probe(questions: list[dict], card: Card | None, skip=()) -> dict | None:
    """Least recently seen; among equals the higher Bloom level once the memory is strong."""
    usable = [q for q in questions if q.get("id") not in skip and _in_practice(q, card)]
    if not usable:
        return None
    seen = card.seen if card else {}
    strong = bool(card and card.memory and card.memory.stability >= STRONG_STABILITY)

    def key(question: dict):
        return seen.get(question["id"], datetime.min), -BLOOM.get(question.get("bloom"), 0) if strong else 0, question["id"]

    return min(usable, key=key)


# --- grading --------------------------------------------------------------------------------


def _clean(text) -> str:
    return " ".join(str(text if text is not None else "").split()).casefold()


def _truth(text) -> bool | None:
    word = _clean(text).strip(".!")
    return True if word in _TRUE else False if word in _FALSE else None


def _numbers(text) -> list[float]:
    """What the last number in the text can mean. ``1.200`` is 1200 in Germany and 1.2 elsewhere,
    so both come back and the caller accepts either; ``1.200,50`` and ``1,200.50`` are not in doubt."""
    tokens = _NUMBER.findall(str(text if text is not None else ""))
    if not tokens:
        return []
    token = re.sub(r"[\s'’]", "", tokens[-1])
    sign = -1 if token[0] in "-−–" else 1
    digits = token.lstrip("-+−–")
    marks = re.findall(r"[.,]", digits)
    if len(set(marks)) == 2:
        decimal = marks[-1]
        whole, _, rest = digits.rpartition(decimal)
        values = [f"{re.sub(r'[.,]', '', whole)}.{rest}"]
    elif len(marks) > 1:
        values = [re.sub(r"[.,]", "", digits)]
    elif marks:
        whole, _, rest = digits.partition(marks[0])
        values = [f"{whole or 0}.{rest}"]
        if len(rest) == 3 and whole and whole != "0" and len(whole) <= 3:
            values.append(whole + rest)
    else:
        values = [digits]
    return [sign * float(value) for value in values]


def _plain_numbers(text) -> list[float]:
    """A model answer counts as a number only if it is one, perhaps with a unit, not a sentence."""
    rest = _NUMBER.sub("", str(text if text is not None else ""))
    return _numbers(text) if len("".join(rest.split())) <= 4 else []


def grade_answer(question: dict, answer: str) -> float | None:
    """0 to 1 where the machine can tell; None means the student grades it (short, case, card)."""
    kind = question.get("type")
    if kind == "mc":
        options = question.get("options") or []
        if not any(option.get("correct") for option in options):
            return None
        given = _clean(answer)
        chosen = next((o for o in options if _clean(o.get("text")) == given), None)
        if chosen is None:  # Claude's own attempt records may say just "B" or "b)"
            letter = re.fullmatch(r"\(?([a-z])[).:]?", given)
            index = ord(letter[1]) - ord("a") if letter else len(options)
            chosen = options[index] if index < len(options) else {}
        return 1.0 if chosen.get("correct") else 0.0
    if kind == "tf":
        expected = _truth(question.get("answer"))
        if expected is None:
            return None
        return 1.0 if _truth(answer) is expected else 0.0
    if kind == "calc":
        numeric = question.get("numeric") if isinstance(question.get("numeric"), dict) else {}
        value = _float(numeric.get("value"))
        expected = [value] if value is not None else _plain_numbers(numeric.get("value") or question.get("answer"))
        if not expected:
            return None
        tolerance = _float(numeric.get("tolerance"))
        for want in expected:
            allowed = abs(tolerance) if tolerance is not None else abs(want) * DEFAULT_TOLERANCE
            if any(abs(given - want) <= allowed + 1e-9 for given in _numbers(answer)):
                return 1.0
        return 0.0
    return None


def rating_for(score: float, confidence: str, hints: int, question: dict) -> int:
    """Confidence is asked before the reveal, so a lucky guess does not count as knowing."""
    if score < PASS:
        return AGAIN
    if score <= GOOD_SCORE or confidence == "unsure":
        rating = HARD
    elif confidence == "guess":
        rating = AGAIN if question.get("type") in ("mc", "tf") else HARD
    else:
        rating = GOOD
    return min(rating, HARD) if hints else rating


# --- replay ---------------------------------------------------------------------------------


@dataclass
class _Answer:
    id: str
    question_id: str
    question: dict
    objective: str
    topic: str
    at: datetime
    answer: str
    score: float
    correct: bool
    confidence: str
    mode: str
    by: str
    rating: int
    graded: bool
    stale: bool


def _answers(knowledge, events) -> list[_Answer]:
    """Attempts in time order with grade events applied. ``stale``: the question's answer has
    changed since, so this says nothing about the objective any more."""
    questions, objectives, aliases = _part(knowledge, "questions"), _part(knowledge, "objectives"), _part(knowledge, "aliases")
    dated = [(when, str(event.get("id", "")), event) for event in events if (when := _when(event.get("at")))]
    dated.sort(key=lambda item: item[:2])
    grades = {}
    for _, _, event in dated:
        if event.get("type") == "grade" and (score := _float(event.get("score"))) is not None:
            grades[event.get("attempt")] = min(1.0, max(0.0, score))
    answers = []
    for when, event_id, event in dated:
        if event.get("type") != "attempt":
            continue
        key = event.get("q", "")
        question = questions.get(key) or {}
        objective = _canon(question.get("objective") or event.get("o") or "", aliases)
        topic = objectives.get(objective, {}).get("topic") or question.get("topic") or event.get("topic") or ""
        graded = event_id in grades
        score = grades[event_id] if graded else _float(event.get("score"))
        if score is None:
            score = 1.0 if event.get("correct") else 0.0
        correct = score >= PASS if graded or "correct" not in event else bool(event["correct"])
        confidence = event.get("confidence") or ""
        rating = event.get("rating")
        if graded or rating not in (1, 2, 3, 4):
            rating = rating_for(score, confidence, int(_float(event.get("hints"), 0)), question)
        stale = bool(question) and event.get("rev") is not None and event["rev"] != question.get("rev", 1)
        answers.append(
            _Answer(event_id, key, question, objective, topic, when, str(event.get("answer") or ""), score, correct, confidence,
                    event.get("mode") or "", event.get("by") or "", rating, graded, stale)
        )
    return answers


def replay(knowledge, events, exam_date: date | None = None) -> dict[str, Card]:
    """One card per objective, rebuilt from scratch. Deterministic: only the events decide, not
    their order in the file. Attempts older than a changed answer still mark the question as
    seen but say nothing about memory. ``exam_date`` is not needed under the uncapped model."""
    cards = {key: Card(key) for key in _active_objectives(knowledge)}
    for answer in _answers(knowledge, events):
        if not answer.objective:
            continue
        card = cards.setdefault(answer.objective, Card(answer.objective))
        card.seen[answer.question_id] = answer.at
        if answer.stale:
            continue
        if card.memory is None:
            card.memory = fsrs.first_review(answer.rating, answer.at)
        else:
            card.memory = fsrs.review(card.memory, answer.rating, answer.at)
        same_day = card.last_seen is not None and card.last_seen.date() == answer.at.date()
        card.last_day = [*card.last_day, answer.rating] if same_day else [answer.rating]
        card.attempts += 1
        card.correct += answer.correct
        card.lapses = card.memory.lapses
        card.last_seen = answer.at
    return cards


# --- what to practise -----------------------------------------------------------------------


def _horizon(now: datetime, exam_date) -> int:
    day = _day(exam_date)
    return max(1, (day - now.date()).days) if day else NO_DATE_DAYS


def _value(objective: dict, card: Card | None, now: datetime, days_to_exam: int) -> float:
    """Expected gain in exam-day recall from answering this now."""
    weight = WEIGHT[_importance(objective)]
    w = fsrs.WEIGHTS
    if card is None or card.memory is None:
        return weight * (FIRST_RIGHT * fsrs.recall(days_to_exam, w[2]) + (1 - FIRST_RIGHT) * fsrs.recall(days_to_exam, w[0]))
    memory = card.memory
    since = max(0, (now - memory.last).days)
    r = fsrs.recall(since, memory.stability)
    good = fsrs.review(memory, GOOD, now).stability
    lapse = fsrs.review(memory, AGAIN, now).stability
    return weight * (
        r * fsrs.recall(days_to_exam, good) + (1 - r) * fsrs.recall(days_to_exam, lapse) - fsrs.recall(since + days_to_exam, memory.stability)
    )


def _answered_on(card: Card | None, day: date) -> bool:
    return bool(card and card.last_seen and card.last_seen.date() == day)


def _shortfall(objective: dict, card: Card | None, now: datetime) -> float:
    recall = fsrs.retrievability(card.memory, now) if card and card.memory else 0.0
    return WEIGHT[_importance(objective)] * (1 - recall)


def weakest(knowledge, cards: dict[str, Card], now: datetime, limit: int | None = None) -> list[dict]:
    """Practised objectives, most importance-weighted recall missing first."""
    practised = [(key, o) for key, o in _active_objectives(knowledge).items() if cards.get(key) and cards[key].memory]
    practised.sort(key=lambda item: (-_shortfall(item[1], cards[item[0]], now), item[0]))
    return [o for _, o in practised[:limit]]


def exhausted(knowledge, cards: dict[str, Card]) -> list[str]:
    """Objectives with no unseen probe left: the next Claude round should write new ones."""
    grouped, out = _questions_by_objective(knowledge), []
    for key in _active_objectives(knowledge):
        card = cards.get(key)
        seen = card.seen if card else {}
        if not any(_in_practice(q, card) and q.get("id") not in seen for q in grouped.get(key, [])):
            out.append(key)
    return sorted(out)


def _session_item(question: dict, objective: dict, **marks) -> dict:
    return {**question, "topic": objective.get("topic") or question.get("topic") or "", **marks}


def _confusable(a: dict, b: dict, knowledge) -> bool:
    aliases, objectives = _part(knowledge, "aliases"), _part(knowledge, "objectives")
    first, second = _canon(a.get("objective", ""), aliases), _canon(b.get("objective", ""), aliases)
    return (
        second in {_canon(x, aliases) for x in objectives.get(first, {}).get("confusable_with") or []}
        or first in {_canon(x, aliases) for x in objectives.get(second, {}).get("confusable_with") or []}
    )


def _interleave(items: list[dict], knowledge) -> list[dict]:
    """Confusable pairs side by side, because the benefit of mixing depends on similarity; otherwise
    no two of a topic in a row when avoidable, keeping the rank order where the topics allow it."""
    blocks, used = [], set()
    for i, item in enumerate(items):
        if i in used:
            continue
        partner = next((j for j in range(i + 1, len(items)) if j not in used and _confusable(item, items[j], knowledge)), None)
        if partner is None:
            blocks.append([item])
        else:
            used.add(partner)
            blocks.append([item, items[partner]])
    out: list[dict] = []
    while blocks:
        last = out[-1].get("topic") if out else None
        counts = Counter(block[0].get("topic") for block in blocks)
        topic, count = counts.most_common(1)[0]
        must = topic if count * 2 > len(blocks) and topic != last else None
        chosen = next((b for b in blocks if b[0].get("topic") == must), None) if must is not None else None
        chosen = chosen or next((b for b in blocks if b[0].get("topic") != last), blocks[0])
        blocks.remove(chosen)
        out.extend(chosen)
    return out


def plan_session(knowledge, cards: dict[str, Card], now: datetime, exam_date=None, mode: str = "today", limit: int = 20, topic: str | None = None, lecture: str | None = None) -> list[dict]:
    """Copies of the questions to ask, in order. One that returns after a rating of 1 today
    carries ``"again": True``. An empty list: nothing left to ask today."""
    if limit <= 0:
        return []
    if mode == "mock":
        return plan_mock(knowledge, cards, now, {"date": exam_date}).questions[:limit]
    grouped = _questions_by_objective(knowledge)
    topics = _part(knowledge, "topics")
    days_to_exam = _horizon(now, exam_date)
    today = now.date()
    open_items, owed = [], []
    for key, objective in _active_objectives(knowledge).items():
        if topic and objective.get("topic") != topic:
            continue
        if lecture and lecture not in (objective.get("lectures") or topics.get(objective.get("topic"), {}).get("lectures") or []):
            continue
        card = cards.get(key)
        if _answered_on(card, today):
            if card.last_day == [AGAIN]:
                skip = {q for q, when in card.seen.items() if when.date() == today}
                probe = _pick_probe(grouped.get(key, []), card, skip)
                if probe:
                    owed.append(_session_item(probe, objective, again=True))
            continue
        if mode == "weak" and not (card and card.memory):
            continue
        probe = _pick_probe(grouped.get(key, []), card)
        if probe:
            score = _shortfall(objective, card, now) if mode == "weak" else _value(objective, card, now, days_to_exam)
            open_items.append((-score, key, probe, objective, card))
    open_items.sort(key=lambda item: item[:2])
    owed = sorted(owed, key=lambda q: q["id"])[:limit]
    room = limit - len(owed)
    guards = 0 if mode == "weak" else round(room * GUARD_SHARE)
    top, rest = open_items[: room - guards], open_items[room - guards :]
    guarded = sorted(
        (item for item in rest if item[4] and item[4].last_seen and _importance(item[3]) >= 2),
        key=lambda item: (item[4].last_seen, item[1]),
    )[:guards]
    chosen = top + guarded
    if len(chosen) < room:  # too few long-unseen objectives: the slots go back to the ranking
        taken = {item[1] for item in chosen}
        chosen += [item for item in rest if item[1] not in taken][: room - len(chosen)]
    return _interleave([_session_item(item[2], item[3]) for item in chosen], knowledge) + owed


def requeue_again(knowledge, cards: dict[str, Card], session: list[dict], position: int, question: dict, rating: int, gap: int = AGAIN_GAP) -> dict | None:
    """After an answer at ``position``: a rating of 1 brings the objective back once, later in
    ``session`` (changed in place) and through another probe. Returns what was inserted, or None:
    other rating, already its second chance, or no other probe, since seeing the same question
    again right after its explanation would only fake memory."""
    if rating != AGAIN or question.get("again"):
        return None
    aliases = _part(knowledge, "aliases")
    key = _canon(question.get("objective", ""), aliases)
    later = session[position + 1 :]
    if any(q.get("again") and _canon(q.get("objective", ""), aliases) == key for q in later):
        return None
    objective = _active_objectives(knowledge).get(key)
    if objective is None:
        return None
    skip = {q.get("id") for q in session} | {question.get("id")}
    probe = _pick_probe(_questions_by_objective(knowledge).get(key, []), cards.get(key), skip)
    if probe is None:
        return None
    item = _session_item(probe, objective, again=True)
    at = min(position + 1 + gap, len(session))
    for slot in range(at, len(session) + 1):  # a later slot if this one would repeat a topic
        before = session[slot - 1].get("topic") if slot else None
        after = session[slot].get("topic") if slot < len(session) else None
        if item["topic"] not in (before, after):
            at = slot
            break
    session.insert(at, item)
    return item


def plan_mock(knowledge, cards: dict[str, Card], now: datetime, exam: dict) -> Mock:
    """An honest test: only verified questions the student has never seen, held-out ones first,
    by the exam model's topic weights and type mix. If too few exist, unseen practice questions
    fill up and the mock is shorter: never a question already seen. Answered, a held-out question
    has been seen, and ``_in_practice`` lets it into ordinary sessions."""
    model = _part(knowledge, "exam_model")
    exam = exam or {}
    objectives = _active_objectives(knowledge)
    aliases = _part(knowledge, "aliases")
    seen = {key for card in cards.values() for key in card.seen}
    fresh = []
    for question in _part(knowledge, "questions").values():
        objective = objectives.get(_canon(question.get("objective", ""), aliases))
        if objective and question.get("verified") and objective.get("verified", True) and _askable(question) and question.get("id") not in seen:
            fresh.append(_session_item(question, objective))
    topic_shares = _shares(model.get("topic_weights"))
    if not topic_shares:
        topics = _part(knowledge, "topics")
        topic_shares = _shares({key: WEIGHT[_importance(topic)] for key, topic in topics.items() if not topic.get("retired")})
    type_shares = _shares(model.get("type_mix")) or _shares({kind: 1 for kind in MINUTES if kind != "card"})
    duration = _float(exam.get("duration_min")) or _float(model.get("duration_min"))
    wanted = MOCK_DEFAULT
    if duration:
        each = sum(share * MINUTES.get(kind, 3) for kind, share in type_shares.items()) or 3
        wanted = min(MOCK_MAX, max(MOCK_MIN, round(duration / each)))
    picked: list[dict] = []
    held = [q for q in fresh if q.get("pool") == "holdout"]
    _fill_mock(picked, held, wanted, topic_shares, type_shares, aliases)
    holdout = len(picked)
    _fill_mock(picked, [q for q in fresh if q.get("pool") != "holdout"], wanted, topic_shares, type_shares, aliases)
    if not picked:
        minutes = 0
    elif duration:
        minutes = int(duration) if len(picked) >= wanted else max(1, math.ceil(duration * len(picked) / wanted))
    else:
        minutes = sum(MINUTES.get(q.get("type"), 3) for q in picked)
    return Mock(_interleave(picked, knowledge), minutes, wanted, holdout)


def _shares(weights) -> dict[str, float]:
    usable = {key: value for key, value in (weights or {}).items() if (_float(value) or 0) > 0}
    total = sum(float(value) for value in usable.values())
    return {key: float(value) / total for key, value in usable.items()}


def _fill_mock(picked: list[dict], candidates: list[dict], wanted: int, topics: dict, types: dict, aliases: dict) -> None:
    """Greedy: always the question that most closes the gap to the target mix, one per objective
    as long as that is possible."""
    candidates = list(candidates)
    topic_counts = Counter(q["topic"] for q in picked)
    type_counts = Counter(q.get("type") for q in picked)
    objectives = {_canon(q.get("objective", ""), aliases) for q in picked}
    while candidates and len(picked) < wanted:
        size = len(picked) + 1

        def key(question: dict):
            need = topics.get(question["topic"], 0) - topic_counts[question["topic"]] / size
            need += types.get(question.get("type"), 0) - type_counts[question.get("type")] / size
            return _canon(question.get("objective", ""), aliases) in objectives, -round(need, 9), question.get("id", "")

        best = min(candidates, key=key)
        candidates.remove(best)
        picked.append(best)
        topic_counts[best["topic"]] += 1
        type_counts[best.get("type")] += 1
        objectives.add(_canon(best.get("objective", ""), aliases))


# --- how it is going ------------------------------------------------------------------------


def wilson(score: float, n: int) -> tuple[float, float]:
    """95% interval for a share measured on n answers: a mock of 10 says little, and shows it."""
    if n <= 0:
        return 0.0, 1.0
    z2 = Z95 * Z95
    middle = score + z2 / (2 * n)
    spread = Z95 * math.sqrt(score * (1 - score) / n + z2 / (4 * n * n))
    return max(0.0, (middle - spread) / (1 + z2 / n)), min(1.0, (middle + spread) / (1 + z2 / n))


def _last_mock(answers: list[_Answer]) -> MockResult | None:
    """Only first encounters count: a question met before says nothing about unseen ones."""
    seen, sittings, previous = set(), [], None
    for answer in answers:
        if answer.mode == "mock":
            if previous is None or answer.at - previous > MOCK_GAP:
                sittings.append([])
            previous = answer.at
            if answer.question_id not in seen:
                sittings[-1].append(answer)
        seen.add(answer.question_id)
    for sitting in reversed(sittings):
        if sitting:
            score = sum(answer.score for answer in sitting) / len(sitting)
            return MockResult(sitting[-1].at, score, len(sitting), *wilson(score, len(sitting)))
    return None


def _weighted_recall(objectives: dict[str, dict], cards: dict[str, Card], moment: datetime) -> float | None:
    """Importance-weighted mean R; what was never answered counts as forgotten."""
    total = sum(WEIGHT[_importance(o)] for o in objectives.values())
    if not total:
        return None
    return sum(
        WEIGHT[_importance(o)] * fsrs.retrievability(cards[key].memory, moment)
        for key, o in objectives.items()
        if cards.get(key) and cards[key].memory
    ) / total


def _exam_moment(now: datetime, exam_date) -> datetime | None:
    day = _day(exam_date)
    return datetime.combine(max(day, now.date()), time(23, 59, 59)) if day else None


def readiness(knowledge, events, cards: dict[str, Card], now: datetime, exam_date) -> Readiness:
    """Three honest numbers, never a grade. The recall estimates stay hidden until there is
    enough practice to mean anything, and without an exam date there is no exam-day recall."""
    objectives = _active_objectives(knowledge)
    answers = [answer for answer in _answers(knowledge, events) if not answer.stale]
    days_active = len({answer.at.date() for answer in answers})
    practised = {key: o for key, o in objectives.items() if cards.get(key) and cards[key].memory}
    total = sum(WEIGHT[_importance(o)] for o in objectives.values())
    enough = len(answers) >= RECALL_MIN_ANSWERS and days_active >= RECALL_MIN_DAYS
    moment = _exam_moment(now, exam_date)
    sure = [answer for answer in answers if answer.confidence == "sure"]
    day = _day(exam_date)
    return Readiness(
        coverage=sum(WEIGHT[_importance(o)] for o in practised.values()) / total if total else 0.0,
        practised=len(practised),
        objectives=len(objectives),
        recall_at_exam=_weighted_recall(objectives, cards, moment) if enough and moment else None,
        recall_now=_weighted_recall(objectives, cards, now) if enough else None,
        last_mock=_last_mock(answers),
        sure_right=sum(answer.correct for answer in sure) / len(sure) if sure else None,
        answers=len(answers),
        days_active=days_active,
        weak=sum(fsrs.retrievability(cards[key].memory, now) < WEAK_RECALL for key in practised),
        new=sum(not (cards.get(key) and cards[key].attempts) for key in objectives),
        days_left=(day - now.date()).days if day else None,
    )


def topic_progress(knowledge, cards: dict[str, Card], events, now: datetime, exam_date) -> list[TopicProgress]:
    """Most important topics first. A recall is None while nothing in the topic was practised."""
    objectives = _active_objectives(knowledge)
    moment = _exam_moment(now, exam_date)
    answers, right = Counter(), Counter()
    for answer in _answers(knowledge, events):
        if not answer.stale:
            answers[answer.topic] += 1
            right[answer.topic] += answer.correct
    out = []
    for key, topic in _part(knowledge, "topics").items():
        if topic.get("retired"):
            continue
        own = {k: o for k, o in objectives.items() if o.get("topic") == key}
        practised = [k for k in own if cards.get(k) and cards[k].memory]
        out.append(
            TopicProgress(
                id=key,
                title=topic.get("title") or key,
                importance=_importance(topic),
                objectives=len(own),
                practised=len(practised),
                recall_now=_weighted_recall(own, cards, now) if practised else None,
                recall_at_exam=_weighted_recall(own, cards, moment) if practised and moment else None,
                weak=sum(fsrs.retrievability(cards[k].memory, now) < WEAK_RECALL for k in practised),
                answers=answers[key],
                correct=right[key],
            )
        )
    out.sort(key=lambda p: (-p.importance, p.title.casefold(), p.id))
    return out


def history(events, now: datetime, days: int = 30) -> list[DayStats]:
    """One entry per day, oldest first, today last; days without practice are there with zeros."""
    today = now.date()
    stats = {today - timedelta(days=back): DayStats(today - timedelta(days=back), 0, 0) for back in range(days - 1, -1, -1)}
    for answer in _answers({}, events):
        if entry := stats.get(answer.at.date()):
            entry.answers += 1
            entry.correct += answer.correct
    return list(stats.values())


# --- the brief for Claude -------------------------------------------------------------------


def _short(text, limit: int = 240) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _percent(value: float | None) -> str:
    return "not enough data yet" if value is None else f"{value:.0%}"


def _right_answer(question: dict) -> str:
    if question.get("type") == "mc":
        return next((o.get("text", "") for o in question.get("options") or [] if o.get("correct")), "")
    numeric = question.get("numeric") if isinstance(question.get("numeric"), dict) else {}
    if numeric.get("value") is not None:
        return f"{numeric['value']} {numeric.get('unit') or ''}".strip()
    return question.get("answer") or ""


def _answer_line(answer: _Answer) -> str:
    question = answer.question
    wrote = "I chose" if question.get("type") in ("mc", "tf") else "I wrote"
    line = f"- {answer.question_id} ({question.get('type') or '?'}, {answer.objective or '?'}, {answer.at:%Y-%m-%d}): {_short(question.get('stem'), 200)}"
    line += f" | {wrote}: \"{_short(answer.answer, 300)}\""
    right = _right_answer(question)
    return f"{line} | right: \"{_short(right, 200)}\"" if right else line


def _section(title: str, lines: list[str], budget: int, intro: str = "") -> tuple[str, int]:
    """Takes lines, most relevant first, as far as the budget goes, and says how many it left
    out. Returns the text and what is left of the budget for the sections after it."""
    if not lines:
        return "", budget
    text = f"## {title}\n" + (f"{intro}\n" if intro else "")
    kept = 0
    for line in lines:
        if len(text) + len(line) + 1 > budget - 60:
            break
        text += line + "\n"
        kept += 1
    if kept < len(lines):
        text += f"({len(lines) - kept} more left out to keep this short.)\n"
    return text + "\n", budget - len(text) - 1


def progress_brief(knowledge, events, cards: dict[str, Card], now: datetime, exam: dict) -> str:
    """Markdown for Claude, under about 8,000 tokens: what the student gets wrong, in their own
    words. Long tails are cut per section, and each section says how much."""
    exam = exam or {}
    exam_date = _day(exam.get("date"))
    objectives = _active_objectives(knowledge)
    questions = _part(knowledge, "questions")
    answers = [answer for answer in _answers(knowledge, events) if not answer.stale]
    state = readiness(knowledge, events, cards, now, exam_date)
    head = ["# My progress", ""]
    if state.days_left is not None:
        head.append(f"Exam on {exam_date.isoformat()}: {state.days_left} days to go.")
    else:
        head.append("No exam date set.")
    details = [f"{exam['duration_min']} minutes" if exam.get("duration_min") else "", f"aids: {exam['aids']}" if exam.get("aids") else "", _short(exam.get("notes"))]
    if any(details):
        head.append("Exam: " + ", ".join(d for d in details if d) + ".")
    head.append(f"{state.answers} answers over {state.days_active} days. {state.practised} of {state.objectives} objectives practised ({state.coverage:.0%} by importance), {state.new} never answered, {state.weak} weak (model recall below {WEAK_RECALL:.0%}).")
    head.append(f"Model estimate of recall on exam day: {_percent(state.recall_at_exam)}. Marked sure and right: {_percent(state.sure_right)}.")
    if state.last_mock:
        mock = state.last_mock
        head.append(f"Last unseen mock ({mock.at:%Y-%m-%d}): {mock.score:.0%} on {mock.n} questions, 95% interval {mock.low:.0%} to {mock.high:.0%}.")
    text = "\n".join(head) + "\n\n"
    room, spare = MAX_BRIEF_CHARS - len(text), 0

    topics = []
    for p in topic_progress(knowledge, cards, events, now, exam_date):
        recall = p.recall_at_exam if p.recall_at_exam is not None else p.recall_now
        topics.append(f"- {p.id} {_short(p.title, 80)}: importance {p.importance}, recall {'never practised' if recall is None else f'{recall:.0%}'}, {p.answers} answers ({p.correct} right), {p.weak} weak of {p.objectives}")

    weak_lines = []
    for objective in weakest(knowledge, cards, now):
        card = cards[objective["id"]]
        weak_lines.append(f"- {objective['id']} (importance {_importance(objective)}, recall {fsrs.retrievability(card.memory, now):.0%}, {card.attempts} answers, {card.lapses} lapses): {_short(objective.get('statement'), 160)}")

    newest = list(reversed(answers))
    sure_wrong = [a for a in newest if not a.correct and a.confidence == "sure"]
    sure_ids = {a.id for a in sure_wrong}
    wrong = [a for a in newest if not a.correct and a.id not in sure_ids]
    unchecked = [a for a in newest if a.by == "self" and not a.graded and a.answer.strip()]

    flags = {}
    for event in events:
        if event.get("type") == "flag" and event.get("q"):
            flags[event["q"]] = event.get("reason") or ""
    for key, question in questions.items():
        if question.get("flagged"):
            flags[key] = question["flagged"] if isinstance(question["flagged"], str) else flags.get(key, "")
    flag_lines = [f"- {key}: {_short(reason, 200) or 'no reason given'} | {_short(questions.get(key, {}).get('stem'), 160)}" for key, reason in sorted(flags.items())]

    seen_ids = sorted({key for card in cards.values() for key in card.seen})

    for title, lines, share, intro in (
        ("Topics", topics, 0.14, ""),
        ("Answers marked sure that were wrong", [_answer_line(a) for a in sure_wrong], 0.15, "These are probably misconceptions, not gaps."),
        ("Recent wrong answers", [_answer_line(a) for a in wrong], 0.22, ""),
        ("My free answers not checked yet", [f"- attempt {a.id}, {a.question_id} (I gave myself {a.score:.0%}): {_short(a.question.get('stem'), 160)} | I wrote: \"{_short(a.answer, 400)}\"" for a in unchecked], 0.18, "I graded these myself. Send a grade record for each, using the attempt id."),
        ("Weakest objectives", weak_lines, 0.13, ""),
        ("Questions I flagged as wrong", flag_lines, 0.06, ""),
        ("Objectives out of unseen questions", [f"- {key}: {_short(objectives[key].get('statement'), 100)}" for key in exhausted(knowledge, cards)], 0.05, "These need new questions."),
    ):
        part, spare = _section(title, lines, int(room * share) + spare, intro)
        text += part
    if seen_ids:
        room = MAX_BRIEF_CHARS - len(text) - 120
        line, kept = "", 0
        for key in seen_ids:
            if len(line) + len(key) + 2 > room:
                break
            line += (", " if line else "") + key
            kept += 1
        text += f"## Question ids already seen\nNever reuse these stems.\n{line}\n"
        if kept < len(seen_ids):
            text += f"({len(seen_ids) - kept} more ids left out to keep this short.)\n"
    return text
