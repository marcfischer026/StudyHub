"""Tests for the live recording check: clarity verdicts, neighbours, whisper checks, battery."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from universe.config import Settings  # noqa: E402
from universe.services import live_check as lc  # noqa: E402
from universe.services.audio_recorder import AudioRecorder  # noqa: E402

RATE = lc.RATE
FLOOR = 0.001  # room background


def speech(seconds: float, amplitude: float, seed: int = 0) -> np.ndarray:
    """Background noise with "syllables": 0.3 s bursts of noise, 0.2 s gaps."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    signal = rng.normal(0, FLOOR, n)
    on = (np.arange(n) % int(0.5 * RATE)) < int(0.3 * RATE)
    signal[on] += rng.normal(0, amplitude, on.sum())
    return signal.astype(np.float32)


def fake_embedder(window: np.ndarray) -> np.ndarray:
    """"Voice" = loudness: loud windows are a neighbour, the rest the lecturer."""
    loud = float(np.sqrt(np.mean(window**2))) > 0.02
    return np.array([1.0, 0.0] if loud else [0.0, 1.0])


def monitor(**kwargs) -> lc.LiveMonitor:
    kwargs.setdefault("embedder", fake_embedder)
    kwargs.setdefault("on_battery_saver", lambda: False)
    return lc.LiveMonitor(Settings(live_check=False), **kwargs)


def play(m: lc.LiveMonitor, audio: np.ndarray, block: int = 1600) -> list[str]:
    states = []
    for i in range(0, len(audio), block):
        m.feed((audio[i : i + block] * 32767).astype(np.int16).reshape(-1, 1))
        if m.step():
            states.append(m.status.state)
    return states


class ClarityTests(unittest.TestCase):
    def test_loud_lecturer_is_clear(self):
        m = monitor()
        play(m, speech(30, 0.01))
        self.assertEqual(m.status.state, "clear")
        self.assertAlmostEqual(m.status.snr_db, 20, delta=4)
        self.assertTrue(m.status.voices)

    def test_quiet_lecturer_asks_to_speak_up(self):
        m = monitor()
        play(m, speech(30, 0.0022))
        self.assertEqual(m.status.state, "quiet")
        self.assertIn("speak up", m.status.message)

    def test_noise_only_is_silence(self):
        m = monitor()
        play(m, np.random.default_rng(1).normal(0, FLOOR, 20 * RATE).astype(np.float32))
        self.assertEqual(m.status.state, "silence")

    def test_without_speaker_model_measures_all_speech(self):
        m = monitor(embedder=None)
        play(m, speech(20, 0.01))
        self.assertEqual(m.status.state, "clear")
        self.assertEqual(m.status.message, "Speech clear")
        self.assertFalse(m.status.voices)

    def test_louder_neighbours_are_reported(self):
        m = monitor()
        states = play(m, np.concatenate([speech(25, 0.006), speech(8, 0.06, seed=2)]))
        lecturer = m.lecturer()
        self.assertEqual(lecturer, 0)  # the quiet voice talked most
        self.assertGreater(m._voices[lecturer].seconds, m._voices[1].seconds)
        self.assertIn("neighbours", states)

    def test_single_odd_second_does_not_flicker(self):
        m = monitor()
        play(m, speech(20, 0.01))
        m.status = lc.LiveStatus(**{**m.status.__dict__})
        m._pending = ("clear", 5)
        with mock.patch.object(lc, "verdict", return_value=("quiet", "too quiet")):
            play(m, speech(1, 0.01))
            self.assertEqual(m.status.state, "clear")
            play(m, speech(2, 0.01))
            self.assertEqual(m.status.state, "quiet")

    def test_speech_band_ignores_rumble(self):
        t = np.arange(RATE) / RATE
        hum = 0.1 * np.sin(2 * np.pi * 50 * t)
        voice = 0.1 * np.sin(2 * np.pi * 1000 * t)
        self.assertLess(lc.frame_levels(hum).mean(), lc.frame_levels(voice).mean() - 30)
        self.assertAlmostEqual(lc.frame_levels(voice).mean(), lc.frame_levels(voice, band=False).mean(), delta=1)


class WhisperCheckTests(unittest.TestCase):
    def test_checks_last_seconds_then_waits(self):
        calls = []

        def transcribe(samples):
            calls.append(len(samples) / RATE)
            return lc.Heard(at=0, text="Die Ableitung von f")

        m = monitor(transcribe=transcribe)
        play(m, speech(40, 0.01))
        m._check_thread.join(5)
        self.assertEqual(calls, [lc.LIVE_CHECK_SECONDS])
        self.assertEqual(m.status.heard.text, "Die Ableitung von f")
        self.assertEqual(m.status.heard.state, "clear")
        self.assertEqual(m.check_interval(), lc.LIVE_CHECK_RELAXED)  # all fine → check rarely

    def test_checks_sooner_when_sound_is_doubtful(self):
        m = monitor(transcribe=lambda s: lc.Heard(0, "…"))
        play(m, speech(30, 0.0022))
        self.assertEqual(m.check_interval(), lc.LIVE_CHECK_SOON)

    def test_battery_saver_skips_whisper(self):
        calls = []
        m = monitor(transcribe=lambda s: calls.append(1), on_battery_saver=lambda: True)
        play(m, speech(40, 0.01))
        self.assertEqual(calls, [])

    def test_battery_rules_follow_settings(self):
        with mock.patch("universe.services.system_monitor.SystemMonitor.battery", return_value=(15, False)):
            self.assertTrue(lc.battery_saver(Settings(battery_pause_below=20)))
            self.assertFalse(lc.battery_saver(Settings(battery_pause_below=0)))
        with mock.patch("universe.services.system_monitor.SystemMonitor.battery", return_value=(90, False)):
            self.assertTrue(lc.battery_saver(Settings(only_when_charging=True)))
        with mock.patch("universe.services.system_monitor.SystemMonitor.battery", return_value=(15, True)):
            self.assertFalse(lc.battery_saver(Settings(battery_pause_below=20)))

    def test_heard_text_from_whisper_json(self):
        raw = {"transcription": [{"text": " Hallo "}, {"text": ""}, {"text": "zusammen."}]}
        self.assertEqual(lc.heard_text(raw), "Hallo zusammen.")

    def test_no_whisper_setup_means_no_check(self):
        with mock.patch("universe.services.transcriber.check_setup", return_value=["missing"]):
            self.assertIsNone(lc.whisper_check(np.zeros(RATE, np.float32), Settings()))


class RecorderTests(unittest.TestCase):
    def test_recorder_feeds_the_monitor_unless_paused(self):
        recorder = AudioRecorder()
        fed = []
        recorder.monitor = mock.Mock(feed=fed.append)
        block = np.full((160, 1), 1000, dtype=np.int16)
        recorder._callback(block, 160, None, None)
        self.assertEqual(len(fed), 1)
        recorder._paused_at = 1.0
        recorder._callback(block, 160, None, None)
        self.assertEqual(len(fed), 1)


class ToolbarTextTests(unittest.TestCase):
    def test_summary_and_details(self):
        from universe.ui.audio import live_details, live_summary

        heard = lc.Heard(at=50, text="Die Ableitung", state="clear")
        status = lc.LiveStatus("quiet", "Lecturer too quiet — ask to speak up", -60, -52, 8, None, True, heard, [heard])
        self.assertEqual(live_summary(status, 60), "🔴 Lecturer too quiet — ask to speak up")
        details = live_details(status, 60)
        self.assertIn("8 dB above the room background", details)
        self.assertIn("Heard 10 s ago: “Die Ableitung”", details)
        self.assertNotIn("Heard", live_details(status, 500))  # stale check not shown


if __name__ == "__main__":
    unittest.main()
