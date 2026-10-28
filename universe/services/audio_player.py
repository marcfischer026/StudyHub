"""Lecture audio playback via ``pygame.mixer`` with seek support.

``pygame.mixer.music.get_pos()`` only reports time since the last ``play()``,
so the player tracks the seek offset itself to expose an absolute position.
"""

from __future__ import annotations

import os
import wave
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

try:
    import pygame
except ImportError:
    pygame = None


class AudioPlayer:
    def __init__(self):
        self.path: Path | None = None
        self.duration = 0.0
        self._offset = 0.0
        self._paused_pos: float | None = None
        self._ready = False

    @staticmethod
    def available() -> bool:
        return pygame is not None

    def _ensure_mixer(self) -> None:
        if pygame is None:
            raise RuntimeError("pygame is not installed — audio playback is disabled.")
        if not self._ready:
            pygame.mixer.init()
            self._ready = True

    def load(self, path: Path) -> None:
        self._ensure_mixer()
        pygame.mixer.music.load(str(path))
        self.path = path
        self.duration = _duration(path)
        self._offset = 0.0
        self._paused_pos = None

    @property
    def playing(self) -> bool:
        return self._ready and pygame.mixer.music.get_busy()

    def position(self) -> float:
        if self._paused_pos is not None:
            return self._paused_pos
        if not self.playing:
            return self._offset
        return self._offset + max(0, pygame.mixer.music.get_pos()) / 1000.0

    def play(self, start: float | None = None) -> None:
        if self.path is None:
            return
        self._ensure_mixer()
        if start is None:
            start = self._paused_pos if self._paused_pos is not None else self._offset
        if self.duration:
            start = min(max(0.0, start), self.duration)
        try:
            pygame.mixer.music.play(start=start)
        except pygame.error:
            # Some codecs cannot seek; fall back to playing from the top.
            pygame.mixer.music.play()
            start = 0.0
        self._offset = start
        self._paused_pos = None

    def pause(self) -> None:
        if self.playing:
            self._paused_pos = self.position()
            pygame.mixer.music.stop()

    def toggle(self) -> None:
        self.pause() if self.playing else self.play()

    def seek(self, seconds: float) -> None:
        if self.playing:
            self.play(seconds)
        else:
            self._paused_pos = None
            self._offset = max(0.0, seconds)

    def stop(self) -> None:
        if self._ready:
            pygame.mixer.music.stop()
        self._offset = 0.0
        self._paused_pos = None

    def unload(self) -> None:
        self.stop()
        if self._ready:
            pygame.mixer.music.unload()
        self.path = None
        self.duration = 0.0


def _duration(path: Path) -> float:
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as wav:
                return wav.getnframes() / float(wav.getframerate())
        except (wave.Error, OSError):
            pass
    if pygame is not None:
        try:
            return pygame.mixer.Sound(str(path)).get_length()
        except pygame.error:
            pass
    return 0.0
