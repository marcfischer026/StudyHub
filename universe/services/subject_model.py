"""One read model of a subject: everything the hub knows about it, under stable ids.

The Claude export, the PDF conversion, the search index and the study tools read a subject
through here, so they agree on what a document is, which talk is private and what an id means.

Ids are short and printed in every export; Claude cites them and PDF pages carry them:

=========  ==========  ==============================================================
``L03``    lecture     stays with the lecture through renames
``SL04``   document    SL slides, EX past exam, TK task, DO anything else; the prefix
                       follows its tags, the number stays
``SL04.p12``  page
``R07``    recording   stays through renames, moves and transcribing again
``R07@751``   line     a paragraph of the transcript, named by the second it starts at, so
                       it needs no snapshot and survives transcribing again
``N03.7``  note line   lecture 3, line 7
``F03.2``  figure      clipped into the notes of lecture 3
``PR1``    professor
=========  ==========  ==============================================================

A deck attached to several lectures is one document, whatever the student highlighted in
each copy: identity comes from what its pages say (``pdf_tools.content_key``).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..professors import ProfessorProfile, ProfessorStore
from ..storage import (
    DOC_TYPES,
    Document,
    Lecture,
    Subject,
    clock_time,
    read_json,
    read_slide_marks,
    read_timeline,
    recording_time,
    write_json,
)
from . import moodle_ical, pdf_conversion, pdf_tools, slide_links, speakers
from .pdf_conversion import PageCheck
from .pdf_tools import Mark
from .transcriber import Segment, Transcript

PREFIXES = {DOC_TYPES["slides"]: "SL", DOC_TYPES["exams"]: "EX", DOC_TYPES["exercises"]: "TK"}
REF_RE = re.compile(
    r"(?P<doc>SL|EX|TK|DO)(?P<docno>\d+)(?:\.p(?P<page>\d+))?"
    r"|R(?P<rec>\d+)(?:@(?P<at>\d+(?:\.\d+)?))?"
    r"|N(?P<noteno>\d+)\.(?P<line>\d+)"
    r"|F(?P<figno>\d+)\.(?P<fig>\d+)"
    r"|L(?P<lecture>\d+)"
    r"|PR(?P<prof>\d+)"
    r"|KN"
)
MIN_SLIDE_SECONDS = 5.0  # a page open for less was scrolled past, not looked at
# Whisper's segments are short; a timestamp and id on each would cost half as much again as the words.
LINE_MAX_CHARS = 280
LINE_MAX_SECONDS = 45.0
LINE_MAX_GAP = 10.0
FIGURE_RE = re.compile(r"!\[([^\]]*)\]\((figures/[^)\s]+)\)")
MARKDOWN_PREFIX = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+|>\s*)*")
MARKDOWN_MARKUP = re.compile(r"\\(?=[\\`*_{}\[\]()#+\-.!|>])|\*\*|__|[*_`]")

_registry_lock = threading.Lock()
_keys: dict[tuple[str, int, int], tuple[str, int]] = {}


def document_key(path: Path) -> tuple[str, int]:
    """``pdf_tools.content_key``, remembered until the file changes."""
    stat = path.stat()
    cache = (str(path), stat.st_mtime_ns, stat.st_size)
    if cache not in _keys:
        _keys[cache] = pdf_tools.content_key(path)
    return _keys[cache]


class IdRegistry:
    """Hands out each number once, ever; ``<Subject>/study/ids.json``. What Claude cites and
    what is stamped on a PDF page must keep meaning the same thing."""

    def __init__(self, subject: Subject):
        self.path = subject.study_dir / "ids.json"
        self.data = self._read()

    def _read(self) -> dict:
        """A lost or broken file must not restart the numbering: every export listed the numbers it used."""
        data = read_json(self.path)
        if data and isinstance(data, dict):
            return data
        try:
            self.path.replace(self.path.with_name("ids.broken.json"))  # what it held may still be of use
        except OSError:
            pass
        data = {}
        for manifest in sorted((self.path.parent / "exports").glob("*.json")):
            exported = read_json(manifest)
            for kind, id_key in (("documents", "key"), ("lectures", "uid"), ("recordings", "uid")):
                for ref, info in exported.get(kind, {}).items():
                    data.setdefault(kind, {}).setdefault(info[id_key], int(re.sub(r"\D", "", ref)))
        return data

    def number(self, kind: str, key: str) -> int:
        known = self.data.get(kind, {}).get(key)
        if known is not None:
            return known
        with _registry_lock:  # the export runs in a worker while the panels read too
            self.data = self._read()
            numbers = self.data.setdefault(kind, {})
            if key not in numbers:
                numbers[key] = max(numbers.values(), default=0) + 1
                write_json(self.path, self.data)
            return numbers[key]


@dataclass(frozen=True)
class PageView:
    number: int
    raw_text: str  # what the PDF's own text layer says
    tables: tuple[str, ...] = ()  # detected in the PDF, as Markdown
    marks: tuple[Mark, ...] = ()
    visual: bool = False  # a chart or image that the raw text misses
    markdown: str = ""  # converted by Gemini or Copilot; empty until then
    check: PageCheck | None = None  # how the Markdown compares with the PDF

    @property
    def converted(self) -> bool:
        return bool(self.markdown)

    @property
    def text(self) -> str:
        """The best text there is of this page."""
        return self.markdown or self.raw_text

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@dataclass(frozen=True)
class DocumentView:
    key: str  # d_… from what the pages say
    number: int
    ref: str  # "SL04"
    title: str  # file name
    types: tuple[str, ...]
    tags: tuple[str, ...]  # the student's own tags, without the types
    occurrences: tuple[Document, ...]  # every copy on disk; the first one is read
    lecture_refs: tuple[str, ...]  # lectures it is attached to
    whole_subject: bool  # also (or only) attached to the subject
    page_count: int
    converted_pages: int
    pages: tuple[PageView, ...] = ()  # empty when loaded with pages=False
    excluded: bool = False  # the student keeps it out of exports

    @property
    def path(self) -> Path:
        return self.occurrences[0].path

    @property
    def type_label(self) -> str:
        return " / ".join(self.types) or "Document"

    @property
    def unit(self) -> str:
        return "slide" if DOC_TYPES["slides"] in self.types else "page"

    @property
    def is_converted(self) -> bool:
        return self.converted_pages >= self.page_count

    def page(self, number: int) -> PageView | None:
        return self.pages[number - 1] if 1 <= number <= len(self.pages) else None


@dataclass(frozen=True)
class SegmentView:
    index: int
    start: float
    end: float
    clock: str  # time of day, "12:03"; empty if the recording's start is unknown
    text: str
    category: str
    exam: bool
    note: str
    source: str
    speaker: str
    near_mic: bool
    visibility: str  # see _visibility
    slide: str = ""  # page the student had open then, "SL04.p12"

    @property
    def speaker_tag(self) -> str:
        return speakers.prompt_tag(Segment(self.start, self.end, self.text, speaker=self.speaker), self.near_mic).strip()


@dataclass(frozen=True)
class LineView:
    """A paragraph of the transcript: what Claude reads and cites."""

    ref: str  # "R07@751"
    start: float
    end: float
    clock: str
    text: str
    category: str
    exam: str  # "" | "you": marked by the student | "machine": a keyword or classifier matched
    visibility: str
    speaker_tag: str
    slide: str


@dataclass(frozen=True)
class RecordingView:
    ref: str
    uid: str
    stem: str
    audio: str  # file name
    started: datetime | None
    duration: float
    segments: tuple[SegmentView, ...]
    lines: tuple[LineView, ...]  # everything except private and judged talk

    def count(self, visibility: str) -> int:
        return sum(s.visibility == visibility for s in self.segments)


@dataclass(frozen=True)
class FigureView:
    ref: str  # "F03.2"
    caption: str
    source: str  # the page it was clipped from, "SL04.p12", if known
    text: str
    tables: tuple[str, ...]


@dataclass(frozen=True)
class NoteLineView:
    ref: str  # "N03.7"
    text: str  # as written, with links to PDFs replaced by their ids
    tags: tuple[str, ...] = ()  # where the student was when writing it: page, time of day, segment
    figure: FigureView | None = None


@dataclass(frozen=True)
class LectureView:
    ref: str
    number: int
    uid: str
    name: str
    date: str  # "2026-10-28": of its first recording, else of when it was created
    lecture: Lecture
    professor_ref: str
    document_refs: tuple[str, ...]
    recordings: tuple[RecordingView, ...]
    untranscribed: tuple[str, ...]  # recordings without a transcript yet
    notes: tuple[NoteLineView, ...]
    has_own_notes: bool  # more than the heading every new lecture starts with
    content_hash: str  # changes when anything Claude would read of this lecture changes


@dataclass(frozen=True)
class ProfessorView:
    ref: str
    profile: ProfessorProfile
    lecture_refs: tuple[str, ...]
    is_default: bool  # the subject's default


@dataclass(frozen=True)
class SubjectView:
    subject: Subject
    uid: str
    name: str
    short_name: str
    description: str
    exam: dict
    professors: tuple[ProfessorView, ...]
    lectures: tuple[LectureView, ...]
    documents: tuple[DocumentView, ...]
    deadlines: tuple[moodle_ical.Event, ...]
    problems: list[str] = field(default_factory=list)  # what could not be read, for the export's data check

    def document(self, ref_or_key_or_number: str | int) -> DocumentView | None:
        wanted = ref_or_key_or_number
        if isinstance(wanted, str) and (match := re.fullmatch(r"(?:SL|EX|TK|DO)(\d+)", wanted)):
            wanted = int(match.group(1))
        return next((d for d in self.documents if d.number == wanted or d.key == wanted), None)

    def lecture(self, ref_or_uid: str) -> LectureView | None:
        return next((v for v in self.lectures if ref_or_uid in (v.ref, v.uid)), None)

    def recording(self, ref_or_uid: str) -> tuple[LectureView, RecordingView] | None:
        for lecture in self.lectures:
            for recording in lecture.recordings:
                if ref_or_uid in (recording.ref, recording.uid):
                    return lecture, recording
        return None

    @property
    def documents_for_export(self) -> tuple[DocumentView, ...]:
        return tuple(d for d in self.documents if not d.excluded and d.page_count)


def doc_prefix(types: tuple[str, ...] | list[str]) -> str:
    return next((PREFIXES[t] for t in types if t in PREFIXES), "DO")


def _load_pages(subject: Subject, primary: Path, copies: list[Path], key: str) -> tuple[PageView, ...]:
    converted = pdf_conversion.load_pages(subject, key)
    contents = pdf_tools.read_pages(primary)
    extra_marks: dict[int, tuple[Mark, ...]] = {}
    for copy in copies:
        for number, marks in pdf_tools.page_marks(copy).items():
            extra_marks[number] = extra_marks.get(number, ()) + marks
    common = pdf_conversion.common_terms([c.text for c in contents])
    offset = pdf_conversion.printed_page_offset([c.text for c in contents])
    pages = []
    for content in contents:
        marks = tuple(dict.fromkeys(content.marks + extra_marks.get(content.number, ())))  # a mark made in both copies counts once
        markdown = converted.get(content.number, "")
        check = pdf_conversion.check_page(content.text, markdown, content.number, common, offset) if markdown else None
        pages.append(PageView(content.number, content.text, content.tables, marks, content.visual, markdown, check))
    return tuple(pages)


def _load_documents(subject: Subject, lectures: list[tuple[Lecture, str]], registry: IdRegistry, pages: bool, problems: list[str]) -> tuple[DocumentView, ...]:
    """Every PDF of the subject, one view per document however many copies there are. Lectures first.
    One that cannot be read stays listed with no pages, under a key from its bytes, and goes on ``problems``."""
    grouped: dict[str, list[tuple[Document, str]]] = {}
    counts: dict[str, int] = {}

    def add(document: Document, lecture_ref: str) -> None:
        try:
            key, counts_here = document_key(document.path)
        except Exception as exc:  # PyMuPDF raises several kinds, and one bad file must not hide the subject
            key, counts_here = f"d_{hashlib.sha256(document.path.read_bytes()).hexdigest()[:12]}", 0
            problems.append(f"{document.path.name} could not be read: {exc}")
        counts[key] = counts_here
        grouped.setdefault(key, []).append((document, lecture_ref))

    for lecture, lecture_ref in lectures:
        for document in lecture.documents():
            add(document, lecture_ref)
    for document in subject.documents():
        add(document, "")

    excluded = subject.excluded_documents
    views = []
    for key, copies in grouped.items():
        documents = tuple(document for document, _ in copies)
        number = registry.number("documents", key)
        types = tuple(t for t in DOC_TYPES.values() if any(t in d.tags for d in documents))
        tags = tuple(dict.fromkeys(t for d in documents for t in d.tags if t not in DOC_TYPES.values()))
        converted = pdf_conversion.load_pages(subject, key)
        if not converted and (stored := pdf_conversion.converted_path(subject, key)):
            problems.append(f"{stored.name} could not be read.")
        views.append(
            DocumentView(
                key=key,
                number=number,
                ref=f"{doc_prefix(types)}{number:02d}",
                title=unicodedata.normalize("NFC", documents[0].path.name),
                types=types,
                tags=tags,
                occurrences=documents,
                lecture_refs=tuple(dict.fromkeys(ref for _, ref in copies if ref)),
                whole_subject=any(not ref for _, ref in copies),
                page_count=counts[key],
                converted_pages=sum(1 for n in converted if 1 <= n <= counts[key]),
                pages=_load_pages(subject, documents[0].path, [d.path for d in documents[1:]], key) if pages and counts[key] else (),
                excluded=key in excluded,
            )
        )
    return tuple(views)


def _visibility(segment: Segment, near: bool) -> str:
    """Who said a line is off-topic, and how far to trust that.

    - ``normal``: course content.
    - ``private``: the student said so; it never leaves this Mac.
    - ``judged``: Claude said so after reading it; no need to send it again.
    - ``uncertain``: a small local model said so about the lecturer's (or an unknown) voice. It
      is often wrong on noisy audio, and once dropped a whole in-class case task.
    - ``side_talk``: a local model said so about another voice or someone by the microphone.
      Mostly classmates' private talk, so it stays here unless the student opts in.
    """
    if not segment.private:
        return "normal"
    if segment.source == "manual":
        return "private"
    if segment.source == "claude":
        return "judged"
    return "side_talk" if near or (segment.speaker and segment.speaker != "lecturer") else "uncertain"


def _exam_mark(segment: SegmentView) -> str:
    if not segment.exam:
        return ""
    return "you" if segment.source == "manual" else "machine"


def _build_lines(ref: str, segments: tuple[SegmentView, ...]) -> tuple[LineView, ...]:
    groups: list[list[SegmentView]] = []
    for seg in segments:
        if seg.visibility in ("private", "judged"):
            groups.append([])  # nothing may read across what was left out
            continue
        current = groups[-1] if groups else []
        same = bool(current) and all(
            getattr(current[-1], name) == getattr(seg, name) for name in ("visibility", "category", "speaker", "near_mic", "slide")
        )
        same = same and _exam_mark(current[-1]) == _exam_mark(seg)
        if (
            same
            and seg.start - current[0].start < LINE_MAX_SECONDS
            and seg.start - current[-1].end < LINE_MAX_GAP
            and sum(len(s.text) + 1 for s in current) < LINE_MAX_CHARS
        ):
            current.append(seg)
        else:
            groups.append([seg])
    lines: list[LineView] = []
    taken: set[str] = set()
    for group in (g for g in groups if g):
        first = group[0]
        at = f"{int(first.start)}"
        for decimals in (1, 2, 3):
            if at not in taken:
                break
            at = f"{first.start:.{decimals}f}"
        taken.add(at)
        lines.append(
            LineView(
                ref=f"{ref}@{at}",
                start=first.start,
                end=group[-1].end,
                clock=first.clock,
                text=" ".join(s.text for s in group),
                category=first.category if first.category in ("question", "answer") else "",
                exam=_exam_mark(first),
                visibility=first.visibility,
                speaker_tag=first.speaker_tag,
                slide=first.slide,
            )
        )
    return tuple(lines)


def _page_ref_finder(lecture: Lecture, documents: tuple[DocumentView, ...]):
    """``find(folder, pdf name, page)`` → "SL04.p12", or "" for a PDF that is gone."""
    by_path = {unicodedata.normalize("NFC", str(occurrence.path)): view.ref for view in documents for occurrence in view.occurrences}

    def find(folder: str, name: str, page: int | None) -> str:
        base = lecture.path / "slides" if folder == "slides" else lecture.subject.path / folder
        ref = by_path.get(unicodedata.normalize("NFC", str(base / name)))
        return f"{ref}.p{page}" if ref and page else ref or ""

    return find


def _slide_at(marks: list[tuple[float, str]], seconds: float) -> str:
    return next((ref for offset, ref in reversed(marks) if offset <= seconds), "")


def _settled_marks(audio: Path, find_page) -> list[tuple[float, str]]:
    """Page turns of a recording, without pages that were only scrolled past."""
    raw = read_slide_marks(audio)
    kept = []
    for i, mark in enumerate(raw):
        following = raw[i + 1]["offset"] if i + 1 < len(raw) else float("inf")
        if following - mark["offset"] >= MIN_SLIDE_SECONDS:
            ref = find_page(mark.get("doc", "slides"), mark.get("pdf", ""), mark.get("page"))
            if ref:
                kept.append((float(mark["offset"]), ref))
    return kept


def _load_recording(lecture: Lecture, path: Path, audio: Path | None, registry: IdRegistry, find_page) -> RecordingView:
    transcript = Transcript.load(path)
    if not transcript.id:  # older transcripts get their id the first time they are read here
        transcript.ensure_id()
        transcript.save(path.parent)
    number = registry.number("recordings", transcript.id)
    ref = f"R{number:02d}"
    timeline = read_timeline(audio) if audio else None
    marks = _settled_marks(audio, find_page) if audio else []
    near = speakers.near_mic(transcript)
    segments = tuple(
        SegmentView(
            index=index,
            start=seg.start,
            end=seg.end,
            clock=f"{clock_time(audio, seg.start, timeline):%H:%M}" if audio else "",
            text=seg.text,
            category=seg.category,
            exam=seg.exam,
            note=seg.note,
            source=seg.source,
            speaker=seg.speaker,
            near_mic=near[index],
            visibility=_visibility(seg, near[index]),
            slide=_slide_at(marks, seg.start),
        )
        for index, seg in enumerate(transcript.segments)
    )
    return RecordingView(
        ref=ref,
        uid=transcript.id,
        stem=path.stem,
        audio=transcript.audio,
        started=recording_time(audio) if audio else None,
        duration=max((s.end for s in transcript.segments), default=0.0),
        segments=segments,
        lines=_build_lines(ref, segments),
    )


def plain_line(line: str) -> str:
    """A Markdown line as the editor shows it, for matching it with its anchor."""
    line = slide_links.MARKDOWN_LINK_RE.sub(r"\1", line)
    return " ".join(MARKDOWN_MARKUP.sub("", MARKDOWN_PREFIX.sub("", line)).split())


def _line_finder(recordings: tuple[RecordingView, ...]):
    """``find(audio stem, seconds)`` → the line being said then, "R07@751"; left-out talk is never pointed at."""
    by_stem = {r.stem: r for r in recordings}

    def find(stem: str, seconds: float) -> str:
        recording = by_stem.get(stem)
        if recording is None:
            return ""
        shown = [line for line in recording.lines if line.visibility != "side_talk" and line.start <= seconds]
        return shown[-1].ref if shown else ""

    return find


def _anchor_tags(anchor: dict, find_page, find_segment) -> tuple[str, ...]:
    tags = []
    if anchor.get("pdf"):
        folder = anchor.get("doc") if anchor.get("doc") in slide_links.KINDS else "slides"
        tags.append(find_page(folder, anchor["pdf"], anchor.get("page", 1)) or f"{anchor['pdf']} p.{anchor.get('page', 1)}")
    if anchor.get("clock"):
        tags.append(anchor["clock"])
    if "t" in anchor and anchor.get("audio"):
        segment = find_segment(anchor["audio"], anchor["t"])
        if segment:
            tags.append(segment)
    return tuple(tags)


def _resolve_links(line: str, find_page) -> str:
    def reference(match: re.Match) -> str:
        target = slide_links.parse_href(match.group(2))
        if target is None:
            return match.group(1)
        return f"(→ {find_page(target['doc'], target['pdf'], target['page']) or match.group(1).lstrip('→ ')})"

    return slide_links.MARKDOWN_LINK_RE.sub(reference, line)


def _load_notes(lecture: Lecture, number: int, find_page, find_segment) -> tuple[tuple[NoteLineView, ...], bool]:
    anchors = [a for a in lecture.read_anchors() if isinstance(a, dict) and str(a.get("text", "")).strip()]
    views: list[NoteLineView] = []
    figures = 0
    last_figure_rows: set[str] = set()
    last_caption = ""
    own_notes = False
    line_no = 0
    anchor_index = 0
    for line in lecture.read_notes().splitlines():
        if not line.strip():
            continue
        match = FIGURE_RE.search(line)
        if match:
            figures += 1
            line_no += 1
            source = lecture.figure_source(lecture.path / match.group(2))
            caption = source.get("caption") or match.group(1)
            tables = tuple(source.get("tables") or ())
            origin = find_page(source["doc"], source["pdf"], source.get("page")) if source.get("doc") and source.get("pdf") else ""
            figure = FigureView(f"F{number:02d}.{figures}", caption, origin, " ".join((source.get("text") or "").split()), tables)
            views.append(NoteLineView(f"N{number:02d}.{line_no}", "", figure=figure))
            last_figure_rows = {row.strip() for table in tables for row in table.splitlines()}
            last_caption = caption
            own_notes = True
            continue
        stripped = line.strip()
        if stripped in last_figure_rows or stripped.strip("*_") == last_caption:
            continue  # the editor puts a figure's caption and tables below it; the figure view has them
        line_no += 1
        tags: tuple[str, ...] = ()
        if not stripped.startswith("|"):  # table rows have no anchors
            wanted = plain_line(line)
            for i in range(anchor_index, len(anchors)):
                if " ".join(MARKDOWN_MARKUP.sub("", anchors[i]["text"]).split()) == wanted:
                    tags = _anchor_tags(anchors[i], find_page, find_segment)
                    anchor_index = i + 1
                    break
        if not (stripped.startswith("#") and plain_line(line) == lecture.name):
            own_notes = True
        views.append(NoteLineView(f"N{number:02d}.{line_no}", pdf_tools.clean_text(_resolve_links(line, find_page)), tags))
    return tuple(views), own_notes


def _lecture_date(lecture: Lecture, recordings: tuple[RecordingView, ...]) -> str:
    started = [r.started for r in recordings if r.started]
    if started:
        return f"{min(started):%Y-%m-%d}"
    return (lecture.meta.get("created") or "")[:10]


def _document_state(subject: Subject, document: DocumentView) -> tuple:
    """What is worth another look by Claude: its Markdown, and highlights, which change the file's
    size. Read the same way with or without pages, since either view may be compared with a stored hash."""
    sizes = sorted(d.path.stat().st_size for d in document.occurrences if d.path.exists())
    return document.key, sorted(pdf_conversion.load_pages(subject, document.key).items()), sizes


def _content_hash(subject: Subject, documents: tuple[DocumentView, ...], recordings: tuple[RecordingView, ...], notes: tuple[NoteLineView, ...]) -> str:
    state = {
        "documents": [_document_state(subject, d) for d in documents],
        # Not the count of normal lines: Claude's own labels change that, and its reply must not reopen the lecture.
        "recordings": [(r.uid, len(r.segments), r.count("private")) for r in recordings],
        "notes": [(n.text, n.tags) for n in notes],
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _load_professors(subject: Subject, lectures: list[Lecture], lecture_refs: dict[str, str], store: ProfessorStore, registry: IdRegistry):
    default = subject.professor_id
    order = list(dict.fromkeys([default] + [lecture.professor_id for lecture in lectures]))
    views = []
    for professor_id in (p for p in order if p):
        profile = store.get(professor_id)
        if profile is None:
            continue
        number = registry.number("professors", professor_id)
        applies = tuple(lecture_refs[lecture.uid] for lecture in lectures if lecture.professor_id == professor_id)
        views.append(ProfessorView(f"PR{number}", profile, applies, professor_id == default))
    return tuple(views)


def _deadlines(subject: Subject) -> tuple[moodle_ical.Event, ...]:
    try:
        events = moodle_ical.upcoming(moodle_ical.parse(moodle_ical.cached()))
    except (OSError, ValueError):
        return ()
    return tuple(e for e in events if moodle_ical.matches(e, subject.name, subject.short_name))


def load_subject(subject: Subject, professors: ProfessorStore | None = None, pages: bool = True, recordings: bool = True) -> SubjectView:
    """Everything about a subject. ``pages=False`` skips reading PDF pages, ``recordings=False``
    the transcripts and notes: what a panel needs to show counts stays quick."""
    registry = IdRegistry(subject)
    lectures = subject.lectures()
    numbers = {lecture.uid: registry.number("lectures", lecture.uid) for lecture in lectures}
    refs = {uid: f"L{number:02d}" for uid, number in numbers.items()}
    problems: list[str] = []
    documents = _load_documents(subject, [(lecture, refs[lecture.uid]) for lecture in lectures], registry, pages, problems)
    store = professors or ProfessorStore()
    professor_views = _load_professors(subject, lectures, refs, store, registry)
    professor_refs = {p.profile.id: p.ref for p in professor_views}

    lecture_views = []
    for lecture in lectures:
        ref = refs[lecture.uid]
        attached = tuple(d for d in documents if ref in d.lecture_refs)
        find_page = _page_ref_finder(lecture, documents)
        audio = {p.stem: p for p in lecture.audio_files()}
        recording_views: tuple[RecordingView, ...] = ()
        notes: tuple[NoteLineView, ...] = ()
        own_notes = False
        if recordings:
            loaded = []
            for path in lecture.transcripts():
                try:
                    loaded.append(_load_recording(lecture, path, audio.get(path.stem), registry, find_page))
                except Exception as exc:  # one bad file must not hide the subject
                    problems.append(f"{path.name} in {lecture.name} could not be read: {exc}")
            recording_views = tuple(loaded)
            notes, own_notes = _load_notes(lecture, numbers[lecture.uid], find_page, _line_finder(recording_views))
        transcribed = {path.stem for path in lecture.transcripts()}
        lecture_views.append(
            LectureView(
                ref=ref,
                number=numbers[lecture.uid],
                uid=lecture.uid,
                name=lecture.name,
                date=_lecture_date(lecture, recording_views),
                lecture=lecture,
                professor_ref=professor_refs.get(lecture.professor_id, ""),
                document_refs=tuple(d.ref for d in attached),
                recordings=recording_views,
                untranscribed=tuple(stem for stem in audio if stem not in transcribed),
                notes=notes,
                has_own_notes=own_notes,
                content_hash=_content_hash(subject, attached, recording_views, notes),
            )
        )
    return SubjectView(
        subject=subject,
        uid=subject.uid,
        name=subject.name,
        short_name=subject.short_name,
        description=subject.meta.get("description", ""),
        exam=subject.exam,
        professors=professor_views,
        lectures=tuple(lecture_views),
        documents=documents,
        deadlines=_deadlines(subject),
        problems=problems,
    )


def best_pages(document: Document, subject: Subject) -> list[tuple[int, str]]:
    """``(page, best text)`` of one PDF: the search index does not need the rest. A conversion
    that failed its check against the PDF gives way to the PDF's own text."""
    try:
        key, _ = document_key(document.path)
        texts = pdf_tools.page_texts(document.path)
    except Exception:  # load_subject reports it
        return []
    converted = pdf_conversion.load_pages(subject, key)
    common, offset = pdf_conversion.common_terms(texts), pdf_conversion.printed_page_offset(texts)
    pages = []
    for number, text in enumerate(texts, start=1):
        markdown = converted.get(number, "")
        good = markdown and pdf_conversion.check_page(text, markdown, number, common, offset).ok
        pages.append((number, markdown if good else text))
    return pages


def locate(view: SubjectView, source: dict) -> dict | None:
    """Where a stored source is now, as the jump handlers want it: ``{"doc", "pdf", "page", "lecture"}``
    for a page, ``{"audio", "t", "lecture"}`` for a moment in a recording, ``{"lecture"}`` for a
    note or figure. ``lecture`` is a Lecture, or None for a document of the whole subject. None if it is gone."""
    kind = source.get("kind")
    if kind == "page":
        document = view.document(source.get("doc", ""))
        if document is None:
            return None
        occurrence = document.occurrences[0]
        lecture = next((v.lecture for v in view.lectures if v.ref in document.lecture_refs), None)
        if occurrence.folder != "slides":
            lecture = None
        return {"doc": occurrence.folder, "pdf": occurrence.path.name, "page": source.get("page", 1), "lecture": lecture}
    if kind == "line":
        found = view.recording(source.get("rec", ""))
        if found is None:
            return None
        lecture, recording = found
        return {"audio": recording.stem, "t": float(source.get("t", 0.0)), "lecture": lecture.lecture}
    if kind in ("note", "figure"):
        lecture = view.lecture(source.get("lecture", ""))
        return {"lecture": lecture.lecture} if lecture else None
    return None
