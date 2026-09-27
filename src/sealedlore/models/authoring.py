"""Settings reviews: the model's proposed changes to a story's setup.

A review never rewrites anything by itself. It returns changes, each a single
setting with its new value and the reason, and the author ticks the ones to
apply. What the review can't fix with settings it says so, in `cannot_fix`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from sealedlore.ids import new_id, utc_now_iso

ChangeKind = Literal[
    "style",  # a field of StyleDirectives
    "story",  # agency_mode, npc_scope, world_activity or context_token_budget
    "world",  # the world bible, whole
    "opening",  # the opening's text or mode, for new playthroughs
    "character",  # one field of a cast or supporting card, by name
    "character_add",  # a new card: target "cast" or "supporting"
    "character_move",  # a card between the cast and supporting: value "cast"/"supporting"
    "lore_add",
    "lore_edit",  # one field of an entry, by title
    "lore_disable",
    "generation",  # a Generation-tab parameter: temperature, max_tokens, reasoning…
    "model",  # main_model or summarization_model, by exact endpoint id
    # One of SealedLore's own prompt texts, for this story only: target is its
    # key (engine/prompt_texts.py), value the whole new text.
    "prompt",
]


class SettingsChange(BaseModel):
    id: str = Field(default_factory=new_id)
    kind: ChangeKind
    # A character's name or a lore entry's title; "cast" or "supporting" for
    # character_add. None for style, story, world and opening.
    target: str | None = None
    field: str | None = None
    value: Any = None
    reason: str = ""
    # What the setting held when the review was made, so a past review reads
    # right after it was applied ("Before"); None for lore_disable.
    before: Any = None
    # Something the author should see before ticking it: a replacement that
    # drops more than it adds, a skill list a supporting card can't keep. A
    # change with a warning starts unticked.
    warning: str | None = None


class AppFinding(BaseModel):
    """Something in SealedLore's own fixed prompt or behaviour that works
    against what the author wants: for whoever maintains the code, since no
    setting reaches it."""

    # The fixed text, quoted, or the part of the prompt it lives in.
    where: str = ""
    problem: str
    suggestion: str = ""


class SettingsReview(BaseModel):
    summary: str = ""
    changes: list[SettingsChange] = Field(default_factory=list)
    # Parts of the complaint no setting can address, and why — including
    # proposals that named something that doesn't exist.
    cannot_fix: list[str] = Field(default_factory=list)
    # Flaws in SealedLore itself the reviewer found while looking.
    app_findings: list[AppFinding] = Field(default_factory=list)
    # A Director turn for the author to send, when the cause is the story so
    # far rather than a setting. Never applied: the author sends it, or not.
    director_turn: str | None = None
    # The reviewer's judgement that the settings changes alone won't take
    # against the history the storyteller reads every turn.
    history_bound: bool = False


class ReviewRecord(BaseModel):
    """One review as it was made: the complaint, the answer, what was applied.

    Kept in `reviews.json` so a closed dialog isn't the end of it — the
    findings can still be copied, the complaint reused, and the changes seen.
    """

    id: str = Field(default_factory=new_id)
    at: str = Field(default_factory=utc_now_iso)
    model: str = ""
    complaint: str = ""
    review: SettingsReview = Field(default_factory=SettingsReview)
    # Ids of the changes the author applied; the Director turn as "director".
    applied_ids: list[str] = Field(default_factory=list)
    undone: bool = False
