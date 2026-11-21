"""A lorebook kept in the author's own Lorebooks (storage/lorebooks.py),
outside any story: a world's lore to read, change, export, and start any
number of stories in, with anyone from Your characters.

The book is kept as it came, in SillyTavern's own shape (entries keyed by
number) or a card's (entries in a list), and edited in place as a card's own
lorebook is (gui/lorebook_form.py, storage/tavern_lore.py), so it goes out
again with every setting SillyTavern reads, and a story takes from it what
any lorebook gives a story.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from sealedlore.ids import new_id, utc_now_iso


class StoredLorebook(BaseModel):
    id: str = Field(default_factory=new_id)
    # The name it is listed by. A SillyTavern lorebook file usually has none
    # of its own (its name is its file's), so it is kept here, not in `book`.
    name: str
    # Where it came from, in words: its file, a card's name, a story's title.
    came_from: str = ""
    added_at: str = Field(default_factory=utc_now_iso)
    changed_at: str = Field(default_factory=utc_now_iso)
    # What converting it from another app's lorebook lost, in words, kept to
    # say again when a story is made from it.
    converted: list[str] = Field(default_factory=list)
    book: dict[str, Any] = Field(default_factory=dict)
    # Shorter versions of its entries, beside them, never in the book (which
    # goes out again as it came): by the fingerprint of the text each was
    # written from (`models.lore.lore_fingerprint` of an entry's text, its
    # @@activate and the like taken off). A story made from the book takes
    # them along, and starts with the whole texts.
    condensed: dict[str, str] = Field(default_factory=dict)
