"""What SealedLore does differently for particular models, in one place: the
prompt texts it leaves out for a model it measured them hurting, the lore it
sends a model's turns, and the ways it shapes a request for a model's own
API. Pure.

The author (Nov 18 2026): the app should show which models it is tuned for
and how. Every model field says so beside it (gui/model_picker.py), and Help
→ Models SealedLore recommends and adjusts for… lists them all. Where a request adjustment is
made elsewhere (providers/anthropic_models.py, providers/private_mode.py,
engine/reasoning.py, providers/wire.py), its entry here only describes it, and
asks the same code which models it applies to, so the words can't drift from
what is done. A prompt adjustment is made here: assembly leaves a text out for
a model this module names (`left_out`). So is a lore adjustment: the session
asks `sends_lore_from_full` whether the storyteller's model in force is given
the parts of a condensed always-on entry's full text a turn is about
(engine/lore_whole_back.py), and embeds them ahead only for one that is.

Add a prompt or lore adjustment only with a measurement behind it, as for any
change of wording, and say it in `why`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from sealedlore.models.config import split_model

if TYPE_CHECKING:
    from sealedlore.engine.prompt_texts import PromptTexts

# "prompt": a text worded the model's own way or left out; "lore": less of the
# lore sent with a turn; "request": the request shaped for the model's API.
TuningKind = Literal["prompt", "lore", "request"]
# The order a list says them in: what the model reads, then how it is asked.
KIND_ORDER: dict[str, int] = {"prompt": 0, "lore": 1, "request": 2}
# Which of a model's uses a prompt or lore adjustment is for: the storyteller
# (a story's turns, Questions and private scenes, whose system block it
# shares), or a character played by their own model. A request adjustment is
# for every use ("any").
TuningRole = Literal["storyteller", "character", "any"]
ROLE_WORDS: dict[str, str] = {
    "storyteller": "as the storyteller",
    "character": "playing a character",
    "any": "",
}


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
    # Which use of the model a prompt adjustment is for.
    role: TuningRole = "any"
    # Prompt texts left out of every request to the model in that role.
    texts_left_out: frozenset[str] = field(default_factory=frozenset)
    # Prompt texts given the model in its own wording, by key (a text the
    # author has edited keeps the author's: `tuned_texts`).
    texts_replaced: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    # A lore adjustment: the parts of a condensed always-on entry's full text
    # a turn is about are not sent with the model's turns in that role
    # (engine/lore_whole_back.py), nor embedded for it (`sends_lore_from_full`).
    lore_from_full_left_out: bool = False


def _opus_5_5(model: str) -> bool:
    return bare(model).startswith("claude-opus-5-5")


def _glm_5_3(model: str) -> bool:
    """GLM 5.3 on any endpoint, its TEE and encrypted forms too; not Flash,
    which was not measured."""
    return bare(model) == "glm-5-3"


# GLM 5.3's character ask ends with this (measured, `glm-5-3-character-ask`).
_GO_BY_THE_STORY = (
    "If someone asks what you said or did, go by the story above alone: if it doesn't "
    "show you saying or doing it, you didn't, and you say so."
)


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
        role="storyteller",
        texts_left_out=frozenset({"turn.round.remember"}),
    ),
    ModelTuning(
        key="glm-5-3-character-ask",
        models="GLM 5.3",
        kind="prompt",
        what=(
            "Characters played by their own model (a trial): the question each answers "
            "ends by telling them to go by the story alone when asked what they said or did "
            "(prompt texts characters.own.ask and characters.own.ask.after)."
        ),
        why=(
            'Measured on leading questions ("what did you tell her?" when nothing was said): '
            "GLM 5.3 invented an answer 9 times in 12 with the usual wording and 4 with this, "
            "on both blind judges, and was preferred on ordinary rounds. Other models keep the "
            "usual wording: Opus 5.5 doesn't invent unasked, and the sentence made DeepSeek "
            "V4.1 Flash and Kimi K3 worse."
        ),
        applies=_glm_5_3,
        role="character",
        texts_replaced=MappingProxyType(
            {
                "characters.own.ask": (
                    "That is where things stand. {name}, what do you say, do or mean to do "
                    "now? " + _GO_BY_THE_STORY
                ),
                "characters.own.ask.after": (
                    "That is where things stand. {player} has just taken their turn, last "
                    "above: what they say is said, and what they try hasn't happened yet; the "
                    "storyteller will write what comes of it. {name}, what do you say, do or "
                    "mean to do now? " + _GO_BY_THE_STORY
                ),
            }
        ),
    ),
    ModelTuning(
        key="claude-lore-from-full",
        models="Claude models, on any endpoint",
        kind="lore",
        what=(
            "Lore from the full entry is not sent: when an always-on lore entry goes "
            "condensed, the parts of its full text a turn is about go with that turn to every "
            "other storyteller, and Claude is given the condensed version alone."
        ),
        why=(
            "Measured in a blind judged comparison: Claude Sonnet 4.6 and Claude Opus 5.5 "
            "wrote no better with those parts, which cost about a third more a turn on Sonnet, "
            "where GLM 5.3 wrote clearly better with them, so every other model is sent them."
        ),
        applies=_claude,
        role="storyteller",
        lore_from_full_left_out=True,
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


def tunings_for(
    model: str, *, anthropic_api: bool | None = None, role: str | None = None
) -> list[ModelTuning]:
    """Every adjustment SealedLore makes for `model` (a reference as a field
    holds it), prompt ones first. `anthropic_api`: whether its endpoint speaks
    Anthropic's own API (False leaves out what only that API gets; None, not
    known, keeps it). `role`: only what applies to the model in that use
    ("storyteller", "character", or another role's name, which no prompt
    adjustment is for); None, every one."""
    if not model.strip():
        return []
    found = [
        tuning
        for tuning in TUNINGS
        if tuning.applies(model)
        and not (anthropic_api is False and tuning.key in ANTHROPIC_ONLY)
        and (role is None or tuning.role in ("any", role))
    ]
    return sorted(found, key=lambda tuning: KIND_ORDER[tuning.kind])


def left_out(model: str | None, role: str = "storyteller") -> frozenset[str]:
    """The prompt texts never sent to `model` in `role`."""
    if not model:
        return frozenset()
    return frozenset().union(*(tuning.texts_left_out for tuning in tunings_for(model, role=role)))


def sends_lore_from_full(model: str | None, role: str = "storyteller") -> bool:
    """Whether `model` in `role` is sent the parts of a condensed always-on
    entry's full text a turn is about (engine/lore_whole_back.py): every
    model but those a lore adjustment names (Claude, as the storyteller)."""
    if not model or not model.strip():
        return True
    # Asked of the lore adjustments alone, as `tunings_for` would choose them:
    # this is asked on every turn and preview.
    return not any(
        tuning.lore_from_full_left_out and tuning.role in ("any", role) and tuning.applies(model)
        for tuning in TUNINGS
    )


def replaced(model: str | None, role: str) -> dict[str, str]:
    """The texts `model` is given in its own wording in `role`, by key."""
    if not model:
        return {}
    texts: dict[str, str] = {}
    for tuning in tunings_for(model, role=role):
        texts.update(tuning.texts_replaced)
    return texts


def tuned_texts(texts: PromptTexts, model: str | None, role: str) -> PromptTexts:
    """The texts in force for `model` in `role`: its own wording where the
    registry has one, and the author's edits (`texts.overrides`) over it, so
    a text the author changed stays theirs whatever the model."""
    from sealedlore.engine.prompt_texts import PromptTexts

    own = replaced(model, role)
    if not own:
        return texts
    return PromptTexts({**own, **texts.overrides})


def _models_in_words(tunings) -> str:
    return ", ".join(
        f"{t.models} ({ROLE_WORDS[t.role]})" if ROLE_WORDS[t.role] else t.models for t in tunings
    )


def models_worded_for(key: str) -> str:
    """The models, in words, that get SealedLore's own wording of text `key`
    in place of the default, for the prompt editor; "" for none. The author's
    edit of the text replaces theirs too (`tuned_texts`)."""
    return _models_in_words(t for t in TUNINGS if key in t.texts_replaced)


def models_left_out_for(key: str) -> str:
    """The models, in words, that aren't sent text `key` at all (`left_out`),
    edited or not; "" for none."""
    return _models_in_words(t for t in TUNINGS if key in t.texts_left_out)


def summary(model: str, *, anthropic_api: bool | None = None, role: str | None = None) -> str:
    """For a field's tooltip: how SealedLore adjusts for this model in
    `role` (every use when None), or ""."""
    tunings = tunings_for(model, anthropic_api=anthropic_api, role=role)
    lines = [
        f"• {tuning.what}"
        + (f" ({ROLE_WORDS[tuning.role]})" if role is None and ROLE_WORDS[tuning.role] else "")
        for tuning in tunings
    ]
    if not lines:
        return ""
    return (
        "SealedLore adjusts for this model:\n"
        + "\n".join(lines)
        + ("\n\nHelp → Models SealedLore recommends and adjusts for… says why.")
    )


def is_tuned(model: str, role: str | None = None) -> bool:
    """Whether SealedLore adjusts its prompt for `model` in `role` (not
    only shapes its requests): what "· tuned prompts" means in a list."""
    return any(tuning.kind == "prompt" for tuning in tunings_for(model, role=role))


def sent_differently(model: str, role: str | None = None) -> bool:
    """Whether what `model` is sent in `role` differs from what other models
    are: its prompt worded for it (`is_tuned`), or less of the lore."""
    return any(tuning.kind in ("prompt", "lore") for tuning in tunings_for(model, role=role))


_SAFE = re.compile(r"\s+")


def described() -> list[tuple[str, str, str, str]]:
    """(models, kind, what, why) for every adjustment, for Help's list; a
    prompt adjustment's models say which use it is for."""
    return [
        (
            t.models + (f", {ROLE_WORDS[t.role]}" if ROLE_WORDS[t.role] else ""),
            t.kind,
            _SAFE.sub(" ", t.what),
            _SAFE.sub(" ", t.why),
        )
        for t in TUNINGS
    ]
