"""Tests for the PDF → Markdown round trip through Gemini or Copilot."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())

import pymupdf  # noqa: E402

from universe.professors import ProfessorStore  # noqa: E402
from universe.services import pdf_conversion, subject_model  # noqa: E402
from universe.services.pdf_conversion import check_page, parse_converted  # noqa: E402
from universe.storage import Workspace  # noqa: E402


def make_pdf(path: Path, pages: list[str], rotation: int = 0) -> Path:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((50, 60), text)
        page.set_rotation(rotation)
    doc.save(path)
    doc.close()
    return path


class Fixture(unittest.TestCase):
    def setUp(self):
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finanzen")
        self.lecture = self.subject.create_lecture("Woche 1")
        self.store = ProfessorStore(Path(tempfile.mkdtemp()))

    def view(self, **options):
        return subject_model.load_subject(self.subject, self.store, **options)


class ExportTests(Fixture):
    def test_every_copy_is_stamped_and_big_decks_go_out_in_parts(self):
        pages = [f"Seite {n}" for n in range(1, 8)]
        original = make_pdf(self.lecture.dir("slides") / "Großes Deck.pdf", pages)
        before = original.read_bytes()
        result = pdf_conversion.export_pdfs(self.view(), part_pages=3)
        names = sorted(p.name for p in result.files)
        self.assertEqual(names, ["SL01 Großes Deck (p1-3).pdf", "SL01 Großes Deck (p4-6).pdf", "SL01 Großes Deck (p7-7).pdf"])
        with pymupdf.open(result.folder / "SL01 Großes Deck (p4-6).pdf") as part:
            self.assertEqual(part.page_count, 3)
            self.assertIn("[[SL01 p.5]]", part[1].get_text())
            self.assertIn("Seite 5", part[1].get_text())
        self.assertEqual(original.read_bytes(), before)  # the student's PDF stays as it was
        self.assertIn("[[SL04 p.12]]", (result.folder / pdf_conversion.PROMPT_FILE).read_text(encoding="utf-8"))

    def test_a_small_deck_stays_whole(self):
        make_pdf(self.lecture.dir("slides") / "VL.pdf", ["a", "b"])
        result = pdf_conversion.export_pdfs(self.view())
        self.assertEqual([p.name for p in result.files], ["SL01 VL.pdf"])

    def test_the_stamp_reads_upright_in_the_corner_of_a_rotated_page(self):
        make_pdf(self.lecture.dir("slides") / "Quer.pdf", ["Text"], rotation=90)
        result = pdf_conversion.export_pdfs(self.view())
        with pymupdf.open(result.files[0]) as copy:
            page = copy[0]
            found = page.search_for("[[SL01 p.1]]")[0] * page.rotation_matrix  # search reports unrotated coordinates
            self.assertLess(found.y1, 30)  # in the top margin as the page is shown
            self.assertGreater(found.x0, page.rect.width / 2)

    def test_only_what_is_still_missing_goes_out(self):
        make_pdf(self.lecture.dir("slides") / "VL.pdf", ["eins", "zwei", "drei"])
        make_pdf(self.lecture.dir("slides") / "Fertig.pdf", ["fertig"])
        view = self.view()
        self.assertEqual([d.title for d in view.documents], ["Fertig.pdf", "VL.pdf"])
        pdf_conversion.import_converted(view, "[[SL01 p.1]]\nfertig\n[[SL02 p.1]]\neins")
        result = pdf_conversion.export_pdfs(self.view())
        self.assertEqual([p.name for p in result.files], ["SL02 VL (p2-3).pdf"])
        self.assertEqual(result.already_converted, 1)
        self.assertEqual(pdf_conversion.export_pdfs(self.view(), only_missing=False).already_converted, 0)

    def test_old_copies_are_cleared_and_excluded_documents_stay_out(self):
        make_pdf(self.lecture.dir("slides") / "Mathe.pdf", ["Integral"])
        make_pdf(self.lecture.dir("slides") / "VL.pdf", ["Aktiva"])
        first = pdf_conversion.export_pdfs(self.view())
        self.assertEqual(len(first.files), 2)
        view = self.view()
        self.subject.set_document_excluded(next(d.key for d in view.documents if d.title == "Mathe.pdf"), True)
        second = pdf_conversion.export_pdfs(self.view())
        self.assertEqual(len(second.files), 1)
        self.assertEqual(len([p for p in second.folder.iterdir() if p.suffix == ".pdf"]), 1)


class ParseTests(unittest.TestCase):
    def test_stamps_survive_what_tools_do_to_them(self):
        for marker in (
            "[[SL04 p.12]]",
            "\\[\\[SL04 p.12\\]\\]",
            "**[[SL04 p.12]]**",
            "## [[SL04 p.12]]",
            "[[ SL04 p. 12 ]]",
            "[[SL04 S.12]]",
            "[SL04 p.12]",
            "<!-- SL04 p.12 -->",
            "[[sl04, p12]]",
        ):
            with self.subTest(marker):
                pages, _ = parse_converted(f"{marker}\n\nHallo\n")
                self.assertEqual(pages, [(4, 12, "Hallo")])

    def test_pages_preamble_fence_and_continue_line(self):
        text = "```markdown\nGerne!\n\n[[SL04 p.1]]\n# A\n\n## [[SL04 p.2]]\n\n| x | y |\n[[CONTINUE AT SL04 p.3]]\n```"
        pages, preamble = parse_converted(text)
        self.assertEqual(pages, [(4, 1, "# A"), (4, 2, "| x | y |")])
        self.assertEqual(preamble, "Gerne!")

    def test_an_empty_page_is_a_page(self):
        pages, _ = parse_converted("[[SL04 p.1]]\n[[SL04 p.2]]\nText")
        self.assertEqual(pages, [(4, 1, ""), (4, 2, "Text")])

    def test_no_stamps_no_pages(self):
        pages, preamble = parse_converted("Just some text")
        self.assertEqual((pages, preamble), ([], "Just some text"))


class CheckTests(unittest.TestCase):
    RAW = "Bilanz Aktiva Passiva Anlagevermögen Umlaufvermögen 1.200,50 Euro"

    def test_a_faithful_conversion_passes(self):
        result = check_page(self.RAW, "## Bilanz\n\n| Aktiva | Passiva |\n\nAnlagevermögen, Umlaufvermögen: **1.200,50** Euro")
        self.assertTrue(result.ok)
        self.assertEqual(result.missing_numbers, ())

    def test_dropped_words_and_numbers_are_caught(self):
        self.assertLess(check_page(self.RAW, "Bilanz").coverage, 0.7)
        missing = check_page(self.RAW, "Bilanz Aktiva Passiva Anlagevermögen Umlaufvermögen Euro")
        self.assertEqual(missing.missing_numbers, ("120050",))
        self.assertFalse(missing.ok)
        self.assertIn("numbers missing", missing.describe())

    def test_invented_text_is_caught_but_figure_lines_are_allowed(self):
        honest = self.RAW + "\n[Figure: Balkendiagramm mit drei Säulen über mehrere Jahre hinweg]"
        self.assertTrue(check_page(self.RAW, honest).ok)
        invented = self.RAW + " " + " ".join(f"erfunden{chr(97 + i)}wort" for i in range(10))
        self.assertGreater(check_page(self.RAW, invented).added, 0.3)

    def test_hyphenation_and_latex_do_not_count_against_a_page(self):
        raw = "Abschrei-\nbung lineare Nutzungsdauer"
        self.assertTrue(check_page(raw, "Abschreibung lineare Nutzungsdauer $\\frac{a}{b}$").ok)

    def test_a_scan_cannot_be_checked(self):
        result = check_page("", "Was auch immer")
        self.assertTrue(result.scanned)
        self.assertTrue(result.ok)
        self.assertIn("scan", result.describe())

    def test_footer_numbers_and_the_page_number_may_go(self):
        common = pdf_conversion.common_numbers(["WS 2026 a", "WS 2026 b", "WS 2026 c", "WS 2026 d 7"])
        self.assertEqual(common, frozenset({"2026"}))
        self.assertEqual(pdf_conversion.printed_page_offset([f"Folie {n + 1}" for n in range(1, 7)]), 1)
        self.assertEqual(pdf_conversion.printed_page_offset(["Bilanz 1200", "Aktiva", "Passiva", "Umlauf 77"]), 0)
        self.assertTrue(check_page("Bilanz 8", "Bilanz", page=7, printed_offset=1).ok)
        self.assertFalse(check_page("Bilanz 9", "Bilanz", page=7, printed_offset=1).ok)
        self.assertTrue(check_page("Bilanz 2026 7", "Bilanz", page=7, common_numbers=common).ok)


class ImportTests(Fixture):
    def setUp(self):
        super().setUp()
        make_pdf(self.lecture.dir("slides") / "VL.pdf", ["Aktiva Passiva Bilanz", "Eigenkapital Fremdkapital", "Gewinn 1200"])

    def test_parts_merge_page_by_page_in_any_order(self):
        report = pdf_conversion.import_converted(self.view(), "[[SL01 p.3]]\nGewinn 1200")
        self.assertEqual(report.documents[0].imported, [3])
        self.assertEqual(report.documents[0].missing, [1, 2])
        self.assertIn("p.1-2 still missing", report.summary())
        self.assertIn("SL01 pages p.1-2", report.fix_request())
        report = pdf_conversion.import_converted(self.view(), "[[SL01 p.1]]\nAktiva Passiva Bilanz\n[[SL01 p.2]]\nEigenkapital Fremdkapital")
        self.assertEqual(report.documents[0].missing, [])
        self.assertIn("complete", report.summary())
        self.assertEqual(report.fix_request(), "")
        document = self.view().documents[0]
        self.assertTrue(document.is_converted)
        self.assertEqual(document.page(3).markdown, "Gewinn 1200")

    def test_a_better_conversion_replaces_a_page_and_the_rest_stays(self):
        pdf_conversion.import_converted(self.view(), "[[SL01 p.1]]\nalt\n[[SL01 p.2]]\nbleibt")
        pdf_conversion.import_converted(self.view(), "[[SL01 p.1]]\nAktiva Passiva Bilanz")
        document = self.view().documents[0]
        self.assertEqual(document.page(1).markdown, "Aktiva Passiva Bilanz")
        self.assertEqual(document.page(2).markdown, "bleibt")

    def test_weak_pages_are_reported_but_still_imported(self):
        report = pdf_conversion.import_converted(self.view(), "[[SL01 p.3]]\nGewinn")
        self.assertEqual(report.imported_pages, 1)
        self.assertEqual(report.documents[0].weak[0][0], 3)
        self.assertIn("1200", report.summary())

    def test_problems_are_named(self):
        view = self.view()
        self.assertIn("No page stamps", pdf_conversion.import_converted(view, "ohne alles").summary())
        self.assertIn("No document number 9", pdf_conversion.import_converted(view, "[[SL09 p.1]]\nx").summary())
        self.assertIn("has 3 pages", pdf_conversion.import_converted(view, "[[SL01 p.8]]\nx").summary())

    def test_files_are_read_by_their_stamps_not_their_names(self):
        path = Path(tempfile.mkdtemp()) / "irgendwas.md"
        path.write_text("[[SL01 p.2]]\nEigenkapital Fremdkapital", encoding="utf-8")
        report = pdf_conversion.import_files(self.view(), [path, path.with_name("fehlt.md")])
        self.assertEqual(report.imported_pages, 1)
        self.assertIn("fehlt.md", report.summary())

    def test_a_conversion_is_a_plain_file_that_round_trips(self):
        pdf_conversion.import_converted(self.view(), "[[SL01 p.1]]\n# Titel\n\n- Punkt")
        document = self.view().documents[0]
        path = pdf_conversion.converted_path(self.subject, document.key)
        self.assertEqual(path.parent.name, "converted")
        self.assertTrue(path.name.startswith("VL."))
        self.assertTrue(re.search(r"\[\[SL01 p\.1\]\]\n\n# Titel\n\n- Punkt", path.read_text(encoding="utf-8")))
        self.assertEqual(pdf_conversion.load_pages(self.subject, document.key), {1: "# Titel\n\n- Punkt"})

    def test_a_retagged_document_still_finds_its_conversion(self):
        pdf_conversion.import_converted(self.view(), "[[SL01 p.1]]\nAktiva Passiva Bilanz")
        self.lecture.documents()[0].set_tags(["Task"])
        document = self.view().documents[0]
        self.assertEqual(document.ref, "TK01")
        self.assertEqual(document.page(1).markdown, "Aktiva Passiva Bilanz")
        report = pdf_conversion.import_converted(self.view(), "[[SL01 p.2]]\nEigenkapital Fremdkapital")  # a stamp from before
        self.assertEqual(report.imported_pages, 1)


if __name__ == "__main__":
    unittest.main()
