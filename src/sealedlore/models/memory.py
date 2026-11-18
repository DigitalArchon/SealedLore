"""What a character played by their own model remembers (the trial,
engine/reactions.py): their own account of a stretch of the story they saw,
older than the recent scenes their round sends whole.

A character's recollections are matched to the path as the story's chapters
are (`reactions.memory_chain`): by the messages they cover, in order, from the
first the character saw, so a branch, a retake or a deletion simply stops
matching where it differs, and the rest is written again for that line. A
stretch edited since it was remembered no longer matches its `fingerprint`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.node import Usage


class Recollection(BaseModel):
    id: str = Field(default_factory=new_id)
    character_id: str
    # The messages it covers, in path order: those the character saw, as
    # `reactions.seen_entries` lists them.
    covered_node_ids: list[str]
    # sha256 of those messages as the character saw them when it was written.
    fingerprint: str
    content: str
    model: str | None = None
    # Changed by the author (the character's prompt dialog): their words
    # stand, and are sent while the messages it covers are as they were.
    edited_at: str | None = None
    usage: Usage = Field(default_factory=Usage)
    created_at: str = Field(default_factory=utc_now_iso)
