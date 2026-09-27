"""How much context a private model really has, asked of its server.

OpenAI-compatible `/models` has no standard context field, and a local server
often runs a model with far less context than it could take (Ollama's
default depends on VRAM, as little as 4k), so each server is asked its own
way, preferring the size actually loaded over the model's maximum:

- nano-gpt / OpenRouter: `/models?detailed=true`, `context_length`.
- Ollama: `/api/ps` (loaded: `context_length`), else `/api/show`
  (`model_info["<arch>.context_length"]`, the maximum).
- LM Studio: `/api/v1/models` (`loaded_instances[].config.context_length`,
  else `max_context_length`).
- llama.cpp: `/props`, `default_generation_settings.n_ctx`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from sealedlore.providers.http import make_client

TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class ContextSize:
    tokens: int
    # Where it came from, for the author ("loaded in Ollama", "the model's maximum").
    source: str
    # True when this is what the model is running with, not what it could take.
    loaded: bool = True


def _root(base_url: str) -> str:
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and value > 0 else None


def detect_context(
    base_url: str, api_key: str, model: str, *, client: httpx.Client | None = None
) -> ContextSize | None:
    """The model's context, or None if no server answered with one."""
    http = client or make_client(base_url, timeout=httpx.Timeout(TIMEOUT_SECONDS))
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        for probe in (_listing, _ollama, _lm_studio, _llama_cpp):
            try:
                found = probe(http, base_url, headers, model)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                found = None
            if found is not None:
                return found
        return None
    finally:
        if client is None:
            http.close()


def _get(http: httpx.Client, url: str, headers: dict, **kwargs) -> Any:
    response = http.get(url, headers=headers, **kwargs)
    if response.status_code >= 400:
        return None
    return response.json()


def _listing(http, base_url, headers, model) -> ContextSize | None:
    body = _get(http, base_url.rstrip("/") + "/models", headers, params={"detailed": "true"})
    for item in (body or {}).get("data") or []:
        if isinstance(item, dict) and item.get("id") == model:
            tokens = _int(item.get("context_length")) or _int(
                (item.get("top_provider") or {}).get("context_length")
            )
            if tokens:
                return ContextSize(tokens, "the endpoint's model list")
    return None


def _ollama(http, base_url, headers, model) -> ContextSize | None:
    root = _root(base_url)
    running = _get(http, root + "/api/ps", headers)
    for item in (running or {}).get("models") or []:
        if isinstance(item, dict) and item.get("name", "").split(":")[0] == model.split(":")[0]:
            tokens = _int(item.get("context_length"))
            if tokens:
                return ContextSize(tokens, "loaded in Ollama")
    response = http.post(root + "/api/show", json={"model": model}, headers=headers)
    if response.status_code >= 400:
        return None
    show = response.json()
    for line in str(show.get("parameters") or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "num_ctx" and parts[1].isdigit():
            return ContextSize(int(parts[1]), "the Ollama model's num_ctx")
    info = show.get("model_info") or {}
    arch = info.get("general.architecture")
    tokens = _int(info.get(f"{arch}.context_length")) if arch else None
    if tokens:
        # The maximum: Ollama loads it with less unless told otherwise.
        return ContextSize(tokens, "the model's maximum (Ollama may load it with less)", False)
    return None


def _lm_studio(http, base_url, headers, model) -> ContextSize | None:
    body = _get(http, _root(base_url) + "/api/v1/models", headers)
    items = (body or {}).get("models") or (body or {}).get("data") or []
    for item in items:
        if not isinstance(item, dict) or model not in (item.get("key"), item.get("id")):
            continue
        for instance in item.get("loaded_instances") or []:
            tokens = _int(((instance or {}).get("config") or {}).get("context_length"))
            if tokens:
                return ContextSize(tokens, "loaded in LM Studio")
        tokens = _int(item.get("max_context_length"))
        if tokens:
            return ContextSize(tokens, "the model's maximum in LM Studio", False)
    return None


def _llama_cpp(http, base_url, headers, model) -> ContextSize | None:
    body = _get(http, _root(base_url) + "/props", headers)
    settings = (body or {}).get("default_generation_settings") or {}
    tokens = _int(settings.get("n_ctx"))
    return ContextSize(tokens, "llama.cpp's loaded context") if tokens else None
