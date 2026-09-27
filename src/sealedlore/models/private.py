"""Private scenes: a span of the story played on a private model.

The author switches to a private model (local, or a nano-gpt TEE model) for a
scene the main provider never sees, then hands back only a summary they have
approved. The span's record is content-free — when it started and ended,
which model, where it hangs in the tree — so it is kept even when the scene's
own messages are kept in memory only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sealedlore.ids import new_id, utc_now_iso

PrivateKeep = Literal["disk", "memory"]
PrivateStatus = Literal["open", "closed", "discarded"]
PrivateRulesMode = Literal["compact", "full", "custom"]


class Attestation(BaseModel):
    """What a TEE check found when the scene began (providers/tee.py), or,
    for an end-to-end encrypted model, what the enclave proved before anything
    was sealed to its key (providers/private_mode.py)."""

    verified: bool = False
    signing_address: str | None = None
    nonce_ok: bool = False
    gpu_ok: bool | None = None
    checked_at: str = Field(default_factory=utc_now_iso)
    detail: str = ""
    # TEE/ models (providers/tee.py): "full" when everything the app can check
    # was checked and held; "partial" when nothing failed but something
    # couldn't be checked or isn't current, each named in `shortfalls`. A
    # refused attestation is never recorded as one: nothing is sent.
    level: Literal["full", "partial", ""] = ""
    shortfalls: list[str] = Field(default_factory=list)
    tcb_status: str = ""
    gpus: int = 0
    # Per-instance providers (Chutes): how many instances were each attested.
    instances: int = 0
    # End-to-end encrypted models only.
    encrypted: bool = False
    enclave: str = ""
    hardware: str = ""
    measurement: str = ""
    release_digest: str = ""
    release_tag: str = ""
    # The newest release, when the enclave runs an older one ("" otherwise,
    # or when it couldn't be looked up: `latest_checked`).
    latest_release: str = ""
    latest_checked: bool = False
    hpke_key_sha256: str = ""


class PrivateSpan(BaseModel):
    id: str = Field(default_factory=new_id)
    # The node the scene hangs from (the story's leaf when it began).
    start_parent_id: str | None = None
    started_at: str = Field(default_factory=utc_now_iso)
    ended_at: str | None = None
    model: str
    # The private endpoint's host, for the record ("localhost", "nano-gpt.com").
    host: str = ""
    keep: PrivateKeep = "memory"
    status: PrivateStatus = "open"
    summary_node_id: str | None = None
    # The story so far, condensed by the main model to fit the private model's
    # budget, and the last node it covers. Pre-scene material only.
    handoff: str | None = None
    handoff_through: str | None = None
    attestation: Attestation | None = None
    # A TEE model's summary, the one reply that joins the story: "verified",
    # "failed" or "unchecked", like `NodeMeta.tee` for the scene's passages.
    summary_tee: str | None = None
    # A scene that outgrows the private model's context is condensed by the
    # private model itself: a rolling summary of its oldest messages, and the
    # last message it covers. Scene text, so a memory-only span never writes
    # it to disk (`bundle_to_save` blanks it).
    scene_summary: str | None = None
    scene_summary_through: str | None = None


class PrivatePrompt(BaseModel):
    """How the private model is told to write, per story.

    Only the storytelling rules are the author's to change: the story's facts
    (world, cast, lore, scene, history) and the per-turn reminders (who the
    author holds, the roster, the length) are always sent, so a custom prompt
    can change how the model writes but not leave it without knowing who's
    who, or free to write the author's character.
    """

    mode: PrivateRulesMode = "compact"
    notes: str = ""
    custom: str = ""
