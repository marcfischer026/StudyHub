"""Tutor chat: talk with the local LLM about the subject's own material.

Three ways to learn: *explain* answers questions, *quiz* explains a little and then asks
one question at a time, *zero* starts from the very basics for someone who knows nothing yet.
Every turn searches the subject's index first and hands the model the best passages, numbered,
so its answers stay with the lecture and cite [1], [2], … which the tab turns into links.
Nothing leaves the machine. Each conversation is kept in ``tutor_chats.jsonl`` next to the
study data, so what the student asked and got wrong is there for a later Claude round.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..config import Settings
from ..storage import Subject
from . import local_llm

if TYPE_CHECKING:  # lancedb is heavy, and the export only renders the log
    from .vector_index import Hit

MODES = {
    "explain": "Explain",
    "quiz": "Quiz me as we go",
    "zero": "Start from zero",
}
SOURCES = 5  # passages per turn: the local context window is small
SOURCE_CHARS = 700
HISTORY_CHARS = 6000  # the latest turns that still fit next to the passages
MAX_TOKENS = 900

BASE = """You are a patient, encouraging tutor for the university course "{subject}".{topic_sentence}
Answer in the language the student writes in. Keep each reply short: a few sentences or a short list, \
then stop. Use the course material below and cite it as [1], [2], … right after what it supports. \
When the material does not cover something, say so and explain from general knowledge, marked as such. \
Never invent what the lecturer said."""

STYLE = {
    "explain": """The student asks questions. Explain clearly with one concrete example, then ask whether \
it is clear or which part to go deeper on.""",
    "quiz": """Teach in small steps and check understanding as you go: explain one idea briefly, then ask \
ONE question about it and wait. When the student answers, say plainly whether it is right, correct \
what is wrong in one or two sentences, and continue with the next small idea and question. \
Mix question types (why, example, calculation, true or false). Do not give the answer in the question.""",
    "zero": """The student knows nothing about this yet. Start from the very basics: first ask in one \
sentence what they already know, if anything. Then build up step by step: plain words before \
technical terms, define every term the first time, use everyday analogies, one idea per reply. \
After each idea ask a very easy check question. Only move on once they got it; if not, explain \
again differently. Now and then show where they are on the path (e.g. "Step 2 of 5: …").""",
}

MATERIAL = """Course material (search results for this turn; may be incomplete):
{passages}"""

OPENERS = {
    "explain": "Explain {topic} to me.",
    "quiz": "Teach me {topic} and quiz me as we go.",
    "zero": "I know nothing about {topic} yet. Start from zero with me.",
}
WHOLE_COURSE = "this course"


@dataclass
class Turn:
    role: str  # "user" or "assistant"
    text: str
    sources: list[Hit] = field(default_factory=list)  # the passages the tutor saw, numbered from 1

    def cited(self) -> list[int]:
        """The passage numbers the reply points at; all of them when it cites none."""
        numbers = {int(n) for n in re.findall(r"\[(\d+)\]", self.text)}
        used = [n for n in range(1, len(self.sources) + 1) if n in numbers]
        return used or list(range(1, len(self.sources) + 1))


def topic_titles(topics: dict) -> list[str]:
    """Topic titles from the study data, most important first."""
    ranked = sorted(topics.values(), key=lambda t: (-int(t.get("importance", 2) or 2), str(t.get("title", ""))))
    return [str(t["title"]) for t in ranked if t.get("title")]


def opener(mode: str, topic: str) -> str:
    return OPENERS[mode].format(topic=topic or WHOLE_COURSE)


def system_prompt(mode: str, subject: str, topic: str, hits: list[Hit]) -> str:
    topic_sentence = f' Today\'s topic: "{topic}".' if topic else ""
    parts = [BASE.format(subject=subject, topic_sentence=topic_sentence), STYLE[mode]]
    if hits:
        passages = "\n\n".join(f"[{n}] ({_label(hit)})\n{_clip(hit.text)}" for n, hit in enumerate(hits, start=1))
        parts.append(MATERIAL.format(passages=passages))
    else:
        parts.append("No course material was found for this turn: cite nothing, and say that you explain from general knowledge.")
    return "\n\n".join(parts)


def messages_for(mode: str, subject: str, topic: str, history: list[Turn], hits: list[Hit]) -> list[dict]:
    """The system prompt with this turn's passages, then as many recent turns as fit."""
    kept: list[dict] = []
    room = HISTORY_CHARS
    for turn in reversed(history):
        room -= len(turn.text)
        if kept and room < 0:
            break
        kept.append({"role": turn.role, "content": turn.text})
    kept.reverse()
    while kept and kept[0]["role"] != "user":  # a conversation starts with the student
        kept.pop(0)
    return [{"role": "system", "content": system_prompt(mode, subject, topic, hits)}, *kept]


def search_query(topic: str, history: list[Turn]) -> str:
    """What to look up: the topic and the latest exchange, so "why?" still finds the right passages."""
    recent = [t.text for t in history[-2:]]
    return " ".join(part for part in (topic, *recent) if part)[-1000:]


def reply(mode: str, subject: str, topic: str, history: list[Turn], settings: Settings, search: Callable[[str], list[Hit]] | None = None) -> Turn:
    """The tutor's next turn. Raises ``local_llm.LocalLLMUnavailable`` when no local model runs."""
    hits: list[Hit] = []
    if search is not None:
        try:
            hits = search(search_query(topic, history))[:SOURCES]
        except Exception:  # no index yet: the tutor still talks, just without citations
            hits = []
    url, model = local_llm.find_server(settings)
    messages = messages_for(mode, subject, topic, history, hits)
    text = local_llm.complete(url, model, messages, timeout=300, ttl=settings.local_llm_unload_after, temperature=0.4, max_tokens=MAX_TOKENS)
    text = _drop_unknown_citations(text, len(hits)) or "(The local AI sent an empty reply. Try asking again.)"
    return Turn("assistant", text, hits)


def log_path(subject: Subject) -> Path:
    return subject.study_dir / "tutor_chats.jsonl"


def log_turn(subject: Subject, chat_id: str, mode: str, topic: str, turn: Turn) -> None:
    """One line per turn; the file only grows."""
    record = {
        "chat": chat_id,
        "at": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "topic": topic,
        "role": turn.role,
        "text": turn.text,
        "sources": [f"[{n}] {_label(turn.sources[n - 1])}" for n in turn.cited()],
    }
    path = log_path(subject)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_log(subject: Subject) -> list[dict]:
    """Every logged turn, oldest first; a broken line is skipped."""
    try:
        lines = log_path(subject).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("role") in ("user", "assistant") and record.get("text"):
            records.append(record)
    return records


EXPORT_HEAD = """# My chats with the local AI tutor

A small AI on my laptop tutored me with my course material. Its answers are NOT evidence and may be \
wrong: never cite them. What counts is ME: my questions show what I found unclear, and my answers to its \
quiz questions show what I understand and what I get wrong. Newest chats first; a long tutor reply is shortened.
"""
MODE_NAMES = {"explain": "I asked questions", "quiz": "it quizzed me as we went", "zero": "it started from zero with me"}
REPLY_CHARS = 400


def render_chats(subject: Subject, limit: int) -> str:
    """The newest chats that fit into ``limit`` characters, as Markdown; "" without any."""
    chats: dict[str, list[dict]] = {}
    for record in read_log(subject):
        chats.setdefault(str(record.get("chat", "")), []).append(record)
    blocks: list[str] = []
    used = len(EXPORT_HEAD)
    for turns in reversed(list(chats.values())):
        if not any(t["role"] == "assistant" for t in turns):
            continue  # a chat that never got an answer says nothing
        first = turns[0]
        title = f"## {str(first.get('at', ''))[:16].replace('T', ' ')}, {first.get('topic') or 'the whole course'}: {MODE_NAMES.get(first.get('mode'), 'chat')}"
        lines = [title, ""]
        for turn in turns:
            text = " ".join(str(turn["text"]).split())
            if turn["role"] == "user":
                lines.append(f"ME: {text}")
            else:
                lines.append(f"TUTOR: {text if len(text) <= REPLY_CHARS else text[: REPLY_CHARS - 1] + '…'}")
        block = "\n".join(lines) + "\n\n"
        if used + len(block) > limit:
            break
        blocks.append(block)
        used += len(block)
    if not blocks:
        return ""
    left = len(chats) - len(blocks)
    tail = f"({left} older chat{'s' * (left != 1)} left out to keep this short.)\n" if left > 0 else ""
    return EXPORT_HEAD + "\n" + "".join(blocks) + tail


def _drop_unknown_citations(text: str, count: int) -> str:
    """A small model cites [1] even when it saw no passage; such a number points nowhere."""
    text = re.sub(r"\s?\[(\d+)\]", lambda m: m.group(0) if 1 <= int(m.group(1)) <= count else "", text)
    return text.strip()


def _label(hit: Hit) -> str:
    return " · ".join(part for part in (hit.lecture, hit.source, hit.ref) if part)


def _clip(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= SOURCE_CHARS else text[: SOURCE_CHARS - 1] + "…"
