"""Images: reference pictures on cards and lore, and pictures generated in play.

A reference image (`ImageRef`) belongs to a character or a lore entry, so the
author's ship or a character's face looks the same in every picture. A
generated image (`GeneratedImage`, images.json) hangs off the passage it
pictures, the way an aside does, but outlives it: deleting passages never
deletes images, since they cost money and aren't story state.

Files live under the story's folder (`images/`, `images/refs/`); a record
holds its path relative to that folder.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sealedlore.ids import new_id, utc_now_iso

# "story": a simple chat's own reference picture (Story.reference_images);
# "picture": a picture the story generated, used as a reference.
RefOwnerKind = Literal["character", "lore", "story", "picture"]


class ImageRef(BaseModel):
    id: str = Field(default_factory=new_id)
    # Relative to the story folder: images/refs/<id>.<ext>.
    file: str
    # What the picture shows ("front view", "in flight suit"), for the prompt
    # writer and the author.
    caption: str = ""


class RefUse(BaseModel):
    """One reference as sent, in order: image 1 is the first."""

    owner_kind: RefOwnerKind
    owner_id: str
    owner_name: str
    ref_id: str
    file: str
    caption: str = ""


class GeneratedImage(BaseModel):
    id: str = Field(default_factory=new_id)
    anchor_node_id: str | None = None
    file: str
    # Exactly the prompt the author approved and the model was sent.
    prompt: str
    direction: str = ""
    model: str
    size: str
    references: list[RefUse] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)
    # The whole request's cost split across its images; None when unknown.
    cost: float | None = None
    cost_reported: bool = False
    request_log_ref: str | None = None
    # Made in a private scene: its direction came from the scene, so it is
    # never offered to another model's prompt writer afterwards.
    private_span: str | None = None
