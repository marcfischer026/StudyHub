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
_STAMP = re.compile(r"\[▶ \d{1,2}:\d{2}:\d{2}\]")


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


def build_dataset(subject_name: str) -> list[dict]:
    """Lecture-content recall (transcripts + notes) plus Q/A from imported quizzes and flashcards."""
    subject_dir = data_root() / "Subjects" / subject_name
    if not subject_dir.exists():
        raise SystemExit(f"Subject folder not found: {subject_dir}")
    system = SYSTEM_PROMPT.format(subject=subject_name)
    examples: list[dict] = []

    for lecture_dir in sorted((subject_dir / "Lectures").iterdir()):
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

        for response in sorted((lecture_dir / "ai").glob("response_*.json")):
            data = json.loads(response.read_text(encoding="utf-8"))
            for quiz in data.get("quizzes", []):
                question = quiz["question"]
                if quiz.get("options"):
                    question += "\n" + "\n".join(f"- {o}" for o in quiz["options"])
                answer = f"{quiz['answer']}\n\n{quiz.get('explanation', '')}".strip()
                examples.append(_example(system, question, answer))
            for card in data.get("flashcards", []):
                examples.append(_example(system, card["front"], card["back"]))
    return examples


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
