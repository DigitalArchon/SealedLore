"""The map that grows as the story goes (models/world.py, engine/world.py,
engine/session_world.py).

The read names places and people; code resolves the names and holds every
entry to a quote. What the replays in SealedLoreDocs design/map.md found the
read getting wrong is checked here: one person under two names, a person who
is only a description, a stated distance overwritten, a direction its quote
doesn't carry, a place made to sit inside itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.character import Character
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.world import WorldMap
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import (
    StoryBundle,
    read_api_log,
    save_story_bundle,
)

SOURCE = (
    "Jane heads north on foot toward Hellsville, five miles away.\n\n"
    "The road north was a grey crease between white fields. Marcus Webb waited at "
    "the diner, a lantern-lit room on Hellsville's main street. A woman watched from "
    "the porch. Marcus slid a folded map across the table to Jane, and she pocketed it."
)
PEOPLE = [Character(id="c-jane", name="Jane"), Character(id="c-marcus", name="Marcus Webb")]


# --- the reply, held to account ------------------------------------------------


def world_place(world: WorldMap, name: str) -> str:
    return next(p.id for p in world.places.values() if p.name == name)


# --- the session ---------------------------------------------------------------


def reply(**fields) -> str:
    return json.dumps(fields)


FIRST_PASSAGE = "Serrik shouldered the cellar door open and climbed up into the cold yard."
FIRST_READ = reply(
    places=[
        {"name": "The cellar", "kind": "room", "quote": "shouldered the cellar door open"},
        {"name": "The yard", "kind": "area", "quote": "climbed up into the cold yard"},
    ],
    links=[
        {
            "from": "The cellar",
            "to": "The yard",
            "direction": "up",
            "quote": "climbed up into the cold yard",
        }
    ],
    where=[{"who": "Serrik", "place": "The yard", "quote": "climbed up into the cold yard"}],
)


def make_session(tmp_path: Path, story, cast, responses: list[str], *, on: bool = True):
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    story.defaults.world_map = on
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    config = Config(
        providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1")],
        active_provider_name="nano",
        min_cacheable_tokens=1,
        scene_model="small/scene-model",
        scene_reads="manual",
        map_art=False,  # drawn inline with the mock; tests/test_world_art.py
    )
    return StorySession(
        bundle,
        config,
        MockChatProvider(responses),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def turn(text: str = "I put my shoulder to the door.") -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text=text, controlled_character_id="char-serrik"
    )


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    return make_session(tmp_path, story, cast, [FIRST_PASSAGE, FIRST_READ])


def test_with_the_map_off_nothing_is_read(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, [FIRST_PASSAGE], on=False)
    list(session.send(turn()))
    kinds = [entry["kind"] for entry in read_api_log(session.story.id, root=tmp_path)]
    assert kinds == ["request", "response"]
    assert not session.map_on


ADA_READ = reply(
    places=[{"name": "The yard", "kind": "area", "quote": "climbed up into the cold yard"}],
    where=[{"who": "Ada", "place": "The yard", "quote": "climbed up into the cold yard"}],
)


def test_a_new_playthrough_keeps_the_map_on(story, cast):
    from sealedlore.storage.scenario import bundle_from_scenario, scenario_from_bundle

    story.defaults.world_map = True
    scenario = scenario_from_bundle(StoryBundle(story=story, cast=cast))
    assert scenario.world_map
    assert bundle_from_scenario(scenario).story.defaults.world_map


def test_country_is_as_far_as_its_edge():
    """The review: a village at the edge of a 12 km forest was told the forest
    lay 8.2 km north, its middle. An area or a region is reached at its edge."""
    from sealedlore.engine.world import _how_far
    from sealedlore.models.world import WorldPlace

    village = WorldPlace(id="v", name="Fernhollow", kind="settlement", x=0.0, y=0.0, size=800)
    forest = WorldPlace(id="f", name="The Silverwood", kind="area", x=2000.0, y=8000.0, size=12000)
    city = WorldPlace(id="c", name="Sylvaranth", kind="settlement", x=0.0, y=14000.0, size=2000)
    world = WorldMap(places={"v": village, "f": forest, "c": city})
    fixed = {"v", "f", "c"}
    assert _how_far(world, "v", "f", fixed) == "its edge 2.2 km / 1.4 miles north"
    assert _how_far(world, "v", "c", fixed).startswith("14 km")  # a town: to its middle
    world.places["f"] = forest.model_copy(update={"size": 20000})
    assert _how_far(world, "v", "f", fixed) == "all around"
