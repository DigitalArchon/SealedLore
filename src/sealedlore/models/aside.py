"""Out-of-character questions to the model (asides.json).

An aside is a conversation *about* the story, never part of it: it is shown in
the transcript but never sent as story history, never archived, and never
exported as prose. It hangs off the node that was the story's leaf when it was
asked, so it appears at that point and goes if that node is deleted.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.node import Usage


class Aside(BaseModel):
    id: str = Field(default_factory=new_id)
    # None when asked before the story had any messages.
    anchor_node_id: str | None = None
    question: str
    answer: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    model: str | None = None
    usage: Usage = Field(default_factory=Usage)
    request_log_ref: str | None = None
    # Asked in a private scene: only that scene's model ever sees it again,
    # wherever it is anchored (one asked before the scene's first message is
    # anchored to the story's last public passage).
    private_span: str | None = None
