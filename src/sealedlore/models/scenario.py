"""A scenario: everything needed to start a story, and nothing from playing it.

Not the story archive of §9, which is a full backup including the node tree,
summaries and API log (Phase 7). A scenario is the shareable base story:
world, opening, starting scene, style, cast and lore — the things an author
writes before the first turn. It leaves out anything a playthrough produces
and anything tied to the author's own setup (model, budget, provider).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import AgencyMode, NpcScope
from sealedlore.models.plot import Plot
from sealedlore.models.prompt_edit import PromptEdit
from sealedlore.models.scene import SceneState
from sealedlore.models.story import (
    DEFAULT_WORLD_ACTIVITY,
    OpeningMode,
    StoryMode,
    StyleDirectives,
    WorldActivity,
)

SCENARIO_FORMAT = "sealedlore-scenario"
SCENARIO_VERSION = 1


class Scenario(BaseModel):
    format: Literal["sealedlore-scenario"] = SCENARIO_FORMAT
    version: int = SCENARIO_VERSION
    title: str
    description: str | None = None
    world_bible: str | None = None
    opening_text: str = ""
    opening_mode: OpeningMode = "generate"
    starting_scene: SceneState = Field(default_factory=SceneState)
    # Cast pinned elsewhere at the start (StorySetup.absent_at_start).
    absent_at_start: list[str] = Field(default_factory=list)
    suggested_character_id: str | None = None
    choose_one_of: list[str] = Field(default_factory=list)
    style: StyleDirectives = Field(default_factory=StyleDirectives)
    agency_mode: AgencyMode = "contested"
    npc_scope: NpcScope = "all"
    world_activity: WorldActivity = DEFAULT_WORLD_ACTIVITY
    cast: list[Character] = Field(default_factory=list)
    lore: list[LoreEntry] = Field(default_factory=list)
    # Cards the story built up for people it introduced. Suggestions and
    # refusals belong to a playthrough and stay behind.
    supporting: list[Character] = Field(default_factory=list)
    # Facts and events for a director (sealedlore.models.plot). Only the
    # definition: what has happened belongs to a playthrough.
    plot: Plot | None = None
    # How pictures should look, and the reference pictures on cards and lore
    # (their `file`, relative to the story folder, to the file's bytes as
    # base64), so a shared scenario brings the author's ship with it.
    image_style: str = ""
    reference_files: dict[str, str] = Field(default_factory=dict)
    # A simple chat (engine/chat.py): its mode, system prompt and own
    # reference pictures.
    mode: StoryMode = "story"
    chat_prompt: str = ""
    chat_tail: str = ""
    reference_images: list[ImageRef] = Field(default_factory=list)
    # The story's own edits to the prompt texts: part of how it is told.
    prompt_edits: dict[str, PromptEdit] = Field(default_factory=dict)
