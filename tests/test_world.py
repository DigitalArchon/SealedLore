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
from sealedlore.engine.session import SessionNotice, StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.world import (
    applied,
    describe_delta,
    render_map,
    render_map_context,
    resolve_reply,
    world_at,
)
from sealedlore.models.character import Character
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.world import WorldMap
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import (
    StoryBundle,
    load_story_bundle,
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


def resolve(world: WorldMap, reply: dict, source: str = SOURCE):
    return resolve_reply(world, reply, source=source, people=PEOPLE)


def farm_and_town() -> WorldMap:
    delta, refused = resolve(
        WorldMap(),
        {
            "places": [
                {"name": "The farmhouse", "kind": "building", "quote": "Jane heads north"},
                {"name": "Hellsville", "kind": "settlement", "quote": "toward Hellsville"},
            ],
            "links": [
                {
                    "from": "the farmhouse",
                    "to": "Hellsville",
                    "direction": "north",
                    "distance": "five miles",
                    "quote": "Jane heads north on foot toward Hellsville, five miles away.",
                }
            ],
        },
    )
    assert refused == []
    return applied(WorldMap(), delta)


# --- the reply, held to account ------------------------------------------------


def test_an_entry_stands_only_on_a_quote_found_in_the_turn_or_passage():
    delta, refused = resolve(
        WorldMap(),
        {
            "places": [
                {"name": "The diner", "quote": "Marcus Webb waited at the diner"},
                {"name": "The lighthouse", "quote": "A lighthouse stood on the cliff."},
                {"name": "The church"},
            ]
        },
    )
    assert [p.name for p in delta.places] == ["The diner"]
    assert len(refused) == 2


def test_one_person_one_name_and_a_description_is_nobody():
    world = farm_and_town()
    delta, refused = resolve(
        world,
        {
            "where": [
                {"who": "Marcus", "place": "Hellsville", "quote": "Marcus Webb waited"},
                {"who": "the woman", "place": "Hellsville", "quote": "A woman watched"},
                {"who": "the player's character", "place": "Hellsville", "quote": "Jane heads"},
            ]
        },
    )
    assert delta.where == {"Marcus Webb": world_place(world, "Hellsville")}
    assert len(refused) == 2


def world_place(world: WorldMap, name: str) -> str:
    return next(p.id for p in world.places.values() if p.name == name)


def test_a_stated_distance_is_kept_and_the_disagreement_noted():
    world = farm_and_town()
    delta, _ = resolve(
        world,
        {
            "links": [
                {
                    "from": "The farmhouse",
                    "to": "Hellsville",
                    "direction": "north",
                    "distance": "three miles",
                    "quote": "The road north was a grey crease",
                }
            ]
        },
    )
    after = applied(world, delta)
    assert [link.distance for link in after.links] == ["five miles"]
    assert len(after.links) == 1
    assert "five miles" in delta.conflicts[0] and "three miles" in delta.conflicts[0]


def test_a_direction_its_own_quote_does_not_carry_is_dropped():
    delta, _ = resolve(
        WorldMap(),
        {
            "places": [
                {"name": "The diner", "quote": "Marcus Webb waited at the diner"},
                {"name": "The porch", "quote": "A woman watched from the porch"},
            ],
            "links": [
                {
                    "from": "The diner",
                    "to": "The porch",
                    "direction": "east",
                    "quote": "A woman watched from the porch.",
                }
            ],
        },
    )
    assert delta.links[0].direction is None


def test_a_place_never_ends_up_inside_itself():
    world = farm_and_town()
    delta, _ = resolve(
        world,
        {
            "places": [
                {"name": "The diner", "inside": "Hellsville", "quote": "Marcus Webb waited"},
                {"name": "Hellsville", "inside": "The diner", "quote": "toward Hellsville"},
            ]
        },
    )
    after = applied(world, delta)
    town = after.places[world_place(after, "Hellsville")]
    diner = after.places[world_place(after, "The diner")]
    assert diner.inside == town.id
    assert town.inside is None
    assert "  - The diner" in render_map(after)


def test_things_change_hands_and_keep_one_entry():
    world = farm_and_town()
    first, _ = resolve(
        world,
        {
            "things": [
                {
                    "name": "Folded map",
                    "at": "Hellsville",
                    "state": "folded",
                    "quote": "a folded map",
                }
            ]
        },
    )
    world = applied(world, first)
    second, _ = resolve(
        world,
        {
            "things": [
                {"name": "the folded map", "at": "carried by Jane", "quote": "she pocketed it"}
            ]
        },
    )
    world = applied(world, second)
    assert len(world.things) == 1
    (thing,) = world.things.values()
    assert thing.carried_by == "Jane" and thing.at is None and thing.state == "folded"


def test_a_name_in_a_script_with_no_case_is_a_name():
    world = farm_and_town()
    source = SOURCE + " 田中 waited at the farmhouse."
    delta, refused = resolve(
        world,
        {
            "where": [
                {"who": "田中", "place": "the farmhouse", "quote": "田中 waited at the farmhouse"},
                {"who": "the woman", "place": "the farmhouse", "quote": "A woman watched"},
            ]
        },
        source,
    )
    assert "田中" in delta.where and refused == ["where the woman: not a name"]


def test_the_map_here_names_the_author_s_character_or_says_so():
    world = farm_and_town()
    world.where["Jane"] = world.places[next(iter(world.places))].id
    assert "Jane is at: The farmhouse" in render_map_context(world, "Jane")
    world.start = world.where["Jane"]
    unheld = render_map_context(world, None)  # a Question before anyone is held
    assert "The author's character is at: The farmhouse" in unheld and "None" not in unheld
    # A thing nobody carries is not "here" for want of anyone held: the review
    # found every thing on the map listed so.
    from sealedlore.models.world import WorldThing

    town = next(p.id for p in world.places.values() if p.name == "Hellsville")
    world.things["lark"] = WorldThing(id="lark", name="The airship", at=town)
    assert "The airship" not in render_map_context(world, None)


def test_the_notice_says_what_changed():
    world = farm_and_town()
    delta, _ = resolve(
        world,
        {
            "places": [{"name": "The diner", "inside": "Hellsville", "quote": "at the diner"}],
            "where": [{"who": "Jane", "place": "The diner", "quote": "to Jane"}],
        },
    )
    assert describe_delta(world, delta) == "new The diner; Jane at The diner"


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


def test_a_passage_is_read_onto_the_map(session: StorySession, tmp_path: Path):
    events = list(session.send(turn()))

    passage = session.nodes[-1]
    assert passage.meta.world is not None
    world = session.world()
    assert sorted(p.name for p in world.places.values()) == ["The cellar", "The yard"]
    assert world.where == {"Serrik Vaun": world_place(world, "The yard")}
    assert world.visited["Serrik Vaun"] == [world_place(world, "The yard")]
    notices = [e.text for e in events if isinstance(e, SessionNotice)]
    assert "Map: new The cellar, The yard; Serrik Vaun at The yard" in notices

    read = session.provider.requests[-1]
    assert read.model == "small/scene-model"  # the map model falls back to the scene model
    assert "THE AUTHOR'S TURN, AS SERRIK VAUN" in read.messages[-1].text
    assert "PEOPLE: Serrik Vaun, Maela Orr, Captain Idris" in read.messages[-1].text
    kinds = [entry["kind"] for entry in read_api_log(session.story.id, root=tmp_path)]
    assert kinds == ["request", "response", "world_request", "world_response"]
    assert len(session.unreported_usage) == 1


def test_the_map_model_is_its_own_when_set(session: StorySession):
    session.config.map_model = "map/reader"
    session.config.map_art_model = None
    list(session.send(turn()))
    assert session.provider.requests[-1].model == "map/reader"
    assert session.map_art_model == "map/reader"


def test_the_next_read_sees_the_map_so_far(session: StorySession):
    list(session.send(turn()))
    session.provider = MockChatProvider(["Serrik crossed the yard to the gate.", reply()])
    list(session.send(turn("I go to the gate.")))

    shown = session.provider.requests[-1].messages[-1].text
    assert "- The yard (area)" in shown
    assert "- The cellar -> The yard: up" in shown
    assert "- Serrik Vaun: The yard" in shown


def test_the_map_is_kept_with_the_story(session: StorySession, tmp_path: Path):
    list(session.send(turn()))
    reloaded = load_story_bundle(session.story.id, root=tmp_path)
    assert sorted(p.name for p in world_at(reloaded.nodes).places.values()) == [
        "The cellar",
        "The yard",
    ]


def test_each_branch_has_its_own_map(session: StorySession):
    list(session.send(turn()))
    first = session.nodes[-1]
    session.provider = MockChatProvider(
        [
            "Serrik went back down into the cellar and found a tunnel.",
            reply(
                places=[{"name": "The tunnel", "quote": "found a tunnel"}],
                where=[{"who": "Serrik", "place": "The tunnel", "quote": "found a tunnel"}],
            ),
        ]
    )
    list(session.send(turn("I go back down.")))
    assert "The tunnel" in {p.name for p in session.world().places.values()}
    assert "The tunnel" not in {p.name for p in session.world(first.id).places.values()}


def test_a_failed_read_never_costs_the_turn(session: StorySession):
    session.provider = MockChatProvider([FIRST_PASSAGE, "I'm not sure what changed."])
    events = list(session.send(turn()))

    assert session.nodes[-1].content == FIRST_PASSAGE
    assert session.nodes[-1].meta.world is None
    assert session.world().places == {}
    notices = [e.text for e in events if isinstance(e, SessionNotice)]
    assert any(n.startswith("Couldn't read the map from this passage") for n in notices)


def test_refused_entries_are_logged(session: StorySession, tmp_path: Path):
    session.provider = MockChatProvider(
        [FIRST_PASSAGE, reply(places=[{"name": "Atlantis", "quote": "It sank long ago."}])]
    )
    list(session.send(turn()))
    log = read_api_log(session.story.id, root=tmp_path)
    refused = [entry for entry in log if entry["kind"] == "world_refused"]
    assert refused and "Atlantis" in refused[0]["refused"][0]


def test_with_the_map_off_nothing_is_read(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, [FIRST_PASSAGE], on=False)
    list(session.send(turn()))
    kinds = [entry["kind"] for entry in read_api_log(session.story.id, root=tmp_path)]
    assert kinds == ["request", "response"]
    assert not session.map_on


def test_a_simple_chat_never_keeps_a_map(tmp_path: Path, story, cast):
    story.mode = "chat"
    session = make_session(tmp_path, story, cast, [FIRST_PASSAGE])
    assert not session.map_on


def test_a_new_playthrough_keeps_the_map_on(story, cast):
    from sealedlore.storage.scenario import bundle_from_scenario, scenario_from_bundle

    story.defaults.world_map = True
    scenario = scenario_from_bundle(StoryBundle(story=story, cast=cast))
    assert scenario.world_map
    assert bundle_from_scenario(scenario).story.defaults.world_map


def test_a_way_the_passage_gave_no_words_for_has_none():
    from sealedlore.engine.world import applied, resolve_reply, world_at

    world = world_at([])
    source = "She crossed the frozen yard to the farmhouse."
    reply = {
        "places": [
            {"name": "frozen yard", "quote": "the frozen yard"},
            {"name": "farmhouse", "quote": "the farmhouse"},
        ],
        "links": [
            {
                "from": "frozen yard",
                "to": "farmhouse",
                "direction": "Unknown",
                "distance": "n/a",
                "quote": "crossed the frozen yard to the farmhouse",
            }
        ],
    }
    delta, _ = resolve_reply(world, reply, source=source, people=[])
    (way,) = applied(world, delta).links
    assert way.direction is None and way.distance is None



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
