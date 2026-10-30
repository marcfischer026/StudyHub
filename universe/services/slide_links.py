"""Links from notes to a page of a PDF: ``[→ VL1, slide 12](slide:slides/VL1.pdf#12)``."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, unquote

KINDS = {"slides": ("", "slide"), "exams": ("Past exam ", "p."), "exercises": ("Exercise ", "p.")}
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\((slide:[^)\s]+)\)")


def make_href(target: dict) -> str:
    return f"slide:{target['doc']}/{quote(target['pdf'], safe='')}#{target['page']}"


def parse_href(href: str) -> dict | None:
    """``{"doc", "pdf", "page"}`` of a slide link, or None for anything else."""
    match = re.fullmatch(r"slide:(\w+)/([^#]+)#(\d+)", href)
    if not match or match.group(1) not in KINDS:
        return None
    return {"doc": match.group(1), "pdf": unquote(match.group(2)), "page": int(match.group(3))}


def link_text(target: dict) -> str:
    kind, unit = KINDS[target["doc"]]
    return f"→ {kind}{Path(target['pdf']).stem}, {unit} {target['page']}"


def for_export(markdown: str) -> str:
    """Replace links by plain references that name the PDF as the export's sections do."""

    def reference(match: re.Match) -> str:
        target = parse_href(match.group(2))
        if target is None:
            return match.group(1)
        kind, unit = KINDS[target["doc"]]
        return f"(→ see {kind.lower()}{target['pdf']} {unit} {target['page']})"

    return MARKDOWN_LINK_RE.sub(reference, markdown)
