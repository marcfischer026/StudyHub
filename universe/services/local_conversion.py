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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import pymupdf

from .. import config
from . import pdf_conversion
from .jobcontrol import JobControl

if TYPE_CHECKING:
    from .subject_model import DocumentView, SubjectView

Progress = Callable[[str], None]

MATH, TABLE, TEXT, SCAN = "math", "table", "text", "scan"
MINERU, MARKER = "mineru", "marker"
SECOND_OPINION = (MATH, TABLE)  # page kinds MinerU always reads too
CHUNK_PAGES = 40  # one converter run: loading the models costs a minute, a stop loses at most this
MINERU_MARGIN = 0.02  # MinerU must beat Marker by this much; small differences are noise
LOG_TAIL = 1500

_MATH_FONT = re.compile(r"math|cmmi|cmsy|cmex|symbol|stix|mt ?extra|euler|msbm|msam", re.IGNORECASE)
_MATH_CHAR = re.compile(r"[∑∏∫√∞≤≥≠≈∂∇∈∉⊂⊆∪∩⇒⇔±÷αβγδεθμσφωΔΣΩ²³ⁿ₀-₉]")
_MARKER_PAGE = re.compile(r"^\{(\d+)\}-{10,}\s*$", re.MULTILINE)
_IMAGE_LINK = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_SPACED_TEXT = re.compile(r"\\text\s*\{((?:\S ){2,}\S)\s*\}")  # MinerU's "\text {G a u ß}"


class ConverterError(RuntimeError):
    pass


def converters_dir() -> Path:
    return config.data_root() / "converters"


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


def _run(command: list[str], log: Path, control: JobControl | None) -> None:
    with log.open("w", encoding="utf-8", errors="replace") as out:
        proc = subprocess.Popen(command, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=_environment(), cwd=log.parent)
        if control is not None:
            control.register_process(proc)
        try:
            code = proc.wait()
        finally:
            if control is not None:
                control.unregister_process(proc)
    if control is not None:
        control.checkpoint()  # a stop shows as Cancelled, not as a failed converter
    if code:
        tail = log.read_text(encoding="utf-8", errors="replace")[-LOG_TAIL:]
        raise ConverterError(f"{Path(command[0]).name} stopped with code {code}:\n{tail}")


def _subset(source: pymupdf.Document, pages: list[int], target: Path) -> None:
    copy = pymupdf.open()
    for number in pages:
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


def run_mineru(pdf: Path, work: Path, control: JobControl | None = None) -> dict[int, _Page]:
    out = work / "mineru"
    out.mkdir(parents=True, exist_ok=True)
    _run([str(executable(MINERU)), "-p", str(pdf), "-o", str(out), "-b", "vlm-auto-engine", "-l", "latin"], work / "mineru.log", control)
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


def convert_pages(document: "DocumentView", pages: list[int], figures: Path, control: JobControl | None = None, progress: Progress = lambda _t: None) -> DocumentResult:
    raw = {p.number: p.raw_text for p in document.pages}
    with pymupdf.open(document.path) as source, tempfile.TemporaryDirectory(prefix="universe-convert-") as temp:
        work = Path(temp)
        if not raw:
            raw = {n: source[n - 1].get_text() for n in range(1, source.page_count + 1)}
        kinds = {n: classify(source[n - 1]) for n in pages}
        common = pdf_conversion.common_numbers(list(raw.values()))
        offset = pdf_conversion.printed_page_offset([raw.get(n, "") for n in range(1, source.page_count + 1)])
        progress(f"{document.ref}: Marker reads {len(pages)} page(s)…")
        _subset(source, pages, work / "marker.pdf")
        marker = run_marker(work / "marker.pdf", work, control)

        def doubtful(index: int, number: int) -> bool:  # Marker lost what the PDF says, e.g. a slide forced into a table
            check = pdf_conversion.check_page(raw.get(number, ""), marker[index].markdown, number, common, offset) if index in marker else None
            return check is None or not (check.ok or check.scanned)

        second = [n for i, n in enumerate(pages) if kinds[n] in SECOND_OPINION or doubtful(i, n)] if executable(MINERU).exists() else []
        mineru: dict[int, _Page] = {}
        if second:
            progress(f"{document.ref}: MinerU reads {len(second)} page(s) with formulas, tables or doubts…")
            _subset(source, second, work / "mineru.pdf")
            mineru = run_mineru(work / "mineru.pdf", work, control)

        engines, parts = {}, []
        for index, number in enumerate(pages):
            versions = {MARKER: marker[index]} if index in marker else {}
            if number in second and second.index(number) in mineru:
                versions[MINERU] = mineru[second.index(number)]
            if not versions:
                continue  # missing stays missing, and the report says so
            engine, _ = pick(raw.get(number, ""), {e: p.markdown for e, p in versions.items()}, number, common, offset)
            engines[number] = engine
            body = _keep_figures(versions[engine], figures, document.ref, number)
            parts.append(f"{pdf_conversion.stamp(document.ref, number)}\n\n{body}")
    return DocumentResult("\n\n".join(parts), engines, kinds)


def convert_subject(view: "SubjectView", control: JobControl | None = None, progress: Progress = lambda _t: None) -> pdf_conversion.ConversionReport:
    """Every page still without Markdown, except from excluded documents. Saved run by run, so a
    stop or an error keeps what was finished."""
    if not installed():
        raise ConverterError("The local converters are not installed. See README → Local PDF conversion.")
    total = pdf_conversion.ConversionReport()
    figures = view.subject.converted_dir / "figures"
    for document in view.documents:
        if document.excluded or not document.page_count:
            continue
        done = pdf_conversion.load_pages(view.subject, document.key)
        missing = [n for n in range(1, document.page_count + 1) if n not in done]
        for first in range(0, len(missing), CHUNK_PAGES):
            if control is not None:
                control.checkpoint()
            chunk = missing[first : first + CHUNK_PAGES]
            try:
                result = convert_pages(document, chunk, figures, control, progress)
            except ConverterError as exc:
                total.problems.append(f"{document.ref} {document.title}: {exc}")
                break
            if result.text:
                total.merge(pdf_conversion.import_converted(view, result.text))
                kept = sum(engine == MINERU for engine in result.engines.values())
                progress(f"{document.ref}: saved {len(result.engines)} page(s), MinerU's version on {kept}.")
    return total
