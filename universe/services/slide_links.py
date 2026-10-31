"""Links from notes to a page of a PDF: ``[→ VL1, slide 12](slide:slides/VL1.pdf#12)``."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, unquote

# ``doc`` in a link is the folder the PDF is stored in; these are the words for a document type.
WORDS = {"Slides": ("", "slide"), "Past exam": ("Past exam ", "p."), "Task": ("Task ", "p.")}
KINDS = {"slides": WORDS["Slides"], "exams": WORDS["Past exam"], "exercises": WORDS["Task"]}
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\((slide:[^)\s]+)\)")


def make_href(target: dict) -> str:
    return f"slide:{target['doc']}/{quote(target['pdf'], safe='')}#{target['page']}"


def parse_href(href: str) -> dict | None:
    """``{"doc", "pdf", "page"}`` of a slide link, or None for anything else."""
    match = re.fullmatch(r"slide:(\w+)/([^#]+)#(\d+)", href)
    if not match or match.group(1) not in KINDS:
        return None
    return {"doc": match.group(1), "pdf": unquote(match.group(2)), "page": int(match.group(3))}


def words_for(types: list[str]) -> tuple[str, str]:
    """(prefix, unit) for a document with these type tags."""
    return WORDS[types[0]] if types else ("", "p.")


def link_text(target: dict) -> str:
    kind, unit = words_for([target["type"]] if target["type"] else []) if "type" in target else KINDS[target["doc"]]
    return f"→ {kind}{Path(target['pdf']).stem}, {unit} {target['page']}"


def for_export(markdown: str, words=None) -> str:
    """Replace links by plain references that name the PDF as the export's sections do.
    ``words(target)`` gives (prefix, unit); by default they follow the PDF's folder."""

    def reference(match: re.Match) -> str:
        target = parse_href(match.group(2))
        if target is None:
            return match.group(1)
        kind, unit = words(target) if words else KINDS[target["doc"]]
        return f"(→ see {kind.lower()}{target['pdf']} {unit} {target['page']})"

    return MARKDOWN_LINK_RE.sub(reference, markdown)
