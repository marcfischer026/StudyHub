"""Regression tests for UI findings."""
from __future__ import annotations
import os, sys, tempfile, threading, time, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import universe  # noqa
from PySide6.QtWidgets import QApplication
from universe.professors import ProfessorStore
from universe.storage import Workspace
from universe.ui.claude_panel import ClaudePanel
from universe.services.transcriber import Segment, Transcript
from universe.ui.transcript_panel import TranscriptPanel
from tests.test_claude_reply import build_subject
app = QApplication.instance() or QApplication([])
def wait_for(panel: ClaudePanel, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while panel._jobs:
        app.processEvents()
        if time.monotonic() > deadline:
            raise TimeoutError("worker did not finish")
        time.sleep(0.005)
    app.processEvents()
class Ui1ApiStuckTests(unittest.TestCase):
    def setUp(self):
        self.professors = ProfessorStore(Path(tempfile.mkdtemp()))
        self.ws = Workspace(Path(tempfile.mkdtemp()))
        self.subject_a = self.ws.create_subject("FinanzenA")
        self.lecture_a = self.subject_a.create_lecture("Woche 1")
        self.subject_b = self.ws.create_subject("PhysikB")
        self.panel = ClaudePanel(self.professors)
        self.addCleanup(self.panel.deleteLater)
        self.panel.set_scope(self.subject_a, self.lecture_a)
    def test_api_card_resets_when_subject_switches_mid_run(self):
        started = threading.Event()
        release = threading.Event()
        def job():
            started.set()
            release.wait(5)
            return 42
        results: list = []
        self.panel._set_api_running(True)
        self.assertFalse(self.panel.api_run_btn.isEnabled())
        self.panel._run("Working…", job, results.append)
        self.assertTrue(started.wait(5))
        self.panel.set_scope(self.subject_b, None)
        release.set()
        wait_for(self.panel)
        self.assertEqual(results, [])
        self.assertTrue(self.panel.api_run_btn.isEnabled())
        self.assertTrue(self.panel.api_stop_btn.isHidden())
class Ui2AllSaveTests(unittest.TestCase):
    def setUp(self):
        self.professors = ProfessorStore(Path(tempfile.mkdtemp()))
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finanzen")
        self.lecture = self.subject.create_lecture("Woche 1")
        build_subject(self.subject, self.lecture)
        self.panel = TranscriptPanel()
        self.addCleanup(self.panel.deleteLater)
        self.panel.set_lecture(self.lecture)
    def test_second_label_saved_when_first_was_retranscribed(self):
        # All recordings shows both transcripts
        self.assertEqual(self.panel.files.itemText(0), "All recordings (in order)")
        self.panel.files.setCurrentIndex(0)
        items = self.panel._segment_items()
        # collect one item per part via ROLE
        from PySide6.QtCore import Qt as _Qt
        ROLE = _Qt.ItemDataRole.UserRole
        first_of_each: dict[int, object] = {}
        for it in items:
            part = it.data(ROLE)[3]
            first_of_each.setdefault(part, it)
        self.assertEqual(set(first_of_each), {0, 1})
        # background re-transcribe of the first file changes its count
        first_path = self.panel.parts[0][0]
        Transcript("Woche.m4a", [Segment(1.0, 2.0, "Ganz neu")], id="r_one").save(first_path.parent)
        both = [first_of_each[0], first_of_each[1]]
        self.panel._set(both, category="question")
        second_path = self.lecture.dir("transcripts") / "Zweite.json"
        saved_second = Transcript.load(second_path)
        self.assertEqual(saved_second.segments[0].category, "question")
class Ui3MidpointTests(unittest.TestCase):
    def setUp(self):
        self.professors = ProfessorStore(Path(tempfile.mkdtemp()))
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finanzen")
        self.lecture = self.subject.create_lecture("Woche 1")
        build_subject(self.subject, self.lecture)
        self.panel = TranscriptPanel()
        self.addCleanup(self.panel.deleteLater)
        self.panel.set_lecture(self.lecture)
        self.panel.files.setCurrentIndex(self.panel.files.findText("Woche"))
        self.path = self.lecture.dir("transcripts") / "Woche.json"
    def test_shifted_times_keep_manual_label_via_midpoint(self):
        shown = Transcript.load(self.path)
        shifted = Transcript(shown.audio, [
            Segment(s.start + 1.0, s.end + 1.0, s.text, speaker=s.speaker) for s in shown.segments
        ], id=shown.id)
        shifted.save(self.path.parent)
        first = self.panel._segment_items()[0]
        old_mid = (shown.segments[0].start + shown.segments[0].end) / 2
        # sanity: old midpoint lies inside the shifted first segment
        self.assertTrue(shifted.segments[0].start <= old_mid < shifted.segments[0].end)
        self.panel._set([first], category="question")
        saved = Transcript.load(self.path)
        self.assertEqual(saved.segments[0].category, "question")
        self.assertEqual(saved.segments[0].source, "manual")
        self.assertEqual(self.panel.parts[0][1].segments[0].category, "question")
    def test_label_without_anchor_warns_through_status(self):
        shown = Transcript.load(self.path)
        far = Transcript(shown.audio, [
            Segment(5000.0 + i * 10.0, 5005.0 + i * 10.0, s.text) for i, s in enumerate(shown.segments)
        ], id=shown.id)
        far.save(self.path.parent)
        messages: list[str] = []
        # TranscriptPanel emits status when a label cannot be kept
        try:
            self.panel.status.connect(messages.append)
        except Exception:
            self.fail("TranscriptPanel has no status signal")
        first = self.panel._segment_items()[0]
        self.panel._set([first], category="question")
        saved = Transcript.load(self.path)
        self.assertEqual(saved.segments[0].category, "")
        self.assertTrue(messages, "expected a status warning when the anchor is gone")
class Ui5SearchStaleTests(unittest.TestCase):
    def setUp(self):
        import uuid
        from unittest import mock
        from universe.config import Settings
        from universe.ui.main_window import MainWindow
        for name in ("QMessageBox",):
            p = mock.patch(f"universe.ui.main_window.{name}")
            p.start()
            self.addCleanup(p.stop)
        idx = mock.patch.object(MainWindow, "_run_index")
        idx.start()
        self.addCleanup(idx.stop)
        self.window = MainWindow(Settings())
        self.addCleanup(self.window.deleteLater)
        tag = uuid.uuid4().hex[:6]
        self.subject_a = self.window.workspace.create_subject(f"Finanzen{tag}")
        self.subject_b = self.window.workspace.create_subject(f"Physik{tag}")
        self.window.subject = self.subject_a
        self.window.ai.set_scope(self.subject_a, None)
    def test_search_result_from_old_subject_is_dropped(self):
        import threading, time
        from unittest import mock
        from universe.services.vector_index import Hit
        hits_a = [Hit("Woche 1", "transcript", "Woche [▶ 00:12:31]", "Bilanz Inhalt", 0.9)]
        started = threading.Event()
        release = threading.Event()
        def fake_search(self, query, limit=12, lecture=None):
            started.set()
            release.wait(5)
            return hits_a
        with mock.patch("universe.ui.main_window.SubjectIndex.search", fake_search):
            self.window.search("Bilanz", False)
            self.assertTrue(started.wait(5))
            self.window.subject = self.subject_b
            self.window.ai.set_scope(self.subject_b, None)
            release.set()
            deadline = time.monotonic() + 10
            while self.window._keep_alive:
                app.processEvents()
                if time.monotonic() > deadline:
                    break
                time.sleep(0.005)
            app.processEvents()
        texts = [self.window.ai.results.item(i).text() for i in range(self.window.ai.results.count())]
        self.assertFalse(any("Bilanz Inhalt" in t for t in texts))
class Ui9SearchClearTests(unittest.TestCase):
    def setUp(self):
        import uuid
        from universe.professors import ProfessorStore
        from universe.ui.ai_panel import AIPanel
        self.professors = ProfessorStore(Path(tempfile.mkdtemp()))
        self.ws = Workspace(Path(tempfile.mkdtemp()))
        self.fa = self.ws.create_subject(f"Finanzen{uuid.uuid4().hex[:4]}")
        self.pb = self.ws.create_subject(f"Physik{uuid.uuid4().hex[:4]}")
        self.panel = AIPanel(self.professors)
        self.addCleanup(self.panel.deleteLater)
    def test_results_cleared_on_subject_change(self):
        from universe.services.vector_index import Hit
        self.panel.set_scope(self.fa, None)
        self.panel.show_results([Hit("Woche 1", "transcript", "Woche [▶ 00:12:31]", "Bilanz", 0.9) for _ in range(5)])
        self.assertEqual(self.panel.results.count(), 5)
        self.panel.set_scope(self.pb, None)
        self.assertEqual(self.panel.results.count(), 0)
class Ui6ScrollDebounceTests(unittest.TestCase):
    def test_scrolled_past_pages_do_not_emit_until_settled(self):
        import time
        import pymupdf
        from PySide6.QtTest import QTest
        from universe.ui.pdf_viewer import PdfViewer
        pdf_path = Path(tempfile.mkdtemp()) / "many.pdf"
        doc = pymupdf.open()
        for _ in range(20):
            doc.new_page()
        doc.save(pdf_path)
        doc.close()
        viewer = PdfViewer()
        self.addCleanup(viewer.deleteLater)
        viewer.resize(400, 300)
        viewer.show()
        viewer.open_file(pdf_path)
        app.processEvents()
        QTest.qWait(100)
        app.processEvents()
        viewer.go_to_page(1)
        app.processEvents()
        seen: list[int] = []
        viewer.pageChanged.connect(seen.append)
        seen.clear()
        # fast drag from p1 to p20: every passed page would emit before the fix
        for p in (5, 10, 15, 20):
            label = viewer.page_labels[p - 1]
            viewer.scroll.verticalScrollBar().setValue(label.y() - 12)
            app.processEvents()
        # intermediates must not have emitted at once; only the settled page may emit after 1.5s
        self.assertEqual(seen, [], f"scrolled-past pages emitted at once: {seen}")
        QTest.qWait(1700)
        app.processEvents()
        self.assertTrue(seen, "settled page never emitted")
        self.assertEqual(seen[-1], viewer.page.value())
        # programmatic jumps still emit at once
        seen.clear()
        viewer.go_to_page(2)
        app.processEvents()
        self.assertIn(2, seen)
class Ui7MenuRefreshTests(unittest.TestCase):
    def setUp(self):
        self.professors = ProfessorStore(Path(tempfile.mkdtemp()))
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finanzen")
        self.lecture = self.subject.create_lecture("Woche 1")
        build_subject(self.subject, self.lecture)
        self.panel = TranscriptPanel()
        self.addCleanup(self.panel.deleteLater)
        self.panel.set_lecture(self.lecture)
        self.panel.files.setCurrentIndex(self.panel.files.findText("Woche"))
        self.path = self.lecture.dir("transcripts") / "Woche.json"
    def test_label_menu_survives_background_refresh(self):
        from unittest import mock
        from PySide6.QtCore import QPoint
        first = self.panel._segment_items()[0]
        first.setSelected(True)
        calls: list = []
        panel = self.panel
        class FakeMenu:
            def __init__(self, *a, **k):
                self.actions: list = []
            def addAction(self, text, slot=None):
                if callable(slot):
                    calls.append((text, slot))
                return None
            def addSeparator(self):
                pass
            def exec(self, *a, **k):
                # background Sort/Transcribe finishes and refreshes while the menu is open
                panel._show_selected()
                # student then picks the first action (exam-relevant)
                calls[0][1]()
        with mock.patch("universe.ui.transcript_panel.QMenu", FakeMenu):
            panel._context_menu(QPoint(5, 5))
        saved = Transcript.load(self.path)
        self.assertTrue(saved.segments[0].exam)
class Ui8GuideLinkTests(unittest.TestCase):
    def setUp(self):
        from tests.test_ai_panels import FULL_REPLY
        from universe.services.claude_reply import check_reply, parse_reply
        from universe.services import subject_model
        from universe.ui.guide_panel import GuidePanel
        self.professors = ProfessorStore(Path(tempfile.mkdtemp()))
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finanzen")
        self.lecture = self.subject.create_lecture("Woche 1")
        build_subject(self.subject, self.lecture)
        from universe.services.study_store import StudyStore
        self.store = StudyStore(self.subject)
        view = subject_model.load_subject(self.subject, self.professors)
        self.store.merge(check_reply(parse_reply(FULL_REPLY), view, self.store.known_ids()), FULL_REPLY, view)
        self.panel = GuidePanel(self.professors)
        self.addCleanup(self.panel.deleteLater)
        self.panel.set_scope(self.subject, self.lecture)
        self.panel.refresh()
    def test_malformed_guide_links_are_ignored(self):
        from PySide6.QtCore import QUrl
        bad = ["accept:prof:99", "accept:prof:x", "dismiss:doc:99", "accept:prof:", "accept:doc:x", "dismiss:prof:99"]
        for href in bad:
            with self.subTest(href=href):
                try:
                    self.panel.open_link(QUrl(href))
                except Exception as exc:
                    self.fail(f"{href} raised {exc!r}")
class Ui10WatchSubjectTests(unittest.TestCase):
    def test_imported_and_own_cleared_on_subject_change(self):
        panel = ClaudePanel(ProfessorStore(Path(tempfile.mkdtemp())))
        self.addCleanup(panel.deleteLater)
        ws = Workspace(Path(tempfile.mkdtemp()))
        sub_a = ws.create_subject("FinanzenA")
        lec_a = sub_a.create_lecture("Woche 1")
        sub_b = ws.create_subject("PhysikB")
        panel.set_scope(sub_a, lec_a)
        panel._imported.add("reply-T")
        panel._own_copies.add("own-T")
        panel.set_scope(sub_b, None)
        self.assertEqual(panel._imported, set())
        self.assertEqual(panel._own_copies, set())
class Ui12HeadingShiftTests(unittest.TestCase):
    def test_markdown_headings_shifted_down_two(self):
        from universe.ui.guide_panel import markdown_html
        html = markdown_html("# Definition\n\nText\n")
        self.assertNotIn("<h1", html)
        self.assertIn("<h3", html)
        html2 = markdown_html("##### Deep\n")
        self.assertIn("<h6", html2)
        fenced = markdown_html("```\n# kept\n```\n")
        self.assertIn("# kept", fenced)
class Ui13SourcesTests(unittest.TestCase):
    def test_sources_deduplicated_keeping_first(self):
        from universe.professors import ProfessorStore
        from universe.ui.guide_panel import GuidePanel
        panel = GuidePanel(ProfessorStore(Path(tempfile.mkdtemp())))
        self.addCleanup(panel.deleteLater)
        out = panel._sources([
            {"label": "SL01.p7", "kind": "page"},
            {"label": "SL01.p7", "kind": "page"},
            {"label": "SL01.p7", "kind": "page"},
        ])
        self.assertEqual(out.count("SL01.p7"), 1)
        self.assertEqual(len(panel.links), 1)
class Ui14FixedTextTests(unittest.TestCase):
    def test_a_fixed_text_gets_the_lines_it_needs_in_a_narrow_dock(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QVBoxLayout, QWidget
        from universe.ui.claude_panel import card, text_label
        label = text_label("Gemini or Copilot turn the PDFs into Markdown, so Claude reads tables and graphics too.", muted=True)
        dock = QWidget()
        self.addCleanup(dock.deleteLater)
        column = QVBoxLayout(dock)
        column.addWidget(card("1 · PDFs to Markdown", label))
        column.addStretch(1)  # as in the tab: spare height goes below the cards
        dock.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        dock.resize(220, 600)
        dock.show()
        app.processEvents()
        needed = label.heightForWidth(label.width())
        self.assertGreater(needed, label.sizeHint().height())  # the size hint guesses fewer lines at this width
        self.assertGreaterEqual(label.height(), needed)
if __name__ == "__main__":
    unittest.main()
