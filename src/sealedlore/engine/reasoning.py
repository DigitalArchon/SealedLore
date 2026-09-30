"""How much each request asks a model to reason. Pure: the listing and the
"reasons unasked" mark come in, what to send goes out.

The author chooses a level for the story model and one for every other call
(Settings → Generation). Measured on NanoGPT (Sept 29-30 2026), no one request
works for every model:

- Sending nothing is lightest for a model that reasons only when asked
  (Sonnet 4.x and 5.5, DeepSeek V4). Asking such a model for "low" switches
  thinking on: Sonnet 4.6 thought 40-130 tokens a turn, Kimi K2.6 went from
  4.8s to 9.6s.
- A model that reasons whatever is sent does so at its own default, which
  can be long: Kimi K3 thought ~2,000 tokens before a hard turn (24-29s to
  its first word) and ~50 when asked for low (5-6s).
- An explicit "off" is refused by some (GLM 5.3: HTTP 400; Opus 5.5: 400 or
  a disguised 503), so it is never sent.
- The listing can't tell the two kinds apart: Sonnet 4.6 and Kimi K3 both
  list "low" first. So "as low as possible" sends nothing, and a model that
  reasons at length anyway (`EXCESSIVE_REASONING`, from the reply itself) is
  marked and asked for its lowest listed level from then on, for a week.
  Kept by model, not host, since the author's own calls are the measurement.

A TEE or encrypted model starts at low: its GLM took 37s (TEE) and 107s
(encrypted) over a 100-word summary at its default, 10s and 9s at low.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sealedlore.models.config import Config, ModelReasoning
from sealedlore.models.generation import (
    EFFORT_SCALE,
    GenerationParams,
    ReasoningConfig,
    ReasoningLevel,
)
from sealedlore.providers.tee import is_tee

# "story": the story model's own turns (passages, Questions, private turns),
# which take the author's sampling settings; "side": everything else, which
# keeps its own fixed limits.
CallKind = Literal["story", "side"]

UNASKED_DAYS = 7
# Reasoning unasked counts only at length. GLM 5.3 always thinks a little
# (median 41-66 tokens a scene read on honest hosts, the official host's
# most 211), and asking it for low bought nothing; Kimi K3 thought 1,800-2,100
# before a hard turn. One reply past this marks the model.
EXCESSIVE_REASONING = 500

LEVEL_LABELS: dict[str, str] = {
    "least": "As low as possible",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "max": "Max",
}

# Asked for a level with nothing listed to map it to, these are the efforts
# endpoints commonly take.
_UNLISTED = {"low": "low", "medium": "medium", "high": "high", "max": "high"}


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def reasons_unasked(facts: ModelReasoning | None, now: datetime | None = None) -> bool:
    """Whether the model reasoned unasked within the last week."""
    when = _parse(facts.unasked_at if facts else None)
    if when is None:
        return False
    return (now or datetime.now(UTC)) - when < timedelta(days=UNASKED_DAYS)


def _ordered(efforts: list[str]) -> list[str]:
    return sorted((e for e in efforts if e in EFFORT_SCALE), key=EFFORT_SCALE.index)


def lowest(facts: ModelReasoning | None) -> str:
    """The least a model lists ("none" included), or "low" when it lists nothing."""
    listed = _ordered(facts.efforts if facts else [])
    return listed[0] if listed else "low"


def nearest(level: str, efforts: list[str]) -> str | None:
    """The listed effort closest to `level`, the higher on a tie (the author
    asked for more, not less). "none" is never a stand-in for a level."""
    listed = [e for e in _ordered(efforts) if e != "none"]
    if not listed:
        return None
    target = EFFORT_SCALE.index(level)
    return min(listed, key=lambda e: (abs(EFFORT_SCALE.index(e) - target), -EFFORT_SCALE.index(e)))


def _effort(effort: str) -> ReasoningConfig:
    if effort == "none":
        return ReasoningConfig(effort="none")
    return ReasoningConfig(enabled=True, effort=effort)  # type: ignore[arg-type]


def reasoning_for(
    level: ReasoningLevel,
    model: str,
    facts: ModelReasoning | None,
    *,
    now: datetime | None = None,
) -> ReasoningConfig:
    """What to send `model` for the author's `level`."""
    if level == "least":
        if is_tee(model) or reasons_unasked(facts, now):
            return _effort(lowest(facts))
        return ReasoningConfig()
    if facts is not None and facts.reasons is False and not facts.efforts:
        return ReasoningConfig()  # listed as not reasoning: nothing to ask for
    listed = nearest(level, facts.efforts) if facts is not None else None
    return _effort(listed or _UNLISTED[level])


def level_for(config: Config, kind: CallKind) -> ReasoningLevel:
    return config.reasoning_story if kind == "story" else config.reasoning_side


def params_for(
    config: Config,
    kind: CallKind,
    model: str,
    facts: ModelReasoning | None,
    **fixed: Any,
) -> GenerationParams:
    """One request's parameters: the author's sampling settings for the story
    model's turns (a fresh set for any other call), `fixed` over them, and the
    reasoning the author's level asks of `model`."""
    params = config.generation.model_copy(deep=True) if kind == "story" else GenerationParams()
    for name, value in fixed.items():
        setattr(params, name, value)
    params.reasoning = reasoning_for(level_for(config, kind), model, facts)
    return params


def config_params(config: Config, kind: CallKind, model: str, **fixed: Any) -> GenerationParams:
    """`params_for` from what the config already knows, for a call made with
    no story open (a premise draft, Test speed…): nothing is fetched."""
    return params_for(config, kind, model, config.model_reasoning.get(model), **fixed)


def config_reasoning(config: Config, kind: CallKind, model: str) -> ReasoningConfig:
    """What the author's level asks of `model`, from what the config knows."""
    return reasoning_for(level_for(config, kind), model, config.model_reasoning.get(model))


def listed(entry: dict[str, Any] | None) -> ModelReasoning | None:
    """A model's reasoning as its models-list entry gives it."""
    if not isinstance(entry, dict):
        return None
    capabilities = entry.get("capabilities")
    reasons = capabilities.get("reasoning") if isinstance(capabilities, dict) else None
    efforts = entry.get("reasoning_efforts")
    return ModelReasoning(
        reasons=reasons if isinstance(reasons, bool) else None,
        efforts=[e for e in efforts if isinstance(e, str)] if isinstance(efforts, list) else [],
    )


def describe(config: ReasoningConfig) -> str:
    """What a request sends, for the Settings note: "nothing", "low"…"""
    if config.effort == "none":
        return "none"
    if not config.enabled:
        return "nothing (the model's own default)"
    return config.effort or "on"


def caught_note(model: str) -> str:
    """Said once, when a model is first caught reasoning unasked: the long
    wait was the model's (or its host's), not the app's."""
    return (
        f"{model} reasoned at length though SealedLore asked it for as little as "
        "possible, which is why that reply was slow to start. From now on it will "
        "be asked for its lowest reasoning level instead."
    )
