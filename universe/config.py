"""Paths and persistent app settings.

Everything UniVerse writes lives under a single data root (default
``~/.universe_data``). Override it with the ``UNIVERSE_DATA_DIR`` environment
variable, e.g. to keep a separate workspace for testing.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path


def data_root() -> Path:
    root = Path(os.environ.get("UNIVERSE_DATA_DIR", Path.home() / ".universe_data"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def subjects_dir() -> Path:
    path = data_root() / "Subjects"
    path.mkdir(parents=True, exist_ok=True)
    return path


def professors_dir() -> Path:
    path = data_root() / "Professors"
    path.mkdir(parents=True, exist_ok=True)
    return path


def models_dir() -> Path:
    path = data_root() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return data_root() / "settings.json"


# Best first. English-only (".en") models always rank after multilingual ones.
MODEL_PREFERENCE = ("large-v3-turbo", "large-v3", "large", "medium", "small", "base", "tiny")
LANGUAGES = (
    ("auto", "Automatic"),
    ("de", "Deutsch"),
    ("en", "English"),
    ("fr", "Français"),
    ("es", "Español"),
    ("it", "Italiano"),
)


def installed_models() -> list[str]:
    """Whisper model files in models_dir(), best first."""

    def rank(name: str) -> tuple:
        stem = name.removeprefix("ggml-").removesuffix(".bin")
        pref = next((i for i, p in enumerate(MODEL_PREFERENCE) if stem.startswith(p)), len(MODEL_PREFERENCE))
        return (".en" in stem, pref, "-q" in stem, stem)

    return sorted((p.name for p in models_dir().glob("ggml-*.bin")), key=rank)


def _default_whisper_binary() -> str:
    # Homebrew's `whisper-cpp` formula installs the CLI as `whisper-cli`.
    for name in ("whisper-cli", "whisper-cpp", "main"):
        found = shutil.which(name)
        if found:
            return found
    return "whisper-cli"


@dataclass
class Settings:
    whisper_binary: str = field(default_factory=_default_whisper_binary)
    whisper_model: str = ""  # "" = best installed model; else a file in models_dir() or an absolute path
    whisper_language: str = "auto"  # default; each subject can override it
    whisper_threads: int = 8
    sample_rate: int = 16_000  # whisper.cpp expects 16 kHz mono
    input_device: str = ""  # microphone name; "" = system default
    auto_transcribe: bool = True
    battery_pause_below: int = 20  # pause background AI work on battery below this percent; 0 = never
    only_when_charging: bool = False  # run background AI work only while plugged in
    local_classify: bool = True  # sort transcripts with a local LLM after transcription, if one is running
    local_llm_url: str = ""  # OpenAI-compatible base URL, e.g. http://localhost:1234/v1; "" = probe usual ports
    local_llm_model: str = ""  # "" = first chat model the server lists
    local_llm_unload_after: int = 300  # seconds without AI work before the model is unloaded; 0 = keep loaded
    embedding_model: str = "all-MiniLM-L6-v2"  # used only if sentence-transformers is installed
    window_state: str = ""  # base64 QMainWindow state
    window_geometry: str = ""
    last_subject: str = ""
    last_lecture: str = ""
    settings_version: int = 2

    def whisper_model_path(self) -> Path:
        if not self.whisper_model:
            models = installed_models()
            return models_dir() / (models[0] if models else "ggml-large-v3-turbo-q5_0.bin")
        path = Path(self.whisper_model).expanduser()
        return path if path.is_absolute() else models_dir() / path

    def save(self) -> None:
        settings_path().write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> "Settings":
        path = settings_path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        if raw.get("settings_version", 1) < 2 and known.get("whisper_model") == "ggml-base.en.bin":
            known["whisper_model"] = ""  # v1 pinned the English-only model; let multilingual ones win
        known["settings_version"] = 2
        return cls(**known)
