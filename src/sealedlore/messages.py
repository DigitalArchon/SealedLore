"""Provider-neutral prompt message primitives.

Prompt assembly produces these; the provider layer renders them to a wire
format (plain strings, or content blocks with cache_control markers — see
sealedlore.providers.wire). A `cache_breakpoint` part means "cache everything
up to and including this part".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Role = Literal["system", "user", "assistant"]

PART_JOINER = "\n\n"


@dataclass(frozen=True)
class ContentPart:
    text: str
    cache_breakpoint: bool = False
    # A picture sent after the part's text, as a data URL
    # ("data:image/jpeg;base64,…"), for a model that reads pictures: the
    # picture a story's style is taken from (`describe_picture_style`).
    # Never part of `PromptMessage.text`, and never in the API log
    # (`wire.without_pictures`).
    image: str = ""
    # Joined to the part before it with nothing between: the same text cut
    # in two, for a cache point between the pieces (Continue keeps what it
    # added in a part of its own, engine/continuation.py). Every other part
    # leads with PART_JOINER.
    glued: bool = False

    def lead(self, first: bool) -> str:
        """The part's text as it goes after the parts before it."""
        return self.text if first or self.glued else PART_JOINER + self.text


@dataclass(frozen=True)
class PromptMessage:
    role: Role
    parts: tuple[ContentPart, ...]

    @property
    def text(self) -> str:
        return "".join(part.lead(index == 0) for index, part in enumerate(self.parts))
