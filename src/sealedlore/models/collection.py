"""A character kept in the author's own characters ("Your characters",
storage/collection.py), outside any story.

Versions of one character are cards of their own, each whole, grouped by the
main card's id: the author keeps "Elena" and "Elena, after the war" apart and
picks which one goes into a story.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from sealedlore.ids import utc_now_iso


class CollectionInfo(BaseModel):
    # A version's main card, by id; "" for a main card.
    family: str = ""
    # What this version is called ("After the war"); a main card has none.
    version: str = ""
    added_at: str = Field(default_factory=utc_now_iso)
    changed_at: str = Field(default_factory=utc_now_iso)
    # Where it came from, in words: a card's file or link, a story's title.
    came_from: str = ""


class PictureMark(BaseModel):
    """A picture as a story took it from a card, or gave it back: its bytes'
    sha256 (a story's copy has a file and an id of its own) and its labels."""

    sha256: str
    look: str = ""
    expression: str = ""
    caption: str = ""
    framing: str = ""


class CardLink(BaseModel):
    """A story's character brought in from the author's own characters
    (engine/card_link.py): which card, and each field and picture as the
    story last had it in step with the card, so a change made in the story
    and one made on the card since can be told apart."""

    card_id: str
    # The card's main card, and the version's name ("" for a main card).
    family: str = ""
    version: str = ""
    # Who `{{user}}` became in the story ("": left as written).
    user: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    pictures: list[PictureMark] = Field(default_factory=list)
    # The story's own changes the author chose to keep in it, by their
    # fingerprint: said quietly until there are others.
    kept_here: str = ""
    linked_at: str = Field(default_factory=utc_now_iso)
