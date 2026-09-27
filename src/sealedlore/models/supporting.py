"""Supporting characters: people the story introduced, kept for continuity (supporting.json).

Separate from the cast on purpose. The cast is what the roster, the
validators, the composer and the cached system block all iterate; a supporting
character is none of those things. It is a card the model is shown only when
the character has been mentioned recently, so that an invented ensign's name
and manner survive after the prose that established them is archived.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.character import Character


class CharacterSuggestion(BaseModel):
    """Someone the model found in the prose, waiting for the author's decision."""

    id: str = Field(default_factory=new_id)
    name: str
    aliases: list[str] = Field(default_factory=list)
    canon: str | None = None
    description: str = ""
    found_at: str = Field(default_factory=utc_now_iso)


class SupportingCast(BaseModel):
    characters: list[Character] = Field(default_factory=list)
    suggestions: list[CharacterSuggestion] = Field(default_factory=list)
    # Lowercased names the author said no to, so a later scan doesn't offer
    # them again.
    dismissed: list[str] = Field(default_factory=list)
