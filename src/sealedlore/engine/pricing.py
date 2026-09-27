"""Estimating a call's cost from the model's listed prices (§8.3).

nano-gpt only sometimes reports what a call cost: the charge appears in the
stream on some responses and not others. It does publish per-model prices on
its models endpoint, and every response reports its token counts, so a
missing cost can be worked out rather than shown as nothing. A reported cost
always wins; an estimate is marked as one wherever it's shown.
"""

from __future__ import annotations

from typing import Any

from sealedlore.models.config import ModelPrice
from sealedlore.models.node import Usage

# Anthropic bills cache writes at 1.25x input and reads at 0.1x. Used only
# when the endpoint doesn't list its own cache prices.
ANTHROPIC_CACHE_WRITE = 1.25
# A one-hour cache write (Config.cache_ttl "1h").
ANTHROPIC_CACHE_WRITE_1H = 2.0
ANTHROPIC_CACHE_READ = 0.10
PER_MILLION = 1_000_000


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def parse_price(model: dict[str, Any]) -> ModelPrice | None:
    """A model's prices in USD per million tokens, from either listing format.

    nano-gpt: {"prompt": 3, "completion": 15, "cacheReadInputPer1kTokens":
    0.0003, "unit": "per_million_tokens"}. OpenRouter: {"prompt": "0.000003",
    "completion": "0.000015", "input_cache_read": "...", "input_cache_write":
    "..."} in USD per token.
    """
    pricing = model.get("pricing")
    if not isinstance(pricing, dict):
        return None
    prompt = _number(pricing.get("prompt"))
    completion = _number(pricing.get("completion"))
    if prompt is None or completion is None:
        return None

    per_million = pricing.get("unit") == "per_million_tokens"
    scale = 1.0 if per_million else PER_MILLION
    cache_read = _number(pricing.get("input_cache_read"))
    cache_write = _number(pricing.get("input_cache_write"))
    per_thousand = _number(pricing.get("cacheReadInputPer1kTokens"))
    return ModelPrice(
        prompt=prompt * scale,
        completion=completion * scale,
        cache_read=(
            per_thousand * 1000
            if per_thousand is not None
            else (cache_read * scale if cache_read is not None else None)
        ),
        cache_write=cache_write * scale if cache_write is not None else None,
    )


def is_anthropic(model: str) -> bool:
    lowered = model.lower()
    return "anthropic/" in lowered or "claude" in lowered


def estimate_cost(usage: Usage, price: ModelPrice, model: str, *, cache_ttl: str = "5m") -> float:
    """What a call should have cost, from its token counts.

    `prompt_tokens` includes cached tokens on both nano-gpt and OpenRouter
    (live: 6,539 = 1,437 fresh + 4,876 read + 226 written), so the fresh part
    is what's left after taking the cache out.
    """
    anthropic = is_anthropic(model)
    read_rate = price.cache_read
    if read_rate is None:
        read_rate = price.prompt * (ANTHROPIC_CACHE_READ if anthropic else 1.0)
    write_rate = price.cache_write
    if cache_ttl == "1h" and anthropic:
        # A listed write price is the five-minute one.
        write_rate = price.prompt * ANTHROPIC_CACHE_WRITE_1H
    elif write_rate is None:
        write_rate = price.prompt * (ANTHROPIC_CACHE_WRITE if anthropic else 1.0)
    fresh = max(0, usage.prompt_tokens - usage.cache_read_tokens - usage.cache_creation_tokens)
    return (
        fresh * price.prompt
        + usage.cache_read_tokens * read_rate
        + usage.cache_creation_tokens * write_rate
        + usage.completion_tokens * price.completion
    ) / PER_MILLION
