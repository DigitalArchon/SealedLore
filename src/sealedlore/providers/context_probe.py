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

from sealedlore.models.config import chutes_tee, key_problem, sealed_service
from sealedlore.providers.base import stated_count
from sealedlore.providers.http import make_client, reply_json

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
    """A context size a server stated, or None for what is none: JSON lets it
    write 1e999, NaN or true where the size goes."""
    return stated_count(value) or None


def _dict(value: Any) -> dict[str, Any]:
    """`value` when it is a JSON object, else an empty one: a server may
    answer with a list or a number where an object is read."""
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def detect_context(
    base_url: str,
    api_key: str,
    model: str,
    *,
    api: str = "auto",
    client: httpx.Client | None = None,
) -> ContextSize | None:
    """The model's context, or None if no server answered with one. `api`:
    what the endpoint speaks, which alone may make it a sealed one."""
    if key_problem(api_key):
        return None  # nothing sent: a header can't carry it (Settings says so first)
    if sealed_service(base_url, api) or chutes_tee(model, base_url):
        # An end-to-end encrypted endpoint, or one of Chutes' TEE models
        # (providers/privacy.py), is sent nothing in the clear, a key with a
        # listing request included (Chutes' context is read from its public
        # list: chutes_provider.chutes_context).
        return None
    http = client or make_client(base_url, timeout=httpx.Timeout(TIMEOUT_SECONDS))
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        for probe in (_listing, _ollama, _lm_studio, _llama_cpp):
            try:
                found = probe(http, base_url, headers, model)
            except (httpx.HTTPError, ValueError, LookupError, TypeError, AttributeError):
                # Each way of asking reads the shape its server answers in;
                # this is for whatever one of them still didn't foresee, so
                # that an answer nothing can read is a server that didn't say.
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
    return reply_json(response)


def _listing(http, base_url, headers, model) -> ContextSize | None:
    body = _get(http, base_url.rstrip("/") + "/models", headers, params={"detailed": "true"})
    for item in _list(_dict(body).get("data")):
        if isinstance(item, dict) and item.get("id") == model:
            tokens = _int(item.get("context_length")) or _int(
                _dict(item.get("top_provider")).get("context_length")
            )
            if tokens:
                return ContextSize(tokens, "the endpoint's model list")
    return None


def _ollama(http, base_url, headers, model) -> ContextSize | None:
    root = _root(base_url)
    running = _get(http, root + "/api/ps", headers)
    for item in _list(_dict(running).get("models")):
        name = item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str) and name.split(":")[0] == model.split(":")[0]:
            tokens = _int(item.get("context_length"))
            if tokens:
                return ContextSize(tokens, "loaded in Ollama")
    response = http.post(root + "/api/show", json={"model": model}, headers=headers)
    if response.status_code >= 400:
        return None
    show = _dict(reply_json(response))
    parameters = show.get("parameters")
    for line in parameters.splitlines() if isinstance(parameters, str) else ():
        parts = line.split()
        # ASCII digits, and no more of them than a size has: "²" is a digit
        # too, and `int` reads neither it nor thousands of them.
        digits = parts[1] if len(parts) == 2 and parts[0] == "num_ctx" else ""
        if digits.isascii() and digits.isdigit() and len(digits) <= 12:
            tokens = _int(int(digits))
            if tokens:
                return ContextSize(tokens, "the Ollama model's num_ctx")
    info = _dict(show.get("model_info"))
    arch = info.get("general.architecture")
    tokens = _int(info.get(f"{arch}.context_length")) if isinstance(arch, str) and arch else None
    if tokens:
        # The maximum: Ollama loads it with less unless told otherwise.
        return ContextSize(tokens, "the model's maximum (Ollama may load it with less)", False)
    return None


def _lm_studio(http, base_url, headers, model) -> ContextSize | None:
    body = _get(http, _root(base_url) + "/api/v1/models", headers)
    body = _dict(body)
    for item in _list(body.get("models")) or _list(body.get("data")):
        if not isinstance(item, dict) or model not in (item.get("key"), item.get("id")):
            continue
        for instance in _list(item.get("loaded_instances")):
            tokens = _int(_dict(_dict(instance).get("config")).get("context_length"))
            if tokens:
                return ContextSize(tokens, "loaded in LM Studio")
        tokens = _int(item.get("max_context_length"))
        if tokens:
            return ContextSize(tokens, "the model's maximum in LM Studio", False)
    return None


def _llama_cpp(http, base_url, headers, model) -> ContextSize | None:
    body = _get(http, _root(base_url) + "/props", headers)
    settings = _dict(_dict(body).get("default_generation_settings"))
    tokens = _int(settings.get("n_ctx"))
    return ContextSize(tokens, "llama.cpp's loaded context") if tokens else None
