"""The message tree."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.plot import Chronicle, Direction
from sealedlore.models.private import TeeUpstream
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
    # Which of their pictures each character is shown with after this node
    # (character id → picture id), kept the same way as `scene`: the
    # nearest above counts (engine/looks.py). None: unchanged here.
    looks: dict[str, str] | None = None
    # When the look chosen after this passage was only the nearest: the look
    # and expression that would have fitted, per character ("armour · angry"),
    # for the People page to offer to draw.
    looks_wanted: dict[str, str] = Field(default_factory=dict)
    # On a passage, for a lorebook too big to send whole: the entries the lore
    # model picked for the passage after it (`Config.lore_selector` "picker"),
    # read in the background. None = not picked; [] = picked, and none needed.
    lore_picks: list[str] | None = None
    # A story firing its lore as SillyTavern does (engine/tavern_firing.py):
    # what fired for this author turn, and the chat's length in messages
    # then, from which sticky, cooldown and delay are replayed along a path.
    # Recorded on every such turn, fired or not: SillyTavern's timed effects
    # move on each scan.
    tavern_fired: list[str] | None = None
    tavern_length: int | None = None
    # On an author's turn: what the plot's director told the storyteller
    # to do in the reply. Regenerate reuses it.
    direction: list[Direction] = Field(default_factory=list)
    # Written in a private scene (models/private.py): never sent to any model
    # but the private one, and kept only in memory if the scene says so.
    private_span: str | None = None
    # On the approved summary that stands in for a private scene.
    private_summary_of: str | None = None
    # A TEE private model's reply: how its check against the attested enclave
    # went (providers/tee.py `check_reply`: "verified", "signed", "failed", or
    # "unchecked" when no check could be made), or "encrypted": it came back
    # sealed by the attested enclave's key (providers/private_mode.py).
    # On the author's message: "encrypted" (sent sealed); "received" (a reply's
    # record, signed by the attested TEE/ key, shows it reached that enclave;
    # its text still passed nano-gpt's gateway); "unproven" (sent to a TEE/
    # model, and no reply's record shows the attested enclave received it).
    # "attested" is the mark before Nov 11 2026, set whatever the reply showed:
    # read by the reply shown with it (gui/transcript.py `sent_mark`).
    tee: (
        Literal[
            "verified",
            "signed",
            "failed",
            "unchecked",
            "encrypted",
            "received",
            "unproven",
            "attested",
        ]
        | None
    ) = None
    # A TEE/ reply's provider, by RedPill's record of its gateway's session
    # (Settings → Private, "Name the provider behind each TEE reply"): None
    # when not looked up.
    tee_upstream: TeeUpstream | None = None
    # Who sealed an "encrypted" message, when not NanoGPT's Private Mode (whose
    # marks always meant NanoGPT's): "tinfoil" (providers/privacy.py).
    tee_service: str | None = None
    # The first 8 characters of the instance an "encrypted" reply was sealed
    # to, where the service chooses one (Chutes): its tooltip names it.
    tee_instance: str | None = None
    # The first message of a branch carries its name (engine/branches.py).
    # A branch is made only by rewriting an earlier message; takes are not.
    branch_name: str | None = None
    # A simple chat's message the author keeps word for word: never folded
    # into a summary, but carried in full after the summary of its part.
    keep_full: bool = False
    # On a passage: models caught reasoning at length unasked while it was
    # written or read (engine/reasoning.py). The transcript says so under it,
    # since that is why the passage was slow to start.
    reasoning_caught: list[str] = Field(default_factory=list)
    # What went wrong while this message was sent or read: a read that
    # failed or was refused, chapters not written or not used, a lore pick
    # that failed... (engine/session.py `_kept_notice`). Said in the status
    # bar too, where the next notice replaced it; kept here, the transcript
    # shows it under the message.
    notes: list[str] = Field(default_factory=list)
    # On a passage written by Regenerate with a note: the note the author
    # gave for that take. For the author to see and the log to tell; no
    # request reads it, so a later take or turn never carries it.
    retake_note: str | None = None
    # On a take made by Change this passage…: the take it was written from
    # (its id) and what the author asked to change. As `retake_note`, read
    # by no request.
    revised_from: str | None = None
    revise_note: str | None = None
    # On a passage: whether its reply came back whole (the model stopped of
    # its own accord), or was stopped, cut off at the limit or left
    # unfinished (False); None for one written before this was kept, or by
    # no call. Continue starts a new paragraph after a whole reply
    # (engine/continuation.py `carried_start`).
    finished: bool | None = None
    # On a passage Continue has carried on: where in its text each
    # continuation began, and the sha256 of the text they were taken in
    # (`continuation.text_digest`). The next Continue sends the passage in
    # those pieces, so all but the newest are read from the cache; an edit
    # changes the text, and the offsets no longer count.
    continued_at: list[int] = Field(default_factory=list)
    continued_sha: str | None = None
    # On a passage: what each character played by their own model answered it
    # with, in the order they were asked (engine/reactions.py). Every reader
    # of the history sees them, rendered as that character's intention.
    reactions: list[Reaction] = Field(default_factory=list)
    # On an author's turn sent with the story's round after it ("after"):
    # its round ran, so `reactions` here (perhaps none) are what the
    # storyteller is given with it, never the passage before's.
    round_after: bool = False
    # On a passage the public record was kept after (engine/public_record.py):
    # the record as it stood after it, a snapshot as `scene` is (the record
    # anywhere is the nearest above), and the keeper's own notes of what was
    # said in confidence, which no model but the keeper is ever shown.
    public_record: list[str] | None = None
    public_notes: list[str] | None = None
    # The passage's text as the keeper read it (its sha256): carried on by
    # Continue or edited since, the passage is read again
    # (engine/session_record.py). None on one kept before this: taken as read.
    public_record_of: str | None = None
    # On a passage that moves while a character played by their own model is
    # in it at one end only (or that cuts away): the paragraphs (from 1) each
    # was there to see or hear, by character id, read by the scene model
    # (engine/reactions.py `apply_views`), for the text `seen_parts_of` holds
    # the sha256 of. Edited since, the whole passage counts again.
    seen_parts: dict[str, list[int]] | None = None
    seen_parts_of: str | None = None


class Reaction(BaseModel):
    """What a character played by their own model answered a passage with
    (`Character.own_model`, the trial): what that character thinks or wants
    to do next, given blind to the storyteller alongside the author's next
    turn, never taken as fact of what they did.

    Kept on the passage it answers — part of that take's record, so a retake
    starts with none and a branch keeps the ones its own passage drew.
    """

    character_id: str
    # Their name as the roster had it when the reaction was made, kept so the
    # prompt's rendering survives a card renamed since.
    name: str
    text: str
    model: str | None = None
    reasoning: str | None = None
    usage: Usage = Field(default_factory=Usage)
    created_at: str = Field(default_factory=utc_now_iso)
    # When the author last edited the text (their answer 4: edit a
    # character's response and redo the turn with the edited response).
    edited_at: str | None = None


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
