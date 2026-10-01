"""Chapter summaries (summaries.json).

Summaries are per-branch: `covered_node_ids` is the exact ordered run of nodes
along one path that this summary replaces, so a summary never silently applies
to a sibling branch it wasn't generated from.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.node import Usage


class Summary(BaseModel):
    id: str = Field(default_factory=new_id)
    covered_node_ids: list[str] = Field(default_factory=list)
    content: str
    model: str | None = None
    hand_edited: bool = False
    stale: bool = False
    created_at: str = Field(default_factory=utc_now_iso)
    edited_at: str | None = None
    # Summarising is a paid call like any other; recording its usage keeps the
    # story's real cost visible rather than hiding it between turns (§8.3).
    usage: Usage = Field(default_factory=Usage)
    # A part: the chapters it was merged from, in order (their ids, which stay
    # in summaries.json). Empty for a chapter. A part is derived from its
    # chapters: when one of its nodes is edited it is dropped, not rebuilt,
    # and the chapters stand in until the next merge.
    merged_from: list[str] = Field(default_factory=list)
    # The story ledger as it stood at the end of this summary's nodes
    # (engine/ledger.py). None while the ledger is off; a part carries its
    # last chapter's.
    ledger: str | None = None
    # The author's "Keep as written": never merged into a part. Merging goes
    # around it, as it goes around a chat's kept messages. Being hand-edited
    # alone doesn't keep a chapter out of a merge (the author's call).
    keep_as_written: bool = False

    @property
    def chapters(self) -> int:
        """How many chapters this stands for."""
        return len(self.merged_from) or 1
