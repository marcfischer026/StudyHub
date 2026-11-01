"""Per-subject semantic search over transcripts, slides, notes, past exams and tasks (LanceDB).

Each subject has its own LanceDB database in ``<Subject>/.index``. Re-indexing
a lecture replaces all of that lecture's rows. PDFs are searched as their Gemini or
Copilot conversion where one exists, because that also reads scans and graphics.

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

from ..storage import Document, Lecture, Subject
from . import slide_links
from .subject_model import best_pages
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
    lecture: str  # "" for a document of the whole subject
    source: str  # "transcript" | "slides" | "notes" | "exams" | "exercises"
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


def _document_chunks(document: Document, subject: Subject, lecture: str = "") -> list[Chunk]:
    try:
        pages = best_pages(document, subject)
    except Exception:  # unreadable PDF: search the rest
        return []
    ref = document.path.name
    return [Chunk(lecture, document.folder, f"{ref} p.{number}", piece) for number, text in pages for piece in _split(text)]


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

    for document in lecture.documents():
        chunks += _document_chunks(document, lecture.subject, lecture.name)

    for piece in _split(slide_links.for_export(lecture.read_notes())):
        chunks.append(Chunk(lecture.name, "notes", "notes", piece))

    return chunks


def subject_chunks(subject: Subject) -> list[Chunk]:
    """Past exams and tasks: they belong to no lecture, and their source is their folder."""
    return [chunk for document in subject.documents() for chunk in _document_chunks(document, subject)]


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

    def _replace(self, lecture_name: str, chunks: list[Chunk]) -> int:
        table = self._table()
        table.delete(f"lecture = '{_sql_quote(lecture_name)}'")
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

    def index_lecture(self, lecture: Lecture) -> int:
        """Replace this lecture's rows. Returns the number of chunks indexed."""
        return self._replace(lecture.name, lecture_chunks(lecture))

    def index_documents(self) -> int:
        """Replace the rows of the subject's past exams and tasks. Returns the number of chunks indexed."""
        return self._replace("", subject_chunks(self.subject))

    def index_all(self) -> int:
        return sum(self.index_lecture(lecture) for lecture in self.subject.lectures()) + self.index_documents()

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
