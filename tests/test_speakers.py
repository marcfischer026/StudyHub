"""Tests for speaker recognition: loudness, clustering, lecturer naming, prompts and the UI."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from universe.config import Settings  # noqa: E402
from universe.services import claude_bridge, local_llm, speakers, transcriber  # noqa: E402
from universe.services.jobcontrol import Cancelled  # noqa: E402
from universe.services.transcriber import Segment, Transcript  # noqa: E402
from universe.storage import Workspace  # noqa: E402

RATE = speakers.SAMPLE_RATE
# (start, end, amplitude): a quiet, distant lecturer talks most; a loud student by the mic twice.
LAYOUT = [(0, 4, 0.02), (4, 6, 0.3), (6, 10, 0.02), (10, 10.5, 0.02), (10.5, 12, 0.3), (12, 16, 0.02)]


def write_wav(path: Path, layout=LAYOUT) -> Path:
    samples = np.zeros(int(layout[-1][1] * RATE), dtype=np.float32)
    rng = np.random.default_rng(0)
    for start, end, amp in layout:
        a, b = int(start * RATE), int(end * RATE)
        samples[a:b] = amp * rng.uniform(-1, 1, b - a) * np.sqrt(3)  # RMS ≈ amp
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes((samples * 32767).astype("<i2").tobytes())
    return path


def fake_embedder(samples: np.ndarray) -> np.ndarray:
    """Stands in for the ONNX model: "voice" = how loud the segment is."""
    loud = float(np.sqrt(np.mean(samples**2))) > 0.1
    return np.array([1.0, 0.05, 0.0] if loud else [0.0, 0.05, 1.0], dtype=np.float32)


def segments(layout=LAYOUT) -> list[Segment]:
    return [Segment(start, end, f"text {i}") for i, (start, end, _amp) in enumerate(layout)]


class ClusterTests(unittest.TestCase):
    def test_groups_similar_vectors(self):
        vectors = np.array([[1, 0], [0.9, 0.1], [0, 1], [0.1, 0.9], [1, 0.05]], dtype=np.float32)
        labels = speakers.cluster(vectors, threshold=0.5)
        self.assertEqual(labels[0], labels[1])
        self.assertEqual(labels[0], labels[4])
        self.assertEqual(labels[2], labels[3])
        self.assertNotEqual(labels[0], labels[2])

    def test_threshold_controls_merging(self):
        vectors = np.array([[1, 0], [0, 1]], dtype=np.float32)
        self.assertEqual(len(set(speakers.cluster(vectors, threshold=0.5))), 2)
        self.assertEqual(len(set(speakers.cluster(vectors, threshold=-0.5))), 1)
        self.assertEqual(speakers.cluster(np.zeros((0, 2)), 0.5), [])


class AnnotateTests(unittest.TestCase):
    def setUp(self):
        self.wav = write_wav(Path(tempfile.mkdtemp()) / "a.wav")

    def test_most_speaking_time_is_the_lecturer(self):
        segs = segments()
        self.assertTrue(speakers.annotate(segs, self.wav, Settings(), embedder=fake_embedder))
        self.assertEqual([s.speaker for s in segs][:3], ["lecturer", "speaker_2", "lecturer"])
        self.assertEqual(segs[4].speaker, "speaker_2")
        self.assertEqual(segs[3].speaker, "lecturer")  # too short to embed: nearest neighbour in time

    def test_loudness_is_measured_per_segment(self):
        segs = segments()
        speakers.annotate(segs, self.wav, Settings(), embedder=fake_embedder)
        self.assertAlmostEqual(segs[0].loudness, 20 * np.log10(0.02), delta=1.0)
        self.assertGreater(segs[1].loudness - segs[0].loudness, 20)
        self.assertEqual(speakers.near_mic(Transcript("a.wav", segs)), [False, True, False, False, True, False])

    def test_without_a_model_only_loudness_is_set(self):
        segs = segments()
        with mock.patch.object(speakers, "load_embedder", return_value=None):
            self.assertFalse(speakers.annotate(segs, self.wav, Settings()))
        self.assertTrue(all(s.loudness is not None and s.speaker == "" for s in segs))

    def test_missing_dependency_or_model_gives_no_embedder(self):
        with mock.patch.object(speakers, "models_dir", return_value=Path(tempfile.mkdtemp())):
            self.assertIsNone(speakers.load_embedder(Settings()))
        with mock.patch.dict(sys.modules, {"sherpa_onnx": None}):
            with mock.patch.object(speakers, "find_model", return_value=self.wav):
                self.assertIsNone(speakers.load_embedder(Settings()))

    def test_model_preference(self):
        folder = Path(tempfile.mkdtemp())
        for name in ("wespeaker_en_voxceleb_resnet34_LM.onnx", "nemo_en_titanet_small.onnx", "other.onnx"):
            (folder / name).write_bytes(b"")
        with mock.patch.object(speakers, "models_dir", return_value=folder):
            self.assertEqual(speakers.find_model(Settings()).name, "nemo_en_titanet_small.onnx")
            self.assertEqual(speakers.find_model(Settings(speaker_model="other.onnx")).name, "other.onnx")
            self.assertIsNone(speakers.find_model(Settings(speaker_model="missing.onnx")))

    def test_errors_never_break_transcription_but_cancel_does(self):
        segs = segments()
        with mock.patch.object(speakers, "annotate", side_effect=RuntimeError("broken model")):
            self.assertFalse(transcriber.identify_speakers(segs, self.wav, Settings()))
        with mock.patch.object(speakers, "annotate", side_effect=Cancelled()):
            with self.assertRaises(Cancelled):
                transcriber.identify_speakers(segs, self.wav, Settings())

    def test_old_transcripts_get_speakers_once(self):
        transcript = Transcript("a.wav", segments())
        with mock.patch.object(speakers, "load_embedder", return_value=fake_embedder):
            self.assertTrue(transcriber.identify_speakers_in(transcript, self.wav, Settings()))
            self.assertEqual(transcript.segments[1].speaker, "speaker_2")
            self.assertFalse(transcriber.identify_speakers_in(transcript, self.wav, Settings()))  # already done
        self.assertFalse(transcriber.identify_speakers_in(Transcript("x.wav", segments()), Path("/nope.wav"), Settings()))


class LabelTests(unittest.TestCase):
    def test_make_lecturer_swaps_voices(self):
        transcript = Transcript("a.wav", segments())
        for seg, who in zip(transcript.segments, ["lecturer", "speaker_2", "lecturer", "speaker_3", "speaker_2", ""]):
            seg.speaker = who
        self.assertEqual(speakers.make_lecturer(transcript, "speaker_2"), 4)
        self.assertEqual(
            [s.speaker for s in transcript.segments],
            ["speaker_2", "lecturer", "speaker_2", "speaker_3", "lecturer", ""],
        )
        self.assertEqual(speakers.make_lecturer(transcript, "lecturer"), 0)

    def test_names_and_prompt_tags(self):
        self.assertEqual(speakers.speaker_name("speaker_2"), "Speaker 2")
        self.assertEqual(speakers.prompt_tag(Segment(0, 1, "x", speaker="lecturer")), "[Lecturer] ")
        self.assertEqual(speakers.prompt_tag(Segment(0, 1, "x", speaker="speaker_2"), near=True), "[Speaker 2, near mic] ")
        self.assertEqual(speakers.prompt_tag(Segment(0, 1, "x")), "")

    def test_speaker_survives_save_and_load(self):
        folder = Path(tempfile.mkdtemp())
        path = Transcript("a.wav", [Segment(0, 1, "hi", speaker="speaker_2", loudness=-31.5)]).save(folder)
        seg = Transcript.load(path).segments[0]
        self.assertEqual((seg.speaker, seg.loudness), ("speaker_2", -31.5))


def tagged_transcript() -> Transcript:
    texts = ["Die Ableitung ist", "Hast du Zeit?", "eine Grenzwertbildung", "Ja klar", "f strich", "von x"]
    who = ["lecturer", "speaker_2", "lecturer", "speaker_2", "lecturer", "lecturer"]
    level = [-40, -25, -40, -41, -39, -40]
    return Transcript(
        "a.wav", [Segment(i, i + 1, t, speaker=w, loudness=l) for i, (t, w, l) in enumerate(zip(texts, who, level))]
    )


class PromptTests(unittest.TestCase):
    def test_classifier_prompt_carries_speaker_tags(self):
        seen = []

        def fake_chat(url, model, prompt, timeout=240, ttl=0):
            seen.append(prompt)
            return "S1.1 O"

        with mock.patch.object(local_llm, "find_server", return_value=("http://x/v1", "qwen")), mock.patch.object(
            local_llm, "chat", fake_chat
        ):
            local_llm.classify(tagged_transcript(), 1, "Mathe", "", Settings())
        self.assertIn("S1.0 [Lecturer] Die Ableitung ist", seen[0])
        self.assertIn("S1.1 [Speaker 2, near mic] Hast du Zeit?", seen[0])
        self.assertIn("S1.3 [Speaker 2] Ja klar", seen[0])
        self.assertIn("voice recognition", seen[0])

    def test_claude_export_carries_speaker_tags(self):
        lecture = Workspace(Path(tempfile.mkdtemp())).create_subject("Mathe").create_lecture("VL 1")
        tagged_transcript().save(lecture.dir("transcripts"))
        text = claude_bridge.build_context(lecture, None)
        self.assertIn("[Speaker 2, near mic] Hast du Zeit?", text)
        self.assertIn("[Lecturer] Die Ableitung ist", text)


class TranscriptPanelTests(unittest.TestCase):
    def test_shows_speakers_and_manual_correction_saves(self):
        from PySide6.QtWidgets import QApplication

        from universe.ui.transcript_panel import TranscriptPanel

        app = QApplication.instance() or QApplication([])  # noqa: F841
        lecture = Workspace(Path(tempfile.mkdtemp())).create_subject("Mathe").create_lecture("VL 1")
        tagged_transcript().save(lecture.dir("transcripts"))
        panel = TranscriptPanel()
        panel.set_lecture(lecture)
        self.assertIn("🎓", panel.list.item(0).text())
        self.assertIn("👥 Speaker 2:", panel.list.item(1).text())

        panel.filter.setCurrentText("👥 Other speakers")
        self.assertEqual([panel.list.item(i).isHidden() for i in range(6)], [True, False, True, False, True, True])

        panel._make_lecturer("speaker_2")
        saved = Transcript.load(lecture.transcripts()[0])
        self.assertEqual(saved.segments[1].speaker, "lecturer")
        self.assertEqual(saved.segments[0].speaker, "speaker_2")

        panel._set_speaker([panel.list.item(0)], speakers.LECTURER)
        self.assertEqual(Transcript.load(lecture.transcripts()[0]).segments[0].speaker, "lecturer")


if __name__ == "__main__":
    unittest.main()
