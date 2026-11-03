"""PDFs → Markdown on this Mac, without a chat tool.

Two document converters run side by side and each page keeps the better result. Marker reads
every page: it keeps the PDF's own text, rebuilds tables and saves figures. Pages with
formulas or tables, and pages where Marker lost what the PDF says, are also read by MinerU's
vision model, which sees details such as
overlined symbols that Marker turns into fractions, but which re-reads the words from the
picture and makes typos where Marker has none. Both versions of such a page are scored against
what the PDF says, and the higher score wins; Marker on a tie.

What comes back is stamped like a chat tool's reply and goes through
:func:`pdf_conversion.import_converted`, so storage, checks and the export to Claude stay the
same. Figures are saved under ``converted/figures`` and linked from the Markdown, so the
student can show Claude the picture itself.

The converters need PyTorch, which does not run on the app's Python, so each lives in an
environment of its own under ``<data root>/converters`` (see ``tools/install_converters.sh``)
and runs as a separate process. Nothing leaves this Mac once their models are downloaded.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import pymupdf

try:
    import psutil
except ImportError:  # optional: helper servers are then left to exit on their own
    psutil = None

from .. import config
from . import pdf_conversion
from .jobcontrol import JobControl

if TYPE_CHECKING:
    from .subject_model import DocumentView, SubjectView

Progress = Callable[["Status"], None]

MATH, TABLE, TEXT, SCAN = "math", "table", "text", "scan"
MINERU, MARKER = "mineru", "marker"
MISSING, WEAK, EVERY = "missing", "weak", "every"  # which pages convert_subject takes on
SECOND_OPINION = (MATH, TABLE)  # page kinds MinerU always reads too
CHUNK_PAGES = 40  # one converter run: loading the models costs a minute, a stop loses at most this
MINERU_MARGIN = 0.02  # MinerU must beat Marker by this much; small differences are noise
LOG_TAIL = 1500
POLL_SECONDS = 3  # how often a running converter's log is read for progress

_MATH_FONT = re.compile(r"math|cmmi|cmsy|cmex|symbol|stix|mt ?extra|euler|msbm|msam", re.IGNORECASE)
_MATH_CHAR = re.compile(r"[∑∏∫√∞≤≥≠≈∂∇∈∉⊂⊆∪∩⇒⇔±÷αβγδεθμσφωΔΣΩ²³ⁿ₀-₉]")
_MARKER_PAGE = re.compile(r"^\{(\d+)\}-{10,}\s*$", re.MULTILINE)
_IMAGE_LINK = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_SPACED_TEXT = re.compile(r"\\text\s*\{((?:\S ){2,}\S)\s*\}")  # MinerU's "\text {G a u ß}"
_TQDM = re.compile(r"Predict:\s*\d+%\|[^|]*\|\s*(\d+)/(\d+)")  # MinerU's progress bars, one per pass


class ConverterError(RuntimeError):
    pass


def converters_dir() -> Path:
    return config.data_root() / "converters"


def stop_helpers() -> int:
    """Marker starts model servers in process groups of their own that outlive it. Ends every
    process still running from the converters folder; returns how many."""
    if psutil is None:
        return 0
    folder, stopped = str(converters_dir()), []
    for proc in psutil.process_iter(["cmdline"]):
        if any(folder in part for part in proc.info["cmdline"] or ()):
            try:
                proc.terminate()
                stopped.append(proc)
            except psutil.Error:
                pass
    _gone, alive = psutil.wait_procs(stopped, timeout=5)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    return len(stopped)


def executable(engine: str) -> Path:
    return converters_dir() / engine / "bin" / ("mineru" if engine == MINERU else "marker_single")


def installed() -> bool:
    """Marker is needed; MinerU only adds its second opinion."""
    return executable(MARKER).exists()


def _environment() -> dict[str, str]:
    root = converters_dir()
    env = dict(os.environ)
    env.update(
        HF_HOME=str(root / "hf"),
        MINERU_MODEL_SOURCE="huggingface",
        MINERU_TOOLS_CONFIG_JSON=str(root / "mineru.json"),
        PYTORCH_ENABLE_MPS_FALLBACK="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        DO_NOT_TRACK="1",
        TOKENIZERS_PARALLELISM="false",
    )
    return env


# --- which page goes where ---------------------------------------------------


def classify(page: pymupdf.Page) -> str:
    text = page.get_text()
    if len(text.strip()) < 20 and page.get_images():
        return SCAN
    spans = [s for b in page.get_text("dict")["blocks"] for line in b.get("lines", ()) for s in line["spans"] if s["text"].strip()]
    chars = sum(len(s["text"]) for s in spans) or 1
    math_font = sum(len(s["text"]) for s in spans if _MATH_FONT.search(s["font"])) / chars
    garbled = len(pdf_conversion.GARBLED.findall(text)) / chars
    if garbled > 0.01 or math_font > 0.04 or len(_MATH_CHAR.findall(text)) / chars > 0.02:
        return MATH
    try:
        if page.find_tables().tables:
            return TABLE
    except Exception:  # table detection is a heuristic; a page it chokes on is plain text
        pass
    return TEXT


# --- choosing between two versions of a page ---------------------------------


def _latex_broken(markdown: str) -> bool:
    math = re.sub(r"\\\$", "", markdown)
    return math.count("$") % 2 == 1 or math.count("{") != math.count("}") or "\\begin{" in math and math.count("\\begin{") != math.count("\\end{")


def score(raw_text: str, markdown: str, page: int, common: frozenset[str] = frozenset(), offset: int = 0) -> float:
    """How well a page's Markdown matches what the PDF says, higher is better. The PDF's text
    knows the words and numbers even where its math is garbled, so typos and dropped lines show."""
    if not markdown.strip():
        return -1.0
    check = pdf_conversion.check_page(raw_text, markdown, page, common, offset)
    value = 0.0 if check.scanned else check.coverage - check.added - 0.02 * len(check.missing_numbers)
    value -= 0.05 * len(pdf_conversion.GARBLED.findall(markdown))  # the broken text layer leaked through
    value -= 0.1 * markdown.count("\\atop")
    value -= 0.2 * _latex_broken(markdown)
    return value


def pick(raw_text: str, versions: dict[str, str], page: int, common: frozenset[str] = frozenset(), offset: int = 0) -> tuple[str, str]:
    """``(engine, markdown)`` of the best version; Marker unless MinerU is clearly better."""
    scores = {engine: score(raw_text, md, page, common, offset) for engine, md in versions.items()}
    best = MARKER if MARKER in versions else next(iter(versions))
    if MINERU in versions and best == MARKER and scores[MINERU] > scores[MARKER] + MINERU_MARGIN:
        best = MINERU
    return best, versions[best]


# --- running a converter -----------------------------------------------------


def _run(command: list[str], log: Path, control: JobControl | None, watch: Callable[[str], None] | None = None) -> None:
    """``watch`` sees the end of the log every few seconds while the converter runs."""
    with log.open("w", encoding="utf-8", errors="replace") as out:
        proc = subprocess.Popen(command, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=_environment(), cwd=log.parent)
        if control is not None:
            control.register_process(proc)
        try:
            while True:
                try:
                    code = proc.wait(timeout=POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    if watch is not None:
                        watch(_log_tail(log))
        finally:
            if control is not None:
                control.unregister_process(proc)
    if control is not None:
        control.checkpoint()  # a stop shows as Cancelled, not as a failed converter
    if code:
        tail = log.read_text(encoding="utf-8", errors="replace")[-LOG_TAIL:]
        raise ConverterError(f"{Path(command[0]).name} stopped with code {code}:\n{tail}")


def _log_tail(log: Path) -> str:
    with log.open("rb") as f:
        f.seek(max(0, log.stat().st_size - LOG_TAIL))
        return f.read().decode("utf-8", errors="replace")


def mineru_progress(log_tail: str) -> tuple[int, int] | None:
    """``(done, total)`` of MinerU's latest progress bar: first over the pages, then over their blocks."""
    bars = _TQDM.findall(log_tail)
    return tuple(map(int, bars[-1])) if bars else None


def _subset(pages: list[tuple[pymupdf.Document, int]], target: Path) -> None:
    """One PDF of the given pages, which may come from several documents."""
    copy = pymupdf.open()
    for source, number in pages:
        copy.insert_pdf(source, from_page=number - 1, to_page=number - 1, annots=False)
    copy.save(target, garbage=3, deflate=True)
    copy.close()


@dataclass
class _Page:
    markdown: str
    images: dict[str, Path]  # link in the Markdown → the file the converter wrote


def parse_marker(markdown: str, folder: Path) -> dict[int, _Page]:
    """Marker's ``--paginate_output``: each page after ``{0}-----…``, counted from 0."""
    pages: dict[int, _Page] = {}
    parts = _MARKER_PAGE.split(markdown)
    for index, body in zip(parts[1::2], parts[2::2]):
        images = {link: folder / link for _alt, link in _IMAGE_LINK.findall(body) if (folder / link).exists()}
        pages[int(index)] = _Page(body.strip(), images)
    return pages


def _mineru_block(item: dict) -> str:
    kind = item.get("type")
    if kind == "text":
        level = int(item.get("text_level") or 0)
        text = str(item.get("text", "")).strip()
        return f"{'#' * min(level + 1, 4)} {text}" if level and text else text
    if kind == "list":
        return "\n".join(str(line).strip() for line in item.get("list_items", ()))
    if kind == "equation":
        return _SPACED_TEXT.sub(lambda m: "\\text{" + m.group(1).replace(" ", "") + "}", str(item.get("text", "")).strip())
    if kind in ("table", "image"):
        caption = " ".join(item.get(f"{kind}_caption", ()) or ())
        footnote = " ".join(item.get(f"{kind}_footnote", ()) or ())
        body = item.get("table_body", "") if kind == "table" else ""
        if not body and item.get("img_path"):
            body = f"![]({item['img_path']})"
        return "\n\n".join(part.strip() for part in (caption, body, footnote) if part and part.strip())
    if kind in ("page_footnote", "code"):
        return str(item.get("text") or item.get("code_body") or "").strip()
    return ""  # header, footer, page_number: repeated on every page


def parse_mineru(content: list[dict], folder: Path) -> dict[int, _Page]:
    """MinerU's ``*_content_list.json``: blocks in reading order, each with its page, counted from 0."""
    blocks: dict[int, list[str]] = {}
    for item in content:
        text = _mineru_block(item)
        if text:
            blocks.setdefault(int(item.get("page_idx", 0)), []).append(text)
    pages = {}
    for index, parts in blocks.items():
        body = html.unescape("\n\n".join(parts))
        images = {link: folder / link for _alt, link in _IMAGE_LINK.findall(body) if (folder / link).exists()}
        pages[index] = _Page(body, images)
    return pages


def run_marker(pdf: Path, work: Path, control: JobControl | None = None) -> dict[int, _Page]:
    out = work / "marker"
    out.mkdir(parents=True, exist_ok=True)
    _run([str(executable(MARKER)), str(pdf), "--output_dir", str(out), "--paginate_output"], work / "marker.log", control)
    found = next(iter(sorted(out.rglob("*.md"))), None)
    if found is None:
        raise ConverterError("Marker wrote no Markdown.")
    return parse_marker(found.read_text(encoding="utf-8"), found.parent)


def run_mineru(pdf: Path, work: Path, control: JobControl | None = None, watch: Callable[[int, int], None] = lambda _d, _t: None) -> dict[int, _Page]:
    """``watch`` gets ``(done, total)`` of MinerU's progress bar as it moves."""
    out = work / "mineru"
    out.mkdir(parents=True, exist_ok=True)
    shown = [None]

    def look(tail: str) -> None:
        found = mineru_progress(tail)
        if found and found != shown[0]:
            shown[0] = found
            watch(*found)

    _run([str(executable(MINERU)), "-p", str(pdf), "-o", str(out), "-b", "vlm-auto-engine", "-l", "latin"], work / "mineru.log", control, look)
    found = next(iter(sorted(out.rglob("*_content_list.json"))), None)
    if found is None:
        raise ConverterError("MinerU wrote no content list.")
    return parse_mineru(json.loads(found.read_text(encoding="utf-8")), found.parent)


# --- a document --------------------------------------------------------------


def _keep_figures(page: _Page, figures: Path, ref: str, number: int) -> str:
    """Copies the page's images beside the conversion and points the Markdown at the copies."""
    markdown = page.markdown
    for index, (link, source) in enumerate(page.images.items(), start=1):
        name = f"{ref}-p{number}-{index}{source.suffix.lower() or '.jpg'}"
        figures.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, figures / name)
        markdown = markdown.replace(f"]({link})", f"](figures/{name})")
    return markdown


@dataclass
class DocumentResult:
    text: str  # stamped like a chat tool's reply
    engines: dict[int, str]  # page → which converter's version was kept
    kinds: dict[int, str]
    kept: list[int] = field(default_factory=list)  # pages whose earlier version matched the PDF better


@dataclass
class _Document:
    """What a document's pages are scored against, worked out once per run."""

    view: "DocumentView"
    raw: dict[int, str]
    common: frozenset[str]
    offset: int
    earlier: dict[int, str]  # pages converted before, which stay unless the new version scores higher


def _prepare(document: "DocumentView", earlier: dict[int, str]) -> _Document:
    raw = {p.number: p.raw_text for p in document.pages}
    if not raw:
        with pymupdf.open(document.path) as source:
            raw = {n: source[n - 1].get_text() for n in range(1, source.page_count + 1)}
    texts = [raw.get(n, "") for n in range(1, document.page_count + 1)]
    return _Document(document, raw, pdf_conversion.common_terms(texts), pdf_conversion.printed_page_offset(texts), earlier)


Step = Callable[[str, float], None]  # what runs now, and how far the batch is, 0 to 1
MARKER_SHARE, LAYOUT_SHARE = 0.1, 0.7  # of a batch's time: Marker until 0.1, MinerU's layout pass until 0.7, its text pass the rest


def convert_batch(items: list[tuple[_Document, int]], figures: Path, control: JobControl | None = None, step: Step = lambda _t, _f: None) -> dict[str, DocumentResult]:
    """Pages from one or more documents, through one Marker run and at most one MinerU run, so
    the models load once per batch, not once per document."""
    results: dict[str, DocumentResult] = {}
    with ExitStack() as stack:
        work = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="universe-convert-")))
        paths = {doc.view.key: doc.view.path for doc, _n in items}
        sources = {key: stack.enter_context(pymupdf.open(path)) for key, path in paths.items()}
        pages = [(sources[doc.view.key], number) for doc, number in items]
        kinds = [classify(source[number - 1]) for source, number in pages]
        with_mineru = executable(MINERU).exists()
        step(f"Marker reads {len(items)} page{'s' * (len(items) != 1)}", 0.0)
        _subset(pages, work / "marker.pdf")
        marker = run_marker(work / "marker.pdf", work, control)

        def doubtful(index: int) -> bool:  # Marker lost what the PDF says, e.g. a slide forced into a table
            doc, number = items[index]
            if index not in marker:
                return True
            check = pdf_conversion.check_page(doc.raw.get(number, ""), marker[index].markdown, number, doc.common, doc.offset)
            return not (check.ok or check.scanned)

        second = [i for i in range(len(items)) if kinds[i] in SECOND_OPINION or doubtful(i)] if with_mineru else []
        mineru: dict[int, _Page] = {}
        if second:
            pages_word = f"{len(second)} page{'s' * (len(second) != 1)}"

            def watch(done: int, total: int) -> None:
                if total == len(second):
                    step(f"MinerU double-checks {pages_word} with formulas, tables or text Marker missed: page {done} of {total}", MARKER_SHARE + (LAYOUT_SHARE - MARKER_SHARE) * done / total)
                else:
                    step(f"MinerU reads the text on those {pages_word}: part {done} of {total}", LAYOUT_SHARE + (1 - LAYOUT_SHARE) * done / total)

            step(f"MinerU double-checks {pages_word} with formulas, tables or text Marker missed: starting", MARKER_SHARE)
            _subset([pages[i] for i in second], work / "mineru.pdf")
            found = run_mineru(work / "mineru.pdf", work, control, watch)
            mineru = {second[position]: page for position, page in found.items() if position < len(second)}

        parts: dict[str, list[str]] = {}
        for index, (doc, number) in enumerate(items):
            result = results.setdefault(doc.view.key, DocumentResult("", {}, {}))
            result.kinds[number] = kinds[index]
            versions = {MARKER: marker[index]} if index in marker else {}
            if index in mineru:
                versions[MINERU] = mineru[index]
            if not versions:
                continue  # missing stays missing, and the report says so
            raw = doc.raw.get(number, "")
            engine, markdown = pick(raw, {e: p.markdown for e, p in versions.items()}, number, doc.common, doc.offset)
            if number in doc.earlier and score(raw, doc.earlier[number], number, doc.common, doc.offset) >= score(raw, markdown, number, doc.common, doc.offset):
                result.kept.append(number)
                continue
            result.engines[number] = engine
            body = _keep_figures(versions[engine], figures, doc.view.ref, number)
            parts.setdefault(doc.view.key, []).append(f"{pdf_conversion.stamp(doc.view.ref, number)}\n\n{body}")
    for key, texts in parts.items():
        results[key].text = "\n\n".join(texts)
    return results


@dataclass(frozen=True)
class Status:
    """What the panel shows while converting."""

    text: str  # two lines: the whole run, then what runs now
    fraction: float  # of the whole run, 0 to 1


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "less than a minute"
    return f"about {minutes} min" if minutes < 90 else f"about {minutes / 60:.1f} h"


class _Tracker:
    """Turns a batch's progress into progress through the whole run, with the time left."""

    def __init__(self, total: int, report: Callable[[Status], None]) -> None:
        self.total, self.done, self.report = max(total, 1), 0, report
        self.started = time.monotonic()

    def show(self, step: str, within: float, batch: int) -> None:
        fraction = min(1.0, (self.done + within * batch) / self.total)
        elapsed = time.monotonic() - self.started
        head = f"{fraction:.0%} of {self.total} page{'s' * (self.total != 1)}"
        if fraction >= 0.03 and elapsed >= 30:
            head += f" · {_duration(elapsed / fraction * (1 - fraction))} left"
        self.report(Status(f"{head}\n{step}", fraction))


def convert_subject(view: "SubjectView", control: JobControl | None = None, progress: Progress = lambda _t: None, redo: str = MISSING) -> pdf_conversion.ConversionReport:
    """Every page still without Markdown, except from excluded documents; with ``redo`` WEAK also
    the pages that do not match the PDF, with EVERY all pages. A page converted before keeps its
    version unless the new one matches the PDF better. Saved run by run, so a stop or an error
    keeps what was finished."""
    if not installed():
        raise ConverterError("The local converters are not installed. See README → Local PDF conversion.")
    try:
        return _convert_documents(view, control, progress, redo)
    finally:
        stop_helpers()


def pages_to_convert(document: "DocumentView", done: dict[int, str], redo: str = MISSING) -> list[int]:
    if redo == EVERY:
        return list(range(1, document.page_count + 1))
    weak = {p.number for p in document.pages if p.check is not None and not p.check.ok} if redo == WEAK else set()
    return [n for n in range(1, document.page_count + 1) if n not in done or n in weak]


def _convert_documents(view: "SubjectView", control: JobControl | None, progress: Progress, redo: str = MISSING) -> pdf_conversion.ConversionReport:
    total = pdf_conversion.ConversionReport()
    figures = view.subject.converted_dir / "figures"
    items: list[tuple[_Document, int]] = []
    for document in view.documents:
        if document.excluded or not document.page_count:
            continue
        done = pdf_conversion.load_pages(view.subject, document.key)
        todo = pages_to_convert(document, done, redo)
        if todo:
            doc = _prepare(document, {n: done[n] for n in todo if n in done})
            items += [(doc, n) for n in todo]
    tracker = _Tracker(len(items), progress)
    for first in range(0, len(items), CHUNK_PAGES):
        if control is not None:
            control.checkpoint()
        batch = items[first : first + CHUNK_PAGES]
        try:
            results = convert_batch(batch, figures, control, lambda text, within: tracker.show(text, within, len(batch)))
        except ConverterError as exc:
            refs = ", ".join(dict.fromkeys(f"{doc.view.ref} {doc.view.title}" for doc, _n in batch))
            total.problems.append(f"{refs}: {exc}")
            break
        tracker.show("Saving…", 1.0, len(batch))
        for doc, _n in batch:
            result = results.pop(doc.view.key, None)
            if result is None:
                continue  # already saved
            if result.text:
                total.merge(pdf_conversion.import_converted(view, result.text))
            if result.kept:
                total.problems.append(f"{doc.view.ref}: kept the earlier version of {pdf_conversion.page_list(result.kept)}, which matches the PDF better.")
        tracker.done += len(batch)
    return total
