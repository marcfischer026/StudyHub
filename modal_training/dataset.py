"""Build a chat-format training set from one UniVerse subject (runs locally).

Preview without Modal or any network access::

    python modal_training/dataset.py --subject "Corporate Finance"
    python modal_training/dataset.py --subject "Corporate Finance" --out cf.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

SYSTEM_PROMPT = "You are a tutor for the university course '{subject}'. Answer precisely, as the professor would expect."
_STAMP = re.compile(r"\[▶ \d{1,2}:\d{2}:\d{2}(?: · \d{1,2}:\d{2})?\]")


def data_root() -> Path:
    return Path(os.environ.get("UNIVERSE_DATA_DIR", Path.home() / ".universe_data"))


def _chunks(text: str, size: int = 1800) -> list[str]:
    out, current, length = [], [], 0
    for word in text.split():
        current.append(word)
        length += len(word) + 1
        if length >= size:
            out.append(" ".join(current))
            current, length = [], 0
    if current:
        out.append(" ".join(current))
    return out


def _example(system: str, user: str, assistant: str) -> dict:
    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ]
    }


def _question_example(system: str, question: dict) -> dict | None:
    """One stored question as Q/A; a card is its front and back. None if it has no answer to learn."""
    stem = question.get("stem") or ""
    options = question.get("options") or []
    correct = next((o["text"] for o in options if o.get("correct")), "")
    numeric = question.get("numeric") or {}
    worked_out = f"{numeric['value']} {numeric.get('unit', '')}".strip() if "value" in numeric else ""
    answer = correct or question.get("answer") or worked_out
    if not stem or not answer:
        return None
    if question.get("type") == "card":
        return _example(system, stem, answer)
    asked = "\n".join([stem, *(f"- {o['text']}" for o in options)])
    return _example(system, asked, f"{answer}\n\n{question.get('explain') or ''}".strip())


def _question_examples(subject_dir: Path, system: str) -> list[dict]:
    """Questions the student flagged as wrong or Claude retired would teach mistakes, so they stay out."""
    path = subject_dir / "study" / "knowledge.json"
    if not path.exists():
        return []
    questions = json.loads(path.read_text(encoding="utf-8")).get("questions", {}).values()
    usable = (q for q in questions if not q.get("retired") and not q.get("flagged"))
    return [example for example in (_question_example(system, q) for q in usable) if example]


def build_dataset(subject_name: str) -> list[dict]:
    """Lecture-content recall (transcripts + notes) plus Q/A from the questions imported from Claude."""
    subject_dir = data_root() / "Subjects" / subject_name
    if not subject_dir.exists():
        raise SystemExit(f"Subject folder not found: {subject_dir}")
    system = SYSTEM_PROMPT.format(subject=subject_name)
    examples: list[dict] = []
    lectures_dir = subject_dir / "Lectures"
    lecture_dirs = sorted(lectures_dir.iterdir()) if lectures_dir.is_dir() else []

    for lecture_dir in lecture_dirs:
        meta_path = lecture_dir / "lecture.json"
        if not meta_path.exists():
            continue
        lecture = json.loads(meta_path.read_text(encoding="utf-8")).get("name", lecture_dir.name)

        sources = []
        for transcript in sorted((lecture_dir / "transcripts").glob("*.json")):
            segments = json.loads(transcript.read_text(encoding="utf-8")).get("segments", [])
            sources.append(" ".join(s["text"] for s in segments if s.get("category") != "off_topic"))
        notes = lecture_dir / "notes.md"
        if notes.exists():
            sources.append(_STAMP.sub("", notes.read_text(encoding="utf-8")))

        for text in sources:
            for i, chunk in enumerate(_chunks(text)):
                examples.append(_example(system, f"Explain part {i + 1} of the lecture '{lecture}'.", chunk))

    return examples + _question_examples(subject_dir, system)


def write_jsonl(examples: list[dict], path: Path) -> Path:
    path.write_text("\n".join(json.dumps(ex, ensure_ascii=False) for ex in examples), encoding="utf-8")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preview or export the training set for a subject.")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--out", type=Path, help="write the JSONL here")
    ns = parser.parse_args()
    rows = build_dataset(ns.subject)
    print(f"{len(rows)} examples for '{ns.subject}'")
    if ns.out:
        print(f"Wrote {write_jsonl(rows, ns.out)}")
    for row in rows[:3]:
        print(json.dumps(row, ensure_ascii=False, indent=2)[:800], "\n---")
