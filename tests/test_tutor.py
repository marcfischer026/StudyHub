"""Tests for the tutor chat: the prompts it builds, its log, and the Tutor tab."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import universe  # noqa: E402,F401
from universe.config import Settings  # noqa: E402
from universe.ui.main_window import MainWindow  # noqa: E402,F401  (before PySide6: lancedb fails to import after it on Python 3.15)

from PySide6.QtCore import QUrl  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tests.test_ai_panels import wait_until  # noqa: E402
from universe.services import local_llm, tutor  # noqa: E402
from universe.services.study_store import StudyStore  # noqa: E402
from universe.services.vector_index import Hit  # noqa: E402
from universe.storage import Workspace  # noqa: E402
from universe.ui.tutor_panel import WHOLE_COURSE, TutorPanel  # noqa: E402

app = QApplication.instance() or QApplication([])

HITS = [
    Hit("Woche 1", "slides", "VL p.2", "Die Passivseite zeigt die Mittelherkunft.", score=0.9),
    Hit("Woche 1", "transcript", "Woche 12:31", "Das kommt in der Klausur dran.", score=0.8),
]


class PromptTests(unittest.TestCase):
    def test_each_mode_has_its_own_style_and_opener(self):
        for mode in tutor.MODES:
            prompt = tutor.system_prompt(mode, "Finanzen", "Bilanz", [])
            self.assertIn(tutor.STYLE[mode], prompt)
            self.assertIn('"Bilanz"', prompt)
        self.assertIn("from zero", tutor.opener("zero", "Bilanz"))
        self.assertIn(tutor.WHOLE_COURSE, tutor.opener("quiz", ""))

    def test_passages_are_numbered_with_their_place(self):
        prompt = tutor.system_prompt("explain", "Finanzen", "", HITS)
        self.assertIn("[1] (Woche 1 · slides · VL p.2)\nDie Passivseite", prompt)
        self.assertIn("[2] (Woche 1 · transcript · Woche 12:31)", prompt)

    def test_history_keeps_the_latest_turns_and_starts_with_the_student(self):
        history = [tutor.Turn("user", "a" * 4000), tutor.Turn("assistant", "b" * 4000), tutor.Turn("user", "Warum?")]
        messages = tutor.messages_for("quiz", "Finanzen", "", history, [])
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual([m["role"] for m in messages[1:]], ["user"])
        self.assertEqual(messages[-1]["content"], "Warum?")

    def test_citations_pick_the_passages_and_default_to_all(self):
        self.assertEqual(tutor.Turn("assistant", "Siehe [2].", HITS).cited(), [2])
        self.assertEqual(tutor.Turn("assistant", "Ohne Beleg.", HITS).cited(), [1, 2])
        self.assertEqual(tutor.Turn("assistant", "Falsch [7].", HITS).cited(), [1, 2])

    def test_reply_searches_then_asks_the_local_model(self):
        seen = {}

        def complete(url, model, messages, **kwargs):
            seen["messages"] = messages
            return "Die Passivseite zeigt die Herkunft [1]. Was zeigt die Aktivseite?"

        with mock.patch.object(local_llm, "find_server", return_value=("http://x", "m")), mock.patch.object(local_llm, "complete", complete):
            turn = tutor.reply("quiz", "Finanzen", "Bilanz", [tutor.Turn("user", "Los")], Settings(), search=lambda q: HITS)
        self.assertEqual(turn.role, "assistant")
        self.assertEqual(turn.cited(), [1])
        self.assertIn("Mittelherkunft", seen["messages"][0]["content"])

    def test_a_missing_index_does_not_stop_the_tutor(self):
        def broken(query):
            raise FileNotFoundError("no index")

        with mock.patch.object(local_llm, "find_server", return_value=("http://x", "m")), mock.patch.object(local_llm, "complete", return_value="Hallo"):
            turn = tutor.reply("explain", "Finanzen", "", [tutor.Turn("user", "Hi")], Settings(), search=broken)
        self.assertEqual((turn.text, turn.sources), ("Hallo", []))

    def test_citations_of_passages_it_never_saw_are_dropped(self):
        with mock.patch.object(local_llm, "find_server", return_value=("http://x", "m")), mock.patch.object(local_llm, "complete", return_value="Ein Foto [1], siehe [3]."):
            turn = tutor.reply("explain", "Finanzen", "", [tutor.Turn("user", "Hi")], Settings(), search=lambda q: HITS[:1])
        self.assertEqual(turn.text, "Ein Foto [1], siehe.")

    def test_topics_most_important_first(self):
        topics = {"a": {"title": "Nebensache", "importance": 1}, "b": {"title": "Bilanz", "importance": 3}, "c": {"importance": 3}}
        self.assertEqual(tutor.topic_titles(topics), ["Bilanz", "Nebensache"])


class TutorTabTests(unittest.TestCase):
    def setUp(self):
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finanzen")
        self.panel = TutorPanel(settings=Settings())
        self.panel.set_scope(self.subject, None)
        self.panel.show()
        self.addCleanup(self.panel.close)

    def answer(self, text: str = "Die Bilanz hat zwei Seiten [1]. Wie heißen sie?"):
        return mock.patch.object(tutor, "reply", return_value=tutor.Turn("assistant", text, HITS))

    def test_start_opens_the_chosen_way_and_logs_both_turns(self):
        self.panel.mode.setCurrentIndex(self.panel.mode.findData("zero"))
        self.panel.topic.setCurrentText("Bilanz")
        with self.answer():
            self.panel.start()
            wait_until(lambda: not self.panel.busy)
        self.assertEqual([t.role for t in self.panel.history], ["user", "assistant"])
        self.assertIn("from zero", self.panel.history[0].text)
        self.assertIn("Wie heißen sie?", self.panel.browser.toPlainText())
        records = [json.loads(line) for line in tutor.log_path(self.subject).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(r["mode"], r["topic"], r["role"]) for r in records], [("zero", "Bilanz", "user"), ("zero", "Bilanz", "assistant")])
        self.assertEqual(records[1]["sources"], ["[1] Woche 1 · slides · VL p.2"])

    def test_a_cited_source_jumps_like_a_search_hit(self):
        with self.answer():
            self.panel.input.setText("Was ist eine Bilanz?")
            self.panel.send()
            wait_until(lambda: not self.panel.busy)
        opened = []
        self.panel.hitActivated.connect(opened.append)
        self.assertIn("src:1:1", self.panel.browser.toHtml())
        self.panel.open_link(QUrl("src:1:1"))
        self.assertEqual(opened, [HITS[0]])

    def test_no_local_ai_says_what_to_do(self):
        with mock.patch.object(tutor, "reply", side_effect=local_llm.LocalLLMUnavailable("No local AI server found.")):
            self.panel.input.setText("Hallo")
            self.panel.send()
            wait_until(lambda: not self.panel.busy)
        self.assertIn("LM Studio", self.panel.hint.text())
        self.assertTrue(self.panel.send_btn.isEnabled())

    def test_a_reply_for_an_abandoned_chat_is_dropped(self):
        with self.answer():
            self.panel.input.setText("Hallo")
            self.panel.send()
            self.panel.new_chat()
            wait_until(lambda: not self.panel._workers)
        self.assertEqual(self.panel.history, [])

    def test_topics_come_from_the_study_data(self):
        store = StudyStore(self.subject)
        knowledge = store.knowledge()
        knowledge.topics = {"t_b": {"title": "Bilanz", "importance": 3}}
        store._save(knowledge)
        self.panel.mark_changed()
        self.assertEqual([self.panel.topic.itemText(i) for i in range(self.panel.topic.count())], [WHOLE_COURSE, "Bilanz"])

    def test_podcast_is_announced_but_off(self):
        self.assertIn("coming soon", self.panel.podcast_btn.text())
        self.assertFalse(self.panel.podcast_btn.isEnabled())


if __name__ == "__main__":
    unittest.main()
