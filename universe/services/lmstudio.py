"""LM Studio model instances: list what is loaded, unload what is idle.

LM Studio only auto-unloads models it loaded on demand ("JIT") — a model
loaded by hand stays in memory forever. UniVerse therefore asks for a TTL on
its own requests and, after a quiet period, unloads the instance it used.

Uses LM Studio's REST API (``/api/v1/models``, ``/api/v1/models/unload``) on
the same server as the OpenAI-compatible endpoint. Other servers simply
report "not LM Studio".
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

GB = 1e9


@dataclass
class Instance:
    instance_id: str
    model: str  # model key, e.g. "qwen3-14b-mlx"
    name: str  # display name, e.g. "Qwen3 14B"
    kind: str  # "llm" | "embedding" | …
    size_gb: float
    context: int | None
    ttl_left: int | None = None  # seconds until LM Studio unloads it by itself; None = stays loaded


def server_root(base_url: str) -> str:
    """"http://localhost:8080/v1" → "http://localhost:8080"."""
    url = base_url.rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


def loaded_instances(root: str, timeout: float = 1.5) -> list[Instance] | None:
    """Loaded model instances, or ``None`` if the server is not LM Studio / not reachable."""
    try:
        with urllib.request.urlopen(f"{root}/api/v1/models", timeout=timeout) as response:
            models = json.load(response).get("models", [])
    except (OSError, ValueError):
        return None
    instances = []
    for model in models:
        for inst in model.get("loaded_instances") or []:
            instances.append(
                Instance(
                    instance_id=inst.get("id", model.get("key", "")),
                    model=model.get("key", ""),
                    name=model.get("display_name") or model.get("key", ""),
                    kind=model.get("type", ""),
                    size_gb=(model.get("size_bytes") or 0) / GB,
                    context=(inst.get("config") or {}).get("context_length"),
                    ttl_left=inst.get("remaining_ttl_seconds"),
                )
            )
    return instances


def unload(root: str, instance_id: str, timeout: float = 30) -> None:
    body = json.dumps({"instance_id": instance_id}).encode()
    request = urllib.request.Request(f"{root}/api/v1/models/unload", body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            pass
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        if "not loaded" in detail or "model_not_found" in detail:
            return  # already gone — that is what we wanted
        raise RuntimeError(f"LM Studio refused to unload {instance_id}: {detail[:200]}") from exc
