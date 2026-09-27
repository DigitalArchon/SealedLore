"""Generation controls.

Reasoning text is stored on the node for display but never fed back into a
later prompt — see sealedlore.engine.prompt, which reads only Node.content.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ReasoningEffort = Literal["low", "medium", "high"]


class ReasoningConfig(BaseModel):
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
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)
