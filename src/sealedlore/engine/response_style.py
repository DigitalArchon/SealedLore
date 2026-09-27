"""How long, and how dense, the model's passage should be. See §8.2.

One table, one place to tune. Each preset says three things, because human
testing showed a word count alone isn't enough: a 29-word turn ("I exit my ship
and head for the nearest bar") got 363 words, and the excess wasn't the model
running past the author's action — it was describing every ship in the bay and
every patron in the bar. So each preset states a length *and* what is worth
describing, and ends on handing the turn back.

Where the text goes is fixed by caching (§6.1): the story's default lives in
the system style block, which must not change turn to turn; a per-turn
override goes in the volatile tail only; and a short form rides on the final
`REMEMBER:` line, because recency is the strongest lever there is.
"""

from __future__ import annotations

from dataclasses import dataclass

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.models.node import ResponseStyle
from sealedlore.models.story import StyleDirectives


@dataclass(frozen=True)
class ResponsePreset:
    key: ResponseStyle
    label: str
    summary: str


# The wording of each is in engine/prompt_texts.py ("length.<key>"), with how
# it was calibrated.
PRESETS: dict[ResponseStyle, ResponsePreset] = {
    "adaptive": ResponsePreset(
        key="adaptive",
        label="Match my turn",
        summary="scales with what you write; never more than ~250 words",
    ),
    "brief": ResponsePreset(key="brief", label="Brief", summary="1–3 sentences, ~20–60 words"),
    "standard": ResponsePreset(
        key="standard", label="Standard", summary="about a paragraph, ~60–150 words"
    ),
    "descriptive": ResponsePreset(
        key="descriptive", label="Descriptive", summary="2–4 paragraphs, ~150–300 words"
    ),
    "literary": ResponsePreset(
        key="literary", label="Literary", summary="several paragraphs, ~300–600 words"
    ),
    "custom": ResponsePreset(key="custom", label="Custom", summary="your own wording"),
}

PRESET_ORDER: tuple[ResponseStyle, ...] = (
    "adaptive",
    "brief",
    "standard",
    "descriptive",
    "literary",
    "custom",
)

# What a blank Custom falls back to. Sending no length instruction at all is
# the bug this module exists to fix, so "custom but empty" must not mean that.
FALLBACK_STYLE: ResponseStyle = "adaptive"


@dataclass(frozen=True)
class LengthInstruction:
    directive: str
    reminder: str
    # True when a per-turn choice differs from the story's standing default,
    # so the tail can say "for this passage only" rather than contradict it.
    is_override: bool = False


def instruction_for(
    style: ResponseStyle, custom_text: str | None = None, texts: PromptTexts = DEFAULT_TEXTS
) -> LengthInstruction:
    """The wording for one preset, or the author's own for Custom."""
    if style == "custom":
        text = (custom_text or "").strip().rstrip(".")
        if not text:
            return instruction_for(FALLBACK_STYLE, texts=texts)
        return LengthInstruction(
            directive=f"{text}.", reminder=texts.fill("length.custom.short", text=text)
        )
    return LengthInstruction(
        directive=texts[f"length.{style}"], reminder=texts[f"length.{style}.short"]
    )


def story_default(style: StyleDirectives, texts: PromptTexts = DEFAULT_TEXTS) -> LengthInstruction:
    """The standing instruction, for the system block."""
    return instruction_for(style.response_style, style.length_target, texts)


def for_turn(
    style: StyleDirectives,
    override: ResponseStyle | None = None,
    custom_text: str | None = None,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> LengthInstruction:
    """The instruction in force for one turn, for the tail and the reminder.

    Per-turn custom text wins, then a per-turn preset, then the story default.
    An "override" that resolves to the same words as the default isn't one.
    """
    default = story_default(style, texts)
    if custom_text and custom_text.strip():
        chosen = instruction_for("custom", custom_text, texts)
    elif override is not None:
        chosen = instruction_for(override, style.length_target, texts)
    else:
        return default
    if chosen.directive == default.directive:
        return default
    return LengthInstruction(chosen.directive, chosen.reminder, is_override=True)


def label_for(style: ResponseStyle) -> str:
    return PRESETS[style].label
