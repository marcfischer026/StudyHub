"""The ``[▶ HH:MM:SS]`` note timestamp format: position in the recording, optionally
followed by the time of day, ``[▶ 00:15:03 · 12:15]``."""

from __future__ import annotations

import re
from datetime import datetime

# Qt's Markdown writer escapes brackets, so accept an optional backslash.
TIMESTAMP_RE = re.compile(r"\\?\[▶ (\d{1,2}):(\d{2}):(\d{2})(?: · (\d{1,2}:\d{2}))?\\?\]")


def format_seconds(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def make_stamp(seconds: float, clock: datetime | str | None = None) -> str:
    if isinstance(clock, datetime):
        clock = f"{clock:%H:%M}"
    return f"[▶ {format_seconds(seconds)}{f' · {clock}' if clock else ''}]"


def stamp_seconds(match: re.Match) -> int:
    h, m, s = (int(g) for g in match.groups()[:3])
    return h * 3600 + m * 60 + s


def stamp_at(text: str, column: int) -> int | None:
    """Seconds of the timestamp covering ``column`` in ``text``, if any."""
    for match in TIMESTAMP_RE.finditer(text):
        if match.start() <= column <= match.end():
            return stamp_seconds(match)
    return None


def normalize(markdown: str) -> str:
    """Remove the escaping Qt adds so notes on disk read as plain ``[▶ ...]``."""
    return TIMESTAMP_RE.sub(lambda m: make_stamp(stamp_seconds(m), m.group(4)), markdown)
