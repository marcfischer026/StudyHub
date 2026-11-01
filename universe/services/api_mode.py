"""Optional API mode: the Gemini and Claude steps of the round trip through one paid API.

Off by default. The everyday path stays copy & paste, and nothing outside this module, one
settings tab and one card in the Claude tab knows about it. Any OpenAI-compatible
``/chat/completions`` endpoint works. What comes back goes through the same checks as a
pasted reply, so a bad answer costs what a bad paste costs.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

import pymupdf

from ..config import Settings
from . import pdf_conversion
from .claude_reply import CheckedReply, check_reply, follow_up_prompt, parse_reply, repair_prompt
from .jobcontrol import Cancelled
from .study_store import MergeSummary, StudyStore
from .subject_model import SubjectView

# (name, base URL, default model). Only Meta's model is given: the others change too often to pin.
PRESETS = (
    ("Meta Model API", "https://api.meta.ai/v1", "muse-spark-1.3"),
    ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", ""),
    ("OpenAI", "https://api.openai.com/v1", ""),
    ("Anthropic", "https://api.anthropic.com/v1", ""),
)
KEY_VARIABLE = "UNIVERSE_API_KEY"  # wins over settings.json
PAGES_PER_CALL = 10
PAGE_DPI = 144
CONVERT_PASSES = 2  # a second try for the pages a reply dropped
MAX_TURNS = 12  # continuations of one long reply
RETRIES = 3
RETRY_STATUS = (429, 500, 502, 503, 504)
TIMEOUT = 600.0
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

REPLY_NOTE = """

You are answering through an API, not in a chat: there is no file to download and no code block. \
Write the JSON Lines records directly, nothing else. If you run out of room, stop after a complete \
record and write an end record with "more":true; you will be asked for the next part."""
CONTINUE = "Continue exactly where you stopped, with the next complete record. Do not repeat earlier records."
PAGES_NOTE = "The PDF is attached as images of its pages, in order: {stamps}.\n\n"

Progress = Callable[[str], None]


class ApiError(RuntimeError):
    """Shown to the student as it is, so it never holds the key."""


@dataclass
class Answer:
    text: str
    finish_reason: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would take the key, and with some codes the material, to another host."""

    def redirect_request(self, *_args, **_kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


@dataclass
class ApiClient:
    url: str
    key: str
    model: str
    stop: threading.Event = field(default_factory=threading.Event)

    def _request(self, path: str, payload: dict | None = None, timeout: float = TIMEOUT) -> dict:
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "User-Agent": "UniVerse"}
        request = urllib.request.Request(f"{self.url}/{path}", data=data, headers=headers)
        for attempt in range(RETRIES + 1):
            if self.stop.is_set():
                raise Cancelled()
            try:
                with _opener.open(request, timeout=timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                with exc:
                    detail = exc.read().decode("utf-8", "replace")[:300].strip().replace(self.key, "***")
                if exc.code not in RETRY_STATUS or attempt == RETRIES:
                    raise ApiError(f"The API answered {exc.code}: {detail}") from None
            except (OSError, ValueError) as exc:
                if attempt == RETRIES:
                    raise ApiError(f"Could not reach the API: {exc}") from None
            if self.stop.wait(2**attempt):
                raise Cancelled()
        raise AssertionError("unreachable")

    def chat(self, messages: list[dict]) -> Answer:
        """No token limit is set: the name of that field differs between providers, and a reply
        that hits the provider's own limit is continued."""
        reply = self._request("chat/completions", {"model": self.model, "messages": messages})
        try:
            choice = reply["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ApiError("The API's answer has no message in it.") from None
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return Answer(content or "", choice.get("finish_reason") or "")

    def models(self) -> list[str]:
        reply = self._request("models", timeout=20.0)
        return sorted(str(item["id"]) for item in reply.get("data", ()) if isinstance(item, dict) and "id" in item)


def client_from(settings: Settings) -> ApiClient:
    url = settings.api_url.strip().rstrip("/")
    key = os.environ.get(KEY_VARIABLE) or settings.api_key.strip()
    model = settings.api_model.strip()
    parsed = urlparse(url)
    if not (parsed.hostname and (parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS))):
        raise ApiError("The API URL must start with https:// (http:// only for this Mac).")
    if not key:
        raise ApiError("No API key: enter one under Settings → API.")
    if not model:
        raise ApiError("No model: choose one under Settings → API.")
    return ApiClient(url, key, model)


def host(settings: Settings) -> str:
    return urlparse(settings.api_url.strip()).hostname or "the API"


# --- PDFs to Markdown --------------------------------------------------------


def _page_images(document, first: int, last: int) -> list[str]:
    """Stamped like the copies for the chat tools, as data URLs."""
    images = []
    with pymupdf.open(document.path) as source:
        copy = pymupdf.open()
        copy.insert_pdf(source, from_page=first - 1, to_page=last - 1, annots=False)
        for offset, page in enumerate(copy):
            pdf_conversion._stamp_page(page, pdf_conversion.stamp(document.ref, first + offset))
            encoded = base64.b64encode(page.get_pixmap(dpi=PAGE_DPI).tobytes("jpeg")).decode()
            images.append(f"data:image/jpeg;base64,{encoded}")
        copy.close()
    return images


def _convert_run(client: ApiClient, document, first: int, last: int) -> str:
    stamps = ", ".join(pdf_conversion.stamp(document.ref, n) for n in range(first, last + 1))
    content: list[dict] = [{"type": "text", "text": PAGES_NOTE.format(stamps=stamps) + pdf_conversion.PROMPT}]
    content += [{"type": "image_url", "image_url": {"url": url}} for url in _page_images(document, first, last)]
    return client.chat([{"role": "user", "content": content}]).text


def _add_report(total: pdf_conversion.ConversionReport, report: pdf_conversion.ConversionReport) -> None:
    total.problems += [problem for problem in report.problems if problem not in total.problems]
    for new in report.documents:
        known = next((d for d in total.documents if d.ref == new.ref), None)
        if known is None:
            total.documents.append(new)
            continue
        redone = set(new.imported)
        known.imported = sorted(set(known.imported) | redone)
        known.weak = [item for item in known.weak if item[0] not in redone] + new.weak
        known.missing = new.missing


def convert_pdfs(view: SubjectView, client: ApiClient, progress: Progress = lambda _text: None) -> pdf_conversion.ConversionReport:
    """Every page still without Markdown, except from excluded documents. Saved call by call, so
    stopping or an error keeps what has come back."""
    total = pdf_conversion.ConversionReport()
    for document in view.documents:
        if document.excluded:
            continue
        for _ in range(CONVERT_PASSES):
            done = pdf_conversion.load_pages(view.subject, document.key)
            pages = [n for n in range(1, document.page_count + 1) if n not in done]
            for first, last in pdf_conversion._ranges(pages, PAGES_PER_CALL):
                progress(f"Converting {document.ref}, pages {first}-{last} of {document.page_count}…")
                _add_report(total, pdf_conversion.import_converted(view, _convert_run(client, document, first, last)))
    return total


# --- the reply ---------------------------------------------------------------


@dataclass
class Exchange:
    steps: list[tuple[CheckedReply, MergeSummary]] = field(default_factory=list)
    problem: str = ""  # a later round failed; what earlier rounds brought is saved

    @property
    def checked(self) -> CheckedReply | None:
        return self.steps[-1][0] if self.steps else None


def _read_reply(client: ApiClient, messages: list[dict]) -> str:
    """One reply, over as many turns as it takes. ``messages`` grows, for the rounds after it."""
    texts: list[str] = []
    for _ in range(MAX_TURNS):
        answer = client.chat(messages)
        messages.append({"role": "assistant", "content": answer.text})
        texts.append(answer.text)
        parsed = parse_reply("\n".join(texts))
        if parsed.finished and not parsed.truncated:
            break
        if not parsed.records and answer.finish_reason != "length":
            break  # not records and not cut off: more turns will not change that
        messages.append({"role": "user", "content": CONTINUE})
    return "\n".join(texts)


def _merge(text: str, load: Callable[[], SubjectView], store: StudyStore) -> tuple[CheckedReply, MergeSummary] | None:
    parsed = parse_reply(text)
    if not parsed.records:
        return None
    view = load()
    checked = check_reply(parsed, view, store.known_ids())
    return checked, store.merge(checked, text, view)


def exchange(client: ApiClient, prompt: str, load: Callable[[], SubjectView], store: StudyStore, progress: Progress = lambda _text: None) -> Exchange:
    """Sends the prompt, saves the reply, then asks once each for fixes and for pages nothing
    accounted for, as the student would with the two buttons in step 3."""
    messages = [{"role": "user", "content": prompt + REPLY_NOTE}]
    progress(f"Waiting for {client.model}…")
    text = _read_reply(client, messages)
    first = _merge(text, load, store)
    if first is None:
        raise ApiError(f"The API's answer holds no records. It starts: {text[:200]!r}")
    result = Exchange([first])
    for what, build, needed in (
        ("Asking for fixes…", repair_prompt, lambda c: bool(c.errors)),
        ("Asking about the missing pages…", follow_up_prompt, lambda c: bool(c.uncovered)),
    ):
        checked = result.checked
        request = build(checked) if needed(checked) else ""
        if not request:
            continue
        progress(what)
        messages.append({"role": "user", "content": request})
        try:
            step = _merge(_read_reply(client, messages), load, store)
        except ApiError as exc:
            result.problem = str(exc)
            break
        if step is not None:
            result.steps.append(step)
    return result
