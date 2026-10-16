"""What Anthropic's Messages API takes from each generation of Claude. Pure.

The request shape changed with the models, and the API refuses the wrong
one with HTTP 400, so the client has to know which it is talking to
(Anthropic's own docs, read Oct 2026 through the claude-api reference):

- **Through Claude 4.5** (Opus 4.1, Sonnet 4.5, Haiku 4.5 and older):
  extended thinking is `{"type": "enabled", "budget_tokens": N}`, and
  `max_tokens` must leave room above the budget. Temperature and top_p are
  taken, though not beside thinking.
- **Opus 4.6 and Sonnet 4.6**: adaptive thinking, `{"type": "adaptive"}`,
  with `output_config.effort` (low, medium, high, max); a token budget still
  works but is deprecated. Sampling is taken. Thinking is off unless asked,
  and its text comes back summarised.
- **Opus 4.7 and 4.8**: adaptive thinking only (a budget is refused), with
  xhigh added to the efforts; temperature, top_p and top_k are refused;
  thinking is off unless asked, and its text is left out unless
  `display: "summarized"` asks for it.
- **Opus 5 and later, Sonnet 5 and later, Haiku 5.5 and later, Fable,
  Mythos**: as 4.7, and thinking runs whether or not it is asked for, at a
  default effort (medium or high); it can't be turned off, so the least is
  effort "low".

A model id that can't be read is taken as the newest shape: new models
follow it, and an older id reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sealedlore.models.generation import ReasoningConfig

FAMILIES = ("opus", "sonnet", "haiku", "fable", "mythos")
# Thinking is always on, whatever the version number says.
ALWAYS_THINKING = ("fable", "mythos")

# The app's efforts to Anthropic's. "none" and "minimal" aren't levels
# Anthropic has; the app never sends them here (none is listed), and if one
# came it would mean as little as possible.
_EFFORTS = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}
# A token budget for the models that take one (Claude 4.5 and older), by
# the app's level: Anthropic's minimum is 1,024.
BUDGETS = {"low": 1024, "medium": 4096, "high": 16000, "xhigh": 32000, "max": 32000}
# The Messages API needs max_tokens on every request; the app's own default
# for a passage (models/generation.py) stands in when a request names none.
DEFAULT_MAX_TOKENS = 4000

_NUMBER = re.compile(r"^\d+$")


@dataclass(frozen=True)
class ModelSurface:
    """What one model's requests may carry."""

    family: str
    version: tuple[int, int]
    # Thinking is adaptive with an effort; otherwise a token budget.
    adaptive: bool
    # Temperature and top_p are taken (never beside thinking).
    samples: bool
    # The effort ladder has xhigh (Opus 4.7 and later).
    xhigh: bool
    # Thinking runs when the request says nothing, and can't be turned off.
    thinks_by_default: bool
    # Thinking text is left out of the reply unless `display` asks for it.
    quiet: bool

    @property
    def efforts(self) -> list[str]:
        """The levels to list for the model, in the app's vocabulary."""
        return ["low", "medium", "high", *(["xhigh"] if self.xhigh else []), "max"]


def parse_model(model: str) -> tuple[str, tuple[int, int]]:
    """A model id's family and version: ("sonnet", (4, 6)) for
    "claude-sonnet-4-6", ("opus", (4, 1)) for "claude-opus-4-1-20250805",
    ("sonnet", (3, 5)) for "claude-3-5-sonnet-20241022". Unknown parts
    read as "" and (0, 0)."""
    name = model.strip().lower().rsplit("/", 1)[-1]
    tokens = re.split(r"[-.]", name)
    family = next((token for token in tokens if token in FAMILIES), "")
    # A date (20250805) is eight digits; a version part is one or two.
    numbers = [int(token) for token in tokens if _NUMBER.match(token) and len(token) < 8]
    major = numbers[0] if numbers else 0
    minor = numbers[1] if len(numbers) > 1 else 0
    return family, (major, minor)


def surface_of(model: str) -> ModelSurface:
    family, version = parse_model(model)
    if family in ALWAYS_THINKING:
        return ModelSurface(family, version, True, False, True, True, True)
    if not family and version == (0, 0):
        # Unread: the newest shape, with nothing assumed about its default.
        return ModelSurface(family, version, True, False, True, False, True)
    if version < (4, 6):
        return ModelSurface(family, version, False, True, False, False, False)
    if version < (4, 7):
        return ModelSurface(family, version, True, True, False, False, False)
    # Opus 4.7 and 4.8 think only when asked; from 5 on, thinking is always on.
    return ModelSurface(family, version, True, False, True, version >= (5, 0), True)


@dataclass(frozen=True)
class ThinkingFields:
    """What one request carries for its reasoning level."""

    fields: dict[str, Any]
    max_tokens: int
    # Whether temperature and top_p may go with it.
    sampling: bool


def thinking_fields(
    surface: ModelSurface, reasoning: ReasoningConfig, max_tokens: int | None
) -> ThinkingFields:
    """The thinking fields for `reasoning` on `surface`, and the max_tokens
    and sampling that go with them.

    Nothing asked (the app's "as low as possible" on a model it knows
    nothing about) sends nothing, except on a model whose thinking can't be
    turned off: there the least is effort "low", and the client asks for it
    rather than leave the model at its own medium or high.
    """
    limit = max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS
    effort = reasoning.effort if reasoning.enabled else None
    if effort in (None, "none"):
        if surface.thinks_by_default:
            return ThinkingFields({"output_config": {"effort": "low"}}, limit, False)
        return ThinkingFields({}, limit, surface.samples)
    level = _EFFORTS.get(effort, "high")
    if surface.adaptive:
        if level == "xhigh" and not surface.xhigh:
            level = "high"
        thinking: dict[str, Any] = {"type": "adaptive"}
        if surface.quiet:
            # The reasoning is kept with the passage and shown in the
            # transcript; left out, a slow start would look like nothing.
            thinking["display"] = "summarized"
        fields = {"thinking": thinking, "output_config": {"effort": level}}
        return ThinkingFields(fields, limit, False)
    budget = BUDGETS[level]
    fields = {"thinking": {"type": "enabled", "budget_tokens": budget}}
    # The budget counts against max_tokens, so the answer keeps its own room.
    return ThinkingFields(fields, budget + limit, False)


def listing_entry(item: dict[str, Any]) -> dict[str, Any]:
    """A `/v1/models` entry in the shape the app's catalog reads
    (engine/catalog.py): Anthropic's fields kept, with the names the app
    looks for beside them. No prices: Anthropic lists none."""
    surface = surface_of(str(item.get("id", "")))
    capabilities = item.get("capabilities")
    entry = dict(item)
    entry.setdefault("name", item.get("display_name", ""))
    entry.setdefault("owned_by", "anthropic")
    entry.setdefault("context_length", item.get("max_input_tokens"))
    entry.setdefault("max_output_tokens", item.get("max_tokens"))
    entry["capabilities"] = {
        **(capabilities if isinstance(capabilities, dict) else {}),
        "reasoning": True,
    }
    entry["reasoning_efforts"] = surface.efforts
    return entry


def reasoning_default(model: str) -> dict[str, Any]:
    """What `model` does about thinking when a request says nothing, in the
    shape `engine/reasoning.default_reasoning` reads: on by default, or
    only when asked."""
    kind = "adaptive" if surface_of(model).thinks_by_default else "disabled"
    return {"defaultSettings": {"thinking": {"type": kind}}}
