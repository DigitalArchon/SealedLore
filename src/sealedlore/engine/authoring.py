"""Authoring with the model: a whole story from a premise, and a review of its settings.

Pure: prompts, parsing and applying changes. `StoryDraft` (engine.drafting)
and `StorySession.review_settings` make the calls.

Both halves speak the same shape — the settings a scenario file carries, with
characters named rather than identified by id, since the model can't know ids.
Generation fills that shape from nothing; a review proposes single changes to
it, and the author ticks the ones to apply.
"""

from __future__ import annotations

import copy
import difflib
import json
import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from typing import Any, get_args

from pydantic import ValidationError

from sealedlore.engine.catalog import ModelInfo
from sealedlore.engine.chronicle import chronicle_at, format_clock
from sealedlore.engine.dice import BAND_SHORT
from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_edits import missing_slots, set_text, texts_in_force
from sealedlore.engine.prompt_texts import (
    CHAPTERS,
    CHARACTERS,
    DEFAULT_TEXTS,
    LORE,
    PICTURES,
    PLOT,
    PRIVATE,
    QUESTIONS,
    SCENE_READ,
    STORYTELLER,
    TEXTS,
    TURN,
    PromptTexts,
)
from sealedlore.engine.response_style import label_for
from sealedlore.engine.rules import narration_reminder, render_history_node
from sealedlore.engine.scene_state import move_into_cast, move_out_of_cast, rename_in_scene
from sealedlore.engine.validators import mentions
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.authoring import AppFinding, SettingsChange, SettingsReview
from sealedlore.models.character import Character, Competence, CompetenceTier
from sealedlore.models.config import Config
from sealedlore.models.generation import REASONING_LEVELS, GenerationParams
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import AgencyMode, Node, NpcScope, ResponseStyle
from sealedlore.models.scenario import Scenario
from sealedlore.models.scene import OffstageCharacter, SceneState
from sealedlore.models.story import (
    NarrativePerson,
    NarrativeTense,
    OpeningMode,
    Perspective,
    StyleDirectives,
    WorldActivity,
)
from sealedlore.storage.repository import StoryBundle
from sealedlore.tree import active_path

TIERS: tuple[str, ...] = get_args(CompetenceTier)
RESPONSE_STYLES: tuple[str, ...] = get_args(ResponseStyle)
PERSPECTIVES: tuple[str, ...] = get_args(Perspective)
AGENCY_MODES: tuple[str, ...] = get_args(AgencyMode)
NPC_SCOPES: tuple[str, ...] = get_args(NpcScope)
# "selected" is a per-turn choice: the ids live on the turn, never on the
# story, so as a story default it renders "voice no named character" and
# mutes the cast until the author notices the composer's picker.
REVIEW_NPC_SCOPES: tuple[str, ...] = tuple(scope for scope in NPC_SCOPES if scope != "selected")
WORLD_ACTIVITIES: tuple[str, ...] = get_args(WorldActivity)
PERSONS: tuple[str, ...] = get_args(NarrativePerson)
TENSES: tuple[str, ...] = get_args(NarrativeTense)
OPENING_MODES: tuple[str, ...] = get_args(OpeningMode)
# The story model's reasoning levels (Settings → Generation), as the review
# may suggest one; "least" is as little as the model allows.
REASONING_EFFORTS: tuple[str, ...] = REASONING_LEVELS
# The length presets a model may pick by name. "custom" is never picked on its
# own: it means "use length_target", and without that wording it falls back to
# the default — live, a review proposed "custom" and put its wording where
# nothing read it, and the story's length reset to the default.
PRESET_STYLES: tuple[str, ...] = tuple(s for s in RESPONSE_STYLES if s != "custom")
# The style fields a hand-written (detached) style block still honours; every
# other field is replaced by the block and would change nothing.
LIVE_WHEN_DETACHED = ("response_style", "length_target", "perspective", "custom_text")

# A whole setup is long: a world of several hundred words, sheets, lore. The
# story's own max_tokens is tuned for a passage and would cut it off mid-JSON.
# Measured: Sonnet 4.6 used ~5,000 output tokens, Opus 5 ~12,400 — too close to
# an earlier 16,000 cap, and a cut-off draft is a paid failure. Only the tokens
# actually used are billed.
GENERATION_MAX_TOKENS = 32_000
# The answer allowance when the reviewer's own output limit isn't known. A
# review is sent with as many tokens as its model can write (the limit is a
# ceiling, only what is used is billed): a world rewrite plus two cards
# passed 8k, and a cut-off review is a paid failure.
REVIEW_MAX_TOKENS = 32_000
REVIEW_TEMPERATURE = 0.3
EVIDENCE_REPLIES = 3
# A replacement that drops this much of the current text, and this many lines,
# gets a warning and starts unticked: the reviewer was asked to keep what
# isn't part of the problem word for word, and nothing else checks.
LOSS_LINES = 3
LOSS_RATIO = 0.25
# The Config settings the reviewer is shown, read-only: they shape the story
# (what the storyteller remembers, who is recalled, which cards fit) but are
# the application's, not the story's, so a complaint they explain goes to
# cannot_fix naming the control.
APP_SETTINGS = (
    "lore_retrieval_k",
    "lore_query_turns",
    "lore_similarity_threshold",
    "lore_token_cap",
    "lore_whole_share",
    "lore_selector",
    "archive_chunk_turns",
    "summary_target_words",
    "archive_target_ratio",
    "supporting_recall_turns",
    "supporting_limit",
    "scene_reads",
    "scene_model",
    "plot_reads",
    "plot_model",
    "story_ledger",
    "suggest_characters",
    "cast_token_cap",
)


def _choices(values: Sequence[str]) -> str:
    return ", ".join(f'"{value}"' for value in values)


# Free-text style fields, and the ones restricted to a fixed set of values.
STYLE_TEXT_FIELDS = (
    "length_target",
    "prose_density",
    "dialogue_narration_balance",
    "pacing",
    "content_rating",
    "content_limits",
    "notes",
    "custom_text",
)
STYLE_CHOICE_FIELDS: dict[str, tuple[str, ...]] = {
    "response_style": RESPONSE_STYLES,
    "perspective": PERSPECTIVES,
    "person": PERSONS,
    "tense": TENSES,
}
STYLE_FIELDS = (*STYLE_CHOICE_FIELDS, *STYLE_TEXT_FIELDS, "forbidden_phrases")

# The names the model sees for a sheet's fields, and where each lives.
CHARACTER_FIELDS = (
    "name",
    "aliases",
    "canon",
    "summary",
    "description",
    "voice",
    "competence",
    "skills",
    "playable",
    "author_only",
)
LORE_FIELDS = ("title", "content", "keywords", "always_on", "enabled", "until_mentioned")
# The text fields a change replaces whole, checked for silent loss.
WHOLE_TEXT_FIELDS = {
    "world": (None,),
    "style": ("custom_text", "notes", "content_limits"),
    "character": ("description", "summary", "voice", "competence"),
    "lore_edit": ("content",),
    "opening": ("text",),
}

# Generation-tab parameters the review may suggest, with their allowed ranges.
# They are the author's settings for every story, so a review only suggests
# them: the author changes them by hand (Settings → Generation), and
# `apply_change` checks one without applying it.
GENERATION_RANGES: dict[str, tuple[float, float]] = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "presence_penalty": (-2.0, 2.0),
    "frequency_penalty": (-2.0, 2.0),
}
GENERATION_FIELDS = (*GENERATION_RANGES, "max_tokens", "reasoning")
# Below this a passage is cut off mid-sentence at any length setting.
MIN_MAX_TOKENS = 256
MAX_MAX_TOKENS = 200_000
MIN_CONTEXT_BUDGET = 4_000
MODEL_FIELDS = ("main_model", "summarization_model")


def _slots(texts: PromptTexts) -> dict[str, str]:
    """What the drafting and review prompts' slots stand for: the program's
    own lists of allowed values, so an edited prompt still names them right."""
    lists = {
        "tiers": TIERS,
        "style_fields": STYLE_FIELDS,
        "agency_modes": AGENCY_MODES,
        "npc_scopes": REVIEW_NPC_SCOPES,
        "world_activities": WORLD_ACTIVITIES,
        "opening_modes": OPENING_MODES,
        "character_fields": CHARACTER_FIELDS,
        "lore_fields": LORE_FIELDS,
        "generation_fields": GENERATION_FIELDS,
        "reasoning_efforts": REASONING_EFFORTS,
    }
    slots = {name: _choices(values) for name, values in lists.items()}
    slots["character_shape"] = texts["authoring.character_shape"]
    slots["style_guide"] = texts.fill(
        "authoring.style_guide",
        styles=_choices(PRESET_STYLES),
        persons=_choices(PERSONS),
        tenses=_choices(TENSES),
    )
    return slots


def draft_system(texts: PromptTexts = DEFAULT_TEXTS) -> str:
    slots = _slots(texts)
    return texts.fill(
        "authoring.draft",
        tiers=slots["tiers"],
        style_guide=slots["style_guide"],
        character_shape=slots["character_shape"],
    )


def review_system(detached: bool = False, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    system = texts.fill("authoring.review", **_slots(texts))
    return system + (texts["authoring.review.detached"] if detached else "")


def build_generation_messages(
    premise: str,
    *,
    person: NarrativePerson | None = None,
    tense: NarrativeTense | None = None,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """The draft call. A person or tense the author chose comes before the
    draft, so the opening is written in it (the author, Sept 2026: a
    second-person story drafted with a third-person opening)."""
    user = "The author's premise:\n\n" + premise.strip()
    if person or tense:
        who = {"first": "I", "second": "you"}.get(person or "")
        user += "\n\n" + texts.fill(
            "authoring.draft.voice",
            narration=narration_reminder(person, tense, None, texts),
            who=f', with the "play_as" character as "{who}"' if who else "",
        )
    return [
        PromptMessage(role="system", parts=(ContentPart(text=draft_system(texts)),)),
        PromptMessage(role="user", parts=(ContentPart(text=user),)),
    ]


# --- reading what the model wrote -------------------------------------------


def _text(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped and stripped.lower() != "null" else None
    return None


def _strings(value: object, *, commas: bool = False) -> list[str]:
    """A list the model may also give as one string, split on semicolons and
    lines. `commas` also splits on commas, as the Lore and Cast panels split
    keywords and aliases ("dock, harbour" is two); never for phrases or
    sentences, where "Well, well" is one."""
    if isinstance(value, str):
        if commas:
            value = value.replace(",", "\n")
        value = [part for part in value.replace(";", "\n").splitlines()]
    if not isinstance(value, list):
        return []
    return [text for text in (_text(item) for item in value) if text]


def _list(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _skills(value: object, who: str, warnings: list[str]) -> dict[str, CompetenceTier]:
    if not isinstance(value, dict):
        return {}
    skills: dict[str, CompetenceTier] = {}
    for domain, tier in value.items():
        name = str(domain).strip().lower()
        level = str(tier).strip().lower()
        if not name:
            continue
        if level in TIERS:
            skills[name] = level  # type: ignore[assignment]
        else:
            warnings.append(f"{who}: skipped skill “{name}” — “{tier}” isn't a tier")
    return skills


def character_from(item: dict[str, Any], warnings: list[str], *, playable: bool) -> Character:
    """A card from the model's character object. Raises ValueError without a name."""
    name = _text(item.get("name"))
    if not name:
        raise ValueError("a character had no name")
    raw_playable = item.get("playable")
    playable = raw_playable if isinstance(raw_playable, bool) else playable
    raw_author_only = item.get("author_only")
    return Character(
        name=name,
        aliases=[
            alias
            for alias in _strings(item.get("aliases"), commas=True)
            if alias.lower() != name.lower()
        ],
        canon=_text(item.get("canon")),
        summary=_text(item.get("summary")),
        full_description=_text(item.get("description")),
        voice_notes=_text(item.get("voice")),
        competence=Competence(
            notes=_text(item.get("competence")) or "",
            tiers=_skills(item.get("skills"), name, warnings),
        ),
        is_player_available=playable,
        # Only a playable character can be the author's alone: otherwise the
        # storyteller voices them, and nobody would be left to write them.
        author_only=playable and raw_author_only is True,
    )


def _choice(value: object, allowed: Sequence[str], label: str, warnings: list[str]) -> str | None:
    text = _text(value)
    if text is None:
        return None
    if text.lower() in allowed:
        return text.lower()
    warnings.append(f"ignored {label} “{text}” — not one of {', '.join(allowed)}")
    return None


def _style(value: object, warnings: list[str]) -> StyleDirectives:
    data = value if isinstance(value, dict) else {}
    style = StyleDirectives()
    for field, allowed in STYLE_CHOICE_FIELDS.items():
        chosen = _choice(data.get(field), allowed, field.replace("_", " "), warnings)
        if chosen is not None:
            setattr(style, field, chosen)
    for field in STYLE_TEXT_FIELDS:
        if field != "custom_text":
            setattr(style, field, _text(data.get(field)))
    style.forbidden_phrases = _strings(data.get("forbidden_phrases"))
    # A custom length is its wording: with it, it's custom; without, "custom"
    # would silently fall back to the default, so say so instead.
    if style.length_target:
        style.response_style = "custom"
    elif style.response_style == "custom":
        style.response_style = StyleDirectives().response_style
        warnings.append("a custom length came without its wording; using the default length")
    return style


def _name_index(characters: Sequence[Character]) -> dict[str, Character]:
    index: dict[str, Character] = {}
    for character in characters:
        for form in (character.name, *character.aliases):
            index.setdefault(form.strip().lower(), character)
    return index


def parse_generated(text: str) -> tuple[Scenario, list[str]]:
    """The setup from the model's reply, and what had to be dropped from it.

    Characters get fresh ids and every by-name reference is resolved to one.
    Anything unusable — a skill tier that doesn't exist, a scene naming someone
    who isn't in the cast — is left out and reported, not guessed at. Raises
    ValueError when there is no setup or no cast at all.
    """
    data = extract_json(text, dict, "story setup")
    warnings: list[str] = []

    cast: list[Character] = []
    for item in _list(data.get("cast")):
        try:
            cast.append(character_from(item, warnings, playable=True))
        except ValueError as exc:
            warnings.append(str(exc))
    if not cast:
        raise ValueError("the setup had no cast")
    supporting: list[Character] = []
    for item in _list(data.get("supporting")):
        try:
            card = character_from(item, warnings, playable=False)
        except ValueError as exc:
            warnings.append(str(exc))
            continue
        card.competence.tiers = {}
        supporting.append(card)

    by_name = _name_index(cast)

    def cast_id(name: object, where: str) -> str | None:
        wanted = _text(name)
        if wanted is None:
            return None
        found = by_name.get(wanted.lower())
        if found is None:
            warnings.append(f"{where}: “{wanted}” isn't in the cast")
        return found.id if found else None

    scene_data = data.get("starting_scene")
    scene_data = scene_data if isinstance(scene_data, dict) else {}
    present: list[str] = []
    for name in _strings(scene_data.get("present"), commas=True):
        found = cast_id(name, "starting scene")
        if found and found not in present:
            present.append(found)
    nearby = []
    for entry in _list(scene_data.get("nearby")):
        found = cast_id(entry.get("name"), "nearby")
        if found and found not in present:
            nearby.append(
                OffstageCharacter(character_id=found, note=_text(entry.get("note")) or "")
            )

    play_as = cast_id(data.get("play_as"), "play as")
    if play_as is not None and play_as not in present:
        # The author's own character must be in the scene they start in.
        present.insert(0, play_as)
        nearby = [entry for entry in nearby if entry.character_id != play_as]

    # The roster beats the narrative: a cast member the opening puts in the
    # scene but the roster calls elsewhere gets written back out. Mentioning
    # someone isn't proof they're present ("her master, back at the Temple"),
    # so say so rather than guess.
    opening = _text(data.get("opening")) or ""
    placed = set(present) | {entry.character_id for entry in nearby}
    for character in cast:
        if character.id not in placed and mentions(opening, character):
            warnings.append(
                f"the opening mentions {character.name}, who isn't in the starting scene — "
                "if they're there, tick them as present in Setup"
            )

    lore = []
    for item in _list(data.get("lore")):
        title, content = _text(item.get("title")), _text(item.get("content"))
        if title and content:
            lore.append(
                LoreEntry(
                    title=title,
                    content=content,
                    keywords=_strings(item.get("keywords"), commas=True),
                    until_mentioned=item.get("until_mentioned") is True,
                )
            )

    agency = _choice(data.get("agency_mode"), AGENCY_MODES, "agency mode", warnings)
    scenario = Scenario(
        title=_text(data.get("title")) or "Untitled story",
        description=_text(data.get("description")),
        world_bible=_text(data.get("world")),
        opening_text=opening,
        opening_mode="generate",
        starting_scene=SceneState(
            location=_text(scene_data.get("location")),
            time_of_day=_text(scene_data.get("time_of_day")),
            situation=_text(scene_data.get("situation")),
            present_character_ids=present,
            offstage_but_nearby=nearby,
        ),
        suggested_character_id=play_as,
        style=_style(data.get("style"), warnings),
        agency_mode=agency or "contested",  # type: ignore[arg-type]
        cast=cast,
        supporting=supporting,
        lore=lore,
    )
    return scenario, warnings


# --- the settings review ------------------------------------------------------


def character_view(character: Character) -> dict[str, Any]:
    return {
        "name": character.name,
        "aliases": character.aliases,
        "canon": character.canon,
        "summary": character.summary,
        "description": character.full_description,
        "voice": character.voice_notes,
        "competence": character.competence.notes or None,
        "skills": dict(character.competence.tiers),
        "playable": character.is_player_available,
        "author_only": character.author_only,
    }


def _name_of(bundle: StoryBundle, character_id: str | None) -> str | None:
    for character in (*bundle.cast, *bundle.supporting.characters):
        if character.id == character_id:
            return character.name
    return None


# Who plays or voices whom, in prose: "The author now plays John Carver.",
# "Jane Moss is a character you voice freely", "unless the author states
# them". An old review wrote such lines into the style notes before the roster
# said who voices whom (`rules.voicing`).
_VOICING_PROSE = re.compile(
    r"\b(?:the author|you)\b[^.!?\n]{0,40}\b(?:now )?(?:plays?|playing|controls?|holds?)\b"
    r"|\bcharacters? you voice\b|\bvoice (?:her|him|them|it)? ?freely\b"
    r"|\bunless the author (?:states|writes)\b|\bas (?:an? )?NPC\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")


def stale_voicing_notes(notes: str | None, cast: Sequence[Character]) -> list[str]:
    """Sentences of the style notes that say who plays or voices a cast member.

    They sit in the cached system block and never change, so the first time
    the author switches character they contradict the roster, which says the
    same thing correctly every turn (`rules.voicing`). The reviewer is asked to
    remove them, and the Prompt tab points them out.
    """
    if not notes:
        return []
    return [
        sentence.strip()
        for sentence in _SENTENCE_END.split(notes)
        if sentence.strip()
        and _VOICING_PROSE.search(sentence)
        and any(mentions(sentence, character) for character in cast)
    ]


def playthrough_view(
    bundle: StoryBundle,
    *,
    path: Sequence[Node] | None = None,
    cast_fits: bool | None = None,
) -> dict[str, Any]:
    """The state of the story, by name, for the reviewer: not settings, context.

    Who the author plays is named only in the prompt's tail, and the roster
    lives on the nodes; without this a review with no exchanges couldn't tell
    which character was the author's, or that the silent one is listed as
    elsewhere. `path` is the story as models may see it (private scenes left
    out); without it the count includes them. `cast_fits` is whether the cast's
    full descriptions fit under the cap on the next turn.
    """
    story = bundle.story
    scene = story.scene
    if path is None:
        path = active_path(bundle.nodes, story.active_leaf_id) if bundle.nodes else []
    plot: dict[str, Any] | None = None
    if story.plot is not None:
        chronicle = chronicle_at(path, story.plot)
        plot = {
            "clock": format_clock(chronicle.minutes),
            "place": chronicle.place,
            "facts": dict(chronicle.facts),
            "events_happened": [
                event.title
                for event in story.plot.events
                if (status := chronicle.events.get(event.id)) is not None
                and status.state == "happened"
            ],
        }
    return {
        "author_plays": _name_of(bundle, story.held_character_id),
        "stale_voicing_notes": stale_voicing_notes(story.style.notes, bundle.cast) or None,
        "messages_so_far": len(path),
        "chapters_archived": len(bundle.summaries),
        "cast_descriptions_sent": cast_fits,
        "plot": plot,
        "scene": {
            "location": scene.location,
            "time_of_day": scene.time_of_day,
            "situation": scene.situation,
            "present": [
                name for name in (_name_of(bundle, i) for i in scene.present_character_ids) if name
            ]
            + list(scene.present_others),
            "elsewhere": [
                {"name": _name_of(bundle, entry.character_id), "where": entry.note}
                for entry in scene.offstage_but_nearby
                if _name_of(bundle, entry.character_id)
            ],
            "privacy": scene.privacy,
            "kept_by": "the story's own scene reads" if scene.source == "story" else "the author",
            "everyone_tracked": scene.tracked,
        },
    }


def app_settings_view(config: Config) -> dict[str, Any]:
    """SealedLore's own settings that shape a story, read-only for the reviewer."""
    return {name: getattr(config, name) for name in APP_SETTINGS}


def settings_view(
    bundle: StoryBundle,
    *,
    model: str | None = None,
    facts: ModelInfo | None = None,
    config: Config | None = None,
    alternatives: Sequence[ModelInfo] = (),
    hidden_ids: Collection[str] = frozenset(),
    path: Sequence[Node] | None = None,
    cast_fits: bool | None = None,
) -> dict[str, Any]:
    """The settings as the reviewer sees them: by name, no ids. Cards and
    lore in `hidden_ids` (a plot's, not yet brought in) are left out: the
    reviewer's text reaches the author, and would spoil them.

    `model` is the storyteller's effective model (the story's own, or the
    provider's when the story names none); `facts` is what the endpoint lists
    for it — only it, never the whole list, which runs to hundreds of models.
    `alternatives` is a short list of the endpoint's other models, so a model
    suggestion can be an id that exists. `config` adds the application's own
    settings, read-only.
    """
    story = bundle.story
    params = config.generation if config is not None else GenerationParams()
    style = story.style.model_dump(exclude={"detached"})
    if not story.style.detached:
        style.pop("custom_text", None)
    return {
        "title": story.title,
        "world": story.world_bible,
        "style": style,
        "style_written_by_hand": story.style.detached,
        "agency_mode": story.defaults.agency_mode,
        "npc_scope": story.defaults.npc_scope,
        "world_activity": story.defaults.world_activity,
        "opening": {"text": story.setup.opening_text, "mode": story.setup.opening_mode},
        "model": {
            "main_model": story.defaults.main_model or model,
            "summarization_model": story.defaults.summarization_model,
            "context_token_budget": story.defaults.context_token_budget,
            # The author's, for every story: suggested, never applied.
            "generation": {
                "temperature": params.temperature,
                "top_p": params.top_p,
                "max_tokens": params.max_tokens,
                "presence_penalty": params.presence_penalty,
                "frequency_penalty": params.frequency_penalty,
                "reasoning": config.reasoning_story if config is not None else "least",
            },
            "endpoint_facts": facts.facts() if facts else None,
            "alternatives": [info.facts() for info in alternatives] or None,
        },
        "cast": [character_view(c) for c in bundle.cast if c.id not in hidden_ids],
        "supporting": [
            character_view(c) for c in bundle.supporting.characters if c.id not in hidden_ids
        ],
        "lore": [
            {
                "title": entry.title,
                "content": entry.content,
                "keywords": entry.keywords,
                "always_on": entry.always_on,
                "enabled": entry.enabled,
                "until_mentioned": entry.until_mentioned,
            }
            for entry in bundle.lore
            if entry.id not in hidden_ids
        ],
        "playthrough": playthrough_view(bundle, path=path, cast_fits=cast_fits),
        "app_settings": app_settings_view(config) if config is not None else None,
    }


def reply_nodes(path: Sequence[Node]) -> list[Node]:
    """The storyteller's replies on a path, oldest first."""
    return [node for node in path if node.kind == "assistant" and node.content.strip()]


def _turn_shape_lines(turn: Node, cast_by_id: dict[str, Character]) -> list[str]:
    """What the author chose for one turn, in words, so the reviewer can tell
    a one-off from a setting."""
    meta = turn.meta
    lines: list[str] = []
    if meta.response_style:
        lines.append(
            f"Length for this turn only: {label_for(meta.response_style)} (instead of the "
            "story's default)."
        )
    if meta.length_hint:
        lines.append(f"Length wording for this turn only: “{meta.length_hint}”.")
    if meta.npc_scope == "selected":
        names = [cast_by_id[i].name for i in meta.npc_scope_ids if i in cast_by_id]
        lines.append(
            "Voices this turn: only " + (", ".join(names) if names else "no named character") + "."
        )
    elif meta.npc_scope == "model_choice":
        lines.append("Voices this turn: the storyteller's choice.")
    if meta.direction:
        kinds = ", ".join(dict.fromkeys(str(getattr(d, "kind", "")) for d in meta.direction))
        lines.append(
            f"The plot's director gave the storyteller {len(meta.direction)} direction(s) "
            f"this turn ({kinds}): what it ordered is the plot's doing, not a setting."
        )
    if meta.agency_mode:
        lines.append(f"Outcome mode for this turn: {meta.agency_mode}.")
    if meta.roll is not None:
        roll = meta.roll
        who = cast_by_id.get(meta.controlled_character_id or "")
        against = f" against {roll.target}" if roll.target is not None else ""
        skill = f" at {roll.domain}" if roll.domain else ""
        lines.append(
            f"Dice: {who.name if who else 'the author'}{skill} rolled {roll.value}{against} — "
            f"{BAND_SHORT[roll.band]}. The storyteller was told to write that outcome."
        )
    return lines


def _flag_lines(reply: Node) -> str:
    """The shortcuts the scene read stood behind in a reply."""
    found = [
        f"{shortcut.name} {shortcut.kind.replace('_', ' ')}: “{shortcut.quote}”"
        for shortcut in reply.meta.scene_shortcuts
    ]
    if not found:
        return ""
    return "SealedLore flagged this reply: " + "; ".join(dict.fromkeys(found)) + "."


def render_exchange(
    path: Sequence[Node],
    reply: Node,
    cast: Sequence[Character],
    *,
    position: int | None = None,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    """A reply with the author's turn that prompted it: one is no evidence
    without the other (was it long for the turn, did it write what the author
    said their character did?). With it, what shaped the turn — a one-off
    length, the outcome mode, a dice result — and what was flagged in the
    reply, so the reviewer doesn't read a dictated failure as a settings
    problem. `position` is the reply's message number in the story, so a
    reason can name a passage the author can find."""
    cast_by_id = {character.id: character for character in cast}
    index = next(i for i, node in enumerate(path) if node.id == reply.id)
    parts: list[str] = []
    if position is not None:
        parts.append(f"(message {position} of the story)")
    if index > 0 and path[index - 1].kind == "user":
        turn = path[index - 1]
        shape = _turn_shape_lines(turn, cast_by_id)
        if shape:
            parts.append("\n".join(shape))
        parts.append("The author:\n" + render_history_node(turn, cast_by_id, texts))
    parts.append("The storyteller:\n" + reply.content.strip())
    flagged = _flag_lines(reply)
    if flagged:
        parts.append(flagged)
    return "\n\n".join(parts)


# The storyteller's texts are always offered to the review: they are what it
# reads. The rest are the side calls', sent only when the author asks (the
# author's choice, Sept 2026), since they are long and seldom the cause. A
# simple chat's and the review's own are never offered.
REVIEW_PROMPT_GROUPS = (STORYTELLER, TURN)
REVIEW_SIDE_GROUPS = (QUESTIONS, SCENE_READ, PLOT, CHAPTERS, CHARACTERS, LORE, PRIVATE, PICTURES)


def review_prompt_keys(*, side: bool = False) -> list[str]:
    """The prompt texts a review may change: the storyteller's, and with
    `side` the side calls' too."""
    groups = REVIEW_PROMPT_GROUPS + (REVIEW_SIDE_GROUPS if side else ())
    return [key for key, text in TEXTS.items() if text.group in groups]


def render_prompt_catalogue(
    keys: Sequence[str],
    texts: PromptTexts,
    *,
    story_edits: Collection[str] = (),
    app_edits: Collection[str] = (),
    shown: str = "",
) -> str:
    """The texts the review may change, by key: what each does, what fills
    its placeholders, whose edit is in force, and the text itself unless it
    already stands word for word in the prompt the review was sent."""
    blocks = []
    for key in keys:
        text = TEXTS[key]
        current = texts[key]
        lines = [f"## {key} — {text.title}", text.about]
        if text.placeholders:
            lines.append(
                "Filled in by SealedLore: "
                + "; ".join(f"{{{name}}} = {meaning}" for name, meaning in text.placeholders)
            )
        if key in story_edits:
            lines.append("The author has edited this for this story.")
        elif key in app_edits:
            lines.append("The author has edited this for every story.")
        if current.strip() and current in shown:
            lines.append("Text: as it stands in the prompt above.")
        else:
            lines.append(f"Text:\n<<<\n{current}\n>>>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_whole_prompt(messages: Sequence[PromptMessage]) -> str:
    """The storyteller's prompt as text, each message marked with its role."""
    return "\n\n".join(
        f"=== {message.role.upper()} MESSAGE ===\n{message.text.strip()}" for message in messages
    )


def build_review_messages(
    bundle: StoryBundle,
    *,
    complaint: str,
    system_prompt: str,
    evidence: Sequence[str] = (),
    messages_so_far: int = 0,
    model: str | None = None,
    facts: ModelInfo | None = None,
    whole_prompt: str = "",
    in_prompt: int = 0,
    tail: str = "",
    config: Config | None = None,
    alternatives: Sequence[ModelInfo] = (),
    hidden_ids: Collection[str] = frozenset(),
    path: Sequence[Node] | None = None,
    cast_fits: bool | None = None,
    texts: PromptTexts = DEFAULT_TEXTS,
    prompt_catalogue: str = "",
) -> list[PromptMessage]:
    """The review call.

    `tail` is the per-turn part of the prompt (roster, directives, reminder),
    sent after the system prompt: it is where most causes were found live,
    and the only place the held character is named. `whole_prompt` replaces
    both with everything the storyteller receives; `in_prompt` is how many of
    the most recent replies it already carries, so `evidence` (exchanges,
    oldest first) holds only older ones.
    """
    system = review_system(bundle.story.style.detached, texts)
    view = settings_view(
        bundle,
        model=model,
        facts=facts,
        config=config,
        alternatives=alternatives,
        hidden_ids=hidden_ids,
        path=path,
        cast_fits=cast_fits,
    )
    sections: list[str] = []
    if whole_prompt.strip():
        sections.append(
            "The whole prompt the storyteller will receive on its next turn (the "
            "author's turn itself is a placeholder):\n\n<<<\n" + whole_prompt.strip() + "\n>>>"
        )
    else:
        sections.append(
            "The system prompt the storyteller receives:\n\n<<<\n" + system_prompt.strip() + "\n>>>"
        )
        if tail.strip():
            sections.append(
                "The per-turn instructions the storyteller receives after the story, at the "
                "end of every turn (as they would stand on its next turn):\n\n<<<\n"
                + tail.strip()
                + "\n>>>"
            )
    if prompt_catalogue.strip():
        sections.append(
            "prompt_texts: SealedLore's own texts you may change for this story "
            '(kind "prompt"), by key:\n\n' + prompt_catalogue.strip()
        )
    body = [
        "The author's complaint:\n\n" + complaint.strip(),
        "The story's settings:\n\n" + json.dumps(view, indent=2, ensure_ascii=False),
        *sections,
        (
            f"The story has {messages_so_far} messages so far."
            if messages_so_far
            else "The story hasn't started yet."
        ),
    ]
    if evidence:
        already = (
            f" The {in_prompt} most recent replies are in the prompt above, so these are "
            "the ones before them."
            if in_prompt
            else ""
        )
        body.append(
            f"Exchanges from the story, oldest first — evidence only.{already}\n\n"
            + "\n\n".join(f"--- exchange ---\n{text.strip()}" for text in evidence)
        )
    return [
        PromptMessage(role="system", parts=(ContentPart(text=system),)),
        PromptMessage(role="user", parts=tuple(ContentPart(text=text) for text in body)),
    ]


# --- changes -------------------------------------------------------------------


def _find_character(bundle: StoryBundle, name: str | None, *, aliases: bool = True) -> Character:
    wanted = (name or "").strip().lower()
    for character in (*bundle.cast, *bundle.supporting.characters):
        if character.name.lower() == wanted:
            return character
        if aliases and wanted in (a.lower() for a in character.aliases):
            return character
    raise ValueError(f"there is no character called “{name}”")


def _all_scenes(bundle: StoryBundle) -> Iterator[SceneState]:
    yield bundle.story.scene
    yield bundle.story.setup.starting_scene
    for node in bundle.nodes:
        if node.meta.scene is not None:
            yield node.meta.scene


def _find_lore(bundle: StoryBundle, title: str | None) -> LoreEntry:
    wanted = (title or "").strip().lower()
    for entry in bundle.lore:
        if entry.title.strip().lower() == wanted:
            return entry
    raise ValueError(f"there is no lore entry titled “{title}”")


def _required_text(value: object, what: str) -> str:
    text = _text(value)
    if text is None:
        raise ValueError(f"{what} can't be empty")
    return text


# Person and tense are the story's voice: set before it begins, fixed after
# (the author, Sept 2026). A switch does take hold once the REMEMBER line
# carries it, but the story so far stays in the old voice: a transcript half in
# one person, chapters in another. A new telling is a new playthrough. One not
# set when the story began may be set once, to what it is written in.
FIXED_ONCE_STARTED = ("person", "tense")
FIXED_STYLE_NOTE = (
    "person and tense are fixed once a story has begun; to tell it another way, "
    "use Restart or Duplicate settings, which open Setup for a new playthrough"
)


def style_fixed(style: StyleDirectives, field: str, started: bool) -> bool:
    """Whether `field` can no longer change on this story."""
    return started and field in FIXED_ONCE_STARTED and getattr(style, field) is not None


def _apply_prompt(bundle: StoryBundle, key: str | None, value: object) -> None:
    """One of SealedLore's texts, for this story only (engine/prompt_texts.py)."""
    if key not in TEXTS:
        raise ValueError(f"SealedLore has no text “{key}”")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a prompt text must be the whole new text")
    set_text(bundle.story.prompt_edits, key, value)


def _apply_style(bundle: StoryBundle, field: str | None, value: object) -> None:
    style = bundle.story.style
    if field not in STYLE_FIELDS:
        raise ValueError(f"“{field}” isn't a style setting")
    if style_fixed(style, field, bool(bundle.nodes)):
        raise ValueError(FIXED_STYLE_NOTE)
    if style.detached and field not in LIVE_WHEN_DETACHED:
        raise ValueError(
            "the style block is written by hand, so this field wouldn't reach the "
            "storyteller — the hand-written block itself has to change"
        )
    if field == "length_target":
        text = _text(value)
        if text:
            # The wording is the custom length; setting it selects Custom.
            style.length_target = text
            style.response_style = "custom"
        elif style.response_style == "custom":
            raise ValueError(
                "clearing the custom length's wording would leave Custom with nothing to "
                "follow — choose a preset length instead"
            )
        else:
            style.length_target = None
        return
    if field == "response_style" and (_text(value) or "").lower() == "custom":
        if not style.length_target:
            raise ValueError("a custom length needs its wording — propose length_target with it")
        style.response_style = "custom"
        return
    if field == "custom_text":
        if not style.detached:
            raise ValueError("the style block isn't hand-written; change its fields instead")
        style.custom_text = _required_text(value, "the style block")
        return
    if field == "forbidden_phrases":
        style.forbidden_phrases = _strings(value)
        return
    if field in STYLE_CHOICE_FIELDS:
        text = _text(value)
        allowed = STYLE_CHOICE_FIELDS[field]
        if text is not None and text.lower() not in allowed:
            raise ValueError(f"“{text}” isn't one of {', '.join(allowed)}")
        if text is None and field in ("response_style", "perspective"):
            raise ValueError(f"{field.replace('_', ' ')} can't be cleared")
        setattr(style, field, text.lower() if text else None)
        return
    setattr(style, field, _text(value))


SUPPORTING_ONLY_FIELDS = ("name", "aliases", "canon", "summary", "voice")


def _apply_character(bundle: StoryBundle, change: SettingsChange) -> None:
    character = _find_character(bundle, change.target)
    field, value = change.field, change.value
    supporting = any(card is character for card in bundle.supporting.characters)
    if supporting and field not in SUPPORTING_ONLY_FIELDS:
        # The supporting block sends only the summary and voice; skills and
        # playability belong to the cast. Applying the rest changed nothing
        # the storyteller could see.
        raise ValueError(
            f"{character.name} is a supporting card: it carries only a name, aliases, "
            "canon, summary and voice (move them to the cast for the rest)"
        )
    if field == "name":
        new_name = _required_text(value, "a name")
        try:
            other = _find_character(bundle, new_name, aliases=False)
        except ValueError:
            other = None
        if other is not None and other is not character:
            raise ValueError(f"there is already a character called “{new_name}”")
        # Supporting cards are on rosters by name; the new name takes their place.
        for scene in _all_scenes(bundle):
            rename_in_scene(scene, character, new_name)
        character.name = new_name
    elif field == "aliases":
        character.aliases = _strings(value, commas=True)
    elif field == "canon":
        character.canon = _text(value)
    elif field == "summary":
        character.summary = _text(value)
    elif field == "description":
        character.full_description = _text(value)
    elif field == "voice":
        character.voice_notes = _text(value)
    elif field == "competence":
        character.competence.notes = _text(value) or ""
    elif field == "skills":
        warnings: list[str] = []
        tiers = _skills(value, character.name, warnings)
        if warnings:
            raise ValueError("; ".join(warnings))
        character.competence.tiers = tiers
    elif field == "playable":
        if not isinstance(value, bool):
            raise ValueError("playable must be true or false")
        if not value and character.id == bundle.story.held_character_id:
            # The composer lists only playable characters: this would drop the
            # author to "Nobody" and let the storyteller write their character.
            raise ValueError(
                f"the author is playing {character.name}; they can be made the "
                "storyteller's only once the author plays someone else"
            )
        character.is_player_available = value
        if not value:
            character.author_only = False
    elif field == "author_only":
        if not isinstance(value, bool):
            raise ValueError("author_only must be true or false")
        if value and not character.is_player_available:
            raise ValueError(
                f"{character.name} isn't available to play, so the storyteller already "
                "voices them; author_only would leave nobody to write them"
            )
        character.author_only = value
    else:
        raise ValueError(f"“{field}” isn't a character setting")


def _apply_lore_edit(entry: LoreEntry, field: str | None, value: object) -> None:
    if field == "title":
        entry.title = _required_text(value, "a title")
    elif field == "content":
        entry.content = _required_text(value, "an entry")
    elif field == "keywords":
        entry.keywords = _strings(value, commas=True)
    elif field == "always_on":
        if not isinstance(value, bool):
            raise ValueError("always_on must be true or false")
        entry.always_on = value
    elif field == "enabled":
        if not isinstance(value, bool):
            raise ValueError("enabled must be true or false")
        entry.enabled = value
        return
    elif field == "until_mentioned":
        if not isinstance(value, bool):
            raise ValueError("until_mentioned must be true or false")
        entry.until_mentioned = value
        return
    else:
        raise ValueError(f"“{field}” isn't a lore setting")
    # Changed text means a changed vector.
    entry.embedding_hash = None


def _number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            value = float(str(value))
        except ValueError:
            raise ValueError(f"{what} must be a number") from None
    return float(value)


def _check_generation(field: str | None, value: object, facts: ModelInfo | None) -> None:
    """Whether a suggested generation setting is one the author could make:
    raises ValueError if not. Nothing is changed: these are the author's
    settings for every story, changed by hand."""
    params = GenerationParams()
    if field in GENERATION_RANGES:
        if value is None:
            return
        low, high = GENERATION_RANGES[field]
        number = _number(value, field.replace("_", " "))
        if not low <= number <= high:
            raise ValueError(f"{field.replace('_', ' ')} must be between {low:g} and {high:g}")
        setattr(params, field, number)
    elif field == "max_tokens":
        if value is None:
            return
        number = int(_number(value, "max tokens"))
        ceiling = (facts.max_output_tokens if facts else None) or MAX_MAX_TOKENS
        if number < MIN_MAX_TOKENS:
            raise ValueError(
                f"max tokens below {MIN_MAX_TOKENS} cuts passages off mid-sentence; length "
                "is set by the style's length settings"
            )
        if number > ceiling:
            raise ValueError(f"the model writes at most {ceiling:,} tokens per reply")
    elif field == "reasoning":
        text = _text(value)
        if text is None or text.lower() not in REASONING_EFFORTS:
            raise ValueError(f"reasoning must be one of {', '.join(REASONING_EFFORTS)}")
    else:
        raise ValueError(f"“{field}” isn't a generation setting")


def _apply_model(
    bundle: StoryBundle, field: str | None, value: object, models: Mapping[str, ModelInfo] | None
) -> None:
    if field not in MODEL_FIELDS:
        raise ValueError(f"“{field}” isn't a model setting")
    model_id = _text(value)
    if model_id is None:
        if field == "main_model":
            raise ValueError("the storyteller needs a model")
        bundle.story.defaults.summarization_model = None
        return
    if models is not None and model_id not in models:
        raise ValueError(f"your endpoint doesn't offer a model called “{model_id}”")
    setattr(bundle.story.defaults, field, model_id)


def apply_change(
    bundle: StoryBundle,
    change: SettingsChange,
    *,
    models: Mapping[str, ModelInfo] | None = None,
    current_model: str | None = None,
) -> None:
    """Apply one change in place. Raises ValueError, in plain words, if it can't.

    `models` is the endpoint's list; with it, a model id must be on it and
    token limits are checked against the current model's. Without it those
    checks are skipped — `parse_review` has already made them.

    A change that applies with a reservation — a new card whose skills had to
    be dropped — gets it written on `change.warning`, for the author to see.
    """
    kind, field, value = change.kind, change.field, change.value
    story = bundle.story
    model_id = story.defaults.main_model or current_model
    facts = models.get(model_id) if models and model_id else None
    if kind == "style":
        _apply_style(bundle, field, value)
    elif kind == "prompt":
        _apply_prompt(bundle, change.target, value)
    elif kind == "generation":
        _check_generation(field, value, facts)
    elif kind == "model":
        _apply_model(bundle, field, value, models)
    elif kind == "story" and field == "npc_scope" and (_text(value) or "").lower() == "selected":
        raise ValueError(
            "“selected” is chosen per turn in the composer, with the characters to voice; "
            "as a story setting it would voice no named character at all"
        )
    elif kind == "story" and field == "context_token_budget":
        number = int(_number(value, "the context budget"))
        ceiling = facts.context_length if facts and facts.context_length else None
        if number < MIN_CONTEXT_BUDGET:
            raise ValueError(f"a context budget below {MIN_CONTEXT_BUDGET:,} can't hold a turn")
        if ceiling and number > ceiling:
            raise ValueError(f"the model reads at most {ceiling:,} tokens")
        story.defaults.context_token_budget = number
    elif kind == "story":
        allowed = {
            "agency_mode": AGENCY_MODES,
            "npc_scope": NPC_SCOPES,
            "world_activity": WORLD_ACTIVITIES,
        }.get(field or "")
        text = (_text(value) or "").lower()
        if allowed is None:
            raise ValueError(f"“{field}” isn't a story setting")
        if text not in allowed:
            raise ValueError(f"“{value}” isn't one of {', '.join(allowed)}")
        setattr(story.defaults, field, text)  # type: ignore[arg-type]
    elif kind == "world":
        story.world_bible = _text(value)
    elif kind == "opening":
        if field == "text":
            story.setup.opening_text = _text(value) or ""
        elif field == "mode":
            text = (_text(value) or "").lower()
            if text not in OPENING_MODES:
                raise ValueError(f"“{value}” isn't one of {', '.join(OPENING_MODES)}")
            story.setup.opening_mode = text  # type: ignore[assignment]
        else:
            raise ValueError(f"“{field}” isn't an opening setting")
    elif kind == "character":
        _apply_character(bundle, change)
    elif kind == "character_add":
        if change.target not in ("cast", "supporting"):
            raise ValueError("a new character goes in the cast or supporting")
        if not isinstance(value, dict):
            raise ValueError("the new character had no sheet")
        warnings: list[str] = []
        card = character_from(value, warnings, playable=change.target == "cast")
        try:
            # By name only: a new "Hollis" beside a card that merely answers to
            # "Hollis" as an alias is the author's call, not a duplicate.
            _find_character(bundle, card.name, aliases=False)
        except ValueError:
            pass
        else:
            raise ValueError(f"there is already a character called “{card.name}”")
        if change.target == "cast":
            bundle.cast.append(card)
        else:
            if card.competence.tiers:
                warnings.append("its skills were dropped: supporting cards have none")
            card.competence.tiers = {}
            card.is_player_available = False
            card.author_only = False
            bundle.supporting.characters.append(card)
        if warnings:
            change.warning = "; ".join(warnings)
    elif kind == "character_move":
        character = _find_character(bundle, change.target)
        where = (_text(value) or "").lower()
        if where not in ("cast", "supporting"):
            raise ValueError("a character moves to the cast or to supporting")
        in_cast = character in bundle.cast
        if where == "cast" and not in_cast:
            bundle.supporting.characters.remove(character)
            bundle.cast.append(character)
            for scene in _all_scenes(bundle):
                move_into_cast(scene, character)
        elif where == "supporting" and in_cast:
            if character.id == story.held_character_id:
                raise ValueError(f"the author is holding {character.name}")
            bundle.cast.remove(character)
            for scene in _all_scenes(bundle):
                move_out_of_cast(scene, character)
            character.competence.tiers = {}
            character.is_player_available = False
            character.author_only = False
            bundle.supporting.characters.append(character)
        else:
            raise ValueError(f"{character.name} is already {where}")
    elif kind == "lore_add":
        if not isinstance(value, dict):
            raise ValueError("the new lore entry had no content")
        title = _required_text(value.get("title"), "a title")
        if any(entry.title.strip().lower() == title.lower() for entry in bundle.lore):
            raise ValueError(f"there is already a lore entry titled “{title}”")
        bundle.lore.append(
            LoreEntry(
                title=title,
                content=_required_text(value.get("content"), "an entry"),
                keywords=_strings(value.get("keywords"), commas=True),
                always_on=value.get("always_on") is True,
                until_mentioned=value.get("until_mentioned") is True,
            )
        )
    elif kind == "lore_edit":
        entry = _find_lore(bundle, change.target)
        if field == "title":
            wanted = (_text(value) or "").lower()
            if any(e is not entry and e.title.strip().lower() == wanted for e in bundle.lore):
                raise ValueError(f"there is already a lore entry titled “{_text(value)}”")
        _apply_lore_edit(entry, field, value)
    elif kind == "lore_disable":
        _find_lore(bundle, change.target).enabled = False
    else:  # pragma: no cover - the model's Literal rules this out
        raise ValueError(f"unknown kind of change “{kind}”")


def _is_rename(change: SettingsChange) -> bool:
    return (change.kind == "character" and change.field == "name") or (
        change.kind == "lore_edit" and change.field == "title"
    )


def ordered_for_apply(changes: Sequence[SettingsChange]) -> list[SettingsChange]:
    """The order to apply a review's changes in: renames last.

    Targets are names and titles, so an edit that follows a rename of the same
    card would look for a name that no longer exists. Applied after everything
    else, a rename can strand nothing.
    """
    return [c for c in changes if not _is_rename(c)] + [c for c in changes if _is_rename(c)]


# The Style tab's own names for its fields, so a proposal reads like the form.
_FIELD_LABELS = {
    "response_style": "length",
    "length_target": "custom length",
    "dialogue_narration_balance": "dialogue / narration",
    "forbidden_phrases": "avoid",
    "notes": "other notes",
    "custom_text": "hand-written style block",
    "agency_mode": "outcomes",
    "npc_scope": "who the model may voice",
    "world_activity": "how much the world does on its own",
    "always_on": "always on",
    "enabled": "on",
    "until_mentioned": "held back until you mention it",
    "playable": "available to play",
    "author_only": "never voiced by the storyteller",
    "context_token_budget": "context budget",
    "main_model": "storyteller",
    "summarization_model": "summariser",
    "max_tokens": "max tokens",
    "top_p": "top p",
    "reasoning_effort": "reasoning effort",
}


def describe(change: SettingsChange) -> str:
    """What a change touches, for a list row: "Style › pacing", "Jane Moss › voice"."""
    field = _FIELD_LABELS.get(change.field or "", (change.field or "").replace("_", " "))
    return {
        "style": f"Style › {field}",
        "story": f"Story › {field}",
        "world": "World",
        "opening": f"Opening › {field}",
        "character": f"{change.target} › {field}",
        "character_add": f"New {'cast member' if change.target == 'cast' else 'supporting card'}",
        "character_move": (
            f"{change.target} › {'into the cast' if change.value == 'cast' else 'to supporting'}"
        ),
        "lore_add": "New lore entry",
        "lore_edit": f"Lore › {change.target} › {field}",
        "lore_disable": f"Lore › {change.target} › turn off",
        "generation": f"Generation › {field}",
        "model": f"Model › {field}",
        "prompt": "Prompt › "
        + (TEXTS[change.target].title if change.target in TEXTS else str(change.target)),
    }[change.kind]


def format_value(value: object) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return "(not set)"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        if "name" in value or "title" in value:
            return json.dumps(value, indent=2, ensure_ascii=False)
        return ", ".join(f"{key}: {item}" for key, item in value.items())
    return str(value)


def current_value(
    bundle: StoryBundle,
    change: SettingsChange,
    texts: PromptTexts | None = None,
    config: Config | None = None,
) -> object:
    """What the setting holds now, for the before/after view. For a prompt
    text, the one in force: `texts` when given (the edits for every story
    included), else this story's edits over the defaults. A generation
    setting is the author's for every story, in `config`."""
    story = bundle.story
    if change.kind == "prompt":
        if change.target not in TEXTS:
            return None
        return (texts or texts_in_force({}, story.prompt_edits))[change.target]
    if change.kind == "style":
        return getattr(story.style, change.field or "", None)
    if change.kind in ("story", "model"):
        return getattr(story.defaults, change.field or "", None)
    if change.kind == "generation":
        if config is None:
            return None
        if change.field == "reasoning":
            return config.reasoning_story
        return getattr(config.generation, change.field or "", None)
    if change.kind == "world":
        return story.world_bible
    if change.kind == "opening":
        return story.setup.opening_text if change.field == "text" else story.setup.opening_mode
    if change.kind == "character":
        try:
            return character_view(_find_character(bundle, change.target)).get(change.field or "")
        except ValueError:
            return None
    if change.kind == "character_move":
        try:
            character = _find_character(bundle, change.target)
        except ValueError:
            return None
        return "cast" if character in bundle.cast else "supporting"
    if change.kind in ("lore_edit", "lore_disable"):
        try:
            entry = _find_lore(bundle, change.target)
        except ValueError:
            return None
        return (
            entry.enabled if change.kind == "lore_disable" else getattr(entry, change.field or "")
        )
    return None


_WORDING_KEYS = ("length_target", "length", "custom_length", "wording")


def _custom_length_wording(item: Mapping[str, Any]) -> str | None:
    """The wording a "custom length" proposal carries, wherever the model put it."""
    for key in _WORDING_KEYS:
        text = _text(item.get(key))
        if text:
            return text
    value = item.get("value")
    if isinstance(value, Mapping):
        for key in _WORDING_KEYS:
            text = _text(value.get(key))
            if text:
                return text
    return None


def _is_custom_length(item: Mapping[str, Any]) -> bool:
    if item.get("kind") != "style" or item.get("field") != "response_style":
        return False
    value = item.get("value")
    if isinstance(value, Mapping):
        value = value.get("response_style", value.get("value"))
    return (_text(value) or "").lower() == "custom"


def _normalise_length(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make every custom length one `length_target` change.

    A custom length is two settings — the choice and its wording — and a
    change to either alone does nothing (a blank Custom falls back to the
    default). So a "custom" proposal takes its wording along, from wherever
    the model put it, or is folded into the separate wording change beside
    it. Either way the author sees, and ticks, one row.
    """
    has_wording = any(
        item.get("kind") == "style"
        and item.get("field") == "length_target"
        and _text(item.get("value"))
        for item in items
    )
    result: list[dict[str, Any]] = []
    for item in items:
        if not _is_custom_length(item):
            result.append(item)
            continue
        wording = _custom_length_wording(item)
        if wording:
            result.append({**item, "field": "length_target", "value": wording})
        elif not has_wording:
            result.append(item)  # left to fail with its explanation
        # else: the separate wording change carries it
    return result


def _findings(value: object) -> list[AppFinding]:
    findings: list[AppFinding] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str) and _text(item):
            findings.append(AppFinding(problem=item.strip()))
        elif isinstance(item, dict) and _text(item.get("problem")):
            findings.append(
                AppFinding(
                    where=_text(item.get("where")) or "",
                    problem=_text(item.get("problem")) or "",
                    suggestion=_text(item.get("suggestion")) or "",
                )
            )
    return findings


def findings_markdown(review: SettingsReview, *, complaint: str = "", model: str = "") -> str:
    """The findings about SealedLore, ready to paste into an issue or a chat."""
    lines = ["# SealedLore findings from a settings review", ""]
    if model:
        lines.append(f"Reviewer: {model}")
    if complaint:
        lines.append(f"Author's complaint: {complaint.strip()}")
    for number, finding in enumerate(review.app_findings, start=1):
        lines.extend(["", f"## {number}. {finding.problem}"])
        if finding.where:
            lines.extend(["", "Where:", "", *[f"> {line}" for line in finding.where.splitlines()]])
        if finding.suggestion:
            lines.extend(["", f"Suggestion: {finding.suggestion}"])
    return "\n".join(lines).strip() + "\n"


def review_markdown(
    review: SettingsReview,
    bundle: StoryBundle | None = None,
    *,
    complaint: str = "",
    model: str = "",
    applied_ids: Sequence[str] = (),
) -> str:
    """The whole review as Markdown: summary, changes, what can't be fixed,
    the Director turn and the findings. With `bundle`, each change shows what
    it replaces; `applied_ids` marks the ones the author applied."""
    lines = ["# Settings review", ""]
    if model:
        lines.append(f"Reviewer: {model}")
    if complaint:
        lines.append(f"Author's complaint: {complaint.strip()}")
    if review.summary:
        lines.extend(["", review.summary.strip()])
    if review.changes:
        lines.extend(["", "## Proposed changes"])
        for change in review.changes:
            mark = " (applied)" if change.id in applied_ids else ""
            if change.kind == "generation":
                mark = " (a suggestion: change it in Settings → Generation)"
            lines.extend(["", f"### {describe(change)}{mark}"])
            if change.reason:
                lines.append(change.reason.strip())
            if change.warning:
                lines.append(f"Warning: {change.warning}")
            if change.before is not None:
                lines.append(f"Before: {format_value(change.before)}")
            elif bundle is not None and change.kind != "lore_disable":
                lines.append(f"Before: {format_value(current_value(bundle, change))}")
            lines.append(f"Proposed: {format_value(change.value)}")
    if review.director_turn:
        mark = " (sent to the composer)" if "director" in applied_ids else ""
        lines.extend(["", f"## Director turn{mark}", "", review.director_turn.strip()])
    if review.cannot_fix:
        lines.extend(["", "## Can't be fixed with settings", ""])
        lines.extend(f"- {note}" for note in review.cannot_fix)
    if review.app_findings:
        lines.extend(
            ["", findings_markdown(review).replace("# SealedLore findings", "## Findings")]
        )
    return "\n".join(lines).strip() + "\n"


def _loss_warning(before: object, after: object) -> str | None:
    """A replacement that drops much of the text it replaces, said plainly.

    The reviewer is asked to keep what isn't part of the problem word for
    word; this is the check. Measured in lines against what is there now.
    """
    old, new = _text(before) or "", _text(after) or ""
    if not old:
        return None
    old_lines = [line for line in old.splitlines() if line.strip()]
    new_lines = [line for line in new.splitlines() if line.strip()]
    removed = added = 0
    for line in difflib.ndiff(old_lines, new_lines):
        if line.startswith("- "):
            removed += 1
        elif line.startswith("+ "):
            added += 1
    if removed < LOSS_LINES or removed <= added:
        return None
    if removed / max(1, len(old_lines)) < LOSS_RATIO:
        return None
    return (
        f"this replaces {removed} of the {len(old_lines)} lines now there with "
        f"{added} new ones — check that nothing you want is being dropped"
    )


def _replaces_whole_text(change: SettingsChange) -> bool:
    fields = WHOLE_TEXT_FIELDS.get(change.kind)
    return fields is not None and change.field in fields


def parse_review(
    text: str,
    bundle: StoryBundle,
    *,
    models: Mapping[str, ModelInfo] | None = None,
    current_model: str | None = None,
    prompt_keys: Collection[str] = (),
    texts: PromptTexts | None = None,
    config: Config | None = None,
) -> SettingsReview:
    """The review from the model's reply, keeping only changes that apply.

    A prompt text may be changed only if the review was shown it
    (`prompt_keys`); `texts` are the texts in force, for its "before";
    `config` holds the generation settings a suggestion's "before" is read from.

    Each proposal is tried on a copy of the bundle; one that can't be applied
    — a character that doesn't exist, a value outside its choices, a model the
    endpoint doesn't offer — becomes a `cannot_fix` note rather than
    disappearing. Without the endpoint's model list (`models` None), a model
    suggestion can't be checked, so it is kept as advice, not applied blind.
    Raises ValueError when there is no review at all.
    """
    data = extract_json(text, dict, "review")
    review = SettingsReview(
        summary=_text(data.get("summary")) or "",
        cannot_fix=_strings(data.get("cannot_fix")),
        app_findings=_findings(data.get("app_findings")),
        director_turn=_text(data.get("director_turn")),
        history_bound=data.get("history_bound") is True,
    )
    trial = copy.deepcopy(bundle)
    proposed: list[SettingsChange] = []
    for item in _normalise_length(_list(data.get("changes"))):
        try:
            proposed.append(
                SettingsChange(
                    kind=item.get("kind"),
                    target=_text(item.get("target")),
                    field=_text(item.get("field")),
                    value=item.get("value"),
                    reason=_text(item.get("reason")) or "",
                )
            )
        except ValidationError:
            review.cannot_fix.append(
                f"A proposed change of an unknown kind ({item.get('kind')!r})."
            )
    # Renames go last, so an edit to the same card still finds it by name.
    for change in ordered_for_apply(proposed):
        if change.kind == "model" and models is None:
            review.cannot_fix.append(
                f"Suggested {describe(change)} “{change.value}”"
                + (f" ({change.reason})" if change.reason else "")
                + ", but your endpoint's model list couldn't be checked, so it isn't offered "
                "as a change. Pick it in Settings if it's right."
            )
            continue
        if change.kind == "prompt":
            if change.target not in prompt_keys:
                review.cannot_fix.append(
                    f"Proposed a change to SealedLore's text “{change.target}”, which the "
                    "review wasn't shown, so it isn't offered."
                )
                continue
            if change.value is None:
                # Back to the default: shown as the default's text, and applied
                # as a reset (writing the default is one).
                change.value = TEXTS[change.target].default
        before = (
            current_value(bundle, change, texts, config) if change.kind != "lore_disable" else None
        )
        try:
            apply_change(trial, change, models=models, current_model=current_model)
        except ValueError as exc:
            review.cannot_fix.append(f"Proposed {describe(change)}, but {exc}.")
            continue
        change.before = before
        if change.kind == "prompt":
            missing = missing_slots(change.target or "", change.value)
            if missing:
                change.warning = (
                    "this leaves out "
                    + ", ".join(f"{{{slot}}}" for slot in missing)
                    + ": what SealedLore puts there would no longer reach the model"
                )
        if _replaces_whole_text(change) or change.kind == "prompt":
            loss = _loss_warning(before, change.value)
            if loss:
                change.warning = f"{change.warning}; {loss}" if change.warning else loss
        review.changes.append(change)
    return review
