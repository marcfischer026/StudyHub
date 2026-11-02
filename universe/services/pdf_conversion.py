"""PDFs → Markdown by Gemini or Copilot → back into the hub.

The slides are the professor's word, and plain text extraction loses much of what they
show: charts, T-accounts, formulas, table layout. So every PDF of a subject can go out to
an AI tool that reads the pages and come back as Markdown, which Claude then gets instead.

Each exported copy carries a small stamp on every page, ``[[SL04 p.12]]``, and the tool is
told to start each page's Markdown with it. The hub never has to guess what came back:
replies may be pasted in any order, cover part of a deck or several decks at once. A check
compares each page with the PDF's own text; a page that fails keeps the raw text beside it
in the export, so nothing a tool dropped is lost.

Conversions are plain files, ``<Subject>/converted/<pdf name>.<document key>.md``, and may
be edited by hand. The originals are never touched.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pymupdf

from ..storage import Subject, atomic_write_text
from . import pdf_tools

if TYPE_CHECKING:
    from .subject_model import DocumentView, SubjectView

PART_PAGES = 30  # a chat tool answers a part this long in one go; a whole 108-page deck gets cut off
EXPORT_FOLDER = "PDFs to convert"
PROMPT_FILE = "PROMPT for Gemini or Copilot.md"

PROMPT = """Convert the attached PDF into Markdown for a student's study archive. It is lecture material \
from their professor. An AI tutor will later treat your Markdown as the exact content of these pages, \
so completeness and accuracy matter more than anything else.

1. Every page has a small stamp in its top right corner, like [[SL04 p.12]]. Start each page's Markdown \
with that stamp, copied exactly, on its own line. Go through the pages in order and skip none; a page \
with nothing on it is just its stamp.
2. Copy all text word for word, in its original language. Do not summarise, shorten, translate, correct \
or explain, and add nothing of your own.
3. Keep the structure: headings, nested bullet and numbered lists, bold where the page uses it.
4. Tables become Markdown tables with every row and column. T-accounts and balance sheets become tables \
with every amount. Booking entries stay exactly as written.
5. Formulas go in LaTeX, $...$ or $$...$$. Every number, unit and sign must be exact.
6. For a chart, diagram, flowchart or image that carries information, add a line starting with \
[Figure: and say what it shows: its type, axes, labels, values, the boxes and which arrows connect them. \
Skip logos and decoration. Never mix such a description into the page's own text.
7. Leave out only what repeats on every page, such as a logo, footer or page number.
8. Reply with the Markdown only: no introduction, no comments, no code fence.
9. If you cannot finish all pages in this reply, stop after a complete page and put this on a last line, \
with the next page's stamp: [[CONTINUE AT SL04 p.31]]. I will ask you to go on.
"""

FIX_PROMPT = "Please also convert the missing pages, following the same rules, starting each page with its stamp:\n"

_REF = r"(SL|EX|TK|DO)\s*0*(\d+)\s*[,·:\-–—]?\s*(?:p|s|page|seite|folie|slide)\s*\.?\s*(\d+)"
_MARKER = re.compile(
    rf"<!--\s*{_REF}\s*-->|\\?\[(?:\\?\[)?\s*{_REF}\s*\\?\](?:\\?\])?",
    re.IGNORECASE,
)
_CONTINUE = re.compile(r"\\?\[(?:\\?\[)?\s*CONTINUE[^\]\n]*\\?\](?:\\?\])?\s*$", re.IGNORECASE | re.MULTILINE)
_FENCE = re.compile(r"^\s*(?:```|~~~)[\w-]*\s*$")
_FENCE_JOIN = re.compile(r"^\s*(?:```|~~~)\s*\n\s*(?:```|~~~)[\w-]*\s*$\n?", re.MULTILINE)  # two fenced parts pasted as one
_DECORATION = " \t*_#>`"
_FIGURE_LINE = re.compile(r"^\W*\[?\s*(?:Figure|Abbildung|Grafik)\s*:.*$", re.IGNORECASE | re.MULTILINE)
_IMAGE_LINK = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# Math fonts without a proper Unicode map come out as Syriac to Sinhala letters or private-use
# characters: no words to compare. U+F0xx is left out: that is where Wingdings bullets land.
GARBLED = re.compile("[\u0700-\u0dff\u1d00-\u1dbf\ue000-\uefff\uf100-\uf8ff]")
_LATEX_COMMAND = re.compile(r"\\[a-zA-Z]+")
_WORD = re.compile(r"[^\W\d_]{3,}")
_NUMBER = re.compile(r"[0-9]+(?:[.,][0-9]+)*")  # not \d: garbled math fonts produce Tamil and Malayalam digits

MIN_COVERAGE = 0.7  # below this share of the PDF's words, the conversion lost something
MAX_ADDED = 0.3  # above this share of words the PDF does not have, the tool made things up
PREAMBLE_NOTICE = 200  # text before the first stamp longer than this is worth mentioning


def stamp(ref: str, page: int) -> str:
    return f"[[{ref} p.{page}]]"


# --- checking a page against the PDF -----------------------------------------


def _words(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(\w)[-\u00ad]\s*\n\s*(\w)", r"\1\2", text)  # words the PDF hyphenated across lines
    return {w.casefold() for w in _WORD.findall(text) if not GARBLED.search(w)}


def _numbers(text: str) -> set[str]:
    return {re.sub(r"[^0-9]", "", n) for n in _NUMBER.findall(GARBLED.sub(" ", unicodedata.normalize("NFKC", text)))}


@dataclass(frozen=True)
class PageCheck:
    """How a page's Markdown compares with the text inside the PDF."""

    coverage: float = 1.0  # share of the PDF's words found in the Markdown
    added: float = 0.0  # share of the Markdown's words the PDF does not have
    missing_numbers: tuple[str, ...] = ()
    scanned: bool = False  # no text in the PDF to compare with

    @property
    def ok(self) -> bool:
        return self.coverage >= MIN_COVERAGE and self.added <= MAX_ADDED and not self.missing_numbers

    def describe(self) -> str:
        if self.scanned:
            return "a scan: could not be compared with the PDF"
        problems = []
        if self.coverage < MIN_COVERAGE:
            problems.append(f"only {self.coverage:.0%} of the PDF's words are there")
        if self.missing_numbers:
            problems.append("numbers missing: " + ", ".join(self.missing_numbers[:6]))
        if self.added > MAX_ADDED:
            problems.append(f"{self.added:.0%} of the words are not in the PDF")
        return "; ".join(problems) or "matches the PDF"


def check_page(raw_text: str, markdown: str, page: int = 0, common_numbers: frozenset[str] = frozenset(), printed_offset: int = 0) -> PageCheck:
    """``common_numbers`` repeat through the document (dates in footers), which the tool may drop,
    as it may the page's own number, also as printed on the slide (``printed_offset``)."""
    raw_words = _words(raw_text)
    if not raw_words and not _numbers(raw_text):
        return PageCheck(scanned=True)
    own_text = _LATEX_COMMAND.sub(" ", _FIGURE_LINE.sub("", _IMAGE_LINK.sub("", markdown)))
    words = _words(own_text)
    coverage = len(raw_words & _words(markdown)) / len(raw_words) if raw_words else 1.0
    added = len(words - raw_words) / len(words) if words else 0.0
    ignored = common_numbers | {str(page), str(page + printed_offset)}
    missing = sorted((_numbers(raw_text) - _numbers(markdown)) - ignored, key=lambda n: (len(n), n))
    return PageCheck(coverage, added, tuple(missing))


def common_numbers(raw_texts: list[str]) -> frozenset[str]:
    """Numbers on more than half of a document's pages."""
    if len(raw_texts) < 4:
        return frozenset()
    counts: dict[str, int] = {}
    for text in raw_texts:
        for number in _numbers(text):
            counts[number] = counts.get(number, 0) + 1
    return frozenset(n for n, count in counts.items() if count > len(raw_texts) / 2)


def printed_page_offset(raw_texts: list[str]) -> int:
    """How far the numbers printed on the slides run ahead of the PDF's pages: a title slide often goes uncounted."""
    found = [_numbers(text) for text in raw_texts]
    hits = {offset: sum(str(page + offset) in numbers for page, numbers in enumerate(found, start=1)) for offset in range(-3, 301)}  # a later block of one lecture starts at slide 24
    best = max(hits, key=lambda offset: hits[offset])
    return best if hits[best] >= 3 and hits[best] > len(found) / 3 else 0


# --- parsing what came back --------------------------------------------------


def _unfence(text: str) -> str:
    lines = text.strip().splitlines()
    if lines and _FENCE.match(lines[0]):
        lines = lines[1:]
    if lines and _FENCE.match(lines[-1]):
        lines = lines[:-1]
    return "\n".join(lines)


def parse_converted(text: str) -> tuple[list[tuple[int, int, str]], str]:
    """``([(document number, page, markdown), …], text before the first stamp)``. A page
    stamped twice is kept twice, in order; the last one wins when saved."""
    text = _CONTINUE.sub("", _FENCE_JOIN.sub("", _unfence(text)))
    # At the start of a line, after decoration: "see [[SL01 p.2]]" in running text is no page boundary.
    matches = [m for m in _MARKER.finditer(text) if not text[text.rfind("\n", 0, m.start()) + 1 : m.start()].strip(_DECORATION + "\\")]
    pages = []
    for i, match in enumerate(matches):
        groups = match.groups()
        number, page = (groups[1], groups[2]) if groups[0] else (groups[4], groups[5])
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        head, sep, tail = text[match.end() : end].partition("\n")
        # On its own line the stamp takes its closing ** along; text after it keeps its opening **.
        body = (tail if not head.strip(_DECORATION + "\\") else head.lstrip(" \t") + sep + tail).rstrip()
        if i + 1 < len(matches):  # "## [[SL04 p.2]]": the decoration before a stamp is not page text
            lines = body.split("\n")
            lines[-1] = lines[-1].rstrip(_DECORATION)
            body = "\n".join(lines).rstrip()
        pages.append((int(number), int(page), body.strip("\n")))
    preamble = text[: matches[0].start()].strip(_DECORATION + "\n") if matches else text.strip()
    return pages, preamble


# --- stored conversions ------------------------------------------------------


FILE_STEM_CHARS = 120  # a file name may have 255 bytes, an umlaut takes two, and ids and parts come on top


def _file_stem(name: str) -> str:
    """Readable and safe; macOS hands out decomposed umlauts, which would turn into dashes."""
    return re.sub(r"[^\w .()-]+", "-", unicodedata.normalize("NFC", name)).strip()[:FILE_STEM_CHARS].strip()


def converted_path(subject: Subject, key: str) -> Path | None:
    return next(iter(sorted(subject.converted_dir.glob(f"*.{key}.md"))), None)


def load_pages(subject: Subject, key: str) -> dict[int, str]:
    """``{page: markdown}`` of a document's conversion."""
    path = converted_path(subject, key)
    if path is None:
        return {}
    try:
        pages, _ = parse_converted(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # load_subject reports it
        return {}
    return {page: body for _, page, body in pages}


def save_pages(subject: Subject, key: str, title: str, ref: str, pages: dict[int, str]) -> Path:
    path = converted_path(subject, key)
    if path is None:
        name = _file_stem(Path(title).stem)
        path = subject.converted_dir / f"{name}.{key}.md"
    head = f"<!-- Conversion of {title}. Edit freely, but keep the page stamps. -->\n\n"
    atomic_write_text(path, head + "\n\n".join(f"{stamp(ref, n)}\n\n{pages[n]}".rstrip() for n in sorted(pages)) + "\n")
    return path


# --- out: stamped copies for the tool ----------------------------------------


@dataclass
class PdfExport:
    folder: Path
    files: list[Path] = field(default_factory=list)
    already_converted: int = 0  # documents left out because they are complete
    problems: list[str] = field(default_factory=list)  # documents left out because they cannot be read


def _stamp_page(page: pymupdf.Page, text: str) -> None:
    """Top right, in the corner as the page is shown, whatever its rotation: outside the content."""
    width = pymupdf.get_text_length(text, fontname="helv", fontsize=7)
    point = pymupdf.Point(page.rect.width - 6 - width, 9) * page.derotation_matrix
    page.insert_text(point, text, fontname="helv", fontsize=7, color=(0.45, 0.45, 0.45), rotate=page.rotation)


def _ranges(pages: list[int], limit: int) -> list[tuple[int, int]]:
    """Runs of consecutive pages, none longer than ``limit`` (0: no limit)."""
    runs: list[tuple[int, int]] = []
    for page in pages:
        if runs and page == runs[-1][1] + 1 and (not limit or page - runs[-1][0] < limit):
            runs[-1] = (runs[-1][0], page)
        else:
            runs.append((page, page))
    return runs


def _write_copies(document: "DocumentView", pages: list[int], folder: Path, part_pages: int) -> list[Path]:
    files = []
    runs = _ranges(pages, part_pages)
    stem = _file_stem(document.path.stem)
    with pymupdf.open(document.path) as source:
        for first, last in runs:
            whole = (first, last) == (1, source.page_count)
            target = folder / (f"{document.ref} {stem}.pdf" if whole else f"{document.ref} {stem} (p{first}-{last}).pdf")
            copy = pymupdf.open()
            copy.insert_pdf(source, from_page=first - 1, to_page=last - 1, annots=False)
            for offset, page in enumerate(copy):
                _stamp_page(page, stamp(document.ref, first + offset))
            copy.save(target, garbage=3, deflate=True)
            copy.close()
            files.append(target)
    return files


def export_pdfs(view: "SubjectView", only_missing: bool = True, part_pages: int = PART_PAGES) -> PdfExport:
    """Stamped copies of every document of the subject, in a folder of their own, with the prompt
    next to them. By default only the pages that still need converting; big decks go out in parts."""
    folder = view.subject.claude_dir / EXPORT_FOLDER
    folder.mkdir(exist_ok=True)
    for old in folder.iterdir():  # this folder belongs to the hub: last time's copies are stale
        if old.is_file() and old.suffix.lower() in (".pdf", ".md"):
            old.unlink()
    result = PdfExport(folder)
    for document in view.documents:
        if document.excluded:
            continue
        if not document.page_count:
            result.problems.append(f"{document.path.name} could not be read.")
            continue
        done = load_pages(view.subject, document.key) if only_missing else {}
        pages = [n for n in range(1, document.page_count + 1) if n not in done]
        if not pages:
            result.already_converted += 1
            continue
        try:
            result.files += _write_copies(document, pages, folder, part_pages)
        except Exception as exc:  # one bad file must not stop the others
            result.problems.append(f"{document.path.name} could not be written: {exc}")
    atomic_write_text(folder / PROMPT_FILE, PROMPT)
    return result


# --- in: importing what came back --------------------------------------------


@dataclass
class DocumentReport:
    ref: str
    title: str
    imported: list[int] = field(default_factory=list)
    missing: list[int] = field(default_factory=list)  # pages still without Markdown
    weak: list[tuple[int, str]] = field(default_factory=list)  # (page, what the check found)


@dataclass
class ConversionReport:
    documents: list[DocumentReport] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def imported_pages(self) -> int:
        return sum(len(d.imported) for d in self.documents)

    def merge(self, report: "ConversionReport") -> None:
        """Adds a later import; a page imported again counts once, with its latest check."""
        self.problems += [problem for problem in report.problems if problem not in self.problems]
        for new in report.documents:
            known = next((d for d in self.documents if d.ref == new.ref), None)
            if known is None:
                self.documents.append(new)
                continue
            redone = set(new.imported)
            known.imported = sorted(set(known.imported) | redone)
            known.weak = [item for item in known.weak if item[0] not in redone] + new.weak
            known.missing = new.missing

    def summary(self) -> str:
        if not self.documents and not self.problems:
            return "Nothing to import."
        lines = list(self.problems)
        for doc in self.documents:
            line = f"{doc.ref} {doc.title}: {len(doc.imported)} page(s) imported"
            line += f", {page_list(doc.missing)} still missing" if doc.missing else ", complete"
            lines.append(line)
            lines += [f"   page {page}: {what}" for page, what in doc.weak[:8]]
            if len(doc.weak) > 8:
                lines.append(f"   and {len(doc.weak) - 8} more pages to check")
        return "\n".join(lines)

    def fix_request(self) -> str:
        """What to ask the tool next, for the pages still missing."""
        wanted = [f"{doc.ref} pages {page_list(doc.missing)}" for doc in self.documents if doc.missing]
        return FIX_PROMPT + "\n".join(wanted) if wanted else ""


def page_list(pages: list[int]) -> str:
    """``[1, 2, 3, 7]`` → ``"p.1-3, 7"``."""
    return "p." + ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in _ranges(sorted(pages), 0))


def import_converted(view: "SubjectView", text: str) -> ConversionReport:
    """Merge one tool reply into the stored conversions, page by page."""
    report = ConversionReport()
    pages, preamble = parse_converted(text)
    if not pages:
        report.problems.append(
            "No page stamps found, so the hub cannot tell which pages this is. "
            "Ask the tool to start each page with the stamp from its corner, like [[SL04 p.12]]."
        )
        return report
    if len(preamble) > PREAMBLE_NOTICE:
        report.problems.append(f"Ignored {len(preamble):,} characters before the first page stamp.")
    by_number = {d.number: d for d in view.documents}
    received: dict[int, dict[int, str]] = {}
    for number, page, body in pages:
        document = by_number.get(number)
        if document is None:
            problem = f"No document number {number} in {view.name}: is this reply from another subject?"
            if problem not in report.problems:
                report.problems.append(problem)
        elif not 1 <= page <= document.page_count:
            report.problems.append(f"{document.ref} has {document.page_count} pages, but a stamp says page {page}.")
        else:
            received.setdefault(number, {})[page] = body
    for number, new_pages in received.items():
        document = by_number[number]
        stored = load_pages(view.subject, document.key) | new_pages
        save_pages(view.subject, document.key, document.title, document.ref, stored)
        raw = {p.number: p.raw_text for p in document.pages} or dict(enumerate(pdf_tools.page_texts(document.path), start=1))
        common, offset = common_numbers(list(raw.values())), printed_page_offset([raw.get(n, "") for n in range(1, document.page_count + 1)])
        doc_report = DocumentReport(document.ref, document.title, sorted(new_pages))
        doc_report.missing = [n for n in range(1, document.page_count + 1) if n not in stored]
        for page in sorted(new_pages):
            result = check_page(raw.get(page, ""), new_pages[page], page, common, offset)
            if not result.ok:
                doc_report.weak.append((page, result.describe()))
        report.documents.append(doc_report)
    return report


def import_files(view: "SubjectView", paths: list[Path]) -> ConversionReport:
    """Import Markdown files; the stamps inside say what they are, not their names."""
    combined = ConversionReport()
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            combined.problems.append(f"{path.name}: could not read it ({exc}).")
            continue
        report = import_converted(view, text)
        combined.documents += report.documents
        combined.problems += [f"{path.name}: {problem}" for problem in report.problems]
    return combined
