"""Claude round trip: export a lecture as ``Context.md``, import the JSON reply.

There is no API call anywhere: the user pastes ``Context.md`` into Claude and
pastes Claude's answer back. The prompt pins Claude to a strict JSON schema,
and :func:`parse_response` validates it before anything is written to disk.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..professors import ProfessorProfile
from ..storage import Lecture
from . import pdf_tools, relevance, speakers
from .timestamps import make_stamp
from .transcriber import Transcript

SCHEMA_VERSION = "1.0"
QUESTION_TYPES = ("multiple_choice", "true_false", "short_answer")
DIFFICULTIES = ("easy", "medium", "hard")

RESPONSE_TEMPLATE = {
    "schema_version": SCHEMA_VERSION,
    "lecture": "<lecture name, copied from the context>",
    "summary": "<5-10 sentence summary of the lecture>",
    "key_concepts": [{"term": "<concept>", "explanation": "<1-3 sentences>"}],
    "exam_focus": [{"topic": "<topic>", "likelihood": "high|medium|low", "reason": "<why this professor would test it>"}],
    "knowledge_gaps": ["<things the student's notes miss or get wrong>"],
    "quizzes": [
        {
            "question": "<question text>",
            "type": "multiple_choice|true_false|short_answer",
            "options": ["<A>", "<B>", "<C>", "<D>"],
            "answer": "<exact text of the correct option, 'True'/'False', or a model answer>",
            "explanation": "<why this is correct>",
            "difficulty": "easy|medium|hard",
            "source_ref": "<slide page or [▶ HH:MM:SS] timestamp>",
        }
    ],
    "flashcards": [{"front": "<prompt>", "back": "<answer>"}],
    "segment_labels": [
        {
            "from": "S1.12",
            "to": "S1.15",
            "category": "off_topic|question|answer|lecture",
            "exam": False,
            "reason": "<max 8 words>",
        }
    ],
}


def build_prompt(lecture_name: str, n_questions: int) -> str:
    template = json.dumps(RESPONSE_TEMPLATE, indent=2, ensure_ascii=False)
    return f"""## Instructions for Claude

You are a university tutor. Study every section of this document — the professor
profile, the student's notes, the lecture transcript and the slides — and prepare
exam material for the lecture **{lecture_name}**.

1. Tailor everything to the professor profile: match their testing style, question
   types and focus areas.
2. Write **{n_questions} quiz questions** mixing types and difficulties, each
   pointing back to a slide page or transcript timestamp in `source_ref`.
3. List the topics most likely to appear on the exam, and the gaps or mistakes
   you see in the student's notes.
4. The recording also catches students chatting near the microphone. In
   `segment_labels`, using the `[S…]` ids from the transcript, list every
   segment (or `from`/`to` range within one recording) that is **off_topic**
   (private chatter, unrelated talk, noise), a student **question** about the
   content, or the **answer** to one — plus every segment where the lecturer
   signals exam relevance (`"exam": true`, keep its category `lecture`).
   Segments you do not list count as normal lecture content. Lines may carry a
   speaker tag from automatic voice recognition: `[Lecturer]` is the voice that
   talks most, `[Speaker 2]` etc. are other voices (usually students), and
   `near mic` marks someone much louder than usual, i.e. sitting next to the
   laptop. Use the tags as strong hints, but trust the wording when it plainly
   disagrees. Ignore off-topic
   segments for everything else you write.

**Respond with exactly one JSON code block and nothing else.** It must parse as
JSON and follow this schema (`options` only for `multiple_choice`; for
`true_false` the answer is `"True"` or `"False"`):

```json
{template}
```
"""


def _transcript_section(lecture: Lecture) -> str:
    """Transcript lines carry ``[S1.12 ▶ 00:01:05]`` ids for ``segment_labels``.
    Segments already marked private are left out; exam-flagged ones are listed first."""
    exam_lines, parts, hidden = [], [], 0
    for t_no, path in enumerate(lecture.transcripts(), start=1):
        transcript = Transcript.load(path)
        lines = []
        near = speakers.near_mic(transcript)
        for index, seg in enumerate(transcript.segments):
            if seg.private:
                hidden += 1
                continue
            tag = f"[{relevance.segment_id(t_no, index)} {make_stamp(seg.start)[1:-1]}]"
            kind = {"question": " (question)", "answer": " (answer)"}.get(seg.category, "")
            lines.append(f"{tag}{kind} {speakers.prompt_tag(seg, near[index])}{seg.text}")
            if seg.exam:
                exam_lines.append(f"- {tag} {seg.text}")
        parts.append(f"### {transcript.audio}\n\n" + ("\n".join(lines) or "_Only private talk._"))
    if not parts:
        return "_No transcript yet._"
    head = ""
    if exam_lines:
        head += "### Marked as exam-relevant\n\n" + "\n".join(exam_lines) + "\n\n"
    if hidden:
        head += f"_{hidden} segment(s) of private talk were removed before export._\n\n"
    return head + "\n\n".join(parts)


def _slides_section(lecture: Lecture) -> str:
    parts = []
    for pdf in lecture.pdfs():
        pages = pdf_tools.page_texts(pdf)
        body = "\n\n".join(f"**Page {i}**\n\n{text}" for i, text in enumerate(pages, start=1) if text)
        parts.append(f"### {pdf.name}\n\n{body or '_No extractable text (scanned slides?)._'}")
    return "\n\n".join(parts) or "_No slides._"


def build_context(lecture: Lecture, professor: ProfessorProfile | None, n_questions: int = 10) -> str:
    subject = lecture.subject
    other_lectures = [lec.name for lec in subject.lectures() if lec.path != lecture.path]
    professor_md = professor.to_markdown() if professor else "_No professor profile assigned._"
    return f"""# UniVerse Context — {subject.name} / {lecture.name}

- **Subject:** {subject.name}
- **Lecture:** {lecture.name}
- **Exported:** {datetime.now():%Y-%m-%d %H:%M}
- **Other lectures in this subject:** {", ".join(other_lectures) or "none"}

{build_prompt(lecture.name, n_questions)}
---

## Professor Profile

{professor_md}

## Student Notes

{lecture.read_notes().strip() or "_No notes yet._"}

## Lecture Transcript

{_transcript_section(lecture)}

## Slides

{_slides_section(lecture)}
"""


def export_context(lecture: Lecture, professor: ProfessorProfile | None, n_questions: int = 10) -> Path:
    path = lecture.dir("ai") / "Context.md"
    path.write_text(build_context(lecture, professor, n_questions), encoding="utf-8")
    relevance.save_segment_map(lecture)
    return path


# --- import -----------------------------------------------------------------


class ResponseError(ValueError):
    pass


@dataclass
class AIResponse:
    data: dict

    @property
    def quizzes(self) -> list[dict]:
        return self.data["quizzes"]


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a reply that may include fences or prose."""
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fenced:
        return max(fenced, key=len)
    start = text.find("{")
    if start == -1:
        raise ResponseError("No JSON object found in the pasted text.")
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ResponseError("The JSON object is not closed — was the reply cut off?")


def _require(cond: bool, message: str, errors: list[str]) -> bool:
    if not cond:
        errors.append(message)
    return cond


def _validate(data: dict) -> list[str]:
    errors: list[str] = []
    for key, kind in (("summary", str), ("quizzes", list), ("key_concepts", list), ("exam_focus", list)):
        _require(isinstance(data.get(key), kind), f"'{key}' must be a {kind.__name__}.", errors)
    for i, label in enumerate(data.get("segment_labels") or [], start=1):
        if _require(isinstance(label, dict), f"segment_labels[{i}] must be an object.", errors):
            _require(
                isinstance(label.get("from") or label.get("id"), str),
                f"segment_labels[{i}] needs a 'from' segment id like \"S1.12\".",
                errors,
            )
            if "category" in label:
                _require(
                    label["category"] in relevance.CATEGORIES,
                    f"segment_labels[{i}].category must be one of {relevance.CATEGORIES}.",
                    errors,
                )
    for key in ("knowledge_gaps", "flashcards", "segment_labels"):
        if key in data:
            _require(isinstance(data[key], list), f"'{key}' must be a list.", errors)

    for i, quiz in enumerate(data.get("quizzes") or [], start=1):
        where = f"quizzes[{i}]"
        if not _require(isinstance(quiz, dict), f"{where} must be an object.", errors):
            continue
        for key in ("question", "answer"):
            _require(isinstance(quiz.get(key), str) and quiz[key].strip(), f"{where}.{key} is missing.", errors)
        qtype = quiz.get("type")
        _require(qtype in QUESTION_TYPES, f"{where}.type must be one of {QUESTION_TYPES}.", errors)
        if quiz.get("difficulty") not in DIFFICULTIES:
            quiz["difficulty"] = "medium"
        if qtype == "multiple_choice":
            options = quiz.get("options")
            if _require(
                isinstance(options, list) and len(options) >= 2 and all(isinstance(o, str) for o in options),
                f"{where}.options needs at least two strings.",
                errors,
            ):
                _require(quiz.get("answer") in options, f"{where}.answer must match one of its options exactly.", errors)
        elif qtype == "true_false":
            _require(str(quiz.get("answer")).lower() in ("true", "false"), f"{where}.answer must be True or False.", errors)

    for i, concept in enumerate(data.get("key_concepts") or [], start=1):
        _require(isinstance(concept, dict) and "term" in concept, f"key_concepts[{i}] needs a 'term'.", errors)
    for i, card in enumerate(data.get("flashcards") or [], start=1):
        _require(isinstance(card, dict) and "front" in card and "back" in card, f"flashcards[{i}] needs front/back.", errors)
    return errors


def parse_response(text: str) -> AIResponse:
    try:
        data = json.loads(_extract_json(text))
    except json.JSONDecodeError as exc:
        raise ResponseError(f"Invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ResponseError("The top-level JSON value must be an object.")
    errors = _validate(data)
    if errors:
        shown = "\n".join(f"• {e}" for e in errors[:15])
        more = f"\n…and {len(errors) - 15} more" if len(errors) > 15 else ""
        raise ResponseError(f"The response does not match the UniVerse schema:\n{shown}{more}")
    data.setdefault("knowledge_gaps", [])
    data.setdefault("flashcards", [])
    data.setdefault("segment_labels", [])
    return AIResponse(data)


def save_response(lecture: Lecture, response: AIResponse) -> Path:
    """Write a timestamped copy plus ``latest.json`` into the lecture's ai/ folder."""
    folder = lecture.dir("ai")
    payload = json.dumps(
        {**response.data, "imported_at": datetime.now().isoformat(timespec="seconds")}, indent=2, ensure_ascii=False
    )
    stamped = folder / f"response_{datetime.now():%Y%m%d_%H%M%S}.json"
    stamped.write_text(payload, encoding="utf-8")
    (folder / "latest.json").write_text(payload, encoding="utf-8")
    return stamped


def load_latest(lecture: Lecture) -> AIResponse | None:
    path = lecture.dir("ai") / "latest.json"
    if not path.exists():
        return None
    try:
        return AIResponse(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return None


def apply_segment_labels(lecture: Lecture, response: AIResponse) -> int:
    """Write Claude's ``segment_labels`` into the transcripts (ids as exported)."""
    labels = response.data.get("segment_labels") or []
    if not labels:
        return 0
    map_path = lecture.dir("ai") / "segment_ids.json"
    try:
        mapping = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        mapping = None  # exported by an older version: fall back to the current order
    return relevance.apply_labels(lecture, labels, "claude", mapping)
