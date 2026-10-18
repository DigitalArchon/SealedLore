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
    # tile set designer is shown the author's pictures of their world. Never
    # part of `PromptMessage.text`, and never in the API log
    # (`wire.without_pictures`).
    image: str = ""


@dataclass(frozen=True)
class PromptMessage:
    role: Role
    parts: tuple[ContentPart, ...]

    @property
    def text(self) -> str:
        return PART_JOINER.join(part.text for part in self.parts)
