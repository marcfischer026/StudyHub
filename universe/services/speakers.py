"""Who spoke: offline speaker recognition for transcript segments.

The laptop mic catches the lecturer (distant, reverberant) and students next to
it (close, loud). Two signals help tell them apart:

1. **loudness** — RMS level of each segment in dBFS. Always computed; needs only numpy.
2. **speaker** — a voice embedding per segment from a small ONNX model run with
   ``sherpa-onnx``, clustered by cosine similarity. The voice with the most total
   speaking time is the ``"lecturer"``, the others ``"speaker_2"``, ``"speaker_3"``, …

Optional: without ``sherpa-onnx`` or a model file in ``~/.universe_data/models/``
segments simply get no speaker, like the local LLM is skipped when no server runs.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import numpy as np

from ..config import Settings, models_dir

if TYPE_CHECKING:
    from .transcriber import Segment, Transcript

LECTURER = "lecturer"
OTHER = "other"  # set by hand: "someone else said this"
SAMPLE_RATE = 16_000
MIN_EMBED_SECONDS = 1.0  # shorter segments take the speaker of their nearest neighbour in time
MAX_EMBED_SECONDS = 15.0  # enough for a stable embedding, keeps long segments fast
NEAR_MIC_DB = 6.0  # this much louder than the transcript's median → probably next to the laptop
# Best first, measured on a German lecture mixed with student chatter (README → Speaker
# recognition): NeMo TitaNet-small separated them cleanly; WeSpeaker ResNet34 did not.
MODEL_PREFERENCE = ("nemo_en_titanet", "3dspeaker", "wespeaker")


def speaker_name(tag: str) -> str:
    """"lecturer" → "Lecturer", "speaker_2" → "Speaker 2"."""
    if tag == LECTURER:
        return "Lecturer"
    if tag == OTHER:
        return "Other speaker"
    return tag.replace("_", " ").capitalize()


# --- audio -----------------------------------------------------------------


def read_wav(path: Path) -> np.ndarray:
    """16 kHz mono 16-bit WAV (what whisper.cpp gets) as float32 in [-1, 1]."""
    with wave.open(str(path), "rb") as wav:
        if wav.getframerate() != SAMPLE_RATE or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError(f"{path.name} is not 16 kHz mono 16-bit")
        frames = wav.readframes(wav.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def _slice(samples: np.ndarray, seg: "Segment") -> np.ndarray:
    return samples[int(seg.start * SAMPLE_RATE) : int(seg.end * SAMPLE_RATE)]


def loudness_db(samples: np.ndarray) -> float | None:
    """RMS level in dBFS (0 = full scale, quiet speech around -40), None for no audio."""
    if samples.size == 0:
        return None
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    return round(20 * np.log10(max(rms, 1e-6)), 1)


def near_mic(transcript: "Transcript") -> list[bool]:
    """Per segment: much louder than the transcript's typical level (students by the laptop)."""
    levels = [s.loudness for s in transcript.segments if s.loudness is not None]
    if len(levels) < 3:
        return [False] * len(transcript.segments)
    median = float(np.median(levels))
    return [s.loudness is not None and s.loudness - median >= NEAR_MIC_DB for s in transcript.segments]


def prompt_tag(seg: "Segment", near: bool = False) -> str:
    """``"[Lecturer] "``, ``"[Speaker 2, near mic] "`` or ``""`` for classifier prompts and exports."""
    parts = [speaker_name(seg.speaker)] if seg.speaker else []
    if near:
        parts.append("near mic")
    return f"[{', '.join(parts)}] " if parts else ""


# --- embeddings --------------------------------------------------------------


def find_model(settings: Settings) -> Path | None:
    if settings.speaker_model:
        path = Path(settings.speaker_model).expanduser()
        path = path if path.is_absolute() else models_dir() / path
        return path if path.exists() else None

    def rank(path: Path) -> int:
        return next((i for i, p in enumerate(MODEL_PREFERENCE) if path.name.startswith(p)), len(MODEL_PREFERENCE))

    models = sorted((p for p in models_dir().glob("*.onnx") if rank(p) < len(MODEL_PREFERENCE)), key=rank)
    return models[0] if models else None


def load_embedder(settings: Settings) -> Callable[[np.ndarray], np.ndarray] | None:
    """A function samples → embedding, or None if sherpa-onnx or a model is missing."""
    model = find_model(settings)
    if model is None:
        return None
    try:
        import sherpa_onnx
    except ImportError:
        return None
    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=2)
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)

    def embed(samples: np.ndarray) -> np.ndarray:
        stream = extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        stream.input_finished()
        if not extractor.is_ready(stream):
            raise ValueError("segment too short")
        return np.asarray(extractor.compute(stream), dtype=np.float32)

    return embed


# --- clustering --------------------------------------------------------------


def cluster(embeddings: np.ndarray, threshold: float) -> list[int]:
    """Average-linkage agglomerative clustering on cosine similarity: keep merging
    the two most similar groups while their mean similarity is ≥ ``threshold``.
    Returns a cluster number per row."""
    n = len(embeddings)
    if n == 0:
        return []
    unit = embeddings / np.maximum(np.linalg.norm(embeddings, axis=1, keepdims=True), 1e-9)
    sim = (unit @ unit.T).astype(np.float64)
    np.fill_diagonal(sim, -np.inf)
    sizes = np.ones(n)
    owner = np.arange(n)  # row → representative row of its group
    while True:
        flat = int(np.argmax(sim))
        i, j = divmod(flat, n)
        if sim[i, j] < threshold:
            break
        merged = (sim[i] * sizes[i] + sim[j] * sizes[j]) / (sizes[i] + sizes[j])
        sim[i], sim[:, i] = merged, merged
        sim[i, i] = -np.inf
        sim[j], sim[:, j] = -np.inf, -np.inf
        sizes[i] += sizes[j]
        owner[owner == j] = i
    numbers = {rep: k for k, rep in enumerate(dict.fromkeys(owner.tolist()))}
    return [numbers[rep] for rep in owner.tolist()]


def _name_clusters(segments: list["Segment"], groups: dict[int, int]) -> dict[int, str]:
    """Cluster number → "lecturer" for the most speaking time, then "speaker_2", … ."""
    talk: dict[int, float] = {}
    for index, group in groups.items():
        talk[group] = talk.get(group, 0.0) + segments[index].end - segments[index].start
    ranked = sorted(talk, key=lambda g: -talk[g])
    return {g: LECTURER if k == 0 else f"speaker_{k + 1}" for k, g in enumerate(ranked)}


def annotate(
    segments: list["Segment"],
    wav: Path,
    settings: Settings,
    embedder: Callable[[np.ndarray], np.ndarray] | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> bool:
    """Set ``loudness`` on every segment and ``speaker`` when an embedding model is
    available. ``embedder`` defaults to :func:`load_embedder`. Returns True if
    speakers were assigned."""
    samples = read_wav(wav)
    for seg in segments:
        seg.loudness = loudness_db(_slice(samples, seg))
    if not segments:
        return False
    embed = embedder or load_embedder(settings)
    if embed is None:
        return False

    vectors, embedded = [], []
    for index, seg in enumerate(segments):
        if seg.end - seg.start < MIN_EMBED_SECONDS:
            continue
        if checkpoint:
            checkpoint()
        chunk = _slice(samples, seg)[: int(MAX_EMBED_SECONDS * SAMPLE_RATE)]
        try:
            vectors.append(embed(chunk))
        except ValueError:
            continue
        embedded.append(index)
    if not vectors:
        return False

    groups = dict(zip(embedded, cluster(np.stack(vectors), settings.speaker_threshold)))
    names = _name_clusters(segments, groups)
    def gap(a: "Segment", b: "Segment") -> float:
        return max(0.0, a.start - b.end, b.start - a.end)

    for index, seg in enumerate(segments):
        # Short segment: the closest embedded one in time; on a tie the earlier (often the same sentence).
        nearest = index if index in groups else min(embedded, key=lambda e: gap(segments[e], seg))
        seg.speaker = names[groups[nearest]]
    return True


# --- manual corrections ------------------------------------------------------


def make_lecturer(transcript: "Transcript", speaker: str) -> int:
    """The user says ``speaker`` is the lecturer: swap that voice's label with the
    current lecturer's on every segment. Returns segments changed. For single lines
    set ``segment.speaker`` directly (``LECTURER`` / ``OTHER``)."""
    if not speaker or speaker in (LECTURER, OTHER):
        return 0
    changed = 0
    for seg in transcript.segments:
        if seg.speaker == speaker:
            seg.speaker = LECTURER
            changed += 1
        elif seg.speaker == LECTURER:
            seg.speaker = speaker
            changed += 1
    return changed
