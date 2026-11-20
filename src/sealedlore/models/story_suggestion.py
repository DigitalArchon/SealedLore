"""Changes to the story's lore and cards that the story itself suggests
(engine/story_suggestions.py): what the prose has made part of the world, or
changed in it, waiting for the author to apply, change or turn down.

Kept on the story (story.json), never applied without the author."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sealedlore.ids import new_id, utc_now_iso

# A new lore entry; a lore entry's new text; words to add to a character's card.
SuggestionKind = Literal["lore_new", "lore_change", "card_change"]


class StorySuggestion(BaseModel):
    id: str = Field(default_factory=new_id)
    kind: SuggestionKind
    # The lore entry's or the character's id, for a change.
    target_id: str | None = None
    # The entry's title (new or as it stands), or the character's name.
    title: str = ""
    keywords: list[str] = Field(default_factory=list)
    # A lore entry's whole text (new, or as it would now read); for a card,
    # the words to add to the character's description.
    content: str = ""
    # What in the story it rests on, in a sentence.
    why: str = ""
    # The passages it was read from.
    node_ids: list[str] = Field(default_factory=list)
    found_at: str = Field(default_factory=utc_now_iso)
