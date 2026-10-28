"""Offline speech-to-text through the whisper.cpp CLI.

Homebrew's ``whisper-cpp`` is built with Metal, so on Apple Silicon the GPU is
used automatically. Audio that is not already 16 kHz mono WAV is converted
with macOS's built-in ``afconvert`` (or ``ffmpeg`` when present).

Transcripts are stored per audio file as
``transcripts/<audio stem>.json`` (segments) and ``.txt`` (readable, with
``[▶ HH:MM:SS]`` stamps).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from ..config import Settings
from .jobcontrol import Cancelled, JobControl
from .timestamps import make_stamp

_PROGRESS_RE = re.compile(r"progress\s*=\s*(\d+)%")
# Whole-segment annotations such as "[INDISTINCT CHATTER]", "(Musik)", "* applause *".
_NOISE_RE = re.compile(r"^\s*(\[[^\]]*\]|\([^)]*\)|\*[^*]*\*)\s*$")


class TranscriptionError(RuntimeError):
    pass


@dataclass
class Segment:
    start: float
    end: float
    text: str
    # Relevance labels (see services/relevance.py). Empty category = not classified yet.
    category: str = ""  # "lecture" | "question" | "answer" | "off_topic"
    exam: bool = False  # flagged as exam-relevant
    note: str = ""  # why: exam cue found, classifier's reason, …
    source: str = ""  # who labelled it: "rules" | "local" | "claude" | "manual"
    # Who spoke (see services/speakers.py). Empty = unknown (no speaker model installed).
    speaker: str = ""  # "lecturer" | "speaker_2" | … | "other" (set by hand)
    loudness: float | None = None  # RMS level in dBFS; students next to the mic are loud

    @property
    def private(self) -> bool:
        return self.category == "off_topic"


@dataclass
class Transcript:
    audio: str
    segments: list[Segment]

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments)

    def to_stamped_text(self, include_private: bool = True) -> str:
        return "\n".join(
            f"{make_stamp(s.start)} {s.text}" for s in self.segments if include_private or not s.private
        )

    def save(self, folder: Path) -> Path:
        stem = Path(self.audio).stem
        json_path = folder / f"{stem}.json"
        json_path.write_text(
            json.dumps({"audio": self.audio, "segments": [asdict(s) for s in self.segments]}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (folder / f"{stem}.txt").write_text(self.to_stamped_text() + "\n", encoding="utf-8")
        return json_path

    @classmethod
    def load(cls, path: Path) -> "Transcript":
        raw = json.loads(path.read_text(encoding="utf-8"))
        fields = Segment.__dataclass_fields__
        segments = [Segment(**{k: v for k, v in s.items() if k in fields}) for s in raw.get("segments", [])]
        return cls(audio=raw.get("audio", path.stem), segments=segments)


def check_setup(settings: Settings) -> list[str]:
    """Human-readable problems that would stop transcription; empty if ready."""
    problems = []
    if not shutil.which(settings.whisper_binary) and not Path(settings.whisper_binary).exists():
        problems.append(f"whisper.cpp binary not found ({settings.whisper_binary}). Install with: brew install whisper-cpp")
    if not settings.whisper_model_path().exists():
        problems.append(f"Whisper model missing: {settings.whisper_model_path()} (see README → Speech-to-text).")
    return problems


def _is_whisper_ready_wav(path: Path) -> bool:
    if path.suffix.lower() != ".wav":
        return False
    try:
        with wave.open(str(path), "rb") as wav:
            return wav.getframerate() == 16_000 and wav.getnchannels() == 1 and wav.getsampwidth() == 2
    except (wave.Error, OSError):
        return False


def convert_to_wav(source: Path, target: Path) -> None:
    if shutil.which("afconvert"):
        cmd = ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(source), str(target)]
    elif shutil.which("ffmpeg"):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(target)]
    else:
        raise TranscriptionError("Cannot convert audio: neither afconvert nor ffmpeg is available.")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise TranscriptionError(f"Audio conversion failed: {result.stderr.strip()}")


def identify_speakers(segments: list[Segment], wav: Path, settings: Settings, control: "JobControl | None" = None) -> bool:
    """Loudness and speaker labels (optional extra): never lets transcription fail."""
    from . import speakers

    try:
        return speakers.annotate(segments, wav, settings, checkpoint=control.checkpoint if control else None)
    except Cancelled:
        raise
    except Exception:  # missing/broken model, odd audio: keep the transcript without speakers
        return False


def identify_speakers_in(transcript: Transcript, audio: Path, settings: Settings, control: "JobControl | None" = None) -> bool:
    """Add loudness/speakers to a transcript made before speaker recognition existed.
    Returns True if anything was set (the caller saves)."""
    if not audio.exists() or any(s.speaker or s.loudness is not None for s in transcript.segments):
        return False
    with tempfile.TemporaryDirectory(prefix="universe-speakers-") as tmp:
        wav = audio
        if not _is_whisper_ready_wav(audio):
            wav = Path(tmp) / "input.wav"
            try:
                convert_to_wav(audio, wav)
            except TranscriptionError:
                return False
        identify_speakers(transcript.segments, wav, settings, control)
    return any(s.loudness is not None for s in transcript.segments)


def transcribe(
    audio: Path,
    settings: Settings,
    progress: Callable[[int], None] | None = None,
    language: str | None = None,
    prompt: str = "",
    control: "JobControl | None" = None,
) -> Transcript:
    """``language`` is a whisper code ("de", "en", "auto"); ``prompt`` primes
    vocabulary, e.g. the subject name. ``control`` lets the caller pause
    (SIGSTOP) or cancel the whisper.cpp process."""
    problems = check_setup(settings)
    if problems:
        raise TranscriptionError("\n".join(problems))

    with tempfile.TemporaryDirectory(prefix="universe-whisper-") as tmp:
        tmp_dir = Path(tmp)
        wav = audio
        if not _is_whisper_ready_wav(audio):
            wav = tmp_dir / "input.wav"
            convert_to_wav(audio, wav)

        out_base = tmp_dir / "out"
        cmd = [
            settings.whisper_binary,
            "-m", str(settings.whisper_model_path()),
            "-f", str(wav),
            "-l", language or settings.whisper_language,
            "-t", str(settings.whisper_threads),
            "-sns",  # suppress non-speech tokens
            "-oj",
            "-of", str(out_base),
            "-pp",
        ]
        if prompt:
            cmd += ["--prompt", prompt]
        if control:
            control.checkpoint()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if control:
            control.register_process(proc)
        log_tail: list[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                log_tail = (log_tail + [line.rstrip()])[-20:]
                match = _PROGRESS_RE.search(line)
                if match and progress:
                    progress(int(match.group(1)))
            returncode = proc.wait()
        finally:
            if control:
                control.unregister_process(proc)
        if control and control.cancelled:
            raise Cancelled()
        if returncode != 0:
            raise TranscriptionError("whisper.cpp failed:\n" + "\n".join(log_tail))

        raw = json.loads(out_base.with_suffix(".json").read_text(encoding="utf-8"))
        segments = [
            Segment(
                start=item["offsets"]["from"] / 1000.0,
                end=item["offsets"]["to"] / 1000.0,
                text=item["text"].strip(),
            )
            for item in raw.get("transcription", [])
            if item.get("text", "").strip() and not _NOISE_RE.match(item["text"])
        ]
        identify_speakers(segments, wav, settings, control)
    if progress:
        progress(100)
    return Transcript(audio=audio.name, segments=segments)
