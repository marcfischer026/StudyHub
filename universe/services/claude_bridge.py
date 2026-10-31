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
from ..storage import Document, Lecture, Subject, clock_time, read_timeline
from . import pdf_tools, relevance, slide_links, speakers
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
profile, the student's notes, the lecture transcript, the slides and the subject's
past exams and tasks — and prepare exam material for the lecture **{lecture_name}**.

1. Tailor everything to the professor profile: match their testing style, question
   types and focus areas.
2. Write **{n_questions} quiz questions** mixing types and difficulties, each
   pointing back to a slide page or transcript timestamp in `source_ref`.
   Every document says its type (slides, past exam, task) and the student's own
   tags in its heading. Where past exams or tasks are included, they show how this
   professor actually tests: weight `exam_focus` by them and write questions
   in their style. Figures the student clipped into the notes and text they
   highlighted on the slides are what they found important — make sure those
   are covered.
3. Lines of the student's notes end with a `⟨…⟩` tag: the slide or page that
   was open, the time of day and the transcript segment (`S…`) the lecturer
   was at when the student wrote the line. Use it to connect notes, slides and
   transcript — e.g. to fill in what the notes left out.
4. List the topics most likely to appear on the exam, and the gaps or mistakes
   you see in the student's notes.
5. The recording also catches students chatting near the microphone. In
   `segment_labels`, using the `[S…]` ids from the transcript, list every
   segment (or `from`/`to` range within one recording) that is **off_topic**
   (private chatter, unrelated talk, noise), a student **question** about the
   content, or the **answer** to one — plus every segment where the lecturer
   signals exam relevance (`"exam": true`, keep its category `lecture`).
   Segments you do not list count as normal lecture content. Lines may carry a
   speaker tag from automatic voice recognition: `[Lecturer]` is the voice that
   talks most, `[Speaker 2]` etc. are other voices (usually students), and
   `near mic` marks someone much louder than usual, i.e. sitting next to the
   laptop. The tags are automatic and can be wrong: use them only as hints and
   decide off_topic by the wording, never by the tag alone. Off-topic lines are
   dropped from the student's material, so when in doubt leave a line out of
   `segment_labels` (it stays lecture content). Ignore off-topic
   segments for everything else you write.

**Respond with exactly one JSON code block and nothing else.** It must parse as
JSON and follow this schema (`options` only for `multiple_choice`; for
`true_false` the answer is `"True"` or `"False"`):

```json
{template}
```
"""


def _transcript_section(lecture: Lecture) -> str:
    """Transcript lines carry ``[S1.12 ▶ 00:01:05 · 12:01]`` ids (with the time of day
    when the audio is there) for ``segment_labels``.
    Segments already marked private are left out; exam-flagged ones are listed first."""
    exam_lines, parts, hidden = [], [], 0
    audio = {p.stem: p for p in lecture.audio_files()}
    for t_no, path in enumerate(lecture.transcripts(), start=1):
        transcript = Transcript.load(path)
        source = audio.get(Path(transcript.audio).stem)
        timeline = read_timeline(source) if source else None
        lines = []
        near = speakers.near_mic(transcript)
        for index, seg in enumerate(transcript.segments):
            if seg.private:
                hidden += 1
                continue
            clock = clock_time(source, seg.start, timeline) if source else None
            tag = f"[{relevance.segment_id(t_no, index)} {make_stamp(seg.start, clock)[1:-1]}]"
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


FIGURE_RE = re.compile(r"!\[([^\]]*)\]\((figures/[^)\s]+)\)")


MARKDOWN_PREFIX = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+|>\s*)*")
MARKDOWN_MARKUP = re.compile(r"\\(?=[\\`*_{}\[\]()#+\-.!|>])|\*\*|__|[*_`]")


def _plain(line: str) -> str:
    """A Markdown line as the editor shows it, for matching it with its anchor."""
    line = slide_links.MARKDOWN_LINK_RE.sub(r"\1", line)
    return " ".join(MARKDOWN_MARKUP.sub("", MARKDOWN_PREFIX.sub("", line)).split())


def _segment_finder(lecture: Lecture):
    """``find(audio stem, seconds)`` → the exported transcript segment playing then, e.g. "S1.12"."""
    by_stem = {}
    for t_no, path in enumerate(lecture.transcripts(), start=1):
        transcript = Transcript.load(path)
        shown = [(seg.start, i) for i, seg in enumerate(transcript.segments) if not seg.private]  # as exported
        by_stem[Path(transcript.audio).stem] = (t_no, shown)

    def find(stem: str, seconds: float) -> str | None:
        t_no, shown = by_stem.get(stem, (0, []))
        index = max((i for start, i in shown if start <= seconds), default=None)
        return relevance.segment_id(t_no, index) if index is not None else None

    return find


def _doc_words(lecture: Lecture):
    """``words(target)`` → (prefix, unit) for a linked PDF by its current tags. A PDF that is
    gone is named by the folder the link points to."""
    types = {(d.folder, d.path.name): d.types for d in lecture.documents() + lecture.subject.documents()}

    def words(target: dict) -> tuple[str, str]:
        found = types.get((target["doc"], target["pdf"]))
        return slide_links.words_for(found) if found is not None else slide_links.KINDS[target["doc"]]

    return words


def _anchor_tag(anchor: dict, find_segment, words) -> str:
    parts = []
    if anchor.get("pdf"):
        doc = anchor.get("doc") if anchor.get("doc") in slide_links.KINDS else "slides"
        kind, unit = words({"doc": doc, "pdf": anchor["pdf"]})
        parts.append(f"{kind.lower()}{anchor['pdf']} {'slide' if unit == 'slide' else 'page'} {anchor.get('page', 1)}")
    if anchor.get("clock"):
        parts.append(anchor["clock"])
    if "t" in anchor and anchor.get("audio"):
        segment = find_segment(anchor["audio"], anchor["t"])
        if segment:
            parts.append(segment)
    return f" ⟨{' · '.join(parts)}⟩" if parts else ""


def _tag_note_lines(lecture: Lecture, notes: str) -> str:
    """Append each line's hidden anchor (see note_editor) as a ``⟨slide · time · segment⟩`` tag."""
    anchors = [a for a in lecture.read_anchors() if a.get("text", "").strip()]
    if not anchors:
        return notes
    find_segment = _segment_finder(lecture)
    words = _doc_words(lecture)
    lines = notes.splitlines()
    start = 0
    for anchor in anchors:
        wanted = " ".join(MARKDOWN_MARKUP.sub("", anchor["text"]).split())
        for i in range(start, len(lines)):
            line = lines[i]
            if line.lstrip().startswith("|") or FIGURE_RE.search(line):
                continue  # table rows and figures are matched by neither
            if _plain(line) == wanted:
                lines[i] = line.rstrip() + _anchor_tag(anchor, find_segment, words)
                start = i + 1
                break
    return "\n".join(lines)


def _notes_section(lecture: Lecture) -> str:
    """The notes, each line tagged with its slide and time, and each clipped
    figure replaced by what Claude can read of it."""
    count = 0

    def figure(match: re.Match) -> str:
        nonlocal count
        count += 1
        source = lecture.figure_source(lecture.path / match.group(2))
        lines = [f"> **[Figure F{count} — {source.get('caption') or match.group(1)}]** (clipped by the student as important)"]
        if source.get("text") and not source.get("tables"):  # a table already carries its text, in order
            lines.append("> Text in the figure: " + " ".join(source["text"].split()))
        for table in source.get("tables", []):
            lines += [">", *(f"> {row}" for row in table.splitlines())]
        return "\n".join(lines)

    notes = FIGURE_RE.sub(figure, _tag_note_lines(lecture, lecture.read_notes().strip()))
    notes = slide_links.for_export(notes, _doc_words(lecture))
    return notes or "_No notes yet._"


def _pdf_section(document: Document, unit: str = "Page") -> str:
    pdf = document.path
    pages = pdf_tools.page_texts(pdf)
    marks = pdf_tools.highlights(pdf)
    blocks = []
    for i, text in enumerate(pages, start=1):
        if not text:
            continue
        marked = "".join(f"\n- ✎ highlighted: {m}" for m in marks.get(i, []))
        blocks.append(f"**{unit} {i}**{marked}\n\n{text}")
    own_tags = [t for t in document.tags if t not in document.types]
    tags = f"\n\n**Tags:** {', '.join(own_tags)}" if own_tags else ""
    return f"### {pdf.name} — Type: {document.type_label}{tags}\n\n" + ("\n\n".join(blocks) or "_No extractable text (scanned?)._")


def _lecture_documents_section(lecture: Lecture) -> str:
    return "\n\n".join(_pdf_section(doc) for doc in lecture.documents()) or "_No documents._"


def _course_material_section(subject: Subject) -> str:
    """The documents of the whole subject, e.g. past exams and tasks."""
    return "\n\n".join(_pdf_section(doc) for doc in subject.documents()) or "_None added yet._"


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

{_notes_section(lecture)}

## Lecture Transcript

{_transcript_section(lecture)}

## Documents of This Lecture

{_lecture_documents_section(lecture)}

## Documents of the Whole Subject ({subject.name}, all lectures)

{_course_material_section(subject)}
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
