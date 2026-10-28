"""What in a lecture recording matters: content vs. side talk, and exam hints.

Three sources label transcript segments, from cheapest to smartest:

1. **rules** — exam cues ("das kommt in der Klausur", "important") found by regex,
   applied right after transcription, offline and instant.
2. **local** — a local LLM (LM Studio / any OpenAI-compatible server) sorts
   segments into lecture / question / answer / off_topic. See ``local_llm.py``.
3. **claude** — the Claude round trip returns ``segment_labels``.

A segment labelled ``manual`` by the user is never overwritten. Off-topic
segments are left out of the Claude export, the search index and training data.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..storage import Lecture
from .transcriber import Segment, Transcript

CATEGORIES = ("lecture", "question", "answer", "off_topic")
CATEGORY_LABELS = {
    "lecture": "Lecture content",
    "question": "Question",
    "answer": "Answer",
    "off_topic": "Private / off-topic",
}

EXAM_CUES = re.compile(
    r"klausur|\bprüfung|examen|abgefragt|kommt\s+(?:sicher\s+|bestimmt\s+|garantiert\s+)?dran|"
    r"merken\s+sie\s+sich|merkt\s+euch|sollten\s+sie\s+(?:sich\s+)?(?:merken|wissen|können)|auswendig|"
    r"(?:ist|sehr|besonders|ganz)\s+wichtig|\bexams?\b|\bmidterm\b|will\s+be\s+(?:on\s+the|tested)|"
    r"remember\s+this|make\s+sure\s+you\s+(?:know|understand)|\bimportant\b",
    re.IGNORECASE,
)

SEGMENT_ID = re.compile(r"S(\d+)\.(\d+)")


def segment_id(transcript_no: int, index: int) -> str:
    """Stable id used in exports and classifier prompts: transcript 1, segment 12 → "S1.12"."""
    return f"S{transcript_no}.{index}"


def apply_exam_cues(transcript: Transcript) -> int:
    """Flag segments whose wording signals exam relevance. Returns how many were newly flagged."""
    flagged = 0
    for seg in transcript.segments:
        if seg.exam or seg.source == "manual":
            continue
        match = EXAM_CUES.search(seg.text)
        if match:
            seg.exam = True
            seg.note = seg.note or f"Exam cue: “{match.group(0)}”"
            if not seg.source:
                seg.source = "rules"
            flagged += 1
    return flagged


def apply_label(seg: Segment, category: str | None, exam: bool | None, reason: str, source: str) -> bool:
    """Apply one label unless the user set it by hand. Returns True if applied."""
    if seg.source == "manual":
        return False
    if category in CATEGORIES:
        seg.category = category
    if exam is not None:
        # An explicit exam cue in the wording beats a classifier saying "not exam".
        seg.exam = bool(exam) or (seg.exam and seg.source == "rules")
    if reason:
        seg.note = reason
    seg.source = source
    return True


def set_manual(seg: Segment, category: str | None = None, exam: bool | None = None) -> None:
    if category is not None:
        seg.category = category
    if exam is not None:
        seg.exam = exam
    seg.source = "manual"
    seg.note = "Set by you"


def lecture_segment_map(lecture: Lecture) -> dict[str, tuple[Path, int]]:
    """Segment id → (transcript file, index) for all transcripts of a lecture."""
    mapping = {}
    for t_no, path in enumerate(lecture.transcripts(), start=1):
        for index, _ in enumerate(Transcript.load(path).segments):
            mapping[segment_id(t_no, index)] = (path, index)
    return mapping


def save_segment_map(lecture: Lecture) -> None:
    """Remember which id meant which segment at export time, for the import."""
    mapping = {sid: [path.name, index] for sid, (path, index) in lecture_segment_map(lecture).items()}
    (lecture.dir("ai") / "segment_ids.json").write_text(json.dumps(mapping), encoding="utf-8")


def expand_ids(label: dict) -> list[str]:
    """Segment ids a label covers: {"id"} / {"from"} alone, or a {"from", "to"} range."""
    first = str(label.get("from") or label.get("id") or "")
    return [segment_id(t, i) for t, i in _expand(first, label.get("to"))]


def _expand(start_id: str, end_id: str | None) -> list[tuple[int, int]]:
    a = SEGMENT_ID.fullmatch(start_id.strip())
    if not a:
        return []
    b = SEGMENT_ID.fullmatch(end_id.strip()) if end_id else a
    if not b or a.group(1) != b.group(1):
        b = a
    t_no, lo, hi = int(a.group(1)), int(a.group(2)), int(b.group(2))
    return [(t_no, i) for i in range(min(lo, hi), max(lo, hi) + 1)]


def apply_labels(lecture: Lecture, labels: list[dict], source: str, segment_map: dict | None = None) -> int:
    """Apply ``[{"id"|"from", "to"?, "category"?, "exam"?, "reason"?}]`` to the lecture's
    transcripts and save them. ``segment_map`` maps id → [file name, index]
    (from export time); defaults to the current transcripts. Returns segments changed."""
    folder = lecture.dir("transcripts")
    if segment_map is None:
        segment_map = {sid: [p.name, i] for sid, (p, i) in lecture_segment_map(lecture).items()}
    loaded: dict[str, Transcript] = {}
    changed = 0
    for label in labels:
        if not isinstance(label, dict):
            continue
        first = str(label.get("from") or label.get("id") or "")
        for t_no, index in _expand(first, label.get("to")):
            target = segment_map.get(segment_id(t_no, index))
            if not target:
                continue
            name, seg_index = target
            path = folder / name
            if not path.exists():
                continue
            transcript = loaded.setdefault(name, Transcript.load(path))
            if seg_index >= len(transcript.segments):
                continue
            exam = label.get("exam")
            if apply_label(
                transcript.segments[seg_index],
                label.get("category"),
                exam if isinstance(exam, bool) else None,
                str(label.get("reason") or "")[:200],
                source,
            ):
                changed += 1
    for transcript in loaded.values():
        transcript.save(folder)
    return changed


def summary(lecture: Lecture) -> dict[str, int]:
    counts = {"exam": 0, "off_topic": 0, "question": 0, "unclassified": 0}
    for path in lecture.transcripts():
        for seg in Transcript.load(path).segments:
            counts["exam"] += seg.exam
            counts["off_topic"] += seg.private
            counts["question"] += seg.category == "question"
            counts["unclassified"] += not seg.category
    return counts


def course_context(lecture: Lecture, limit: int = 1500) -> str:
    """Slide text excerpt so a classifier can tell course content from side talk."""
    from . import pdf_tools

    text = []
    for pdf in lecture.pdfs():
        try:
            text.extend(pdf_tools.page_texts(pdf))
        except Exception:  # unreadable PDF: classify without it
            continue
        if sum(len(t) for t in text) > limit:
            break
    joined = " ".join(" ".join(text).split())
    return f"Lecture “{lecture.name}”. {joined[:limit]}" if joined else f"Lecture “{lecture.name}”."
