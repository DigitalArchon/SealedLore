"""Generation controls.

Reasoning text is stored on the node for display but never fed back into a
later prompt — see sealedlore.engine.prompt, which reads only Node.content.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# What a request asks for, as endpoints name it. "none" is DeepSeek's way of
# saying no reasoning at all; the rest run from least to most.
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
EFFORT_SCALE: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# What the author chooses (Settings → Generation), for the story model and for
# every other call: "least" is as little as the model allows, the rest map to
# the nearest level the model lists (engine/reasoning.py).
ReasoningLevel = Literal["least", "low", "medium", "high", "max"]
REASONING_LEVELS: tuple[str, ...] = ("least", "low", "medium", "high", "max")


class ReasoningConfig(BaseModel):
    """What one request sends: `enabled` with an effort, an effort of "none",
    or nothing at all (the default)."""

    enabled: bool = False
    effort: ReasoningEffort | None = None


class GenerationParams(BaseModel):
    temperature: float | None = None
    top_p: float | None = None
    # A ceiling, not the length: the style block sets that. It has to leave a
    # reasoning model room to think first — live, GLM 5.3 spent 1,246 of 1,500
    # on reasoning and the passage stopped mid-sentence.
    max_tokens: int | None = 4000
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    seed: int | None = None
    stop: list[str] = Field(default_factory=list)
    # Set per request from the author's reasoning levels (engine/reasoning.py),
    # never stored with the settings.
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)
