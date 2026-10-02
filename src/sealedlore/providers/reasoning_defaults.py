"""What each NanoGPT model does about reasoning when a request says nothing.

`GET {origin}/api/models` is the listing NanoGPT's own chat page reads. Each
text model carries the settings the page starts it with, among them
`defaultSettings.reasoning_effort` (and `thinking`), which the
OpenAI-compatible `/models` listing doesn't give: it lists the levels, not the
default. It needs no key, so none is sent.

Checked Oct 2 2026 (`~/Documents/SealedLoreDocs/design/reasoning.md`): the
stated defaults match what every model did with nothing sent (GLM 5.3 low,
GLM 5.3 Flash and the TEE GLMs max, Kimi K3 max, Opus 5.5 high, Sonnet 4.6 and
DeepSeek V4 none), and nothing sent reasoned like the default on GLM 5.3 and
Flash. Undocumented: read defensively, and an endpoint without it is fine.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from sealedlore.providers.base import ProviderError
from sealedlore.providers.http import make_client

# The fields engine/reasoning.default_reasoning reads; the listing is ~2 MB
# and nothing else of it is kept.
_TOP = ("defaultThinkingEnabled", "defaultReasoningEffort")
_SETTINGS = ("reasoning_effort", "thinking", "reasoning")


def defaults_url(base_url: str) -> str:
    """The listing's address: on the endpoint's host, outside its /api/v1."""
    parsed = urlparse(base_url.strip())
    return f"{parsed.scheme}://{parsed.netloc}/api/models"


def trim(entry: Any) -> dict[str, Any]:
    """The reasoning fields of one listing entry."""
    if not isinstance(entry, Mapping):
        return {}
    kept: dict[str, Any] = {name: entry[name] for name in _TOP if name in entry}
    settings = entry.get("defaultSettings")
    if isinstance(settings, Mapping):
        kept["defaultSettings"] = {name: settings[name] for name in _SETTINGS if name in settings}
    return kept


def fetch_reasoning_defaults(base_url: str, *, timeout: float = 60.0) -> dict[str, dict[str, Any]]:
    """Model id → its reasoning fields. Network: call off the GUI thread.
    Raises ProviderError."""
    url = defaults_url(base_url)
    try:
        with make_client(url, timeout=timeout) as client:
            response = client.get(url)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError(f"couldn't read the models' default reasoning: {exc}") from exc
    models = data.get("models") if isinstance(data, Mapping) else None
    text = models.get("text") if isinstance(models, Mapping) else None
    if not isinstance(text, Mapping):
        raise ProviderError("the models listing had no text models")
    return {model: trim(entry) for model, entry in text.items() if isinstance(model, str)}
