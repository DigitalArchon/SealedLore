"""What SealedLore does differently for particular models, in one place: the
prompt texts it leaves out for a model it measured them hurting, and the ways
it shapes a request for a model's own API. Pure.

The author (Nov 18 2026): the app should show which models it is tuned for
and how. Every model field says so beside it (gui/model_picker.py), and Help
→ Models SealedLore adjusts for… lists them all. Where a request adjustment is
made elsewhere (providers/anthropic_models.py, providers/private_mode.py,
engine/reasoning.py, providers/wire.py), its entry here only describes it, and
asks the same code which models it applies to, so the words can't drift from
what is done. A prompt adjustment is made here: assembly leaves a text out for
a model this module names (`left_out`).

Add a prompt adjustment only with a measurement behind it, as for any change
of wording, and say it in `why`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from sealedlore.models.config import split_model

TuningKind = Literal["prompt", "request"]


def bare(model: str) -> str:
    """A model reference as one comparable name: no endpoint, no vendor
    prefix, no variant suffix, dots as dashes, lower case
    ("Anthropic::claude-opus-5-5", "anthropic/claude-opus-5.5" and
    "claude-opus-5-5:thinking" all read "claude-opus-5-5")."""
    name = split_model(model.strip())[1].lower()
    name = name.rsplit("/", 1)[-1].split(":", 1)[0]
    return name.replace(".", "-")


@dataclass(frozen=True)
class ModelTuning:
    key: str
    # Which models, in words, for the list.
    models: str
    kind: TuningKind
    # What is done differently, in a sentence.
    what: str
    # Why: the measurement, or what the model's API requires.
    why: str
    applies: Callable[[str], bool]
    # Prompt texts left out of every request to the model.
    texts_left_out: frozenset[str] = field(default_factory=frozenset)


def _opus_5_5(model: str) -> bool:
    return bare(model).startswith("claude-opus-5-5")


def _claude(model: str) -> bool:
    return bare(model).startswith("claude-")


def _cache_marked(model: str) -> bool:
    """As the wire decides with the config's default patterns
    (`Config.anthropic_model_patterns`, `cache_control_mode` "auto")."""
    from sealedlore.models.config import Config
    from sealedlore.providers.wire import should_use_cache_control

    return should_use_cache_control(model, patterns=Config().anthropic_model_patterns)


def _anthropic_shape(model: str) -> bool:
    from sealedlore.providers.anthropic_models import parse_model

    return _claude(model) and parse_model(bare(model))[0] != ""


def _private_family(model: str) -> bool:
    """NanoGPT's Private Mode (providers/privacy.py decides), in a family its
    request is shaped for (providers/private_mode.py `shape_body`)."""
    from sealedlore.providers.privacy import NANOGPT_PRIVATE, privacy_at

    if privacy_at(model).service != NANOGPT_PRIVATE:
        return False
    return bare(model).startswith(("glm-5", "deepseek-v4", "gemma", "kimi"))


def _private(model: str) -> bool:
    """As engine/reasoning.py asks it: private by providers/privacy.py."""
    from sealedlore.providers.privacy import privacy_at

    return privacy_at(model).private


TUNINGS: tuple[ModelTuning, ...] = (
    ModelTuning(
        key="opus-5-5-round-dialogue",
        models="Claude Opus 5.5",
        kind="prompt",
        what=(
            "Characters played by their own model (a trial): the sentence that asks the "
            "storyteller to put their words into the passage as dialogue is left out "
            "(prompt text turn.round.remember)."
        ),
        why=(
            "Measured on replayed turns: Opus 5.5 never reported a character's speech instead "
            "of writing it, with or without the sentence, and blind judges preferred its "
            "passages without it. Other models keep it: it fixed that fault for them."
        ),
        applies=_opus_5_5,
        texts_left_out=frozenset({"turn.round.remember"}),
    ),
    ModelTuning(
        key="anthropic-thinking",
        models="Claude models, on Anthropic's own API",
        kind="request",
        what=(
            "Thinking is asked for in the form each generation takes: a token budget up to "
            "Claude 4.5, an effort from 4.6, never switched off from 5 (the lowest effort "
            "instead); temperature is left out from 4.7."
        ),
        why="Anthropic's API refuses the other forms for those models.",
        applies=_anthropic_shape,
    ),
    ModelTuning(
        key="claude-cache-markers",
        models="Claude models, on any endpoint",
        kind="request",
        what=(
            "The prompt carries cache markers (an hour's lifetime by default), so the "
            "unchanging part of each request is read back at a tenth of the price."
        ),
        why="Claude caches only what a request marks; other models cache by themselves.",
        applies=_cache_marked,
    ),
    ModelTuning(
        key="private-mode-families",
        models="NanoGPT's end-to-end encrypted GLM, DeepSeek, Gemma and Kimi models",
        kind="request",
        what=(
            "The sealed request is shaped for the model family as NanoGPT's own proxy "
            "shapes it (GLM always reasons, Gemma only as :thinking, DeepSeek only when "
            "asked, Kimi without its sampling fields)."
        ),
        why="The enclave's router takes each family's fields only in that form.",
        applies=_private_family,
    ),
    ModelTuning(
        key="private-lowest-reasoning",
        models="TEE and end-to-end encrypted models",
        kind="request",
        what="'As low as possible' reasoning is the model's lowest listed level, always.",
        why=(
            "Their default reasoning made replies slow to start, where the lowest level "
            "answered quickly and as well."
        ),
        applies=_private,
    ),
)


# Adjustments made only when the endpoint speaks Anthropic's own API.
ANTHROPIC_ONLY = frozenset({"anthropic-thinking"})


def tunings_for(model: str, *, anthropic_api: bool | None = None) -> list[ModelTuning]:
    """Every adjustment SealedLore makes for `model` (a reference as a field
    holds it), prompt ones first. `anthropic_api`: whether its endpoint speaks
    Anthropic's own API (False leaves out what only that API gets; None, not
    known, keeps it)."""
    if not model.strip():
        return []
    found = [
        tuning
        for tuning in TUNINGS
        if tuning.applies(model) and not (anthropic_api is False and tuning.key in ANTHROPIC_ONLY)
    ]
    return sorted(found, key=lambda tuning: tuning.kind != "prompt")


def left_out(model: str | None) -> frozenset[str]:
    """The prompt texts never sent to `model`."""
    if not model:
        return frozenset()
    return frozenset().union(*(tuning.texts_left_out for tuning in tunings_for(model)))


def summary(model: str, *, anthropic_api: bool | None = None) -> str:
    """For a field's tooltip: how SealedLore adjusts for this model, or ""."""
    lines = [f"• {tuning.what}" for tuning in tunings_for(model, anthropic_api=anthropic_api)]
    if not lines:
        return ""
    return (
        "SealedLore adjusts for this model:\n"
        + "\n".join(lines)
        + ("\n\nHelp → Models SealedLore adjusts for… says why.")
    )


_SAFE = re.compile(r"\s+")


def described() -> list[tuple[str, str, str, str]]:
    """(models, kind, what, why) for every adjustment, for Help's list."""
    return [(t.models, t.kind, _SAFE.sub(" ", t.what), _SAFE.sub(" ", t.why)) for t in TUNINGS]
