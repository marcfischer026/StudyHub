"""Tests for the optional API mode, against a real HTTP server on this Mac."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import universe  # noqa: E402,F401
from universe.config import Settings, settings_path  # noqa: E402
from universe.professors import ProfessorStore  # noqa: E402
from universe.services import api_mode, pdf_conversion, subject_model  # noqa: E402
from universe.services.study_store import StudyStore  # noqa: E402
from universe.storage import Workspace  # noqa: E402
from universe.ui.claude_panel import ClaudePanel  # noqa: E402
from universe.ui.settings_dialog import SettingsDialog  # noqa: E402

app = QApplication.instance() or QApplication([])
KEY = "test-key-not-real"


class FakeApi:
    """Answers with what ``replies`` holds, in order; an int is an HTTP error."""

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _send(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(200, {"data": [{"id": "model-b"}, {"id": "model-a"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                api.requests.append(body)
                api.headers.append(dict(self.headers))
                reply = api.replies.pop(0)
                if isinstance(reply, int):
                    self._send(reply, {"error": "nope"})
                    return
                text, finish = reply if isinstance(reply, tuple) else (reply, "stop")
                self._send(200, {"choices": [{"message": {"content": text}, "finish_reason": finish}]})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = api_mode.ApiClient(f"http://127.0.0.1:{self.server.server_port}/v1", KEY, "test-model")

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.subject = Workspace(Path(tempfile.mkdtemp())).create_subject("Finanzen")
        self.lecture = self.subject.create_lecture("Woche 1")
        self.professors = ProfessorStore(Path(tempfile.mkdtemp()))

    def api(self, replies=()) -> FakeApi:
        api = FakeApi(replies)
        self.addCleanup(api.close)
        return api

    def view(self):
        return subject_model.load_subject(self.subject, self.professors)

    def pdf(self, pages: list[str]) -> None:
        doc = pymupdf.open()
        for text in pages:
            doc.new_page().insert_text((50, 60), text)
        doc.save(self.lecture.dir("slides") / "Deck.pdf")
        doc.close()


class ClientTests(Fixture):
    def test_settings_must_be_complete_and_safe(self):
        good = {"api_url": "https://api.example.com/v1/", "api_key": "k", "api_model": "m"}
        self.assertEqual(api_mode.client_from(Settings(**good)).url, "https://api.example.com/v1")
        for broken in ({"api_url": "http://api.example.com/v1"}, {"api_url": ""}, {"api_key": ""}, {"api_model": ""}):
            with self.assertRaises(api_mode.ApiError):
                api_mode.client_from(Settings(**(good | broken)))
        self.assertTrue(api_mode.client_from(Settings(**(good | {"api_url": "http://localhost:1234/v1"}))))

    def test_error_names_the_status_and_never_the_key(self):
        api = self.api([401])
        with self.assertRaises(api_mode.ApiError) as caught:
            api.client.chat([{"role": "user", "content": "x"}])
        self.assertIn("401", str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))
        self.assertEqual(api.headers[0]["Authorization"], f"Bearer {KEY}")

    def test_rate_limit_is_retried(self):
        api = self.api([429, "fine"])
        original, api_mode.RETRIES = api_mode.RETRIES, 1
        self.addCleanup(setattr, api_mode, "RETRIES", original)
        self.assertEqual(api.client.chat([]).text, "fine")

    def test_lists_models(self):
        self.assertEqual(self.api().client.models(), ["model-a", "model-b"])


class ConversionTests(Fixture):
    def test_pages_go_as_stamped_images_and_land_in_the_conversion(self):
        self.pdf(["Eins", "Zwei"])
        ref = self.view().documents[0].ref
        api = self.api([f"[[{ref} p.1]]\n\nEins\n\n[[{ref} p.2]]\n\nZwei"])
        report = api_mode.convert_pdfs(self.view(), api.client)
        content = api.requests[0]["messages"][0]["content"]
        self.assertEqual([part["type"] for part in content], ["text", "image_url", "image_url"])
        self.assertIn(f"[[{ref} p.2]]", content[0]["text"])
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(report.imported_pages, 2)
        self.assertEqual(self.view().documents[0].pages[1].markdown, "Zwei")

    def test_a_dropped_page_is_asked_for_again(self):
        self.pdf(["Eins", "Zwei"])
        ref = self.view().documents[0].ref
        api = self.api([f"[[{ref} p.1]]\n\nEins", f"[[{ref} p.2]]\n\nZwei"])
        report = api_mode.convert_pdfs(self.view(), api.client)
        self.assertEqual(len(api.requests), 2)
        self.assertEqual(report.documents[0].missing, [])
        self.assertEqual(report.documents[0].imported, [1, 2])

    def test_excluded_and_finished_documents_cost_nothing(self):
        self.pdf(["Eins"])
        document = self.view().documents[0]
        self.subject.set_document_excluded(document.key, True)
        api = self.api()
        api_mode.convert_pdfs(self.view(), api.client)
        self.assertEqual(api.requests, [])


def record(**fields) -> str:
    return json.dumps(fields)


HEAD = record(t="head", schema="universe.reply/2", export="x1", part=1)
TOPIC = record(t="topic", id="t_a", title="Bilanz", lectures=[], importance=3, why="wichtig")
END = record(t="end", records=1, more=False)


class ExchangeTests(Fixture):
    def run_exchange(self, api):
        store = StudyStore(self.subject)
        return store, api_mode.exchange(api.client, "PROMPT", self.view, store)

    def test_a_clean_reply_is_merged_in_one_call(self):
        api = self.api(["\n".join((HEAD, TOPIC, END))])
        store, result = self.run_exchange(api)
        self.assertEqual(len(api.requests), 1)
        self.assertIn("PROMPT", api.requests[0]["messages"][0]["content"])
        self.assertIn("t_a", store.knowledge().topics)
        self.assertEqual(len(result.steps), 1)

    def test_a_cut_off_reply_is_continued_with_its_history(self):
        api = self.api([("\n".join((HEAD, TOPIC)), "length"), END])
        store, result = self.run_exchange(api)
        self.assertEqual(len(api.requests), 2)
        self.assertEqual(api.requests[1]["messages"][-1]["content"], api_mode.CONTINUE)
        self.assertEqual(api.requests[1]["messages"][1]["role"], "assistant")
        self.assertFalse(result.checked.parsed.truncated)
        self.assertIn("t_a", store.knowledge().topics)

    def test_a_reply_without_records_raises_and_saves_nothing(self):
        api = self.api(["Sorry, I cannot do that."])
        store = StudyStore(self.subject)
        with self.assertRaises(api_mode.ApiError):
            api_mode.exchange(api.client, "PROMPT", self.view, store)
        self.assertFalse(store.knowledge().topics)

    def test_a_failed_fix_round_keeps_the_first_reply(self):
        broken = '{"t":"q","id":"x"}'
        api = self.api(["\n".join((HEAD, TOPIC, broken, END)), 400])
        store, result = self.run_exchange(api)
        self.assertIn("t_a", store.knowledge().topics)
        self.assertTrue(result.checked.errors)
        self.assertIn("400", result.problem)


class PanelRunTests(Fixture):
    def panel_with(self, api: FakeApi) -> ClaudePanel:
        settings = Settings(api_enabled=True, api_url=api.client.url, api_key=KEY, api_model="test-model")
        panel = ClaudePanel(self.professors, settings=settings)
        panel._confirm_api = lambda _what: True
        panel.subject = self.subject
        view = self.view()
        panel.view, panel.plan = view, panel._make_plan(view, self.subject, panel._choices())
        return panel

    def setUp(self):
        super().setUp()
        # On this thread, so no event loop runs: that would wake windows left over from other test files.
        patcher = mock.patch("universe.ui.claude_panel.workers.run", lambda worker, _pool=None: worker.run())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_button_converts_exports_and_imports(self):
        self.pdf(["Eins"])
        ref = self.view().documents[0].ref
        api = self.api([f"[[{ref} p.1]]\n\nEins", "\n".join((HEAD, TOPIC, END))])
        panel = self.panel_with(api)
        imported = []
        panel.replyImported.connect(lambda: imported.append(True))
        panel.run_api()
        self.assertEqual(len(api.requests), 2)
        self.assertIn("Eins", api.requests[1]["messages"][0]["content"])
        self.assertIn("t_a", StudyStore(self.subject).knowledge().topics)
        self.assertTrue(imported)
        self.assertTrue(panel.api_run_btn.isEnabled())
        self.assertIn("1 page", panel.api_report.text())

    def test_failure_is_shown_and_keeps_the_conversion(self):
        self.pdf(["Eins"])
        ref = self.view().documents[0].ref
        api = self.api([f"[[{ref} p.1]]\n\nEins", 401])
        panel = self.panel_with(api)
        panel.run_api()
        self.assertIn("401", panel.api_report.text())
        self.assertTrue(panel.api_run_btn.isEnabled())
        self.assertEqual(self.view().documents[0].pages[0].markdown, "Eins")

    def test_stop_ends_the_run_before_the_next_call(self):
        self.pdf(["Eins"])
        api = self.api()
        panel = self.panel_with(api)
        original, api_mode.client_from = api_mode.client_from, lambda _settings: api.client
        self.addCleanup(setattr, api_mode, "client_from", original)
        api.client.stop.set()
        panel.run_api()
        self.assertEqual(api.requests, [])
        self.assertIn("Cancelled", panel.api_report.text())


class SettingsTests(Fixture):
    def test_off_by_default(self):
        self.assertFalse(Settings().api_enabled)
        panel = ClaudePanel(self.professors, settings=Settings())
        self.assertTrue(panel.api_card.isHidden())

    def test_dialog_saves_and_preset_fills_in(self):
        settings = Settings()
        dialog = SettingsDialog(settings)
        dialog.api_enabled.setChecked(True)
        dialog.api_preset.setCurrentIndex(1)
        dialog._api_preset_chosen(1)
        dialog.api_key.setText(" secret ")
        dialog.accept()
        self.assertTrue(settings.api_enabled)
        self.assertEqual((settings.api_url, settings.api_model), ("https://api.meta.ai/v1", "muse-spark-1.3"))
        self.assertEqual(settings.api_key, "secret")
        self.assertEqual(stat.S_IMODE(settings_path().stat().st_mode), 0o600)
        self.assertEqual(Settings.load().api_key, "secret")
        settings_path().unlink()

    def test_panel_shows_the_card_once_enabled(self):
        settings = Settings(api_url="https://api.meta.ai/v1")
        panel = ClaudePanel(self.professors, settings=settings)
        settings.api_enabled = True
        panel.show_api_mode()
        self.assertFalse(panel.api_card.isHidden())
        self.assertIn("api.meta.ai", panel.api_note.text())

    def test_missing_key_is_reported_without_a_run(self):
        panel = ClaudePanel(self.professors, settings=Settings(api_url="https://a.example/v1", api_model="m"))
        panel._start_api("x", lambda client: self.fail("must not run"))
        self.assertIn("No API key", panel.api_report.text())


if __name__ == "__main__":
    unittest.main()
