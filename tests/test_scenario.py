"""Scenario files: the shareable base story, and nothing a playthrough produces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import Node
from sealedlore.models.scenario import Scenario
from sealedlore.models.scene import SceneState
from sealedlore.models.story import Story, StorySetup
from sealedlore.models.summary import Summary
from sealedlore.storage.repository import StoryBundle, load_story_bundle, save_story_bundle
from sealedlore.storage.scenario import (
    ScenarioError,
    bundle_from_scenario,
    read_scenario,
    scenario_from_bundle,
    write_scenario,
)


@pytest.fixture
def bundle(story, cast) -> StoryBundle:
    story.defaults.main_model = "anthropic/claude-sonnet-4.6"
    story.defaults.agency_mode = "plausible"
    story.setup = StorySetup(
        description="A heist in a ruined keep.",
        opening_text="Serrik and Maela reach the undercroft as the torches gutter.",
        starting_scene=SceneState(
            location="The gate of Calder Keep", present_character_ids=["char-serrik"]
        ),
        suggested_character_id="char-serrik",
    )
    cast[0].portrait_path = "/home/someone/serrik.png"
    lore = [LoreEntry(title="The Keep", content="Built on a fault line.", embedding_hash="abc")]
    nodes = [Node(kind="user", speaker_id="char-serrik", content="I go in.")]
    return StoryBundle(
        story=story,
        cast=cast,
        lore=lore,
        nodes=nodes,
        summaries=[Summary(content="Chapter one.", covered_node_ids=[nodes[0].id])],
    )


def test_a_scenario_carries_the_base_story(bundle: StoryBundle):
    scenario = scenario_from_bundle(bundle)

    assert scenario.title == "The Sundering"
    assert scenario.world_bible == "Calder Keep has stood empty for a hundred years."
    assert scenario.opening_text.startswith("Serrik and Maela")
    assert scenario.starting_scene.location == "The gate of Calder Keep"
    assert scenario.agency_mode == "plausible"
    assert [c.name for c in scenario.cast] == ["Serrik Vaun", "Maela Orr", "Captain Idris"]
    assert scenario.style.person == "third"


def test_a_scenario_leaves_out_the_playthrough_and_the_machine(bundle: StoryBundle, tmp_path: Path):
    path = tmp_path / "heist.sealedlore-scenario.json"
    write_scenario(path, scenario_from_bundle(bundle))
    raw = path.read_text()
    data = json.loads(raw)

    for key in ("nodes", "summaries", "asides", "embeddings", "defaults", "active_leaf_id"):
        assert key not in data
    assert "I go in." not in raw
    assert "anthropic/claude-sonnet-4.6" not in raw
    assert "/home/someone" not in raw
    assert data["lore"][0]["embedding_hash"] is None


def test_the_current_scene_stands_in_for_a_missing_start(bundle: StoryBundle):
    bundle.story.setup.starting_scene = SceneState()

    scenario = scenario_from_bundle(bundle)

    assert scenario.starting_scene.location == "The undercroft beneath Calder Keep"


def test_round_trip_through_a_file_makes_a_fresh_unplayed_story(
    bundle: StoryBundle, tmp_path: Path
):
    path = tmp_path / "heist.sealedlore-scenario.json"
    write_scenario(path, scenario_from_bundle(bundle))

    fresh = bundle_from_scenario(read_scenario(path), main_model="some/model")

    assert fresh.story.id != bundle.story.id
    assert fresh.nodes == [] and fresh.summaries == []
    assert fresh.story.active_leaf_id is None
    assert fresh.story.world_bible == bundle.story.world_bible
    assert fresh.story.setup.opening_text == bundle.story.setup.opening_text
    assert fresh.story.scene.location == "The gate of Calder Keep"
    assert fresh.story.defaults.main_model == "some/model"
    assert fresh.story.defaults.agency_mode == "plausible"
    assert [c.id for c in fresh.cast] == [c.id for c in bundle.cast]
    assert fresh.lore[0].content == "Built on a fault line."


def test_the_scene_and_its_starting_point_are_separate_copies(bundle: StoryBundle):
    fresh = bundle_from_scenario(scenario_from_bundle(bundle))

    fresh.story.scene.present_character_ids.append("char-maela")

    assert fresh.story.setup.starting_scene.present_character_ids == ["char-serrik"]


def test_unknown_characters_in_a_hand_edited_file_are_dropped():
    scenario = Scenario(
        title="Edited",
        starting_scene=SceneState(present_character_ids=["char-ghost"]),
        suggested_character_id="char-ghost",
    )

    fresh = bundle_from_scenario(scenario)

    assert fresh.story.scene.present_character_ids == []
    assert fresh.story.setup.suggested_character_id is None


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not json", "isn't valid JSON"),
        ('{"title": "A story"}', "isn't a SealedLore scenario"),
        ('{"format": "sealedlore-scenario", "version": 99, "title": "x"}', "version 99"),
        ('{"format": "sealedlore-scenario", "version": 1}', "title"),
    ],
)
def test_bad_files_are_refused_in_plain_words(tmp_path: Path, content: str, message: str):
    path = tmp_path / "bad.json"
    path.write_text(content)

    with pytest.raises(ScenarioError, match=message):
        read_scenario(path)


def test_an_old_story_loads_with_an_empty_setup(tmp_path: Path):
    story = Story(title="Before scenarios")
    save_story_bundle(StoryBundle(story=story), root=tmp_path)
    story_file = tmp_path / "stories" / story.id / "story.json"
    data = json.loads(story_file.read_text())
    del data["setup"]
    story_file.write_text(json.dumps(data))

    loaded = load_story_bundle(story.id, root=tmp_path)

    assert loaded.story.setup == StorySetup()


def test_supporting_cards_travel_but_suggestions_do_not(bundle: StoryBundle):
    from sealedlore.models.character import Character
    from sealedlore.models.supporting import CharacterSuggestion

    bundle.supporting.characters.append(Character(name="Corporal Patel", origin="model"))
    bundle.supporting.suggestions.append(CharacterSuggestion(name="Hollis"))
    bundle.supporting.dismissed.append("wade")

    fresh = bundle_from_scenario(scenario_from_bundle(bundle))

    assert [c.name for c in fresh.supporting.characters] == ["Corporal Patel"]
    assert fresh.supporting.suggestions == [] and fresh.supporting.dismissed == []


def test_a_scene_carried_into_a_scenario_is_a_fresh_card(bundle: StoryBundle):
    """The live scene is a snapshot from this playthrough: who set it and when
    mean nothing in another one."""
    bundle.story.setup.starting_scene = SceneState()
    bundle.story.scene.source = "story"
    bundle.story.scene.set_at_node_id = bundle.nodes[0].id

    starting = scenario_from_bundle(bundle).starting_scene

    assert starting.source == "author"
    assert starting.changes == []
    assert starting.set_at_node_id is None
    assert starting.location == "The undercroft beneath Calder Keep"
