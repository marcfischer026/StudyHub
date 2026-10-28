"""PDF text extraction with PyMuPDF (rendering lives in the UI viewer)."""

from __future__ import annotations

from pathlib import Path

import pymupdf


def page_texts(path: Path) -> list[str]:
    """Plain text of every page, in order."""
    with pymupdf.open(path) as doc:
        return [page.get_text("text").strip() for page in doc]
