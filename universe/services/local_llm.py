"""Sort transcript segments with a local LLM (LM Studio, Ollama, mlx_lm.server, …).

Talks to any OpenAI-compatible ``/v1/chat/completions`` endpoint on this Mac, so
nothing leaves the machine. Optional: if no server is running, classification is
simply skipped and the Claude round trip can do it instead.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Callable

from ..config import Settings
from . import lmstudio, speakers
from .jobcontrol import JobControl
from .relevance import CATEGORIES, expand_ids, segment_id

CODES = {"L": "lecture", "Q": "question", "A": "answer", "O": "off_topic"}
_LINE = re.compile(r"^[ \t]*[-*]?[ \t]*(S\d+\.\d+)[ \t]*[:=-]?[ \t]*([LQAO])(\*)?[ \t]*(.*)$", re.MULTILINE)
from .transcriber import Transcript

# LM Studio (1234), llama.cpp / mlx_lm servers (8080), Ollama (11434).
DEFAULT_URLS = ("http://localhost:1234/v1", "http://localhost:8080/v1", "http://localhost:11434/v1")
BATCH = 40
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)

PROMPT = """You sort the transcript of a university lecture recording for the course "{subject}".
The laptop microphone also caught students chatting next to it. Classify EVERY segment:

- lecture: the lecturer teaching content of this course
- question: a question ABOUT THE COURSE CONTENT (to the lecturer or a classmate)
- answer: the reply to such a content question
- off_topic: everything else — private chatter, small talk, questions and answers about other
  things (tech, plans, politics, the recording itself, …), greetings, noise

Course context (from the slides): {context}

A tag before a segment comes from voice recognition: [Lecturer] is the voice that talks most in
the recording; [Speaker 2], [Speaker 3], … are other voices, usually students. "near mic" means
much louder than usual — someone sitting next to the laptop, almost never the lecturer. Use the
tags: lecture content comes from the lecturer; another voice is a question, an answer or off_topic,
and near-mic chatter is off_topic unless it is clearly about the course. The tags are automatic and
can be wrong — when the wording plainly says otherwise, trust the wording.

Set "exam": true when the speaker says it is relevant for the exam, will be tested, must be
remembered, or stresses it is especially important.

Reply with JSON only, one entry per segment, reason max 5 words:
{{"labels": [{{"id": "S1.0", "category": "off_topic", "exam": false, "reason": "talks about weekend plans"}}]}}

Segments:
{segments}
/no_think"""


class LocalLLMUnavailable(RuntimeError):
    pass


def _get(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def find_server(settings: Settings) -> tuple[str, str]:
    """(base url, model id) of a running local server, or raise LocalLLMUnavailable."""
    urls = (settings.local_llm_url.rstrip("/"),) if settings.local_llm_url else DEFAULT_URLS
    for url in urls:
        try:
            models = [m["id"] for m in _get(f"{url}/models", timeout=2).get("data", [])]
        except (OSError, ValueError, KeyError):
            continue
        chat_models = [m for m in models if "embed" not in m.lower()]
        lmstudio_llms = lmstudio.chat_models(lmstudio.server_root(url))
        if lmstudio_llms is not None:  # LM Studio knows which models can chat
            chat_models = [m for m in lmstudio_llms if m in models]
        if settings.local_llm_model:
            if settings.local_llm_model in models:
                return url, settings.local_llm_model
            continue
        if chat_models:
            return url, chat_models[0]
    raise LocalLLMUnavailable(
        "No local AI server found. Start LM Studio's local server (or Ollama), or set local_llm_url in settings.json."
    )


def chat(url: str, model: str, prompt: str, timeout: float = 600, ttl: int = 0) -> str:
    """``ttl``: seconds LM Studio may keep a model it loads on demand (JIT) for this request."""
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 3000}
    if ttl:
        payload["ttl"] = ttl  # LM Studio: unload this JIT-loaded model after ttl idle seconds
    body = json.dumps(payload).encode()
    request = urllib.request.Request(f"{url}/chat/completions", body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = json.load(response)["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as exc:  # e.g. model not loaded in LM Studio
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise LocalLLMUnavailable(f"Local AI ({model}) refused the request: {exc.code} {detail}") from exc
    except (OSError, ValueError, KeyError, IndexError) as exc:
        raise LocalLLMUnavailable(f"Local AI request failed: {exc}") from exc
    return _THINK.sub("", content or "")


def _parse(text: str) -> list[dict]:
    """Read "S1.4 O" / "S1.9 L* reason" lines; fall back to a JSON ``labels`` list."""
    labels = [
        {"id": sid, "category": CODES[code], "exam": bool(star), "reason": reason.strip()[:80] if star else ""}
        for sid, code, star, reason in _LINE.findall(text)
    ]
    if labels:
        return labels
    from .claude_bridge import ResponseError, _extract_json

    try:
        data = json.loads(_extract_json(text))
    except (ResponseError, json.JSONDecodeError):
        return []
    found = data.get("labels", []) if isinstance(data, dict) else []
    return [l for l in found if isinstance(l, dict) and l.get("category", "lecture") in CATEGORIES]


def classify(
    transcript: Transcript,
    transcript_no: int,
    subject: str,
    context: str,
    settings: Settings,
    progress: Callable[[int], None] | None = None,
    control: JobControl | None = None,
) -> list[dict]:
    """Labels for one transcript, ids as in :func:`relevance.segment_id`. Every
    segment the model does not mention is returned as plain lecture content."""
    url, model = find_server(settings)
    labels: dict[str, dict] = {}
    segments = list(enumerate(transcript.segments))
    near = speakers.near_mic(transcript)
    batches = [segments[i : i + BATCH] for i in range(0, len(segments), BATCH)] or [[]]
    for n, batch in enumerate(batches, start=1):
        if not batch:
            break
        if control:
            control.checkpoint()  # pause / cancel between batches
        lines = "\n".join(f"{segment_id(transcript_no, i)} {speakers.prompt_tag(seg, near[i])}{seg.text}" for i, seg in batch)
        prompt = PROMPT.format(subject=subject, context=context or "none", segments=lines)
        reply = chat(url, model, prompt, ttl=settings.local_llm_unload_after)
        ids_in_batch = {segment_id(transcript_no, i) for i, _ in batch}
        for label in _parse(reply):
            for sid in expand_ids(label):
                if sid in ids_in_batch:
                    labels[sid] = {**label, "id": sid, "from": None, "to": None}
        for sid in ids_in_batch:
            labels.setdefault(sid, {"id": sid, "category": "lecture", "exam": None, "reason": ""})
        if progress:
            progress(int(n * 100 / len(batches)))
    return list(labels.values())
