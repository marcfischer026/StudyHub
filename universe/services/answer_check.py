"""A second opinion on an answer the machine marked wrong, from the local LLM.

``practice.grade_answer`` compares strings and numbers, so "1,2 Mio." against 1200000 or a
key that is itself wrong both come out as 0. The local model reads the question, the
expected answer and the student's answer and says whether it is right after all. It only
advises: the student decides whether to count it, and nothing leaves the machine.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..config import Settings
from . import local_llm

VERDICTS = ("correct", "partly", "wrong")
_VERDICT = re.compile(r"\b(correct|partly|wrong)\b", re.IGNORECASE)

PROMPT = """You check a student's answer in a university practice quiz. An automatic check
compared it with the expected answer and marked it wrong. Decide whether the student's answer
is in fact correct: the same value written differently (units, rounding, 1.2 Mio. = 1,200,000,
German decimal commas), the same meaning in other words, or the expected answer itself is wrong.

Be strict: an answer that is merely close, vague or a different option is wrong.

Question type: {kind}
Question: {stem}
{options}Expected answer: {expected}
{explain}Student's answer: {answer}

Reply with JSON only, reason in one or two short sentences, in the language of the question:
{{"verdict": "correct" | "partly" | "wrong", "reason": "..."}}"""


@dataclass
class Recheck:
    verdict: str  # one of VERDICTS
    reason: str


def _expected(question: dict) -> str:
    if question.get("type") == "mc":
        return next((str(o.get("text", "")) for o in question.get("options") or [] if o.get("correct")), "")
    numeric = question.get("numeric") if isinstance(question.get("numeric"), dict) else None
    if numeric and numeric.get("value") is not None:
        tolerance = f" (tolerance {numeric['tolerance']})" if numeric.get("tolerance") is not None else ""
        return f"{numeric['value']} {numeric.get('unit') or ''}".strip() + tolerance
    return str(question.get("answer") or "")


def prompt_for(question: dict, answer: str) -> str:
    options = ""
    if question.get("type") == "mc":
        lines = "\n".join(f"- {o.get('text', '')}" for o in question.get("options") or [])
        options = f"Options:\n{lines}\n"
    why = " ".join(str(part) for part in (question.get("explain"), *(question.get("steps") or [])) if part)
    return PROMPT.format(
        kind=question.get("type", ""),
        stem=question.get("stem", ""),
        options=options,
        expected=_expected(question),
        explain=f"Explanation: {why}\n" if why else "",
        answer=answer,
    )


def parse(text: str) -> Recheck:
    """The JSON reply, or failing that the first verdict word in it; unreadable counts as wrong."""
    decoder = json.JSONDecoder()
    for start in re.finditer(r"\{", text):
        try:
            data = decoder.raw_decode(text, start.start())[0]
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and str(data.get("verdict", "")).lower() in VERDICTS:
            return Recheck(str(data["verdict"]).lower(), str(data.get("reason") or "").strip())
    found = _VERDICT.search(text)
    return Recheck(found[1].lower() if found else "wrong", "")


def recheck(question: dict, answer: str, settings: Settings) -> Recheck:
    """Raises ``local_llm.LocalLLMUnavailable`` when no local model runs."""
    url, model = local_llm.find_server(settings)
    reply = local_llm.chat(url, model, prompt_for(question, answer), timeout=120, ttl=settings.local_llm_unload_after)
    return parse(reply)
