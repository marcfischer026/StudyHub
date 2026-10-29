"""PDF text extraction with PyMuPDF (rendering lives in the UI viewer)."""

from __future__ import annotations

from pathlib import Path

import pymupdf


def page_texts(path: Path) -> list[str]:
    """Plain text of every page, in order."""
    with pymupdf.open(path) as doc:
        return [page.get_text("text").strip() for page in doc]


def highlights(path: Path) -> dict[int, list[str]]:
    """Text the student highlighted (or boxed), by 1-based page number."""
    found: dict[int, list[str]] = {}
    with pymupdf.open(path) as doc:
        for number, page in enumerate(doc, start=1):
            for annot in page.annots(types=[pymupdf.PDF_ANNOT_HIGHLIGHT, pymupdf.PDF_ANNOT_SQUARE]):
                text = " ".join(page.get_text("text", clip=annot.rect).split())
                if text:
                    found.setdefault(number, []).append(text)
    return found
