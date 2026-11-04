"""A character kept in the author's own characters ("Your characters",
storage/collection.py), outside any story.

Versions of one character are cards of their own, each whole, grouped by the
main card's id: the author keeps "Elena" and "Elena, after the war" apart and
picks which one goes into a story.
"""

from __future__ import annotations

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
