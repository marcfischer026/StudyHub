"""Tests for relevance labels: exam cues, local/Claude labels, private-talk filtering."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())

from universe.config import Settings  # noqa: E402
from universe.services import claude_bridge, local_llm, relevance  # noqa: E402
from universe.services.transcriber import Segment, Transcript  # noqa: E402
from universe.services.vector_index import HashingEmbedder, SubjectIndex  # noqa: E402
from universe.storage import Workspace  # noqa: E402

TEXTS = [
    "Der Kapitalwert ist die Summe der abgezinsten Zahlungen.",
    "Das kommt garantiert in der Klausur dran.",
    "Hast du heute Abend Zeit fürs Kino?",
    "Ja, um acht vielleicht.",
    "Gilt das auch bei nicht-flachen Zinsstrukturen?",
    "Nein, dann nehmen wir laufzeitabhängige Zinssätze.",
]


def make_lecture():
    lecture = Workspace(Path(tempfile.mkdtemp())).create_subject("Einführung ins BWL").create_lecture("VL 1")
    segments = [Segment(i * 10, i * 10 + 9, text) for i, text in enumerate(TEXTS)]
    Transcript("BWL 2026-10-28 15.30.wav", segments).save(lecture.dir("transcripts"))
    return lecture


def load(lecture) -> Transcript:
    return Transcript.load(lecture.transcripts()[0])


class ExamCueTests(unittest.TestCase):
    def test_cues_flag_only_matching_segments(self):
        transcript = Transcript("a.wav", [Segment(0, 1, t) for t in TEXTS])
        self.assertEqual(relevance.apply_exam_cues(transcript), 1)
        self.assertTrue(transcript.segments[1].exam)
        self.assertIn("Klausur", transcript.segments[1].note)
        self.assertFalse(transcript.segments[0].exam)

    def test_old_transcripts_without_labels_still_load(self):
        lecture = make_lecture()
        path = lecture.transcripts()[0]
        raw = json.loads(path.read_text())
        raw["segments"] = [{"start": 0, "end": 1, "text": "hi"}]
        path.write_text(json.dumps(raw))
        self.assertEqual(load(lecture).segments[0].category, "")


class LabelTests(unittest.TestCase):
    def test_ranges_and_manual_labels_win(self):
        lecture = make_lecture()
        transcript = load(lecture)
        relevance.set_manual(transcript.segments[3], category="lecture")
        transcript.save(lecture.dir("transcripts"))
        labels = [
            {"from": "S1.2", "to": "S1.3", "category": "off_topic", "reason": "Kino-Plan"},
            {"id": "S1.4", "category": "question"},
            {"id": "S1.5", "category": "answer"},
            {"id": "S9.1", "category": "off_topic"},  # unknown id: ignored
        ]
        relevance.apply_labels(lecture, labels, "local")
        segs = load(lecture).segments
        self.assertEqual(segs[2].category, "off_topic")
        self.assertEqual(segs[2].source, "local")
        self.assertEqual(segs[3].category, "lecture")  # set by hand → untouched
        self.assertEqual(segs[3].source, "manual")
        self.assertEqual((segs[4].category, segs[5].category), ("question", "answer"))

    def test_classifier_cannot_unflag_an_explicit_exam_cue(self):
        lecture = make_lecture()
        transcript = load(lecture)
        relevance.apply_exam_cues(transcript)
        transcript.save(lecture.dir("transcripts"))
        relevance.apply_labels(lecture, [{"id": "S1.1", "category": "lecture", "exam": False}], "local")
        self.assertTrue(load(lecture).segments[1].exam)


class PrivateTalkIsExcludedTests(unittest.TestCase):
    def setUp(self):
        self.lecture = make_lecture()
        relevance.apply_labels(
            self.lecture,
            [{"from": "S1.2", "to": "S1.3", "category": "off_topic"}, {"id": "S1.1", "exam": True}],
            "local",
        )

    def test_export_drops_private_talk_and_lists_exam_lines_first(self):
        text = claude_bridge.build_context(self.lecture, None)
        self.assertNotIn("Kino", text)
        self.assertIn("2 segment(s) of private talk were removed", text)
        self.assertIn("### Marked as exam-relevant", text)
        self.assertIn("[S1.0 ▶ 00:00:00]", text)
        self.assertIn('"segment_labels"', text)

    def test_search_index_skips_private_talk(self):
        index = SubjectIndex(self.lecture.subject, HashingEmbedder())
        index.index_lecture(self.lecture)
        texts = " ".join(row["text"] for row in index._table().to_arrow().to_pylist())
        self.assertIn("Kapitalwert", texts)
        self.assertNotIn("Kino", texts)


class ClaudeRoundTripTests(unittest.TestCase):
    def test_import_applies_segment_labels_by_exported_ids(self):
        lecture = make_lecture()
        claude_bridge.export_context(lecture, None)
        reply = {
            "summary": "s",
            "key_concepts": [],
            "exam_focus": [],
            "quizzes": [],
            "segment_labels": [
                {"from": "S1.2", "to": "S1.3", "category": "off_topic", "exam": False, "reason": "Privat"},
                {"from": "S1.1", "category": "lecture", "exam": True, "reason": "Klausurhinweis"},
            ],
        }
        response = claude_bridge.parse_response(json.dumps(reply))
        self.assertEqual(claude_bridge.apply_segment_labels(lecture, response), 3)
        segs = load(lecture).segments
        self.assertTrue(segs[2].private and segs[3].private)
        self.assertTrue(segs[1].exam)
        self.assertEqual(segs[1].source, "claude")

    def test_rejects_bad_categories(self):
        reply = {"summary": "s", "key_concepts": [], "exam_focus": [], "quizzes": [],
                 "segment_labels": [{"from": "S1.1", "category": "gossip"}]}
        with self.assertRaises(claude_bridge.ResponseError):
            claude_bridge.parse_response(json.dumps(reply))


class LocalLLMTests(unittest.TestCase):
    def test_classify_batches_and_defaults_to_lecture(self):
        transcript = Transcript("a.wav", [Segment(i, i + 1, t) for i, t in enumerate(TEXTS)])
        seen_prompts = []

        def fake_chat(url, model, prompt, timeout=240, ttl=0):
            seen_prompts.append(prompt)
            return "<think>hmm</think>S1.0 L\nS1.1 L* kommt in der Klausur\nS1.2 O\n- S1.3: O\n"

        originals = local_llm.find_server, local_llm.chat
        local_llm.find_server = lambda settings: ("http://x/v1", "qwen")
        local_llm.chat = fake_chat
        try:
            labels = {l["id"]: l for l in local_llm.classify(transcript, 1, "BWL", "Kapitalwert", Settings())}
        finally:
            local_llm.find_server, local_llm.chat = originals
        self.assertEqual(labels["S1.2"]["category"], "off_topic")
        self.assertEqual(labels["S1.0"]["category"], "lecture")
        self.assertTrue(labels["S1.1"]["exam"])
        self.assertEqual(labels["S1.3"]["category"], "off_topic")  # tolerates "- S1.3: O"
        self.assertEqual(len(labels), len(TEXTS))
        self.assertIn("S1.5 Nein, dann", seen_prompts[0])

    def test_parse_accepts_wrapped_and_bare_json_lists(self):
        items = [{"id": "S1.0", "category": "lecture", "exam": True, "reason": "exam"},
                 {"id": "S1.1", "category": "off_topic", "exam": False, "reason": "chatter"}]
        for reply in (json.dumps({"labels": items}), json.dumps(items),
                      "Here you go:\n```json\n" + json.dumps(items) + "\n```"):
            labels = local_llm._parse(reply)
            self.assertEqual([l["id"] for l in labels], ["S1.0", "S1.1"], reply)
            self.assertEqual(labels[1]["category"], "off_topic")
        self.assertEqual(local_llm._parse("no labels here"), [])

    def test_rank_models_prefers_benchmark_winner(self):
        models = ["google/gemma-4-12b", "qwen3-14b-mlx", "qwen3.5-9b-mlx"]
        self.assertEqual(local_llm.rank_models(models), ["qwen3.5-9b-mlx", "qwen3-14b-mlx", "google/gemma-4-12b"])
        self.assertEqual(local_llm.rank_models(["b", "a"]), ["b", "a"])  # unknown models keep server order

    def test_chat_prefills_empty_think_block(self):
        sent = []

        class FakeResponse:
            def read(self, *args):
                return json.dumps({"choices": [{"message": {"content": '{"labels": []}'}}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            sent.append(json.loads(request.data))
            return FakeResponse()

        original = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            self.assertEqual(local_llm.chat("http://x/v1", "qwen3.5-9b-mlx", "hi"), '{"labels": []}')
        finally:
            urllib.request.urlopen = original
        messages = sent[0]["messages"]
        self.assertEqual(messages[0], {"role": "user", "content": "hi"})
        self.assertEqual(messages[-1]["role"], "assistant")
        self.assertIn("</think>", messages[-1]["content"])

    def test_unavailable_server_raises(self):
        settings = Settings(local_llm_url="http://127.0.0.1:9/v1")
        with self.assertRaises(local_llm.LocalLLMUnavailable):
            local_llm.find_server(settings)

    def test_lm_studio_skips_models_that_cannot_chat(self):
        # LM Studio's /v1/models lists its speech model first; only /api/v1/models says it is no LLM.
        responses = {
            "http://lms/v1/models": {"data": [{"id": "phonon-40m-multilingual"}, {"id": "qwen3-14b-mlx"}]},
            "http://lms/api/v1/models": {
                "models": [
                    {"key": "qwen3-14b-mlx", "type": "llm", "loaded_instances": []},
                    {"key": "text-embedding-nomic-embed-text-v1.5", "type": "embedding"},
                ]
            },
        }

        class FakeResponse:
            def __init__(self, url):
                self.data = json.dumps(responses[url]).encode()

            def read(self, *args):
                return self.data

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        original = urllib.request.urlopen
        urllib.request.urlopen = lambda url, timeout=None: FakeResponse(url)
        try:
            found = local_llm.find_server(Settings(local_llm_url="http://lms/v1"))
        finally:
            urllib.request.urlopen = original
        self.assertEqual(found, ("http://lms/v1", "qwen3-14b-mlx"))


if __name__ == "__main__":
    unittest.main()
