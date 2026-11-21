"""Whom a story's {{user}} and {{char}} name (engine/placeholders.py)."""

from __future__ import annotations

from pydantic import BaseModel


class StoryPlaceholders(BaseModel):
    # {{user}}: the story's player, the "You are" of a card or lorebook it was
    # made from, else whom the author first plays. Fixed, whoever is played.
    user_id: str | None = None
    # {{char}} in anything not a character's own text: the card the story was
    # made from; with none, `char_name` (a world card's name), else blank.
    char_id: str | None = None
    char_name: str = ""
