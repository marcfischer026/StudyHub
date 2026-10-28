"""Live check while recording: can the computer make out the lecturer?

You have no reference for how the laptop mic hears the room compared to your
ears. :class:`LiveMonitor` answers it while you record, so you can ask the
lecturer to speak up (or move closer) before the lecture is lost:

* **Clarity** — how far the lecturer's voice stands above the room's background
  noise (signal-to-noise ratio in dB), every second. Voices are told apart with
  the speaker model from ``speakers.py`` when installed: the voice heard most is
  the lecturer, louder voices from other clusters are neighbours.
* **Heard** — every 45 s to 3 min whisper.cpp transcribes the last 15 seconds
  and you see the text: compare it with what you heard. (whisper's own token
  probabilities are no use here: on pure noise it invents text "99 % sure".) The interval grows while all is
  well and the check pauses on battery under the same rules as background AI work,
  so loudness/voice tracking (well under 1 % CPU) is all that runs most of the time.

No Qt here: the recorder feeds audio blocks from its callback, a daemon thread
analyses, the UI reads :attr:`LiveMonitor.status`.
"""

from __future__ import annotations

import collections
import json
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from ..config import Settings
from . import speakers

RATE = speakers.SAMPLE_RATE
BUFFER_SECONDS = 30  # background noise is estimated over this much audio
FRAME = RATE // 20  # 50 ms loudness frames
WINDOW_SECONDS = 2.0  # one voice sample, analysed every STEP_SECONDS
STEP_SECONDS = 1.0
SPEECH_ABOVE_NOISE_DB = 6.0  # a window with frames this far above the floor contains speech
RECENT_SECONDS = 20.0  # the verdict looks at this much recent speech
LIVE_THRESHOLD = 0.35  # cosine similarity to a voice's centroid to count as that voice
MIN_LECTURER_SECONDS = 10.0  # talk time before a voice is called the lecturer
NEIGHBOUR_LOUDER_DB = 6.0
STEADY_STEPS = 3  # a new verdict must hold this many seconds before it is shown (no flicker)
# The whisper check is the only costly part (~3 s of GPU per check), so it is spaced out:
# often while the sound is doubtful, rarely while the lecturer is clear and understood.
LIVE_CHECK_SOON = 45.0
LIVE_CHECK_RELAXED = 180.0
LIVE_CHECK_SECONDS = 15.0

# Clarity verdicts by the lecturer's signal-to-noise ratio in the speech band (dB). Calibrated
# on a real lecture recording with added noise: clean chunks whisper got right measured
# 12–20 dB; once whisper got most words wrong it was ≤ 9 dB (README → Live check).
CLEAR_DB = 12.0
OKAY_DB = 9.0


@dataclass
class Heard:
    """One live whisper check."""

    at: float  # seconds into the recording (end of the checked audio)
    text: str
    state: str = ""  # clarity verdict when the audio was taken; text heard in "silence" is likely made up


@dataclass
class LiveStatus:
    state: str = "listening"  # listening | silence | clear | okay | quiet | neighbours | no_lecturer
    message: str = "Listening…"
    noise_db: float | None = None  # room background, dBFS
    voice_db: float | None = None  # lecturer's (or, without speaker model, any) speech level, dBFS
    snr_db: float | None = None  # voice above background
    neighbour_db: float | None = None  # loudest other voice recently, dBFS
    voices: bool = False  # True when voices are told apart (speaker model available)
    heard: Heard | None = None
    history: list[Heard] = field(default_factory=list)


@dataclass
class _Voice:
    total: np.ndarray  # sum of unit embeddings
    seconds: float = 0.0

    def centroid(self) -> np.ndarray:
        return self.total / max(np.linalg.norm(self.total), 1e-9)


def _db(rms: np.ndarray | float) -> np.ndarray | float:
    return 20 * np.log10(np.maximum(rms, 1e-6))


SPEECH_BAND = (300, 3400)  # Hz; room rumble and hiss outside it barely hurt recognition
_BAND = slice(SPEECH_BAND[0] * FRAME // RATE, SPEECH_BAND[1] * FRAME // RATE + 1)


def frame_levels(samples: np.ndarray, band: bool = True) -> np.ndarray:
    """Loudness in dBFS of each 50 ms frame, by default only within the speech band."""
    n = len(samples) // FRAME
    if n == 0:
        return np.zeros(0)
    frames = samples[: n * FRAME].reshape(n, FRAME).astype(np.float64)
    if not band:
        return _db(np.sqrt(np.mean(frames**2, axis=1)))
    spectrum = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    # Parseval: band energy → RMS of the band-limited signal.
    return _db(np.sqrt(2 * spectrum[:, _BAND].sum(axis=1)) / FRAME)


def verdict(snr: float | None, neighbour_louder: bool, voices: bool) -> tuple[str, str]:
    who = "Lecturer" if voices else "Speech"
    if snr is None:
        return ("no_lecturer", "Lecturer not heard recently") if voices else ("silence", "No speech stands out from the noise")
    if neighbour_louder:
        return "neighbours", "Neighbours are louder than the lecturer"
    if snr >= CLEAR_DB:
        return "clear", f"{who} clear"
    if snr >= OKAY_DB:
        return "okay", f"{who} okay — a bit quiet"
    return "quiet", f"{who} too quiet — ask to speak up"


class LiveMonitor:
    def __init__(
        self,
        settings: Settings,
        embedder: Callable[[np.ndarray], np.ndarray] | None = None,
        transcribe: Callable[[np.ndarray], Heard | None] | None = None,
        language: str | None = None,
        on_battery_saver: Callable[[], bool] | None = None,
    ):
        """``embedder`` defaults to the speaker model (if installed); ``transcribe``
        to a whisper.cpp run (``None`` and ``settings.live_check`` off = no text check)."""
        self.settings = settings
        self.language = language
        self._battery_saver = on_battery_saver or (lambda: battery_saver(settings))
        self._embedder = embedder
        self._transcribe = transcribe
        self._buffer: collections.deque[np.ndarray] = collections.deque()
        self._buffered = 0
        self._fed = 0  # samples received in total = position in the recording
        self._analysed = 0
        self._lock = threading.Lock()
        self._voices: list[_Voice] = []
        self._windows: collections.deque[tuple[float, float, int]] = collections.deque()  # (time, dB, voice or -1)
        self._noise: float | None = None
        self._pending: tuple[str, int] = ("", 0)  # verdict waiting to be shown, how many steps in a row
        self._check_thread: threading.Thread | None = None
        self._history: list[Heard] = []  # live whisper checks, newest last (written by the check thread)
        self._last_check: float | None = None  # first check as soon as there is enough audio
        self._checking = False
        self.status = LiveStatus()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- input (audio thread) ----------------------------------------------

    def feed(self, block: np.ndarray) -> None:
        """int16 or float samples straight from the recorder callback. Cheap: only buffers."""
        samples = block.reshape(-1)
        if samples.dtype == np.int16:
            samples = samples.astype(np.float32) / 32768.0
        with self._lock:
            self._buffer.append(samples)
            self._buffered += len(samples)
            self._fed += len(samples)
            while self._buffered - len(self._buffer[0]) >= BUFFER_SECONDS * RATE:
                self._buffered -= len(self._buffer.popleft())

    def _recent(self, seconds: float) -> np.ndarray:
        with self._lock:
            data = np.concatenate(list(self._buffer)) if self._buffer else np.zeros(0, np.float32)
        return data[-int(seconds * RATE) :]

    # --- analysis (monitor thread) -----------------------------------------

    def start(self) -> None:
        if self._transcribe is None and self.settings.live_check:
            self._transcribe = lambda samples: whisper_check(samples, self.settings, self.language)
        self._thread = threading.Thread(target=self._run, name="live-check", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        if self._embedder is None:
            try:  # loaded here, not in start(), so pressing Record stays instant
                self._embedder = speakers.load_embedder(self.settings)
            except Exception:  # broken model: still measure loudness
                self._embedder = None
        while not self._stop.wait(STEP_SECONDS / 2):
            try:
                self.step()
            except Exception:  # never let the monitor take the recording down
                continue

    def step(self) -> bool:
        """Analyse the newest window if a full step of new audio arrived. Returns True if it did."""
        if self._fed - self._analysed < STEP_SECONDS * RATE:
            return False
        self._analysed = self._fed
        now = self._fed / RATE
        buffer = self._recent(BUFFER_SECONDS)
        levels = frame_levels(buffer)
        if levels.size < 10:
            return False
        self._noise = float(np.percentile(levels, 10))
        window = buffer[-int(WINDOW_SECONDS * RATE) :]
        window_levels = levels[-int(WINDOW_SECONDS * RATE) // FRAME :]
        loud = window_levels[window_levels > self._noise + SPEECH_ABOVE_NOISE_DB]
        if loud.size >= len(window_levels) // 4:  # at least a quarter of the window is speech
            # The loudest syllables against the background floor (p90 − p10) tracked
            # recognition best in calibration; a median of all speech frames did not.
            self._windows.append((now, float(np.percentile(window_levels, 90)), self._voice_of(window)))
        while self._windows and self._windows[0][0] < now - RECENT_SECONDS:
            self._windows.popleft()
        self._update_status()
        self._maybe_check(now)
        return True

    def _voice_of(self, window: np.ndarray) -> int:
        if self._embedder is None:
            return -1
        try:
            vector = np.asarray(self._embedder(window), dtype=np.float64)
        except ValueError:
            return -1
        unit = vector / max(np.linalg.norm(vector), 1e-9)
        sims = [float(v.centroid() @ unit) for v in self._voices]
        best = int(np.argmax(sims)) if sims else -1
        if best < 0 or sims[best] < LIVE_THRESHOLD:
            self._voices.append(_Voice(np.zeros_like(unit)))
            best = len(self._voices) - 1
        self._voices[best].total += unit
        self._voices[best].seconds += STEP_SECONDS
        return best

    def lecturer(self) -> int | None:
        if not self._voices:
            return None
        best = max(range(len(self._voices)), key=lambda i: self._voices[i].seconds)
        return best if self._voices[best].seconds >= MIN_LECTURER_SECONDS else None

    def _update_status(self) -> None:
        voices = self._embedder is not None
        lecturer = self.lecturer()
        learning = voices and lecturer is None  # speaker model present, lecturer not known yet
        if voices and lecturer is not None:
            mine = [db for _t, db, v in self._windows if v == lecturer]
            others = [db for _t, db, v in self._windows if v not in (lecturer, -1)]
        else:
            mine, others = [db for _t, db, _v in self._windows], []
        voice = float(np.median(mine)) if mine else None
        neighbour = float(np.percentile(others, 75)) if len(others) >= 2 else None
        snr = voice - self._noise if voice is not None and self._noise is not None else None
        louder = voice is not None and neighbour is not None and neighbour - voice >= NEIGHBOUR_LOUDER_DB
        state, message = verdict(snr, louder, voices and not learning)
        if learning and snr is not None:
            message += " (learning voices…)"
        old = self.status
        streak = self._pending[1] + 1 if self._pending[0] == state else 1
        self._pending = (state, streak)
        if state != old.state and streak < STEADY_STEPS and old.state != "listening":
            state, message = old.state, old.message
        self.status = LiveStatus(state, message, self._noise, voice, snr, neighbour, voices, *self._heard())

    def _heard(self) -> tuple[Heard | None, list[Heard]]:
        history = self._history
        return (history[-1] if history else None), history

    def check_interval(self) -> float:
        heard = self.status.heard
        fine = self.status.state == "clear" and heard is not None and heard.state == "clear"
        return LIVE_CHECK_RELAXED if fine else LIVE_CHECK_SOON

    def _maybe_check(self, now: float) -> None:
        if self._transcribe is None or self._checking or now < LIVE_CHECK_SECONDS:
            return
        due = self._last_check is None or now - self._last_check >= self.check_interval()
        if self.status.state in ("silence", "listening") or not due:
            return
        if self._battery_saver():
            return
        self._last_check = now
        self._checking = True
        state = self.status.state
        samples = self._recent(LIVE_CHECK_SECONDS)

        def run():
            try:
                heard = self._transcribe(samples)
            except Exception:
                heard = None
            finally:
                self._checking = False
            if heard is not None:
                heard.at, heard.state = now, state
                self._history = (self._history + [heard])[-20:]
                self.status = LiveStatus(**{**self.status.__dict__, "heard": heard, "history": self._history})

        self._check_thread = threading.Thread(target=run, name="live-whisper", daemon=True)
        self._check_thread.start()


def battery_saver(settings: Settings) -> bool:
    """True when background AI work would be paused now (Settings → battery rules)."""
    from .system_monitor import SystemMonitor

    try:
        percent, plugged = SystemMonitor().battery()
    except Exception:
        return False
    if plugged is None or plugged:
        return False
    if settings.only_when_charging:
        return True
    return bool(settings.battery_pause_below) and percent is not None and percent < settings.battery_pause_below


# --- whisper ---------------------------------------------------------------


def heard_text(whisper_json: dict) -> str:
    return " ".join(t for s in whisper_json.get("transcription", []) if (t := s.get("text", "").strip()))


def whisper_check(samples: np.ndarray, settings: Settings, language: str | None = None) -> Heard | None:
    """Transcribe a few seconds with whisper.cpp; None if whisper is not set up."""
    from .transcriber import check_setup

    if check_setup(settings):
        return None
    with tempfile.TemporaryDirectory(prefix="universe-live-") as tmp:
        wav_path = Path(tmp) / "live.wav"
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            wav.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
        out = Path(tmp) / "live"
        cmd = [
            settings.whisper_binary,
            "-m", str(settings.whisper_model_path()),
            "-f", str(wav_path),
            "-l", language or settings.whisper_language,
            "-t", "4",
            "-sns",
            "-oj",
            "-of", str(out),
            "-np",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return None
        text = heard_text(json.loads(out.with_suffix(".json").read_text(encoding="utf-8")))
    return Heard(at=0.0, text=text)
