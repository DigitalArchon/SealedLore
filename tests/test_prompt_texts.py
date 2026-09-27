"""The registry of every text sent to a model (engine/prompt_texts.py)."""

from __future__ import annotations

import re
from typing import get_args

import pytest

from sealedlore.engine.prompt import AssemblyOptions, TurnRequest, assemble_prompt
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, GROUPS, TEXTS, PromptTexts
from sealedlore.engine.scene_update import build_read_messages
from sealedlore.models.character import Character
from sealedlore.models.node import AgencyMode, ResponseStyle, RollBand
from sealedlore.models.scene import SceneState
from sealedlore.models.story import (
    NarrativePerson,
    NarrativeTense,
    Perspective,
    Story,
    WorldActivity,
)

SLOT = re.compile(r"\{([a-z_]+)\}")


def test_every_text_is_in_a_known_group():
    assert {text.group for text in TEXTS.values()} <= set(GROUPS)


@pytest.mark.parametrize("key", sorted(TEXTS))
def test_every_slot_in_a_default_is_declared(key):
    """A `{word}` the program doesn't fill would reach the model as written."""
    text = TEXTS[key]
    assert set(SLOT.findall(text.default)) <= set(text.slots), key


def test_the_texts_chosen_by_name_all_exist():
    """Keys the program builds from a setting's value, which no test of a
    single story would reach all of."""
    keys = [
        *(f"storyteller.perspective.{p}" for p in get_args(Perspective)),
        *(f"turn.roster.cast_elsewhere.{p}" for p in get_args(Perspective)),
        *(f"storyteller.world.{a}" for a in get_args(WorldActivity)),
        *(f"turn.agency.{mode}" for mode in get_args(AgencyMode)),
        *(f"turn.dice.{band}" for band in get_args(RollBand)),
        *(f"length.{s}" for s in get_args(ResponseStyle) if s != "custom"),
        *(f"length.{s}.short" for s in get_args(ResponseStyle) if s != "custom"),
        *(
            f"turn.narration.{person}_{tense}"
            for person in get_args(NarrativePerson)
            for tense in get_args(NarrativeTense)
        ),
        *(f"turn.narration.{tense}" for tense in get_args(NarrativeTense)),
        *(f"plot.direction.{kind}" for kind in ("begin", "lead_in", "gap", "reveal")),
    ]
    assert [key for key in keys if key not in TEXTS] == []


def test_filling_is_one_pass():
    """A value that happens to look like a slot is never filled in turn."""
    assert DEFAULT_TEXTS.fill("turn.directives.held", held="{held}").startswith(
        "The author holds {held}."
    )


def test_a_slot_the_text_does_not_declare_is_a_bug():
    with pytest.raises(AssertionError):
        DEFAULT_TEXTS.fill("turn.directives.held", nobody="x")


# --- an edit reaches the model ----------------------------------------------------


@pytest.fixture
def cast() -> list[Character]:
    return [Character(id="c-ana", name="Ana Reyes"), Character(id="c-bo", name="Bo Lind")]


@pytest.fixture
def story() -> Story:
    story = Story(title="T")
    story.scene = SceneState(present_character_ids=["c-ana"])
    return story


def _assemble(story, cast, texts: PromptTexts):
    return assemble_prompt(
        story=story,
        cast=cast,
        history_nodes=[],
        turn=TurnRequest(speaker_id="c-ana", user_text="I wait.", controlled_character_id="c-ana"),
        options=AssemblyOptions(texts=texts),
    )


def test_an_edited_rule_replaces_the_default_in_the_system_block(story, cast):
    edited = PromptTexts({"storyteller.rules": "HOUSE RULES."})
    system = _assemble(story, cast, edited).messages[0].text
    assert "HOUSE RULES." in system
    assert DEFAULT_TEXTS["storyteller.rules"][:60] not in system


def test_an_edited_reminder_is_what_the_model_reads_last(story, cast):
    edited = PromptTexts({"turn.remember.presence": "Bring in whoever the moment wants."})
    last = _assemble(story, cast, edited).messages[-1].text
    assert "Bring in whoever the moment wants." in last.rsplit("REMEMBER:", 1)[1]


def test_an_edited_roster_line_keeps_its_names(story, cast):
    edited = PromptTexts({"turn.roster.cast_elsewhere.whole_story": "AWAY FOR NOW:"})
    story.style.perspective = "whole_story"
    story.setup.absent_at_start = ["c-bo"]  # pinned elsewhere, not the opening's to place
    scene = _assemble(story, cast, edited).section("tail.scene").text
    assert "AWAY FOR NOW:\n  - Bo Lind" in scene


def test_an_edited_side_call_prompt_is_sent(story, cast):
    edited = PromptTexts({"scene_read.system": "Read the scene. {focus}"})
    messages = build_read_messages(
        scene=story.scene,
        cast=cast,
        supporting=[],
        held=cast[0],
        author_turn="I wait.",
        passage="Nothing happens.",
        texts=edited,
    )
    assert messages[0].text.startswith("Read the scene. The scene is wherever Ana Reyes")
