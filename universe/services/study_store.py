"""Everything stored of what Claude worked out and how the student is doing.

``study/knowledge.json`` is what was imported, merged; replies are always deltas, so whatever a
reply does not mention stays. ``study/events.jsonl`` is the learning history, append-only: progress
is replayed from it (``practice.replay``), so a new reply can never cost any of it.

The student's own material stays theirs: ``notes.md`` is never written, a label set by hand is never
overwritten, and what Claude thinks about a professor or a document waits to be accepted.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from ..professors import ProfessorStore
from ..storage import Subject, atomic_write_text, read_json, write_json
from . import relevance
from .claude_reply import CheckedReply, normalise
from .practice import rating_for
from .subject_model import SubjectView
from .transcriber import Transcript

HOLDOUT_MIN_PROBES = 3
EVENT_PREFIX = {"attempt": "a", "grade": "g", "flag": "f"}
LIST_FIELDS = ("focus_areas", "question_types")
# What the app decides about a question; a reply saying otherwise is ignored.
APP_FIELDS = ("pool", "rev", "retired", "suspended", "flagged")
QUESTION_DEFAULTS = {
    "pool": "practice", "options": [], "answer": "", "numeric": None, "steps": [], "hints": [], "rubric": [], "explain": "",
    "difficulty": "", "bloom": "", "style_of": None, "sources": [], "verified": False, "rev": 1, "retired": False, "suspended": False, "flagged": "",
}
TOPIC_DEFAULTS = {"lectures": [], "importance": 2, "why": "", "signals": [], "misconceptions": [], "sources": [], "retired": False}
OBJECTIVE_DEFAULTS = {"truth": "", "signals": [], "confusable_with": [], "sources": [], "lectures": [], "verified": False, "retired": False}

_lock = threading.RLock()  # imports run in a worker while the panels flag questions


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Knowledge:
    topics: dict = field(default_factory=dict)
    objectives: dict = field(default_factory=dict)
    questions: dict = field(default_factory=dict)
    guides: dict = field(default_factory=dict)
    said: dict = field(default_factory=dict)
    conflicts: dict = field(default_factory=dict)
    aliases: dict = field(default_factory=dict)
    note_fixes: dict = field(default_factory=dict)
    exam_model: dict = field(default_factory=dict)
    doc_notes: list = field(default_factory=list)
    prof_suggestions: list = field(default_factory=list)
    tutor_notes: list = field(default_factory=list)
    lectures: dict = field(default_factory=dict)
    updated: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> "Knowledge":
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})


@dataclass
class MergeSummary:
    added: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    answer_changed: int = 0
    duplicates: int = 0
    labels_applied: int = 0
    labels_stale: int = 0
    attempts_added: int = 0
    lectures_done: list[str] = field(default_factory=list)
    uncovered_pages: int = 0
    unverified: int = 0

    def describe(self) -> str:
        """For the import report."""

        def counted(counts: dict[str, int]) -> str:
            return ", ".join(f"{n} {kind}" for kind, n in counts.items())

        lines = []
        if self.added:
            lines.append(f"Added: {counted(self.added)}.")
        if self.updated:
            lines.append(f"Updated: {counted(self.updated)}.")
        if self.answer_changed:
            lines.append(f"{self.answer_changed} questions have a new answer; earlier tries at them no longer count.")
        if self.duplicates:
            lines.append(f"{self.duplicates} questions were already there under another id.")
        if self.labels_applied or self.labels_stale:
            lines.append(f"{self.labels_applied} transcript labels applied, {self.labels_stale} no longer match the transcript.")
        if self.attempts_added:
            lines.append(f"{self.attempts_added} answers from the tutor session added to your progress.")
        if self.lectures_done:
            lines.append(f"Processed: {', '.join(self.lectures_done)}.")
        if self.uncovered_pages:
            lines.append(f"{self.uncovered_pages} slide pages are not accounted for.")
        if self.unverified:
            lines.append(f"{self.unverified} objectives or questions have no verified source; they stay out of mock exams.")
        return "\n".join(lines) or "Nothing new in this reply."


def _bump(counts: dict[str, int], kind: str) -> None:
    counts[kind] = counts.get(kind, 0) + 1


def _clean(record: dict) -> dict:
    """A checked record as stored: without the reply's own bookkeeping."""
    return {k: v for k, v in record.items() if k != "t" and not k.startswith("_")}


def _content(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in ("sources", "verified")}


def _upsert(store: dict, key: str, item: dict, kind: str, summary: MergeSummary, defaults: dict | None = None) -> None:
    old = store.get(key)
    if old is None:
        store[key] = {**copy.deepcopy(defaults or {}), **item}
        _bump(summary.added, kind)
        return
    merged = {**old, **item}
    if "sources" in old and not item.get("sources"):  # nothing new to say about where it stands
        merged["sources"] = old["sources"]
        if "verified" in old:
            merged["verified"] = old["verified"]
    if _content(merged) != _content(old):
        _bump(summary.updated, kind)
    store[key] = merged


def _union(first: list, second: list) -> list:
    return list(dict.fromkeys([*first, *second]))


def _answer_key(question: dict) -> tuple:
    """What decides whether earlier tries at a question still count."""
    kind = question.get("type")
    if kind == "mc":
        return (kind, tuple(normalise(str(o.get("text", ""))) for o in question.get("options", ()) if o.get("correct")))
    numeric = question.get("numeric")
    value = (numeric.get("value"), numeric.get("unit") or "") if kind == "calc" and numeric else None
    return (kind, value, normalise(str(question.get("answer") or "")) if value is None else "")


def _follow(aliases: dict, objective: str) -> str:
    seen = {objective}
    while objective in aliases and aliases[objective] not in seen:
        objective = aliases[objective]
        seen.add(objective)
    return objective


def _page_keys(value: object, found: set[str]) -> None:
    """Every page cited anywhere, however deep: ``d_key.p12``."""
    if isinstance(value, dict):
        if value.get("kind") == "page":
            found.add(f"{value.get('doc')}.p{value.get('page')}")
        for item in value.values():
            _page_keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _page_keys(item, found)


class StudyStore:
    def __init__(self, subject: Subject):
        self.subject = subject
        self.folder = subject.study_dir
        self.path = self.folder / "knowledge.json"
        self.prev_path = self.folder / "knowledge.prev.json"
        self.events_path = self.folder / "events.jsonl"
        self._cached: Knowledge | None = None
        self._stamp: tuple[int, int] | None = None

    def knowledge(self) -> Knowledge:
        """Read-only by convention. Re-read when the file changed."""
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        if self._cached is None or stamp != self._stamp:
            self._cached = Knowledge.from_dict(read_json(self.path))
            self._stamp = stamp
        return self._cached

    def _save(self, knowledge: Knowledge) -> None:
        knowledge.updated = _now()
        write_json(self.path, asdict(knowledge))
        self._cached = None

    def known_ids(self) -> dict[str, set[str]]:
        """What a reply may point at beyond itself. ``accounted_pages``: pages earlier parts
        cited or listed, so the last part of a long reply is not asked about them again."""
        knowledge = self.knowledge()
        pages: set[str] = set()
        _page_keys([knowledge.topics, knowledge.objectives, knowledge.questions, knowledge.guides, knowledge.said, knowledge.conflicts], pages)
        _page_keys([knowledge.note_fixes, knowledge.exam_model, knowledge.prof_suggestions], pages)
        for lecture in knowledge.lectures.values():
            pages.update(lecture.get("accounted", ()))
        return {
            "topics": set(knowledge.topics),
            "objectives": set(knowledge.objectives),
            "questions": set(knowledge.questions),
            "attempts": {e["id"] for e in self.events() if e.get("type") == "attempt"},
            "accounted_pages": pages,
        }

    # Importing

    def merge(self, checked: CheckedReply, raw_text: str, view: SubjectView) -> MergeSummary:
        """Applies a checked reply. The same reply twice changes nothing, so a double paste is harmless."""
        with _lock:
            self._archive(raw_text)
            before = self.knowledge()
            knowledge = copy.deepcopy(before)
            summary = MergeSummary()
            events = self.events()
            records = checked.records
            for record in records.get("topic", ()):
                self._merge_topic(knowledge, _clean(record), summary)
            for record in records.get("objective", ()):
                self._merge_objective(knowledge, _clean(record), summary)
            for record in records.get("merge", ()):
                self._merge_objectives(knowledge, record)
            stems = {(q.get("topic"), normalise(q.get("stem", ""))): q["id"] for q in reversed(list(knowledge.questions.values()))}
            renamed = {r["id"]: self._merge_question(knowledge, _clean(r), stems, summary) for r in records.get("q", ())}
            self._merge_simple(knowledge, records, summary)
            self._merge_coverage(knowledge, checked, summary)
            for record in records.get("retire", ()):
                self._retire(knowledge, record)
            self._assign_holdouts(knowledge, events)
            summary.unverified = sum(not r.get("verified") for kind in ("objective", "q") for r in records.get(kind, ()))
            self._write_if_changed(before, knowledge)
            self._apply_labels(records.get("label", ()), view, summary)
            self._append_reply_events(knowledge, records, renamed, events, summary)
            return summary

    def _archive(self, raw_text: str) -> None:
        folder = self.folder / "imports"
        folder.mkdir(exist_ok=True)
        digest = hashlib.sha1(raw_text.encode()).hexdigest()[:8]
        if not any(folder.glob(f"*-{digest}.jsonl")):
            atomic_write_text(folder / f"{datetime.now():%Y%m%d-%H%M%S}-{digest}.jsonl", raw_text)

    def _write_if_changed(self, before: Knowledge, after: Knowledge) -> None:
        """Without a change the undo step stays that of the last real import."""
        old, new = asdict(before), asdict(after)
        old.pop("updated"), new.pop("updated")
        if old == new:
            return
        if self.path.exists():
            atomic_write_text(self.prev_path, self.path.read_text(encoding="utf-8"))
        else:
            write_json(self.prev_path, asdict(Knowledge()))
        self._save(after)

    @staticmethod
    def _merge_topic(knowledge: Knowledge, topic: dict, summary: MergeSummary) -> None:
        old = knowledge.topics.get(topic["id"], {})
        topic["lectures"] = _union(old.get("lectures", []), topic.get("lectures", []))
        topic.pop("retired", None)
        _upsert(knowledge.topics, topic["id"], topic, "topics", summary, TOPIC_DEFAULTS)

    @staticmethod
    def _merge_objective(knowledge: Knowledge, objective: dict, summary: MergeSummary) -> None:
        old = knowledge.objectives.get(objective["id"], {})
        objective["lectures"] = _union(old.get("lectures", []), objective.get("lectures", []))
        objective.pop("retired", None)
        _upsert(knowledge.objectives, objective["id"], objective, "objectives", summary, OBJECTIVE_DEFAULTS)

    @staticmethod
    def _merge_objectives(knowledge: Knowledge, record: dict) -> None:
        """The old objective's probes now test the one it was merged into; replay folds the attempts."""
        into = _follow(knowledge.aliases, record["into"])
        for old in record["from"]:
            if old == into or old not in knowledge.objectives:
                continue
            knowledge.aliases[old] = into
            knowledge.objectives[old]["retired"] = True
            target = knowledge.objectives.get(into, {})
            for question in knowledge.questions.values():
                if question.get("objective") == old:
                    question["objective"] = into
                    question["topic"] = target.get("topic", question.get("topic", ""))

    @staticmethod
    def _merge_question(knowledge: Knowledge, question: dict, stems: dict, summary: MergeSummary) -> str:
        """Returns the id it is stored under. History stays: ``rev`` moves only when the answer does."""
        for name in APP_FIELDS:
            question.pop(name, None)
        objective = _follow(knowledge.aliases, question["objective"])
        topic = knowledge.objectives.get(objective, {}).get("topic") or question.get("topic", "")
        question.update(objective=objective, topic=topic)
        key = question["id"]
        stem = (topic, normalise(question["stem"]))
        if key not in knowledge.questions and stem in stems:
            key = question["id"] = stems[stem]
            summary.duplicates += 1
        stems.setdefault(stem, key)
        old = knowledge.questions.get(key)
        if old is not None and _answer_key({**old, **question}) != _answer_key(old):
            question["rev"] = old.get("rev", 1) + 1
            summary.answer_changed += 1
        _upsert(knowledge.questions, key, question, "questions", summary, QUESTION_DEFAULTS)
        return key

    @staticmethod
    def _merge_simple(knowledge: Knowledge, records: dict[str, list[dict]], summary: MergeSummary) -> None:
        for record in records.get("guide", ()):
            guide = _clean(record)
            _upsert(knowledge.guides, guide["topic"], guide, "guides", summary)
        for record in records.get("said", ()):
            said = _clean(record)
            _upsert(knowledge.said, said["id"], said, "said", summary)
        for record in records.get("conflict", ()):
            conflict = _clean(record)
            _upsert(knowledge.conflicts, conflict["id"], conflict, "conflicts", summary)
        for record in records.get("note_fix", ()):
            fix = _clean(record)
            lecture = fix.pop("lecture")
            fixes = knowledge.note_fixes.setdefault(lecture, [])
            old = next((f for f in fixes if f.get("line") == fix["line"]), None)
            if old is None:
                fixes.append(fix)
                _bump(summary.added, "note fixes")
            elif old != fix:
                fixes[fixes.index(old)] = fix
                _bump(summary.updated, "note fixes")
        for record in records.get("exam_model", ()):
            model = _clean(record)
            if not model.get("sources") and knowledge.exam_model.get("sources"):
                model["sources"] = knowledge.exam_model["sources"]
            if model != knowledge.exam_model:
                _bump(summary.updated if knowledge.exam_model else summary.added, "exam model")
            knowledge.exam_model = model
        for record in records.get("doc_note", ()):
            note = _clean(record)
            if not any((n.get("doc"), n.get("issue"), n.get("detail")) == (note["doc"], note["issue"], note["detail"]) for n in knowledge.doc_notes):
                knowledge.doc_notes.append({**note, "dismissed": False})
                _bump(summary.added, "document notes")
        for record in records.get("prof", ()):
            value = normalise(json.dumps(record["value"], ensure_ascii=False))
            known = (
                (s.get("prof"), s.get("field"), normalise(json.dumps(s.get("value"), ensure_ascii=False))) for s in knowledge.prof_suggestions
            )
            if (record["prof"], record["field"], value) not in known:
                knowledge.prof_suggestions.append({**_clean(record), "state": "open"})
                _bump(summary.added, "professor suggestions")
        for record in records.get("tutor_note", ()):
            note = _clean(record)
            same = ("topic", "diagnosis", "next")
            if not any(all(n.get(k) == note.get(k) for k in same) for n in knowledge.tutor_notes):
                knowledge.tutor_notes.append({"topic": "", "diagnosis": "", "next": "", **note, "at": note.get("at") or _now()})
                _bump(summary.added, "tutor notes")

    @staticmethod
    def _merge_coverage(knowledge: Knowledge, checked: CheckedReply, summary: MergeSummary) -> None:
        """A lecture is done once Claude accounted for it. What later parts cite shrinks what is still open."""
        stated: dict[str, list[dict]] = {}
        for record in checked.records.get("coverage", ()):
            uid = record["lecture"]
            old = knowledge.lectures.get(uid, {})
            entry = {
                "processed": True,
                "hash": record["hash"],
                "export": checked.manifest.export_id,
                "uncovered": old.get("uncovered", []),
                "accounted": sorted({*old.get("accounted", []), *record["accounted"]}),
                "imported": old.get("imported", ""),
            }
            if entry != old:
                entry["imported"] = _now()
            knowledge.lectures[uid] = entry
            stated.setdefault(uid, []).extend(record["uncovered"])
            summary.lectures_done.append(record["ref"])
        for ref, info in checked.manifest.lectures.items():
            entry = knowledge.lectures.get(info["uid"])
            if entry is None:
                continue
            reasons = {u["page"]: u for u in [*entry.get("uncovered", []), *stated.get(info["uid"], [])] if u.get("why")}
            entry["uncovered"] = [*reasons.values(), *checked.uncovered.get(ref, [])]
        summary.uncovered_pages = sum(len(items) for items in checked.uncovered.values())

    @staticmethod
    def _retire(knowledge: Knowledge, record: dict) -> None:
        for store in (knowledge.questions, knowledge.objectives, knowledge.topics):
            if record["id"] in store:
                store[record["id"]]["retired"] = True
                if record["why"]:
                    store[record["id"]]["retired_why"] = record["why"]

    @staticmethod
    def _assign_holdouts(knowledge: Knowledge, events: list[dict]) -> None:
        """One probe of each objective that matters stays unseen for the mock exam: the app chooses,
        by a hash, so Claude cannot lean on it. A question already answered has been seen."""
        attempted = {e.get("q") for e in events if e.get("type") == "attempt"}
        probes: dict[str, list[dict]] = {}
        for question in knowledge.questions.values():
            if not question.get("retired"):
                probes.setdefault(question.get("objective", ""), []).append(question)
        for objective, questions in probes.items():
            info = knowledge.objectives.get(objective, {})
            if info.get("retired") or info.get("importance", 0) < 2 or len(questions) < HOLDOUT_MIN_PROBES:
                continue
            if any(q.get("pool") == "holdout" for q in questions):
                continue
            # Mocks take only verified questions, and flags and suspensions keep a question out of them.
            usable = [q for q in questions if q["id"] not in attempted and q.get("verified") and not q.get("suspended") and not q.get("flagged")]
            if usable:
                min(usable, key=lambda q: hashlib.sha1(q["id"].encode()).hexdigest())["pool"] = "holdout"

    @staticmethod
    def _apply_labels(labels: list[dict], view: SubjectView, summary: MergeSummary) -> None:
        """Every current segment whose midpoint falls inside a label's time range gets it."""
        grouped: dict[str, list[dict]] = {}
        for label in labels:
            grouped.setdefault(label["rec"], []).append(label)
        for uid, group in grouped.items():
            found = view.recording(uid)
            path = found[0].lecture.dir("transcripts") / f"{found[1].stem}.json" if found else None
            if path is None or not path.exists():
                summary.labels_stale += len(group)
                continue
            transcript = Transcript.load(path)
            visibility = {s.index: s.visibility for s in found[1].segments}
            changed = False
            for label in group:
                inside = [(i, s) for i, s in enumerate(transcript.segments) if label["start"] <= (s.start + s.end) / 2 <= label["end"]]
                if not inside:
                    summary.labels_stale += 1
                    continue
                # A range from what Claude read must not wave through side talk between its ends, which
                # it never saw and which would then leave this Mac. A range starting in side talk was read.
                from_side_talk = visibility.get(inside[0][0]) == "side_talk"
                applied = False
                for index, seg in inside:
                    if visibility.get(index) == "side_talk" and not from_side_talk and label.get("category") != "off_topic":
                        continue
                    exam = label.get("exam")
                    category = label.get("category")
                    same = seg.source == "claude" and seg.category == (category or seg.category) and seg.note == (label["reason"] or seg.note)
                    if same and (exam is not True or seg.exam):
                        applied = True  # already has it; applying again would take back a rule's exam cue
                        continue
                    if relevance.apply_label(seg, category, exam if isinstance(exam, bool) else None, label["reason"], "claude"):
                        applied = changed = True
                summary.labels_applied += applied
            if changed:
                transcript.save(path.parent)

    def _append_reply_events(self, knowledge: Knowledge, records: dict, renamed: dict, events: list[dict], summary: MergeSummary) -> None:
        seen = {e.get("id") for e in events}
        for record in records.get("attempt", ()):
            if record["id"] in seen:
                continue
            q_id = renamed.get(record["q"], record["q"])
            question = knowledge.questions.get(q_id, {"id": q_id})
            fields = self._attempt_fields(question, record["answer"], record["score"], "", 0, 0, "tutor", "claude")
            fields["correct"] = record["correct"]
            self.append_event("attempt", id=record["id"], at=record["at"] or _now(), **fields)
            seen.add(record["id"])
            summary.attempts_added += 1
        for record in records.get("grade", ()):
            if record["id"] not in seen:
                self.append_event("grade", id=record["id"], attempt=record["attempt"], score=record["score"], feedback=record["feedback"], by="claude")
                seen.add(record["id"])

    def undo_last_import(self) -> bool:
        """One step back in what was imported. What the student did since stays: their suspensions, flags
        and decisions on suggestions. Transcript labels and answers already in the history are not taken back."""
        with _lock:
            if not self.prev_path.exists():
                return False
            current = self.knowledge()
            restored = Knowledge.from_dict(read_json(self.prev_path))
            for key, question in restored.questions.items():
                now = current.questions.get(key)
                if now:
                    question.update(suspended=now.get("suspended", False), flagged=now.get("flagged", ""))
            for items, now_items, keys, state in (
                (restored.doc_notes, current.doc_notes, ("doc", "issue", "detail"), "dismissed"),
                (restored.prof_suggestions, current.prof_suggestions, ("prof", "field", "value"), "state"),
            ):
                for item in items:
                    later = next((c for c in now_items if all(c.get(k) == item.get(k) for k in keys)), None)
                    if later:
                        item[state] = later.get(state, item.get(state))
            self._save(restored)
            self.prev_path.unlink(missing_ok=True)
            return True

    # The history

    def events(self) -> list[dict]:
        """A crash or a full disk can tear a line; what it says is lost, the rest is not."""
        try:
            text = self.events_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        found = []
        for line in text.split("\n"):  # not splitlines: a pasted answer may hold U+2028 or a form feed
            try:
                event = json.loads(line) if line.strip() else None
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                found.append(event)
        return found

    def append_event(self, type: str, **fields) -> dict:
        """One write in append mode, so the history is never rewritten and a crash costs one line at most."""
        prefix = EVENT_PREFIX.get(type, "e")
        event = {"v": 1, "id": f"{prefix}_{uuid.uuid4().hex[:12]}", "at": _now(), "type": type, **fields}
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with _lock:
            fd = os.open(self.events_path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                size = os.fstat(fd).st_size
                if size and os.pread(fd, 1, size - 1) != b"\n":  # after a torn line: do not glue onto it
                    line = "\n" + line
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        return event

    @staticmethod
    def _attempt_fields(question: dict, answer: str, score: float, confidence: str, hints: int, ms: int, mode: str, by: str) -> dict:
        return {
            "q": question.get("id", ""),
            "o": question.get("objective", ""),
            "topic": question.get("topic", ""),
            "rev": question.get("rev", 1),
            "mode": mode,
            "answer": answer,
            "correct": score >= 0.5,
            "score": score,
            "confidence": confidence,
            "hints": hints,
            "ms": ms,
            "by": by,
            "rating": rating_for(score, confidence, hints, question),
        }

    def record_attempt(self, question: dict, answer: str, score: float, confidence: str, hints: int, ms: int, mode: str, by: str) -> dict:
        return self.append_event("attempt", **self._attempt_fields(question, answer, score, confidence, hints, ms, mode, by))

    # What the student decides

    def _change(self, apply) -> None:
        with _lock:
            knowledge = copy.deepcopy(self.knowledge())
            apply(knowledge)
            self._save(knowledge)

    def flag_question(self, q_id: str, reason: str) -> None:
        """The flag goes to Claude with the next export and stays through later imports."""
        self.append_event("flag", q=q_id, reason=reason)

        def apply(knowledge: Knowledge) -> None:
            if q_id in knowledge.questions:
                knowledge.questions[q_id]["flagged"] = reason or "flagged"

        self._change(apply)

    def set_suspended(self, q_id: str, suspended: bool) -> None:
        def apply(knowledge: Knowledge) -> None:
            if q_id in knowledge.questions:
                knowledge.questions[q_id]["suspended"] = suspended

        self._change(apply)

    def resolve_suggestion(self, index: int, accept: bool, professors: ProfessorStore) -> None:
        """Accepting adds to the profile; what the student wrote there is never replaced."""
        with _lock:
            knowledge = copy.deepcopy(self.knowledge())
            suggestion = knowledge.prof_suggestions[index]
            if suggestion.get("state") != "open":
                return
            if accept:
                profile = professors.get(suggestion["prof"])
                if profile is None:
                    return
                name, value = suggestion["field"], suggestion["value"]
                if name in LIST_FIELDS:
                    items = getattr(profile, name)
                    items.extend(v for v in map(str, value if isinstance(value, list) else [value]) if v not in items)
                else:
                    text = getattr(profile, name)
                    addition = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
                    if addition not in text:
                        setattr(profile, name, f"{text}\n{addition}" if text else addition)
                professors.save(profile)
            suggestion["state"] = "accepted" if accept else "dismissed"
            self._save(knowledge)

    def dismiss_doc_note(self, index: int) -> None:
        def apply(knowledge: Knowledge) -> None:
            knowledge.doc_notes[index]["dismissed"] = True

        self._change(apply)

    def apply_doc_note(self, index: int) -> None:
        """Only a deck from another course has something to apply: it stays out of exports. The rest is
        for the student to read, so accepting those just puts them away."""
        note = self.knowledge().doc_notes[index]
        if note.get("issue") == "wrong_subject":
            self.subject.set_document_excluded(note["doc"], True)
        self.dismiss_doc_note(index)
