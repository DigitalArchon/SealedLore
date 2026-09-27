"""The endpoint's model list, read into something the app can show and check.

`ChatProvider.fetch_model_prices` returns the raw `/models?detailed=true`
entries (id → entry). This turns each into a `ModelInfo`: what the model
picker lists and what the settings review is told about the current model.
Pure — the fetch happens elsewhere, off the GUI thread.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sealedlore.engine.pricing import parse_price


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str = ""
    owner: str = ""
    description: str = ""
    context_length: int | None = None
    max_output_tokens: int | None = None
    reasoning: bool = False
    reasoning_efforts: tuple[str, ...] = field(default_factory=tuple)
    # USD per million tokens, when listed.
    prompt_price: float | None = None
    completion_price: float | None = None

    def facts(self) -> dict[str, Any]:
        """What the settings review is told about the story's model."""
        return {
            "id": self.id,
            "context_length": self.context_length,
            "max_output_tokens": self.max_output_tokens,
            "supports_reasoning": self.reasoning,
            "reasoning_efforts": list(self.reasoning_efforts) or None,
            "usd_per_million_tokens": (
                {"input": self.prompt_price, "output": self.completion_price}
                if self.prompt_price is not None
                else None
            ),
        }


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _outputs_text(entry: Mapping[str, Any]) -> bool:
    architecture = entry.get("architecture")
    if not isinstance(architecture, dict):
        return True  # unknown: assume a chat model rather than hide it
    outputs = architecture.get("output_modalities")
    if not isinstance(outputs, list):
        return True
    return "text" in outputs


def model_info(model_id: str, entry: Mapping[str, Any]) -> ModelInfo:
    capabilities = entry.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    efforts = entry.get("reasoning_efforts")
    price = parse_price(dict(entry))
    return ModelInfo(
        id=model_id,
        name=_text(entry.get("name")),
        owner=_text(entry.get("owned_by")),
        description=_text(entry.get("description")),
        context_length=_int(entry.get("context_length")),
        max_output_tokens=_int(entry.get("max_output_tokens")),
        reasoning=capabilities.get("reasoning") is True,
        reasoning_efforts=tuple(e for e in efforts if isinstance(e, str))
        if isinstance(efforts, list)
        else (),
        prompt_price=price.prompt if price else None,
        completion_price=price.completion if price else None,
    )


def chat_models(listing: Mapping[str, Mapping[str, Any]]) -> dict[str, ModelInfo]:
    """The chat models in a listing: those whose output is text."""
    return {
        model_id: model_info(model_id, entry)
        for model_id, entry in listing.items()
        if isinstance(entry, Mapping) and _outputs_text(entry)
    }


def matches(info: ModelInfo, query: str) -> bool:
    """Every word of the query appears in the id, name or owner (case-insensitive)."""
    haystack = f"{info.id} {info.name} {info.owner}".lower()
    return all(word in haystack for word in query.lower().split())


def short_count(tokens: int | None) -> str:
    """200000 → "200k", 1000000 → "1M"; blank when unknown."""
    if not tokens:
        return ""
    if tokens >= 1_000_000:
        return f"{round(tokens / 1_000_000, 1):g}M"
    if tokens >= 1_000:
        return f"{round(tokens / 1_000):g}k"
    return str(tokens)
