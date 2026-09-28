"""Story metadata, defaults, and style directives (story.json). See §2.1, §2.5, §8.2.

Generation-parameter and provider-selection fields are deliberately minimal here:
Phase 1 introduces the provider interface and Phase 8's full generation-control
surface, and extending a small model then beats guessing its shape now.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.character import Character
from sealedlore.models.generation import GenerationParams
from sealedlore.models.image import ImageRef
from sealedlore.models.node import AgencyMode, NpcScope, ResponseStyle
from sealedlore.models.plot import Plot
from sealedlore.models.private import Attestation, PrivatePrompt, PrivateSpan
from sealedlore.models.prompt_edit import PromptEdit
from sealedlore.models.route import ModelRoute
from sealedlore.models.scene import SceneLogEntry, SceneState

NarrativePerson = Literal["first", "second", "third"]
NarrativeTense = Literal["past", "present"]
# Human testing wanted both: some roleplays should show only what the author's
# character perceives; others want the wider story too, cutting to the
# command centre while the author's character hides in a maintenance corridor.
Perspective = Literal["whole_story", "with_character"]

# How much the world does on its own. Human testing found the prompt had no
# statement that it does anything at all: 847 words of rules, all constraint,
# and the one positive instruction about other characters was "show how those
# present react". So the model wrote well-behaved reactions and the story
# stopped moving between the author's turns.
WorldActivity = Literal["quiet", "normal", "eventful"]
DEFAULT_WORLD_ACTIVITY: WorldActivity = "normal"
# Keeps what the model was already doing: cutting away to the world's people.
DEFAULT_PERSPECTIVE: Perspective = "whole_story"

# Human testing: with no length instruction at all, a 29-word turn got a
# 363-word reply. Scaling to the author's turn is the default that fixes that
# without clipping the moments that deserve room.
DEFAULT_RESPONSE_STYLE: ResponseStyle = "adaptive"


class StyleDirectives(BaseModel):
    response_style: ResponseStyle = DEFAULT_RESPONSE_STYLE
    perspective: Perspective = DEFAULT_PERSPECTIVE
    # The author's own length wording, used when response_style is "custom".
    length_target: str | None = None
    person: NarrativePerson | None = None
    tense: NarrativeTense | None = None
    prose_density: str | None = None
    dialogue_narration_balance: str | None = None
    pacing: str | None = None
    content_rating: str | None = None
    content_limits: str | None = None
    forbidden_phrases: list[str] = Field(default_factory=list)
    # Anything else about how passages should read that no field above covers
    # ("let the narration dip into other characters' thoughts"). The settings
    # review needed somewhere to put such requests short of detaching the block.
    notes: str | None = None
    # Hand-edited override of the assembled style block; once set it takes
    # precedence over the structured fields above until the user resets it.
    custom_text: str | None = None
    detached: bool = False


class StoryDefaults(BaseModel):
    agency_mode: AgencyMode = "contested"
    npc_scope: NpcScope = "all"
    world_activity: WorldActivity = DEFAULT_WORLD_ACTIVITY
    # The author's testing (Sept 2026): Sonnet 4.5/4.6 storytelling degrades
    # quickly past ~60k tokens of context, so 60k is the ceiling as well as
    # the default. Existing stories keep what they were saved with.
    context_token_budget: int = 60_000
    main_model: str | None = None
    # A simple chat's own route for its model, chosen with it (New chat);
    # a story's roles take theirs from `Config.model_routes`.
    main_route: ModelRoute | None = None
    summarization_model: str | None = None
    generation: GenerationParams = Field(default_factory=GenerationParams)


OpeningMode = Literal["generate", "as_written"]
StoryMode = Literal["story", "chat"]
ChatKeep = Literal["disk", "memory"]


class StorySetup(BaseModel):
    """How a playthrough begins — what a scenario file carries besides cast and lore.

    Human testing put the whole premise into the first Director turn, where it
    would eventually be archived like any other message. The opening still
    becomes the first message, but it is kept here too, so the story can be
    restarted or exported without digging it out of the transcript.
    """

    description: str | None = None
    # "generate": the model writes the opening from these notes, sent as a
    # Director turn, so Regenerate gives a different one. "as_written": the
    # text is the opening passage itself, and no call is made.
    opening_text: str = ""
    opening_mode: OpeningMode = "generate"
    # Where a new playthrough starts. The live scene is `Story.scene`, which
    # moves on as the story does. Its cast roster is who the author pinned as
    # present; `absent_at_start` who they pinned as elsewhere. Every other
    # cast member is the opening's to place (the author's call, Sept 2026):
    # until the first passage the roster lists them as not placed yet.
    starting_scene: SceneState = Field(default_factory=SceneState)
    absent_at_start: list[str] = Field(default_factory=list)
    suggested_character_id: str | None = None
    # Play one of these; the others never enter the story ("John or Jane").
    # Whoever isn't picked is dropped from the cast when the story begins.
    choose_one_of: list[str] = Field(default_factory=list)
    # The ones not picked, off the cast but kept, so a restart or a scenario
    # export can offer the choice again.
    set_aside: list[Character] = Field(default_factory=list)


class Story(BaseModel):
    id: str = Field(default_factory=new_id)
    title: str
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    # Stable setting/world material for the system block (§6.1); never
    # turn-specific, or it would invalidate the cache prefix every turn.
    world_bible: str | None = None
    setup: StorySetup = Field(default_factory=StorySetup)
    defaults: StoryDefaults = Field(default_factory=StoryDefaults)
    style: StyleDirectives = Field(default_factory=StyleDirectives)
    # The scene at the active leaf: a copy of the nearest snapshot on the path
    # (NodeMeta.scene), kept here for the panels and the prompt.
    scene: SceneState = Field(default_factory=SceneState)
    # Scenes that have ended, oldest first (see SceneLogEntry).
    scene_log: list[SceneLogEntry] = Field(default_factory=list)
    active_leaf_id: str | None = None
    # The story's first line of branches (the rest are named on their first
    # message, NodeMeta.branch_name).
    main_branch_name: str = "Main"
    # Which character the author currently inhabits. Switchable at any point,
    # which is the whole thesis; kept here so it survives reopening the story.
    held_character_id: str | None = None
    # Facts and events a director runs (sealedlore.models.plot). None for a
    # story without one, which then behaves exactly as before.
    plot: Plot | None = None
    # The day each timed event falls on in this playthrough, drawn once from
    # its window (engine/chronicle.schedule). Shared by every branch; a
    # restart is a new story and draws again.
    plot_schedule: dict[str, int] = Field(default_factory=dict)
    # How pictures of this story should look ("painterly sci-fi concept art"),
    # given to whoever writes an image prompt. Never reaches the storyteller.
    image_style: str = ""
    # A picture shown behind the transcript, relative to the story folder.
    background_image: str | None = None
    # The pictures in the story's folder have had their hidden data removed
    # (storage.images.clean_story_images). False for a story made before
    # that was done on the way in, which is cleaned once when it next opens.
    images_cleaned: bool = False
    # Private scenes played in this story, oldest first (models/private.py),
    # and how the private model is told to write.
    private_spans: list[PrivateSpan] = Field(default_factory=list)
    private_prompt: PrivatePrompt = Field(default_factory=PrivatePrompt)
    # A simple chat (engine/chat.py): the author's own system prompt and a
    # plain conversation, none of the storytelling around it. Chosen when the
    # story is made and fixed.
    mode: StoryMode = "story"
    chat_prompt: str = ""
    # Fixed text after the user's message in every request, never in the
    # history: a style reminder, say (engine/chat.py). Recency is its point.
    chat_tail: str = ""
    # A chat on a TEE model: "memory" keeps it off the disk entirely, the log
    # included (the author: "a full incognito conversation"). Fixed at start.
    chat_keep: ChatKeep = "disk"
    # What the TEE check found when the chat was last opened.
    chat_attestation: Attestation | None = None
    # A chat's own reference pictures, added from disk (a story's are on its
    # cards and lore entries).
    reference_images: list[ImageRef] = Field(default_factory=list)
    # The author's edits to the program's prompt texts for this story, by key
    # (engine/prompt_texts.py); over the config's edits for every story.
    prompt_edits: dict[str, PromptEdit] = Field(default_factory=dict)

    @property
    def chat(self) -> bool:
        return self.mode == "chat"
