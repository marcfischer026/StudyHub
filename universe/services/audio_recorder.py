"""Microphone recording with ``sounddevice`` straight to a 16 kHz mono WAV.

16 kHz / mono / int16 is exactly what whisper.cpp wants, so recordings can be
transcribed without any conversion step.
"""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import numpy as np

try:
    import sounddevice as sd
except (ImportError, OSError):  # OSError: PortAudio library missing
    sd = None


class RecorderUnavailable(RuntimeError):
    pass


class AudioRecorder:
    def __init__(self, sample_rate: int = 16_000, channels: int = 1):
        self.sample_rate = sample_rate
        self.channels = channels
        self._stream = None
        self._wav: wave.Wave_write | None = None
        self._lock = threading.Lock()
        self._started_at = 0.0
        self._paused_total = 0.0
        self._paused_at: float | None = None
        self.level = 0.0  # last RMS level, 0..1, for a VU meter
        self.peak_level = 0.0  # loudest level this recording; stays ~0 if the mic is blocked
        self._peak_sample = 0  # largest absolute int16 sample, for normalising on stop
        self.path: Path | None = None
        self.monitor = None  # optional live_check.LiveMonitor fed with every block

    @staticmethod
    def available() -> bool:
        return sd is not None

    def input_devices(self, refresh: bool = False) -> list[str]:
        """Names of available microphones. ``refresh`` re-scans hardware, so
        AirPods / iPhone (Continuity) mics that came or went show up correctly."""
        if sd is None:
            return []
        if refresh and not self.recording:
            sd._terminate()
            sd._initialize()
        return [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]

    @staticmethod
    def default_input_name() -> str:
        if sd is None:
            return ""
        try:
            return sd.query_devices(kind="input")["name"]
        except Exception:  # no input device at all
            return ""

    @staticmethod
    def _device_index(name: str) -> int | None:
        """Device indices shift when hardware changes, so devices are stored by name."""
        if not name:
            return None
        for index, device in enumerate(sd.query_devices()):
            if device["name"] == name and device["max_input_channels"] > 0:
                return index
        return None

    @property
    def recording(self) -> bool:
        return self._stream is not None

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    def elapsed(self) -> float:
        if not self.recording:
            return 0.0
        now = self._paused_at or time.monotonic()
        return now - self._started_at - self._paused_total

    def start(self, path: Path, device_name: str = "") -> None:
        """Record to ``path``; an unknown or empty ``device_name`` means the system default."""
        if sd is None:
            raise RecorderUnavailable("sounddevice/PortAudio is not available.")
        if self.recording:
            raise RuntimeError("Already recording.")
        device = self._device_index(device_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        wav = wave.open(str(path), "wb")
        wav.setnchannels(self.channels)
        wav.setsampwidth(2)
        wav.setframerate(self.sample_rate)
        self._wav, self.path = wav, path
        self._paused_total, self._paused_at = 0.0, None
        self.peak_level = 0.0
        self._peak_sample = 0
        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                device=device,
                callback=self._callback,
            )
            self._stream.start()
        except Exception:
            self._stream = None
            wav.close()
            path.unlink(missing_ok=True)
            raise
        self._started_at = time.monotonic()

    def _callback(self, indata, frames, time_info, status) -> None:
        if self._paused_at is not None:
            self.level = 0.0
            return
        samples = indata.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples**2)))
        # Meter in dB (-60 dBFS → 0, 0 dBFS → 1): normal speech sits mid-scale instead of near zero.
        self.level = float(max(0.0, min(1.0, (20 * np.log10(rms + 1e-9) + 60) / 60)))
        self.peak_level = max(self.peak_level, self.level)
        self._peak_sample = max(self._peak_sample, int(np.abs(indata.astype(np.int32)).max(initial=0)))
        with self._lock:
            if self._wav is not None:
                self._wav.writeframes(indata.tobytes())
        if self.monitor is not None:
            self.monitor.feed(indata)

    def pause(self) -> None:
        if self.recording and self._paused_at is None:
            self._paused_at = time.monotonic()

    def resume(self) -> None:
        if self._paused_at is not None:
            self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None

    def stop(self) -> Path | None:
        if self._stream is None:
            return None
        self._stream.stop()
        self._stream.close()
        self._stream = None
        with self._lock:
            if self._wav is not None:
                self._wav.close()
                self._wav = None
        self.level = 0.0
        self._paused_at = None
        if self.path is not None:
            normalize_wav(self.path, self._peak_sample)
        return self.path


TARGET_PEAK = int(32767 * 0.89)  # -1 dBFS
MAX_GAIN = 8.0  # +18 dB; more would mostly amplify room noise


def normalize_wav(path: Path, peak: int, chunk_frames: int = 16_000 * 30) -> float:
    """Raise a quiet 16-bit WAV so its loudest sample sits at -1 dBFS.

    Quiet laptop-mic recordings are hard to listen to; whisper copes either way.
    Works in chunks so hour-long lectures do not need to fit in memory.
    Returns the gain applied (1.0 = unchanged).
    """
    if peak <= 0:
        return 1.0
    gain = min(MAX_GAIN, TARGET_PEAK / peak)
    if gain < 1.1:  # already loud enough
        return 1.0
    tmp = path.with_suffix(".normalizing.wav")
    with wave.open(str(path), "rb") as src, wave.open(str(tmp), "wb") as dst:
        dst.setparams(src.getparams())
        while True:
            frames = src.readframes(chunk_frames)
            if not frames:
                break
            data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) * gain
            dst.writeframes(np.clip(data, -32768, 32767).astype(np.int16).tobytes())
    tmp.replace(path)
    return gain
