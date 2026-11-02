"""Tests for converting PDFs on this Mac with Marker and MinerU. The converters themselves are
not run: their output is faked in the shapes they write."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())

from universe.services import local_conversion, pdf_conversion  # noqa: E402
from universe.services.local_conversion import MARKER, MATH, MINERU, TABLE, TEXT, _Page  # noqa: E402
from tests.test_pdf_conversion import Fixture, make_pdf  # noqa: E402


class FakePage:
    """Just what :func:`local_conversion.classify` reads."""

    def __init__(self, spans: list[tuple[str, str]], tables: int = 0):
        self.spans, self.tables = spans, tables

    def get_text(self, kind: str = "text"):
        if kind == "dict":
            return {"blocks": [{"lines": [{"spans": [{"text": t, "font": f} for t, f in self.spans]}]}]}
        return "\n".join(t for t, _f in self.spans)

    def get_images(self):
        return []

    def find_tables(self):
        return mock.Mock(tables=[object()] * self.tables)


class ClassifyTests(unittest.TestCase):
    def test_a_garbled_math_font_is_math(self):
        page = FakePage([("Aus (VI) folgt, dass ", "Calibri"), ("ݒଷ= 1", "Calibri")])
        self.assertEqual(local_conversion.classify(page), MATH)

    def test_a_math_font_is_math(self):
        page = FakePage([("Die Ableitung lautet ", "Calibri"), ("f'(x) = 2x", "CambriaMath")])
        self.assertEqual(local_conversion.classify(page), MATH)

    def test_wingdings_bullets_are_not_math(self):
        page = FakePage([(" Bilanz und Inventar im Überblick, mit allen Posten", "Wingdings")])
        self.assertEqual(local_conversion.classify(page), TEXT)

    def test_a_table_and_plain_text(self):
        self.assertEqual(local_conversion.classify(FakePage([("Kasse 900 €", "Arial")], tables=1)), TABLE)
        self.assertEqual(local_conversion.classify(FakePage([("Die Inventur ist eine Bestandsaufnahme.", "Arial")])), TEXT)


class ParseTests(unittest.TestCase):
    def test_marker_pages_and_their_images(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "_page_1_Picture_3.jpeg").write_bytes(b"jpg")
        text = "{0}" + "-" * 48 + "\n\n# Bilanz\n\n{1}" + "-" * 48 + "\n\nText\n\n![](_page_1_Picture_3.jpeg)\n![](gone.jpeg)\n"
        pages = local_conversion.parse_marker(text, folder)
        self.assertEqual(pages[0].markdown, "# Bilanz")
        self.assertEqual(list(pages[1].images), ["_page_1_Picture_3.jpeg"])

    def test_mineru_blocks_by_page_without_headers_and_footers(self):
        content = [
            {"type": "header", "text": "MATHEMATIK", "page_idx": 0},
            {"type": "text", "text": "Schritt 3:", "text_level": 1, "page_idx": 0},
            {"type": "equation", "text": "$$\nA \\xrightarrow {\\text {G a u ß}} B\n$$", "page_idx": 0},
            {"type": "page_number", "text": "5", "page_idx": 0},
            {"type": "list", "list_items": ["- homogen", "- inhomogen"], "page_idx": 1},
            {"type": "table", "table_caption": ["Bilanz"], "table_body": "<table><tr><td>Kasse &amp; Bank</td></tr></table>", "page_idx": 1},
        ]
        pages = local_conversion.parse_mineru(content, Path(tempfile.mkdtemp()))
        self.assertEqual(pages[0].markdown, "## Schritt 3:\n\n$$\nA \\xrightarrow {\\text{Gauß}} B\n$$")
        self.assertIn("- homogen\n- inhomogen", pages[1].markdown)
        self.assertIn("Kasse & Bank", pages[1].markdown)


class PickTests(unittest.TestCase):
    RAW = "Aus (VI) folgt, dass ݒଷ= 1. Eingesetzt in (IV) erhalten wir 9 und 12 als Lösung der Gleichung."

    def test_garbled_math_glyphs_do_not_count_as_words_or_numbers(self):
        check = pdf_conversion.check_page(self.RAW, "Aus (VI) folgt, dass $x_3 = 1$. Eingesetzt in (IV) erhalten wir 9 und 12 als Lösung der Gleichung.")
        self.assertTrue(check.ok, check.describe())

    def test_marker_wins_a_tie_and_a_clearly_better_mineru_wins(self):
        good = "Aus (VI) folgt, dass $x_3 = 1$. Eingesetzt in (IV) erhalten wir 9 und 12 als Lösung der Gleichung."
        self.assertEqual(local_conversion.pick(self.RAW, {MARKER: good, MINERU: good}, 1)[0], MARKER)
        dropped = "Aus (VI) folgt, dass $x_3 = 1$."
        self.assertEqual(local_conversion.pick(self.RAW, {MARKER: dropped, MINERU: good}, 1)[0], MINERU)

    def test_typos_broken_latex_and_leaked_glyphs_cost_points(self):
        good = "Aus (VI) folgt, dass $x_3 = 1$. Eingesetzt in (IV) erhalten wir 9 und 12 als Lösung der Gleichung."
        best = local_conversion.score(self.RAW, good, 1)
        self.assertLess(local_conversion.score(self.RAW, good.replace("Eingesetzt", "Eingesezt"), 1), best)
        self.assertLess(local_conversion.score(self.RAW, good.replace("$x_3", "$x_{3"), 1), best)
        self.assertLess(local_conversion.score(self.RAW, good + " ݒଷ", 1), best)
        self.assertEqual(local_conversion.score(self.RAW, "  ", 1), -1.0)


class ConvertTests(Fixture):
    def test_pages_are_saved_with_figures_and_the_better_version(self):
        make_pdf(self.lecture.dir("slides") / "Mathe.pdf", ["Die Inventur ist eine Bestandsaufnahme.", "Kasse 900 und Bank 14100 am Stichtag"])
        figure = Path(tempfile.mkdtemp()) / "_page_0_Figure_1.jpeg"
        figure.write_bytes(b"jpg")
        marker = {
            0: _Page("Die Inventur ist eine Bestandsaufnahme.\n\n![](_page_0_Figure_1.jpeg)", {"_page_0_Figure_1.jpeg": figure}),
            1: _Page("Kasse 900 am Stichtag", {}),
        }
        mineru = {0: _Page("| Kasse | 900 |\n| Bank | 14100 |\n\nam Stichtag", {})}
        kinds = {1: TEXT, 2: TABLE}
        with (
            mock.patch.object(local_conversion, "installed", return_value=True),
            mock.patch.object(local_conversion, "executable", return_value=Path(__file__)),
            mock.patch.object(local_conversion, "classify", side_effect=lambda page: kinds[page.number + 1]),
            mock.patch.object(local_conversion, "run_marker", return_value=marker),
            mock.patch.object(local_conversion, "run_mineru", return_value=mineru) as run_mineru,
        ):
            report = local_conversion.convert_subject(self.view())
        self.assertEqual(report.documents[0].imported, [1, 2])
        self.assertEqual(run_mineru.call_count, 1)  # only the table page gets a second opinion
        document = self.view().documents[0]
        pages = pdf_conversion.load_pages(self.subject, document.key)
        self.assertIn("](figures/SL01-p1-1.jpeg)", pages[1])
        self.assertTrue((self.subject.converted_dir / "figures" / "SL01-p1-1.jpeg").exists())
        self.assertIn("14100", pages[2])  # Marker dropped the bank line; MinerU's table is kept

    def test_a_text_page_marker_mangled_gets_a_second_opinion(self):
        make_pdf(self.lecture.dir("slides") / "Inventar.pdf", ["Das Inventar ist ein aufgrund der Inventur erstelltes Bestandsverzeichnis"])
        mangled = {0: _Page("| Inventur | erstelltes |", {})}
        whole = {0: _Page("Das Inventar ist ein aufgrund der Inventur erstelltes Bestandsverzeichnis", {})}
        with (
            mock.patch.object(local_conversion, "installed", return_value=True),
            mock.patch.object(local_conversion, "executable", return_value=Path(__file__)),
            mock.patch.object(local_conversion, "classify", return_value=TEXT),
            mock.patch.object(local_conversion, "run_marker", return_value=mangled),
            mock.patch.object(local_conversion, "run_mineru", return_value=whole),
        ):
            local_conversion.convert_subject(self.view())
        pages = pdf_conversion.load_pages(self.subject, self.view().documents[0].key)
        self.assertIn("Bestandsverzeichnis", pages[1])

    def test_without_marker_nothing_runs(self):
        with mock.patch.object(local_conversion, "installed", return_value=False):
            with self.assertRaises(local_conversion.ConverterError):
                local_conversion.convert_subject(self.view())

    def test_a_failing_converter_is_reported_and_keeps_earlier_pages(self):
        make_pdf(self.lecture.dir("slides") / "Deck.pdf", ["Seite eins mit Text", "Seite zwei mit Text"])
        with (
            mock.patch.object(local_conversion, "installed", return_value=True),
            mock.patch.object(local_conversion, "CHUNK_PAGES", 1),
            mock.patch.object(local_conversion, "run_marker", side_effect=[{0: _Page("Seite eins mit Text", {})}, local_conversion.ConverterError("out of memory")]),
        ):
            report = local_conversion.convert_subject(self.view())
        self.assertEqual(report.documents[0].imported, [1])
        self.assertIn("out of memory", report.problems[0])


if __name__ == "__main__":
    unittest.main()
