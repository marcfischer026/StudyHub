"""Deadlines and events from a Moodle calendar export (iCal).

In Moodle: Calendar → Export calendar → "All courses" + "Recent and next
60 days" → Get calendar URL. That URL carries a personal token, so anyone
who has it can read the calendar; it lives only in settings.json.

The last successful download is kept in ``cache/moodle/calendar.ics`` so
deadlines still show offline.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import config


@dataclass(frozen=True)
class Event:
    uid: str
    title: str
    start: datetime  # local time, timezone-aware
    course: str  # Moodle's CATEGORIES: the course name; "" for personal / site events
    description: str = ""
    all_day: bool = False


def cache_path() -> Path:
    return config.cache_dir("moodle") / "calendar.ics"


def fetch(url: str, timeout: float = 20) -> str:
    """Download the calendar, cache it and return its text."""
    url = url.strip()
    if url.startswith("webcal://"):
        url = "https://" + url.removeprefix("webcal://")
    if not url.startswith(("https://", "http://")):
        raise ValueError("The calendar URL must start with https://")
    request = urllib.request.Request(url, headers={"User-Agent": "UniVerse"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Moodle answered {exc.code} — is the calendar URL still valid?") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Moodle not reachable: {getattr(exc, 'reason', exc)}") from exc
    if "BEGIN:VCALENDAR" not in text:
        raise RuntimeError("That URL did not return a calendar — copy it from Moodle's Export calendar page.")
    cache_path().write_text(text, encoding="utf-8")
    return text


def cached() -> str:
    path = cache_path()
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _unfold(text: str) -> list[str]:
    """RFC 5545 lines: a line starting with a space or tab continues the previous one."""
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        elif line:
            lines.append(line)
    return lines


def _unescape(value: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value).strip()


def _parse_time(value: str, params: dict[str, str]) -> tuple[datetime, bool]:
    if params.get("VALUE") == "DATE" or len(value) == 8:
        day = date(int(value[:4]), int(value[4:6]), int(value[6:8]))
        return datetime.combine(day, time(0, 0)).astimezone(), True
    moment = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return moment.replace(tzinfo=timezone.utc).astimezone(), False
    try:
        zone = ZoneInfo(params["TZID"]) if "TZID" in params else None
    except ZoneInfoNotFoundError:
        zone = None
    return (moment.replace(tzinfo=zone) if zone else moment.astimezone()).astimezone(), False


def parse(text: str) -> list[Event]:
    """Events in ``text``, earliest first. Unparseable events are skipped."""
    events: list[Event] = []
    current: dict[str, tuple[str, dict[str, str]]] | None = None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None and "DTSTART" in current:
                try:
                    start, all_day = _parse_time(*current["DTSTART"])
                except ValueError:
                    start = None
                if start is not None:
                    get = lambda key: _unescape(current.get(key, ("", {}))[0])  # noqa: E731
                    events.append(
                        Event(
                            uid=get("UID"),
                            title=get("SUMMARY") or "(untitled)",
                            start=start,
                            course=get("CATEGORIES"),
                            description=get("DESCRIPTION"),
                            all_day=all_day,
                        )
                    )
            current = None
            continue
        if current is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, *raw_params = head.split(";")
        params = dict(p.split("=", 1) for p in raw_params if "=" in p)
        current[name.upper()] = (value, {k.upper(): v.strip('"') for k, v in params.items()})
    return sorted(events, key=lambda e: e.start)


def upcoming(events: list[Event], now: datetime | None = None, limit: int | None = None) -> list[Event]:
    """Events that have not started yet (all-day events count until the day is over)."""
    now = now or datetime.now().astimezone()
    today = now.date()
    future = [e for e in events if (e.start.date() >= today if e.all_day else e.start >= now)]
    return future[:limit] if limit else future


def _norm(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def matches(event: Event, subject_name: str, short_name: str = "") -> bool:
    """Whether a Moodle event belongs to a UniVerse subject, judged by the course name."""
    course, name = _norm(event.course), _norm(subject_name)
    if not course or not name:
        return False
    if name in course or course in name:
        return True
    short = _norm(short_name)
    return len(short) >= 2 and short in course.split()


def when(event: Event, now: datetime | None = None) -> str:
    """Short date for lists: "today 23:59", "tomorrow", "Wed 04.11. 23:59"."""
    now = now or datetime.now().astimezone()
    days = (event.start.date() - now.date()).days
    day = {0: "today", 1: "tomorrow"}.get(days) or event.start.strftime("%a %d.%m.")
    return day if event.all_day else f"{day} {event.start:%H:%M}"
