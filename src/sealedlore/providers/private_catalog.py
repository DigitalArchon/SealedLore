"""nano-gpt's Private Mode models in the model list (providers/private_mode.py).

The relay lists them (`/api/v1/private/tinfoil/models`) by id alone, with no
prices or context lengths. Each is the same model nano-gpt sells as `TEE/…`
(its billing model), plus a Private Mode markup, so a `private/` entry is
made from its `TEE/` counterpart's: the id with dots as dashes
(`TEE/glm-5.3` ↔ `private/glm-5-3`), its prices times `MARKUP`.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from sealedlore.providers.http import make_client

RELAY_MODELS_PATH = "/api/v1/private/tinfoil/models"
# nano-gpt's published Private Mode markup (the proxy's README, Sept 2026).
MARKUP = 1.05
PRIVATE_HOSTS = frozenset({"nano-gpt.com"})
_PRICE_FIELDS = (
    "prompt",
    "completion",
    "cacheReadInputPer1kTokens",
    "input_cache_read",
    "input_cache_write",
)


def offers_private_mode(base_url: str) -> bool:
    return (urlparse(base_url).hostname or "").lower() in PRIVATE_HOSTS


def _key(model_id: str) -> str:
    return model_id.split("/", 1)[-1].lower().replace(".", "-")


def tee_counterpart(model_id: str, ids: Iterable[str]) -> str | None:
    """The `TEE/` model among `ids` that a `private/` model is."""
    if not model_id.startswith("private/"):
        return None
    key = _key(model_id)
    return next((i for i in ids if i.startswith("TEE/") and _key(i) == key), None)


def private_entries(
    private_ids: list[str], listing: Mapping[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """An entry for each Private Mode model, from its TEE/ counterpart's when
    the listing has one (else bare: listed, unpriced)."""
    counterparts = {
        _key(model_id): entry for model_id, entry in listing.items() if model_id.startswith("TEE/")
    }
    entries: dict[str, dict[str, Any]] = {}
    for model_id in private_ids:
        source = counterparts.get(_key(model_id))
        entry = copy.deepcopy(source) if source is not None else {}
        entry["id"] = model_id
        name = entry.get("name") or model_id.split("/", 1)[-1]
        entry["name"] = f"{name} (end-to-end encrypted)"
        pricing = entry.get("pricing")
        if isinstance(pricing, dict):
            for field in _PRICE_FIELDS:
                value = pricing.get(field)
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                scaled = number * MARKUP
                pricing[field] = str(scaled) if isinstance(value, str) else scaled
        entries[model_id] = entry
    return entries


def fetch_private_ids(base_url: str, *, timeout: float = 30.0) -> list[str]:
    """The relay's list of Private Mode models (public; no key sent)."""
    parsed = urlparse(base_url)
    url = f"{parsed.scheme}://{parsed.netloc}{RELAY_MODELS_PATH}"
    with make_client(url) as client:
        try:
            response = client.get(url, timeout=timeout)
            data = response.json() if response.status_code < 400 else {}
        except (httpx.HTTPError, ValueError):
            return []
    items = data.get("data") if isinstance(data, dict) else None
    return [
        item["id"]
        for item in items or []
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item["id"].startswith("private/")
    ]


def encrypted_counterpart(model_id: str, ids) -> str | None:
    """The private/ model that is this TEE/ model end-to-end encrypted, if any."""
    if not model_id.startswith("TEE/"):
        return None
    key = _key(model_id)
    return next((i for i in ids if i.startswith("private/") and _key(i) == key), None)


def private_alternatives(model_id: str, ids) -> list[str]:
    """The other TEE and end-to-end encrypted versions of the same model:
    what a private model can move to when its one host is slow (Help → Model
    trouble). The encrypted twin first."""
    if not model_id.startswith(("TEE/", "private/")):
        return []
    key = _key(model_id)
    same = [i for i in ids if i != model_id and i.startswith(("TEE/", "private/"))]
    same = [i for i in same if _key(i) == key]
    return sorted(dict.fromkeys(same), key=lambda i: (not i.startswith("private/"), i))


E2EE_NOTE = (
    "End-to-end encrypted: every message is sealed on this machine to a key only the "
    "model's attested enclave holds. nano-gpt relays ciphertext; it sees your account, "
    "the model, timing, sizes and cost, never the text."
)
TEE_NOTE = (
    "TEE: the model runs in an attested enclave and its replies are signed, but the text "
    "passes nano-gpt's gateway in the clear."
)


def privacy_label(model_id: str) -> tuple[str, str]:
    """(short label, explanation) for a model's privacy, or ("", "")."""
    if model_id.startswith("private/"):
        return "🔐 End-to-end encrypted", E2EE_NOTE
    if model_id.startswith("TEE/"):
        return "TEE", TEE_NOTE
    return "", ""
