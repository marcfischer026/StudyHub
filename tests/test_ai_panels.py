"""Tests for the study tabs, driven through their widgets, and what the main window does around them."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import universe  # noqa: E402,F401
from universe.config import Settings  # noqa: E402
from universe.ui.main_window import MainWindow  # noqa: E402  (before PySide6: lancedb fails to import after it on Python 3.15)

from PySide6.QtCore import QUrl  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from tests.test_claude_reply import END, HEAD, MC, OBJECTIVE, PASSIV, TOPIC, build_subject, jsonl  # noqa: E402
from universe.professors import ProfessorProfile, ProfessorStore  # noqa: E402
from universe.services import pdf_conversion, relevance, subject_model  # noqa: E402
from universe.services.claude_reply import check_reply, parse_reply  # noqa: E402
from universe.services.study_store import StudyStore  # noqa: E402
from universe.services.transcriber import Segment, Transcript  # noqa: E402
from universe.services.vector_index import Hit  # noqa: E402
from universe.storage import Workspace, read_slide_marks  # noqa: E402
from universe.ui import claude_panel  # noqa: E402
from universe.ui.ai_panel import AIPanel, hit_target  # noqa: E402
from universe.ui.claude_panel import ClaudePanel, DocumentsDialog, FolderLine  # noqa: E402
from universe.ui.guide_panel import GuidePanel, link_citations  # noqa: E402
from universe.ui.practice_panel import PracticePanel  # noqa: E402
from universe.ui.progress_panel import ExamForm, ProgressPanel, exam_event  # noqa: E402
from universe.services import moodle_ical  # noqa: E402

app = QApplication.instance() or QApplication([])

SRC = [{"id": "SL01.p1", "q": PASSIV}]
OBJECTIVE_2 = {**OBJECTIVE, "id": "L01-o002", "statement": "Aktivseite erklären", "src": [{"id": "SL01.p2", "q": "Die Aktivseite zeigt die Mittelverwendung"}]}
TF = {"t": "q", "id": "q_tf", "objective": "L01-o002", "type": "tf", "stem": "Die Aktivseite zeigt die Mittelverwendung.", "answer": "True", "explain": "Folie 2", "src": SRC}
SHORT = {
    "t": "q", "id": "q_short", "objective": "L01-o001", "type": "short", "stem": "Erklären Sie die Passivseite.", "answer": "Sie zeigt die Mittelherkunft.",
    "rubric": [{"point": "Mittelherkunft genannt", "marks": 1}, {"point": "Beispiel genannt", "marks": 3}], "src": SRC,
}  # fmt: skip
CALC = {
    "t": "q", "id": "q_calc", "objective": "L01-o002", "type": "calc", "stem": "Was ist 600 + 600?", "answer": "1200", "numeric": {"value": 1200, "tolerance": 0.5, "unit": "€"},
    "steps": ["600 + 600 = 1200"], "hints": ["Addieren", "Zweimal 600"], "src": SRC,
}  # fmt: skip
CARD = {"t": "q", "id": "q_card", "objective": "L01-o002", "type": "card", "stem": "Vorderseite", "answer": "Rückseite", "src": SRC}
GUIDE = {"t": "guide", "topic": "t_b", "md": "# Bilanz\n\nDie **Passivseite** zeigt die Herkunft [SL01.p1, R01@751].\n\nUnbekannt [Quelle]."}
SAID = {"t": "said", "id": "s_01", "lecture": "L01", "kind": "exam_hint", "text": "Kommt in der Klausur dran", "src": [{"id": "R01@797", "q": "Das kommt in der Klausur dran"}]}
CONFLICT = {"t": "conflict", "id": "c_01", "a": "SL01.p1", "b": "R01@751", "issue": "Folie und Vortrag weichen ab", "working": "a", "needs_student_check": True}
NOTE_FIX = {"t": "note_fix", "lecture": "L01", "line": "N01.1", "verdict": "incomplete", "fix": "Aktiv und Passiv nennen", "src": SRC}
PROF = {"t": "prof", "prof": "PR1", "field": "focus_areas", "value": "Bilanzen", "src": SRC}
DOC_NOTE = {"t": "doc_note", "doc": "EX02", "issue": "wrong_subject", "detail": "Anderes Fach"}
COVERAGE = {"t": "coverage", "lecture": "L01", "covered": ["SL01.p1-4", "R01", "R02"], "uncovered": []}
FULL_REPLY = jsonl(TOPIC, OBJECTIVE, OBJECTIVE_2, MC, TF, SHORT, CALC, CARD, GUIDE, SAID, CONFLICT, NOTE_FIX, PROF, DOC_NOTE, COVERAGE)


def wait_for(panel: ClaudePanel) -> None:
    """Until the worker is done and its result has reached the UI thread."""
    deadline = time.monotonic() + 60
    while panel._jobs:
        app.processEvents()
        if time.monotonic() > deadline:
            raise TimeoutError("worker did not finish")
        time.sleep(0.005)
    app.processEvents()


class Base(unittest.TestCase):
    def setUp(self):
        self.professors = ProfessorStore(Path(tempfile.mkdtemp()))
        self.profile = ProfessorProfile("Muster", testing_style="Eigener Text")
        self.professors.save(self.profile)
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finanzen", professor_id=self.profile.id)
        self.lecture = self.subject.create_lecture("Woche 1")
        build_subject(self.subject, self.lecture)
        self.store = StudyStore(self.subject)
        patcher = mock.patch.object(claude_panel, "reveal")
        self.reveal = patcher.start()
        self.addCleanup(patcher.stop)

    def view(self):
        return subject_model.load_subject(self.subject, self.professors)

    def seed(self, reply: str = FULL_REPLY) -> None:
        view = self.view()
        self.store.merge(check_reply(parse_reply(reply), view, self.store.known_ids()), reply, view)

    def attempts(self) -> list[dict]:
        return [e for e in self.store.events() if e["type"] == "attempt"]


class ClaudeTabTests(Base):
    def setUp(self):
        super().setUp()
        self.panel = ClaudePanel(self.professors)
        self.addCleanup(self.panel.deleteLater)
        self.panel.set_scope(self.subject, self.lecture)
        self.panel.refresh()
        wait_for(self.panel)

    def clipboard(self) -> str:
        return QGuiApplication.clipboard().text()

    def test_status_and_plan_with_only_a_subject_selected(self):
        panel = AIPanel(self.professors)
        self.addCleanup(panel.deleteLater)
        panel.set_scope(self.subject, None)
        self.assertTrue(panel.tabs.isEnabled())
        self.assertFalse(panel.claude.scope.model().item(2).isEnabled())  # "This lecture" needs one
        panel.claude.refresh()
        wait_for(panel.claude)
        self.assertIn("0 of 2 PDFs converted", panel.claude.pdf_status.text())
        self.assertIn("L01", panel.claude.plan_label.text())
        self.assertIn("fits one chat", panel.claude.plan_label.text())
        panel.claude.export()
        wait_for(panel.claude)
        self.assertTrue(panel.claude.result.attach)

    def test_no_subject_disables_the_tabs(self):
        panel = AIPanel(self.professors)
        self.addCleanup(panel.deleteLater)
        panel.set_scope(None, None)
        self.assertFalse(panel.tabs.isEnabled())

    def test_this_lecture_falls_back_when_the_lecture_closes(self):
        self.panel.scope.setCurrentIndex(2)
        wait_for(self.panel)
        self.panel.set_scope(self.subject, None)
        wait_for(self.panel)
        self.assertEqual(self.panel.scope.currentData(), "next")

    def test_scope_this_lecture_plans_that_lecture(self):
        self.panel.scope.setCurrentIndex(2)
        wait_for(self.panel)
        self.assertEqual([v.name for v in self.panel.plan.lectures], ["Woche 1"])
        self.panel.size_choice.setCurrentIndex(1)
        wait_for(self.panel)
        self.assertEqual(self.panel.plan.budget, claude_panel.LONG_BUDGET)

    def test_pdf_export_copies_the_prompt_and_reveals_the_folder(self):
        self.panel.export_pdfs()
        wait_for(self.panel)
        self.assertEqual(self.clipboard(), pdf_conversion.PROMPT)
        folder = self.subject.claude_dir / pdf_conversion.EXPORT_FOLDER
        self.reveal.assert_called_with(folder)
        self.assertTrue(list(folder.glob("SL01*.pdf")))
        self.assertIn("Wrote 2 PDF files", self.panel.pdf_report.text())
        self.assertEqual(self.panel.pdf_folder.where.text(), f"Saved in {claude_panel.shown_path(folder)}")
        self.panel.pdf_folder.copy_path()
        self.assertEqual(self.clipboard(), str(folder))

    def test_paste_conversion_shows_the_report_and_asks_for_what_is_missing(self):
        imported = []
        self.panel.conversionsImported.connect(lambda: imported.append(True))
        QGuiApplication.clipboard().setText("[[SL01 p.1]]\nDie Passivseite zeigt die Mittelherkunft\n")
        self.panel.paste_conversion()
        wait_for(self.panel)
        self.assertIn("SL01 VL.pdf: 1 page(s) imported", self.panel.pdf_report.text())
        self.assertFalse(self.panel.copy_missing_btn.isHidden())
        self.assertEqual(imported, [True])
        self.panel.copy_missing()
        self.assertIn("SL01 pages p.2-4", self.clipboard())
        self.panel.refresh()
        wait_for(self.panel)
        self.assertIn("1 of 5 pages", self.panel.pdf_status.text())

    def test_convert_again_offers_the_pages_to_check(self):
        with mock.patch.object(claude_panel.local_conversion, "installed", return_value=True):
            self.assertFalse(self.panel.redo_local_btn.isEnabled())  # nothing converted yet
            QGuiApplication.clipboard().setText("[[SL01 p.1]]\nBilanz\n")
            self.panel.paste_conversion()
            wait_for(self.panel)
            self.panel.refresh()
            wait_for(self.panel)
            self.assertTrue(self.panel.redo_local_btn.isEnabled())
            self.assertEqual(self.panel.redo_weak_action.text(), "Pages to check (1)")
            with mock.patch.object(claude_panel.local_conversion, "convert_subject", return_value=pdf_conversion.ConversionReport()) as convert:
                self.panel.redo_weak_action.trigger()
                wait_for(self.panel)
        self.assertEqual(convert.call_args.args[3], claude_panel.local_conversion.WEAK)

    def test_paste_without_stamps_says_why(self):
        QGuiApplication.clipboard().setText("Einfach nur Text")
        self.panel.paste_conversion()
        wait_for(self.panel)
        self.assertIn("No page stamps", self.panel.pdf_report.text())
        self.assertTrue(self.panel.copy_missing_btn.isHidden())

    def test_import_md_files(self):
        path = Path(tempfile.mkdtemp()) / "x.md"
        path.write_text("[[EX02 p.1]]\nAufgabe eins Erstellen Sie eine Bilanz\n", encoding="utf-8")
        self.panel.import_conversion_files([path])
        wait_for(self.panel)
        self.assertIn("EX02", self.panel.pdf_report.text())
        self.assertIn("complete", self.panel.pdf_report.text())

    def test_documents_dialog_excludes_and_opens(self):
        dialog = DocumentsDialog(self.view())
        self.addCleanup(dialog.deleteLater)
        pages = []
        dialog.pageRequested.connect(pages.append)
        self.assertEqual(dialog.table.rowCount(), 2)
        dialog.boxes[1].setChecked(True)
        self.assertIn(dialog.view.documents[1].key, self.subject.excluded_documents)
        dialog.table.cellDoubleClicked.emit(0, 1)
        self.assertEqual(pages, [{"kind": "page", "doc": dialog.view.documents[0].key, "page": 1}])

    def test_export_writes_files_copies_prompt_and_says_what_to_do(self):
        self.panel.export()
        wait_for(self.panel)
        result = self.panel.result
        self.assertTrue(all(path.exists() and path.suffix == ".md" for path in result.attach))
        self.assertEqual(self.clipboard(), result.prompt)
        self.reveal.assert_called_with(result.attach[0].parent)
        self.assertEqual(self.panel.export_done.text(), f"Drag the {len(result.attach)} files into Claude, then paste the prompt.")
        self.assertFalse(self.panel.export_actions.isHidden())
        self.assertFalse(self.panel.export_folder.isHidden())
        self.assertEqual(self.panel.export_folder.folder, result.attach[0].parent)
        self.panel.copy_all()
        self.assertIn("<documents>", self.clipboard())
        self.panel.copy_prompt()
        self.assertEqual(self.clipboard(), result.prompt)

    def test_side_talk_box_stays_hidden_without_side_talk(self):
        self.assertTrue(self.panel.side_talk.isHidden())

    def test_side_talk_box_shows_its_count_and_goes_along_when_ticked(self):
        Transcript("Stimmen.m4a", [Segment(0.0, 2.0, "Hast du Zeit?", category="off_topic", source="local", speaker="speaker_2")], id="r_v").save(self.lecture.dir("transcripts"))
        self.panel.refresh()
        wait_for(self.panel)
        self.assertFalse(self.panel.side_talk.isHidden())
        self.assertIn("Also send 1 line of", self.panel.side_talk.label.text())
        self.assertFalse(self.panel.side_talk.box.isChecked())
        self.panel.export()
        wait_for(self.panel)
        self.assertFalse(any("Side talk" in path.name for path in self.panel.result.attach))
        self.panel.side_talk.label.mousePressEvent(None)  # the text is part of the checkbox
        wait_for(self.panel)
        self.panel.export()
        wait_for(self.panel)
        self.assertTrue(any("Side talk" in path.name for path in self.panel.result.attach))

    def test_paste_import_reports_counts_and_can_be_undone(self):
        imported = []
        self.panel.replyImported.connect(lambda: imported.append(True))
        QGuiApplication.clipboard().setText(FULL_REPLY)
        self.panel.paste_reply()
        wait_for(self.panel)
        report = self.panel.reply_report.text()
        self.assertIn("the last part has arrived", report)
        self.assertIn("Added:", report)
        self.assertIn("5 questions", report)
        self.assertEqual(imported, [True])
        self.assertTrue(self.fix_hidden())
        self.assertEqual(len(self.store.knowledge().questions), 5)
        self.assertTrue(self.panel.undo_btn.isEnabled())
        with mock.patch.object(claude_panel.QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as asked:
            self.panel.undo_import()
        asked.assert_called_once()
        self.assertEqual(len(self.store.knowledge().questions), 5)  # the student said no
        with mock.patch.object(claude_panel.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.panel.undo_import()
        wait_for(self.panel)
        self.assertEqual(self.store.knowledge().questions, {})
        self.assertIn("undone", self.panel.reply_report.text())

    def fix_hidden(self) -> bool:
        return self.panel.fix_btn.isHidden()

    def test_a_broken_record_offers_a_fix_request_and_the_rest_imports(self):
        broken = {**MC, "id": "q_bad", "options": [{"text": "nur eine", "correct": True}]}
        reply = jsonl(TOPIC, OBJECTIVE, MC, broken)
        self.panel.import_reply(reply)
        wait_for(self.panel)
        self.assertIn("could not be used", self.panel.reply_report.text())
        self.assertFalse(self.fix_hidden())
        self.assertIn("q_1", self.store.knowledge().questions)
        self.panel.copy_fix()
        self.assertIn("q_bad", self.clipboard())

    def test_a_cut_off_reply_says_so(self):
        self.panel.import_reply(jsonl(TOPIC, OBJECTIVE, end=None))
        wait_for(self.panel)
        self.assertIn("cut off", self.panel.reply_report.text())
        self.assertFalse(self.fix_hidden())

    def test_missing_pages_offer_a_follow_up(self):
        self.panel.import_reply(jsonl(TOPIC, OBJECTIVE, {**COVERAGE, "covered": ["SL01.p1"]}))
        wait_for(self.panel)
        self.assertIn("not covered", self.panel.reply_report.text())
        self.assertFalse(self.panel.follow_up_btn.isHidden())
        self.panel.copy_follow_up()
        self.assertIn("SL01.p2", self.clipboard())

    def test_text_without_records_changes_nothing(self):
        self.panel.import_reply("Sorry, I cannot do that.")
        wait_for(self.panel)
        self.assertIn("Nothing in this looks like a reply", self.panel.reply_report.text())
        self.assertEqual(self.store.knowledge().questions, {})

    def test_import_a_folder_of_parts(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "part-01.jsonl").write_text(jsonl(TOPIC, OBJECTIVE, end={**END, "more": True}), encoding="utf-8")
        (folder / "part-02.jsonl").write_text(jsonl(MC, head={**HEAD, "part": 2}), encoding="utf-8")
        self.panel.import_reply_paths([folder])
        wait_for(self.panel)
        self.assertIn("Parts 1, 2", self.panel.reply_report.text())
        self.assertIn("q_1", self.store.knowledge().questions)

    def test_watch_clipboard_imports_a_copied_part_once(self):
        QGuiApplication.clipboard().setText(jsonl(TOPIC, head=HEAD, end=END))  # there before ticking: not imported
        self.panel.watch.box.setChecked(True)
        self.panel.check_clipboard()
        wait_for(self.panel)
        self.assertEqual(self.store.knowledge().topics, {})

        part_one = jsonl(TOPIC, OBJECTIVE, end={**END, "more": True})
        with mock.patch.object(self.panel, "import_reply", wraps=self.panel.import_reply) as imported:
            QGuiApplication.clipboard().setText(part_one)
            self.panel.check_clipboard()
            wait_for(self.panel)
            self.panel.check_clipboard()
            wait_for(self.panel)
            QGuiApplication.clipboard().setText("something else")
            self.panel.check_clipboard()
            QGuiApplication.clipboard().setText(part_one)  # copied again: still one import
            self.panel.check_clipboard()
            wait_for(self.panel)
            self.assertEqual(imported.call_count, 1)
            self.assertIn("more to come", self.panel.reply_report.text())

            QGuiApplication.clipboard().setText(jsonl(MC, head={**HEAD, "part": 2}))
            self.panel.check_clipboard()
            wait_for(self.panel)
            self.assertEqual(imported.call_count, 2)
        self.assertIn("Parts 1, 2", self.panel.reply_report.text())
        self.assertIn("q_1", self.store.knowledge().questions)
        self.panel.watch.box.setChecked(False)
        self.assertFalse(self.panel._watch.isActive())

    def test_watching_does_not_take_the_prompt_for_a_reply(self):
        self.panel.export()
        wait_for(self.panel)
        self.panel.watch.box.setChecked(True)
        self.addCleanup(self.panel.watch.box.setChecked, False)
        QGuiApplication.clipboard().setText("x")
        self.panel.check_clipboard()
        with mock.patch.object(self.panel, "import_reply") as imported:
            self.panel.copy_prompt()
            self.panel.check_clipboard()
            QGuiApplication.clipboard().setText("y")
            self.panel.check_clipboard()
            QGuiApplication.clipboard().setText(self.panel.result.prompt)  # even when it comes back from elsewhere
            self.panel.check_clipboard()
            imported.assert_not_called()

    def test_tutor_buttons_wait_for_knowledge_then_copy_prompts(self):
        self.assertFalse(self.panel.tutor_buttons[0].isEnabled())
        self.seed()
        self.panel.refresh()
        wait_for(self.panel)
        self.assertTrue(self.panel.tutor_buttons[0].isEnabled())
        self.panel.tutor_buttons[0].click()
        wait_for(self.panel)
        self.assertIn("L01-o001", self.clipboard())
        self.panel.tutor_buttons[1].click()
        wait_for(self.panel)
        self.assertIn("one question at a time", self.clipboard().lower())

    def test_a_failure_is_shown_not_swallowed(self):
        with mock.patch.object(claude_panel.claude_export, "write_export", side_effect=ValueError("disk full")), mock.patch.object(claude_panel.QMessageBox, "warning") as warned:
            self.panel.export()
            wait_for(self.panel)
        self.assertIn("disk full", warned.call_args[0][2])

    def test_a_result_for_the_previous_subject_is_dropped(self):
        other = Workspace(self.subject.path.parent).create_subject("Anderes")
        self.panel.export()
        self.panel.set_scope(other, None)
        wait_for(self.panel)
        self.assertIsNone(self.panel.result)

    def test_another_subject_hides_where_the_last_export_went(self):
        self.panel.export_pdfs()
        wait_for(self.panel)
        self.assertFalse(self.panel.pdf_folder.isHidden())
        self.panel.set_scope(Workspace(self.subject.path.parent).create_subject("Anderes"), None)
        self.assertTrue(self.panel.pdf_folder.isHidden())

    def test_the_notes_file_stays_untouched(self):
        path = next(self.lecture.path.glob("notes*"))
        raw = path.read_bytes()
        self.panel.import_reply(FULL_REPLY)
        wait_for(self.panel)
        self.assertEqual(path.read_bytes(), raw)

    def test_opening_a_lecture_does_not_read_the_subject_again(self):
        with mock.patch.object(self.panel, "_load") as load:
            self.panel.set_scope(self.subject, None)
            self.panel.set_scope(self.subject, self.lecture)
            wait_for(self.panel)
        load.assert_not_called()
        self.assertIsNotNone(self.panel.view)


class FolderLineTests(unittest.TestCase):
    DOT_FOLDER = Path.home() / ".universe_data" / "Subjects" / "Zynth" / "claude" / "PDFs to convert"

    def line(self, copy=None) -> FolderLine:
        line = FolderLine(copy or (lambda text, what: None))
        self.addCleanup(line.deleteLater)
        return line

    def test_hidden_and_inert_until_an_export(self):
        copied = []
        line = self.line(lambda text, what: copied.append(text))
        with mock.patch.object(claude_panel, "reveal") as reveal:
            line.copy_path()
            line.open_folder()
        self.assertTrue(line.isHidden())
        self.assertEqual(copied, [])
        reveal.assert_not_called()

    def test_a_folder_inside_a_dot_folder_says_how_to_reach_it(self):
        copied = []
        line = self.line(lambda text, what: copied.append((text, what)))
        line.show_folder(self.DOT_FOLDER)
        self.assertFalse(line.isHidden())
        self.assertEqual(line.where.text(), "Saved in ~/.universe_data/Subjects/Zynth/claude/PDFs to convert")
        self.assertFalse(line.hint.isHidden())
        line.copy_path()
        self.assertEqual(copied, [(str(self.DOT_FOLDER), "The folder path")])
        with mock.patch.object(claude_panel, "reveal") as reveal:
            line.open_folder()
        reveal.assert_called_once_with(self.DOT_FOLDER)

    def test_a_visible_folder_needs_no_hint(self):
        line = self.line()
        line.show_folder(Path("/Volumes/Uni/claude/2026-11-02 09.24 next/attach"))
        self.assertEqual(line.where.text(), "Saved in /Volumes/Uni/claude/2026-11-02 09.24 next/attach")
        self.assertTrue(line.hint.isHidden())


class PracticeTabTests(Base):
    def setUp(self):
        super().setUp()
        self.panel = PracticePanel()
        self.addCleanup(self.panel.deleteLater)
        self.panel.set_scope(self.subject, None)

    def only(self, *questions: dict, objectives=(OBJECTIVE, OBJECTIVE_2)) -> None:
        self.seed(jsonl(TOPIC, *objectives, *questions))

    def pick(self, text: str) -> None:
        next(c for c in self.panel.choices if c.label.text() == text).box.setChecked(True)

    def test_top_line_counts(self):
        self.subject.set_exam(date=(date.today() + timedelta(days=34)).isoformat())
        self.seed()
        self.panel.refresh()
        self.assertEqual(self.panel.header.text(), "Exam in 34 days · 0 weak · 2 new")

    def test_nothing_imported_says_where_to_go(self):
        self.panel.refresh()
        self.assertIn("Claude tab", self.panel.idle.text())
        self.panel.start()
        self.assertTrue(self.panel.question_view.isHidden())

    def test_a_session_end_to_end_records_each_answer_at_once(self):
        self.only(MC, TF, objectives=(OBJECTIVE, OBJECTIVE_2))
        self.panel.start()
        self.assertEqual(len(self.panel.session), 2)
        for position in range(2):
            question = self.panel.question
            self.assertEqual(self.panel.stem.text(), question["stem"])
            self.assertFalse(self.panel.confidence_row.isHidden())
            self.pick("Mittelverwendung" if question["id"] == "q_1" else "True")
            self.panel.unsure.click() if question["id"] == "q_1" else self.panel.sure.click()
            self.assertEqual(len(self.attempts()), position + 1)  # recorded before the session ends
            if question["id"] == "q_1":
                self.assertIn("Not quite", self.panel.feedback.text())
                self.assertIn("Das ist die Aktivseite", self.panel.feedback.text())  # why_wrong of the chosen option
                self.assertIn("Mittelherkunft", self.panel.feedback.text())
                self.assertIn("src:0", self.panel.feedback.text())
            else:
                self.assertIn("Correct", self.panel.feedback.text())
            self.panel.next_btn.click()
        self.assertTrue(self.panel.question_view.isHidden())
        self.assertIn("1 of 2 right", self.panel.idle.text())
        by_question = {a["q"]: a for a in self.attempts()}
        self.assertEqual(by_question["q_1"]["confidence"], "unsure")
        self.assertEqual(by_question["q_1"]["rating"], 1)
        self.assertEqual(by_question["q_tf"]["rating"], 3)
        self.assertEqual(by_question["q_tf"]["by"], "auto")
        self.assertEqual(by_question["q_tf"]["mode"], "today")

    def test_confidence_changes_the_rating(self):
        self.only(MC)
        self.panel.start()
        self.pick("Mittelherkunft")
        self.panel.guess.click()
        self.assertEqual(self.attempts()[0]["confidence"], "guess")
        self.assertEqual(self.attempts()[0]["rating"], 1)  # right by luck on mc is no knowledge

    def test_no_answer_is_not_recorded(self):
        self.only(MC)
        self.panel.start()
        self.panel.sure.click()
        self.assertEqual(self.attempts(), [])
        self.assertIn("Answer first", self.panel.feedback.text())

    def test_wrong_brings_another_probe_back_later(self):
        second = {**MC, "id": "q_2", "stem": "Wozu dient die Passivseite?"}
        self.only(MC, second)
        self.panel.start()
        self.assertEqual(len(self.panel.session), 1)
        asked = self.panel.question["id"]
        self.pick("Mittelverwendung")
        self.panel.sure.click()
        self.assertEqual(len(self.panel.session), 2)
        self.panel.next_btn.click()
        self.assertTrue(self.panel.question.get("again"))
        self.assertNotEqual(self.panel.question["id"], asked)
        self.assertIn("second chance", self.panel.meta.text())

    def test_hints_show_one_by_one_and_cap_the_rating(self):
        self.only(CALC)
        self.panel.start()
        self.assertFalse(self.panel.hint_btn.isHidden())
        self.panel.hint_btn.click()
        self.assertIn("Addieren", self.panel.hint_label.text())
        self.assertNotIn("Zweimal", self.panel.hint_label.text())
        self.panel.hint_btn.click()
        self.assertIn("Zweimal", self.panel.hint_label.text())
        self.assertFalse(self.panel.hint_btn.isEnabled())
        self.panel.answer_line.setText("1.200,00")
        self.panel.sure.click()
        attempt = self.attempts()[0]
        self.assertEqual(attempt["hints"], 2)
        self.assertEqual(attempt["rating"], 2)  # right and sure, but hinted
        self.assertIn("600 + 600", self.panel.feedback.text())

    def test_free_answer_with_rubric(self):
        self.only(SHORT)
        self.panel.start()
        self.assertFalse(self.panel.confidence_row.isHidden())
        self.assertFalse(self.panel.answer_text.isHidden())
        self.panel.answer_text.setPlainText("Die Herkunft der Mittel")
        self.panel.sure.click()
        self.assertIn("Sie zeigt die Mittelherkunft", self.panel.model_answer.text())
        self.assertEqual(self.attempts(), [])  # nothing until graded
        self.assertEqual(len(self.panel.rubric), 2)
        self.assertTrue(self.panel.grade_row.isHidden())
        self.panel.rubric[0][0].box.setChecked(True)
        self.panel.rubric_save.click()
        attempt = self.attempts()[0]
        self.assertEqual(attempt["answer"], "Die Herkunft der Mittel")
        self.assertEqual(attempt["score"], 0.25)  # one mark of four
        self.assertEqual(attempt["by"], "self")
        self.assertEqual(attempt["rating"], 1)
        self.assertEqual(attempt["confidence"], "sure")

    def test_free_answer_without_rubric_uses_three_buttons(self):
        self.only({**SHORT, "rubric": []})
        self.panel.start()
        self.assertFalse(self.panel.confidence_row.isHidden())
        self.panel.answer_text.setPlainText("etwas")
        self.panel.sure.click()
        self.assertFalse(self.panel.grade_row.isHidden())
        self.panel.partly.click()
        attempt = self.attempts()[0]
        self.assertEqual(attempt["rating"], 2)
        self.assertGreaterEqual(attempt["score"], 0.5)
        self.assertEqual(attempt["confidence"], "sure")

    def test_a_card_has_no_text_field(self):
        self.only(CARD)
        self.panel.start()
        self.assertTrue(self.panel.answer_text.isHidden())
        self.assertFalse(self.panel.confidence_row.isHidden())
        self.panel.sure.click()
        self.assertIn("Rückseite", self.panel.model_answer.text())
        self.panel.got_it.click()
        self.assertEqual(self.attempts()[0]["rating"], 3)
        self.assertEqual(self.attempts()[0]["confidence"], "sure")

    def test_source_links_jump(self):
        self.only(MC)
        jumps = []
        self.panel.sourceActivated.connect(jumps.append)
        self.panel.start()
        self.pick("Mittelherkunft")
        self.panel.sure.click()
        self.panel._open_link("src:0")
        self.assertEqual(jumps[0]["kind"], "page")
        self.assertEqual(jumps[0]["label"], "SL01.p1")

    def test_unverified_questions_show_a_badge(self):
        self.only({**MC, "src": []})
        self.panel.start()
        self.assertIn("not verified", self.panel.meta.text())

    def test_wrong_question_flag_keeps_the_reason_and_moves_on(self):
        self.only(MC, TF, objectives=(OBJECTIVE, OBJECTIVE_2))
        self.panel.start()
        flagged = self.panel.question["id"]
        with mock.patch("universe.ui.practice_panel.QInputDialog.getText", return_value=("Folie sagt anderes", True)):
            self.panel.flag_btn.click()
        self.assertEqual(self.store.knowledge().questions[flagged]["flagged"], "Folie sagt anderes")
        self.assertEqual(len(self.panel.session), 1)
        self.assertNotEqual(self.panel.question["id"], flagged)
        self.assertEqual(self.attempts(), [])
        with mock.patch("universe.ui.practice_panel.QInputDialog.getText", return_value=("", False)):
            self.panel.flag_btn.click()  # cancelled
        self.assertEqual(len(self.panel.session), 1)

    def test_flagging_the_last_unanswered_question_ends_the_session(self):
        self.only(MC)
        self.panel.start()
        with mock.patch("universe.ui.practice_panel.QInputDialog.getText", return_value=("falsch", True)):
            self.panel.flag_btn.click()
        self.assertTrue(self.panel.question_view.isHidden())

    def test_mock_gives_no_feedback_until_the_end(self):
        self.only(MC, TF, SHORT, objectives=(OBJECTIVE, OBJECTIVE_2))
        self.subject.set_exam(duration_min=30)
        self.panel.start_mock()
        self.assertTrue(self.panel.is_mock)
        self.assertTrue(self.panel.timer.isActive())
        self.assertIn("left", self.panel.meta.text())
        seen = set()
        while self.panel.in_session and not self.panel._reviewing:
            question = self.panel.question
            seen.add(question["id"])
            self.assertTrue(self.panel.hint_btn.isHidden())
            if question["type"] == "short":
                self.panel.answer_text.setPlainText("Meine Antwort")
                self.panel.save_free_btn.click()
            else:
                self.pick("Mittelverwendung" if question["type"] == "mc" else "True")
                self.panel.sure.click()
            self.assertTrue(self.panel.feedback.isHidden())
            self.assertTrue(self.panel.next_btn.isHidden())
        self.assertEqual(seen, {"q_1", "q_tf", "q_short"})
        self.assertEqual(len(self.attempts()), 3)  # all recorded as they were given
        self.assertTrue(all(a["mode"] == "mock" for a in self.attempts()))

        # now the free answer is held against the model answer
        self.assertIn("Grade your free answers", self.panel.meta.text())
        self.assertIn("Sie zeigt die Mittelherkunft", self.panel.model_answer.text())
        self.panel.rubric[0][0].box.setChecked(True)
        self.panel.rubric[1][0].box.setChecked(True)
        self.panel.rubric_save.click()
        grades = [e for e in self.store.events() if e["type"] == "grade"]
        self.assertEqual(grades[0]["score"], 1.0)

        self.assertTrue(self.panel.question_view.isHidden())
        summary = self.panel.idle.text()
        self.assertIn("Mock finished: 67% on 3 questions", summary)
        self.assertIn("Mittelherkunft", summary)  # the right answer, now that it may be seen
        self.assertFalse(self.panel.timer.isActive())
        self.assertEqual(self.panel.mode, "")

    def test_mock_with_nothing_unseen_says_why(self):
        self.only({**MC, "src": []})
        self.panel.start_mock()
        self.assertIn("No mock possible", self.panel.idle.text())
        self.assertFalse(self.panel.timer.isActive())

    def test_mock_ends_when_time_is_up(self):
        self.only(MC, TF, objectives=(OBJECTIVE, OBJECTIVE_2))
        self.panel.start_mock()
        self.pick(self.panel.choices[0].label.text())
        self.panel.sure.click()
        self.panel.deadline = time.monotonic() - 1
        self.panel._tick()
        self.assertTrue(self.panel.question_view.isHidden())
        self.assertIn("on 1 question.", self.panel.idle.text())

    def test_half_a_session_keeps_its_data_and_lecture_changes_do_not_end_it(self):
        self.only(MC, TF, objectives=(OBJECTIVE, OBJECTIVE_2))
        self.panel.start()
        self.pick(self.panel.choices[0].label.text())
        self.panel.sure.click()
        self.panel.set_scope(self.subject, self.lecture)  # another lecture opened
        self.assertTrue(self.panel.in_session)
        other = Workspace(self.subject.path.parent).create_subject("Anderes")
        self.panel.set_scope(other, None)
        self.assertFalse(self.panel.in_session)
        self.assertEqual(len(self.attempts()), 1)

    def test_topic_and_lecture_modes(self):
        self.seed()
        self.panel.set_scope(self.subject, self.lecture)
        self.panel.refresh()
        self.panel.mode_choice.setCurrentIndex(2)
        self.assertEqual(self.panel.target.currentData(), "t_b")
        self.panel.start_chosen()
        self.assertEqual(self.panel.mode, "topic")
        self.panel.finish()
        self.panel.mode_choice.setCurrentIndex(3)
        self.assertEqual(self.panel.target.currentData(), self.lecture.uid)
        self.panel.start_chosen()
        self.assertEqual(self.panel.mode, "lecture")
        self.assertTrue(self.panel.session)


class GuideTabTests(Base):
    def setUp(self):
        super().setUp()
        self.panel = GuidePanel(self.professors)
        self.addCleanup(self.panel.deleteLater)
        self.seed()
        self.panel.set_scope(self.subject, self.lecture)
        self.panel.refresh()

    def page(self) -> str:
        return self.panel.browser.toHtml()

    def test_citations_in_markdown_become_links_only_when_known(self):
        text = link_citations("a [SL01.p1, R01@751] b [Quelle] c [ZZ]", {"SL01.p1": {}, "R01@751": {}})
        self.assertIn("[[SL01.p1](cite:SL01.p1), [R01@751](cite:R01@751)]", text)
        self.assertIn("[Quelle]", text)

    def test_topic_page_shows_the_sheet_and_citations_jump(self):
        jumps = []
        self.panel.sourceActivated.connect(jumps.append)
        self.panel.select("topic", "t_b")
        page = self.page()
        self.assertIn("cite:SL01.p1", page)
        self.assertIn("Passivseite", page)
        self.assertIn("Passivseite erklären", page)  # objectives
        self.assertIn("Kommt in der Klausur dran", page)  # what was said
        self.assertIn("Folie und Vortrag weichen ab", page)  # conflicts
        self.panel.browser.anchorClicked.emit(QUrl("cite:SL01.p1"))
        self.assertEqual(jumps[0]["kind"], "page")
        self.assertEqual(jumps[0]["page"], 1)
        self.panel.browser.anchorClicked.emit(QUrl("cite:R01@751"))
        self.assertEqual(jumps[1]["kind"], "line")
        self.assertEqual(jumps[1]["t"], 751.2)
        self.panel.open_link(QUrl("cite:Unknown"))
        self.assertEqual(len(jumps), 2)

    def test_lecture_page_is_built_without_touching_the_notes(self):
        path = next(self.lecture.path.glob("notes*"))
        raw = path.read_bytes()
        self.panel.select("lecture", self.lecture.uid)
        page = self.page()
        self.assertIn("Passivseite erklären", page)
        self.assertLess(page.index("Passivseite erklären"), page.index("Aktivseite erklären"))  # slide order
        self.assertIn("Kommt in der Klausur dran", page)
        self.assertIn("Folie und Vortrag weichen ab", page)
        self.assertIn("Aktiv und Passiv nennen", page)
        self.assertIn("Die Bilanz hat", page)  # the note it corrects
        self.assertEqual(path.read_bytes(), raw)

    def test_suggestions_accept_and_dismiss(self):
        changed = []
        self.panel.changed.connect(lambda: changed.append(True))
        self.panel.select("suggestions")
        page = self.page()
        self.assertIn("Bilanzen", page)
        self.assertIn("another course", page)
        self.panel.open_link(QUrl("accept:prof:0"))
        self.assertIn("Bilanzen", self.professors.get(self.profile.id).focus_areas)
        self.assertEqual(self.professors.get(self.profile.id).testing_style, "Eigener Text")
        self.assertEqual(changed, [True])
        self.panel.select("suggestions")
        self.assertNotIn("Bilanzen", self.page())
        key = self.store.knowledge().doc_notes[0]["doc"]
        self.panel.open_link(QUrl("dismiss:doc:0"))
        self.assertNotIn(key, self.subject.excluded_documents)
        self.assertIsNone(self.panel._find(("suggestions", "")))  # nothing left to decide

    def test_accepting_a_wrong_subject_note_excludes_the_document(self):
        key = self.store.knowledge().doc_notes[0]["doc"]
        self.panel.open_link(QUrl("accept:doc:0"))
        self.assertIn(key, self.subject.excluded_documents)

    def test_empty_subject_says_what_to_do(self):
        empty = Workspace(self.subject.path.parent).create_subject("Leer")
        self.panel.set_scope(empty, None)
        self.panel.refresh()
        self.assertIn("No guide yet", self.page())

    def test_topic_entries_show_importance(self):
        self.assertIsNotNone(self.panel._find(("topic", "t_b")))
        self.assertIn("★★★ Bilanz", self.panel._find(("topic", "t_b")).text())


class ProgressTabTests(Base):
    def setUp(self):
        super().setUp()
        self.panel = ProgressPanel(self.professors)
        self.addCleanup(self.panel.deleteLater)
        self.panel.set_scope(self.subject, self.lecture)

    def answer(self, days_ago: int, q: dict, n: int = 1) -> None:
        for _ in range(n):
            self.store.append_event(
                "attempt", q=q["id"], o=q["objective"], topic="t_b", rev=1, mode="today", answer="x", correct=True, score=1.0,
                confidence="sure", hints=0, ms=10, by="auto", rating=3,
            )  # fmt: skip
        if days_ago:  # move them into the past
            path = self.store.events_path
            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            when = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
            for event in lines[-n:]:
                event["at"] = when
            path.write_text("\n".join(json.dumps(event) for event in lines) + "\n", encoding="utf-8")

    def test_gates_say_not_enough_data(self):
        self.seed()
        self.panel.refresh()
        self.assertIn("0%", self.panel.coverage.text())
        self.assertIn("not enough data yet (0 of 30 answers, 0 of 3 days)", self.panel.recall.text())
        self.assertIn("not enough data yet", self.panel.mock.text())
        self.assertIn("no data yet", self.panel.calibration.text())
        self.assertEqual(self.panel.unprocessed.text(), "Every lecture has been processed by Claude.")

    def test_nothing_imported(self):
        self.panel.refresh()
        self.assertIn("nothing imported", self.panel.coverage.text())
        self.assertEqual(self.panel.unprocessed.text(), "Lectures not processed yet: L01")
        self.assertEqual(self.panel.table.rowCount(), 0)

    def test_numbers_open_up_with_enough_answers(self):
        self.subject.set_exam(date=(date.today() + timedelta(days=20)).isoformat())
        self.seed()
        q = self.store.knowledge().questions["q_1"]
        for days_ago in (2, 1, 0):
            self.answer(days_ago, q, 11)
        self.panel.refresh()
        self.assertIn("Exam-day recall:", self.panel.recall.text())
        self.assertNotIn("not enough", self.panel.recall.text())
        self.assertIn("model", self.panel.recall.text())
        self.assertIn("100% were right", self.panel.calibration.text())
        self.assertEqual(self.panel.table.rowCount(), 1)
        self.assertIsNotNone(self.panel.table.cellWidget(0, 1))
        self.assertEqual(sum(day.answers for day in self.panel.chart.days), 33)
        self.assertIn("33 answers on 3 days", self.panel.chart_caption.text())
        self.panel.chart.resize(400, 56)
        self.panel.chart.grab()  # paints

    def test_a_mock_result_shows_its_range(self):
        self.seed()
        q = self.store.knowledge().questions["q_1"]
        self.store.record_attempt(q, "Mittelherkunft", 1.0, "sure", 0, 10, "mock", "auto")
        self.panel.refresh()
        self.assertIn("100% on 1 question", self.panel.mock.text())
        self.assertIn("between", self.panel.mock.text())

    def test_double_click_starts_that_topic(self):
        self.seed()
        self.panel.refresh()
        topics = []
        self.panel.practiseTopic.connect(topics.append)
        self.panel.table.cellDoubleClicked.emit(0, 0)
        self.assertEqual(topics, ["t_b"])

    def test_host_starts_practice_for_the_topic(self):
        host = AIPanel(self.professors)
        self.addCleanup(host.deleteLater)
        self.seed()
        host.set_scope(self.subject, None)
        host.progress.refresh()
        host.progress.table.cellDoubleClicked.emit(0, 0)
        self.assertIs(host.tabs.currentWidget(), host.practice)
        self.assertEqual(host.practice.mode, "topic")
        self.assertTrue(host.practice.session)

    def test_exam_date_duration_and_aids_are_saved(self):
        self.panel.refresh()
        form = self.panel.exam
        self.assertFalse(form.known.isChecked())
        self.assertEqual(self.subject.exam, {})
        changed = []
        self.panel.examChanged.connect(lambda: changed.append(True))
        form.known.setChecked(True)
        form.date.setDate(date(2027, 2, 12))
        form.duration.setValue(90)
        form.aids.setText("Taschenrechner")
        form.save()
        self.assertEqual(self.subject.exam, {"date": "2027-02-12", "duration_min": 90, "aids": "Taschenrechner"})
        self.assertTrue(changed)
        form.known.setChecked(False)
        self.assertNotIn("date", self.subject.exam)
        self.assertEqual(self.subject.exam["duration_min"], 90)

    def test_opening_does_not_write(self):
        self.subject.set_exam(date="2027-02-12", notes="Raum 3")
        before = dict(self.subject.exam)
        self.panel.refresh()
        self.assertEqual(self.subject.exam, before)
        self.assertEqual(self.panel.exam.date.date().toPython(), date(2027, 2, 12))

    def test_moodle_suggests_and_the_student_confirms(self):
        start = datetime.now().astimezone() + timedelta(days=40)
        events = [
            moodle_ical.Event("1", "Klausur Finanzen", start, "Finanzen"),
            moodle_ical.Event("2", "Abgabe Blatt 3", start - timedelta(days=9), "Finanzen"),
            moodle_ical.Event("3", "Klausur Physik", start - timedelta(days=9), "Physik"),
        ]
        self.assertEqual(exam_event(events, self.subject).uid, "1")
        form = ExamForm()
        self.addCleanup(form.deleteLater)
        form.load(self.subject, events)
        self.assertFalse(form.suggestion.isHidden())
        self.assertEqual(self.subject.exam, {})  # not until confirmed
        form.suggestion.click()
        self.assertEqual(self.subject.exam["date"], start.date().isoformat())
        self.assertTrue(form.suggestion.isHidden())


class MainWindowTests(Base):
    def setUp(self):
        for name in ("QMessageBox",):
            patcher = mock.patch(f"universe.ui.main_window.{name}")
            patcher.start()
            self.addCleanup(patcher.stop)
        index = mock.patch.object(MainWindow, "_run_index")
        self.run_index = index.start()
        self.addCleanup(index.stop)
        os_reveal = mock.patch.object(claude_panel, "reveal")
        os_reveal.start()
        self.addCleanup(os_reveal.stop)
        self.window = MainWindow(Settings())
        self.addCleanup(self.window.deleteLater)
        name = self.id().rsplit(".", 1)[-1]
        self.subject = self.window.workspace.create_subject(name)
        self.professors = self.window.professors
        self.lecture = self.subject.create_lecture("Woche 1")
        self.other = self.subject.create_lecture("Woche 2")
        build_subject(self.subject, self.lecture)
        self.store = StudyStore(self.subject)
        self.window.sidebar.refresh(select=self.lecture)

    def start_recording(self):
        recorder = self.window.audio.recorder
        recorder.path = self.lecture.new_recording_path()
        recorder.path.write_bytes(b"")
        recorder._stream = object()
        self.addCleanup(setattr, recorder, "_stream", None)
        self.window.audio.recording_lecture = self.lecture
        elapsed = mock.patch.object(recorder, "elapsed", return_value=12.34)
        elapsed.start()
        self.addCleanup(elapsed.stop)
        return recorder

    def status(self) -> str:
        return self.window.statusBar().currentMessage()

    def test_toolbar_works_with_a_subject_and_no_lecture(self):
        self.window.sidebar.refresh(select=self.subject)
        self.assertIsNone(self.window.lecture)
        export, import_btn = self.window.subject_buttons
        self.assertTrue(export.isEnabled())
        self.assertTrue(import_btn.isEnabled())
        self.assertFalse(self.window.lecture_buttons[0].isEnabled())
        self.assertEqual(self.window.ai.subject.path, self.subject.path)
        self.assertTrue(self.window.ai.tabs.isEnabled())
        self.window._export_for_claude()  # what ⌘E does
        wait_for(self.window.ai.claude)
        self.assertIs(self.window.ai.tabs.currentWidget(), self.window.ai.claude)
        self.assertIsNotNone(self.window.ai.claude.result)
        self.assertIn("Drag the", self.window.ai.claude.export_done.text())

    def test_export_shortcut_is_cmd_e(self):
        action = next(a for a in self.window.findChildren(type(self.window.notes_action)) if a.text() == "Export for Claude")
        self.assertEqual(action.shortcut().toString(), "Ctrl+E")

    def test_nothing_selected_explains(self):
        self.window.subject = None
        self.window.lecture = None
        self.window._export_for_claude()
        self.assertIn("Pick a subject", self.status())

    def test_jump_opens_the_slide_and_switches_to_its_lecture(self):
        self.window.sidebar.refresh(select=self.other)
        view = subject_model.load_subject(self.subject, self.professors, pages=False)
        document = next(d for d in view.documents if d.title == "VL.pdf")
        self.window._jump_to_source({"kind": "page", "doc": document.key, "page": 2, "label": "SL01.p2"})
        self.assertEqual(self.window.lecture.path, self.lecture.path)
        self.assertEqual(self.window.documents.viewer.path, document.path)

    def test_claudes_own_knowledge_has_nothing_to_open(self):
        self.window._jump_to_source({"kind": "own", "label": "KN"})
        self.assertIn("nothing to open", self.status())

    def test_jump_to_audio(self):
        view = subject_model.load_subject(self.subject, self.professors, pages=False)
        with mock.patch.object(self.window, "_seek_audio") as seek:
            self.window._jump_to_source({"kind": "line", "rec": view.lectures[0].recordings[0].uid, "t": 751.2})
        seek.assert_called_once()
        self.assertEqual(seek.call_args[0][1], 751.2)

    def test_a_source_that_is_gone_says_so(self):
        self.window._jump_to_source({"kind": "page", "doc": "d_nope", "page": 2, "label": "SL09.p2"})
        self.assertIn("SL09.p2 is no longer there", self.status())

    def test_recording_guard_keeps_the_open_lecture_on_a_jump(self):
        self.window.sidebar.refresh(select=self.other)
        recording = self.other
        recorder = self.window.audio.recorder
        recorder.path = recording.new_recording_path()
        recorder.path.write_bytes(b"")
        recorder._stream = object()
        self.addCleanup(setattr, recorder, "_stream", None)
        view = subject_model.load_subject(self.subject, self.professors, pages=False)
        document = next(d for d in view.documents if d.title == "VL.pdf")
        self.window._jump_to_source({"kind": "page", "doc": document.key, "page": 2, "label": "SL01.p2"})
        self.assertEqual(self.window.lecture.path, recording.path)
        self.assertIn("Still recording", self.status())
        self.window._jump_to_source({"kind": "line", "rec": view.lectures[0].recordings[0].uid, "t": 751.2})
        self.assertEqual(self.window.lecture.path, recording.path)
        self.window._open_hit(Hit("Woche 1", "transcript", "Woche [▶ 00:12:31]", "x", 0.5))
        self.assertEqual(self.window.lecture.path, recording.path)
        self.assertIn("Still recording", self.status())

    def test_recording_guard_lets_a_document_of_the_open_lecture_open(self):
        self.start_recording()
        view = subject_model.load_subject(self.subject, self.professors, pages=False)
        document = next(d for d in view.documents if d.title == "VL.pdf")
        self.window._jump_to_source({"kind": "page", "doc": document.key, "page": 2})
        self.assertEqual(self.window.lecture.path, self.lecture.path)
        self.assertEqual(self.window.documents.viewer.path, document.path)

    def test_exam_hits_do_not_switch_lecture(self):
        hit = Hit("", "exams", "K.pdf p.1", "Aufgabe", 0.5)
        self.assertEqual(hit_target(hit), ("exams", "K.pdf", 1.0))
        self.assertEqual(hit_target(Hit("", "exercises", "T.pdf p.3", "x", 0.1)), ("exercises", "T.pdf", 3.0))
        self.assertEqual(hit_target(Hit("L", "slides", "S.pdf p.2", "x", 0.1)), ("slides", "S.pdf", 2.0))
        self.window._open_hit(hit)
        self.assertEqual(self.window.lecture.path, self.lecture.path)
        self.assertEqual(self.window.documents.viewer.path.name, "K.pdf")

    def test_exam_date_from_the_sidebar(self):
        with mock.patch("universe.ui.main_window.ExamDialog") as dialog:
            self.window.sidebar.examDateRequested.emit(self.subject)
        dialog.assert_called_once()
        dialog.return_value.exec.assert_called_once()

    def test_slide_marks_are_written_only_on_page_turns_while_recording(self):
        viewer, documents = self.window.documents.viewer, self.window.documents
        documents.open_page("slides", "VL.pdf", 1)
        app.processEvents()
        viewer.go_to_page(1)
        recorder = self.start_recording()
        self.window._on_recording_changed(True)  # the page open now is only remembered
        for _ in range(5):
            self.window.audio._on_tick()
        self.assertEqual(read_slide_marks(recorder.path), [])

        viewer.go_to_page(1)  # the same page: no turn
        self.assertEqual(read_slide_marks(recorder.path), [])
        self.window._on_page_turned(1)
        self.assertEqual(read_slide_marks(recorder.path), [])

        viewer.pageChanged.emit(2)
        marks = read_slide_marks(recorder.path)
        self.assertEqual(marks[0], {"offset": 0.0, "doc": "slides", "pdf": "VL.pdf", "page": 1})
        self.assertEqual(marks[1], {"offset": 12.3, "doc": "slides", "pdf": "VL.pdf", "page": 2})
        viewer.pageChanged.emit(3)
        self.assertEqual(len(read_slide_marks(recorder.path)), 3)

    def test_a_real_page_turn_emits_page_changed(self):
        viewer = self.window.documents.viewer
        self.window.documents.open_page("slides", "VL.pdf", 1)
        app.processEvents()
        seen = []
        viewer.pageChanged.connect(seen.append)
        viewer.go_to_page(1)
        self.assertEqual(seen, [])  # still that page
        viewer._set_page_indicator(2)
        viewer._set_page_indicator(2)
        self.assertEqual(seen, [2])

    def test_the_page_open_at_the_start_goes_on_file_even_without_a_turn(self):
        recorder = self.start_recording()
        self.window.audio.start_slide_marks("slides", "VL.pdf", 4)
        with mock.patch.object(recorder, "stop", return_value=recorder.path):
            self.window.audio.stop_recording()
        self.assertEqual([m["page"] for m in read_slide_marks(recorder.path)], [4])

    def test_no_marks_without_recording_or_while_another_lecture_is_open(self):
        path = self.lecture.new_recording_path()
        self.window.audio.mark_slide("slides", "VL.pdf", 2)
        self.assertEqual(read_slide_marks(path), [])
        recorder = self.start_recording()
        self.window.sidebar.refresh(select=self.other)
        self.window.audio.mark_slide("slides", "VL.pdf", 3)
        self.assertEqual(read_slide_marks(recorder.path), [])

    def test_transcribing_again_keeps_manual_labels_and_the_id(self):
        audio = self.lecture.dir("audio") / "Woche.m4a"
        audio.write_bytes(b"")
        folder = self.lecture.dir("transcripts")
        old = Transcript.load(folder / "Woche.json")
        relevance.set_manual(old.segments[0], category="off_topic")
        old.save(folder)
        fresh = Transcript("Woche.m4a", [Segment(751.0, 755.0, "Neu gehört"), Segment(797.0, 800.0, "Auch neu")])
        with mock.patch("universe.ui.main_window.transcriber.check_setup", return_value=[]), mock.patch("universe.ui.main_window.transcriber.transcribe", return_value=fresh), mock.patch.object(self.window.jobs, "submit") as submit:
            self.window.transcribe(audio)
            job = submit.call_args[0][2]
            job(lambda _p: None, None)
        saved = Transcript.load(folder / "Woche.json")
        self.assertEqual(saved.id, "r_one")
        self.assertEqual(saved.segments[0].text, "Neu gehört")
        self.assertEqual(saved.segments[0].category, "off_topic")
        self.assertEqual(saved.segments[0].source, "manual")
        self.assertEqual(saved.segments[1].source, "")

    def test_reply_refreshes_transcript_and_reindexes(self):
        self.run_index.reset_mock()
        panel = self.window.ai.claude
        panel.import_reply(jsonl(TOPIC, OBJECTIVE, MC, {"t": "label", "from": "R01@948", "category": "question", "exam": False, "reason": "x"}))
        wait_for(panel)
        self.assertTrue(self.run_index.called)
        self.assertIn("q_1", self.store.knowledge().questions)

    def test_welcome_shows_readiness_once_there_is_data(self):
        self.window.sidebar.refresh(select=self.subject)
        self.assertTrue(self.window.welcome.readiness.isHidden())
        view = subject_model.load_subject(self.subject, self.professors)
        reply = jsonl(TOPIC, OBJECTIVE, MC)
        self.store.merge(check_reply(parse_reply(reply), view, self.store.known_ids()), reply, view)
        self.window.welcome.show_subject(self.subject)
        self.assertFalse(self.window.welcome.readiness.isHidden())
        self.assertIn("0% practised", self.window.welcome.readiness.text())
        self.assertIn("1 new", self.window.welcome.readiness.text())


class TranscriptSaveTests(Base):
    def test_save_keeps_labels_a_background_job_wrote_meanwhile(self):
        from universe.ui.transcript_panel import TranscriptPanel

        panel = TranscriptPanel()
        self.addCleanup(panel.deleteLater)
        panel.set_lecture(self.lecture)
        panel.files.setCurrentIndex(panel.files.findText("Woche"))
        path = self.lecture.dir("transcripts") / "Woche.json"
        background = Transcript.load(path)  # another job labels line 2 after the panel read the file
        background.segments[1].exam, background.segments[1].source = True, "local"
        background.save(path.parent)

        first = panel._segment_items()[0]
        panel._set([first], category="question")

        saved = Transcript.load(path)
        self.assertEqual(saved.segments[0].category, "question")
        self.assertEqual(saved.segments[0].source, "manual")
        self.assertTrue(saved.segments[1].exam)
        self.assertEqual(saved.segments[1].source, "local")
        self.assertEqual(saved.id, "r_one")
        self.assertTrue(panel._segment(panel._segment_items()[1]).exam)  # and the view shows it

    def test_save_after_transcribing_again_touches_only_what_still_exists(self):
        from universe.ui.transcript_panel import TranscriptPanel

        panel = TranscriptPanel()
        self.addCleanup(panel.deleteLater)
        panel.set_lecture(self.lecture)
        panel.files.setCurrentIndex(panel.files.findText("Woche"))
        path = self.lecture.dir("transcripts") / "Woche.json"
        Transcript("Woche.m4a", [Segment(1.0, 2.0, "Ganz neu")], id="r_one").save(path.parent)
        panel._set([panel._segment_items()[0]], category="question")
        saved = Transcript.load(path)
        self.assertEqual([s.text for s in saved.segments], ["Ganz neu"])
        self.assertEqual(saved.segments[0].category, "")
        self.assertEqual(panel.parts[0][1].segments[0].text, "Ganz neu")


if __name__ == "__main__":
    unittest.main()
