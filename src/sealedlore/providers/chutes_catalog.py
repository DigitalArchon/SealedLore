"""Chutes' public model list, for an ordinary Chutes endpoint's Browse…

A Chutes endpoint (models/config.py `on_chutes`: llm.chutes.ai, or
claude.chutes.ai speaking Anthropic's Messages API) offers every model
Chutes lists. The list is Chutes' own, at llm.chutes.ai/v1/models, public:
read without the key, whatever the endpoint speaks (claude.chutes.ai's own
listing isn't documented), and parsed as the end-to-end encrypted client
parses it (providers/chutes_e2ee.py `listed_models`), with each model's
price. How private each model is, is its id's (`chutes_tee`): its -TEE ones
go end-to-end encrypted (providers/chutes_provider.py), the others in the
clear.
"""

from __future__ import annotations

from typing import Any

import httpx

from sealedlore.providers.base import ProviderError
from sealedlore.providers.chutes_e2ee import listed_models
from sealedlore.providers.http import reply_json

MODELS_URL = "https://llm.chutes.ai/v1/models"


def fetch_chutes_models(client: httpx.Client) -> dict[str, dict[str, Any]]:
    """Every model Chutes lists, by id, read through `client` with no key and
    nothing of ours in the request (the plain client's own, with its caps and
    its redirect guard)."""
    try:
        response = client.get(MODELS_URL, timeout=30.0)
    except httpx.HTTPError as exc:
        raise ProviderError(f"couldn't fetch Chutes' model list: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise ProviderError(
            f"Chutes' model list couldn't be read (HTTP {response.status_code})",
            status_code=response.status_code,
        )
    try:
        data = reply_json(response)
    except ValueError as exc:
        raise ProviderError("Chutes' model list wasn't JSON") from exc
    return listed_models(data)
