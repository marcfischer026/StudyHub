"""Tests for the Moodle calendar import: parsing, upcoming filter, subject matching, the start page."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from universe.services import moodle_ical  # noqa: E402
from universe.storage import Workspace  # noqa: E402
from universe.ui.welcome import WelcomePage  # noqa: E402

app = QApplication.instance() or QApplication([])

# Shaped like Moodle's calendar/export_execute.php output, including a folded line.
SAMPLE = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Moodle Pty Ltd//NONSGML Moodle Version 2024100700//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:101@elearning.dhbw-stuttgart.de\r\n"
    "SUMMARY:Übungsblatt 3 ist fällig\r\n"
    "DESCRIPTION:Bitte als PDF\\, max. 5 Seiten.\\nViel Erfolg\r\n"
    "CATEGORIES:Corporate Finance WWI24A\r\n"
    "DTSTART:20261104T225900Z\r\n"
    "DTEND:20261104T225900Z\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:102@elearning.dhbw-stuttgart.de\r\n"
    "SUMMARY:Klausur Mathematik I – Raum 2.1\r\n"
    " 3\r\n"
    "CATEGORIES:Mathematik I\r\n"
    "DTSTART;TZID=Europe/Berlin:20261020T090000\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:103@elearning.dhbw-stuttgart.de\r\n"
    "SUMMARY:Vorlesungsfrei\r\n"
    "DTSTART;VALUE=DATE:20261102\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "SUMMARY:kaputt\r\n"
    "DTSTART:nonsense\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc).astimezone()


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.events = moodle_ical.parse(SAMPLE)

    def test_events_are_parsed_and_sorted(self):
        self.assertEqual([e.uid for e in self.events], ["102@elearning.dhbw-stuttgart.de", "103@elearning.dhbw-stuttgart.de", "101@elearning.dhbw-stuttgart.de"])
        exam, holiday, sheet = self.events
        self.assertEqual(exam.title, "Klausur Mathematik I – Raum 2.13")  # folded line joined
        self.assertEqual(exam.start, datetime(2026, 10, 20, 7, 0, tzinfo=timezone.utc))
        self.assertEqual(sheet.course, "Corporate Finance WWI24A")
        self.assertEqual(sheet.description, "Bitte als PDF, max. 5 Seiten.\nViel Erfolg")
        self.assertEqual(sheet.start, datetime(2026, 11, 4, 22, 59, tzinfo=timezone.utc))
        self.assertTrue(holiday.all_day)

    def test_upcoming_drops_past_events(self):
        self.assertEqual([e.title for e in moodle_ical.upcoming(self.events, NOW)], ["Vorlesungsfrei", "Übungsblatt 3 ist fällig"])
        self.assertEqual(len(moodle_ical.upcoming(self.events, NOW, limit=1)), 1)

    def test_subject_matching(self):
        sheet = self.events[-1]
        self.assertTrue(moodle_ical.matches(sheet, "Corporate Finance"))
        self.assertTrue(moodle_ical.matches(sheet, "Finanzierung", short_name="WWI24A"))
        self.assertFalse(moodle_ical.matches(sheet, "Mathematik I"))
        self.assertFalse(moodle_ical.matches(self.events[1], "Corporate Finance"))  # no course

    def test_when(self):
        sheet = self.events[-1]
        self.assertIn(sheet.start.strftime("%H:%M"), moodle_ical.when(sheet, NOW))
        self.assertEqual(moodle_ical.when(self.events[1], datetime(2026, 11, 1, 12, tzinfo=NOW.tzinfo)), "tomorrow")

    def test_fetch_rejects_non_calendars(self):
        with self.assertRaises(ValueError):
            moodle_ical.fetch("ftp://example.com/cal.ics")


class StartPageTests(unittest.TestCase):
    def test_deadlines_follow_the_selected_subject(self):
        workspace = Workspace()
        page = WelcomePage(workspace)
        events = [e for e in moodle_ical.parse(SAMPLE.replace("2026", "2099"))]  # all in the future
        page.set_events(events)
        self.assertFalse(page.deadlines.isHidden())
        self.assertIn("Übungsblatt 3", page.deadlines_list.text())
        self.assertIn("Corporate Finance WWI24A", page.deadlines_list.text())

        page.show_subject(workspace.create_subject("Mathematik I"))
        self.assertIn("Klausur", page.deadlines_list.text())
        self.assertNotIn("Übungsblatt", page.deadlines_list.text())

        page.show_subject(workspace.create_subject("Physik"))
        self.assertTrue(page.deadlines.isHidden())

    def test_no_events_no_card(self):
        page = WelcomePage(Workspace())
        page.set_events([])
        self.assertTrue(page.deadlines.isHidden())


if __name__ == "__main__":
    unittest.main()
