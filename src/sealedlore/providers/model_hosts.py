"""A NanoGPT model's upstream hosts, and NanoGPT's own figures for each.

`GET {origin}/api/models/{id}/providers` (the endpoint NanoGPT's own model
pages read) lists, for a model with a choice of hosts, each host's precision,
privacy class, price, whether it caches, and NanoGPT's current measurement of
its speed: tokens a second and the time to its first token. It also gives the
same figures for NanoGPT's own routing. It needs no key, so none is sent.

The figures are NanoGPT's, one current value each, and time the first token:
a model that reasons takes longer to its first word, which only the speed
test measures (engine/speed_test.py).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from sealedlore.providers.base import ProviderError
from sealedlore.providers.http import make_client

# Bits per weight, for the "FP8 or better" floor. NanoGPT's own floor leaves
# out a host whose precision it doesn't know, so this does too.
BITS = {
    "int4": 4,
    "fp4": 4,
    "nvfp4": 4,
    "fp6": 6,
    "int8": 8,
    "fp8": 8,
    "fp16": 16,
    "bf16": 16,
    "fp32": 32,
}
PRIVACY_LABELS = {
    "zdr": "No retention",
    "no_training": "Kept, not trained on",
    "logs_training": "Kept and trained on",
}


@dataclass(frozen=True)
class Host:
    id: str
    name: str
    available: bool = True
    quantization: str | None = None
    privacy: str | None = None
    tokens_per_second: float | None = None
    first_token_ms: float | None = None
    # USD per million tokens.
    input_price: float | None = None
    output_price: float | None = None
    cache_read_price: float | None = None
    caching: bool | None = None

    @property
    def bits(self) -> int | None:
        return BITS.get((self.quantization or "").lower())

    @property
    def fp8_or_better(self) -> bool:
        return (self.bits or 0) >= 8

    @property
    def privacy_label(self) -> str:
        return PRIVACY_LABELS.get(self.privacy or "", "Unknown")


@dataclass(frozen=True)
class ModelHosts:
    model: str
    supported: bool = False
    # NanoGPT's own routing, as it measures it.
    auto_tokens_per_second: float | None = None
    auto_first_token_ms: float | None = None
    auto_quantization: str | None = None
    hosts: tuple[Host, ...] = field(default_factory=tuple)

    @property
    def available(self) -> tuple[Host, ...]:
        return tuple(host for host in self.hosts if host.available)

    def host(self, host_id: str | None) -> Host | None:
        return next((host for host in self.hosts if host.id == host_id), None)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _per_million(pricing: Any, key: str) -> float | None:
    per_1k = _number(pricing.get(key)) if isinstance(pricing, Mapping) else None
    return per_1k * 1000 if per_1k is not None else None


def parse_model_hosts(model: str, data: Mapping[str, Any]) -> ModelHosts:
    hosts = []
    for entry in data.get("providers") or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("provider"), str):
            continue
        privacy = entry.get("privacy") if isinstance(entry.get("privacy"), Mapping) else {}
        quantization = entry.get("quantization")
        hosts.append(
            Host(
                id=entry["provider"],
                name=str(entry.get("displayName") or entry["provider"]),
                available=entry.get("available") is not False,
                quantization=quantization if isinstance(quantization, str) else None,
                privacy=privacy.get("classification")
                if isinstance(privacy.get("classification"), str)
                else None,
                tokens_per_second=_number(entry.get("tps")),
                first_token_ms=_number(entry.get("ttftMs")),
                input_price=_per_million(entry.get("pricing"), "inputPer1kTokens"),
                output_price=_per_million(entry.get("pricing"), "outputPer1kTokens"),
                cache_read_price=_per_million(entry.get("pricing"), "cacheReadInputPer1kTokens"),
                caching=entry.get("supportsPromptCaching")
                if isinstance(entry.get("supportsPromptCaching"), bool)
                else None,
            )
        )
    auto_quantization = data.get("autoQuantization")
    return ModelHosts(
        model=model,
        supported=data.get("supportsProviderSelection") is True,
        auto_tokens_per_second=_number(data.get("autoTps")),
        auto_first_token_ms=_number(data.get("autoTtftMs")),
        auto_quantization=auto_quantization if isinstance(auto_quantization, str) else None,
        hosts=tuple(hosts),
    )


def hosts_url(base_url: str, model: str) -> str:
    """The listing's address: on the endpoint's host, outside its /api/v1."""
    parsed = urlparse(base_url.strip())
    return f"{parsed.scheme}://{parsed.netloc}/api/models/{quote(model, safe='')}/providers"


def fetch_model_hosts(base_url: str, model: str, *, timeout: float = 30.0) -> ModelHosts:
    """Network: call off the GUI thread. Raises ProviderError."""
    url = hosts_url(base_url, model)
    try:
        with make_client(url, timeout=timeout) as client:
            response = client.get(url)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError(f"couldn't list the hosts for {model}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ProviderError(f"the hosts listing for {model} wasn't an object")
    return parse_model_hosts(model, data)
