"""Subject / lecture workspace on disk.

Layout::

    ~/.universe_data/Subjects/<Subject>/
        subject.json
        .index/                      LanceDB vector store for the subject
        exams/                       past exams (PDFs), shared by all lectures
        exercises/                   exercise sheets (PDFs)
        Lectures/<Lecture>/
            lecture.json
            notes.md                 Markdown notes with [▶ HH:MM:SS] stamps
            audio/                   recordings ("BWL 2026-10-28 15.30.wav") and imported audio
            slides/                  PDFs
            transcripts/             whisper.cpp output (*.json, *.txt)
            ai/                      Context.md exports and imported AI responses
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config

LECTURE_SUBDIRS = ("audio", "slides", "transcripts", "ai")
SUBJECT_SUBDIRS = ("exams", "exercises")
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".ogg", ".flac"}

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# Names written by UniVerse before recordings were named "<abbr> <date> <time>".
_LEGACY_RECORDING = re.compile(r"recording_(\d{8}_\d{6})")
# "<abbr> 2026-10-28 15.30" or "... (2)": UniVerse's own recording names.
_RECORDING_NAME = re.compile(r"(?P<abbr>.*\S) (?P<when>\d{4}-\d{2}-\d{2} \d{2}\.\d{2})(?: \(\d+\))?")
# Date and time in other names, e.g. Voice Memos' "20261022 101500".
_COMPACT_TIME = re.compile(r"(?<!\d)(\d{8})[ _-](\d{6})(?!\d)")
# Words skipped when building an abbreviation from initials.
_FILLER_WORDS = {
    "einführung", "grundlagen", "in", "ins", "im", "in die", "der", "die", "das", "des", "und", "zur", "zum",
    "von", "für", "an", "am", "introduction", "intro", "to", "into", "of", "the", "and", "for", "a", "an",
}


def abbreviate(name: str) -> str:
    """Short subject code for file names: "Einführung ins BWL" → "BWL",
    "Corporate Finance" → "CF", "Mikroökonomie" → "MIKR"."""
    words = re.findall(r"\w+", name)
    acronyms = [w for w in words if len(w) >= 2 and w.isupper() and w.isalpha()]
    if acronyms:
        return acronyms[0][:6]
    meaningful = [w for w in words if w.lower() not in _FILLER_WORDS] or words
    if not meaningful:
        return "REC"
    if len(meaningful) == 1:
        return meaningful[0][:4].upper()
    return "".join(w[0] for w in meaningful[:4]).upper()


def safe_name(name: str) -> str:
    """Turn a user-facing name into a folder name, keeping it readable."""
    cleaned = _UNSAFE.sub("_", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        raise ValueError("Name must contain at least one usable character.")
    return cleaned[:120]


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def recording_time(path: Path) -> datetime:
    """When a recording was made: from its name if UniVerse (or Voice Memos) named
    it, else the file's modification time. Used to keep recordings in order."""
    match = _RECORDING_NAME.fullmatch(path.stem)
    if match:
        return datetime.strptime(match.group("when"), "%Y-%m-%d %H.%M")
    for pattern, fmt in ((_LEGACY_RECORDING, "%Y%m%d_%H%M%S"), (_COMPACT_TIME, "%Y%m%d %H%M%S")):
        found = pattern.search(path.stem)
        if found:
            try:
                return datetime.strptime(" ".join(found.groups()) if pattern is _COMPACT_TIME else found.group(1), fmt)
            except ValueError:
                pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return datetime.min


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Lecture:
    subject: "Subject"
    path: Path

    @property
    def name(self) -> str:
        return self.meta.get("name", self.path.name)

    @property
    def meta(self) -> dict:
        return _read_json(self.path / "lecture.json")

    def update_meta(self, **changes) -> None:
        meta = self.meta
        meta.update(changes)
        _write_json(self.path / "lecture.json", meta)

    @property
    def professor_id(self) -> str:
        """Lecture-level professor, falling back to the subject default."""
        return self.meta.get("professor_id") or self.subject.professor_id

    def dir(self, kind: str) -> Path:
        if kind not in LECTURE_SUBDIRS:
            raise ValueError(f"Unknown lecture folder: {kind}")
        path = self.path / kind
        path.mkdir(exist_ok=True)
        return path

    @property
    def notes_path(self) -> Path:
        return self.path / "notes.md"

    def read_notes(self) -> str:
        return self.notes_path.read_text(encoding="utf-8") if self.notes_path.exists() else ""

    def write_notes(self, markdown: str) -> None:
        self.notes_path.write_text(markdown, encoding="utf-8")

    def pdfs(self) -> list[Path]:
        return sorted(self.dir("slides").glob("*.pdf"))

    def audio_files(self) -> list[Path]:
        """In the order they were recorded, so the newest recording is last."""
        files = [p for p in self.dir("audio").iterdir() if p.suffix.lower() in AUDIO_SUFFIXES]
        return sorted(files, key=lambda p: (recording_time(p), p.name))

    def new_recording_path(self, when: datetime | None = None) -> Path:
        """``audio/BWL 2026-10-28 15.30.wav`` — subject abbreviation, date, time."""
        when = when or datetime.now()
        base = f"{safe_name(self.subject.short_name)} {when:%Y-%m-%d %H.%M}"
        path, n = self.dir("audio") / f"{base}.wav", 2
        while path.exists():
            path, n = self.dir("audio") / f"{base} ({n}).wav", n + 1
        return path

    def rename_audio(self, audio: Path, new_stem: str) -> Path:
        """Rename a recording together with its transcripts."""
        target = audio.with_name(new_stem + audio.suffix)
        if target.exists():
            raise FileExistsError(f"{target.name} already exists.")
        audio.rename(target)
        transcripts = self.dir("transcripts")
        txt = transcripts / f"{audio.stem}.txt"
        if txt.exists():
            txt.rename(transcripts / f"{new_stem}.txt")
        js = transcripts / f"{audio.stem}.json"
        if js.exists():
            data = _read_json(js)
            data["audio"] = target.name
            _write_json(transcripts / f"{new_stem}.json", data)
            js.unlink()
        return target

    def audio_bundle(self, audio: Path) -> list[Path]:
        """The recording and the transcript files made from it — what deleting it removes."""
        transcripts = self.dir("transcripts")
        extra = [transcripts / f"{audio.stem}{suffix}" for suffix in (".json", ".txt")]
        return [audio, *(p for p in extra if p.exists())]

    def move_audio(self, audio: Path, target: "Lecture") -> Path:
        """Move a recording and its transcripts to another lecture.

        A recording UniVerse named after this subject ("BWL 2026-10-28 15.30")
        takes the target subject's abbreviation, so names keep saying where it
        belongs. Returns the new path."""
        if target.path == self.path:
            return audio
        match = _RECORDING_NAME.fullmatch(audio.stem)
        if match and match.group("abbr") == safe_name(self.subject.short_name):
            stem = target.new_recording_path(recording_time(audio)).stem
            if audio.suffix.lower() != ".wav":  # new_recording_path de-duplicates .wav names only
                stem = _free_stem(target.dir("audio"), stem, audio.suffix)
        else:
            stem = _free_stem(target.dir("audio"), audio.stem, audio.suffix)
        new_audio = target.dir("audio") / f"{stem}{audio.suffix}"
        shutil.move(str(audio), str(new_audio))
        source, dest = self.dir("transcripts"), target.dir("transcripts")
        txt = source / f"{audio.stem}.txt"
        if txt.exists():
            shutil.move(str(txt), str(dest / f"{stem}.txt"))
        js = source / f"{audio.stem}.json"
        if js.exists():
            data = _read_json(js)
            data["audio"] = new_audio.name
            _write_json(dest / f"{stem}.json", data)
            js.unlink()
        return new_audio

    def migrate_recording_names(self) -> bool:
        """Rename old ``recording_YYYYMMDD_HHMMSS`` files to the readable scheme.
        Returns True if anything was renamed."""
        changed = False
        for audio in self.audio_files():
            match = _LEGACY_RECORDING.fullmatch(audio.stem)
            if match:
                when = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
                self.rename_audio(audio, self.new_recording_path(when).stem)
                changed = True
        return changed

    def transcripts(self) -> list[Path]:
        """whisper.cpp JSON transcripts, one per audio file, in recording order."""
        audio = {p.stem: p for p in self.audio_files()}
        return sorted(
            self.dir("transcripts").glob("*.json"),
            key=lambda t: (recording_time(audio.get(t.stem, t)), t.name),
        )

    def import_file(self, source: Path, kind: str) -> Path:
        """Copy an external file (PDF, audio) into this lecture."""
        return import_file(source, self.dir(kind))


def import_file(source: Path, folder: Path) -> Path:
    """Copy ``source`` into ``folder``, adding " (1)", " (2)"… if the name is taken."""
    target = folder / source.name
    stem, suffix, n = target.stem, target.suffix, 1
    while target.exists():
        target = target.with_name(f"{stem} ({n}){suffix}")
        n += 1
    shutil.copy2(source, target)
    return target


def _free_stem(folder: Path, stem: str, suffix: str) -> str:
    """``stem``, or ``stem (2)``, ``stem (3)``… — whichever is not taken in ``folder``."""
    candidate, n = stem, 2
    while (folder / f"{candidate}{suffix}").exists():
        candidate, n = f"{stem} ({n})", n + 1
    return candidate


@dataclass(frozen=True)
class Subject:
    path: Path

    @property
    def name(self) -> str:
        return self.meta.get("name", self.path.name)

    @property
    def meta(self) -> dict:
        return _read_json(self.path / "subject.json")

    def update_meta(self, **changes) -> None:
        meta = self.meta
        meta.update(changes)
        _write_json(self.path / "subject.json", meta)

    @property
    def professor_id(self) -> str:
        return self.meta.get("professor_id", "")

    @property
    def short_name(self) -> str:
        """Abbreviation used in recording file names; set by the user or derived from the name."""
        return self.meta.get("short_name") or abbreviate(self.name)

    def dir(self, kind: str) -> Path:
        if kind not in SUBJECT_SUBDIRS:
            raise ValueError(f"Unknown subject folder: {kind}")
        path = self.path / kind
        path.mkdir(exist_ok=True)
        return path

    @property
    def lectures_dir(self) -> Path:
        path = self.path / "Lectures"
        path.mkdir(exist_ok=True)
        return path

    @property
    def index_dir(self) -> Path:
        return self.path / ".index"

    def lectures(self) -> list[Lecture]:
        found = [Lecture(self, p) for p in self.lectures_dir.iterdir() if (p / "lecture.json").exists()]
        return sorted(found, key=lambda lec: (lec.meta.get("created", ""), lec.name))

    def lecture(self, name: str) -> Lecture | None:
        path = self.lectures_dir / safe_name(name)
        return Lecture(self, path) if (path / "lecture.json").exists() else None

    def create_lecture(self, name: str, professor_id: str = "") -> Lecture:
        path = self.lectures_dir / safe_name(name)
        if path.exists():
            raise FileExistsError(f"Lecture '{name}' already exists in {self.name}.")
        path.mkdir(parents=True)
        for sub in LECTURE_SUBDIRS:
            (path / sub).mkdir()
        _write_json(path / "lecture.json", {"name": name, "created": _now(), "professor_id": professor_id})
        (path / "notes.md").write_text(f"# {name}\n\n", encoding="utf-8")
        return Lecture(self, path)

    def rename_lecture(self, lecture: Lecture, new_name: str) -> Lecture:
        target = self.lectures_dir / safe_name(new_name)
        if target.exists() and target != lecture.path:
            raise FileExistsError(f"Lecture '{new_name}' already exists.")
        lecture.path.rename(target)
        moved = Lecture(self, target)
        moved.update_meta(name=new_name)
        return moved

    def delete_lecture(self, lecture: Lecture) -> None:
        shutil.rmtree(lecture.path)


class Workspace:
    """All subjects under the data root."""

    def __init__(self, root: Path | None = None):
        self.root = root or config.subjects_dir()
        self.root.mkdir(parents=True, exist_ok=True)

    def subjects(self) -> list[Subject]:
        found = [Subject(p) for p in self.root.iterdir() if (p / "subject.json").exists()]
        return sorted(found, key=lambda s: s.name.lower())

    def subject(self, name: str) -> Subject | None:
        path = self.root / safe_name(name)
        return Subject(path) if (path / "subject.json").exists() else None

    def create_subject(self, name: str, description: str = "", professor_id: str = "") -> Subject:
        path = self.root / safe_name(name)
        if path.exists():
            raise FileExistsError(f"Subject '{name}' already exists.")
        (path / "Lectures").mkdir(parents=True)
        _write_json(
            path / "subject.json",
            {"name": name, "description": description, "created": _now(), "professor_id": professor_id},
        )
        return Subject(path)

    def rename_subject(self, subject: Subject, new_name: str) -> Subject:
        target = self.root / safe_name(new_name)
        if target.exists() and target != subject.path:
            raise FileExistsError(f"Subject '{new_name}' already exists.")
        subject.path.rename(target)
        moved = Subject(target)
        moved.update_meta(name=new_name)
        return moved

    def delete_subject(self, subject: Subject) -> None:
        shutil.rmtree(subject.path)
