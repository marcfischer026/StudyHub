"""Per-subject semantic search over transcripts, slides and notes (LanceDB).

Each subject has its own LanceDB database in ``<Subject>/.index``. Re-indexing
a lecture replaces all of that lecture's rows.

Embeddings are computed locally. If ``sentence-transformers`` and its model are
available they are used; otherwise a deterministic hashing embedder keeps
search working with zero downloads. The table name includes the embedder id,
so switching embedders never mixes incompatible vectors.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import lancedb
import numpy as np
import pyarrow as pa

from ..storage import Lecture, Subject
from . import pdf_tools
from . import slide_links
from .timestamps import make_stamp
from .transcriber import Transcript

CHUNK_CHARS = 800


class Embedder(Protocol):
    id: str
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray: ...


class HashingEmbedder:
    """Bag of word uni/bigrams hashed into a fixed vector. Fully offline."""

    id = "hash384"
    dim = 384
    _token = re.compile(r"\w+", re.UNICODE)

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            words = self._token.findall(text.lower())
            for feature in words + [f"{a} {b}" for a, b in zip(words, words[1:])]:
                digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
                bucket = int.from_bytes(digest[:4], "little") % self.dim
                sign = 1.0 if digest[4] & 1 else -1.0
                out[row, bucket] += sign
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(norms == 0, 1, norms)


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self.dim = int(self._model.get_sentence_embedding_dimension())
        self.id = "st_" + re.sub(r"\W+", "_", model_name).lower()

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self._model.encode(texts, normalize_embeddings=True), dtype=np.float32)


_embedder: Embedder | None = None


def get_embedder(model_name: str = "all-MiniLM-L6-v2") -> Embedder:
    global _embedder
    if _embedder is None:
        try:
            _embedder = SentenceTransformerEmbedder(model_name)
        except Exception:  # not installed, or model not cached and we are offline
            _embedder = HashingEmbedder()
    return _embedder


@dataclass
class Chunk:
    lecture: str
    source: str  # "transcript" | "slides" | "notes"
    ref: str  # "[▶ 00:12:30]", "Week1.pdf p.4", "notes"
    text: str


@dataclass
class Hit(Chunk):
    score: float


def _split(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, current = [], ""
    for para in paragraphs:
        while len(para) > limit:
            chunks.append(para[:limit])
            para = para[limit:]
        if len(current) + len(para) + 2 > limit and current:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks


def lecture_chunks(lecture: Lecture) -> list[Chunk]:
    chunks: list[Chunk] = []

    for path in lecture.transcripts():
        transcript = Transcript.load(path)
        window, start = [], None
        for seg in transcript.segments:
            if seg.private:  # side talk never becomes searchable
                continue
            start = seg.start if start is None else start
            window.append(seg.text)
            if sum(len(t) for t in window) >= CHUNK_CHARS:
                chunks.append(Chunk(lecture.name, "transcript", f"{path.stem} {make_stamp(start)}", " ".join(window)))
                window, start = [], None
        if window:
            chunks.append(Chunk(lecture.name, "transcript", f"{path.stem} {make_stamp(start or 0)}", " ".join(window)))

    for pdf in lecture.pdfs():
        for number, text in enumerate(pdf_tools.page_texts(pdf), start=1):
            for piece in _split(text):
                chunks.append(Chunk(lecture.name, "slides", f"{pdf.name} p.{number}", piece))

    for piece in _split(slide_links.for_export(lecture.read_notes())):
        chunks.append(Chunk(lecture.name, "notes", "notes", piece))

    return chunks


class SubjectIndex:
    def __init__(self, subject: Subject, embedder: Embedder | None = None):
        self.subject = subject
        self.embedder = embedder or get_embedder()
        self.table_name = f"chunks_{self.embedder.id}"
        Path(subject.index_dir).mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(subject.index_dir))

    def _schema(self) -> pa.Schema:
        return pa.schema(
            [
                pa.field("vector", pa.list_(pa.float32(), self.embedder.dim)),
                pa.field("lecture", pa.string()),
                pa.field("source", pa.string()),
                pa.field("ref", pa.string()),
                pa.field("text", pa.string()),
            ]
        )

    def _table(self):
        return self._db.create_table(self.table_name, schema=self._schema(), exist_ok=True)

    def index_lecture(self, lecture: Lecture) -> int:
        """Replace this lecture's rows. Returns the number of chunks indexed."""
        table = self._table()
        table.delete(f"lecture = '{_sql_quote(lecture.name)}'")
        chunks = lecture_chunks(lecture)
        if not chunks:
            return 0
        vectors = self.embedder.embed([c.text for c in chunks])
        table.add(
            [
                {"vector": vec.tolist(), "lecture": c.lecture, "source": c.source, "ref": c.ref, "text": c.text}
                for c, vec in zip(chunks, vectors)
            ]
        )
        return len(chunks)

    def index_all(self) -> int:
        return sum(self.index_lecture(lecture) for lecture in self.subject.lectures())

    def remove_lecture(self, lecture_name: str) -> None:
        self._table().delete(f"lecture = '{_sql_quote(lecture_name)}'")

    def search(self, query: str, limit: int = 8, lecture: str | None = None) -> list[Hit]:
        table = self._table()
        if table.count_rows() == 0 or not query.strip():
            return []
        vector = self.embedder.embed([query])[0]
        search = table.search(vector.tolist()).limit(limit)
        if lecture:
            search = search.where(f"lecture = '{_sql_quote(lecture)}'", prefilter=True)
        return [
            Hit(row["lecture"], row["source"], row["ref"], row["text"], score=1.0 - float(row["_distance"]) / 2)
            for row in search.to_list()
        ]


def _sql_quote(value: str) -> str:
    return value.replace("'", "''")
