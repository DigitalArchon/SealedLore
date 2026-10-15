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


def test_the_read_s_sense_of_scale_is_kept_within_reason():
    """A vast castle got a building's 40 m and the four-tile box that goes
    with it (the author's castle, Oct 15 2026): the read now says about how
    wide a new place is, and the map keeps it, clamped to its parent."""
    world = farm_and_town()
    delta, refused = resolve(
        world,
        {
            "places": [
                {
                    "name": "The Great Engine's castle",
                    "kind": "site",
                    "inside": "Hellsville",
                    "size": 200,
                    "quote": "Jane heads north",
                },
                {"name": "A hut", "kind": "building", "size": "5 m", "quote": "Jane heads north"},
                {"name": "Nowhere", "kind": "site", "size": -3, "quote": "Jane heads north"},
                {"name": "Everywhere", "kind": "region", "size": 1e9, "quote": "Jane heads north"},
                {"name": "A barn", "kind": "building", "size": True, "quote": "Jane heads north"},
            ]
        },
    )
    assert refused == []
    by = {p.name: p for p in delta.places}
    assert by["The Great Engine's castle"].size == 200 and by["A hut"].size == 5
    assert by["Nowhere"].size is None and by["Everywhere"].size is None
    assert by["A barn"].size is None
    after = applied(world, delta)
    castle = next(p for p in after.places.values() if p.name.endswith("castle"))
    assert castle.size == 200 and castle.inside == world_place(after, "Hellsville")
    hut = next(p for p in after.places.values() if p.name == "A hut")
    assert hut.size == 5


def test_a_vehicle_moves_to_where_the_passage_docks_it():
    """Once in a bay a ship stayed there for good (the review, Oct 15 2026):
    a vehicle has no spot of its own and goes where it is next shown."""
    world = WorldMap()
    text = "The Sable Crest sat in docking bay three. Later it landed at Kendra spaceport."
    delta, _refused = resolve(
        world,
        {
            "places": [
                {"name": "Docking bay three", "kind": "room", "quote": "sat in docking bay three"},
                {
                    "name": "Sable Crest",
                    "kind": "vehicle",
                    "inside": "Docking bay three",
                    "quote": "sat in docking bay three",
                },
            ]
        },
        text,
    )
    world = applied(world, delta)
    ship = next(p for p in world.places.values() if p.name == "Sable Crest")
    bay = next(p for p in world.places.values() if p.name == "Docking bay three")
    assert ship.inside == bay.id
    delta, _refused = resolve(
        world,
        {
            "places": [
                {"name": "Kendra spaceport", "kind": "site", "quote": "landed at Kendra spaceport"},
                {
                    "name": "Sable Crest",
                    "kind": "vehicle",
                    "inside": "Kendra spaceport",
                    "quote": "landed at Kendra spaceport",
                },
            ]
        },
        text,
    )
    world = applied(world, delta)
    port = next(p for p in world.places.values() if p.name == "Kendra spaceport")
    assert world.places[ship.id].inside == port.id
    # A place with a spot of its own is never moved, as before.
    delta, _refused = resolve(
        world,
        {
            "places": [
                {
                    "name": "Docking bay three",
                    "inside": "Kendra spaceport",
                    "quote": "sat in docking bay three",
                }
            ]
        },
        text,
    )
    assert applied(world, delta).places[bay.id].inside == bay.inside


def test_someone_in_the_story_is_a_name_whatever_it_looks_like():
    """ "The Hollow King" was refused as a description (the review, Oct 15
    2026): a name form of someone in the story is that person."""
    world = farm_and_town()
    king = Character(id="c-king", name="The Hollow King")
    delta, refused = resolve_reply(
        world,
        {
            "where": [
                {
                    "who": "The Hollow King",
                    "place": "Hellsville",
                    "quote": "Marcus Webb waited at the diner",
                }
            ]
        },
        source=SOURCE,
        people=[*PEOPLE, king],
    )
    assert "The Hollow King" in delta.where and refused == []
    _delta, refused = resolve_reply(
        world,
        {
            "where": [
                {
                    "who": "The woman",
                    "place": "Hellsville",
                    "quote": "A woman watched from the porch",
                }
            ]
        },
        source=SOURCE,
        people=[*PEOPLE, king],
    )
    assert refused == ["where The woman: not a name"]


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


def test_the_map_is_applied_the_moment_its_read_returns(tmp_path: Path, story, cast):
    """Live, a scene read that ran away held the map back 68 seconds though
    its own read was done (the author's castle, Oct 15 2026): every read
    goes out at once and each is applied as it comes back, so the map's
    notice comes before a slow scene read's."""
    import threading
    import time

    scene_reply = json.dumps({"location": "The yard", "arrived": [], "left": [], "shortcuts": []})

    class SlowScene(MockChatProvider):
        """The scene read's line, slow; the map read on a sibling, quick."""

        def __init__(self) -> None:
            super().__init__([FIRST_PASSAGE, scene_reply])
            self.side = MockChatProvider([FIRST_READ])
            self.calls = 0

        def complete(self, request):
            self.calls += 1
            # The scene read: the passage streams, so this is the first `complete`.
            if self.calls == 1:
                time.sleep(0.4)
            return super().complete(request)

        def sibling(self):
            return self.side

    session = make_session(tmp_path, story, cast, [])
    session.config.scene_reads = "every_turn"
    session.provider = SlowScene()
    started = time.monotonic()
    order: list[tuple[str, float]] = []
    for event in session.send(turn()):
        if isinstance(event, SessionNotice) and event.text.split(":")[0] in ("Map", "Scene"):
            order.append((event.text.split(":")[0], time.monotonic() - started))
    assert [kind for kind, _ in order] == ["Map", "Scene"]
    assert order[0][1] < order[1][1]
    assert session.world().where  # the map read was applied
    assert session.path()[-1].meta.scene is not None  # and the scene read after it
    assert session.map_reading is None
    assert threading.active_count() >= 1


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


ADA_READ = reply(
    places=[{"name": "The yard", "kind": "area", "quote": "climbed up into the cold yard"}],
    where=[{"who": "Ada", "place": "The yard", "quote": "climbed up into the cold yard"}],
)


def test_a_simple_chat_keeps_a_map_when_asked(tmp_path: Path, story, cast):
    """The author (Oct 2026): a chat may keep a map as a story does. A chat
    has no cast: the read is given the user's name alone (never the chat's
    stray cards), told who "I" and "you" are, and the map goes in the chat's
    prompt right before the message, as in a story's tail."""
    from sealedlore.engine.prompt_texts import DEFAULT_TEXTS
    from sealedlore.models.node import CHAT_USER_ID

    story.mode = "chat"
    story.chat_prompt = "You run a text adventure."
    story.chat_character = "Ada"
    session = make_session(tmp_path, story, cast, [FIRST_PASSAGE, ADA_READ, "You look.", "{}"])
    assert session.map_on and session.chat_name == "Ada"
    list(session.send(TurnRequest(speaker_id=CHAT_USER_ID, user_text="I open the cellar door.")))
    read = session.provider.requests[-1]
    assert read.model == "small/scene-model"
    text = read.messages[-1].text
    assert "THE USER'S MESSAGE:\nI open the cellar door." in text
    assert "PEOPLE: Ada\n" in text  # the chat's stray cards are nobody's business
    assert "The author's character: Ada\n" in text
    assert DEFAULT_TEXTS.fill("map_read.chat", name="Ada") in text
    world = session.world()
    assert world.where == {"Ada": world_place(world, "The yard")}

    list(session.send(TurnRequest(speaker_id=CHAT_USER_ID, user_text="I look around.")))
    turn_sent = session.provider.requests[-2].messages[-1].text  # the read is last
    assert turn_sent.index("# THE MAP HERE") < turn_sent.index("I look around.")
    assert "Ada is at: The yard" in turn_sent
    names = [section.name for section in session.last_prompt.sections]
    assert names.index("tail.map") < names.index("tail.author_turn")
    # The message is kept as written: the map is never in the history.
    assert all("THE MAP HERE" not in node.content for node in session.path())


def test_a_chat_that_names_nobody_is_tracked_as_you(tmp_path: Path, story, cast):
    from sealedlore.models.node import CHAT_USER_ID

    story.mode = "chat"
    you = reply(
        places=[{"name": "The yard", "kind": "area", "quote": "climbed up into the cold yard"}],
        where=[{"who": "You", "place": "The yard", "quote": "climbed up into the cold yard"}],
    )
    session = make_session(tmp_path, story, cast, [FIRST_PASSAGE, you, "You look.", "{}"])
    assert session.chat_name == "You"
    list(session.send(TurnRequest(speaker_id=CHAT_USER_ID, user_text="I go up.")))
    assert "The author's character: You\n" in session.provider.requests[-1].messages[-1].text
    world = session.world()
    assert world.where == {"You": world_place(world, "The yard")}
    assert session.map_held_name(None) == "You"
    list(session.send(TurnRequest(speaker_id=CHAT_USER_ID, user_text="I look around.")))
    assert "You are at: The yard" in session.provider.requests[-2].messages[-1].text


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


def test_what_names_a_place_no_longer_on_the_map_is_pruned():
    """A take switched moves what followed under the other take, whose map
    lacks the places the first made; live, the view indexed one and the
    transcript's reload died (Oct 15 2026)."""
    from sealedlore.models.world import WorldDelta, WorldLink, WorldPlace, WorldThing

    world = farm_and_town()
    farm = next(iter(world.places.values()))
    ghost = "no-such-place"
    delta = WorldDelta(
        places=[
            WorldPlace(id="shed", name="The shed", kind="building", inside=ghost, x=1.0, y=1.0)
        ],
        links=[WorldLink(a=farm.id, b=ghost)],
        where={"Jane": ghost, "Marcus Webb": farm.id},
        things=[WorldThing(id="axe", name="Axe", at=ghost)],
        seen=[ghost],
    )
    after = applied(world, delta)
    assert after.where == {"Marcus Webb": farm.id}
    assert all(ghost not in (k.a, k.b) for k in after.links)
    assert after.things["axe"].at is None and after.places["shed"].inside is None
    assert ghost not in after.seen and ghost not in after.visited.get("Jane", [])
    assert render_map_context(after, "Jane") == ""  # unplaced, not crashed
