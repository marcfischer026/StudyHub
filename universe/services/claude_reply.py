"""Reading what Claude sends back: JSON Lines, checked record by record against what was exported.

Paste is the transport, so a reply may be cut off, wrapped in prose or hold a broken line. All
of that costs one record, never the whole import. What Claude cites is resolved through the
export's manifest into durable sources (see ``subject_model``) and its quotes are checked against
the cited page or line: a bad citation is a warning and the record stays, marked unverified,
because rejecting it would cost the student a round trip.

Validate a reply from the command line (Claude Code does this):
``python -m universe.services.claude_reply <file or folder> --subject NAME``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from ..storage import Subject, Workspace, read_json, write_json
from .pdf_tools import PRIVATE_USE
from .subject_model import REF_RE, LectureView, SubjectView, load_subject

SCHEMA = "universe.reply/2"
TUTOR_EXPORT = "tutor"  # tutor rounds have no manifest: their ids are read with the subject as it is now
KEEP_MANIFESTS = 20
FUZZY_SHARE = 0.8  # the longest common run must span this much of the quote
RECORD_ORDER = (  # dependencies first: a question needs its objective, an objective its topic
    "topic", "exam_model", "objective", "q", "guide", "said", "conflict", "note_fix", "label",
    "doc_note", "prof", "coverage", "merge", "retire", "attempt", "grade", "tutor_note",
)
QUESTION_TYPES = ("mc", "tf", "short", "case", "calc", "card")
TRUTHS = ("slides", "said", "exam", "notes", "outside")
CONFIDENCES = ("low", "medium", "high")
DIFFICULTIES = ("easy", "medium", "hard")
BLOOMS = ("remember", "understand", "apply", "analyze")
SAID_KINDS = ("exam_hint", "example", "explanation", "correction")
VERDICTS = ("wrong", "incomplete", "ok")
LABEL_CATEGORIES = ("lecture", "question", "answer", "off_topic")
DOC_ISSUES = ("wrong_subject", "outdated", "duplicate", "unreadable", "other")
PROF_FIELDS = ("testing_style", "focus_areas", "question_types", "grading_notes", "style_notes")
EVIDENCE_TIERS = ("SL", "R", "EX", "TK")  # what can carry a fact on its own
CUT_OFF = "The reply is cut off: it stops without an end record."
REPAIR_WARNINGS = 60  # enough to act on; a long list of the same warning helps nobody

# Printed in the prompt, so what Claude is told cannot drift from what is checked here.
RECORD_CATALOGUE = f"""\
{{"t":"head","schema":"{SCHEMA}","export":"<the export id>","part":1}}
{{"t":"exam_model","formats":["90 min written: 3 calculation tasks, 20 MC"],"duration_min":90,"aids":"Calculator","type_mix":{{"calc":0.5,"mc":0.3,"short":0.2}},"topic_weights":{{"t_bilanz":0.3}},"confidence":"{'|'.join(CONFIDENCES)}","why":"…","src":[{{"id":"EX09.p1","q":"verbatim"}}]}}
{{"t":"topic","id":"t_bilanz","title":"Bilanz","lectures":["L03"],"importance":3,"why":"…","signals":["lecturer_cue:R07@797","past_exam:EX09.p2"],"misconceptions":["…"],"src":[{{"id":"SL04.p12","q":"verbatim"}}]}}
{{"t":"objective","id":"L03-o012","topic":"t_bilanz","statement":"Aufbau der Bilanz erklären","importance":3,"truth":"{'|'.join(TRUTHS)}","signals":["…"],"confusable_with":["L03-o013"],"src":[…]}}
{{"t":"q","id":"q_L03_001","objective":"L03-o012","type":"mc","stem":"…","options":[{{"text":"…","correct":true}},{{"text":"…","why_wrong":"…"}}],"explain":"…","difficulty":"{'|'.join(DIFFICULTIES)}","bloom":"{'|'.join(BLOOMS)}","style_of":"EX09.p3","hints":["…"],"src":[…]}}
   type tf: "answer":"True"|"False".  short, case: "answer":"model answer","rubric":[{{"point":"…","marks":1}}].
   calc: "answer":"…","numeric":{{"value":1200,"tolerance":0.5,"unit":"€"}},"steps":["…"],"rubric":[…].  card: stem is the front, "answer" the back.
{{"t":"guide","topic":"t_bilanz","md":"exam-ready sheet with inline citations like [SL04.p12]"}}
{{"t":"said","id":"s_01","lecture":"L03","kind":"{'|'.join(SAID_KINDS)}","text":"…","slide":"SL04.p12","src":[{{"id":"R07@797","q":"verbatim"}}]}}
{{"t":"conflict","id":"c_01","a":"SL04.p9","b":"R07@1211","issue":"…","working":"a","needs_student_check":false}}
{{"t":"note_fix","lecture":"L03","line":"N03.7","verdict":"{'|'.join(VERDICTS)}","fix":"…","src":[…]}}
{{"t":"label","from":"R07@948","to":"R07@995","category":"{'|'.join(LABEL_CATEGORIES)}","exam":false,"reason":"…"}}
{{"t":"doc_note","doc":"SL08","issue":"{'|'.join(DOC_ISSUES)}","detail":"…"}}
{{"t":"prof","prof":"PR1","field":"{'|'.join(PROF_FIELDS)}","value":"…","src":[…]}}
{{"t":"coverage","lecture":"L03","covered":["SL04.p1-40","R07"],"uncovered":[{{"page":"SL04.p2","why":"agenda only"}}]}}
{{"t":"merge","into":"L03-o012","from":["L07-o004"]}}
{{"t":"retire","id":"q_L03_001","why":"…"}}
{{"t":"attempt","id":"optional","q":"q_x","correct":true,"score":1.0,"answer":"what I said","at":"2026-11-02T18:00:00","by":"claude"}}
{{"t":"grade","attempt":"a_x","score":0.5,"feedback":"…"}}
{{"t":"tutor_note","topic":"t_x","diagnosis":"…","next":"…"}}
{{"t":"end","records":123,"more":false}}"""

_OPENER = re.compile(r'\s*(?:\{\s*"records"\s*:\s*)?\[')  # the array around records: read them one by one
_RECORD_START = re.compile(r'\{\s*"(?:t|records)"\s*:')
_FRAGMENT = re.compile(r'"t"\s*:')  # a record that lost its opening brace
_EXPORT_ID = re.compile(r"[A-Za-z0-9_.-]+")
_LECTURE_IN_ID = re.compile(r"(L\d+)[-_]")
_COVER_RE = re.compile(r"(?:SL|EX|TK|DO)0*(\d+)(?:\.p(\d+)(?:\s*[-–]\s*(?:(?:SL|EX|TK|DO)\d+\.p)?(\d+))?)?")
_REF_TOKEN = r"(?:(?:SL|EX|TK|DO)\d+(?:\.p\d+(?:[-–]\d+)?)?|R\d+(?:@\d+(?:\.\d+)?)?|N\d+\.\d+|F\d+\.\d+|PR\d+|KN)"
_INLINE_CITATION = re.compile(rf"\[\s*({_REF_TOKEN}(?:\s*[,;]\s*{_REF_TOKEN})*)\s*\]")
_WHOLE_SOURCE = re.compile(r"(?:SL|EX|TK|DO)\d+|R\d+")
_RANGE_END = re.compile(r"[-–]\d+$")  # SL04.p12-14 stands on its first page
_REF_IN_TEXT = re.compile(rf"(?<![A-Za-z0-9]){_REF_TOKEN}(?![A-Za-z0-9])")

_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_LIST_DASH = re.compile(r"^\s*[-+]\s+", re.MULTILINE)
_MARKUP_GAP = re.compile(r"<br\s*/?>|[|#>•·▪▫◦‣■□●○]")
_MARKUP_GONE = re.compile(r"[*_`~\\$\"'„“”‚‘’«»]")
_HYPHENATED = re.compile(r"(\w)[-‐]\s+(\w)")


def normalise(text: str) -> str:
    """Text as compared with a quote: PDFs hyphenate and hide glyphs, Markdown adds markup,
    and neither says anything about whether the words are there."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub("\u00ad\\s*", "", text)
    text = PRIVATE_USE.sub(" ", text)
    text = _LIST_DASH.sub(" ", _LINK.sub(r"\1", text))
    text = _MARKUP_GONE.sub("", _MARKUP_GAP.sub(" ", text))
    text = _HYPHENATED.sub(r"\1\2", text)
    return " ".join(text.casefold().split())


def quote_found(quote: str, haystack: str) -> bool:
    """Both already normalised."""
    if not quote or not haystack:
        return False
    if quote in haystack:
        return True
    match = SequenceMatcher(None, haystack, quote, autojunk=False).find_longest_match(0, len(haystack), 0, len(quote))
    return match.size >= FUZZY_SHARE * len(quote)


def question_id(topic: str, stem: str) -> str:
    return "q_" + hashlib.sha1((topic + normalise(stem)).encode()).hexdigest()[:10]


def _short(text: str, limit: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _name(record: dict) -> str:
    """How an issue names a record: "q q_L03_001"."""
    for key in ("id", "topic", "lecture", "from", "doc", "prof", "q", "attempt", "into"):
        if isinstance(record.get(key), str) and record[key]:
            return f"{record.get('t', '?')} {record[key]}"
    return str(record.get("t", "?"))


@dataclass
class Issue:
    level: str  # "error" | "warning"
    line: int
    record: str
    message: str


@dataclass
class ParsedReply:
    records: list[dict]  # each record gets "_line", and "_part" once a head named it
    issues: list[Issue]
    export_id: str
    parts: list[int]
    finished: bool  # the last part arrived: an end record with more=false
    truncated: bool  # stopped in the middle; an end with more=true is a pause, not a cut


def _read_records(text: str) -> tuple[list[dict], list[Issue], bool]:
    """Every record that can be read, and whether the last thing in the text was broken."""
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    decoder = json.JSONDecoder(strict=False)  # a real line break inside a string costs nothing
    records: list[dict] = []
    issues: list[Issue] = []
    broken_last = False
    part = 0
    pos = 0
    while pos < len(text):
        end = text.find("\n", pos)
        end = len(text) if end < 0 else end
        line = text[pos:end]
        body = line.lstrip(" \t,")
        first = pos + len(line) - len(body)
        number = bisect_right(starts, first)
        if not body.strip() or body.startswith("```"):
            pos = end + 1
            continue
        if body[0] == "{":
            start = first
        elif body[0] == "[":
            pos = first + 1  # an array, or prose in brackets: what is inside still gets read
            continue
        elif match := _RECORD_START.search(line):
            start = pos + match.start()  # "Here is the first record: {…"
        else:
            if _FRAGMENT.match(body):
                issues.append(Issue("error", number, "", f"Not a complete record, skipped: {_short(body)}"))
                broken_last = True
            pos = end + 1
            continue
        if opener := _OPENER.match(text, start):
            pos = opener.end()
            continue
        try:
            value, stop = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            shown = body if len(body.strip()) > 3 else " ".join(text[first : first + 200].split())
            issues.append(Issue("error", number, "", f"Not valid JSON, skipped: {_short(shown)}"))
            broken_last = True
            pos = end + 1
            continue
        pos = stop
        if not isinstance(value, dict) or not isinstance(value.get("t"), str):
            issues.append(Issue("error", number, "", f'No record type "t", skipped: {_short(text[start:stop])}'))
            continue
        broken_last = False
        if value["t"] == "head" and isinstance(value.get("part"), int):
            part = value["part"]
        value["_line"] = number
        if part:
            value["_part"] = part
        records.append(value)
    return records, issues, broken_last


def parse_reply(text: str) -> ParsedReply:
    """Tolerant by design: fences, prose around records, objects over several lines, a JSON array or
    ``{"records":[…]}``, several parts pasted together. One broken line costs that line."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    records, issues, broken_last = _read_records(text)
    export_id = ""
    parts: list[int] = []
    finished = False
    in_part = 0
    for record in records:
        kind = record["t"]
        if kind == "head":
            export_id = export_id or str(record.get("export") or "")
            if isinstance(record.get("part"), int) and record["part"] not in parts:
                parts.append(record["part"])
            in_part = 0
        elif kind == "end":
            finished = finished or record.get("more") is not True
            claimed = record.get("records")
            if isinstance(claimed, int) and claimed > in_part + 2:  # may or may not count head and end
                issues.append(Issue("warning", record["_line"], "end", f"This part says {claimed} records, but only {in_part} arrived."))
            in_part = 0
        else:
            in_part += 1
    last = records[-1] if records else None
    truncated = broken_last or (last is not None and last["t"] != "end")
    if truncated:
        where = last["_line"] if last else 1
        issues.append(Issue("error", where, _name(last) if last else "", CUT_OFF))
    elif not records and not issues:
        issues.append(Issue("error", 0, "", "No records found in this text."))
    return ParsedReply(records, issues, export_id, parts, finished, truncated)


@dataclass
class Manifest:
    """Which id meant what when an export was made. ``lectures`` holds only that export's lectures."""

    export_id: str
    created: str
    lectures: dict = field(default_factory=dict)  # ref → {"uid", "hash", "documents": [refs of its slides]}
    documents: dict = field(default_factory=dict)  # ref → {"key", "pages", "empty": [pages]}
    recordings: dict = field(default_factory=dict)  # ref → {"uid", "lecture": uid, "lines": [[start, end], …]}
    notes: dict = field(default_factory=dict)  # "N03.7" or "F03.2" → {"lecture": uid, "text"}


def build_manifest(view: SubjectView, lectures: list[LectureView], export_id: str) -> Manifest:
    documents = {
        d.ref: {"key": d.key, "pages": d.page_count, "empty": [p.number for p in d.pages if p.is_empty]} for d in view.documents
    }
    excluded = {d.ref for d in view.documents if d.excluded}
    manifest = Manifest(export_id, datetime.now().isoformat(timespec="seconds"), documents=documents)
    for lecture in lectures:
        slides = [ref for ref in lecture.document_refs if ref.startswith("SL") and ref not in excluded]
        manifest.lectures[lecture.ref] = {"uid": lecture.uid, "hash": lecture.content_hash, "documents": slides}
        for recording in lecture.recordings:
            lines = [[line.start, line.end] for line in recording.lines]
            manifest.recordings[recording.ref] = {"uid": recording.uid, "lecture": lecture.uid, "lines": lines}
        for note in lecture.notes:
            figure = note.figure
            text = " ".join([figure.caption, figure.text, *figure.tables]) if figure else note.text
            manifest.notes[note.ref] = {"lecture": lecture.uid, "text": text}
            if figure:
                manifest.notes[figure.ref] = {"lecture": lecture.uid, "text": text}
    return manifest


def _exports_dir(subject: Subject) -> Path:
    path = subject.study_dir / "exports"
    path.mkdir(exist_ok=True)
    return path


def save_manifest(subject: Subject, manifest: Manifest) -> Path:
    folder = _exports_dir(subject)
    path = folder / f"{manifest.export_id}.json"
    write_json(path, asdict(manifest))
    saved = sorted(folder.glob("*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name))
    for old in saved[:-KEEP_MANIFESTS]:
        old.unlink(missing_ok=True)
    return path


def load_manifest(subject: Subject, export_id: str) -> Manifest | None:
    if not _EXPORT_ID.fullmatch(export_id or "") or export_id.startswith("."):  # it comes from a pasted reply
        return None
    raw = read_json(_exports_dir(subject) / f"{export_id}.json")
    if not raw:
        return None
    return Manifest(**{k: v for k, v in raw.items() if k in Manifest.__dataclass_fields__})


@dataclass
class CheckedReply:
    parsed: ParsedReply
    manifest: Manifest
    records: dict[str, list[dict]]  # by type, usable ones only, in stored form (see below)
    issues: list[Issue]
    uncovered: dict[str, list[dict]]  # lecture ref → [{"page": "SL04.p2", "why": ""}]: pages nothing accounts for
    unknown_pages: dict[str, list[str]]  # lecture ref → what a coverage record lists that does not exist
    counts: dict[str, int]

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]


class _Checker:
    """One pass over a reply. Each ``_check_<type>`` returns the record ready for storing, or None
    after reporting why it cannot be used. Ids become durable sources and uids here, so the
    store never needs the manifest."""

    def __init__(self, parsed: ParsedReply, view: SubjectView, known_ids: dict[str, set[str]] | None):
        self.parsed = parsed
        self.view = view
        self.known = known_ids or {}
        self.issues: list[Issue] = list(parsed.issues)
        self.current = build_manifest(view, list(view.lectures), parsed.export_id)
        found = load_manifest(view.subject, parsed.export_id)
        self.from_export = found is not None
        self.manifest = found or self.current
        # Numbers are never reused, so ids from outside this export still mean the same thing.
        self.documents = {int(re.sub(r"\D", "", ref)): (ref, info) for ref, info in {**self.manifest.documents, **self.current.documents}.items()}
        self.ids: dict[str, set[str]] = {"topics": set(), "objectives": set(), "questions": set(), "attempts": set()}
        self.accounted: set[str] = set(self.known.get("accounted_pages", ()))
        self.unknown_pages: dict[str, list[str]] = {}
        self.covered_lectures: set[str] = set()
        self.topics_in_reply: dict[str, dict] = {}
        self.objectives_in_reply: dict[str, dict] = {}
        self._texts: dict[tuple, str] = {}
        self._warn_about_head()

    def _warn_about_head(self) -> None:
        heads = [r for r in self.parsed.records if r["t"] == "head"]
        line = heads[0]["_line"] if heads else 0
        for head in heads:
            if head.get("schema") not in (None, SCHEMA):
                self.warn(head, f"Expected schema {SCHEMA}, got {head.get('schema')}.")
            if str(head.get("export") or self.parsed.export_id) != self.parsed.export_id:
                self.warn(head, f"Part of another export ({head.get('export')}); read with {self.parsed.export_id}.")
        if not self.from_export and self.parsed.export_id != TUTOR_EXPORT:
            what = f"Unknown export {self.parsed.export_id}" if self.parsed.export_id else "No export id in the reply"
            self.issues.append(Issue("warning", line, "head", f"{what}: ids were read with the subject as it is now."))

    def warn(self, record: dict, message: str) -> None:
        self.issues.append(Issue("warning", record.get("_line", 0), _name(record), message))

    def error(self, record: dict, message: str) -> None:
        self.issues.append(Issue("error", record.get("_line", 0), _name(record), message))
        return None

    # Ids

    def _exists(self, kind: str, wanted: object) -> bool:
        return isinstance(wanted, str) and (wanted in self.ids[kind] or wanted in self.known.get(kind, ()))

    def lecture(self, ref: object) -> tuple[str, dict] | None:
        match = re.fullmatch(r"L0*(\d+)", str(ref or "").strip())
        if not match:
            return None
        canonical = f"L{int(match.group(1)):02d}"
        info = self.manifest.lectures.get(canonical) or self.current.lectures.get(canonical)
        return (canonical, info) if info else None

    def _recording(self, number: str) -> tuple[str, dict] | None:
        ref = f"R{int(number):02d}"
        info = self.manifest.recordings.get(ref) or self.current.recordings.get(ref)
        return (ref, info) if info else None

    def _note(self, label: str) -> dict | None:
        return self.manifest.notes.get(label) or self.current.notes.get(label)

    def line(self, ref: str) -> tuple[str, dict, list[float]] | None:
        """``R07@751`` → (its canonical id, the recording, [start, end]). As exported, else as it is now."""
        match = REF_RE.fullmatch(ref.strip())
        if not match or match.group("at") is None:
            return None
        canonical = f"R{int(match.group('rec')):02d}"
        at = match.group("at")
        decimals = len(at.partition(".")[2])
        for manifest in (self.manifest, self.current):
            info = manifest.recordings.get(canonical)
            for start, end in (info or {}).get("lines", ()):
                if (f"{start:.{decimals}f}" if decimals else str(int(start))) == at:
                    return f"{canonical}@{at}", info, [start, end]
        return None

    def _page_text(self, key: str, page: int) -> str | None:
        document = self.view.document(key)
        shown = document.page(page) if document else None
        if shown is None:
            return None
        return self._cached(("page", key, page), lambda: f"{shown.markdown}\n{shown.raw_text}")

    def _cached(self, key: tuple, text) -> str:
        if key not in self._texts:
            self._texts[key] = normalise(text())
        return self._texts[key]

    def _line_text(self, uid: str, start: float, end: float) -> str | None:
        """The line and one neighbour on each side, as they are now: transcribing again may shift them."""
        found = self.view.recording(uid)
        if found is None:
            return None
        lines = found[1].lines

        def read() -> str:
            hit = [i for i, line in enumerate(lines) if line.start <= end and line.end >= start]
            if not hit:
                return ""
            return " ".join(line.text for line in lines[max(hit[0] - 1, 0) : hit[-1] + 2])

        return self._cached(("line", uid, start, end), read)

    def resolve(self, ref: object, quote: str = "") -> dict | None:
        """A printed id → a durable source, or None if it names nothing. ``verified`` here says the
        quote was found; with no quote there is nothing to disprove, see ``sources``."""
        ref = _RANGE_END.sub("", str(ref or "").strip())
        match = REF_RE.fullmatch(ref)
        if not match:
            return None
        quote = str(quote or "").strip()
        wanted = normalise(quote)
        if match.group("doc") and match.group("page"):
            page = int(match.group("page"))
            known = self.documents.get(int(match.group("docno")))
            if known is None or not 1 <= page <= known[1]["pages"]:
                return None
            text = self._page_text(known[1]["key"], page)
            verified = quote_found(wanted, text) if wanted and text is not None else not wanted
            source = {"kind": "page", "doc": known[1]["key"], "page": page, "label": f"{known[0]}.p{page}", "quote": quote, "verified": verified}
            self.accounted.add(f"{known[1]['key']}.p{page}")
            return source
        if match.group("rec"):
            found = self.line(ref)
            if found is None:
                return None
            label, info, (start, end) = found
            text = self._line_text(info["uid"], start, end)
            verified = quote_found(wanted, text) if wanted and text is not None else not wanted
            return {"kind": "line", "rec": info["uid"], "t": start, "end": end, "label": label, "quote": quote, "verified": verified}
        if match.group("noteno") or match.group("figno"):
            figure = bool(match.group("figno"))
            number, item = (match.group("figno"), match.group("fig")) if figure else (match.group("noteno"), match.group("line"))
            label = f"{'F' if figure else 'N'}{int(number):02d}.{int(item)}"
            info = self._note(label)
            if info is None:
                return None
            text = normalise(info["text"])
            verified = quote_found(wanted, text) if wanted and text else True
            return {"kind": "figure" if figure else "note", "lecture": info["lecture"], "label": label, "quote": quote, "verified": verified}
        if match.group("prof"):
            label = f"PR{int(match.group('prof'))}"
            professor = next((p for p in self.view.professors if p.ref == label), None)
            if professor is None:
                return None
            verified = quote_found(wanted, normalise(professor.profile.to_markdown())) if wanted else True
            return {"kind": "prof", "prof": professor.profile.id, "label": label, "quote": quote, "verified": verified}
        if ref == "KN":
            return {"kind": "own", "label": "KN", "quote": quote, "verified": True}
        return None

    def reference(self, record: dict, ref: object, what: str) -> dict | None:
        """An id with no quote to it: ``style_of``, ``slide``, the two sides of a conflict."""
        if not ref:
            return None
        source = self.resolve(ref)
        if source is None:
            self.warn(record, f"{what}: {ref} does not exist.")
        return source

    def sources(self, record: dict) -> list[dict]:
        items = record.get("src") or []
        out = []
        for item in items if isinstance(items, list) else [items]:
            ref, quote = (item.get("id"), item.get("q")) if isinstance(item, dict) else (item, "")
            source = self.resolve(ref, quote)
            if source is None:
                self.warn(record, f"Source {ref} does not exist, dropped.")
                continue
            if not source["verified"]:
                self.warn(record, f"Quote not found on {source['label']}: “{_short(quote, 60)}”")
            elif source["kind"] in ("page", "line", "note") and not source["quote"]:
                source["verified"] = False
                self.warn(record, f"Source {source['label']} has no quote to check.")
            out.append(source)
        return out

    def _signals(self, record: dict) -> list[str]:
        signals = [str(s) for s in _as_list(record.get("signals"))]
        for signal in signals:
            for token in _REF_IN_TEXT.findall(signal):
                if not _WHOLE_SOURCE.fullmatch(token) and self.resolve(token) is None:
                    self.warn(record, f"Signal cites {token}, which does not exist.")
        return signals

    @staticmethod
    def _well_supported(sources: list[dict]) -> bool:
        return any(s["verified"] and _tier(s) in EVIDENCE_TIERS for s in sources)

    def _importance(self, record: dict) -> int:
        value = record.get("importance")
        try:
            if int(value) in (1, 2, 3):
                return int(value)
        except (TypeError, ValueError):
            pass
        self.warn(record, f"Importance must be 1, 2 or 3, got {value}; using 2.")
        return 2

    def _choice(self, record: dict, key: str, allowed: tuple[str, ...], default: str = "") -> str:
        value = record.get(key)
        if value in allowed:
            return value
        if value not in (None, ""):
            self.warn(record, f"{key} must be one of {', '.join(allowed)}, got {value}.")
        return default

    # Records

    def _check_exam_model(self, record: dict) -> dict | None:
        record["confidence"] = self._choice(record, "confidence", CONFIDENCES, "low")
        weights = record.get("topic_weights")
        for topic in weights if isinstance(weights, dict) else ():
            if not self._exists("topics", topic):
                self.warn(record, f"topic_weights names unknown topic {topic}.")
        record["sources"] = self.sources(record)
        return record

    def _check_topic(self, record: dict) -> dict | None:
        if not record.get("id") or not record.get("title"):
            return self.error(record, "A topic needs an id and a title.")
        lectures = []
        for ref in _as_list(record.get("lectures")):
            found = self.lecture(ref)
            if found:
                lectures.append(found[1]["uid"])
            else:
                self.warn(record, f"Lecture {ref} does not exist, dropped.")
        record.update(
            importance=self._importance(record),
            lectures=lectures,
            signals=self._signals(record),
            misconceptions=[str(m) for m in _as_list(record.get("misconceptions"))],
            sources=self.sources(record),
        )
        self.ids["topics"].add(record["id"])
        return record

    def _objective_lectures(self, record: dict, sources: list[dict]) -> list[str]:
        """Whose objective this is, for practising by lecture. The id says so by convention
        (``L03-o012``); a deck may belong to several lectures, a spoken line only to one."""
        found = [self.lecture(ref) for ref in _as_list(record.get("lectures"))]
        if match := _LECTURE_IN_ID.match(record["id"]):
            found.append(self.lecture(match.group(1)))
        uids = [item[1]["uid"] for item in found if item]
        for source in sources:
            if source["kind"] == "line" and (held := self.view.recording(source["rec"])):
                uids.append(held[0].uid)
        if not uids:
            topic = self.topics_in_reply.get(record["topic"], {})
            uids = list(topic.get("lectures", ()))
        return list(dict.fromkeys(uids))

    def _check_objective(self, record: dict) -> dict | None:
        if not record.get("id") or not record.get("statement"):
            return self.error(record, "An objective needs an id and a statement.")
        if not self._exists("topics", record.get("topic")):
            return self.error(record, f"Topic {record.get('topic')} does not exist.")
        sources = self.sources(record)
        truth = self._choice(record, "truth", TRUTHS)
        verified = self._well_supported(sources)
        if truth == "slides" and not any(s["verified"] and _tier(s) == "SL" for s in sources):
            self.warn(record, 'truth "slides" needs a verified slide source.')
            verified = False
        elif not verified:
            self.warn(record, "No verified source from slides, recordings, past exams or tasks.")
        if truth == "outside" and not any(s["kind"] == "own" for s in sources):
            self.warn(record, 'truth "outside" must carry KN.')
            verified = False
        record.update(
            importance=self._importance(record),
            truth=truth,
            signals=self._signals(record),
            confusable_with=[str(o) for o in _as_list(record.get("confusable_with"))],
            sources=sources,
            lectures=self._objective_lectures(record, sources),
            verified=verified,
        )
        self.ids["objectives"].add(record["id"])
        return record

    def _check_options(self, record: dict) -> list[dict] | None:
        options = []
        for option in _as_list(record.get("options")):
            if not isinstance(option, dict) or not str(option.get("text", "")).strip():
                return self.error(record, "An mc option needs a text.")
            options.append({**option, "correct": option.get("correct") is True, "why_wrong": str(option.get("why_wrong") or "")})
        if len(options) < 2:
            return self.error(record, "An mc question needs at least 2 options.")
        right = sum(o["correct"] for o in options)
        if right != 1:
            return self.error(record, f"An mc question needs exactly one correct option, this one has {right}.")
        for option in options:
            if not option["correct"] and not option["why_wrong"]:
                self.warn(record, f"Wrong option has no why_wrong: {_short(option['text'], 40)}")
        return options

    def _check_question_type(self, record: dict) -> bool:
        kind = record.get("type")
        answer = record.get("answer")
        if kind == "mc":
            options = self._check_options(record)
            if options is None:
                return False
            record["options"] = options
        elif kind == "tf":
            word = str(answer).strip().casefold()
            value = {"true": "True", "wahr": "True", "richtig": "True", "false": "False", "falsch": "False"}.get(word)
            if value is None:
                self.error(record, f'A tf question needs "answer":"True" or "False", got {answer}.')
                return False
            record["answer"] = value
        elif kind == "calc":
            numeric = record.get("numeric")
            usable = isinstance(numeric, dict) and isinstance(numeric.get("value"), (int, float)) and not isinstance(numeric.get("value"), bool)
            if not usable:
                record["numeric"] = None
                if numeric:
                    self.warn(record, "numeric needs a number in value; dropped.")
            if not usable and not answer:
                self.error(record, "A calc question needs numeric or answer.")
                return False
        elif kind in ("short", "case", "card"):
            if not answer:
                self.error(record, f"A {kind} question needs an answer.")
                return False
            if kind != "card" and not record.get("rubric"):
                self.warn(record, f"A {kind} question should have a rubric; it can still be self-graded.")
        else:
            self.error(record, f"Unknown question type {kind}; use {', '.join(QUESTION_TYPES)}.")
            return False
        return True

    def _check_q(self, record: dict) -> dict | None:
        if not str(record.get("stem", "")).strip():
            return self.error(record, "A question needs a stem.")
        if not self._exists("objectives", record.get("objective")):
            return self.error(record, f"Objective {record.get('objective')} does not exist.")
        if not self._check_question_type(record):
            return None
        topic = self.objectives_in_reply.get(record["objective"], {}).get("topic") or str(record.get("topic") or "")
        record["id"] = str(record.get("id") or question_id(topic or record["objective"], record["stem"]))
        sources = self.sources(record)
        verified = self._well_supported(sources)
        if not verified:
            self.warn(record, "No verified source from slides, recordings, past exams or tasks.")
        record.update(
            stem=str(record["stem"]),
            topic=topic,
            difficulty=self._choice(record, "difficulty", DIFFICULTIES),
            bloom=self._choice(record, "bloom", BLOOMS),
            style_of=self.reference(record, record.get("style_of"), "style_of"),
            sources=sources,
            verified=verified,
        )
        self.ids["questions"].add(record["id"])
        return record

    def _check_guide(self, record: dict) -> dict | None:
        if not self._exists("topics", record.get("topic")):
            return self.error(record, f"Topic {record.get('topic')} does not exist.")
        if not str(record.get("md", "")).strip():
            return self.error(record, "A guide needs md.")
        citations = {}
        for match in _INLINE_CITATION.finditer(record["md"]):
            for label in re.split(r"\s*[,;]\s*", match.group(1)):
                source = self.resolve(label)
                if source is None:
                    self.warn(record, f"Citation [{label}] does not exist.")
                else:
                    citations[label] = source
        record["citations"] = citations
        return record

    def _check_said(self, record: dict) -> dict | None:
        lecture = self.lecture(record.get("lecture"))
        if lecture is None:
            return self.error(record, f"Lecture {record.get('lecture')} does not exist.")
        if not str(record.get("text", "")).strip():
            return self.error(record, "A said record needs a text.")
        record.update(
            id=str(record.get("id") or "s_" + hashlib.sha1((lecture[1]["uid"] + normalise(record["text"])).encode()).hexdigest()[:10]),
            lecture=lecture[1]["uid"],
            kind=self._choice(record, "kind", SAID_KINDS, "explanation"),
            slide=self.reference(record, record.get("slide"), "slide"),
            sources=self.sources(record),
        )
        return record

    def _check_conflict(self, record: dict) -> dict | None:
        if not str(record.get("issue", "")).strip():
            return self.error(record, "A conflict needs an issue.")
        record.update(
            id=str(record.get("id") or "c_" + hashlib.sha1(normalise(record["issue"]).encode()).hexdigest()[:10]),
            a=self.reference(record, record.get("a"), "a"),
            b=self.reference(record, record.get("b"), "b"),
            working=self._choice(record, "working", ("a", "b"), "a"),
            needs_student_check=record.get("needs_student_check") is True,
        )
        return record

    def _check_note_fix(self, record: dict) -> dict | None:
        lecture = self.lecture(record.get("lecture"))
        if lecture is None:
            return self.error(record, f"Lecture {record.get('lecture')} does not exist.")
        if not record.get("line"):
            return self.error(record, "A note_fix needs a line.")
        if record.get("verdict") not in VERDICTS:
            return self.error(record, f"verdict must be one of {', '.join(VERDICTS)}.")
        if record["verdict"] != "ok" and not record.get("fix"):
            return self.error(record, "A wrong or incomplete note needs a fix.")
        if self.resolve(record["line"]) is None:
            self.warn(record, f"Note line {record['line']} does not exist.")
        record.update(lecture=lecture[1]["uid"], fix=str(record.get("fix") or ""), sources=self.sources(record))
        return record

    def _check_label(self, record: dict) -> dict | None:
        first = self.line(str(record.get("from") or ""))
        if first is None:
            return self.error(record, f"Line {record.get('from')} does not exist.")
        last = self.line(str(record["to"])) if record.get("to") else first
        if last is None:
            return self.error(record, f"Line {record.get('to')} does not exist.")
        if first[1]["uid"] != last[1]["uid"]:
            return self.error(record, "from and to must be in one recording.")
        category = record.get("category")
        if category is not None and category not in LABEL_CATEGORIES:
            return self.error(record, f"category must be one of {', '.join(LABEL_CATEGORIES)}.")
        if category is None and not isinstance(record.get("exam"), bool):
            return self.error(record, "A label needs a category or exam.")
        spans = [first[2], last[2]]
        record.update(rec=first[1]["uid"], start=min(s[0] for s in spans), end=max(s[1] for s in spans), reason=str(record.get("reason") or "")[:200])
        return record

    def _check_doc_note(self, record: dict) -> dict | None:
        match = re.fullmatch(r"(?:SL|EX|TK|DO)0*(\d+)", str(record.get("doc") or "").strip())
        known = self.documents.get(int(match.group(1))) if match else None
        if known is None:
            return self.error(record, f"Document {record.get('doc')} does not exist.")
        if record.get("issue") not in DOC_ISSUES:
            return self.error(record, f"issue must be one of {', '.join(DOC_ISSUES)}.")
        record.update(ref=known[0], doc=known[1]["key"], detail=str(record.get("detail") or ""))
        return record

    def _check_prof(self, record: dict) -> dict | None:
        professor = next((p for p in self.view.professors if p.ref == _prof_ref(record.get("prof"))), None)
        if professor is None:
            return self.error(record, f"Professor {record.get('prof')} does not exist.")
        if record.get("field") not in PROF_FIELDS:
            return self.error(record, f"field must be one of {', '.join(PROF_FIELDS)}.")
        if not record.get("value"):
            return self.error(record, "A prof record needs a value.")
        record.update(prof=professor.profile.id, sources=self.sources(record))
        return record

    def _expand_cover(self, entry: str) -> tuple[list[tuple[str, str, int]], list[str]]:
        """``SL04.p1-40`` → [(document key, its ref, page), …] and whatever of it does not exist."""
        pages: list[tuple[str, str, int]] = []
        unknown: list[str] = []
        for token in filter(None, (t.strip() for t in str(entry).split(","))):
            if re.fullmatch(r"R\d+(?:@\d+(?:\.\d+)?)?", token):
                if not self._recording(re.sub(r"\D", "", token.partition("@")[0])):
                    unknown.append(token)
                continue
            match = _COVER_RE.fullmatch(token)
            known = self.documents.get(int(match.group(1))) if match else None
            if known is None:
                unknown.append(token)
                continue
            ref, info = known
            low = int(match.group(2) or 1)
            high = int(match.group(3) or match.group(2) or info["pages"])
            pages += [(info["key"], ref, page) for page in range(max(low, 1), min(high, info["pages"]) + 1)]
            if low < 1 or high > info["pages"]:
                unknown.append(token)
        return pages, unknown

    def _check_coverage(self, record: dict) -> dict | None:
        lecture = self.lecture(record.get("lecture"))
        if lecture is None:
            return self.error(record, f"Lecture {record.get('lecture')} does not exist.")
        ref, info = lecture
        covered, unknown = [], []
        for entry in _as_list(record.get("covered")):
            pages, missing = self._expand_cover(entry)
            covered += pages
            unknown += missing
        stated = []
        for item in _as_list(record.get("uncovered")):
            page, why = (item.get("page", ""), item.get("why", "")) if isinstance(item, dict) else (item, "")
            pages, missing = self._expand_cover(page)
            covered += pages
            unknown += missing
            stated += [{"page": f"{r}.p{n}", "why": str(why)} for _, r, n in pages]
        accounted = sorted({f"{key}.p{page}" for key, _, page in covered})
        self.accounted.update(accounted)
        if unknown:
            self.unknown_pages.setdefault(ref, []).extend(unknown)
            self.warn(record, f"Coverage lists what does not exist: {', '.join(unknown[:10])}.")
        self.covered_lectures.add(ref)
        record.update(ref=ref, lecture=info["uid"], hash=info["hash"], accounted=accounted, uncovered=stated)
        return record

    def _check_merge(self, record: dict) -> dict | None:
        into = record.get("into")
        if not self._exists("objectives", into):
            return self.error(record, f"Objective {into} does not exist.")
        sources = []
        for old in _as_list(record.get("from")):
            if self._exists("objectives", old) and old != into:
                sources.append(old)
            else:
                self.warn(record, f"Cannot merge {old} into {into}.")
        if not sources:
            return self.error(record, "Nothing to merge.")
        record["from"] = sources
        return record

    def _check_retire(self, record: dict) -> dict | None:
        wanted = record.get("id")
        if not any(self._exists(kind, wanted) for kind in ("questions", "objectives", "topics")):
            return self.error(record, f"{wanted} does not exist.")
        record["why"] = str(record.get("why") or "")
        return record

    def _check_attempt(self, record: dict) -> dict | None:
        if not self._exists("questions", record.get("q")):
            return self.error(record, f"Question {record.get('q')} does not exist.")
        score = record.get("score")
        correct = record.get("correct")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            if not isinstance(correct, bool):
                return self.error(record, "An attempt needs a score or correct.")
            score = 1.0 if correct else 0.0
        score = max(0.0, min(1.0, float(score)))
        answer = str(record.get("answer") or "")
        at = str(record.get("at") or "")
        fallback = "a_" + hashlib.sha1(f"{record['q']}{answer}{at}".encode()).hexdigest()[:12]
        record.update(
            id=str(record.get("id") or fallback), score=score, correct=correct if isinstance(correct, bool) else score >= 0.5, answer=answer, at=at
        )
        self.ids["attempts"].add(record["id"])
        return record

    def _check_grade(self, record: dict) -> dict | None:
        if not self._exists("attempts", record.get("attempt")):
            return self.error(record, f"Attempt {record.get('attempt')} does not exist.")
        score = record.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            return self.error(record, "A grade needs a score.")
        feedback = str(record.get("feedback") or "")
        grade_id = "g_" + hashlib.sha1(f"{record['attempt']}{score}{feedback}".encode()).hexdigest()[:12]
        record.update(id=grade_id, score=max(0.0, min(1.0, float(score))), feedback=feedback)
        return record

    def _check_tutor_note(self, record: dict) -> dict | None:
        if not record.get("diagnosis") and not record.get("next"):
            return self.error(record, "A tutor_note needs a diagnosis or next.")
        if record.get("topic") and not self._exists("topics", record["topic"]):
            self.warn(record, f"Topic {record['topic']} does not exist.")
        return record

    # The whole reply

    def _uncovered(self) -> dict[str, list[dict]]:
        """Pages of each lecture's slides that nothing accounts for. Without a manifest "each lecture
        of this export" means nothing, so only lectures with a coverage record are held to it."""
        found: dict[str, list[dict]] = {}
        accounted = set(self.accounted)  # a deck of several lectures is asked for once
        for ref, info in self.manifest.lectures.items():
            if not self.from_export and ref not in self.covered_lectures:
                continue
            for doc_ref in info.get("documents", ()):
                document = self.manifest.documents.get(doc_ref)
                if document is None:
                    continue
                shown_ref = self.documents.get(int(re.sub(r"\D", "", doc_ref)), (doc_ref,))[0]
                empty = set(document.get("empty", ()))
                for page in range(1, document["pages"] + 1):
                    key = f"{document['key']}.p{page}"
                    if page not in empty and key not in accounted:
                        accounted.add(key)
                        found.setdefault(ref, []).append({"page": f"{shown_ref}.p{page}", "why": ""})
        return found

    def run(self) -> CheckedReply:
        by_type: dict[str, list[dict]] = {}
        for record in self.parsed.records:
            kind = record["t"]
            if kind in ("head", "end"):
                continue
            if kind in RECORD_ORDER:
                by_type.setdefault(kind, []).append(dict(record))
            else:
                self.error(record, f"Unknown record type {kind}.")
        records: dict[str, list[dict]] = {}
        for kind in RECORD_ORDER:  # so it does not matter in which order Claude wrote them
            for record in by_type.get(kind, ()):
                try:
                    checked = getattr(self, f"_check_{kind}")(record)
                except (AttributeError, KeyError, TypeError, ValueError):  # a list where a string belongs, and so on
                    checked = self.error(record, "A field has the wrong type.")
                if checked is None:
                    continue
                checked.pop("src", None)
                records.setdefault(kind, []).append(checked)
                if kind == "topic":
                    self.topics_in_reply[checked["id"]] = checked
                elif kind == "objective":
                    self.objectives_in_reply[checked["id"]] = checked
        self.issues.sort(key=lambda issue: issue.line)
        counts = {kind: len(items) for kind, items in records.items()}
        return CheckedReply(self.parsed, self.manifest, records, self.issues, self._uncovered(), self.unknown_pages, counts)


def _as_list(value: object) -> list:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _prof_ref(value: object) -> str:
    match = re.fullmatch(r"PR0*(\d+)", str(value or "").strip())
    return f"PR{match.group(1)}" if match else ""


def _tier(source: dict) -> str:
    """SL > R > EX/TK > N/F > PR > KN."""
    if source["kind"] == "page":
        return source["label"][:2]
    return {"line": "R", "note": "N", "figure": "F", "prof": "PR", "own": "KN"}.get(source["kind"], "")


def check_reply(parsed: ParsedReply, view: SubjectView, known_ids: dict[str, set[str]] | None = None) -> CheckedReply:
    """Everything that can be used, in stored form, and all that is wrong with the rest.
    ``known_ids`` (``StudyStore.known_ids``): replies are deltas, they may point at earlier imports."""
    return _Checker(parsed, view, known_ids).run()


def _part_of(parsed: ParsedReply, line: int) -> int:
    heads = [r for r in parsed.records if r["t"] == "head" and isinstance(r.get("part"), int)]
    return next((r["part"] for r in reversed(heads) if r["_line"] <= line), 0)


def _where(parsed: ParsedReply, issue: Issue) -> str:
    part = _part_of(parsed, issue.line)
    bits = ([f"part {part}"] if part else []) + ([f"line {issue.line}"] if issue.line else []) + ([issue.record] if issue.record else [])
    return ", ".join(bits) or "reply"


def _head_example(checked: CheckedReply) -> str:
    return json.dumps({"t": "head", "schema": SCHEMA, "export": checked.manifest.export_id or checked.parsed.export_id, "part": 1})


def repair_prompt(checked: CheckedReply) -> str:
    """What to send back to Claude; empty when there is nothing to fix."""
    parsed = checked.parsed
    problems = [i for i in checked.errors if i.message != CUT_OFF]
    warnings = [i for i in checked.warnings if i.record != "head"]  # about this app's state, nothing Claude can fix
    lines = []
    if problems or warnings:
        lines.append("UniVerse checked your reply. Resend ONLY these records, corrected, as JSON Lines in the same format, one record per line. The other records are fine.")
        lines += ["", *(f"- {_where(parsed, i)}: {i.message}" for i in problems)]
        lines += [f"- {_where(parsed, i)} (kept for now): {i.message}" for i in warnings[:REPAIR_WARNINGS]]
        if len(warnings) > REPAIR_WARNINGS:
            lines.append(f"- … and {len(warnings) - REPAIR_WARNINGS} more warnings of these kinds.")
    if parsed.truncated:
        records = [r for r in parsed.records if r["t"] != "head"]
        if lines:
            lines.append("")
        if records:
            last = records[-1]
            part = last.get("_part")
            where = f"part {part}, line {last['_line']}" if part else f"line {last['_line']}"
            lines.append(f"Your reply was cut off. The last complete record was {_name(last)} ({where}). Continue from the next record, and do not repeat earlier ones.")
        else:
            lines.append("Your reply was cut off before any complete record. Start again from the beginning.")
    if not lines:
        return ""
    lines += ["", f"Start with {_head_example(checked)} and end with an end record."]
    return "\n".join(lines)


def follow_up_prompt(checked: CheckedReply) -> str:
    """Asks for exactly the pages nothing accounted for; empty when there are none."""
    if not checked.uncovered:
        return ""
    lines = [
        "Your reply does not account for these slide pages. Please read them now and write what is missing: "
        "objective, q, said and conflict records that cite them, or, if a page holds nothing for the exam, a coverage "
        "record listing it under uncovered with the reason.",
        "",
    ]
    lines += [f"- {ref}: {', '.join(item['page'] for item in items)}" for ref, items in checked.uncovered.items()]
    lines += [
        "",
        "Only these pages, in the same format: JSON Lines, one record per line, reusing the ids you already gave. "
        f"Start with {_head_example(checked)} and end with an end record.",
    ]
    return "\n".join(lines)


def _read_replies(path: Path) -> tuple[str, list[tuple[int, str]]]:
    """A file, or a folder of parts: the text, and on which line each file starts."""
    files = sorted(p for p in path.iterdir() if p.suffix.lower() in (".jsonl", ".json", ".txt", ".md")) if path.is_dir() else [path]
    text = ""
    starts = []
    for file in files:
        starts.append((text.count("\n") + 1, file.name))
        text += file.read_text(encoding="utf-8").rstrip("\n") + "\n"
    return text, starts


def main(argv: list[str] | None = None) -> int:
    """Prints what is wrong with a reply, exit 1 on errors. For Claude Code, which fixes until clean."""
    parser = argparse.ArgumentParser(prog="python -m universe.services.claude_reply", description="Check a reply from Claude before importing it.")
    parser.add_argument("path", type=Path, help="a reply file, or a folder of part-NN.jsonl files")
    parser.add_argument("--subject", required=True, help="the subject's name")
    args = parser.parse_args(argv)
    from .study_store import StudyStore  # it imports this module

    subject = Workspace().subject(args.subject)
    if subject is None:
        print(f"No subject called {args.subject}.", file=sys.stderr)
        return 2
    if not args.path.exists():
        print(f"Nothing at {args.path}.", file=sys.stderr)
        return 2
    text, starts = _read_replies(args.path)
    checked = check_reply(parse_reply(text), load_subject(subject), StudyStore(subject).known_ids())

    def where(line: int) -> str:
        first, name = next(((f, n) for f, n in reversed(starts) if f <= line), (1, starts[0][1] if starts else ""))
        return f"{name}:{line - first + 1}"

    for issue in checked.issues:
        print(f"{issue.level.upper()} {where(issue.line)} {issue.record}: {issue.message}")
    print("Records: " + (", ".join(f"{n} {kind}" for kind, n in checked.counts.items()) or "none"))
    for ref, items in checked.uncovered.items():
        print(f"Not accounted for in {ref}: {', '.join(item['page'] for item in items)}")
    print(f"{len(checked.errors)} errors, {len(checked.warnings)} warnings; " + ("complete." if checked.parsed.finished else "not finished: no end record with more=false."))
    return 1 if checked.errors else 0


if __name__ == "__main__":
    sys.exit(main())
