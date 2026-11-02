"""PDF text extraction with PyMuPDF (rendering lives in the UI viewer)."""

from __future__ import annotations

import hashlib
import re
import shutil
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pymupdf

# As the PDF viewer stores them; a mark gets the name of the nearest.
MARK_COLORS = {
    "yellow": (1.0, 0.92, 0.23),
    "green": (0.56, 0.93, 0.47),
    "blue": (0.53, 0.81, 0.98),
    "pink": (1.0, 0.6, 0.8),
}
VISUAL_IMAGE_SHARE = 0.25  # images covering this much of a page carry content the text misses
BACKGROUND_SHARE = 0.9
TEMPLATE_SHARE = 0.3  # an image on this share of a deck's pages is its template
NEARLY_EMPTY = 30  # characters: a page saying less than this says it some other way
VISUAL_DRAWINGS = 25  # as do this many vector shapes: charts, diagrams, T-accounts
PRIVATE_USE = re.compile("[\ue000-\uf8ff]")


def needs_password(path: Path) -> bool:
    """Whether the PDF only opens with a password (one that merely restricts
    printing or copying opens fine and is not meant here)."""
    try:
        with pymupdf.open(path) as doc:
            return bool(doc.needs_pass)
    except Exception:  # damaged: the viewer says so
        return False


def unlock(path: Path, password: str) -> bool:
    """Replace a password-locked PDF with an unlocked copy, so the viewer, the search
    index and the Claude export can all read it. The locked original stays next to it
    as a hidden ``.<name>.locked``. False if the password is wrong."""
    with pymupdf.open(path) as doc:
        if not doc.needs_pass:
            return True
        if not doc.authenticate(password):
            return False
        tmp = path.with_name(path.name + ".saving")
        doc.save(tmp, garbage=1, encryption=pymupdf.PDF_ENCRYPT_NONE)
    backup = path.with_name(f".{path.name}.locked")
    if not backup.exists():
        shutil.copy2(path, backup)
    tmp.replace(path)
    return True


def page_texts(path: Path) -> list[str]:
    """Plain text of every page, in order."""
    with pymupdf.open(path) as doc:
        return [page.get_text("text").strip() for page in doc]


@dataclass(frozen=True)
class Mark:
    """What the student marked on a page; ``text`` is empty for a box around a chart."""

    text: str
    color: str = ""


@dataclass(frozen=True)
class PageContent:
    number: int
    text: str
    tables: tuple[str, ...]  # Markdown
    marks: tuple[Mark, ...]
    visual: bool  # images or drawings carry content that the text misses


def _color_name(rgb) -> str:
    if not rgb or len(rgb) != 3:
        return ""
    return min(MARK_COLORS, key=lambda name: sum((a - b) ** 2 for a, b in zip(MARK_COLORS[name], rgb)))


def _marked_text(page: pymupdf.Page, annot) -> str:
    """A highlight's own lines (a bounding box would also catch the rest of the first and
    last line); a box takes everything inside it."""
    vertices = annot.vertices or []
    if annot.type[0] == pymupdf.PDF_ANNOT_HIGHLIGHT and len(vertices) >= 4:
        lines = [pymupdf.Quad(vertices[i : i + 4]).rect for i in range(0, len(vertices) - 3, 4)]
        return " ".join(" ".join(page.get_text("text", clip=rect).split()) for rect in lines).strip()
    return " ".join(page.get_text("text", clip=annot.rect).split())


def _page_marks(page: pymupdf.Page) -> tuple[Mark, ...]:
    marks = []
    for annot in page.annots(types=[pymupdf.PDF_ANNOT_HIGHLIGHT, pymupdf.PDF_ANNOT_SQUARE]):
        marks.append(Mark(clean_text(_marked_text(page, annot)), _color_name(annot.colors.get("stroke"))))
    return tuple(marks)


def _is_visual(page: pymupdf.Page, template: set[int]) -> bool:
    """A hint, not a judgement: it cannot tell a chart from clip art. ``template``: images on most
    pages of the deck, such as a logo or background, say nothing; nor does a full-bleed image behind text."""
    area = abs(page.rect) or 1.0
    characters = len(page.get_text("text").strip())
    if characters < NEARLY_EMPTY:
        return True
    has_text = characters > 0
    images = 0.0
    for info in page.get_image_info(xrefs=True):
        share = abs(pymupdf.Rect(info["bbox"]) & page.rect) / area
        if info.get("xref") not in template and not (has_text and share >= BACKGROUND_SHARE):
            images += share
    return images >= VISUAL_IMAGE_SHARE or len(page.get_drawings()) >= VISUAL_DRAWINGS


def _template_images(doc: pymupdf.Document) -> set[int]:
    if doc.page_count < 4:
        return set()
    counts = Counter(xref for page in doc for xref in {image[0] for image in page.get_images()})
    return {xref for xref, count in counts.items() if count > doc.page_count * TEMPLATE_SHARE}


def clean_text(text: str) -> str:
    """Symbol fonts put bullets into Unicode's private use area, which reads as noise,
    and PDFs keep soft hyphens that break searching for a word."""
    return PRIVATE_USE.sub("•", unicodedata.normalize("NFC", text)).replace("\u00ad", "")


def _page_tables(page: pymupdf.Page) -> tuple[str, ...]:
    try:
        tables = page.find_tables().tables
    except Exception:  # table detection is best effort (odd layouts)
        return ()
    return tuple(t.to_markdown(clean=False).strip() for t in tables if t.row_count > 1 and t.col_count > 1)


def read_pages(path: Path) -> list[PageContent]:
    """Everything a PDF says, page by page."""
    with pymupdf.open(path) as doc:
        template = _template_images(doc)
        return [
            PageContent(
                number, clean_text(page.get_text("text")).strip(), _page_tables(page), _page_marks(page), _is_visual(page, template)
            )
            for number, page in enumerate(doc, start=1)
        ]


def page_marks(path: Path) -> dict[int, tuple[Mark, ...]]:
    """Only the student's marks, by 1-based page number."""
    with pymupdf.open(path) as doc:
        return {number: marks for number, page in enumerate(doc, start=1) if (marks := _page_marks(page))}


def highlights(path: Path) -> dict[int, list[str]]:
    """Text the student highlighted (or boxed), by 1-based page number."""
    found: dict[int, list[str]] = {}
    with pymupdf.open(path) as doc:
        for number, page in enumerate(doc, start=1):
            for mark in _page_marks(page):
                if mark.text:
                    found.setdefault(number, []).append(mark.text)
    return found


def content_key(path: Path) -> tuple[str, int]:
    """``(key, pages)``. The key follows what the pages say, not the file's bytes: highlights
    live inside the PDF, so two copies of a deck differ by bytes but are one document."""
    with pymupdf.open(path) as doc:
        parts = [" ".join(page.get_text("text").split()).encode() for page in doc]
        if not any(parts):  # a scan: its images are what it says, and marks do not change them
            parts = [b"".join(doc.xref_stream_raw(image[0]) or b"" for image in page.get_images()) for page in doc]
        digest = hashlib.sha256(b"\f".join(parts)).hexdigest()
        return f"d_{digest[:12]}", len(parts)
