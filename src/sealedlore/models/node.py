"""The message tree."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.plot import Chronicle, Direction
from sealedlore.models.scene import SceneShortcut, SceneState

NodeKind = Literal["user", "assistant"]
AgencyMode = Literal["fiat", "plausible", "contested", "dice"]
NpcScope = Literal["all", "selected", "model_choice"]
# How long, and how dense, the model's passage should be. The wording for
# each lives in sealedlore.engine.response_style.
ResponseStyle = Literal["adaptive", "brief", "standard", "descriptive", "literary", "custom"]
RollBand = Literal[
    "critical_success", "success", "success_at_a_cost", "failure", "critical_failure"
]

# The per-message difficulty hint for dice (§4.2). The table that turns it
# into a chance lives in sealedlore.engine.dice.
Difficulty = Literal["trivial", "easy", "standard", "hard", "desperate"]

NARRATOR_SPEAKER_ID = "__narrator__"
DIRECTOR_SPEAKER_ID = "__director__"
# Who writes a simple chat's own messages (engine/chat.py).
CHAT_USER_ID = "__user__"


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost: float = 0.0
    # True when the endpoint didn't report a cost and it was worked out from
    # the model's listed prices instead (sealedlore.engine.pricing).
    cost_estimated: bool = False
    # True when the endpoint stated the cost, even as zero: a subscription's
    # included model reports 0 and is free, not unpriced. Estimating it from
    # listed prices showed GLM 5.3 turns at ~1¢ on a flat-fee plan.
    cost_reported: bool = False


class Roll(BaseModel):
    die: int
    value: int
    band: RollBand
    # What the roll was made against, so the chip and the log can say why it
    # came out as it did. Optional: rolls recorded before Phase 6 lack them.
    target: int | None = None
    tier: str | None = None
    domain: str | None = None
    difficulty: Difficulty | None = None


class NodeMeta(BaseModel):
    model: str | None = None
    usage: Usage = Field(default_factory=Usage)
    reasoning: str | None = None
    agency_mode: AgencyMode | None = None
    roll: Roll | None = None
    npc_scope: NpcScope | None = None
    npc_scope_ids: list[str] = Field(default_factory=list)
    controlled_character_id: str | None = None
    # A per-turn length override, kept so regenerating the turn keeps it.
    # None means the story's default was in force.
    response_style: ResponseStyle | None = None
    length_hint: str | None = None
    # Dice (§4.2), on the author's turn: which skill and how hard, so Reroll
    # can roll again against the same odds.
    dice_domain: str | None = None
    difficulty: Difficulty | None = None
    request_log_ref: str | None = None
    # The scene after this node: who is here, where, and what changed. Set by
    # the scene read on a passage, or by the author on whatever node was the
    # leaf. The scene at any point is the nearest snapshot above it, so it
    # follows branches, retakes and deletions with no bookkeeping.
    scene: SceneState | None = None
    # A scene read was made on this passage, so presence was judged by it
    # rather than by the pattern checks in engine/validators.py.
    scene_read: bool = False
    # Who the read found brought into the scene by shortcut.
    scene_shortcuts: list[SceneShortcut] = Field(default_factory=list)
    # The plot's clock, facts and events after this node (models/plot.py),
    # kept the same way as `scene`. Only on stories with a plot.
    chronicle: Chronicle | None = None
    # On a passage, for a lorebook too big to send whole: the entries the lore
    # model picked for the passage after it (`Config.lore_selector` "picker"),
    # read in the background. None = not picked; [] = picked, and none needed.
    lore_picks: list[str] | None = None
    # On an author's turn: what the plot's director told the storyteller
    # to do in the reply. Regenerate reuses it.
    direction: list[Direction] = Field(default_factory=list)
    # Written in a private scene (models/private.py): never sent to any model
    # but the private one, and kept only in memory if the scene says so.
    private_span: str | None = None
    # On the approved summary that stands in for a private scene.
    private_summary_of: str | None = None
    # A TEE private model's reply: whether its signature checked out against
    # the attested enclave (providers/tee.py), or "encrypted": it came back
    # sealed by the attested enclave's key (providers/private_mode.py).
    # On the author's message: "encrypted" (sent sealed) or "attested" (sent to
    # the attested TEE/ enclave; its text still passed nano-gpt's gateway).
    tee: Literal["verified", "failed", "unchecked", "encrypted", "attested"] | None = None
    # The first message of a branch carries its name (engine/branches.py).
    # A branch is made only by rewriting an earlier message; takes are not.
    branch_name: str | None = None
    # A simple chat's message the author keeps word for word: never folded
    # into a summary, but carried in full after the summary of its part.
    keep_full: bool = False


class Node(BaseModel):
    id: str = Field(default_factory=new_id)
    parent_id: str | None = None
    children: list[str] = Field(default_factory=list)
    kind: NodeKind
    speaker_id: str
    content: str
    ooc: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    edited_at: str | None = None
    meta: NodeMeta = Field(default_factory=NodeMeta)
